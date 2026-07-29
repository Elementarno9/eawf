"""Config-modal + edit-field cosmic-terminal reskin snapshot (P30-I02-W20).

The reskin migrates the config modal off its hardcoded ``> *`` markers
onto the shared sigil vocabulary: the overridden-key marker now renders
the half-filled ``claimed`` sigil (sourced from
:func:`~eawf.surfaces.tui.widgets.sigils.glyph`), the typed (bool / choice
/ float) value cells render as ``<...>`` chips in the green ``$accent``
palette, and the writable-layer indicator leads with the ``overview``
chrome sigil. The scalar :class:`~eawf.surfaces.tui.screens.overlays.edit_field.EditFieldModal`
overlay grows a leading green caret beside its input and renders its inline
validation feedback as a chip in the rotated green palette (flipping to
``$error`` only on a real validation failure).

These tests mount each overlay IN ISOLATION through a Pilot harness and
pin the wave's close-gate bar:

* a dedicated snapshot golden of the ``flow`` tab on the ``repo`` layer
  with one field staged dirty -- the frame carries the overridden sigil
  marker, the bool / choice / float chips, and the layer-indicator sigil;
* per-cell assertions that the overridden marker resolves to the half-
  filled claimed sigil (NOT the legacy ``*``) and that the bool / choice /
  float value cells render as chips while a free-text ``str`` / ``int``
  does not;
* the edit-field overlay renders its inline validation chip + green caret,
  and the chip flips to the error palette on a failed validation.

The snapshot golden is daemonless-stable (the registry resolves against an
empty ``tmp_path`` home and every git probe is stubbed). Regenerate it
after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 EAWF_DAEMONLESS=1 uv run pytest \\
        tests/snapshots/tui/test_config_modal_reskin_w20.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.kernel.config.registry import ConfigKey, registry_lookup
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.config_modal import ConfigModal
from eawf.surfaces.tui.screens.overlays.edit_field import EditFieldModal
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph

_SIZE = (120, 40)
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"

#: The unicode marks the default render mode surfaces -- the half-filled
#: claimed sigil for the overridden marker, and the overview triple-bar for
#: the layer-indicator line.
_CLAIMED_SIGIL = glyph(Sigil.CLAIMED, mode="unicode")
_OVERVIEW_SIGIL = chrome("overview", mode="unicode")
_DISPATCH_CARET = chrome("dispatch", mode="unicode")

#: The flow tab carries one of each chip-bearing shape: a transition bool, a choice
#: (``flow.budget.enforce`` -> ``soft`` / ``hard``), and a float
#: (``flow.budget.multiplier``). The budget keys are global/workspace/repo-
#: writable, so the repo layer renders them editable (no read-only lock).
_FLOW_TAB = "flow"
_FLOW_BOOL = "flow.advance_after.audit"
_FLOW_CHOICE = "flow.budget.enforce"
_FLOW_FLOAT = "flow.budget.multiplier"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every git probe so the rendered chrome is deterministic."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )
    monkeypatch.setattr("eawf.surfaces.tui.widgets.git_pane._git_run", lambda *a, **k: None)


def _goto_tab(modal: ConfigModal, tab: str) -> None:
    """Activate *tab* and clear focus the way the modal's own switch does."""
    from textual.widgets import TabbedContent

    modal.query_one("#config-tabs", TabbedContent).active = modal._tab_pane_id(tab)
    modal.set_focus(None)
    modal.field_index = 0


# --------------------------------------------------------------------------
# Pure helpers: the override marker is a sigil, typed cells are chips
# --------------------------------------------------------------------------


def test_override_marker_is_claimed_sigil_not_asterisk(tmp_path: Path) -> None:
    # The overridden-key marker resolves to the half-filled claimed sigil
    # (the migrated marker), never the legacy hardcoded ``*``.
    modal = ConfigModal(workspace=None, repo=tmp_path)
    marker = modal._override_marker()
    assert marker == _CLAIMED_SIGIL
    assert marker != "*"


def test_chip_types_render_value_as_chip(tmp_path: Path) -> None:
    # bool / choice / float value cells render inside ``<...>`` chip
    # delimiters -- the discrete-shape pill affordance.
    modal = ConfigModal(workspace=None, repo=tmp_path)
    for key in (_FLOW_BOOL, _FLOW_CHOICE, _FLOW_FLOAT):
        entry = registry_lookup(key)
        assert entry is not None
        chip = modal._value_chip(entry, entry.default)
        assert chip.startswith("<") and chip.endswith(">"), f"{key} not chipped: {chip!r}"


def test_free_text_types_render_value_plainly(tmp_path: Path) -> None:
    # An open-ended ``str`` / ``int`` field renders its value plainly -- no
    # chip delimiters -- because a chip would only add noise to free text.
    modal = ConfigModal(workspace=None, repo=tmp_path)
    int_entry = ConfigKey(tab="t", key="t.n", label="n", type="int", default=7)
    str_entry = ConfigKey(tab="t", key="t.s", label="s", type="str", default="abc")
    assert modal._value_chip(int_entry, 7) == "7"
    assert modal._value_chip(str_entry, "abc") == "abc"


def test_layer_line_leads_with_overview_sigil(tmp_path: Path) -> None:
    # The writable-layer indicator line leads with the overview chrome sigil.
    modal = ConfigModal(workspace=None, repo=tmp_path)
    line = modal._layer_line()
    assert line.startswith(_OVERVIEW_SIGIL), line


def test_overridden_field_row_carries_claimed_sigil_marker(tmp_path: Path) -> None:
    # A field staged in the dirty map renders the claimed sigil as its
    # overridden marker in the row text (not the legacy ``*``); a clean field
    # does not.
    modal = ConfigModal(workspace=None, repo=tmp_path)
    entry = registry_lookup(_FLOW_BOOL)
    assert entry is not None
    clean_row = modal._field_line(entry)
    assert _CLAIMED_SIGIL not in clean_row
    modal._view.dirty = {_FLOW_BOOL: True}
    dirty_row = modal._field_line(entry)
    assert _CLAIMED_SIGIL in dirty_row
    assert "*" not in dirty_row


# --------------------------------------------------------------------------
# Snapshot golden: overridden sigil + bool/choice/float chips + layer line
# --------------------------------------------------------------------------


def test_config_modal_reskin_flow_tab_snapshot(tmp_path: Path) -> None:
    # The snapshot golden of the flow tab on the repo layer (one field staged
    # dirty) carries the overridden sigil marker, the bool / choice / float
    # chips, and the layer-indicator sigil.
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = ConfigModal(workspace=None, repo=tmp_path)
            app.push_screen(modal)
            await settle_screen(pilot)
            modal._view.layer = "repo"
            _goto_tab(modal, _FLOW_TAB)
            # Settle the programmatic tab-activation repaint before staging
            # so the dirty assignment + repaint below is not racing the
            # TabActivated handler's own repaint (snapshot determinism).
            await settle_screen(pilot)
            # Stage the float multiplier dirty so the overridden sigil marker
            # renders on a real row in the captured frame.
            modal._view.dirty = {_FLOW_FLOAT: 1.5}
            modal._repaint_fields()
            await settle_screen(pilot)

            frame = normalize_snapshot(capture_screen_text(app))
            # The layer-indicator line leads with the overview sigil.
            assert _OVERVIEW_SIGIL in frame
            # The overridden (dirty) float row carries the claimed sigil.
            assert _CLAIMED_SIGIL in frame
            # The typed chips render around the discrete values: the choice
            # cell (soft/hard) and the staged float both read as chips.
            assert "<soft>" in frame or "<hard>" in frame
            assert "<1.5>" in frame

            assert_screen_snapshot(app, _GOLDEN / "config_modal_reskin_flow_w20.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Edit-field overlay: inline validation chip + caret in the rotated palette
# --------------------------------------------------------------------------


def test_edit_field_renders_green_caret_and_calm_chip() -> None:
    # The scalar edit-field overlay renders a leading caret (the dispatch
    # chrome glyph) and seeds its inline validation row as a calm chip in the
    # rotated green ``-ok`` palette.
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            entry = registry_lookup(_FLOW_FLOAT)
            assert entry is not None
            modal = EditFieldModal(entry, 1.0)
            app.push_screen(modal)
            await settle_screen(pilot)

            from textual.widgets import Static

            caret_row = modal.query_one("#edit-field-caret", Static)
            assert str(caret_row.render()) == _DISPATCH_CARET

            error_row = modal.query_one("#edit-field-error", Static)
            # The calm chip seeds in the green -ok palette (the chip pill, not
            # an empty cell), flagging "buffer not yet failed".
            assert "-ok" in error_row.classes
            assert str(error_row.render()) == "<valid>"

    asyncio.run(body())


def test_edit_field_validation_failure_chips_in_error_palette() -> None:
    # A failed validation flips the inline chip out of the green -ok palette
    # and renders the error wrapped in chip delimiters -- the validation chip
    # in the error colour.
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            # eu_quantum is a bounded float; an out-of-range value fails
            # coerce_and_validate so the inline error chip renders.
            entry = registry_lookup("estimation.display.eu_quantum")
            assert entry is not None
            modal = EditFieldModal(entry, 0.25)
            app.push_screen(modal)
            await settle_screen(pilot)

            from textual.widgets import Input, Static

            modal.query_one("#edit-field-input", Input).value = "not-a-number"
            modal.action_accept()
            await settle_screen(pilot)

            error_row = modal.query_one("#edit-field-error", Static)
            # The chip flipped out of the green calm palette...
            assert "-ok" not in error_row.classes
            # ...and the failure renders wrapped in chip delimiters.
            rendered = str(error_row.render())
            assert rendered.startswith("<") and rendered.endswith(">"), rendered
            # The overlay stays open (no dismiss on a failed accept).
            assert isinstance(app.screen, EditFieldModal)

    asyncio.run(body())


def test_edit_field_accept_dismisses_with_typed_value() -> None:
    # affordance preserved: a valid buffer still accepts to the typed value
    # (the reskin is render-only -- the accept/cancel contract is unchanged).
    async def body() -> float | None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        sink: list[object] = []
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            entry = registry_lookup(_FLOW_FLOAT)
            assert entry is not None
            modal = EditFieldModal(entry, 1.0)
            app.push_screen(modal, callback=sink.append)
            await settle_screen(pilot)

            from textual.widgets import Input

            modal.query_one("#edit-field-input", Input).value = "2.0"
            modal.action_accept()
            await settle_screen(pilot)
        return sink[0] if sink else None

    assert asyncio.run(body()) == pytest.approx(2.0)
