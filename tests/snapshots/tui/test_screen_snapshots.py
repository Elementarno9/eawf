"""Golden ASCII snapshot tests for the C06 ``tui_v2`` operator surface.

Drives each key screen / overlay to a known position via Textual's
``App.run_test()`` Pilot and asserts the rendered terminal (captured as
plain ASCII text per the C06 Q-new1 OVERRIDE) byte-matches a committed
golden under ``tests/snapshots/tui/golden/``.

Why a focused set rather than the brief's full ~16-fixture inventory:
each fixture spins a fresh Textual app under ``run_test`` (≈40-80 ms of
mount + settle), and the band's plan flagged that the full ~16-fixture
matrix risks the CI runtime budget. This suite therefore covers the
three scope screens (repo / workspace / user) plus two representative
overlays (help + a destructive confirm) — enough to catch a layout or
chrome regression on every screen *kind* and the overlay capture path,
without paying the per-fixture spin-up cost 16 times. The harness
(:func:`eawf.tui_v2.snapshot.assert_screen_snapshot`) is reusable, so
expanding the set later is one ``assert`` per added golden.

Determinism note: the repo / workspace screens carry a ``GitPane`` that
probes the **live** working tree (``git status`` count, branch, recent
commit subjects, ahead/behind). That output varies run-to-run and would
leak the real branch / commit text into a golden. The autouse
:func:`_isolated_cwd` fixture chdir's every test into a fresh non-git
temp dir, so the pane resolves to deterministic dashes — stable *and*
scrub-safe. The fixture state itself comes from the absolute fixture
paths above, unaffected by the cwd switch.

Regenerate the goldens after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.config.registry import registry_lookup
from eawf.tui_v2.app import EaApp
from eawf.tui_v2.screens.overlays.config_modal import ConfigModal
from eawf.tui_v2.screens.overlays.confirm import ConfirmModal
from eawf.tui_v2.screens.overlays.detail import DetailModal, resolve_detail
from eawf.tui_v2.screens.overlays.edit_field import EditFieldModal
from eawf.tui_v2.screens.overlays.events import EventRow, EventsModal, _row_from_envelope
from eawf.tui_v2.snapshot import assert_screen_snapshot, settle_screen


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Chdir into a non-git temp dir + disable Textual animations.

    Two determinism guards for the goldens:

    * The git pane probes ``Path.cwd()``; pointing it at a fresh non-git
      directory makes every git field resolve to a dash, which keeps the
      golden stable across working-tree changes and free of real branch /
      commit text.
    * Disabling animations settles time-driven chrome (notably the
      ``TabbedContent`` underline marker the config overlay uses) to its
      final position immediately, so a capture under scheduler load cannot
      catch a mid-animation frame and flake the snapshot. Textual reads
      ``constants.TEXTUAL_ANIMATIONS`` once at import, so the env var is
      already cached by test time — patch the constant directly (the App
      copies it into ``self.animation_level`` at construction).
    """
    import textual.constants as _tc

    monkeypatch.setattr(_tc, "TEXTUAL_ANIMATIONS", "none")
    monkeypatch.chdir(tmp_path)


#: Fixed terminal geometry for every snapshot — wide enough for the 2x2
#: quadrant + overlay boxes, matching the asciinema cast viewport so
#: screen + cast frames line up.
_SIZE = (120, 40)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"
_WORKSPACE_STATE = _FIXTURES / "05-workspace-state.json"
_PORTFOLIO_STATE = _FIXTURES / "07-decisions-and-backlog.json"

# Fail loudly on a path mistake: the read-only binder returns ``None`` for
# a missing file (degrading to an empty-scope placeholder), so a wrong
# fixture path would silently snapshot the placeholder rather than the
# populated screen. Guard at import so the breakage is unmissable.
assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"
assert _WORKSPACE_STATE.is_file(), f"missing snapshot fixture: {_WORKSPACE_STATE}"
assert _PORTFOLIO_STATE.is_file(), f"missing snapshot fixture: {_PORTFOLIO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"


# --------------------------------------------------------------------------
# Scope screens — one golden per screen kind
# --------------------------------------------------------------------------


def test_repo_screen_snapshot() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "repo_screen.txt")

    asyncio.run(body())


def test_workspace_screen_snapshot() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "workspace_screen.txt")

    asyncio.run(body())


def test_user_screen_snapshot() -> None:
    async def body() -> None:
        app = EaApp(scope="user", state_path=_PORTFOLIO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "user_screen.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Overlays — exercise the topmost-modal capture path
# --------------------------------------------------------------------------


def test_help_overlay_snapshot() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.action_open_help()
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "help_overlay.txt")

    asyncio.run(body())


def test_confirm_overlay_snapshot() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(ConfirmModal("Abandon wave P01-I01-W01?"))
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "confirm_overlay.txt")

    asyncio.run(body())


def test_detail_overlay_snapshot() -> None:
    """The detail card aligns its ``label: value`` colons in one column."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            state = app.state
            assert state is not None
            wave_id = next(iter(state.waves))
            app.push_modal(DetailModal(resolve_detail(state, wave_id)))
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "detail_overlay.txt")

    asyncio.run(body())


def _open_config(app: EaApp) -> ConfigModal:
    """Push a ConfigModal with a fixed repo anchor and clear tab-bar focus.

    The ``_isolated_cwd`` fixture chdir's into a fresh non-git temp dir, so
    the layered merge resolves to the built-in defaults — the rendered
    field values are therefore deterministic (the registry defaults), and
    no machine path or YAML-layer value leaks into the golden.
    """
    modal = ConfigModal(workspace=None, repo=Path("/repo"))
    app.push_screen(modal)
    return modal


def test_config_modal_audit_tab_snapshot() -> None:
    """Default tab (audit): a bool + a ranged int row, plus the layer line."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            _open_config(app)
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "config_modal_audit.txt")

    asyncio.run(body())


def test_config_modal_planning_tab_snapshot() -> None:
    """Planning tab exercises a choice + bool + int mix in one pane."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = _open_config(app)
            await settle_screen(pilot)
            modal.query_one("#config-tabs").active = modal._tab_pane_id("planning")  # type: ignore[attr-defined]
            modal.set_focus(None)
            modal.field_index = 0
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "config_modal_planning.txt")

    asyncio.run(body())


def test_config_modal_tabbar_focused_snapshot() -> None:
    """``↑`` from the first field focuses the tab bar (zone-aware hint).

    The footer hint flips to the tab-bar keys and no field row carries the
    ``>`` cursor caret — the plain-text proof that focus left the field
    list for the tab selector.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = _open_config(app)
            await settle_screen(pilot)
            await pilot.press("up")  # field 0 -> tab bar
            await settle_screen(pilot)
            assert modal.focus_zone == "tabs"  # type: ignore[attr-defined]
            assert_screen_snapshot(app, _GOLDEN / "config_modal_tabbar_focused.txt")

    asyncio.run(body())


def test_config_modal_runtime_single_field_snapshot() -> None:
    """The single-field ``runtime`` tab: its lone choice row is reachable.

    Regression-proof for the operator's trapped-arrows report — the lone
    ``runtime.default`` choice carries the ``>`` cursor caret (selected via
    ``↓`` from the tab bar), showing it is keyboard-reachable.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = _open_config(app)
            await settle_screen(pilot)
            modal.query_one("#config-tabs").active = modal._tab_pane_id("runtime")  # type: ignore[attr-defined]
            modal.set_focus(None)
            modal.field_index = 0
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "config_modal_runtime.txt")

    asyncio.run(body())


def test_config_modal_dirty_layer_snapshot() -> None:
    """After a toggle, the layer line shows the unsaved-count + dirty marker."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            _open_config(app)
            await settle_screen(pilot)
            await pilot.press("space")  # toggle the first bool — stages a dirty edit
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "config_modal_dirty.txt")

    asyncio.run(body())


def test_config_modal_dirty_discard_prompt_snapshot() -> None:
    """Esc on a dirty modal raises the V15 discard confirm prompt."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            _open_config(app)
            await settle_screen(pilot)
            await pilot.press("space")
            await settle_screen(pilot)
            await pilot.press("escape")
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "config_modal_discard_prompt.txt")

    asyncio.run(body())


def test_edit_field_modal_int_snapshot() -> None:
    """The scalar editor seeds the input + shows the type/range meta line."""

    async def body() -> None:
        entry = registry_lookup("audit.flaky_retry_count")
        assert entry is not None
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_screen(EditFieldModal(entry, 2))
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "edit_field_int.txt")

    asyncio.run(body())


def _event_row(timestamp: str, event_type: str, status: str, summary: str) -> EventRow:
    """Build an :class:`EventRow` through the real envelope-flattening path.

    Routing the raw (possibly mixed-format) *timestamp* through
    :func:`_row_from_envelope` exercises the UTC normalization the overlay
    applies, so the golden captures the normalized + aligned columns.
    """
    row = _row_from_envelope(
        {
            "id": "EV",
            "summary": summary,
            "payload": {"timestamp": timestamp, "event_type": event_type, "status": status},
        }
    )
    assert row is not None
    return row


def test_events_overlay_snapshot() -> None:
    """Events rows render one UTC timestamp format, columns aligned.

    The seeded rows deliberately mix the two on-disk ISO spellings
    (trailing ``Z`` and a ``+00:00`` offset, with fractional seconds); the
    golden must show them collapsed to a single ``...Z`` form with the
    following columns lined up.
    """

    async def body() -> None:
        rows = (
            _event_row("2026-05-10T12:49:10.250985Z", "wave close", "ok", "closed W01"),
            _event_row("2026-05-10T12:49:25.190872+00:00", "dispatch cost", "fail", "boom"),
            _event_row("2026-05-10T12:49:35.093906Z", "executor report", "ok", "report body"),
        )
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(EventsModal(rows))
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "events_overlay.txt")

    asyncio.run(body())
