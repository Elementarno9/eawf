"""UI-068: the notification classes and the read-only matrix the notifications route projects.

``NotificationClass`` is closed over five values, each mapped to its source. The
notifications route draws the presentation matrix per class over exactly three columns --
``CLASS``, ``TOAST`` and ``DECIDED BY`` -- which are the three fields of
``NotificationMatrixView``. ``budget_passed`` toasts once per revision; no class names a
settings leaf that does not exist; and no class reaches any interruption but the toast
rack.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.projection.attention import (
    CLASS_SOURCES,
    NOTIFICATION_MATRIX,
    AttentionNeedKind,
    NotificationClass,
    NotificationMatrixView,
    NotificationPolicy,
    ToastPolicy,
    toast_policy,
)
from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.registers import NOTIFICATIONS_ROUTE, build_register_view
from eawf.kernel.state.epoch2.run import SuspensionReason
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"

#: The matrix as the requirement states it, row for row.
EXPECTED: tuple[tuple[str, str, str], ...] = (
    ("needs_permission", "yes", "attention projection"),
    ("needs_answer", "yes", "attention projection"),
    ("stopped_responding", "yes", "attention projection"),
    ("run_finished", "no", "run lifecycle"),
    ("budget_passed", "once per revision", "budget notify fraction"),
)


def _frame(*, native: bool, sel: int = 0) -> list[str]:
    """Render the notifications route, from the prototype registers or a served register."""
    fixture = load_fixture(FIXTURE)
    view_register = None
    if native:
        projection = build_route_projection(
            route=NOTIFICATIONS_ROUTE, document={}, cursor=4, scope_id="EAWF", generated_at=AT
        )
        view_register = build_register_view(projection)
    session = Session()
    session.route = NOTIFICATIONS_ROUTE
    session.sel = sel
    return render_route(View(session=session, fixture=fixture, w=120, h=30, register=view_register))


def test_ui_068_the_class_enum_is_closed_over_five_values() -> None:
    """Exactly the five classes, and never ``budget_notice``, which names the record."""
    assert [c.value for c in NotificationClass] == [row[0] for row in EXPECTED]
    with pytest.raises(ValueError):
        NotificationClass("budget_notice")


def test_ui_068_the_matrix_rows_equal_the_requirement() -> None:
    """One row per class, over exactly three fields, as the requirement states them."""
    rows = tuple(
        (r.notification_class.value, r.may_interrupt.value, r.decided_by)
        for r in NOTIFICATION_MATRIX.classes
    )
    assert rows == EXPECTED
    assert set(NotificationPolicy.model_fields) == {
        "notification_class",
        "may_interrupt",
        "decided_by",
    }
    assert set(NotificationMatrixView.model_fields) == {"classes"}


def test_ui_068_budget_passed_toasts_once_per_revision_and_run_finished_never() -> None:
    """The design's ``no`` for a budget crossing is corrected; a terminal move never toasts."""
    assert toast_policy(NotificationClass.BUDGET_PASSED) is ToastPolicy.ONCE_PER_REVISION
    assert toast_policy(NotificationClass.RUN_FINISHED) is ToastPolicy.NO


def test_ui_068_no_class_names_a_settings_owner() -> None:
    """No settings leaf owns the policy, so no row names one."""
    for row in NOTIFICATION_MATRIX.classes:
        assert "settings" not in row.decided_by
        assert "interface" not in row.decided_by


def test_ui_068_each_class_is_mapped_to_its_source() -> None:
    """The suspension classes map to a need and a reason; the others to their records."""
    assert set(CLASS_SOURCES) == set(NotificationClass)
    assert CLASS_SOURCES[NotificationClass.NEEDS_PERMISSION] == (
        AttentionNeedKind.PERMISSION,
        SuspensionReason.AWAITING_PERMISSION_GRANT.value,
    )
    assert CLASS_SOURCES[NotificationClass.NEEDS_ANSWER][0] is AttentionNeedKind.ANSWER
    assert CLASS_SOURCES[NotificationClass.RUN_FINISHED][0] is None


def test_ui_068_the_matrix_is_frozen_and_closed() -> None:
    """A policy row refuses an unknown field and a class outside the five."""
    with pytest.raises(ValidationError):
        NotificationPolicy.model_validate(
            {"notification_class": "budget_notice", "may_interrupt": "yes", "decided_by": "x"}
        )
    with pytest.raises(ValidationError):
        NotificationPolicy.model_validate(
            {
                "notification_class": "run_finished",
                "may_interrupt": "no",
                "decided_by": "x",
                "owner": "interface.notifications",
            }
        )


@pytest.mark.parametrize("native", [False, True])
def test_ui_068_the_route_draws_the_three_columns_and_no_owner(native: bool) -> None:
    """Both frames draw CLASS, TOAST and DECIDED BY, and no settings owner line."""
    text = "\n".join(_frame(native=native))
    assert "TOAST" in text
    assert "DECIDED BY" in text
    assert "MAY INTERRUPT" not in text
    assert "owns this policy" not in text
    assert "interface" not in text
    assert "R23" not in text
    for name, toast, decided in EXPECTED:
        shown = name if native else name.replace("_", " ")
        assert any(shown in row and toast in row and decided in row for row in text.splitlines())


def test_ui_068_the_prototype_route_says_needs_you_only_in_the_header() -> None:
    """The header carries the one ``NEEDS YOU`` on the frame."""
    frame = _frame(native=False)
    assert [i for i, row in enumerate(frame) if "NEEDS YOU" in row] == [0]


@pytest.mark.parametrize(("sel", "effect"), [(3, "raises no toast"), (4, "one toast per revision")])
def test_ui_068_the_foot_states_the_selected_class_effect(sel: int, effect: str) -> None:
    """The foot reads the selected row: the last class toasts once per revision."""
    assert any(effect in row for row in _frame(native=False, sel=sel))
