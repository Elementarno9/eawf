"""``eawf tui``'s launcher: resolve a tree's authority, then open the right app.

A tree is in epoch 1 or epoch 2 (:func:`~eawf.kernel.state.epoch2.authority.resolve_authority`),
and the launcher opens a different app for each: the epoch-2 console
(:class:`~eawf.surfaces.tui.console.app.ConsoleApp`) reading the live daemon projection
over one seam, or the epoch-1 :class:`~eawf.surfaces.tui.app.EaApp` every tree ran before
it. A launch that cannot simply attach -- no registered root, an ambiguous one, a
migration owed or stopped part-way, a schema this console cannot read -- is neither: the
attach path (:func:`~eawf.surfaces.tui.console.attach.resolve_attach`) names the entry
state it landed in, and the console opens on that pre-session frame so the operator reads
the exact next command rather than a silent fallback to the epoch-1 view. A native
console that did attach draws the resolving frame until its first projection arrives.

This module is the library side of "CLI is dispatch": :func:`launch_tui` is the one
entry point, and :mod:`eawf.surfaces.cli.app` only resolves flags and calls it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.kernel.state.epoch2.authority import RootAuthority, resolve_authority
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.platform.registry import default_registry_path
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.tui.console.attach import (
    AttachRequest,
    offline_snapshot,
    offline_state,
    resolve_attach,
    resolving_state,
    with_entry_state,
)
from eawf.surfaces.tui.console.chrome import ConsoleChrome, EntryState, load_chrome
from eawf.surfaces.tui.terminal_logging import restore_root_logging, swap_root_logging_to_textual

if TYPE_CHECKING:
    from eawf.surfaces.tui.console.app import ConsoleApp
    from eawf.surfaces.tui.console.operations import Operator
    from eawf.surfaces.tui.console.seam import ProjectionSeam

#: SURF-085: the exit code a terminal entry-layer state returns off a TTY. Nothing
#: interactive can be shown there, and the deterministic status frame would misstate a
#: tree the resolver could not attach to, so this stands in for both.
TERMINAL_ENTRY_EXIT_CODE = exit_codes.ATTACH_FAILURE

#: The ``exit`` value of an entry-layer state that ends the process ("quit - exit 4").
_TERMINAL_EXIT = "terminal"

#: The console's pre-session route id, whose key the session addresses it by.
_ENTRY_ROUTE = "entry"

#: The route the seam opens on before the app retargets it to the session's own route.
_HOME_ROUTE = "scope.home"

#: The session-local workspace selection ``eawf workspace select`` exports.
WORKSPACE_KEY_ENV = "EAWF_WORKSPACE_KEY"

#: Printed before the epoch-1 app opens on an ordinary epoch-1 tree, so an operator who
#: expected the native console knows why they did not get it.
EPOCH1_NOTICE = "eawf tui: this tree is epoch-1; opening the classic console"


def project_name(state_path: Path, repo_root: Path) -> str:
    """Return the name the console header gives the tree: its project's slug.

    The projection is addressed by the root's id, which no operator knows the project by,
    so the header names the project the tree's state records instead. Only the project
    record is validated, so a large state file costs one parse and no whole-state check.

    Args:
        state_path: The tree's ``state.json``.
        repo_root: The repository the tree belongs to.

    Returns:
        The project's slug, else the repository directory's name when the state file
        cannot be read or records no valid project.
    """
    import orjson
    from pydantic import ValidationError

    from eawf.kernel.state.models import Project

    try:
        payload = orjson.loads(state_path.read_bytes())
        return Project.model_validate(payload["project"]).slug
    except OSError, orjson.JSONDecodeError, KeyError, TypeError, ValidationError:
        return repo_root.name


def _entry_sel(chrome: ConsoleChrome, state_id: str) -> int:
    """Return the packaged chrome's entry-state index named ``state_id``.

    Raises:
        ValueError: no entry state in the packaged chrome carries this id.
    """
    for index, state in enumerate(chrome.entry):
        if state.id == state_id:
            return index
    raise ValueError(f"chrome carries no entry state {state_id!r}")


def hand_over(state: EntryState) -> str:
    """Return what a terminal entry state leaves on stderr: its title and its commands.

    The session ends with the next command in the operator's terminal rather than only
    an exit code, whether or not a console was ever drawn.

    Args:
        state: The terminal entry state.

    Returns:
        The lines to print, the state's title first.
    """
    return "\n".join((f"eawf tui: {state.title}", *(f"  {c}" for c in state.commands if c)))


def resolve_operator(*, actor: str | None, receipt_ref: str | None) -> Operator | None:
    """Return who the console acts as, from the principal and receipt the operator named.

    The daemon keeps no operator session a client could look a principal up from:
    every native writer names its principal key itself, and the approval seal checks
    only that the resolver is that same actor and that the cited receipt is an evidence
    row the tree holds. So the console takes both from the operator at launch, as the
    ``domain`` seal command does, and checks their shape here so a typo fails before
    the console opens rather than on the first answer.

    Args:
        actor: The principal key the console's writes are attributed to; ``None`` for
            a console that acts as nobody, whose writing verbs then refuse with why.
        receipt_ref: The qualified evidence URN answers are recorded under; ``None``
            leaves Run controls bound and answers refused for want of a receipt.

    Returns:
        The operator, or ``None`` when no principal was named.

    Raises:
        ValueError: A receipt was named without a principal, or either value is
            malformed.
    """
    from pydantic import TypeAdapter, ValidationError

    from eawf.kernel.state.epoch2.base import PrincipalKey
    from eawf.kernel.state.epoch2.urns import EvidenceUrn
    from eawf.surfaces.tui.console.operations import Operator

    if actor is None:
        if receipt_ref is not None:
            raise ValueError("a receipt needs a principal to record the answer under: name --actor")
        return None
    try:
        TypeAdapter(PrincipalKey).validate_python(actor)
    except ValidationError as error:
        raise ValueError(f"principal {actor!r} is not a principal key (e.g. OP-0001)") from error
    if receipt_ref is not None:
        try:
            TypeAdapter(EvidenceUrn).validate_python(receipt_ref)
        except ValidationError as error:
            raise ValueError(f"receipt {receipt_ref!r} is not a qualified evidence URN") from error
    return Operator(principal=actor, receipt_ref=receipt_ref)


def launch_tui(
    *,
    workspace: Path | None,
    no_input: bool,
    plain: bool,
    verbose: bool = False,
    operator: Operator | None = None,
) -> int:
    """Resolve the tree's authority and open the matching app, or fall back off a TTY.

    Args:
        workspace: Optional workspace root from ``-w/--workspace``.
        no_input: Fail-closed flag -- forces the deterministic status fallback.
        plain: Plain-output flag -- forces the deterministic status fallback.
        verbose: Whether the native console's ``--verbose`` key-trace row is shown
            (SURF-173). Has no effect on the epoch-1 app, which carries no such row.
        operator: Who the native console's writes are attributed to, from
            :func:`resolve_operator`; ``None`` leaves every writing verb refused with
            that reason. Has no effect on the epoch-1 app.

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

    # An interactive launch reads the tree before the console takes the terminal. The
    # CLI's stderr log handler would print what that read logs beneath the console, where
    # it still stands once the console exits, so the terminal carries no log line from here
    # on; a scripted caller keeps its stderr log.
    saved = swap_root_logging_to_textual() if tty else None
    try:
        started = time.monotonic()
        state_path, _reason = resolve_with_reason(workspace=workspace)
        # A tree declares its epoch inside its ``.ea`` directory, beside the state file.
        authority = resolve_authority(state_path.parent)
        chrome = load_chrome()
        env_state = os.environ.get("EA_STATE")
        named_by = "EA_STATE" if env_state else ("--workspace" if workspace is not None else None)
        attached = resolve_attach(
            chrome,
            AttachRequest(
                repo_root=state_path.parent.parent,
                authority=authority,
                state_path=state_path,
                named_by=named_by,
                workspace_key=os.environ.get(WORKSPACE_KEY_ENV) or None,
                registry_path=default_registry_path(),
            ),
        )
        entry = attached.entry

        if entry is not None and entry.exit == _TERMINAL_EXIT and not tty:
            print(hand_over(entry), file=sys.stderr)
            return TERMINAL_ENTRY_EXIT_CODE

        if no_input or plain or not tty:
            from eawf.surfaces.tui.chassis.offline import emit_status

            return emit_status(workspace=workspace, no_input=no_input, plain=plain)

        if entry is not None:
            return _launch_entry(
                chrome=with_entry_state(chrome, entry), state=entry, verbose=verbose
            )

        if authority.epoch == 2:
            resolving = resolving_state(chrome, attached.trace, elapsed=time.monotonic() - started)
            return _launch_native(
                authority=authority,
                state_path=state_path,
                chrome=with_entry_state(chrome, resolving),
                verbose=verbose,
                operator=operator,
            )

        print(EPOCH1_NOTICE, file=sys.stderr)
        return _launch_epoch1(state_path=state_path)
    finally:
        if saved is not None:
            restore_root_logging(saved)


def _launch_native(
    *,
    authority: RootAuthority,
    state_path: Path,
    chrome: ConsoleChrome,
    verbose: bool,
    operator: Operator | None,
) -> int:
    """Open the console over a live seam bound to the tree's epoch-2 authority."""
    from eawf.runtime.daemon.epoch2_root import RootIdentity
    from eawf.surfaces.tui.app import _persisted_theme
    from eawf.surfaces.tui.console.app import OUTER_GUTTER, ConsoleApp
    from eawf.surfaces.tui.console.clock import Clock
    from eawf.surfaces.tui.console.seam import ProjectionSeam

    scope_id = RootIdentity.of(authority.root).root_id
    # the offline frame the console lands on if the daemon answers nothing is filled
    # now, from the document on disk, because nothing can be read through it later
    snapshot = offline_snapshot(authority, scope_id=scope_id, now=datetime.now(UTC))
    chrome = with_entry_state(chrome, offline_state(chrome, snapshot))
    # The daemon addresses a tree by its repository and appends ``.ea`` itself.
    repo_root = authority.root.parent
    seam = ProjectionSeam(
        route=_HOME_ROUTE,
        scope_id=scope_id,
        state_path=state_path,
        repo_root=repo_root,
        operator=operator,
        scope_name=project_name(state_path, repo_root),
    )
    app = ConsoleApp(
        chrome=chrome,
        seam=seam,
        clock=Clock(),
        verbose=verbose,
        gutter=OUTER_GUTTER,
        theme=_persisted_theme(repo_root),
    )
    return _run_console(app, seam)


def _launch_entry(*, chrome: ConsoleChrome, state: EntryState, verbose: bool) -> int:
    """Open the console on the pre-session ``state`` the attach path landed in.

    A terminal state leaves its commands on stderr once the console closes and exits 4;
    any other state ends the process cleanly, not attached.
    """
    from eawf.surfaces.tui.app import _persisted_theme
    from eawf.surfaces.tui.console.app import OUTER_GUTTER, ConsoleApp
    from eawf.surfaces.tui.console.clock import Clock
    from eawf.surfaces.tui.console.session import SessionSetup

    app = ConsoleApp(
        chrome=chrome, clock=Clock(), verbose=verbose, gutter=OUTER_GUTTER, theme=_persisted_theme()
    )
    app.reset(SessionSetup(route=_ENTRY_ROUTE, entrySel=_entry_sel(chrome, state.id)))
    _run_console(app, None)
    if state.exit == _TERMINAL_EXIT:
        print(hand_over(state), file=sys.stderr)
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

    # the caller has already taken the CLI's stderr log handler off the terminal: a
    # daemon auto-spawn during connect is enough to tear the frame
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
    "WORKSPACE_KEY_ENV",
    "hand_over",
    "launch_tui",
    "project_name",
    "resolve_operator",
]
