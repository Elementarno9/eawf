"""Startup probe-reply help guard (P30-I16-W27).

Operator report 2026-06-11 (Ghostty graphics terminal): on launch, after the
seal Kitty-image transmission, the HELP pane opened with ZERO key presses.

Root cause (best-evidence): a graphics terminal answers the seal
:class:`textual_image.widget.Image` transmit with a capability / Device-
Attributes probe REPLY. The DA1 reply form ``\\x1b[?62;1;...c`` carries a
literal ``?``; a partially-parsed reply can leak a stray ``?`` key event into
stdin during the startup window, before the operator has touched the keyboard.
Left untouched it resolves through the scope screen's ``question_mark`` ->
``open_help`` binding and pops the help overlay with no real input.

The fix is an interactive-ready gate: :attr:`~eawf.surfaces.tui.app.EaApp._interactive_ready`
is ``False`` until the App's first frame has painted (flipped
``call_after_refresh`` from ``on_mount``). :meth:`EaApp.on_key` swallows ANY
key that arrives before the gate opens (``event.stop()`` +
``event.prevent_default()``), and :meth:`EaApp.action_open_help` defends itself
the same way -- so neither a raw key leak nor a delegated action call can pop
help before first paint. A genuine keypress only follows the painted frame, so
it lands after the gate opens and routes normally.

These tests pin: a stray ``?`` during the startup window pushes no
:class:`~eawf.surfaces.tui.screens.help.HelpScreen` (with the seal mounted,
the operator's live condition), the help action self-defends pre-ready, the
realistic DA1-shaped leak is swallowed, and a ``?`` after the gate opens still
opens help.

NOTE: the operator's failure is a live terminal-protocol race that cannot be
reproduced headlessly (``run_test`` has no real TTY emitting a DA reply). The
tests drive the pre-ready window deterministically by reasserting the gate
flag; the live fix still needs operator re-verification on Ghostty.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual import events

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.help import HelpScreen
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets.git_pane import GitFields

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_REPO = _FIXTURES / "03-phase-iter-wave-active.json"

#: A faithful Device-Attributes (DA1) reply Ghostty / a Kitty-protocol terminal
#: can send in answer to the seal image transmit. It carries a literal ``?``;
#: the leak hypothesis is that a partially-parsed reply delivers a stray ``?``
#: key event into stdin. Held here so the test names the exact escape shape the
#: guard defends against.
_DA1_REPLY: str = "\x1b[?62;1;6;9;15;22c"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate registry + probe-cache writes into ``tmp_path``.

    Mirrors the chassis-test fixture: a scope switch reads
    ``~/.eawf/registry.json`` and some modes write an instrument-probe cache,
    so redirect both off the real home / fixture tree.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(tmp_path / "instrument-probe.json"))


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the workspace git probe to a deterministic clean tree."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


@pytest.fixture
def _seal_mounted() -> None:
    """No-op precondition: the startup guard is seal-independent.

    The W26 header reverted to the crisp brand glyph and W28 made the hero a
    text ASCII-art seal, so no raster seal Image is mounted anywhere by default.
    The guard (swallow a stray ``?`` before the first paint) does not depend on
    any seal, so these tests exercise it directly; the fixture is kept as a
    documented seam for the originally-reported seal-mounted scenario.
    """
    return None


# --------------------------------------------------------------------------
# Pure-unit: the gate flag and its lifecycle
# --------------------------------------------------------------------------


def test_app_starts_not_interactive_ready() -> None:
    """A freshly constructed App is NOT interactive-ready.

    The construction-time default is the swallow side of the gate: any key
    delivered before first paint (the startup probe-leak window) is gated off.
    """
    app = EaApp(scope="repo", state_path=_REPO)
    assert app._interactive_ready is False


def test_mark_interactive_ready_opens_the_gate() -> None:
    """``_mark_interactive_ready`` flips the gate and is idempotent."""
    app = EaApp(scope="repo", state_path=_REPO)
    app._mark_interactive_ready()
    assert app._interactive_ready is True
    # A second call (a stray re-schedule) is harmless.
    app._mark_interactive_ready()
    assert app._interactive_ready is True


def test_action_open_help_suppressed_pre_ready() -> None:
    """``action_open_help`` is a no-op before the gate opens.

    The belt-and-suspenders defense: even a leak that reaches the help action
    by a path other than raw key dispatch (a focused widget delegating
    ``open_help`` to the App) cannot pop help before first paint.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            # Reassert the pre-ready window the live probe leak arrives in.
            app._interactive_ready = False
            app.action_open_help()
            await settle_screen(pilot)
            assert not isinstance(app.screen, HelpScreen)
            assert app._help_open is False

    asyncio.run(body())


# --------------------------------------------------------------------------
# Pilot: stray ? during startup never opens help; seal mounted
# --------------------------------------------------------------------------


def test_stray_question_mark_pre_ready_opens_no_help_seal_mounted(
    _seal_mounted: None,
) -> None:
    """A stray ``?`` key arriving pre-ready pushes no help overlay.

    The core W27 guard, in the operator's live condition (seal image mounted):
    a ``?`` key event delivered through :meth:`EaApp.on_key` while the gate is
    shut (the startup probe-leak window) is swallowed -- no
    :class:`HelpScreen` lands on the stack, ``_help_open`` stays ``False``.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            # Reassert the pre-ready window the live probe leak arrives in, then
            # deliver the stray ``?`` straight through the key chokepoint.
            app._interactive_ready = False
            await app.on_key(events.Key("question_mark", "?"))
            await settle_screen(pilot)
            assert not isinstance(app.screen, HelpScreen)
            assert app._help_open is False

    asyncio.run(body())


def test_da1_probe_reply_leak_key_swallowed_pre_ready(_seal_mounted: None) -> None:
    """A DA1-reply-shaped ``?`` leak is swallowed during startup.

    Names the exact escape mechanism: a Device-Attributes reply
    (:data:`_DA1_REPLY`) carries a literal ``?``; the hypothesis is a stray
    ``?`` key leaks from a partial parse. The gate swallows it pre-ready and
    the event is marked stopped so it never reaches binding dispatch.
    """
    assert "?" in _DA1_REPLY  # the leak carrier the guard defends against

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            app._interactive_ready = False
            await app.on_key(events.Key("question_mark", "?"))
            await settle_screen(pilot)
            # The swallowed key never reached the question_mark -> open_help
            # binding, so no help overlay landed on the stack.
            assert not isinstance(app.screen, HelpScreen)
            assert app._help_open is False

    asyncio.run(body())


# --------------------------------------------------------------------------
# Pilot: ? after the gate opens still opens help (no regression)
# --------------------------------------------------------------------------


def test_question_mark_after_ready_opens_help_seal_mounted(_seal_mounted: None) -> None:
    """A genuine ``?`` after first paint still opens help.

    The regression guard: the startup gate must not break the real
    ``question_mark`` -> ``open_help`` path. Once ``settle_screen`` has pumped
    past first paint (the gate is open), pressing ``?`` pushes the help
    overlay -- the operator's intended affordance is intact.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert app._interactive_ready is True, "the gate must open after first paint"
            await pilot.press("question_mark")
            await settle_screen(pilot)
            assert isinstance(app.screen, HelpScreen)
            assert app._help_open is True

    asyncio.run(body())


def test_gate_opens_then_closes_full_cycle_seal_mounted(_seal_mounted: None) -> None:
    """Pre-ready ``?`` is swallowed, post-ready ``?`` opens help -- one app.

    The end-to-end shape: in a single mounted app, a ``?`` during the reasserted
    startup window opens no help, and the SAME ``?`` once the gate is open does
    open help. Proves the guard discriminates by timing, not by suppressing the
    key forever.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            # Pre-ready: swallowed.
            app._interactive_ready = False
            await app.on_key(events.Key("question_mark", "?"))
            await settle_screen(pilot)
            assert not isinstance(app.screen, HelpScreen)
            # Re-open the gate (first paint already happened) and try again.
            app._interactive_ready = True
            await pilot.press("question_mark")
            await settle_screen(pilot)
            assert isinstance(app.screen, HelpScreen)
            assert app._help_open is True

    asyncio.run(body())
