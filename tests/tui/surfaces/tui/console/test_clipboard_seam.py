"""The console's one clipboard seam: a copy reaches the clipboard before the rack says so.

``y`` and ``Y`` answer ``copied`` only once the host's clipboard took the text, which a
running console writes as the terminal's OSC 52 copy sequence; a console with no terminal
to copy through says the text was not copied. Every URN a copy yields is the one the
identity module formats, so two copies of one record never disagree.
"""

from __future__ import annotations

import asyncio

from eawf.kernel.identity.keys import EntityKind
from eawf.kernel.identity.urn import parse_qualified_urn
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.navigation import NOT_COPIED, Ctx
from eawf.surfaces.tui.console.session import Session, SessionSetup
from eawf.surfaces.tui.console.tokens import Severity
from tests.tui.surfaces.tui.console.test_guarded_quit import FIXTURE, _Host
from tests.tui.surfaces.tui.console.test_live_tree_frames import PROJECT_URN, _linked


async def _copied(route: str, key: str) -> tuple[str | None, str]:
    """Press ``key`` on a running console over ``route``; return the clipboard and the toast."""
    app = ConsoleApp(FIXTURE, FakeClock())
    async with app.run_test(size=(120, 30)) as pilot:
        app.reset(SessionSetup(route=route, size=1))
        app.render_frame()
        app.press_key(key)
        await pilot.pause()
        return app.clipboard, app.session.toasts[-1].text


def test_y_puts_the_value_on_the_clipboard_and_then_says_so() -> None:
    clipboard, toast = asyncio.run(_copied("activity", "y"))
    assert clipboard
    assert toast == clipboard


def test_shift_y_puts_the_urn_on_the_clipboard_and_then_says_so() -> None:
    clipboard, toast = asyncio.run(_copied("activity", "Y"))
    assert clipboard is not None and clipboard.startswith("urn:eawf:")
    assert toast == clipboard


def test_a_console_with_no_clipboard_says_the_text_was_not_copied() -> None:
    session = Session()
    session.route = "activity"
    ctx = Ctx(session=session, fixture=FIXTURE, host=_Host(), w=120, h=30)
    dispatch(ctx, "y", False)
    toast = session.toasts[-1]
    assert (toast.title, toast.sev) == ("not copied", Severity.WARN)
    assert toast.text.endswith(NOT_COPIED)
    assert session.log[0].note.startswith("not copied")


def test_a_refusing_clipboard_is_never_reported_as_a_copy() -> None:
    session = Session()
    session.route = "activity"
    taken: list[str] = []

    def refuses(text: str) -> bool:
        taken.append(text)
        return False

    ctx = Ctx(session=session, fixture=FIXTURE, host=_Host(), w=120, h=30, clipboard=refuses)
    dispatch(ctx, "Y", False)
    assert len(taken) == 1
    assert session.toasts[-1].title == "not copied"


def test_a_console_that_is_not_running_writes_no_clipboard() -> None:
    assert ConsoleApp(FIXTURE, FakeClock()).copy_text("RUN-1") is False


def test_every_copied_urn_is_the_one_the_identity_module_formats() -> None:
    """A record's URN and the tree's URN both round-trip the identity formatter."""
    app = _linked("scope.home", sel="MLS-0100")
    rows = app.view().rows
    record = dv.urn(app.session, app.fixture, rows=rows)
    tree = dv.scope_urn(rows)
    assert tree == PROJECT_URN
    for urn in (record, tree):
        assert str(parse_qualified_urn(urn)) == urn
    held, project = parse_qualified_urn(record), parse_qualified_urn(PROJECT_URN)
    assert (held.workspace_key, held.project_key) == (project.workspace_key, project.project_key)
    # a project is addressed from the reserved slot, a Milestone from its repository
    assert (project.kind, project.repository_slot) == (EntityKind.PROJECT, "_")
    assert (held.kind, held.repository_key) == (EntityKind.MILESTONE, "EAWF")


def test_the_urn_copy_passes_through_the_formatter() -> None:
    app = _linked("scope.home", sel="MLS-0100")
    dispatch(app._ctx(), "Y", False)
    copied = app.session.toasts[-1].text
    assert copied == str(parse_qualified_urn(copied))
    assert copied.endswith("/milestone/MLS-0100")
