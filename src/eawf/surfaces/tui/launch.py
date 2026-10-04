"""``eawf ui``'s launcher: resolve a tree's authority, then open the console on it.

Every launch opens the epoch-2 console (:class:`~eawf.surfaces.tui.console.app.ConsoleApp`)
and nothing else. A tree the console can attach to, one whose authority
(:func:`~eawf.kernel.state.epoch2.authority.resolve_authority`) is epoch 2, opens on the
live daemon projection over one seam. A launch that cannot simply attach -- no registered
root, an ambiguous one, an epoch-1 tree whose migration is owed, one stopped part-way, a
schema this console cannot read -- lands in the entry state the attach path
(:func:`~eawf.surfaces.tui.console.attach.resolve_attach`) names, so the operator reads
the exact next command. A native console that did attach draws the resolving frame until
its first projection arrives.

A launch with no interactive terminal (``--plain``, ``--no-input`` or a stdout that is not
a TTY) draws no app at all: it writes the console's own frame in plain mode
(:mod:`~eawf.surfaces.tui.console.plain`), the offline snapshot for an attached tree or
the entry state for one it could not attach to.

This module is the library side of "CLI is dispatch": :func:`launch_tui` is the one
entry point, and :mod:`eawf.surfaces.cli.app` only resolves flags and calls it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast, get_args

from eawf.kernel.config.schema import ToastVerbosity
from eawf.kernel.state.epoch2.authority import RootAuthority, resolve_authority
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.platform.registry import default_registry_path
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.tui.console.attach import (
    ONBOARDING,
    AttachRequest,
    offline_snapshot,
    offline_state,
    resolve_attach,
    resolving_state,
    with_entry_state,
)
from eawf.surfaces.tui.console.chrome import ConsoleChrome, EntryState, load_chrome
from eawf.surfaces.tui.console.onboarding import FirstRun, project_code
from eawf.surfaces.tui.terminal_logging import restore_root_logging, swap_root_logging_to_textual

if TYPE_CHECKING:
    from eawf.surfaces.tui.console.app import ConsoleApp
    from eawf.surfaces.tui.console.operations import Operator
    from eawf.surfaces.tui.console.seam import ProjectionSeam

logger = logging.getLogger(__name__)

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


def persisted_toast_verbosity(repo_root: Path) -> ToastVerbosity:
    """Return the operator's ``ui.toasts`` level from the layered config.

    A missing layer, an unreadable one or a value outside the levels keeps
    ``important``, the level the built-in layer ships: a toast preference is cosmetic
    and never blocks a launch.

    Args:
        repo_root: The repository whose layered config is read.
    """
    from eawf.kernel.config.layered import get_dotted, merge_config
    from eawf.surfaces.cli.errors import ValidationError

    try:
        merged, _sources = merge_config(repo=repo_root)
        value = get_dotted(merged, "ui.toasts")
    except (KeyError, OSError, ValidationError) as exc:
        logger.debug(f"persisted_toast_verbosity fallback exc={exc!r}")
        return "important"
    if value not in get_args(ToastVerbosity):
        logger.debug(f"persisted_toast_verbosity unrecognised value={value!r}")
        return "important"
    return cast("ToastVerbosity", value)


def persisted_glyphs(repo_root: Path | None = None) -> str:
    """Return the glyph allocation ``ui.glyphs`` selects for the console.

    ``unicode`` and ``ascii`` are taken as written. ``auto`` follows the terminal: a
    stdout that declares an encoding other than UTF-8 gets the ASCII allocation,
    because it cannot carry the console's glyphs; one that declares none keeps Unicode.
    A missing layer, an unreadable one or a value outside the three reads as ``auto``:
    the allocation is cosmetic and never blocks a launch.

    Args:
        repo_root: The repository whose layered config is read; ``None`` reads only the
            global and environment layers.
    """
    from eawf.kernel.config.layered import get_dotted, merge_config
    from eawf.surfaces.cli.errors import ValidationError

    try:
        merged, _sources = merge_config(repo=repo_root)
        value = get_dotted(merged, "ui.glyphs")
    except (KeyError, OSError, ValidationError) as exc:
        logger.debug(f"persisted_glyphs fallback exc={exc!r}")
        value = "auto"
    if value in ("unicode", "ascii"):
        return str(value)
    encoding = getattr(sys.stdout, "encoding", None)
    if encoding and encoding.lower().replace("-", "") != "utf8":
        return "ascii"
    return "unicode"


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
    return "\n".join((f"eawf ui: {state.title}", *(f"  {c}" for c in state.commands if c)))


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


def tree_operator(authority: RootAuthority) -> Operator | None:
    """Return the operator the tree's own Tracks name as their owner, to act as by default.

    Every Track policy records the principal that owns it, so a tree whose Tracks are all
    owned by one operator already says who works it; a launch without ``--actor`` acts as
    that operator rather than as nobody. A tree with no such Track, or with Tracks owned
    by different operators, names no one to default to.

    Args:
        authority: The tree's resolved authority, whose selected generation is read.

    Returns:
        The single owning operator, with no receipt; ``None`` when the tree names none,
        names several, or its document cannot be read.
    """
    from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
    from eawf.kernel.state.epoch2.values import OwnerPrincipal
    from eawf.kernel.store.compaction import read_document
    from eawf.surfaces.tui.console.operations import Operator

    target, generation = authority.target, authority.generation_id
    if target is None or generation is None:
        return None
    try:
        tracks = read_document(target.generation_path(generation) / GENERATION_DOCUMENT).get(
            "track", {}
        )
        owners = {
            OwnerPrincipal.model_validate(track["policy"]["ownership_principal"])
            for track in tracks.values()
        }
    except (OSError, ValueError, KeyError) as error:
        logger.info(f"tree_operator unreadable cause={error!s}")
        return None
    operators = {owner.principal_id for owner in owners if owner.principal_kind == "operator"}
    if len(operators) != 1:
        return None
    return Operator(principal=operators.pop())


def launch_tui(
    *,
    workspace: Path | None,
    no_input: bool,
    plain: bool,
    verbose: bool = False,
    operator: Operator | None = None,
) -> int:
    """Resolve the tree's authority and open the console on it, or write its plain frame.

    Args:
        workspace: Optional workspace root from ``-w/--workspace``.
        no_input: Fail-closed flag -- writes the plain frame instead of opening the app.
        plain: Plain-output flag -- writes the plain frame instead of opening the app.
        verbose: Whether the console's ``--verbose`` key-trace row is shown (SURF-173).
        operator: Who the console's writes are attributed to, from
            :func:`resolve_operator`; ``None`` acts as the operator the tree's Tracks
            name as owner (:func:`tree_operator`), and a tree naming none leaves every
            writing verb refused with that reason.

    Returns:
        Process exit code: ``0`` on a clean quit or a written frame,
        :data:`TERMINAL_ENTRY_EXIT_CODE` for a launch that lands in a terminal entry
        state.
    """
    tty = sys.stdout.isatty()
    interactive = tty and not (no_input or plain)
    env_state = os.environ.get("EA_STATE")
    named_by = "EA_STATE" if env_state else ("--workspace" if workspace is not None else None)
    # Off a TTY with no tree named, walking pwd-upward would land on whatever tree sits
    # above cwd, so a scripted caller reads only the tree at cwd itself.
    tree = workspace if tty or named_by is not None else Path.cwd()

    # An interactive launch reads the tree before the console takes the terminal. The
    # CLI's stderr log handler would print what that read logs beneath the console, where
    # it still stands once the console exits, so the terminal carries no log line from here
    # on; a scripted caller keeps its stderr log.
    saved = swap_root_logging_to_textual() if interactive else None
    try:
        started = time.monotonic()
        state_path, _reason = resolve_with_reason(workspace=tree)
        # A tree declares its epoch inside its ``.ea`` directory, beside the state file.
        authority = resolve_authority(state_path.parent)
        chrome = load_chrome()
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

        if not interactive:
            return _emit_plain(chrome=chrome, authority=authority, entry=entry)

        if entry is not None:
            return _launch_entry(
                chrome=with_entry_state(chrome, entry),
                state=entry,
                verbose=verbose,
                first_run=_first_run(entry, state_path),
            )

        resolving = resolving_state(chrome, attached.trace, elapsed=time.monotonic() - started)
        return _launch_native(
            authority=authority,
            state_path=state_path,
            chrome=with_entry_state(chrome, resolving),
            verbose=verbose,
            operator=operator if operator is not None else tree_operator(authority),
        )
    finally:
        if saved is not None:
            restore_root_logging(saved)


def _emit_plain(
    *, chrome: ConsoleChrome, authority: RootAuthority, entry: EntryState | None
) -> int:
    """Write the console's frame in plain mode for a launch with no interactive terminal.

    A terminal entry state writes only its hand-over to stderr and exits 4 (SURF-085):
    a frame would misstate a tree the resolver could not attach to. Any other entry state
    is written as it stands; an attached tree is written as its offline snapshot, the
    last state it committed, read from disk without asking the daemon.

    Args:
        chrome: The packaged chrome.
        authority: The tree's resolved authority.
        entry: The entry state the attach path landed in; ``None`` once it attached.

    Returns:
        ``0`` once the frame is written, else :data:`TERMINAL_ENTRY_EXIT_CODE`.
    """
    from eawf.runtime.daemon.epoch2_root import RootIdentity
    from eawf.surfaces.tui.console.fixture import Fixture
    from eawf.surfaces.tui.console.plain import plain_text, render_plain
    from eawf.surfaces.tui.console.session import SessionSetup

    if entry is not None and entry.exit == _TERMINAL_EXIT:
        print(hand_over(entry), file=sys.stderr)
        return TERMINAL_ENTRY_EXIT_CODE
    if entry is None:
        scope_id = RootIdentity.of(authority.root).root_id
        snapshot = offline_snapshot(authority, scope_id=scope_id, now=datetime.now(UTC))
        entry = offline_state(chrome, snapshot)
    fixture = Fixture.from_chrome(with_entry_state(chrome, entry))
    setup = SessionSetup(route=_ENTRY_ROUTE, entrySel=_entry_sel(chrome, entry.id))
    print(plain_text(render_plain(fixture, setup)))
    return 0


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
    from eawf.surfaces.tui.chassis.theme import persisted_theme
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
        theme=persisted_theme(repo_root),
        toast_verbosity=persisted_toast_verbosity(repo_root),
        glyphs=persisted_glyphs(repo_root),
    )
    return _run_console(app, seam)


def _first_run(state: EntryState, state_path: Path) -> FirstRun | None:
    """Return the first run the onboarding ``state`` performs its workspace step for.

    Any other state, or a tree whose state records no project code to register it
    under, has no step to perform and hands its command over instead.
    """
    from eawf.surfaces.cli._daemon_client import DaemonClient

    code = project_code(state_path)
    if state.id != ONBOARDING or code is None:
        return None
    return FirstRun(
        repo_root=state_path.parent.parent,
        code=code,
        registry_path=default_registry_path(),
        client=DaemonClient,
    )


def _launch_entry(
    *, chrome: ConsoleChrome, state: EntryState, verbose: bool, first_run: FirstRun | None
) -> int:
    """Open the console on the pre-session ``state`` the attach path landed in.

    A terminal state leaves its commands on stderr once the console closes and exits 4;
    any other state ends the process cleanly, not attached. A first run performs its
    workspace step in the console through ``first_run``.
    """
    from eawf.surfaces.tui.chassis.theme import persisted_theme
    from eawf.surfaces.tui.console.app import OUTER_GUTTER, ConsoleApp
    from eawf.surfaces.tui.console.clock import Clock
    from eawf.surfaces.tui.console.session import SessionSetup

    app = ConsoleApp(
        chrome=chrome,
        clock=Clock(),
        verbose=verbose,
        gutter=OUTER_GUTTER,
        theme=persisted_theme(),
        glyphs=persisted_glyphs(),
        first_run=first_run,
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


__all__ = [
    "TERMINAL_ENTRY_EXIT_CODE",
    "WORKSPACE_KEY_ENV",
    "hand_over",
    "launch_tui",
    "project_name",
    "resolve_operator",
    "tree_operator",
]
