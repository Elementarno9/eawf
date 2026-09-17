"""The arm overlay's cap row reads as a claim cap, not a spend cap.

The fleet drive loop tests its EU / USD / waves caps on the CLAIM step only
(:func:`~eawf.runtime.daemon.methods.fleet.budget_exhausted` gates
``_fill_lanes``), so a reached cap stops the loop claiming the next wave and
never kills a lane already running. The overlay used to caption the row a bare
``caps`` beside a dollar figure, which an operator reads as a spend ceiling the
run will not cross -- an overstatement, because the lanes in flight when the cap
fires still bill. These tests pin the corrected caption and the docstrings that
carry the correction, plus the autopilot footer contract the relabel must not
disturb: every key the pane advertises stays bound to a real action.
"""

from __future__ import annotations

import inspect

from textual.binding import Binding

from eawf.runtime.daemon.methods import fleet
from eawf.surfaces.tui.modes.autopilot import AutopilotModeScreen
from eawf.surfaces.tui.screens.overlays.arm import (
    BUDGET_OPTIONS,
    CAPS_ROW_CAPTION,
    ArmModal,
    render_caps_row,
    render_halt_row,
)

#: Footer key tokens the app owns rather than the Autopilot pane: scope switch,
#: command palette, help and quit are bound on the host App, so the pane's own
#: ``BINDINGS`` never carries them.
_APP_LEVEL_TOKENS: frozenset[str] = frozenset({"w/r/u", "/", "?", "q"})

#: The arrow token the footer advertises as one label, expanded to the two
#: Textual key names a pane must bind for it to work.
_ARROW_KEYS: tuple[str, ...] = ("up", "down")


def _flat(doc: str | None) -> str:
    """Collapse a docstring's line wrapping so a phrase check spans line breaks."""
    return " ".join((doc or "").split())


def _bound_keys(bindings: list[object]) -> set[str]:
    """Return every key name bound by *bindings*, splitting comma-joined keys."""
    keys: set[str] = set()
    for binding in bindings:
        if isinstance(binding, Binding):
            keys.update(part.strip() for part in binding.key.split(","))
        elif isinstance(binding, tuple):
            keys.update(part.strip() for part in str(binding[0]).split(","))
    return keys


def test_arm_caps_row_reads_as_claim_caps() -> None:
    """The cap row is captioned ``claim caps``, never a bare or spend cap."""
    row = render_caps_row("standard")
    assert CAPS_ROW_CAPTION == "claim caps"
    assert "claim caps" in row
    assert "spend" not in row.lower()
    # The three axes survive the relabel.
    assert "EU" in row
    assert "$" in row
    assert "waves" in row


def test_render_caps_row_labels_every_budget_tier() -> None:
    """Every selectable budget tier renders the claim-cap caption."""
    for budget in BUDGET_OPTIONS:
        assert render_caps_row(budget).startswith(f"[$muted]{CAPS_ROW_CAPTION}[/]")


def test_render_caps_row_unknown_tier_renders_uncapped() -> None:
    """An unknown tier still renders the caption with every axis uncapped."""
    row = render_caps_row("")
    assert CAPS_ROW_CAPTION in row
    assert row.count("--") == 3


def test_arm_modal_docstring_states_caps_stop_claims_only() -> None:
    """The ArmModal docstring says a cap stops claims and kills no lane."""
    doc = _flat(inspect.getdoc(ArmModal))
    assert "stops new claims" in doc
    assert "never kills an in-flight lane" in doc


def test_fleet_budget_docstrings_state_caps_stop_claims_only() -> None:
    """The fleet budget gate and params say the same as the overlay."""
    gate_doc = _flat(inspect.getdoc(fleet.budget_exhausted))
    assert "stops new claims" in gate_doc
    assert "never kills an in-flight lane" in gate_doc
    params_doc = _flat(inspect.getdoc(fleet.DriveParams))
    assert "stops new claims" in params_doc
    assert "never kills an in-flight lane" in params_doc


def test_budget_stop_row_still_names_the_lane_disposition() -> None:
    """The paired row still tells the operator what happens to running lanes."""
    assert "drain in-flight lanes" in render_halt_row("auto-close, fork on fail")
    assert "hard-halt in-flight lanes" in render_halt_row("auto-close, hard-halt on fail")


def test_autopilot_footer_keeps_every_advertised_key_bound() -> None:
    """Every key the autopilot footer advertises resolves to a real binding."""
    bound = _bound_keys(list(AutopilotModeScreen.BINDINGS))
    advertised = [hint.split(" ", 1)[0] for hint in AutopilotModeScreen.FOOTER_HINTS]
    unbound = []
    for token in advertised:
        if token in _APP_LEVEL_TOKENS:
            continue
        expected = _ARROW_KEYS if token == "↑↓" else (token,)
        unbound.extend(key for key in expected if key not in bound)
    assert unbound == []


def test_autopilot_footer_still_advertises_the_arm_key() -> None:
    """The relabel leaves the ``a`` arm hint on the footer."""
    assert "a arm" in " ".join(AutopilotModeScreen.FOOTER_HINTS)
