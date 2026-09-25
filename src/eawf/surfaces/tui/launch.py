"""``eawf tui``'s launcher: resolve a tree's authority, then open the right app.

A tree is in epoch 1 or epoch 2 (:func:`~eawf.kernel.state.epoch2.authority.resolve_authority`),
and the launcher opens a different app for each: the epoch-2 console
(:class:`~eawf.surfaces.tui.console.app.ConsoleApp`) reading the live daemon projection
over one seam, or the epoch-1 :class:`~eawf.surfaces.tui.app.EaApp` every tree ran before
it. A tree declared for the epoch-2 canary but stuck mid-migration (the marker absent or
unreadable) is neither: it opens the console on its own pre-session entry layer instead,
so the operator reads the exact repair command rather than a silent fallback to the
epoch-1 view.

This module is the library side of "CLI is dispatch": :func:`launch_tui` is the one
entry point, and :mod:`eawf.surfaces.cli.app` only resolves flags and calls it.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.kernel.state.epoch2.authority import AuthorityGap, RootAuthority, resolve_authority
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.tui.console.chrome import ConsoleChrome, load_chrome

if TYPE_CHECKING:
    from eawf.surfaces.tui.console.app import ConsoleApp
    from eawf.surfaces.tui.console.seam import ProjectionSeam

#: SURF-085: the exit code a terminal entry-layer state returns off a TTY. Nothing
#: interactive can be shown there, and the deterministic status frame would misstate a
#: tree the resolver could not attach to, so this stands in for both.
TERMINAL_ENTRY_EXIT_CODE = 4

#: The entry-layer state a stuck migration's exit key itself names ("quit - exit 4"),
#: reused for the interactive path once the operator leaves that screen.
_TERMINAL_EXIT = "terminal"

#: The console's pre-session route id, whose key the session addresses it by.
_ENTRY_ROUTE = "entry"

#: The route the seam opens on before the app retargets it to the session's own route.
_HOME_ROUTE = "scope.home"

#: Which entry-layer state names a canary tree stuck mid-migration, by the gap
#: :func:`resolve_authority` withheld epoch 2 for. ``AuthorityGap.UNDECLARED`` -- a tree
#: that never declared the epoch-2 canary -- is not one of these: it is the ordinary
#: epoch-1 tree every repo runs as today, so it keeps ``EaApp`` rather than showing a
#: migration screen.
_GAP_ENTRY_STATE: dict[AuthorityGap, str] = {
    AuthorityGap.MARKER_ABSENT: "migration",
    AuthorityGap.MARKER_UNREADABLE: "interrupted",
}

#: Printed before the epoch-1 app opens on an ordinary epoch-1 tree, so an operator who
#: expected the native console knows why they did not get it.
EPOCH1_NOTICE = "eawf tui: this tree is epoch-1; opening the classic console"


def entry_state_id_for(authority: RootAuthority) -> str | None:
    """Return the entry-layer state id a stuck migration should open the console on.

    Args:
        authority: The tree's resolved authority.

    Returns:
        The state id, or ``None`` when ``authority`` names no migration in progress:
        epoch 2 (there is no pre-session layer to show) or a plain, never-declared
        epoch-1 tree.
    """
    if authority.gap is None:
        return None
    return _GAP_ENTRY_STATE.get(authority.gap)


def _entry_sel(chrome: ConsoleChrome, state_id: str) -> int:
    """Return the packaged chrome's entry-state index named ``state_id``.

    Raises:
        ValueError: no entry state in the packaged chrome carries this id.
    """
    for index, state in enumerate(chrome.entry):
        if state.id == state_id:
            return index
    raise ValueError(f"chrome carries no entry state {state_id!r}")


def _is_terminal(chrome: ConsoleChrome, state_id: str) -> bool:
    """Return whether entry state ``state_id`` ends the session on its own Escape."""
    return chrome.entry[_entry_sel(chrome, state_id)].exit == _TERMINAL_EXIT


def launch_tui(
    *,
    workspace: Path | None,
    no_input: bool,
    plain: bool,
    verbose: bool = False,
) -> int:
    """Resolve the tree's authority and open the matching app, or fall back off a TTY.

    Args:
        workspace: Optional workspace root from ``-w/--workspace``.
        no_input: Fail-closed flag -- forces the deterministic status fallback.
        plain: Plain-output flag -- forces the deterministic status fallback.
        verbose: Whether the native console's ``--verbose`` key-trace row is shown
            (SURF-173). Has no effect on the epoch-1 app, which carries no such row.

    Returns:
        Process exit code (``0`` on a clean quit).
    """
    tty = sys.stdout.isatty()
    explicit_state = workspace is not None or bool(os.environ.get("EA_STATE"))

    if not tty and not explicit_state:
        # Nobody chose a tree: no -w/--workspace and no EA_STATE. Resolving now
        # would walk pwd-upward onto whatever tree happens to sit above cwd, so
        # the ambient bare/headless invocation skips authority resolution
        # entirely and prints the deterministic status frame, which does its
        # own cheap cwd-relative read instead. An explicit override or an
        # interactive TTY both mean the caller (or the operator) did pick a
        # tree on purpose, so those still resolve below for epoch/SURF-085
        # detection.
        from eawf.surfaces.tui.chassis.offline import emit_status

        return emit_status(workspace=workspace, no_input=no_input, plain=plain)

    state_path, _reason = resolve_with_reason(workspace=workspace)
    root = state_path.parent.parent
    authority = resolve_authority(root)
    chrome = load_chrome()
    state_id = entry_state_id_for(authority)

    if state_id is not None and _is_terminal(chrome, state_id) and not tty:
        return TERMINAL_ENTRY_EXIT_CODE

    if no_input or plain or not tty:
        from eawf.surfaces.tui.chassis.offline import emit_status

        return emit_status(workspace=workspace, no_input=no_input, plain=plain)

    if authority.epoch == 2:
        return _launch_native(
            authority=authority, state_path=state_path, chrome=chrome, verbose=verbose
        )

    if state_id is not None:
        return _launch_entry(chrome=chrome, state_id=state_id, verbose=verbose)

    print(EPOCH1_NOTICE, file=sys.stderr)
    return _launch_epoch1(state_path=state_path)


def _launch_native(
    *, authority: RootAuthority, state_path: Path, chrome: ConsoleChrome, verbose: bool
) -> int:
    """Open the console over a live seam bound to the tree's epoch-2 authority."""
    from eawf.runtime.daemon.epoch2_root import RootIdentity
    from eawf.surfaces.tui.console.app import ConsoleApp
    from eawf.surfaces.tui.console.clock import Clock
    from eawf.surfaces.tui.console.seam import ProjectionSeam

    scope_id = RootIdentity.of(authority.root).root_id
    seam = ProjectionSeam(
        route=_HOME_ROUTE, scope_id=scope_id, state_path=state_path, repo_root=authority.root
    )
    app = ConsoleApp(chrome=chrome, seam=seam, clock=Clock(), verbose=verbose)
    return _run_console(app, seam)


def _launch_entry(*, chrome: ConsoleChrome, state_id: str, verbose: bool) -> int:
    """Open the console on the pre-session entry state a stuck migration names."""
    from eawf.surfaces.tui.console.app import ConsoleApp
    from eawf.surfaces.tui.console.clock import Clock
    from eawf.surfaces.tui.console.session import SessionSetup

    app = ConsoleApp(chrome=chrome, clock=Clock(), verbose=verbose)
    app.reset(SessionSetup(route=_ENTRY_ROUTE, entry_sel=_entry_sel(chrome, state_id)))
    _run_console(app, None)
    if _is_terminal(chrome, state_id):
        return TERMINAL_ENTRY_EXIT_CODE
    return 0


def _run_console(app: ConsoleApp, seam: ProjectionSeam | None) -> int:
    """Run ``app`` to completion, holding ``seam``'s transport open for its length.

    Args:
        app: The console to run. Blocks until the operator quits.
        seam: The console's live link, connected before the run and disconnected
            after; ``None`` for a console that draws no route from a seam at all
            (the pre-session entry layer).

    Returns:
        ``0``; a caller that needs a different code (a terminal entry state) applies
        it after this returns, since the console itself has no exit code of its own.
    """

    async def _drive() -> None:
        if seam is not None:
            await seam.connect()
        try:
            await app.run_async()
        finally:
            if seam is not None:
                await seam.disconnect()

    asyncio.run(_drive())
    return 0


def _launch_epoch1(*, state_path: Path) -> int:
    """Open the epoch-1 :class:`~eawf.surfaces.tui.app.EaApp`, exactly as it always has."""
    import orjson

    from eawf.kernel.state.enums import ScopeKind
    from eawf.surfaces.tui.app import resolve_scope, run_app

    if state_path.is_file():
        try:
            payload = orjson.loads(state_path.read_bytes())
            scope_kind = ScopeKind(payload["scope_kind"])
        except orjson.JSONDecodeError, OSError, KeyError, ValueError:
            return run_app("repo", state_path)
        return run_app(resolve_scope(scope_kind), state_path)
    return run_app("user", None)


__all__ = [
    "EPOCH1_NOTICE",
    "TERMINAL_ENTRY_EXIT_CODE",
    "entry_state_id_for",
    "launch_tui",
]
