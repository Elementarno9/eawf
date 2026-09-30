"""The dispatch queue states each running verification leg the way its manifest does.

UI-025: an executing leg carries its identity, start, resolved timeout budget and progress
mode; an enumerating leg adds its completed and total counts, the last unit it finished and
its running pass, fail and unknown tallies. UI-027: an ended leg is not in the queue, so a
partial collection is never presented as a result.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from eawf.kernel.economics.governor import InFlightGovernor
from eawf.kernel.runtime.dispatch_queue import ProgressMode
from eawf.runtime.daemon.dispatch_queue import dispatch_queue_view
from eawf.runtime.verification.progress import (
    LegIdentity,
    LegLiveness,
    LegOutcome,
    ObligationDisposition,
    ObligationRecord,
    ProgressManifest,
    collection_digest,
    progress_manifest_id,
)
from eawf.runtime.verification.progress import ProgressMode as LegMode

AT: Final = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
GOVERNOR: Final = InFlightGovernor(
    max_concurrent_runs=3, max_in_flight_tokens=1_000, admission="queue"
)


def _manifest(
    key: str, *, collected: tuple[str, ...] | None, outcome: LegOutcome = LegOutcome.RUNNING
) -> ProgressManifest:
    done = (
        ObligationRecord(
            obligation_id="t::a", disposition=ObligationDisposition.PASS, completed_at=AT
        ),
        ObligationRecord(
            obligation_id="t::b", disposition=ObligationDisposition.FAIL, completed_at=AT
        ),
    )
    return ProgressManifest(
        id=progress_manifest_id(key),
        leg=LegIdentity(
            attempt_id="ATT-1",
            criterion_id="CR-01",
            gate_id="pytest",
            freshness_key=key,
            claimed_at=AT,
        ),
        producer_digest=f"sha256:{'a' * 64}",
        writer_pid=4,
        progress_mode=LegMode.ENUMERATED if collected is not None else LegMode.NONE,
        outcome=outcome,
        collected=collected,
        collection_digest=collection_digest(collected) if collected is not None else None,
        obligations=done if collected is not None else (),
        cursor=3,
        liveness=LegLiveness(
            started_at=AT,
            heartbeat_at=AT + timedelta(seconds=40),
            elapsed_ms=40_000,
            resolved_timeout_seconds=2_700,
        ),
        created_at=AT,
        updated_at=AT + timedelta(seconds=40),
    )


def test_ui_025_an_enumerating_leg_states_counts_last_unit_and_tallies() -> None:
    manifest = _manifest("1" * 64, collected=("t::a", "t::b", "t::c", "t::d"))

    view = dispatch_queue_view({}, (), governor=GOVERNOR, manifests=(manifest,), now=AT)

    (leg,) = view.legs
    assert leg.progress_mode is ProgressMode.ENUMERATED
    assert (leg.completed, leg.total, leg.last_completed) == (2, 4, "t::b")
    assert (leg.passed, leg.failed, leg.unknown) == (1, 1, 0)
    assert leg.budget_seconds == 2_700 and leg.started_at == AT


def test_ui_025_a_leg_that_publishes_nothing_is_stated_opaque_with_its_budget() -> None:
    view = dispatch_queue_view(
        {}, (), governor=GOVERNOR, manifests=(_manifest("2" * 64, collected=None),), now=AT
    )

    (leg,) = view.legs
    assert leg.progress_mode is ProgressMode.OPAQUE
    assert leg.completed is None and leg.total is None
    assert leg.budget_seconds == 2_700


def test_ui_027_an_ended_leg_is_not_in_the_queue() -> None:
    ended = _manifest("3" * 64, collected=("t::a", "t::b"), outcome=LegOutcome.PASSED)

    view = dispatch_queue_view({}, (), governor=GOVERNOR, manifests=(ended,), now=AT)

    assert view.legs == ()


def test_ui_067_an_empty_tree_states_its_slots_and_no_control() -> None:
    view = dispatch_queue_view({}, (), governor=GOVERNOR, manifests=(), now=AT)

    assert (view.plan.slots, view.plan.in_use, view.plan.edges) == (3, 0, ())
    assert view.control.last_request is None and view.control.holding is None


def test_ui_067_unreadable_economics_leaves_the_slots_unstated() -> None:
    view = dispatch_queue_view({}, (), governor=None, manifests=(), now=AT)

    assert view.plan.slots is None
