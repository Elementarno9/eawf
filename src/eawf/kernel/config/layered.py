"""Layered config merge engine with source tracking.

Eä settings layer (P25-W14, cluster C08 extends the original six durable
layers with a ``branch`` layer + a daemon-resident transient ``wave``
layer):

1. built-in defaults
2. global ``~/.config/eawf/config.yaml``
3. workspace ``<workspace>/.ea/config.yaml``
4. repo ``<repo>/.ea/config.yaml``
5. branch ``<repo>/.ea/branches/<branch>.yaml`` (subdirectory form;
   e.g. branch ``feature/foo`` lives at ``feature/foo.yaml``)
6. local ``<repo>/.ea/local/config.yaml``
7. wave (daemon RAM, no file — keyed by ``Wave.id``, dropped on close)
8. env vars / CLI flags

Later layers override earlier layers. Merge semantics: maps deep-merge,
scalar values replace, keyed lists merge by stable ``id``, ordinary lists
replace, and CLI/env overrides are runtime-only unless explicitly saved.
Top-level sections required in the merged config are listed in
``docs/architecture/envelope.md`` "Config schema required sections".

The single public entry point is :func:`merge_config`. It returns a tuple of
``(merged_config, source_map)`` where ``source_map[dotted_key] = layer``. The
source map is consumed by ``eawf config get`` to surface the layer that
contributed the final value.

Layer labels (canonical):

    "built-in" | "global" | "workspace" | "repo" | "branch" | "local"
    | "wave" | "env" | "cli"

Lower-precedence labels appear earlier in :data:`LAYER_ORDER`. The
:class:`Layer` enum gives type-checked access to the labels for new
call sites; the bare string constants stay supported because every
existing caller passes them by value.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from eawf.kernel.config.defaults import built_in_defaults
from eawf.kernel.config.loader import load_yaml_layer

logger = logging.getLogger(__name__)


class Layer(StrEnum):
    """Typed enumeration of layered-config labels.

    :class:`enum.StrEnum` (PEP 663, Python 3.11+) gives each member a
    string identity so ``Layer.REPO == "repo"``. Callers that still
    pass bare strings continue to work unchanged; new call sites
    should reference the enum for grep-ability.
    """

    BUILT_IN = "built-in"
    GLOBAL = "global"
    WORKSPACE = "workspace"
    REPO = "repo"
    BRANCH = "branch"
    LOCAL = "local"
    WAVE = "wave"
    ENV = "env"
    CLI = "cli"


# Canonical layer labels in precedence order (lowest → highest).
LAYER_ORDER: tuple[str, ...] = (
    Layer.BUILT_IN.value,
    Layer.GLOBAL.value,
    Layer.WORKSPACE.value,
    Layer.REPO.value,
    Layer.BRANCH.value,
    Layer.LOCAL.value,
    Layer.WAVE.value,
    Layer.ENV.value,
    Layer.CLI.value,
)

# Layers that map to a writable YAML file on disk. ``branch`` lives
# inside the repo (under ``.ea/branches/``) so the same write helpers
# apply once the caller provides the branch name.
_FILE_LAYERS: tuple[str, ...] = ("global", "workspace", "repo", "branch", "local")

# Subset of :data:`LAYER_ORDER` that ``config set`` may target. Built-in is
# read-only; env / cli / wave are runtime-only (wave is daemon-RAM-only).
WRITABLE_LAYERS: tuple[str, ...] = _FILE_LAYERS

# Env-var prefix for the ``env`` config layer (the ``EAWF_FOO__BAR`` form
# is described above; reflected in tests and ``eawf config get`` output).
_ENV_PREFIX: str = "EAWF_"


def global_config_path() -> Path:
    """Return ``~/.config/eawf/config.yaml`` for the *global* layer."""
    return Path.home() / ".config" / "eawf" / "config.yaml"


def workspace_config_path(workspace: Path) -> Path:
    """Return ``<workspace>/.ea/config.yaml``."""
    return Path(workspace) / ".ea" / "config.yaml"


def repo_config_path(repo: Path) -> Path:
    """Return ``<repo>/.ea/config.yaml``."""
    return Path(repo) / ".ea" / "config.yaml"


def local_config_path(repo: Path) -> Path:
    """Return ``<repo>/.ea/local/config.yaml``."""
    return Path(repo) / ".ea" / "local" / "config.yaml"


def branch_config_path(repo: Path, branch: str) -> Path:
    """Return ``<repo>/.ea/branches/<branch>.yaml`` for the named *branch*.

    Branch names that contain ``/`` (e.g. ``feature/eawf-v0.3-p25``) map to
    a subdirectory layout so the on-disk tree mirrors git's own
    ``.git/refs/heads/<branch>`` namespace. Empty or
    ``/``-only branch names raise ``ValueError`` — the loader walker
    cannot disambiguate them from missing files.
    """
    if not branch or branch.strip("/") == "":
        raise ValueError(f"branch name must be non-empty: {branch!r}")
    return Path(repo) / ".ea" / "branches" / f"{branch}.yaml"


def detect_current_branch(repo: Path | None) -> str | None:
    """Resolve the current branch via ``git symbolic-ref --short HEAD``.

    Args:
        repo: Repo root. ``None`` short-circuits to ``None`` so callers
            in workspace-only contexts skip the branch layer cleanly.

    Returns:
        Branch name on success, ``None`` when the call fails for any
        reason (detached HEAD, not a git tree, git binary absent). The
        loader treats ``None`` identically to a missing branch file.
    """
    if repo is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"detect_current_branch error repo={repo} err={exc!r}")
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def layer_path(
    layer: str,
    *,
    workspace: Path | None,
    repo: Path | None,
    branch: str | None = None,
) -> Path:
    """Return the on-disk YAML path for *layer*.

    Args:
        layer: Canonical layer label (must be in :data:`_FILE_LAYERS`).
        workspace: Workspace root (required for the ``workspace`` layer).
        repo: Repo root (required for ``repo``, ``branch``, and ``local``
            layers).
        branch: Branch name (required for the ``branch`` layer; ignored
            for every other layer). Subdirectory form is preserved
            verbatim — ``feature/foo`` resolves under
            ``.ea/branches/feature/foo.yaml``.

    Raises:
        ValueError: If *layer* is not a writable file layer, or required
            anchor (workspace/repo/branch) is missing.
    """
    if layer == "global":
        return global_config_path()
    if layer == "workspace":
        if workspace is None:
            raise ValueError("workspace path required for 'workspace' layer")
        return workspace_config_path(workspace)
    if layer == "repo":
        if repo is None:
            raise ValueError("repo path required for 'repo' layer")
        return repo_config_path(repo)
    if layer == "branch":
        if repo is None:
            raise ValueError("repo path required for 'branch' layer")
        if not branch:
            raise ValueError("branch name required for 'branch' layer")
        return branch_config_path(repo, branch)
    if layer == "local":
        if repo is None:
            raise ValueError("repo path required for 'local' layer")
        return local_config_path(repo)
    raise ValueError(f"unknown writable layer: {layer!r}")


def _flatten(mapping: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested mapping into ``a.b.c`` dotted keys.

    Lists and scalars are leaves; nested mappings recurse. ``None`` values are
    treated as scalar leaves so absent built-in defaults still own a source
    label.
    """
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        full = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict) and value:
            out.update(_flatten(value, full))
        else:
            out[full] = value
    return out


def _set_dotted(mapping: dict[str, Any], dotted: str, value: Any) -> None:
    """Set ``mapping[a][b][c] = value`` for dotted key ``a.b.c``.

    Intermediate keys are auto-created as dicts. If a non-dict already exists
    at an intermediate key, it is overwritten so the deeper-layer value wins
    (consistent with "later layers override earlier" for scalar→map upgrades).
    """
    parts = dotted.split(".")
    cur: dict[str, Any] = mapping
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _deep_merge_with_sources(
    *,
    base: dict[str, Any],
    base_sources: dict[str, str],
    overlay: Mapping[str, Any],
    overlay_layer: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Deep-merge *overlay* into *base*, recording the layer of each leaf.

    Maps merge per-key; lists and scalars replace wholesale ("ordinary
    lists replace" per the layered-config rules above). The keyed-list
    merge by stable ``id`` is deferred to Phase 3 W02 — for v0.1 P02 we
    treat all lists as ordinary.

    Returns:
        ``(merged, source_map)`` — fresh dicts; the inputs are not mutated.
    """
    merged = dict(base)
    sources = dict(base_sources)
    flat_overlay = _flatten(overlay)
    for dotted, value in flat_overlay.items():
        _set_dotted(merged, dotted, value)
        sources[dotted] = overlay_layer
    # Re-flatten the merged structure to surface any keys nested under maps
    # that the overlay did not name explicitly. They retain their prior layer.
    return merged, sources


#: ``EAWF_*`` env vars that are runtime control knobs, not config overrides.
#: These names are claimed by infrastructure (daemon escape hatch, verbose-
#: logging gate, etc.) and MUST be excluded from the layered-config env merge
#: so they cannot accidentally inject phantom top-level keys.
_RESERVED_ENV_VARS: frozenset[str] = frozenset(
    {
        "EAWF_DAEMONLESS",
        "EAWF_VERBOSE",
        "EAWF_REGISTRY_PATH",
        "EAWF_STATE",
        "EAWF_RUNTIME_DIR",
        "EAWF_DAEMON_IDLE_TIMEOUT",
        "EAWF_DAEMON_SESSION_TTL",
        "EAWF_LOCK_TIMEOUT",
        "EAWF_SKIP_GLOBAL_HOOKS",
        "EAWF_SKIP_PERF",
        "EAWF_BLITZ_DEPTH",
        "EAWF_BLITZ_DEPTH_COUNTER",
    }
)


def _collect_env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """Translate ``EAWF_FOO__BAR=baz`` env vars into ``{foo: {bar: "baz"}}``.

    The double-underscore is the conventional dotted-key separator (single
    underscores belong to a single key segment per the schema sample, e.g.
    ``schema_version``). Boolean strings (``true``/``false``, case-insensitive)
    and integer strings are coerced for ergonomic CLI overrides; everything
    else stays a string. JSON-mode callers can pre-coerce by passing CLI
    overrides directly via :func:`merge_config`.

    Reserved control vars (:data:`_RESERVED_ENV_VARS`) are skipped — they
    name runtime knobs, not config keys.
    """
    out: dict[str, Any] = {}
    for raw_key, raw_val in env.items():
        if not raw_key.startswith(_ENV_PREFIX):
            continue
        if raw_key in _RESERVED_ENV_VARS:
            continue
        stripped = raw_key[len(_ENV_PREFIX) :]
        if not stripped:
            continue
        dotted = stripped.replace("__", ".").lower()
        _set_dotted(out, dotted, _coerce_scalar(raw_val))
    return out


def _coerce_scalar(raw: str) -> Any:
    """Best-effort scalar coercion for string-typed env/CLI inputs."""
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


_LEGACY_RUNTIME_WARN_EMITTED = False


def _normalise_runtime_adapters(overlay: dict[str, Any]) -> None:
    """In-place legacy-kind shim: synthesise ``runtime.adapters`` from ``runtime.kind``.

    Applied per-overlay BEFORE the deep-merge so that a legacy overlay
    ``{runtime: {kind: <id>}}`` projects to
    ``{runtime: {kind: <id>, adapters: [<id>]}}`` and the synthesised
    adapter list wins over any earlier layer's value.

    Behaviour:
    - When the overlay has ``runtime.kind`` set but lacks (or has an
      empty) ``runtime.adapters``, replace ``runtime.adapters`` with
      ``[kind]`` and emit a one-time deprecation warning via
      ``logging.warning``.
    - When both keys exist, the explicit ``adapters`` list wins.
    - When the overlay carries no ``runtime`` block, the helper is a
      no-op.

    The deprecation warning fires at most once per process.
    """
    global _LEGACY_RUNTIME_WARN_EMITTED
    runtime = overlay.get("runtime")
    if not isinstance(runtime, dict):
        return
    adapters = runtime.get("adapters")
    kind = runtime.get("kind")
    if isinstance(adapters, list) and adapters:
        return
    if kind:
        runtime["adapters"] = [kind]
        if not _LEGACY_RUNTIME_WARN_EMITTED:
            logger.warning(
                f"deprecated_runtime_kind adapters=[{kind!r}]; config still uses "
                f"runtime.kind without runtime.adapters, synthesising adapters. "
                f"Bump config schema_version to 1.0 and emit runtime.preference "
                f"[<id>] to silence this warning."
            )
            _LEGACY_RUNTIME_WARN_EMITTED = True


def merge_config(
    *,
    workspace: Path | None = None,
    repo: Path | None = None,
    env: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    branch: str | None = None,
    wave_overlay: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Compose the layered config and return ``(merged, source_map)``.

    Args:
        workspace: Optional workspace root. When given, the ``workspace``
            layer's ``config.yaml`` is consulted.
        repo: Optional repo root. When given, the ``repo``, ``branch``,
            and ``local`` layers are consulted (the branch layer
            additionally requires *branch* to resolve to a non-``None``
            name).
        env: Environment mapping. Defaults to :data:`os.environ`. Pass an
            empty dict to disable env layer entirely.
        cli_overrides: Optional pre-flattened mapping (nested or dotted-leaf)
            applied as the highest-precedence layer.
        branch: Optional explicit branch name for the ``branch`` layer.
            When ``None`` and *repo* is given, the helper resolves the
            current branch via ``git symbolic-ref``; detached-HEAD or
            non-git contexts skip the branch layer silently.
        wave_overlay: Optional daemon-resident wave-layer overlay. The
            daemon keeps ``wave_config_overrides[wave_id]`` in RAM and
            passes the relevant dict in; library callers without a
            daemon context leave this ``None``.

    Returns:
        ``merged``: a freshly composed dict suitable for direct mutation by
        the caller.

        ``source_map``: a dict keyed by dotted leaf path, valued with the
        canonical layer label that contributed the leaf's final value.
        Layers that contribute no values (empty/missing files) leave no
        entries.
    """
    effective_env = env if env is not None else os.environ
    overrides = cli_overrides if cli_overrides is not None else {}

    # Layer 1: built-in defaults.
    merged = built_in_defaults()
    sources: dict[str, str] = dict.fromkeys(_flatten(merged), "built-in")

    # Layer 2: global.
    global_overlay = load_yaml_layer(global_config_path())
    _normalise_runtime_adapters(global_overlay)
    merged, sources = _deep_merge_with_sources(
        base=merged,
        base_sources=sources,
        overlay=global_overlay,
        overlay_layer="global",
    )

    # Layer 3: workspace (only if anchor provided).
    if workspace is not None:
        ws_overlay = load_yaml_layer(workspace_config_path(workspace))
        _normalise_runtime_adapters(ws_overlay)
        merged, sources = _deep_merge_with_sources(
            base=merged,
            base_sources=sources,
            overlay=ws_overlay,
            overlay_layer="workspace",
        )

    # Layer 4-6: repo + branch + local (only if anchor provided).
    if repo is not None:
        repo_overlay = load_yaml_layer(repo_config_path(repo))
        _normalise_runtime_adapters(repo_overlay)
        merged, sources = _deep_merge_with_sources(
            base=merged,
            base_sources=sources,
            overlay=repo_overlay,
            overlay_layer="repo",
        )

        # Layer 5: branch. Resolved via ``git symbolic-ref --short HEAD``
        # when the caller did not name the branch explicitly. Missing
        # branch file is silently skipped (same as a missing layer).
        effective_branch = branch if branch is not None else detect_current_branch(repo)
        if effective_branch is not None:
            branch_file = branch_config_path(repo, effective_branch)
            if branch_file.exists():
                branch_overlay = load_yaml_layer(branch_file)
                _normalise_runtime_adapters(branch_overlay)
                merged, sources = _deep_merge_with_sources(
                    base=merged,
                    base_sources=sources,
                    overlay=branch_overlay,
                    overlay_layer="branch",
                )

        # Layer 6: local.
        local_overlay = load_yaml_layer(local_config_path(repo))
        _normalise_runtime_adapters(local_overlay)
        merged, sources = _deep_merge_with_sources(
            base=merged,
            base_sources=sources,
            overlay=local_overlay,
            overlay_layer="local",
        )

    # Layer 7: wave (daemon RAM; library callers pass the dict in).
    if wave_overlay:
        # Wave overlay is transient — do NOT apply the legacy-adapter
        # shim here, the daemon already typed the values when it wrote
        # them to the per-wave map.
        merged, sources = _deep_merge_with_sources(
            base=merged,
            base_sources=sources,
            overlay=dict(wave_overlay),
            overlay_layer="wave",
        )

    # Layer 8: env.
    env_overlay = _collect_env_overrides(effective_env)
    if env_overlay:
        _normalise_runtime_adapters(env_overlay)
        merged, sources = _deep_merge_with_sources(
            base=merged,
            base_sources=sources,
            overlay=env_overlay,
            overlay_layer="env",
        )

    # Layer 9: CLI overrides.
    if overrides:
        normalised_overrides: Any = dict(overrides)
        _normalise_runtime_adapters(normalised_overrides)
        merged, sources = _deep_merge_with_sources(
            base=merged,
            base_sources=sources,
            overlay=normalised_overrides,
            overlay_layer="cli",
        )

    return merged, sources


def resolve_runtime_tier_models(repo_root: Path) -> dict[str, tuple[str, str, str]] | None:
    """Return the repo's ``runtime.models`` tier-ladder override, or ``None``.

    Reads the merged ``runtime.models`` config block and validates it through
    :class:`~eawf.kernel.config.schema.RuntimeModelsConfig` (strict,
    ``extra="forbid"``), projecting it into the ``runtime -> (cheap, mid, top)``
    map :func:`eawf.workflow.dispatch.routing.model_for_runtime` consumes. The
    dispatch path passes the result so a repo whose codex account serves
    different model ids than the built-in ladder (e.g. a ChatGPT-account codex)
    drives without editing source.

    Args:
        repo_root: Repo root the layered config is composed against (used as
            both the workspace and repo anchor, mirroring
            :func:`resolve_config_runtime_preference`'s call shape).

    Returns:
        A ``runtime -> (cheap, mid, top)`` map for every overridden runtime, or
        ``None`` when no ``runtime.models`` block is configured (so the caller
        falls back to the built-in ladder).

    Raises:
        pydantic.ValidationError: When the ``runtime.models`` block is present
            but malformed (unknown runtime key, or a ladder that is not a
            3-tuple of strings).
    """
    from eawf.kernel.config.schema import RuntimeModelsConfig

    merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
    runtime = merged.get("runtime")
    if not isinstance(runtime, dict):
        return None
    raw = runtime.get("models")
    if raw is None:
        return None
    override = RuntimeModelsConfig.model_validate(raw).as_override()
    return override or None


def get_dotted(
    merged: Mapping[str, Any],
    dotted: str,
) -> Any:
    """Return ``merged[a][b][c]`` for the dotted key ``a.b.c`` or raise ``KeyError``."""
    parts = dotted.split(".")
    cur: Any = merged
    for part in parts:
        if not isinstance(cur, Mapping) or part not in cur:
            raise KeyError(dotted)
        cur = cur[part]
    return cur
