"""UI-020, UI-063: Trust lists the audit verdicts a Batch's current cycle holds, read live.

A Batch under a Milestone files its verification cycle on the Batch ledger, and the
reviewer Run that reached each audit verdict was bound under a capsule naming its role
and ran under a vendor session naming its harness. ``projection.trust.read`` lists each
verdict as an observation of that Milestone, answered for by ``(agent_role, runtime)``;
the Trust frame draws it with the calibration the gate refused on and each producer's
tally, and a Milestone the Batch is not filed under lists none of it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from eawf.kernel.delivery.batch_proof import AuditVerdict
from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.verification import build_verification_view
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods.delivery import CYCLE_KEY_PREFIX
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import SIZES, Session
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    document_path,
    provision,
    rekeyed,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_semantic_gateway_guards import (
    call_verb,
    digest,
    method_ctx,
    seal_capsule,
)
from tests.unit.kernel.delivery.test_batch_proof import BATCH, REVIEWER, audit, cycle

pytestmark = pytest.mark.integration

MILESTONE = "MLS-0030"


@pytest.fixture
def judged(tmp_path: Path) -> CanaryProvision:
    """Return a canary whose Batch BAT-0007 holds one clearing and one failing verdict."""
    canary = provision(tmp_path / "repo", code="TRU")
    reviewer = rekeyed(seed_row("run", "RUNNING"), key=REVIEWER.rsplit("/", 1)[-1])
    reviewer["vendor_session"] = {"harness": "claude-code", "session_digest": "raw-session-01"}
    seed(
        canary,
        {"batch": {"BAT-0007": seed_row("batch", "ACTIVE")}, "run": {reviewer["key"]: reviewer}},
    )
    capsule = seal_capsule(
        run_ref=REVIEWER,
        scope_ref=BATCH,
        agent_role=AgentSessionRole.REVIEWER.value,
        purpose=RunPurpose.REVIEW.value,
        authority={"state": "read_only", "workspace": "read_only"},
        tool_grants=("repo_read", "diff_read"),
    )
    binding = RunBinding(
        run_ref=REVIEWER,
        compiled_spec_digest=digest("d"),
        authority_capsule_digest=capsule.contract_digest,
        route_policy_revision=1,
        bound_at=AT,
        capsule=capsule,
    )
    document = document_path(canary)
    append_ledger_record(
        ledger_path(document, Epoch2Collection.RUN),
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"binding-{reviewer['key']}",
            status="bound",
            recorded_at=AT,
            payload=binding.model_dump(mode="json"),
        ),
    )
    held = cycle(audits=(audit("CR-01"), audit("CR-02", verdict=AuditVerdict.VERIFIED_FALSE)))
    append_ledger_record(
        ledger_path(document, Epoch2Collection.BATCH),
        LedgerRecord(
            collection=Epoch2Collection.BATCH,
            record_key=f"{CYCLE_KEY_PREFIX}BAT-0007",
            status=held.stage.value,
            recorded_at=AT + timedelta(seconds=1),
            payload=held.model_dump(mode="json"),
        ),
    )
    return canary


def _frame(canary: CanaryProvision, tmp_path: Path, milestone: str) -> list[str]:
    served = call_verb(
        "projection.trust.read", method_ctx(tmp_path / "runtime"), repo_root=str(canary.root)
    )
    model = build_verification_view(RouteProjection.model_validate(served))
    session = Session()
    session.route = "trust"
    session.subj_id = milestone
    w = 120
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=w,
        h=dict(SIZES)[w],
        linked=True,
        projection=model,
    )
    return render_route(view)


def test_ui_063_the_served_trust_route_lists_each_verdict_with_its_producer(
    judged: CanaryProvision, tmp_path: Path
) -> None:
    frame = _frame(judged, tmp_path, MILESTONE)

    cleared = next(row for row in frame if "BAT-0007 CR-01" in row)
    failed = next(row for row in frame if "BAT-0007 CR-02" in row)
    assert "verified_true" in cleared and "verified_false" in failed
    assert "reviewer · claude-code" in cleared
    assert "RUN-00000020" not in cleared, "a verdict is answered for by its producer, not a Run"


def test_ui_020_the_served_trust_route_draws_calibration_and_track_record(
    judged: CanaryProvision, tmp_path: Path
) -> None:
    frame = _frame(judged, tmp_path, MILESTONE)

    calibration = next(row for row in frame if row.startswith(" CALIBRATION"))
    assert "INSUFFICIENT · Brier ∅ unavailable" in calibration
    record = next(row for row in frame if row.startswith(" reviewer · claude-code"))
    assert record.split()[3:6] == ["1", "1", "~0.50"]


def test_ui_063_a_milestone_the_batch_is_not_filed_under_lists_none_of_its_verdicts(
    judged: CanaryProvision, tmp_path: Path
) -> None:
    frame = _frame(judged, tmp_path, "MLS-0999")
    assert not any("BAT-0007" in row for row in frame)
