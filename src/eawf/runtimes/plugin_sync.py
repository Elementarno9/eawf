"""``eawf plugin sync`` — regenerate per-runtime artifacts deterministically.

Plugin sync is the canonical per-runtime artifact regeneration verb.
The existing ``eawf plugin install <runtime>`` surface stays as the
per-runtime affordance; ``plugin sync`` is the **single-shot
multi-runtime** orchestrator that drives all three runtimes in one
call, deterministically derived from :data:`SKILL_REGISTRY` +
:data:`AGENT_REGISTRY` + :data:`HOOK_REGISTRY`.

Architectural shape
-------------------

Plugin sync is **dispatch**, not implementation: it delegates to
the three per-runtime ``install_plugin`` functions that already
handle the renderer details (each runtime's manifest schema,
sidecar layout, scope rules). The orchestrator's added value is:

1. A single entry point that emits all three runtimes in one
   call so a CI workflow / doctor invocation does not need to
   know the per-runtime call conventions.
2. Frozen timestamp default (:data:`~eawf.runtimes.helpers.fs.
   DEFAULT_TIMESTAMP`) so a re-run produces byte-identical output.
3. A :class:`SyncResult` aggregating each runtime's
   :class:`~eawf.runtimes.helpers.fs.FileDelta` list under the
   runtime id, ready for JSON / text rendering by the CLI.

Per the authority map (row 14, 2026-05-18 ratification), the
**daemon** is the canonical writer for the plugin output paths
``.claude/`` + ``.codex/`` + ``.opencode/``. During v0.3-v0.5 the
operator-facing CLI calls into ``plugin_sync.sync_plugins`` which
in turn calls the per-runtime ``install_plugin`` functions; the
v0.4 hygiene wave migrates those file writes through the
``daemon.plugin.sync`` RPC. Until that migration lands the call
chain is in-process — V1 fallback under daemonless contexts
preserves the same write surface.

Scope notes
-----------

* ``scope="project"`` (default) writes under *target_dir*.
* ``scope="user"`` writes under each runtime's user-scope config
  dir (``~/.codex/...`` / ``~/.config/opencode/...``). Claude does
  not support user-scope plugin install — the marketplace export
  is the CC equivalent — so user-scope sync skips Claude.
* ``runtimes=(...)`` restricts the orchestrator to a subset (e.g.
  ``runtimes=("claude-code",)``). Empty / unset means "all three".
* ``dry_run=True`` returns deltas without writing any bytes.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from eawf.runtimes.claude.plugin_install import IntegrityViolation as ClaudeIntegrity
from eawf.runtimes.claude.plugin_install import install_plugin as install_claude_plugin
from eawf.runtimes.codex.plugin_install import IntegrityViolation as CodexIntegrity
from eawf.runtimes.codex.plugin_install import install_plugin as install_codex_plugin
from eawf.runtimes.helpers import DEFAULT_TIMESTAMP, FileDelta
from eawf.runtimes.manifest import RuntimeId
from eawf.runtimes.opencode.plugin_install import IntegrityViolation as OpencodeIntegrity
from eawf.runtimes.opencode.plugin_install import install_plugin as install_opencode_plugin

logger = logging.getLogger(__name__)


SyncScope = Literal["project", "user"]


_ALL_RUNTIMES: tuple[RuntimeId, ...] = ("claude-code", "codex", "opencode")


class PluginSyncIntegrityError(Exception):
    """Raised when one of the per-runtime renderers refuses due to drift.

    Wraps the per-runtime :class:`IntegrityViolation` so a caller
    only needs to catch one type to handle ``--force`` / re-prompt
    branches. The original exception is preserved on
    ``__cause__`` so the runtime that flagged drift is recoverable.
    """


@dataclass(frozen=True)
class RuntimeSyncResult:
    """One runtime's per-file delta record.

    Attributes:
        runtime: Canonical runtime identifier
            (``"claude-code"`` / ``"codex"`` / ``"opencode"``).
        deltas: Ordered :class:`FileDelta` list — one entry per
            file the renderer wrote (or skipped under ``dry_run``).
        dry_run: ``True`` when the run was a dry run (no bytes
            written to disk).
    """

    runtime: RuntimeId
    deltas: list[FileDelta] = field(default_factory=list)
    dry_run: bool = False


@dataclass(frozen=True)
class SyncResult:
    """Aggregated result of one :func:`sync_plugins` call.

    Attributes:
        target_dir: Workspace root the sync ran against.
        scope: Either ``"project"`` or ``"user"`` (Claude is
            skipped under ``"user"`` per the CLI contract).
        results: Per-runtime :class:`RuntimeSyncResult` list in
            requested order (Claude → Codex → OpenCode).
        skipped: Runtimes that were requested but skipped (e.g.
            Claude under ``scope="user"``).
        dry_run: ``True`` when the run was a dry run.
    """

    target_dir: Path
    scope: SyncScope
    results: list[RuntimeSyncResult] = field(default_factory=list)
    skipped: list[RuntimeId] = field(default_factory=list)
    dry_run: bool = False


def _normalise_runtimes(runtimes: Sequence[RuntimeId] | None) -> tuple[RuntimeId, ...]:
    """Return the canonical-order runtime tuple to drive.

    Args:
        runtimes: Caller-requested subset, or ``None`` for "all".

    Returns:
        Canonical-order tuple — Claude first, then Codex, then
        OpenCode — restricted to the requested subset when one
        was supplied.
    """
    if not runtimes:
        return _ALL_RUNTIMES
    requested = set(runtimes)
    return tuple(r for r in _ALL_RUNTIMES if r in requested)


def _flatten_claude(result: object) -> list[FileDelta]:
    """Flatten one Claude :class:`InstallResult` into ordered deltas."""
    # Avoid importing the Claude InstallResult symbol at module level
    # to keep the helpers/runtimes import graph thin. The attribute
    # surface is stable (skills / agents / hooks / settings).
    deltas: list[FileDelta] = []
    for delta in result.skills:  # type: ignore[attr-defined]
        deltas.append(FileDelta(path=delta.path, action=delta.action))
    for delta in result.agents:  # type: ignore[attr-defined]
        deltas.append(FileDelta(path=delta.path, action=delta.action))
    for delta in result.hooks:  # type: ignore[attr-defined]
        deltas.append(FileDelta(path=delta.path, action=delta.action))
    settings_delta = result.settings  # type: ignore[attr-defined]
    if settings_delta is not None:
        deltas.append(FileDelta(path=settings_delta.path, action=settings_delta.action))
    return deltas


def _flatten_codex(result: object) -> list[FileDelta]:
    """Flatten one Codex :class:`InstallResult` into ordered deltas."""
    deltas: list[FileDelta] = []
    for delta in result.skills:  # type: ignore[attr-defined]
        deltas.append(FileDelta(path=delta.path, action=delta.action))
    for delta in result.hooks:  # type: ignore[attr-defined]
        deltas.append(FileDelta(path=delta.path, action=delta.action))
    for attr in ("manifest", "sidecar", "config"):
        delta = getattr(result, attr, None)
        if delta is not None:
            deltas.append(FileDelta(path=delta.path, action=delta.action))
    return deltas


def _flatten_opencode(result: object) -> list[FileDelta]:
    """Flatten one OpenCode :class:`InstallResult` into ordered deltas."""
    deltas: list[FileDelta] = []
    for attr in ("plugin_js", "sidecar", "config"):
        delta = getattr(result, attr, None)
        if delta is not None:
            deltas.append(FileDelta(path=delta.path, action=delta.action))
    for delta in result.agents:  # type: ignore[attr-defined]
        deltas.append(FileDelta(path=delta.path, action=delta.action))
    for delta in result.commands:  # type: ignore[attr-defined]
        deltas.append(FileDelta(path=delta.path, action=delta.action))
    return deltas


def sync_plugins(
    target_dir: Path,
    *,
    scope: SyncScope = "project",
    runtimes: Sequence[RuntimeId] | None = None,
    force: bool = False,
    dry_run: bool = False,
    timestamp: str | None = None,
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> SyncResult:
    """Regenerate per-runtime plugin artifacts deterministically.

    The orchestrator walks the requested runtimes in canonical
    order (Claude → Codex → OpenCode), delegating to each
    runtime's :func:`install_plugin` with shared inputs (frozen
    timestamp by default, pass-through ``force`` / ``dry_run``).

    Args:
        target_dir: Workspace root. ``scope="user"`` ignores this
            for the per-runtime install path but the runtime
            renderers still resolve their workspace-relative
            artifacts (e.g. Codex's ``config.toml`` at
            ``<target_dir>/.codex/config.toml`` for project scope).
        scope: ``"project"`` (default) writes under *target_dir*;
            ``"user"`` writes under the runtime's user-scope
            config dir. Claude is skipped under user-scope.
        runtimes: Subset of canonical runtime ids
            (``"claude-code"`` / ``"codex"`` / ``"opencode"``).
            ``None`` / empty means "all".
        force: When ``True``, hand-edits to managed files are
            overwritten silently. Passed through to each runtime's
            renderer.
        dry_run: When ``True``, no bytes are written; the
            :class:`SyncResult` enumerates what would be written.
        timestamp: ISO 8601 UTC timestamp baked into managed
            namespaces / sidecars. Defaults to
            :data:`~eawf.runtimes.helpers.fs.DEFAULT_TIMESTAMP`
            for byte stability across runs.
        home: Override for :meth:`pathlib.Path.home`. Tests pass
            ``tmp_path``; production callers leave it ``None``.
        opencode_config_dir: Override for ``$OPENCODE_CONFIG_DIR``
            (OpenCode user-scope). Tests pass an explicit path.

    Returns:
        :class:`SyncResult` aggregating the per-runtime renderer
        deltas, plus the list of runtimes that were skipped.

    Raises:
        PluginSyncIntegrityError: One of the per-runtime
            renderers refused due to a hand-edit. The original
            per-runtime exception is chained via ``__cause__``.
    """
    target_dir = Path(target_dir).resolve()
    requested = _normalise_runtimes(runtimes)
    ts = timestamp or DEFAULT_TIMESTAMP
    results: list[RuntimeSyncResult] = []
    skipped: list[RuntimeId] = []

    for runtime in requested:
        if runtime == "claude-code":
            if scope == "user":
                # Claude does not support per-user plugin install via
                # the workspace tree — the CC marketplace export
                # serves that channel. Record the skip but continue.
                skipped.append(runtime)
                continue
            try:
                claude_result = install_claude_plugin(
                    target_dir,
                    force=force,
                    dry_run=dry_run,
                    timestamp=ts,
                )
            except ClaudeIntegrity as exc:
                raise PluginSyncIntegrityError(f"plugin sync claude-code refused: {exc}") from exc
            results.append(
                RuntimeSyncResult(
                    runtime=runtime,
                    deltas=_flatten_claude(claude_result),
                    dry_run=dry_run,
                )
            )
        elif runtime == "codex":
            try:
                codex_result = install_codex_plugin(
                    target_dir,
                    scope=scope,
                    force=force,
                    dry_run=dry_run,
                    timestamp=ts,
                    home=home,
                )
            except CodexIntegrity as exc:
                raise PluginSyncIntegrityError(f"plugin sync codex refused: {exc}") from exc
            results.append(
                RuntimeSyncResult(
                    runtime=runtime,
                    deltas=_flatten_codex(codex_result),
                    dry_run=dry_run,
                )
            )
        elif runtime == "opencode":
            try:
                oc_result = install_opencode_plugin(
                    target_dir,
                    scope=scope,
                    force=force,
                    dry_run=dry_run,
                    timestamp=ts,
                    home=home,
                    opencode_config_dir=opencode_config_dir,
                )
            except OpencodeIntegrity as exc:
                raise PluginSyncIntegrityError(f"plugin sync opencode refused: {exc}") from exc
            results.append(
                RuntimeSyncResult(
                    runtime=runtime,
                    deltas=_flatten_opencode(oc_result),
                    dry_run=dry_run,
                )
            )

    logger.info(
        f"sync_plugins target_dir={target_dir} scope={scope} "
        f"runtimes={[r.runtime for r in results]} skipped={skipped} "
        f"dry_run={dry_run}"
    )
    return SyncResult(
        target_dir=target_dir,
        scope=scope,
        results=results,
        skipped=skipped,
        dry_run=dry_run,
    )


__all__ = [
    "PluginSyncIntegrityError",
    "RuntimeSyncResult",
    "SyncResult",
    "SyncScope",
    "sync_plugins",
]
