"""Budget notices: only the ceiling interrupts, and nothing a notice does takes the screen.

UI-029. No budget fraction but the one the ceiling reaches produces an operator-visible
interrupt. Consumption at the former progress and warning fractions writes no notice, so
there is no row, no badge, no toast and no attention item; the value stays readable in
the usage gauge. The assertion is negative and runs over the notice delivery call path --
the upsert the metering service calls and the statusline segment that reads its ledger --
rather than over the matrix text. At the ceiling one notice is written, non-blocking, and
the presentation matrix lets it toast once per revision.

UI-015. A budget notice reaches the operator without a modal, an automatic open, a focus
change or a route change: it is never a pending action, the Attention projection states
its bucket without a question in it, and the console's only announcement path is the
toast rack.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.projection.attention import (
    AttentionBucket,
    NotificationClass,
    ToastPolicy,
    build_attention_view,
    toast_policy,
)
from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.registers import (
    ATTENTION_ROUTE,
    build_register_view,
    notice_interrupts,
)
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.runtime.budget.notices import load_notice_ledger, notices_path
from eawf.runtime.budget.policy import BLOCK_FRACTION, PromptBudgetCeiling
from eawf.runtime.budget.service import emit_budget_notice
from eawf.surfaces.render.statusline import SegmentSource, budget_segment

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
CEILING = PromptBudgetCeiling(tokens=1000, enforce="soft")
SCOPE = "P01-I01-W01"


def _ledger(tmp_path: Path) -> Path:
    """Return where the notice ledger lives beside a state file under ``tmp_path``."""
    return notices_path(tmp_path / ".ea" / "state.json")


@pytest.mark.parametrize("fraction", [0.5, 0.75, 0.8, 0.999])
def test_ui_029_a_fraction_short_of_the_ceiling_writes_no_notice(
    tmp_path: Path, fraction: float
) -> None:
    """The former progress and warning fractions produce nothing an operator is shown."""
    ledger = _ledger(tmp_path)
    consumed = int(CEILING.tokens * fraction)
    assert (
        emit_budget_notice(
            ledger, scope_id=SCOPE, consumed=consumed, ceiling=CEILING, observed_at=AT
        )
        is None
    )
    assert not ledger.exists()
    assert load_notice_ledger(ledger).notices == {}
    segment = budget_segment(
        spent=consumed,
        limit=CEILING.tokens,
        notice_open=False,
        source=SegmentSource(producer="test", provenance="test#waves.token_budget"),
    )
    assert "!limit" not in segment.text
    assert str(consumed) in segment.text


def test_ui_029_the_ceiling_writes_one_non_blocking_notice(tmp_path: Path) -> None:
    """At the ceiling the notice is recorded once; it asks nothing and blocks nothing."""
    ledger = _ledger(tmp_path)
    consumed = int(CEILING.tokens * BLOCK_FRACTION)
    first = emit_budget_notice(
        ledger, scope_id=SCOPE, consumed=consumed, ceiling=CEILING, observed_at=AT
    )
    again = emit_budget_notice(
        ledger, scope_id=SCOPE, consumed=consumed + 5, ceiling=CEILING, observed_at=AT
    )
    assert first is not None and again is not None
    assert len(load_notice_ledger(ledger).notices) == 1
    assert first.notice.blocking is False
    assert again.notice.revision == first.notice.revision
    assert notice_interrupts() is False


def test_ui_029_a_budget_class_toasts_only_once_per_revision() -> None:
    """The one interrupt the class may make is a toast, once for each revision."""
    assert toast_policy(NotificationClass.BUDGET_PASSED) is ToastPolicy.ONCE_PER_REVISION


def test_ui_029_no_ceiling_means_no_notice(tmp_path: Path) -> None:
    """A scope with no ceiling is never noticed, however much it consumed."""
    ledger = _ledger(tmp_path)
    assert (
        emit_budget_notice(ledger, scope_id=SCOPE, consumed=10**9, ceiling=None, observed_at=AT)
        is None
    )
    assert not ledger.exists()


def test_ui_029_negative_consumption_is_refused(tmp_path: Path) -> None:
    """The error path: a reading below zero is not a reading."""
    with pytest.raises(ValueError, match="non-negative"):
        emit_budget_notice(
            _ledger(tmp_path), scope_id=SCOPE, consumed=-1, ceiling=CEILING, observed_at=AT
        )


def test_ui_015_a_budget_notice_can_never_become_a_pending_action() -> None:
    """A question takes a turn from the operator; a notice may not, so it cannot be one."""
    with pytest.raises((ValidationError, ValueError), match="budget signal"):
        PendingAction.model_validate(
            {"id": "ACT-0001", "observed_value": 1200, "budget_value": 1000, "status": "WAITING"}
        )


def test_ui_015_the_attention_projection_carries_no_notice_as_a_question() -> None:
    """``over budget`` counts the breaches the register holds; no notice is a needs item."""
    register = build_register_view(
        build_route_projection(
            route=ATTENTION_ROUTE, document={}, cursor=1, scope_id="EAWF", generated_at=AT
        )
    )
    view = build_attention_view(register)
    over = next(c for c in view.bucket_counts() if c.bucket is AttentionBucket.OVER_BUDGET)
    assert over.count == 0
    assert over.reason is None
    assert all(not item.notice for item in view.items)
