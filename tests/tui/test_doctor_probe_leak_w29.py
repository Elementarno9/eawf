"""Doctor-mode probe-reply leak guard (P30-I16-W29).

Operator report 2026-06-11 (Ghostty graphics terminal): switching to Doctor
mode (digit ``5``) popped a config / help modal with NO such keypress.

Root cause: the Doctor-mode ``on_mount`` kicks a health gather worker that runs
:func:`~eawf.surfaces.tui.modes.doctor.gather_doctor_health` inside
``asyncio.to_thread``. That gather fans out to blocking subprocesses -- the
instrument version-probes (:func:`eawf.platform.install.instrument_probe.probe`)
and the per-wave ``git log`` drift scan
(:func:`eawf.workflow.lifecycle.wave_sha.build_wave_sha_index`). Both call
``subprocess.run`` with ``capture_output=True`` but the default ``stdin=None``,
so each child INHERITS the live App's controlling TTY as fd 0. On a graphics
terminal a child that touches that TTY can trigger an escape-sequence reply
(a Device-Attributes / capability answer); the terminal writes that reply back
onto the shared TTY where the App's stdin reader parses it as a synthetic key
(``c`` -> config window, ``?`` -> help). It fires on Doctor-mode ENTRY, after
first paint, so the W27 startup gate cannot catch it.

The deterministic root fix detaches fd 0 to ``/dev/null`` for the gather's
duration (:func:`~eawf.observability.doctor.checks.detached_tty_stdin`), so
every probe child inherits a dead stdin and can solicit no TTY reply. A
belt-and-suspenders backstop drops a bare ``c`` / ``?`` in
:meth:`~eawf.surfaces.tui.app.EaApp.on_key` while the gather is in flight
(:attr:`EaApp._health_probe_in_flight`), without suppressing a genuine
post-gather keypress.

These tests pin:

* switching to Doctor mode and draining the gather worker leaves the Doctor
  screen active -- NO :class:`ConfigModal` / :class:`HelpScreen` on the stack;
* the in-flight backstop drops a synthetic ``c`` / ``?`` while the probe flag
  is set, and a genuine ``c`` / ``?`` once the flag clears still opens the
  overlay (no regression of the affordance).

NOTE: the operator's failure is a live terminal-protocol race that cannot be
reproduced headlessly (``run_test`` has no real TTY emitting an escape reply);
live re-verification on Ghostty is still needed. The tests drive the leak
window deterministically.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual import events

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.doctor import DoctorModeScreen
from eawf.surfaces.tui.screens.help import HelpScreen
from eawf.surfaces.tui.screens.overlays.config_modal import ConfigModal
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets.git_pane import GitFields

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_REPO = _FIXTURES / "03-phase-iter-wave-active.json"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate registry + probe-cache writes into ``tmp_path``.

    Mirrors the doctor-mode test fixture: the ``u`` scope switch reads
    ``~/.eawf/registry.json`` and the Doctor mount writes an instrument-probe
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


# --------------------------------------------------------------------------
# Core W29 guard: entering Doctor mode opens no config / help overlay
# --------------------------------------------------------------------------


def test_doctor_mode_entry_opens_no_config_or_help_overlay() -> None:
    """Digit ``5`` then a drained gather leaves Doctor active -- no overlay.

    The core W29 guard, the operator's reported failure: switching to Doctor
    mode and waiting for the gather worker to finish must leave the Doctor
    screen on top -- no :class:`ConfigModal` (the ``c`` leak) and no
    :class:`HelpScreen` (the ``?`` leak) popped with zero real keypresses.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")
            # Drain the gather worker explicitly (settle_screen also waits).
            await app.workers.wait_for_complete()
            await settle_screen(pilot)
            assert app.current_mode == "doctor"
            assert isinstance(app.screen, DoctorModeScreen)
            assert not isinstance(app.screen, ConfigModal)
            assert not isinstance(app.screen, HelpScreen)
            assert app._help_open is False
            # The in-flight flag clears once the gather resolves.
            assert app._health_probe_in_flight is False

    asyncio.run(body())


def test_health_probe_flag_clears_after_gather() -> None:
    """The in-flight flag is set during the gather and cleared after it drains.

    Pins the backstop's lifecycle: the flag rides only the gather window so a
    genuine post-gather ``c`` / ``?`` is never suppressed.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert app._health_probe_in_flight is False
            await pilot.press("5")
            await app.workers.wait_for_complete()
            await settle_screen(pilot)
            assert app._health_probe_in_flight is False

    asyncio.run(body())


# --------------------------------------------------------------------------
# Backstop: a leaked c / ? in the gather window is dropped
# --------------------------------------------------------------------------


def test_leaked_c_in_flight_opens_no_config() -> None:
    """A synthetic ``c`` while the probe is in flight pops no config window.

    Reasserts the in-flight window the live probe leak arrives in, then
    delivers a bare ``c`` straight through the key chokepoint: the backstop
    swallows it so no :class:`ConfigModal` lands on the stack.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")
            await app.workers.wait_for_complete()
            await settle_screen(pilot)
            # Reassert the gather window, then deliver the leaked key.
            app._health_probe_in_flight = True
            await app.on_key(events.Key("c", "c"))
            await settle_screen(pilot)
            assert not isinstance(app.screen, ConfigModal)

    asyncio.run(body())


def test_leaked_question_mark_in_flight_opens_no_help() -> None:
    """A synthetic ``?`` while the probe is in flight pops no help overlay."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")
            await app.workers.wait_for_complete()
            await settle_screen(pilot)
            app._health_probe_in_flight = True
            await app.on_key(events.Key("question_mark", "?"))
            await settle_screen(pilot)
            assert not isinstance(app.screen, HelpScreen)
            assert app._help_open is False

    asyncio.run(body())


# --------------------------------------------------------------------------
# No regression: a genuine c / ? after the gather still opens the overlay
# --------------------------------------------------------------------------


def test_genuine_question_mark_after_gather_opens_help() -> None:
    """Once the gather drains, a genuine ``?`` still opens help (no regression).

    The backstop must discriminate by the in-flight window, not suppress
    ``?`` forever: with the flag cleared, pressing ``?`` pops the help
    overlay -- the operator's affordance is intact.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")
            await app.workers.wait_for_complete()
            await settle_screen(pilot)
            assert app._health_probe_in_flight is False
            await pilot.press("question_mark")
            await settle_screen(pilot)
            assert isinstance(app.screen, HelpScreen)
            assert app._help_open is True

    asyncio.run(body())
