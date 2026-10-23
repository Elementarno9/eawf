"""``eawf doctor`` check implementations.

Each public ``check_*`` returns a :class:`CheckResult` answering one facet of
"is this install workable?". :func:`run_all` assembles the canonical set;
:mod:`eawf.surfaces.cli.commands.doctor` formats it and takes the
highest-severity status as its exit code.

Severity convention: ``fail`` means blocking and is reserved for faults that
make the install wrong rather than degraded — a hard tool missing, or an
AGENTS.md past the byte cap where the truncated tail is silently unread.

The card and policy byte-cap checks live in the sibling
:mod:`eawf.observability.doctor.checks_steering_cap` and are re-exported
below so callers keep addressing them as ``checks.check_agents_md_byte_cap``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import Field, TypeAdapter, ValidationError

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.kernel.config.layered import get_dotted, merge_config
from eawf.kernel.config.registry import LEAF_KEY_REGISTRY
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.observability.doctor import daemon_checks
from eawf.observability.doctor.checks_authority import check_authority_epoch
from eawf.observability.doctor.checks_gitignore import check_gitignore_block
from eawf.observability.doctor.checks_steering_cap import (
    CODEX_PROJECT_DOC_BYTE_CAP as CODEX_PROJECT_DOC_BYTE_CAP,
)
from eawf.observability.doctor.checks_steering_cap import (
    check_agents_md_byte_cap as check_agents_md_byte_cap,
)
from eawf.observability.doctor.checks_steering_cap import (
    check_agents_override_byte_cap as check_agents_override_byte_cap,
)
from eawf.observability.doctor.models import CheckResult as CheckResult
from eawf.observability.doctor.models import CheckStatus as CheckStatus
from eawf.platform.install.instrument_probe import probe
from eawf.platform.profiles.discovery import discover_profile
from eawf.platform.profiles.loader import list_profiles
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.surfaces.render.drift import detect_drift
from eawf.surfaces.render.envelope import OutputEnvelope, from_markdown, to_markdown
from eawf.surfaces.render.manifest import Manifest
from eawf.surfaces.render.manifest import load as load_manifest

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from eawf.runtime.daemon.service_install import SupervisedAgentReport

logger = logging.getLogger(__name__)


# Compatibility seam: callers and tests historically patched this name on
# ``checks``. The public wrapper below reads it at call time.
_probe_running_daemon_version = daemon_checks.probe_running_daemon_version


def _resolve_anchor(workspace: Path | None) -> Path | None:
    """Resolve the workspace anchor (the ``.ea/`` parent directory).

    When *workspace* is provided (``-w/--workspace``) it is returned verbatim.
    When it is ``None`` -- the default for a plain ``eawf doctor`` -- the
    resolver walks UPWARD from the process cwd looking for the nearest
    ancestor that contains a ``.ea/`` directory, mirroring the pwd-upward
    behaviour of :func:`eawf.kernel.state.resolve.resolve_with_reason`.

    Returns the anchor directory, or ``None`` when no ``.ea/`` ancestor
    exists (a truly un-initialised tree).
    """
    if workspace is not None:
        return Path(workspace)
    cur = Path.cwd().resolve()
    for directory in [cur, *cur.parents]:
        if (directory / ".ea").is_dir():
            return directory
    return None


def _default_probe_cache(workspace: Path) -> Path:
    """Return the canonical cache file path under ``<workspace>/.ea/``."""
    return workspace / ".ea" / "instrument-probe.json"


def _resolve_probe_cache_path(anchor: Path | None) -> Path:
    """Return a per-user probe-cache path: ``EA_INSTRUMENT_PROBE`` or ``~/.eawf/cache/``.

    The probe answers a host-wide "are these tools on PATH?" question, so its
    cache is machine-local, not project state — writing it under the anchor
    dropped a stray artifact into whatever directory doctor happened to
    resolve. *anchor* is accepted for symmetry with the other anchor-aware
    helpers and deliberately unused.
    """
    override = os.environ.get("EA_INSTRUMENT_PROBE")
    if override:
        return Path(override)
    return Path.home() / ".eawf" / "cache" / "instrument-probe.json"


def _is_plugin_owned(entry: object) -> bool:
    """Return ``True`` when *entry* is a whole-file plugin render, not a region.

    Plugin artifacts ARE the file — they carry no ``EAWF:BEGIN`` markers — so
    the region drift detector would report every one of them ``missing``. They
    are satisfied by the plugin cache rather than a local re-render, and the
    local render may not exist at all when eawf runs from an installed plugin.

    The discriminator is the ``plugin.`` region-id prefix backed by an
    ``eawf-plugin-`` generator — both must agree so a hand-crafted region id
    cannot accidentally skip the check.
    """
    region_id = getattr(entry, "region_id", "")
    generator = getattr(entry, "generator", "")
    return region_id.startswith("plugin.") and generator.startswith("eawf-plugin-")


def check_tools_available(
    *,
    workspace: Path,
    profile_ids: list[str] | None = None,
    reprobe: bool = False,
    cache_path: Path | None = None,
) -> CheckResult:
    """Run the instrument probe and project its results onto a single status.

    The result rolls every ``ProbeResult`` up to one status:

    - ``ok`` — every probe returned ``ok``.
    - ``warn`` — at least one soft probe failed (no hard fails).
    - ``fail`` — at least one hard probe failed.

    The function lets :class:`eawf.surfaces.cli.errors.UserError`
    (``kind="InstrumentMissing"``) escape so the CLI surface can map it to
    exit code ``6``. Callers that only want the
    snapshot view (no abort) should call :func:`eawf.platform.install.instrument_probe.probe`
    directly with a guard.
    """
    if profile_ids is None:
        profile_ids = ["core"]
    cache = cache_path if cache_path is not None else _default_probe_cache(workspace)
    report = probe(profile_ids, cache_path=cache, reprobe=reprobe)

    statuses = {r.status for r in report.results}
    if "fail" in statuses:
        # Should be unreachable: a hard fail would have raised
        # UserError (kind="InstrumentMissing") inside ``probe``. Soft fails
        # do not exist (they are reclassified to ``warn``). Keep the branch
        # for forward-compat.
        return CheckResult(
            name="tools_available",
            status="fail",
            detail="one or more hard tools missing",
        )
    if "warn" in statuses:
        soft_missing = [r.name for r in report.results if r.status == "warn"]
        return CheckResult(
            name="tools_available",
            status="warn",
            detail=f"soft tool(s) absent: {', '.join(soft_missing)}",
        )
    return CheckResult(
        name="tools_available",
        status="ok",
        detail=f"{len(report.results)} probes ok",
    )


def check_state_present(*, workspace: Path | None) -> CheckResult:
    """Return ``ok`` iff ``state.json`` or a born epoch-2 tree resolves at *workspace*.

    *workspace* may be ``None`` to defer to the resolver's pwd-upward walk
    (which is the default behaviour when ``--workspace`` is not supplied).
    A tree ``eawf init`` bore has no ``state.json``: its selected
    generation is its state.
    """
    from eawf.kernel.state.epoch2.authority import resolve_authority

    state_path, reason = resolve_with_reason(workspace=workspace)
    if state_path.exists():
        return CheckResult(
            name="state_present",
            status="ok",
            detail=f"state.json found at {state_path} (via {reason})",
        )
    authority = resolve_authority(state_path.parent)
    if authority.epoch == 2:
        return CheckResult(
            name="state_present",
            status="ok",
            detail=(
                f"epoch-2 tree at {state_path.parent} reads generation "
                f"{authority.generation_id} (via {reason})"
            ),
        )
    return CheckResult(
        name="state_present",
        status="warn",
        detail=f"no state.json at {state_path}",
    )


def check_config_resolves(*, workspace: Path | None) -> CheckResult:
    """Return ``ok`` when the layered config merge succeeds.

    The merge is best-effort: any unexpected exception collapses to ``warn``
    so the doctor surface stays useful even when a layer file is malformed.
    An explicit workspace anchors both workspace and repo layers so config
    from the process cwd cannot bleed into another repository's diagnosis.
    """
    anchor = _resolve_anchor(workspace)
    repo = anchor if anchor is not None else Path.cwd()
    try:
        merged, _sources = merge_config(repo=repo, workspace=anchor)
    except Exception as exc:
        return CheckResult(
            name="config_resolves",
            status="warn",
            detail=f"config merge failed: {exc}",
        )
    enabled_profiles: list[str] = []
    profiles_section = merged.get("profiles") if isinstance(merged, dict) else None
    if isinstance(profiles_section, dict):
        raw_enabled = profiles_section.get("enabled")
        if isinstance(raw_enabled, list):
            enabled_profiles = [p for p in raw_enabled if isinstance(p, str)]
    # Resolve through the workspace-overlay-aware registry (workspace >
    # user > built-in) so a profile id defined only under
    # ``<anchor>/.ea/profiles/`` is not flagged unknown -- the same
    # registry ``resolve_enabled_profiles`` feeds the AGENTS.md renderer.
    known_ids = list_profiles(workspace=anchor)
    unknown = [p for p in enabled_profiles if p not in known_ids]
    if unknown:
        return CheckResult(
            name="config_resolves",
            status="warn",
            detail=f"unknown profile(s) enabled: {', '.join(unknown)}",
        )
    overlays: list[str] = []
    for pid in enabled_profiles:
        loc = discover_profile(pid, workspace=anchor)
        if loc.source != "builtin":
            overlays.append(f"{pid}={loc.source}")
    overlay_note = f"; overlay: {', '.join(overlays)}" if overlays else ""
    return CheckResult(
        name="config_resolves",
        status="ok",
        detail=f"{len(enabled_profiles)} profile(s) enabled{overlay_note}",
    )


def _reserved_value_is_unsupported(key: str, value: object) -> bool:
    """Return whether a reserved value claims unsafe automation behaviour."""
    return (key == "audit.fix_safe" and value is True) or (
        key.startswith("flow.auto_accept.") and value is True
    )


def check_reserved_config_keys(*, workspace: Path | None) -> CheckResult:
    """Report explicitly configured leaves that have no production effect."""
    anchor = _resolve_anchor(workspace)
    if anchor is None:
        return CheckResult(
            name="reserved_config_key",
            status="ok",
            detail="no workspace anchor; no reserved repo config to inspect",
        )
    try:
        merged, sources = merge_config(repo=anchor, workspace=anchor)
    except Exception as exc:
        return CheckResult(
            name="reserved_config_key",
            status="warn",
            detail=f"reserved config inspection skipped: {exc}",
        )

    from eawf.kernel.config.registry.leaf_catalog import DEPRECATED_LEAF_KEYS

    configured: list[tuple[str, object, str]] = []
    for key, entry in LEAF_KEY_REGISTRY.items():
        source = sources.get(key)
        if not entry.reserved or source is None or source == "built-in":
            continue
        try:
            value = get_dotted(merged, key)
        except KeyError:
            continue
        configured.append((key, value, source))
    for key in sorted(DEPRECATED_LEAF_KEYS - set(LEAF_KEY_REGISTRY)):
        source = sources.get(key)
        if source is None or source == "built-in":
            continue
        try:
            value = get_dotted(merged, key)
        except KeyError:
            continue
        configured.append((key, value, source))

    if not configured:
        return CheckResult(
            name="reserved_config_key",
            status="ok",
            detail="no reserved config keys explicitly set",
        )

    rendered = ", ".join(f"{key}={value!r} ({source})" for key, value, source in configured)
    unsupported = [
        key for key, value, _source in configured if _reserved_value_is_unsupported(key, value)
    ]
    if unsupported:
        return CheckResult(
            name="reserved_config_key",
            status="fail",
            detail=(
                f"unsupported auto-approval or auditor mutation setting(s): "
                f"{', '.join(unsupported)}; reserved values have no effect; configured: {rendered}"
            ),
        )
    return CheckResult(
        name="reserved_config_key",
        status="warn",
        detail=f"reserved config value(s) have no effect: {rendered}",
    )


def check_manifest_in_sync(*, workspace: Path | None) -> CheckResult:
    """Verify on-disk managed regions match the manifest hashes.

    An absent manifest is ``ok`` (the renderer has not run yet); a broken one
    is ``fail``; a missing or hand-edited region is ``warn`` naming the
    offending ``target::id`` entries. Whole-file plugin renders are excluded
    via :func:`_is_plugin_owned` — they carry no region markers, so the
    detector would report every one ``missing``.
    """
    anchor = _resolve_anchor(workspace)
    if anchor is None:
        return CheckResult(
            name="manifest_in_sync",
            status="warn",
            detail="no workspace anchor; cannot resolve manifest path",
        )
    manifest_path = anchor / ".ea" / "indexes" / "generated.json"
    if not manifest_path.exists():
        return CheckResult(
            name="manifest_in_sync",
            status="ok",
            detail=f"no manifest at {manifest_path}; nothing to verify",
        )
    try:
        manifest: Manifest = load_manifest(manifest_path)
    except (ValueError, ValidationError) as exc:
        return CheckResult(
            name="manifest_in_sync",
            status="fail",
            detail=f"manifest at {manifest_path} is malformed: {exc}",
        )

    # Region-marked managed blocks only — whole-file plugin renders are
    # satisfied by the plugin cache (see _is_plugin_owned) and never carry
    # markers, so they must not flow into the marker-drift detector.
    region_entries = [e for e in manifest.generated.values() if not _is_plugin_owned(e)]
    plugin_count = len(manifest.generated) - len(region_entries)
    region_targets = {e.target for e in region_entries}

    drift_summaries: list[str] = []
    for target_str in sorted(region_targets):
        # Manifest stores POSIX-form paths; resolve relative to the anchor
        # so a manifest written with ``"AGENTS.md"`` resolves correctly.
        target_path = Path(target_str)
        if not target_path.is_absolute():
            target_path = anchor / target_path
        reports = detect_drift(target_path, manifest)
        for report in reports:
            if report.kind == "ok":
                continue
            drift_summaries.append(f"{target_str}::{report.id}={report.kind}")

    if drift_summaries:
        # Cap the surfaced list so the detail line stays one-line readable —
        # the exact list is also recoverable via ``eawf sync --check``.
        joined = ", ".join(drift_summaries[:5])
        suffix = f" (+{len(drift_summaries) - 5} more)" if len(drift_summaries) > 5 else ""
        return CheckResult(
            name="manifest_in_sync",
            status="warn",
            detail=f"drift: {joined}{suffix}",
        )

    n_region = len(region_entries)
    plugin_note = f"; {plugin_count} plugin region(s) from cache" if plugin_count else ""
    return CheckResult(
        name="manifest_in_sync",
        status="ok",
        detail=f"{n_region} region(s) hash-stable{plugin_note}",
    )


def _tree_for_check(workspace: Path, *, name: str) -> Path | CheckResult:
    """Resolve the ``.ea`` tree a tree-reading doctor check reads.

    Returns the tree directory, or an early ``ok`` :class:`CheckResult`
    (carrying *name*) when no tree resolves: :func:`check_state_present`
    already surfaces that, so a second check must not double-flip doctor's
    overall status for the same root cause.

    Args:
        workspace: Workspace anchor to resolve the tree against.
        name: Check name stamped onto an early-return :class:`CheckResult`.
    """
    try:
        state_path, _reason = resolve_with_reason(workspace)
    except FileNotFoundError, ValueError:
        return CheckResult(name=name, status="ok", detail="no eawf tree")
    if not state_path.parent.is_dir():
        return CheckResult(name=name, status="ok", detail="no eawf tree")
    return state_path.parent


def _grant_triple_from_state(server: object) -> tuple[str, list[str], dict[str, str]]:
    """Return the (command, args, env-block) grant triple from a state row.

    The env block renders ``env_refs`` to ``{NAME: "${ENV:NAME}"}`` — the
    same literal-token shape the installer writes to disk — so the
    comparison is apples-to-apples.
    """
    from eawf.runtime.mcp.env_ref import render_env_block

    command: str = getattr(server, "command", "")
    args: list[str] = [str(a) for a in getattr(server, "args", [])]
    env: dict[str, str] = render_env_block(getattr(server, "env_refs", []))
    return command, args, env


def _grant_triple_from_disk(body: Mapping[str, object]) -> tuple[str, list[str], dict[str, str]]:
    """Return the (command, args, env-block) grant triple from a disk entry."""
    raw_args = body.get("args") or []
    raw_env = body.get("env") or {}
    args = [str(a) for a in raw_args] if isinstance(raw_args, (list, tuple)) else []
    env = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
    return str(body.get("command", "")), args, env


def _extract_eawf_entries(
    path: Path, *, fmt: str, block_key: str
) -> dict[str, dict[str, object]] | CheckResult:
    """Return the eawf-owned MCP entries in one runtime config file.

    *fmt* is ``"json"`` (Claude settings.json / OpenCode opencode.json) or
    ``"toml"`` (Codex config.toml). Returns ``{id: body}`` for every
    ``__eawf_owner == "eawf"`` entry, an empty dict for a missing file, or
    a ``warn`` :class:`CheckResult` when the file is present but unreadable.
    """
    import json as _json
    import tomllib as _tomllib

    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if fmt == "toml":
            parsed: object = _tomllib.loads(text)
        else:
            parsed = _json.loads(text or "{}")
    except _json.JSONDecodeError, _tomllib.TOMLDecodeError, UnicodeDecodeError:
        return CheckResult(name="mcp_drift", status="warn", detail=f"unreadable {path}")
    block = parsed.get(block_key) or {} if isinstance(parsed, dict) else {}
    out: dict[str, dict[str, object]] = {}
    if isinstance(block, dict):
        for sid, entry in block.items():
            if isinstance(entry, dict) and entry.get("__eawf_owner") == "eawf":
                out[sid] = entry
    return out


def check_mcp_drift(*, workspace: Path | None) -> CheckResult:
    """Compare ``state.mcp_servers`` against runtime-config-emitted entries.

    For every Eä-owned :class:`McpServer` in state, verify the same id is
    present on disk — with a matching command / args / env grant — under
    whichever runtime adapters have materialised an MCP block:

    - Claude: ``<workspace>/.mcp.json:mcpServers[<id>]``
    - OpenCode: ``<workspace>/opencode.json:mcp[<id>]``
    - Codex: ``<workspace>/.codex/config.toml:mcp_servers[<id>]``.

    The check is **warn**-level when:

    - State has an eawf-owned server but no runtime has emitted it
      (``missing-from-runtime``).
    - A runtime entry's grant (command / args / env) diverges from the
      state row that owns it (``content-drift`` — an install that never
      re-ran after an ``eawf mcp update``).
    - A runtime file contains an eawf-owned entry whose id is not in
      state (``orphans`` — a managed entry left behind by a removal that
      skipped the runtime config).

    The check is **ok** when state is empty or when every state id is
    materialised, grant-matched, in at least one runtime file.
    """
    name = "mcp_drift"
    if workspace is None:
        return CheckResult(name=name, status="ok", detail="no workspace anchor")
    tree = _tree_for_check(workspace, name=name)
    if isinstance(tree, CheckResult):
        return tree
    from eawf.runtime.mcp.book import registered, tree_servers

    # The registry lives in the selected generation; an epoch-1 tree has
    # none to compare, which check_authority_epoch already reports.
    standing = tree_servers(tree)
    if standing is None:
        return CheckResult(name=name, status="ok", detail="no epoch-2 mcp registry")
    servers = registered(standing)
    eawf_owned = {sid: s for sid, s in servers.items() if s.owner == "eawf"}
    if not eawf_owned:
        return CheckResult(name=name, status="ok", detail="no eawf-owned mcp servers")

    repo_root = tree.parent
    materialised: set[str] = set()
    orphans: list[str] = []
    drifted: list[str] = []
    runtime_files = (
        (repo_root / ".mcp.json", "mcpServers", "claude", "json"),
        (repo_root / "opencode.json", "mcp", "opencode", "json"),
        (repo_root / ".codex" / "config.toml", "mcp_servers", "codex", "toml"),
    )
    for path, block_key, runtime, fmt in runtime_files:
        entries = _extract_eawf_entries(path, fmt=fmt, block_key=block_key)
        if isinstance(entries, CheckResult):
            return entries
        for sid, body in entries.items():
            materialised.add(sid)
            if sid not in eawf_owned:
                orphans.append(f"{runtime}:{sid}")
            elif _grant_triple_from_disk(body) != _grant_triple_from_state(eawf_owned[sid]):
                drifted.append(f"{runtime}:{sid}")

    missing = sorted(set(eawf_owned) - materialised)
    if missing or drifted or orphans:
        parts: list[str] = []
        if missing:
            parts.append(f"missing-from-runtime: {','.join(missing[:5])}")
        if drifted:
            parts.append(f"content-drift: {','.join(sorted(drifted)[:5])}")
        if orphans:
            parts.append(f"orphans: {','.join(sorted(orphans)[:5])}")
        return CheckResult(name=name, status="warn", detail="; ".join(parts))
    return CheckResult(
        name=name,
        status="ok",
        detail=f"{len(eawf_owned)} eawf-owned server(s) match runtime emit",
    )


def _git_output(cwd: Path, *args: str) -> str | None:
    """Return stripped stdout of a read-only git command, or ``None`` on failure."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            **no_window_kwargs(),
        )
    except OSError, subprocess.SubprocessError:
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


#: Hours after which an unrefreshed remote-tracking set is called stale. A
#: comparison against a base ref nobody fetched is not wrong-ish, it is wrong:
#: the local ref answers for a remote that has since moved.
_FETCH_STALE_HOURS: int = 24


def _fetch_head_path(workspace: Path) -> Path | None:
    """Return ``FETCH_HEAD`` for *workspace*, or ``None`` when there is no repo.

    A linked worktree's ``.git`` is a file holding ``gitdir: <path>``; the
    common dir is its parent, which is where FETCH_HEAD lives.
    """
    dot_git = workspace / ".git"
    if dot_git.is_dir():
        return dot_git / "FETCH_HEAD"
    if not dot_git.is_file():
        return None
    pointer = dot_git.read_text(encoding="utf-8").strip()
    if not pointer.startswith("gitdir:"):
        return None
    git_dir = Path(pointer.removeprefix("gitdir:").strip())
    if not git_dir.is_absolute():
        git_dir = workspace / git_dir
    # <common>/worktrees/<name> -> <common>
    return git_dir.parent.parent / "FETCH_HEAD"


def check_branch_currency(*, workspace: Path | None) -> CheckResult:
    """Warn when the local base branch trails its remote, or the fetch is old.

    The branch-currency rule asks for a fetch-and-compare before opening a
    phase, iter, or wave. It was prose with no backstop, so a base ref left
    unfetched reads as current and every comparison drawn against it inherits
    the staleness. Warn rather than fail: trailing the remote mid-work is
    ordinary, and this check never fetches — it only reports what is knowable
    without touching the network.
    """
    name = "branch_currency"
    if workspace is None:
        return CheckResult(name=name, status="ok", detail="no workspace anchor")
    tree = _tree_for_check(workspace, name=name)
    if isinstance(tree, CheckResult):
        return tree
    from eawf.platform.rules.render import RuleProjectionError, load_project_brief

    try:
        base = load_project_brief(tree.parent).default_branch
    except RuleProjectionError as exc:
        return CheckResult(name=name, status="ok", detail=f"project record unreadable: {exc}")
    if base is None:
        return CheckResult(name=name, status="ok", detail="no project default branch")

    remote_ref = f"origin/{base}"
    # One git call, not three. This check runs inside `run_all`, which the TUI
    # doctor pane awaits on mount, so each subprocess here is latency the whole
    # pane pays. rev-list answers "is the remote ahead" and "does the ref
    # exist" together: a missing ref exits non-zero, which reads as None.
    behind = _git_output(workspace, "rev-list", "--count", f"{base}..{remote_ref}")
    if behind is None:
        return CheckResult(name=name, status="ok", detail=f"no {remote_ref} to compare")

    faults: list[str] = []
    if behind != "0":
        faults.append(f"local {base} is {behind} commit(s) behind {remote_ref}")

    # Resolved from the workspace rather than `rev-parse --git-common-dir`: a
    # worktree's .git is a file naming the common dir, which is cheap to read
    # directly and saves a second subprocess.
    fetch_head = _fetch_head_path(workspace)
    if fetch_head is not None and fetch_head.is_file():
        age_hours = (time.time() - fetch_head.stat().st_mtime) / 3600
        if age_hours > _FETCH_STALE_HOURS:
            faults.append(f"last fetch {age_hours:.0f}h ago")

    if faults:
        return CheckResult(name=name, status="warn", detail="; ".join(faults) + "; run git fetch")
    return CheckResult(name=name, status="ok", detail=f"local {base} is current with {remote_ref}")


def check_cli_daemon_version(
    *,
    probe_version: Callable[[], str | None] | None = None,
) -> CheckResult:
    """Compare installed CLI and running daemon versions without spawning."""
    return daemon_checks.check_cli_daemon_version(
        probe_version=probe_version or _probe_running_daemon_version
    )


_MAX_PARALLEL_WAVES_ADAPTER: TypeAdapter[int] = TypeAdapter(
    Annotated[int, Field(strict=True, ge=1, le=16)]
)


def resolve_max_parallel_waves(repo_root: Path | None) -> int:
    """Return the validated ``planning.max_parallel_waves`` value.

    Args:
        repo_root: Repository root supplying the layered configuration, or
            ``None`` for the built-in default.

    Returns:
        The resolved cap.

    Raises:
        pydantic.ValidationError: When the resolved leaf is not an integer in
            the supported ``1..16`` range.
    """
    if repo_root is None:
        raw: object = BUILT_IN_DEFAULTS["planning"]["max_parallel_waves"]
    else:
        merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
        raw = get_dotted(merged, "planning.max_parallel_waves")
    return _MAX_PARALLEL_WAVES_ADAPTER.validate_python(raw)


def check_parallel_cap_enforcement(
    *,
    workspace: Path | None,
    resolver: Callable[[Path | None], int] | None = None,
) -> CheckResult:
    """Report whether the configured parallel-wave cap resolves to a valid value."""
    name = "parallel_cap_enforcement"
    anchor = _resolve_anchor(workspace)
    resolver = resolver or resolve_max_parallel_waves
    try:
        cap = resolver(anchor)
    except Exception as exc:
        return CheckResult(
            name=name,
            status="warn",
            detail=f"parallel cap cannot be resolved: {exc}",
        )
    return CheckResult(
        name=name,
        status="ok",
        detail=f"planning.max_parallel_waves={cap}",
    )


# ``eawf daemon reclaim`` trims runtime-dir bloat; doctor warns once the
# runtime dir (WAL records plus the never-auto-GC'd ``eawfd.log``) crosses this
# size, or the ``.ea/`` migration backups pile up past this count, so the
# operator knows to run it. Both are advisory (``warn`` only, never ``fail``)
# -- disk pressure is a capacity signal, not a broken install.
RUNTIME_DIR_WARN_BYTES = 100 * 1024 * 1024
STATE_BACKUP_WARN_COUNT = 10


def check_launchd_agent(
    *, detector: Callable[..., SupervisedAgentReport] | None = None
) -> CheckResult:
    """Report the OS-supervised eawfd agent (launchd / systemd) state.

    A daemon under a launchd LaunchAgent (macOS) or a systemd user unit
    (Linux) auto-restarts on exit, which silently defeats a manual
    ``eawf daemon stop`` and can leave a manually spawned daemon racing the
    supervised one. This row surfaces three operability signals the operator
    cannot otherwise see without shelling into launchctl / systemctl by hand:

    - ``loaded`` -- the agent is registered and will respawn the daemon;
    - drift -- the plist / unit points at a STALE binary (a pre-upgrade
      ``service-enable`` that never re-ran);
    - executable -- the configured program still exists and has execute
      permission;
    - a RIVAL daemon PID coexisting with the supervised one (multi-daemon).

    Returns ``ok`` when there is no supervised agent, or a loaded agent with
    neither drift nor a rival; ``warn`` on drift or a detected rival. It never
    returns ``fail`` -- a loaded agent is a normal, healthy install.

    Args:
        detector: Injected detection callable; defaults to
            :func:`eawf.runtime.daemon.service_install.detect_supervised_agent`.
            The doctor suite passes a stub so the check never shells out.
    """
    name = "launchd_agent"
    if detector is None:
        from eawf.runtime.daemon.service_install import detect_supervised_agent

        detector = detect_supervised_agent
    report = detector()
    if report.supervisor == "none" or not (report.installed or report.loaded):
        return CheckResult(name=name, status="ok", detail="no supervised eawfd agent")
    loaded_note = "loaded" if report.loaded else "installed (not loaded)"
    issues: list[str] = []
    if report.drift:
        issues.append(f"unit points at stale binary program={report.program!r}")
    if report.path_drift:
        issues.append("unit PATH is missing or stale")
    if report.installed and report.program is not None:
        program = Path(report.program)
        if not program.is_file() or not os.access(program, os.X_OK):
            issues.append(f"unit program is not executable program={report.program!r}")
    if report.rival_pid is not None:
        issues.append(f"rival daemon pid={report.rival_pid} alongside the supervised agent")
    if issues:
        return CheckResult(
            name=name,
            status="warn",
            detail=f"{report.supervisor} agent {loaded_note}; " + "; ".join(issues),
        )
    return CheckResult(
        name=name,
        status="ok",
        detail=f"{report.supervisor} agent {loaded_note}; no drift or rival",
    )


def _dir_size_bytes(path: Path) -> int:
    """Return the total size in bytes of the files under *path* (0 if absent)."""
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _state_backup_census(workspace: Path | None) -> tuple[int, int]:
    """Return ``(count, total_bytes)`` of ``.ea/state.json.bak.*`` backups."""
    anchor = _resolve_anchor(workspace)
    if anchor is None:
        return 0, 0
    backups = list((anchor / ".ea").glob("state.json.bak.*"))
    total = 0
    for path in backups:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return len(backups), total


def check_runtime_dir_size(*, workspace: Path | None) -> CheckResult:
    """Advise when the daemon runtime dir or the .ea backup census grows large.

    Covers two disk-pressure sources a long-lived install accumulates and
    ``eawf daemon reclaim`` trims: the daemon runtime directory (``~/.eawfd``
    -- WAL records plus the never-auto-GC'd ``eawfd.log``) and the
    ``.ea/state.json.bak.*`` migration backups (each schema bump writes one,
    and they are gitignored, so they never leave the working tree on their
    own).

    Returns ``warn`` -- never ``fail`` -- when the runtime dir crosses
    :data:`RUNTIME_DIR_WARN_BYTES` or the backup count reaches
    :data:`STATE_BACKUP_WARN_COUNT`, pointing the operator at
    ``eawf daemon reclaim``; ``ok`` otherwise.

    Args:
        workspace: Workspace anchor for the ``.ea`` backup census; ``None``
            defers to the pwd-upward ``.ea/`` walk.
    """
    from eawf.runtime.daemon.runtime_dir import runtime_dir

    name = "runtime_dir_size"
    rt_dir = runtime_dir()
    rt_bytes = _dir_size_bytes(rt_dir)
    backup_count, backup_bytes = _state_backup_census(workspace)
    rt_mib = rt_bytes / (1024 * 1024)
    backup_mib = backup_bytes / (1024 * 1024)
    issues: list[str] = []
    if rt_bytes >= RUNTIME_DIR_WARN_BYTES:
        ceiling_mib = RUNTIME_DIR_WARN_BYTES // (1024 * 1024)
        issues.append(f"runtime dir {rt_dir} is {rt_mib:.1f} MiB (>= {ceiling_mib} MiB)")
    if backup_count >= STATE_BACKUP_WARN_COUNT:
        issues.append(f"{backup_count} state.json.bak.* backups ({backup_mib:.1f} MiB)")
    if issues:
        return CheckResult(
            name=name,
            status="warn",
            detail="; ".join(issues) + "; run `eawf daemon reclaim`",
        )
    return CheckResult(
        name=name,
        status="ok",
        detail=(
            f"runtime dir {rt_mib:.1f} MiB; {backup_count} state backup(s) {backup_mib:.1f} MiB"
        ),
    )


def check_render_output_roundtrip() -> CheckResult:
    """Round-trip a synthetic :class:`OutputEnvelope` to confirm the wire-form holds.

    The check is deterministic and self-contained: it constructs a minimal
    but non-trivial envelope, serialises with :func:`to_markdown`, parses
    back with :func:`from_markdown`, and asserts equality. If the envelope
    parser regresses, every skill that emits a JSON envelope is broken — so
    this check anchors the envelope wire contract on every doctor run.
    """
    # Header and footer are typed; older callers passed
    # ``dict`` literals which Pydantic v2 still coerces into the typed
    # models on validation. We use a literal dict here so the doctor
    # smoke check exercises the back-compat path.
    sample = OutputEnvelope.model_validate(
        {
            "header": {
                "skill": "/init",
                "scope_id": "urn:eawf:v1:state:doctor",
                "session": "urn:eawf:v1:store:doctor/sessions/SES-doctor",
                "started_at": "2026-05-09T00:00:00Z",
                "finished_at": "2026-05-09T00:00:01Z",
                "status": "ok",
                "instrument_probe": {},
            },
            "body": "round-trip test\n",
            "footer": {"warnings": []},
        }
    )
    rendered = to_markdown(sample)
    try:
        parsed = from_markdown(rendered)
    except (ValueError, ValidationError) as exc:
        return CheckResult(
            name="render_output_roundtrip",
            status="fail",
            detail=f"from_markdown failed: {exc}",
        )
    if parsed != sample:
        return CheckResult(
            name="render_output_roundtrip",
            status="fail",
            detail="envelope round-trip lost data (header/body/footer mismatch)",
        )
    return CheckResult(
        name="render_output_roundtrip",
        status="ok",
        detail="envelope JSON ⇄ markdown round-trip byte-stable",
    )


def tools_available(
    *,
    workspace: Path,
    profile_ids: list[str] | None = None,
    reprobe: bool = False,
    cache_path: Path | None = None,
) -> CheckResult:
    """Public alias for :func:`check_tools_available` (plan §246 spelling)."""
    return check_tools_available(
        workspace=workspace,
        profile_ids=profile_ids,
        reprobe=reprobe,
        cache_path=cache_path,
    )


def resolve_anchor(workspace: Path | None) -> Path | None:
    """Public alias for :func:`_resolve_anchor` (pwd-upward ``.ea/`` resolver)."""
    return _resolve_anchor(workspace)


def manifest_in_sync(*, workspace: Path | None) -> CheckResult:
    """Public alias for :func:`check_manifest_in_sync`."""
    return check_manifest_in_sync(workspace=workspace)


def render_output_roundtrip() -> CheckResult:
    """Public alias for :func:`check_render_output_roundtrip`."""
    return check_render_output_roundtrip()


def state_present(*, workspace: Path | None) -> CheckResult:
    """Public alias for :func:`check_state_present`."""
    return check_state_present(workspace=workspace)


def config_resolves(*, workspace: Path | None) -> CheckResult:
    """Public alias for :func:`check_config_resolves`."""
    return check_config_resolves(workspace=workspace)


def run_all(
    *,
    workspace: Path | None,
    profile_ids: list[str] | None = None,
    reprobe: bool = False,
    cache_path: Path | None = None,
    reserved_config_result: CheckResult | None = None,
    daemon_version_probe: Callable[[], str | None] | None = None,
) -> list[CheckResult]:
    """Run every doctor check and return the result list.

    The instrument probe is the only check whose hard failure aborts the
    function; it raises :class:`eawf.surfaces.cli.errors.UserError`
    (``kind="InstrumentMissing"``) so the CLI can map it to exit code
    ``6``. Every other check returns a
    :class:`CheckResult`. The manifest-in-sync and
    render-output-roundtrip checks run at the end of the list so the canonical
    envelope shape mirrors the order operators see in the doctor table.

    Anchor resolution: a plain ``eawf doctor`` (no ``-w``) passes
    ``workspace=None``. The anchor-dependent checks (manifest / mcp drift)
    resolve the ``.ea/`` parent by walking upward from pwd via
    :func:`_resolve_anchor` so they verify THIS repo instead of degrading to a
    "no workspace anchor" note. The probe cache is pinned to a per-user
    location (:func:`_resolve_probe_cache_path`) so probing never litters an
    ``instrument-probe.json`` into an arbitrary anchor directory.
    """
    from eawf.observability.doctor.daemon_strays import check_stray_daemons
    from eawf.observability.doctor.runtime_health import run_runtime_tuple_health_checks
    from eawf.observability.doctor.workflow_health import run_workflow_health_checks

    anchor = _resolve_anchor(workspace)
    probe_cache = cache_path if cache_path is not None else _resolve_probe_cache_path(anchor)
    workspace_for_probe = anchor if anchor is not None else Path.cwd()
    results = [
        check_tools_available(
            workspace=workspace_for_probe,
            profile_ids=profile_ids,
            reprobe=reprobe,
            cache_path=probe_cache,
        ),
        check_state_present(workspace=workspace),
        check_config_resolves(workspace=workspace),
        (
            reserved_config_result
            if reserved_config_result is not None
            else check_reserved_config_keys(workspace=workspace)
        ),
        check_manifest_in_sync(workspace=anchor),
        check_mcp_drift(workspace=anchor),
        check_authority_epoch(workspace=anchor),
        check_gitignore_block(workspace=anchor),
        *run_workflow_health_checks(workspace=anchor, daemon_version_probe=daemon_version_probe),
        *run_runtime_tuple_health_checks(workspace=anchor),
        check_launchd_agent(),
        check_stray_daemons(),
        check_runtime_dir_size(workspace=anchor),
        check_render_output_roundtrip(),
        check_agents_md_byte_cap(workspace=anchor),
        check_agents_override_byte_cap(workspace=anchor),
    ]
    return results
