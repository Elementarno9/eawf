"""``eawf plugin install/update/doctor {claude,codex,opencode}`` Typer commands.

This module is the facade for the ``plugin`` command group.
It owns the :data:`plugin_app` Typer group, the three monkeypatchable
runtime-conflict detector wrappers, the install-conflict gates, the
scope / runtime validators, and the small path / banner resolvers. The
concrete verb bodies live in :mod:`eawf.surfaces.cli.commands.plugin_verbs`, and
the JSON / text envelope renderers live in
:mod:`eawf.surfaces.cli.commands.plugin_render`. The verb module attaches its
handlers via ``@plugin_app.command(...)``; importing this module imports
both siblings (at the bottom, after every shared symbol is defined) so
``plugin_app`` carries its full verb set.

Tests monkeypatch the detector wrappers by the qualified name
``eawf.surfaces.cli.commands.plugin.<detector>``; the conflict gates here look
those names up through this module's namespace, so the monkeypatch
takes effect regardless of which sibling imported the gate.

Surface contract:

- ``eawf plugin install <runtime> [--scope project|user] [--force] [--dry-run]``
  renders the runtime plugin tree. ``--scope user`` writes under the
  runtime's user-scope config dir; ``--scope project`` (default)
  writes under the workspace. ``claude`` rejects ``--scope user``
  (use the CC marketplace export instead).
- ``eawf plugin update <runtime> [--scope ...]`` re-renders the tree,
  aborting on hand-edits (exit 8 / ``INTEGRITY_VIOLATION``).
- ``eawf plugin doctor <runtime> [--scope ...] [--strict]`` reports
  drift / missing files; clean exits 0, dirty exits 8. ``--strict``
  runs the Claude checksum sweep under the shared sync/doctor
  portalock so the drift gate reads the post-``plugin sync`` checksum
  from the same lock-scope (closes the sync-then-doctor race per the
  C09 F19 mitigation).

Idempotent: re-running ``install`` against an unchanged source tree
produces a byte-identical tree. Hand-edits abort with exit 8 unless
``--force`` is passed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags

if TYPE_CHECKING:
    # Annotation-only result/report types. The runtime values
    # (install/doctor/package/update/sync functions + integrity
    # exceptions) are imported lazily inside each command handler so
    # importing this module for completion does not pull eawf.runtime.runtimes
    # (and its jinja2/yaml transitive deps).
    from eawf.runtime.runtimes.claude.plugin_conflict import CCPluginConflict
    from eawf.runtime.runtimes.codex.plugin_conflict import CodexUserPluginConflict
    from eawf.runtime.runtimes.manifest import RuntimeId
    from eawf.runtime.runtimes.opencode.plugin_conflict import OpenCodeUserPluginConflict
    from eawf.runtime.runtimes.runtime_set import ClaimDetector, RuntimeSetConflictError

logger = logging.getLogger(__name__)


# Module-level lazy wrappers for the three runtime conflict detectors.
# Tests monkeypatch these names (``eawf.surfaces.cli.commands.plugin.<detector>``)
# to inject synthetic conflicts, so they must stay module-level
# attributes; the real implementations are imported lazily inside each
# wrapper so importing this module for shell completion does not pull
# ``eawf.runtime.runtimes.*`` (and its jinja2 transitive dep).
def detect_marketplace_install() -> CCPluginConflict | None:
    """Detect an existing CC-marketplace eawf install (lazy import)."""
    from eawf.runtime.runtimes.claude.plugin_conflict import (
        detect_marketplace_install as _impl,
    )

    return _impl()


def codex_detect_user_install() -> CodexUserPluginConflict | None:
    """Detect a user-scope codex eawf install (lazy import)."""
    from eawf.runtime.runtimes.codex import detect_user_install as _impl

    return _impl()


def opencode_detect_user_install() -> OpenCodeUserPluginConflict | None:
    """Detect a user-scope opencode eawf install (lazy import)."""
    from eawf.runtime.runtimes.opencode import detect_user_install as _impl

    return _impl()


Scope = Literal["project", "user"]


plugin_app = typer.Typer(
    name="plugin",
    help=(
        "Install, update, or diagnose runtime plugins (claude, codex, opencode). "
        "Use 'install' for all three; 'package' is Claude-only (marketplace export)."
    ),
    no_args_is_help=True,
)


_SUPPORTED_RUNTIMES: tuple[str, ...] = ("claude", "codex", "opencode")
_VALID_SCOPES: tuple[str, ...] = ("project", "user")


def _validate_runtime(runtime: str) -> None:
    """Reject runtimes outside the v0.1 supported list."""
    if runtime not in _SUPPORTED_RUNTIMES:
        raise cli_errors.UserError(
            f"unknown runtime {runtime!r}; expected one of {list(_SUPPORTED_RUNTIMES)}",
            kind="InvalidInput",
        )


def _validate_scope(scope: str, *, runtime: str) -> None:
    """Reject invalid scope values, and ``user`` for claude.

    Raises:
        UserError: when *scope* is not ``project`` / ``user``, or
            when *runtime* is ``claude`` and *scope* is ``user``
            (``kind="InvalidInput"``).
    """
    if scope not in _VALID_SCOPES:
        raise cli_errors.UserError(
            f"invalid --scope {scope!r}; expected one of {list(_VALID_SCOPES)}", kind="InvalidInput"
        )
    if runtime == "claude" and scope == "user":
        raise cli_errors.UserError(
            "claude is project-scope only; use the CC marketplace for user-scope installs",
            kind="InvalidInput",
        )


def _validate_plugin_root(runtime: str | None, plugin_root: Path | None) -> None:
    """Reject ``--plugin-root`` outside the Codex-specific lifecycle surface."""
    if plugin_root is not None and runtime != "codex":
        raise cli_errors.UserError(
            f"--plugin-root applies to the codex runtime only (got {runtime!r})",
            kind="InvalidInput",
        )


def _claim_detectors() -> dict[RuntimeId, ClaimDetector]:
    """Adapt the detector wrappers above to the runtime-set guard's detector shape.

    Each adapter looks its wrapper up by module-global name at call time, so a
    test that monkeypatches the wrapper is seen by the guard.
    """

    def _claude() -> Path | None:
        conflict = detect_marketplace_install()
        return None if conflict is None else conflict.plugin_dir

    def _codex() -> Path | None:
        conflict = codex_detect_user_install()
        return None if conflict is None else conflict.plugin_dir

    def _opencode() -> Path | None:
        conflict = opencode_detect_user_install()
        return None if conflict is None else conflict.plugin_file

    return {"claude-code": _claude, "codex": _codex, "opencode": _opencode}


def _confirm_runtime_conflict(conflict: RuntimeSetConflictError, *, flags: GlobalFlags) -> bool:
    """Return ``True`` when the operator acknowledges *conflict* at the prompt.

    ``--no-input`` refuses with a :exc:`UserError` (``kind="InvalidInput"``)
    so the operator can pick one install before retrying; otherwise a
    :mod:`questionary` confirmation decides, and ``No`` aborts cleanly.
    """
    claude_claimed = any(claim.runtime == "claude-code" for claim in conflict.claims)
    remedy = (
        "`/plugin uninstall eawf@eawf` inside Claude Code first"
        if claude_claimed
        else "remove the user-scope install first"
    )
    if flags.no_input:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"{conflict}. Rerun without --no-input to confirm, pass --force "
                f"to acknowledge, or {remedy}.",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return False
    import questionary

    proceed = questionary.confirm(
        f"{conflict}.\nProceed with the project-local install anyway?",
        default=False,
    ).ask()
    if not proceed:
        runtimes = ", ".join(claim.runtime for claim in conflict.claims)
        print(f"plugin install {runtimes}: aborted (conflict not acknowledged)")
        return False
    return True


def _install_conflict_clear(
    *, runtime_set: tuple[RuntimeId, ...], scope: str, flags: GlobalFlags, force: bool
) -> bool:
    """Return ``True`` when no installed plugin claims a runtime in *runtime_set*.

    *runtime_set* is the already-normalized, fully expanded set the verb will
    write, from :func:`~eawf.runtime.runtimes.runtime_set.normalize_runtime_set`,
    so every verb gates the same canonical ids. ``--force`` bypasses the gate.
    """
    from eawf.runtime.runtimes.runtime_set import RuntimeSetConflictError, guard_runtime_set

    try:
        guard_runtime_set(
            runtime_set,
            scope=cast(Scope, scope),
            detectors=_claim_detectors(),
            force=force,
        )
    except RuntimeSetConflictError as conflict:
        return _confirm_runtime_conflict(conflict, flags=flags)
    return True


def _resolve_target(flags: GlobalFlags) -> Path:
    """Resolve the workspace root the plugin commands operate against."""
    return (flags.workspace or Path.cwd()).resolve()


def _default_package_target(flags: GlobalFlags, *, runtime: str) -> Path:
    """Default ``--target`` for ``plugin package``.

    Claude ships its plugin tree under ``<workspace>/build/eawf-plugin/``;
    Codex ships its marketplace tree under
    ``<workspace>/build/eawf-codex-marketplace/`` so the two outputs
    coexist when both are built side-by-side.
    """
    base = (flags.workspace or Path.cwd()).resolve()
    if runtime == "codex":
        return base / "build" / "eawf-codex-marketplace"
    return base / "build" / "eawf-plugin"


def _scope_tip_banner(*, runtime: str, scope: str, result_path: Path) -> str | None:
    """Return the post-install tip banner (text-mode only) or ``None`` to suppress.

    Shown when *runtime* is codex/opencode and *scope* is project — points
    out the cross-project alternative. Suppressed for claude and for
    user-scope installs (already where the user asked for).
    """
    if runtime == "claude" or scope != "project":
        return None
    return f"tip: --scope user installs cross-project (alongside or in lieu of {result_path})"


def _normalise_runtime_flags(values: list[str]) -> tuple[RuntimeId, ...]:
    """Return the canonical, fully expanded runtime set for *values*.

    Raises:
        UserError: when a value is not a recognised spelling (``kind="InvalidInput"``).
    """
    from eawf.runtime.runtimes.runtime_set import normalize_runtime_set

    try:
        return normalize_runtime_set(values)
    except ValueError as exc:
        raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc


# ---- command registration ---------------------------------------------------
# Importing the sibling modules runs their ``@plugin_app.command(...)``
# decorators so the app above carries its full verb set. The imports sit
# at the bottom, after every shared symbol is defined, so the siblings
# can import the app, the gates, and the renderers from this module
# without a circular-import failure.
from eawf.surfaces.cli.commands import plugin_render as _plugin_render  # noqa: E402, F401
from eawf.surfaces.cli.commands import plugin_verbs as _plugin_verbs  # noqa: E402, F401


def detect_cross_scope_duplicates(workspace: Path) -> list[str]:
    """Return region_ids installed under both ``project`` and ``user`` scope.

    Reads ``<workspace>/.ea/indexes/generated.json`` (the shared
    sidecar manifest the codex / opencode installers write via their
    ``_persist_manifest`` helpers), groups :class:`ManifestEntry` rows
    by ``region_id``, and surfaces any region whose ``scope`` set
    spans more than one of ``{"project", "user"}``.

    A cross-scope duplicate means the runtime (codex or opencode) will
    see two grants of the same plugin region with undefined
    precedence — the operator needs to pick one and uninstall the
    other. The detector is the cross-scope twin of the in-runtime
    install-time runtime-set guard (``_install_conflict_clear``): that
    one runs at install time and blocks; this one runs on demand and
    reports.

    Returns:
        Sorted list of duplicate region_ids. Empty when the manifest
        is absent, unreadable, or carries no rows with ``scope`` set
        at more than one value.
    """
    from eawf.surfaces.render.manifest import load as load_manifest

    manifest_path = workspace / ".ea" / "indexes" / "generated.json"
    if not manifest_path.exists():
        return []
    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        logger.debug(f"detect_cross_scope_duplicates status=unreadable error={exc!r}")
        return []
    scopes_by_region: dict[str, set[str]] = {}
    for entry in manifest.generated.values():
        if entry.scope is None:
            continue
        scopes_by_region.setdefault(entry.region_id, set()).add(entry.scope)
    return sorted(rid for rid, scopes in scopes_by_region.items() if len(scopes) > 1)


__all__ = [
    "detect_cross_scope_duplicates",
    "plugin_app",
]
