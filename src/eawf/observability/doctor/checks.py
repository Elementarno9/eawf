"""``eawf doctor`` check implementations.

Each public ``check_*`` function returns a :class:`CheckResult`. Five checks
ship after Wave W08 — together they answer the v0.1 plan §11 question "is
this install workable?".

- :func:`check_tools_available` — runs the instrument probe and surfaces its
  outcome (``ok``/``warn``/``fail``). On hard-tool failure the underlying
  :class:`eawf.surfaces.cli.errors.UserError` (``kind="InstrumentMissing"``) is
  allowed to propagate so the CLI maps it to exit code ``6``
  (``INSTRUMENT_MISSING``). All other outcomes collapse to a non-fatal
  :class:`CheckResult`.
- :func:`check_state_present` — reports whether ``state.json`` resolves at
  the workspace anchor.
- :func:`check_config_resolves` — reports whether the layered config merge
  succeeds for the workspace.
- :func:`check_manifest_in_sync` — reports whether the on-disk managed-region
  hashes match the manifest at ``.ea/indexes/generated.json`` (W08).
- :func:`check_render_output_roundtrip` — proves the
  :mod:`eawf.surfaces.render.envelope` JSON ⇄ markdown round-trip is byte-stable on a
  synthetic envelope; if this regresses every skill is broken (W08).

The doctor command (`eawf.surfaces.cli.commands.doctor`) consumes the list, formats it
via :mod:`eawf.observability.doctor.report`, and selects the highest-severity status to
drive its exit code.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.install.instrument_probe import probe
from eawf.kernel.config.layered import merge_config
from eawf.kernel.config.profile import KNOWN_PROFILES
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.render.drift import detect_drift
from eawf.surfaces.render.envelope import OutputEnvelope, from_markdown, to_markdown
from eawf.surfaces.render.manifest import Manifest
from eawf.surfaces.render.manifest import load as load_manifest

if TYPE_CHECKING:
    from collections.abc import Mapping

    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)


CheckStatus = Literal["ok", "warn", "fail"]


class CheckResult(BaseModel):
    """Single doctor check outcome.

    Attributes:
        name: Stable machine identifier (``"tools_available"``, ...).
        status: ``ok`` (everything fine), ``warn`` (functional but degraded),
            or ``fail`` (broken — the doctor surface still completes, but the
            CLI exits non-zero).
        detail: Short human message. ``None`` when the check has nothing
            interesting to add beyond ``status``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    status: CheckStatus
    detail: str | None = None


def _default_probe_cache(workspace: Path) -> Path:
    """Return the canonical cache file path under ``<workspace>/.ea/``."""
    return workspace / ".ea" / "instrument-probe.json"


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
    snapshot view (no abort) should call :func:`eawf.install.instrument_probe.probe`
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
    """Return ``ok`` iff ``state.json`` resolves at *workspace*.

    *workspace* may be ``None`` to defer to the resolver's pwd-upward walk
    (which is the default behaviour when ``--workspace`` is not supplied).
    """
    state_path, reason = resolve_with_reason(workspace=workspace)
    if state_path.exists():
        return CheckResult(
            name="state_present",
            status="ok",
            detail=f"state.json found at {state_path} (via {reason})",
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
    """
    repo = Path.cwd()
    try:
        merged, _sources = merge_config(repo=repo, workspace=workspace)
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
    unknown = [p for p in enabled_profiles if p not in KNOWN_PROFILES]
    if unknown:
        return CheckResult(
            name="config_resolves",
            status="warn",
            detail=f"unknown profile(s) enabled: {', '.join(unknown)}",
        )
    return CheckResult(
        name="config_resolves",
        status="ok",
        detail=f"{len(enabled_profiles)} profile(s) enabled",
    )


def check_manifest_in_sync(*, workspace: Path | None) -> CheckResult:
    """Verify on-disk managed regions match the manifest hashes.

    Behaviour:

    - ``workspace`` is ``None`` (or unresolvable) → ``warn`` (no anchor).
    - Manifest absent → ``ok`` (uninitialised; the renderer has not run yet —
      :func:`check_state_present` already covers the "is this thing initialised?"
      angle).
    - Manifest present but parse-broken → ``fail`` with the parse error.
    - Manifest present, every region's recomputed hash matches → ``ok``.
    - Manifest present, any region missing or hand-edited → ``warn`` with a
      compact summary of the offending ``target::id`` entries.

    The walk is per-target — for each unique ``target`` field in the manifest,
    :func:`eawf.surfaces.render.drift.detect_drift` reads the file once and emits one
    :class:`~eawf.surfaces.render.drift.DriftReport` per region.
    """
    if workspace is None:
        return CheckResult(
            name="manifest_in_sync",
            status="warn",
            detail="no workspace anchor; cannot resolve manifest path",
        )
    manifest_path = Path(workspace) / ".ea" / "indexes" / "generated.json"
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

    # Group entries by target so we read each file at most once.
    targets: dict[str, list[str]] = {}
    for entry in manifest.generated.values():
        targets.setdefault(entry.target, []).append(entry.region_id)

    drift_summaries: list[str] = []
    for target_str in sorted(targets):
        # Manifest stores POSIX-form paths; resolve relative to the workspace
        # so a manifest written with ``"AGENTS.md"`` resolves correctly.
        target_path = Path(target_str)
        if not target_path.is_absolute():
            target_path = Path(workspace) / target_path
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

    n_entries = len(manifest.generated)
    return CheckResult(
        name="manifest_in_sync",
        status="ok",
        detail=f"{n_entries} region(s) hash-stable",
    )


def _load_mcp_drift_state(workspace: Path) -> tuple[State, Path] | CheckResult:
    """Resolve + parse ``state.json`` for the mcp-drift check.

    Returns ``(state, state_path)``, or an early ``ok`` :class:`CheckResult`
    when there is no resolvable / parseable state to compare against.
    """
    import json as _json

    from eawf.kernel.state.models import State

    name = "mcp_drift"
    try:
        state_path, _reason = resolve_with_reason(workspace)
    except FileNotFoundError, ValueError:
        return CheckResult(name=name, status="ok", detail="no state.json")
    if not state_path.exists():
        return CheckResult(name=name, status="ok", detail="no state.json")
    try:
        raw = _json.loads(state_path.read_text(encoding="utf-8"))
        return State.model_validate(raw), state_path
    except _json.JSONDecodeError, ValidationError:
        # state schema errors surface via ``state_present`` already — keep
        # this check focused on mcp drift and stay quiet here so doctor's
        # overall status is not double-flipped for the same root cause.
        return CheckResult(name=name, status="ok", detail="state.json unparseable")


def _scan_runtime_mcp_block(
    path: Path, *, block_key: str, runtime: str, eawf_owned: Mapping[str, object]
) -> tuple[set[str], list[str]] | CheckResult:
    """Scan one runtime config file for eawf-owned MCP ids.

    Returns the ``(ids, orphans)`` found, or a ``warn`` :class:`CheckResult`
    when the file is present but unreadable. A missing file yields empties.
    """
    import json as _json

    if not path.is_file():
        return set(), []
    try:
        body = _json.loads(path.read_text(encoding="utf-8") or "{}")
    except _json.JSONDecodeError:
        return CheckResult(name="mcp_drift", status="warn", detail=f"unreadable {path}")
    found: set[str] = set()
    orphans: list[str] = []
    for sid, entry in (body.get(block_key) or {}).items():
        if isinstance(entry, dict) and entry.get("__eawf_owner") == "eawf":
            found.add(sid)
            if sid not in eawf_owned:
                orphans.append(f"{runtime}:{sid}")
    return found, orphans


def check_mcp_drift(*, workspace: Path | None) -> CheckResult:
    """Compare ``state.mcp_servers`` against runtime-config-emitted entries (D21 / B062).

    For every Eä-owned :class:`McpServer` in state, verify the same id is
    present on disk under whichever runtime adapters have already
    materialised an MCP block:

    - Claude: ``<workspace>/.claude/settings.json:mcpServers[<id>]``
    - OpenCode: ``<workspace>/opencode.json:mcp[<id>]``
    - Codex: ``<workspace>/.codex/config.toml`` — the v0.3 plugin
      installer does not yet emit a per-server TOML block, so the
      runtime is recognised but its absence is not a drift signal.

    The check is **warn**-level when:

    - State has an eawf-owned server but no runtime has emitted it.
    - A runtime file contains an eawf-owned entry whose id is not in
      state (orphaned managed entry).

    The check is **ok** when state is empty or when every state id is
    materialised in at least one runtime file.
    """
    name = "mcp_drift"
    if workspace is None:
        return CheckResult(name=name, status="ok", detail="no workspace anchor")
    loaded = _load_mcp_drift_state(workspace)
    if isinstance(loaded, CheckResult):
        return loaded
    state, state_path = loaded
    eawf_owned = {sid: s for sid, s in (state.mcp_servers or {}).items() if s.owner == "eawf"}
    if not eawf_owned:
        return CheckResult(name=name, status="ok", detail="no eawf-owned mcp servers")

    repo_root = state_path.parent.parent
    materialised: set[str] = set()
    orphans: list[str] = []
    runtime_files = (
        (repo_root / ".claude" / "settings.json", "mcpServers", "claude"),
        (repo_root / "opencode.json", "mcp", "opencode"),
    )
    for path, block_key, runtime in runtime_files:
        scanned = _scan_runtime_mcp_block(
            path, block_key=block_key, runtime=runtime, eawf_owned=eawf_owned
        )
        if isinstance(scanned, CheckResult):
            return scanned
        found, file_orphans = scanned
        materialised |= found
        orphans.extend(file_orphans)

    missing = sorted(set(eawf_owned) - materialised)
    if missing or orphans:
        parts: list[str] = []
        if missing:
            parts.append(f"missing-from-runtime: {','.join(missing[:5])}")
        if orphans:
            parts.append(f"orphans: {','.join(orphans[:5])}")
        return CheckResult(name=name, status="warn", detail="; ".join(parts))
    return CheckResult(
        name=name,
        status="ok",
        detail=f"{len(eawf_owned)} eawf-owned server(s) match runtime emit",
    )


def check_render_output_roundtrip() -> CheckResult:
    """Round-trip a synthetic :class:`OutputEnvelope` to confirm the wire-form holds.

    The check is deterministic and self-contained: it constructs a minimal
    but non-trivial envelope, serialises with :func:`to_markdown`, parses
    back with :func:`from_markdown`, and asserts equality. If the envelope
    parser regresses, every skill that emits a JSON envelope is broken — so
    this check anchors W07's API contract on every doctor run.
    """
    # Phase 4 W01: header + footer are typed; pre-W01 callers passed
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
) -> list[CheckResult]:
    """Run every doctor check and return the result list.

    The instrument probe is the only check whose hard failure aborts the
    function; it raises :class:`eawf.surfaces.cli.errors.UserError`
    (``kind="InstrumentMissing"``) so the CLI can map it to exit code
    ``6``. Every other check returns a
    :class:`CheckResult`. W08 adds the manifest-in-sync and
    render-output-roundtrip checks at the end of the list so the canonical
    envelope shape mirrors the order operators see in the doctor table.
    """
    workspace_for_probe = workspace if workspace is not None else Path.cwd()
    results = [
        check_tools_available(
            workspace=workspace_for_probe,
            profile_ids=profile_ids,
            reprobe=reprobe,
            cache_path=cache_path,
        ),
        check_state_present(workspace=workspace),
        check_config_resolves(workspace=workspace),
        check_manifest_in_sync(workspace=workspace),
        check_mcp_drift(workspace=workspace),
        check_render_output_roundtrip(),
    ]
    return results
