"""The Activity grouping: every Run in exactly one of eight buckets, by its typed reason.

UI-030. The buckets partition the queried Runs totally and disjointly: every Run lands in
exactly one top-level bucket, the counts sum to the total drawn on the same frame, a
bucket whose facts the Run register does not carry states the unknown token rather than
folding into a neighbour, and ``running`` is stated as an estimate while a stale Run
cannot yet be told from it. The rail total, the strip and the summary are one count.

UI-031. ``needs operator`` carries one sub-bucket per ``SuspensionReason`` value, typed
against the enum, plus an explicit unknown-reason sub-bucket; the sub-buckets are indented
in the rail at 120 and 160 columns and folded into their parent in the 80-column strip,
and the parent's count is its children's sum.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.activity import (
    RUNNING_ESTIMATE_REASON,
    STATUS_BUCKETS,
    UNSTATED_BUCKETS,
    ActivityExceptionBucket,
    group_runs,
    reason_label,
)
from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.registers import RegisterView, build_register_view
from eawf.kernel.projection.truth import Precision, TruthState
from eawf.kernel.state.enums import MeasurementQuality
from eawf.kernel.state.epoch2.run import RunStatus, SuspensionReason
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import SIZES, Session

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"


def _run(key: str, status: str | None, reason: str | None = None) -> dict[str, Any]:
    """Return one stored Run row stating ``status`` and, when suspended, ``reason``."""
    row: dict[str, Any] = {"urn": f"{SCOPE}/run/{key}", "revision": 1}
    if status is not None:
        row["status"] = status
    if reason is not None:
        row["suspension_reason"] = reason
    return row


#: Nine Runs over every status, two suspended for a known reason and one for none.
RUNS: dict[str, Any] = {
    "RUN-00000001": _run("RUN-00000001", "QUEUED"),
    "RUN-00000002": _run("RUN-00000002", "RUNNING"),
    "RUN-00000003": _run("RUN-00000003", "RUNNING"),
    "RUN-00000004": _run("RUN-00000004", "SUSPENDED", "AWAITING_OPERATOR_INPUT"),
    "RUN-00000005": _run("RUN-00000005", "SUSPENDED", "AWAITING_PERMISSION_GRANT"),
    "RUN-00000006": _run("RUN-00000006", "SUSPENDED"),
    "RUN-00000007": _run("RUN-00000007", "FAILED"),
    "RUN-00000008": _run("RUN-00000008", "COMPLETED"),
    "RUN-00000009": _run("RUN-00000009", "CANCELLED"),
}


def _register(runs: dict[str, Any] | None = None) -> RegisterView:
    """Return the Activity register over ``runs``."""
    return build_register_view(
        build_route_projection(
            route="activity",
            document={"run": RUNS if runs is None else runs},
            cursor=41208,
            scope_id=SCOPE,
            generated_at=AT,
        )
    )


def _counts(register: RegisterView) -> dict[tuple[ActivityExceptionBucket, bool, Any], Any]:
    """Return each row's count value, keyed by bucket, sub flag and reason."""
    return {(c.bucket, c.sub, c.reason): c.count for c in group_runs(register).counts}


def _frame(register: RegisterView, *, w: int) -> list[str]:
    """Render the Activity native frame at width ``w``."""
    session = Session()
    session.route = "activity"
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=w,
        h=dict(SIZES)[w],
        register=register,
        linked=True,
    )
    return render_route(view)


def test_ui_030_the_buckets_are_eight_and_every_status_lands_in_one() -> None:
    """The top level is eight, and the status table is total over the Run statuses."""
    assert len(ActivityExceptionBucket) == 8
    assert set(STATUS_BUCKETS) == set(RunStatus)


def test_ui_030_every_run_lands_in_exactly_one_bucket_and_the_counts_sum() -> None:
    """The known counts and the unbucketed rows sum back to the total."""
    grouping = group_runs(_register())
    tops = grouping.top_level()
    assert len(tops) == 8
    known = sum(int(c.count.value or 0) for c in tops if c.count.state is TruthState.KNOWN)
    assert known + grouping.unbucketed == grouping.total == len(RUNS)
    counts = _counts(_register())
    assert counts[(ActivityExceptionBucket.QUEUED, False, None)].value == "1"
    assert counts[(ActivityExceptionBucket.RUNNING, False, None)].value == "2"
    assert counts[(ActivityExceptionBucket.FAILED, False, None)].value == "1"
    assert counts[(ActivityExceptionBucket.TERMINAL_RECENT, False, None)].value == "2"
    assert counts[(ActivityExceptionBucket.NEEDS_OPERATOR, False, None)].value == "3"


def test_ui_030_a_bucket_the_register_cannot_count_is_unknown_not_zero() -> None:
    """Control outcomes, heartbeats and delivery stages are not on the Run row."""
    counts = _counts(_register())
    for bucket, reason in UNSTATED_BUCKETS.items():
        field = counts[(bucket, False, None)]
        assert field.state is TruthState.UNKNOWN
        assert field.missing_reason == reason
    assert set(UNSTATED_BUCKETS) == {
        ActivityExceptionBucket.UNKNOWN_CONTROL_OUTCOME,
        ActivityExceptionBucket.LOST_STALE,
        ActivityExceptionBucket.CHECKING_INTEGRATING,
    }


def test_ui_030_running_is_an_estimate_while_stale_runs_cannot_be_told_apart() -> None:
    """A Run that went quiet would still count as running, so the count says so."""
    running = _counts(_register())[(ActivityExceptionBucket.RUNNING, False, None)]
    assert running.precision is Precision.BOUNDED
    assert running.measurement_quality is MeasurementQuality.ESTIMATED
    assert RUNNING_ESTIMATE_REASON


def test_ui_030_a_row_stating_no_status_or_an_unknown_one_is_unbucketed() -> None:
    """A row that names nothing the grouping reads lands in no bucket."""
    grouping = group_runs(
        _register(
            {
                "RUN-0000000a": _run("RUN-0000000a", None),
                "RUN-0000000b": _run("RUN-0000000b", "BLOCKED"),
            }
        )
    )
    assert grouping.unbucketed == 2
    assert grouping.total == 2


def test_ui_030_an_empty_register_counts_zero_everywhere_it_can() -> None:
    """The empty boundary: every countable bucket is a zero that was taken."""
    grouping = group_runs(_register({}))
    assert grouping.total == 0
    assert all(c.count.value == "0" for c in grouping.counts if c.count.state is TruthState.KNOWN)


def test_ui_030_asking_another_route_for_a_run_grouping_raises() -> None:
    """Rows that are not Runs cannot be grouped as Runs."""
    register = build_register_view(
        build_route_projection(
            route="attention", document={}, cursor=1, scope_id=SCOPE, generated_at=AT
        )
    )
    with pytest.raises(ValueError, match="states no Run grouping"):
        group_runs(register)


def test_ui_031_needs_operator_has_one_sub_bucket_per_reason_and_an_unknown_one() -> None:
    """Typed against the enum: six reasons, then the explicit unknown reason."""
    subs = [c for c in group_runs(_register()).counts if c.sub]
    assert [c.reason for c in subs] == [*SuspensionReason, None]
    counts = {c.reason: c.count.value for c in subs}
    assert counts[SuspensionReason.AWAITING_OPERATOR_INPUT] == "1"
    assert counts[SuspensionReason.AWAITING_PERMISSION_GRANT] == "1"
    assert counts[None] == "1"
    assert counts[SuspensionReason.AWAITING_LEASE] == "0"


def test_ui_031_a_reason_outside_the_enum_is_the_unknown_reason_not_the_largest() -> None:
    """An unreadable reason is filed under unknown, never guessed into a real one."""
    grouping = group_runs(_register({"RUN-0000000a": _run("RUN-0000000a", "SUSPENDED", "NAPPING")}))
    subs = {c.reason: c.count.value for c in grouping.counts if c.sub}
    assert subs[None] == "1"
    assert sum(int(v or 0) for v in subs.values()) == 1


def test_ui_031_the_parent_count_is_its_childrens_sum() -> None:
    """The parent is derived from the sub-buckets, so the two cannot disagree."""
    grouping = group_runs(_register())
    parent = next(
        c
        for c in grouping.counts
        if c.bucket is ActivityExceptionBucket.NEEDS_OPERATOR and not c.sub
    )
    children = [int(c.count.value or 0) for c in grouping.counts if c.sub]
    assert int(parent.count.value or 0) == sum(children)


@pytest.mark.parametrize("w", [120, 160])
def test_ui_031_the_rail_indents_the_sub_buckets_under_their_parent(w: int) -> None:
    """At 120 and 160 columns each reason is an indented row under ``needs operator``."""
    rail = [row.split("│ ", 1)[1] for row in _frame(_register(), w=w) if "│ " in row]
    parent = next(i for i, line in enumerate(rail) if line.startswith(" needs operator"))
    for offset, reason in enumerate([*SuspensionReason, None], start=1):
        assert rail[parent + offset].startswith(f"   ↳ {reason_label(reason)}")
    running = next(line for line in rail if line.startswith(" running"))
    assert re.search(r"^ running\s+\S*2\s*$", running)
    assert "↳" not in running


def test_ui_031_the_strip_folds_the_sub_buckets_into_their_parent() -> None:
    """At 80 columns the strip names the eight top-level buckets only."""
    frame = _frame(_register(), w=80)
    strip = next(row for row in frame if row.startswith(" BUCKETS"))
    assert "↳" not in strip
    assert "needs operator 3" in strip
