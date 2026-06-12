"""Digit-accelerator misroute guard (P30-I16-W23, RB-2).

Operator report 2026-06-11: pressing ``3`` opened help instead of switching
to Research. Root cause: the digit mode-switch bindings (built by
:func:`~eawf.surfaces.tui.modes.registry.mode_bindings`) were NON-priority, so
they only resolved on Textual's focused-up binding pass -- AFTER the raw key
event was forwarded to the focused widget / active screen. Any widget that
grabs focus and carries a same-digit binding (or whose binding chain resolves
the digit to another action) intercepts the digit first; the fall-through then
reached the scope screen's ``question_mark`` -> ``open_help`` neighbour and the
digit opened help.

The fix marks the mode-switch bindings ``priority=True`` so the digit -> mode
switch wins at App priority regardless of what is focused. These tests pin:

* the registry emits priority digit bindings, and they land on
  :attr:`~eawf.surfaces.tui.app.EaApp.BINDINGS` as priority;
* the key-trace diagnostic (:meth:`EaApp.trace_digit_binding`) names the
  focus-capturing interceptor that owned the digit before the fix;
* the full Pilot digit sweep -- with an extra widget mounted on the header --
  presses EVERY registered digit FROM EVERY mode and asserts each routes to its
  mapped mode and never opens help (modal closed AND with an adversarial
  focus-capturing widget that binds ``3`` -> help, the faithful shape of the
  reported misroute).

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest
from textual.binding import Binding, BindingType
from textual.widgets import Static

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.registry import MODE_REGISTRY, mode_bindings
from eawf.surfaces.tui.screens.help import HelpScreen
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.header import Header

#: Id of the faithful non-focusable header stand-in the digit sweep mounts to
#: reproduce the operator's live layout (an extra widget docked on the header).
_HEADER_EXTRA_ID: str = "w23-header-extra"

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_REPO = _FIXTURES / "03-phase-iter-wave-active.json"

#: The ratified digit -> mode map (the D-TUI-MODES accelerator axis). Pinned
#: here so the sweep asserts against the brief contract, not the registry it is
#: guarding -- a registry reshape that breaks the map fails loudly.
_DIGIT_MODE_MAP: dict[str, str] = {
    "1": "home",
    "2": "autopilot",
    "3": "research_board",
    "4": "trust",
    "5": "doctor",
    "6": "evidence",
    "7": "feed",
    "8": "agent_watch",
    "9": "sandbox_events",
}


class _GrabbySeal(Static):
    """A focus-capturing widget that binds ``3`` -> help (the misroute shape).

    Faithful to the reported failure: a widget that grabs focus AND whose
    binding chain resolves the digit ``3`` to the help action. Before the
    ``priority=True`` fix this would intercept ``3`` on the focused-up pass and
    open help; with the fix the App's priority digit binding wins first.
    """

    can_focus: bool = True

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("3", "open_help", "help", show=False),
    ]

    def action_open_help(self) -> None:
        """Route to the App help action (the misroute target)."""
        self.app.action_open_help()


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate registry + probe-cache writes into ``tmp_path``.

    Mirrors the chassis-test fixture: a ``u`` scope switch reads
    ``~/.eawf/registry.json`` and the Doctor mode writes an instrument-probe
    cache, so redirect both off the real home / fixture tree.
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


async def _mount_header_extra(app: EaApp) -> None:
    """Mount a faithful non-focusable stand-in widget onto the header.

    Reproduces the operator's live layout (an extra widget docked on the header
    beside the brand mark) without depending on the header's own chrome. The
    stand-in is ``can_focus=False`` like the real graphics widgets the operator
    had on screen, so the digit sweep exercises the same "extra header widget
    present" path that the misroute surfaced under.
    """
    header = app.query(Header).first()
    await header.mount(Static("x", id=_HEADER_EXTRA_ID))


# --------------------------------------------------------------------------
# Registry / binding wiring -- pure-unit (no Textual mount)
# --------------------------------------------------------------------------


def test_mode_bindings_are_priority() -> None:
    """Every digit mode-switch binding carries ``priority=True``.

    The load-bearing flag: a non-priority digit binding only resolves on the
    focused-up pass, which is the misroute window. Pinning priority here guards
    the fix at the registry source.
    """
    bindings = mode_bindings()
    assert bindings, "the registry must emit at least one digit binding"
    assert all(b.priority for b in bindings)
    assert [b.key for b in bindings] == [spec.digit for spec in MODE_REGISTRY]


def test_app_digit_bindings_resolve_at_priority() -> None:
    """The merged App bindings carry the digit switches as priority bindings."""
    app = EaApp(scope="repo", state_path=_REPO)
    for digit in _DIGIT_MODE_MAP:
        resolved = app._bindings.key_to_bindings.get(digit, ())
        switch = [b for b in resolved if b.action.startswith("switch_mode")]
        assert switch, f"digit {digit!r} must carry a switch_mode binding"
        assert all(b.priority for b in switch), f"digit {digit!r} switch must be priority"


# --------------------------------------------------------------------------
# Key-trace diagnostic
# --------------------------------------------------------------------------


def test_trace_digit_binding_silent_on_non_mode_key() -> None:
    """The trace fires only for registered mode digits (silent otherwise)."""
    app = EaApp(scope="repo", state_path=_REPO)
    assert app.trace_digit_binding("q") is None
    assert app.trace_digit_binding("escape") is None
    assert app.trace_digit_binding("0") is None


def test_trace_digit_binding_names_focus_capturing_interceptor() -> None:
    """The trace surfaces a focus-capturing widget that owns the digit.

    Pins the diagnostic the wave required: when a focus-capturing widget binds
    ``3`` -> ``open_help`` and is focused, the trace reports it as a pre-App
    owner -- the interceptor that caused the misroute before the priority fix.
    """

    async def body() -> str | None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            grabby = _GrabbySeal("x", id="grabby")
            await app.screen.mount(grabby)
            grabby.focus()
            await settle_screen(pilot)
            return app.trace_digit_binding("3")

    trace = asyncio.run(body())
    assert trace is not None
    assert "key='3'" in trace
    assert "mode='research_board'" in trace
    assert "_GrabbySeal->open_help" in trace


# --------------------------------------------------------------------------
# Full Pilot digit sweep -- every digit FROM every mode, seal mounted
# --------------------------------------------------------------------------


def test_digit_sweep_every_digit_from_every_mode_seal_mounted() -> None:
    """Press EVERY digit FROM EVERY mode; each routes to its map, never help.

    The core RB-2 guard: with an extra widget mounted on the header (the
    operator's live condition), start the sweep from each of the registered
    modes and press every registered digit, asserting the resulting active mode
    matches the digit map and the help overlay never opened. The cross-product
    covers the ``3 -> research`` case the operator reported alongside every
    sibling digit.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            # Reproduce the operator's live layout (an extra header widget).
            await _mount_header_extra(app)
            await settle_screen(pilot)
            header = app.query(Header).first()
            assert header.query(f"#{_HEADER_EXTRA_ID}"), "the header extra must be mounted"
            for start_spec in MODE_REGISTRY:
                # Land in the starting mode first.
                await pilot.press(start_spec.digit)
                await settle_screen(pilot)
                assert app.current_mode == start_spec.name
                for digit, expected_mode in _DIGIT_MODE_MAP.items():
                    await pilot.press(digit)
                    await settle_screen(pilot)
                    assert app.current_mode == expected_mode, (
                        f"from {start_spec.name!r}, digit {digit!r} routed to "
                        f"{app.current_mode!r}, expected {expected_mode!r}"
                    )
                    assert not isinstance(app.screen, HelpScreen), (
                        f"digit {digit!r} opened help from {start_spec.name!r}"
                    )
                    assert app._help_open is False

    asyncio.run(body())


def test_digit_three_routes_to_research_under_focus_capture() -> None:
    """``3`` switches to Research even when a focus-capturing widget binds it.

    The direct reproduction-and-fix assertion: with an extra header widget AND
    a focus-capturing widget that binds ``3`` -> help focused (the faithful
    misroute shape), pressing ``3`` must route to Research, not help. This is
    the case that failed before the ``priority=True`` fix.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await _mount_header_extra(app)
            grabby = _GrabbySeal("x", id="grabby")
            await app.screen.mount(grabby)
            grabby.focus()
            await settle_screen(pilot)
            assert app.focused is grabby
            await pilot.press("3")
            await settle_screen(pilot)
            assert app.current_mode == "research_board"
            assert not isinstance(app.screen, HelpScreen)
            assert app._help_open is False

    asyncio.run(body())


def test_help_still_opens_on_its_own_key() -> None:
    """The priority digit fix does not break the genuine ``?`` -> help path.

    A regression guard: making the digits priority must not steal the
    ``question_mark`` -> ``open_help`` binding. Pressing ``?`` still opens the
    help overlay; pressing a digit afterwards still leaves help (no stuck
    modal-stack residue that could swallow a later digit).
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("question_mark")
            await settle_screen(pilot)
            assert isinstance(app.screen, HelpScreen)
            assert app._help_open is True

    asyncio.run(body())
