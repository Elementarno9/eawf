"""Integration tests for the TUI weekly-burn divisor (P20-I01-W09).

Covers the three success criteria of W09:

1. Field defaults None — exercised in :mod:`tests.unit.test_state_models`.
2. TUI renders ``weekly burn: <consumed_eu> / <target_eu>`` when
   ``state.project.weekly_eu_target`` is non-None.
3. TUI renders no weekly-burn text at all when the field is unset
   (byte-clean — the footer matches the existing keymap-only frame).

The fixtures are constructed in-process (no checked-in JSON) so the
trailing-7-day window is anchored on the test's clock and stays stable
across machines / dates. The ``compute_weekly_burn`` helper is exercised
directly to keep the test independent of the offline-render byte
contract, then the rendered frame is asserted for the burn line.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import Any

from rich.console import Console

from eawf.estimation.metrics import (
    WEEKLY_BURN_WINDOW,
    compute_weekly_burn,
)
from eawf.state.enums import ActualStatus, ProjectStatus, ScopeKind
from eawf.state.models import (
    ActualSummary,
    CurrentPointers,
    Project,
    State,
)
from eawf.tui.layout import (
    build_footer_panel,
    build_frame,
    build_weekly_burn_line,
)


def _render(panel_or_layout: Any) -> str:
    """Render a Rich object to a string for substring assertions."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, record=False)
    console.print(panel_or_layout)
    return buf.getvalue()


def _now() -> datetime:
    """Fixed anchor used by every fixture in this file."""
    return datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


def _state_dict(*, target: float | None, recent_eu: float, stale_eu: float) -> dict[str, Any]:
    """Build a state.json-shaped dict with one in-window and one out-of-window actual.

    The in-window actual contributes ``recent_eu`` to the rollup; the
    out-of-window actual carries ``stale_eu`` but its ``updated_at`` is
    8 days before the anchor so it is excluded from the trailing
    7-day window.
    """
    anchor = _now()
    inside = anchor - timedelta(days=2)
    outside = anchor - WEEKLY_BURN_WINDOW - timedelta(days=1)
    project = Project(
        code="QR",
        slug="qr",
        title="QR",
        description=None,
        domains=["x"],
        default_branch="main",
        status=ProjectStatus.ACTIVE,
        repo_urn="urn:eawf:v1:repo:QR",
        weekly_eu_target=target,
    )
    return {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.REPO.value,
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": anchor.isoformat(),
        "project": project.model_dump(mode="json"),
        "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "actuals": {
            "ACT-recent": ActualSummary(
                id="ACT-recent",
                scope_id="P01-I01-W01",
                status=ActualStatus.ACTIVE,
                elapsed_eu=recent_eu,
                attention_eu=None,
                agent_runtime_eu=None,
                current_store_record_id="REC-recent",
                updated_at=inside,
            ).model_dump(mode="json"),
            "ACT-stale": ActualSummary(
                id="ACT-stale",
                scope_id="P01-I01-W02",
                status=ActualStatus.DONE,
                elapsed_eu=stale_eu,
                attention_eu=None,
                agent_runtime_eu=None,
                current_store_record_id="REC-stale",
                updated_at=outside,
            ).model_dump(mode="json"),
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


# ---------------------------------------------------------------------------
# compute_weekly_burn — pure rollup
# ---------------------------------------------------------------------------


def test_compute_weekly_burn_sums_in_window_actuals_only() -> None:
    """Only actuals within the trailing 7-day window contribute."""
    state_dict = _state_dict(target=10.0, recent_eu=3.5, stale_eu=99.0)
    state = State.model_validate(state_dict)
    metric = compute_weekly_burn(state, now=_now())
    assert metric.consumed_eu == 3.5  # stale_eu excluded
    assert metric.target_eu == 10.0
    assert metric.window_days == 7


def test_compute_weekly_burn_target_none_when_field_unset() -> None:
    """target_eu mirrors Project.weekly_eu_target — None when unset."""
    state_dict = _state_dict(target=None, recent_eu=1.0, stale_eu=0.0)
    state = State.model_validate(state_dict)
    metric = compute_weekly_burn(state, now=_now())
    assert metric.target_eu is None
    assert metric.consumed_eu == 1.0  # rollup still computed


def test_compute_weekly_burn_empty_actuals_is_zero() -> None:
    """No actuals -> consumed_eu == 0.0 (no division by zero, no crash)."""
    state_dict = _state_dict(target=5.0, recent_eu=0.0, stale_eu=0.0)
    # Force actuals to empty.
    state_dict["actuals"] = {}
    state = State.model_validate(state_dict)
    metric = compute_weekly_burn(state, now=_now())
    assert metric.consumed_eu == 0.0
    assert metric.target_eu == 5.0


def test_compute_weekly_burn_project_none_means_target_none() -> None:
    """When state.project is None, target_eu is None regardless of actuals."""
    state_dict = _state_dict(target=10.0, recent_eu=2.0, stale_eu=0.0)
    state_dict["project"] = None
    state_dict["current"] = CurrentPointers().model_dump(mode="json")
    state = State.model_validate(state_dict)
    metric = compute_weekly_burn(state, now=_now())
    assert metric.target_eu is None


# ---------------------------------------------------------------------------
# build_weekly_burn_line — string composition
# ---------------------------------------------------------------------------


def test_build_weekly_burn_line_renders_when_target_set() -> None:
    """The helper returns the formatted line when the target is set."""
    state_dict = _state_dict(target=10.0, recent_eu=3.5, stale_eu=0.0)
    line = build_weekly_burn_line(state_dict)
    assert line is not None
    assert "weekly burn:" in line
    assert "10" in line  # target value present
    # Consumed EU rendered with ``:g`` formatting.
    assert "3.5" in line


def test_build_weekly_burn_line_returns_none_when_unset() -> None:
    """The helper returns None when weekly_eu_target is unset.

    Returning None is the contract that lets build_footer_panel skip the
    line entirely (success criterion 3 — byte-clean when unset).
    """
    state_dict = _state_dict(target=None, recent_eu=3.5, stale_eu=0.0)
    assert build_weekly_burn_line(state_dict) is None


def test_build_weekly_burn_line_returns_none_when_project_missing() -> None:
    """No project dict at all -> no burn line."""
    assert build_weekly_burn_line({}) is None


def test_build_weekly_burn_line_returns_none_on_validation_failure() -> None:
    """Malformed state dict gracefully suppresses the line rather than crashing."""
    bad = {"project": {"weekly_eu_target": 10.0, "bogus": "not-a-real-project"}}
    assert build_weekly_burn_line(bad) is None


# ---------------------------------------------------------------------------
# build_footer_panel — rendered frame
# ---------------------------------------------------------------------------


def test_build_footer_panel_no_state_renders_keymap_only() -> None:
    """Calling build_footer_panel() with no state preserves prior behaviour."""
    rendered = _render(build_footer_panel())
    # P20-I03-W01 rewrote the quadrant footer to advertise quadrant-
    # level keys; ``board`` is the leading token.
    assert "board" in rendered
    assert "weekly burn" not in rendered


def test_build_footer_panel_with_target_renders_burn_line() -> None:
    """When project.weekly_eu_target is set, the footer carries the burn line."""
    state_dict = _state_dict(target=10.0, recent_eu=3.5, stale_eu=99.0)
    rendered = _render(build_footer_panel(state_dict))
    assert "weekly burn:" in rendered
    assert "3.5" in rendered
    assert "10" in rendered
    # The keymap hint stays alongside the burn line.
    # P20-I03-W01 rewrote the quadrant footer to advertise quadrant-
    # level keys; ``board`` is the leading token.
    assert "board" in rendered


def test_build_footer_panel_unset_renders_no_burn_line() -> None:
    """Success criterion 3: byte-clean footer when target is unset.

    The rendered footer must contain the keymap but ZERO mention of the
    burn keyword — the line is absent, not just empty / placeholder.
    """
    state_dict = _state_dict(target=None, recent_eu=3.5, stale_eu=0.0)
    rendered = _render(build_footer_panel(state_dict))
    # P20-I03-W01 rewrote the quadrant footer to advertise quadrant-
    # level keys; ``board`` is the leading token.
    assert "board" in rendered
    assert "weekly burn" not in rendered
    assert "burn" not in rendered  # no remnant text


# ---------------------------------------------------------------------------
# build_frame — full quadrant frame integration
# ---------------------------------------------------------------------------


def test_build_frame_with_target_carries_burn_line() -> None:
    """The full frame renders the burn line inside the footer row."""
    state_dict = _state_dict(target=10.0, recent_eu=3.5, stale_eu=99.0)
    rendered = _render(build_frame(state_dict))
    assert "weekly burn:" in rendered
    # P20-I03-W01: quadrant keymap leads with ``b board``; checking
    # the full leading two-word phrase keeps the assertion meaningful
    # (the bare ``b`` alone is too short to be a stable marker).
    assert "b board" in rendered  # keymap leading fragment present


def test_build_frame_without_target_omits_burn_line() -> None:
    """The full frame stays byte-clean when target is unset."""
    state_dict = _state_dict(target=None, recent_eu=3.5, stale_eu=0.0)
    rendered = _render(build_frame(state_dict))
    assert "weekly burn" not in rendered
    # Existing footer text still present.
    # P20-I03-W01 rewrote the quadrant footer to advertise quadrant-
    # level keys; ``board`` is the leading token.
    assert "board" in rendered


def test_build_frame_empty_state_omits_burn_line() -> None:
    """Bare empty-state input produces no burn line."""
    rendered = _render(build_frame({}))
    assert "weekly burn" not in rendered
