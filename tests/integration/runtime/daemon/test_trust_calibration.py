"""UI-020, UI-063: Trust scores the jury on what its verdicts' subjects went on to do.

A merged Batch whose verification cycle opened a repair for two failing criteria holds
twenty clearing verdicts that held and two failing ones the repair bore out, so the served
calibration is ``SCORED`` and the gate earns the jury blocking authority. A principal's
gold labels, filed through ``runtime.delivery.label_audit``, turn three clearing verdicts
into known-bad subjects the jury waved through, and the gate refuses on the co-error rate
until the repository's ``verify.jury_max_co_error`` ceiling admits it. A Batch that has
not merged settles nothing, so its calibration still reads ``INSUFFICIENT``.
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.delivery.batch_proof import AuditVerdict, BatchVerificationStage
from eawf.kernel.delivery.integration import ConflictExit, ConflictExitKind
from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.verification import build_verification_view
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery import CYCLE_KEY_PREFIX
from eawf.runtime.daemon.methods.delivery_label import DELIVERY_LABEL_AUDIT_METHOD
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
from tests.unit.kernel.delivery.test_batch_proof import (
    BATCH,
    REPAIR_TASK,
    REVIEWER,
    audit,
    cycle,
)

pytestmark = pytest.mark.integration

MILESTONE: Final = "MLS-0030"

#: The clearing criteria the merged Batch's verdicts held on.
CLEARING: Final = tuple(f"CR-{i:02d}" for i in range(1, 21))

#: The failing criteria the repair was opened for.
FAILING: Final = ("CR-21", "CR-22")

NOTE: Final = "the criterion regressed in production after the merge"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the global config layer and every canary runtime under this test's tmp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _reviewed(
    tmp_path: Path, *, status: str, lines: tuple[Any, ...], harness: str | None = "claude-code"
) -> CanaryProvision:
    """Return a canary whose Batch BAT-0007 stands in *status* with *lines* filed.

    With *harness* ``None`` the reviewer ran under no vendor session, so no producer
    identity answers for its verdicts.
    """
    canary = provision(tmp_path / "repo", code="TRC")
    reviewer = rekeyed(seed_row("run", "RUNNING"), key=REVIEWER.rsplit("/", 1)[-1])
    if harness is not None:
        reviewer["vendor_session"] = {"harness": harness, "session_digest": "raw-session-01"}
    seed(
        canary,
        {"batch": {"BAT-0007": seed_row("batch", status)}, "run": {reviewer["key"]: reviewer}},
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
    for offset, line in enumerate(lines, start=1):
        append_ledger_record(
            ledger_path(document, Epoch2Collection.BATCH),
            LedgerRecord(
                collection=Epoch2Collection.BATCH,
                record_key=f"{CYCLE_KEY_PREFIX}BAT-0007",
                status=line.stage.value,
                recorded_at=AT + timedelta(seconds=offset),
                payload=line.model_dump(mode="json"),
            ),
        )
    return canary


@pytest.fixture
def merged(tmp_path: Path) -> CanaryProvision:
    """Return a completed Batch whose one pass held twenty clears and opened a repair."""
    repaired = cycle(
        stage=BatchVerificationStage.REPAIR,
        audits=(
            *(audit(criterion) for criterion in CLEARING),
            *(audit(criterion, verdict=AuditVerdict.VERIFIED_FALSE) for criterion in FAILING),
        ),
        repairs_spent=1,
        exit=ConflictExit(kind=ConflictExitKind.REPAIR_TASK, ref=REPAIR_TASK),
    )
    return _reviewed(tmp_path, status="COMPLETED", lines=(repaired,))


def _served(canary: CanaryProvision, tmp_path: Path) -> list[str]:
    served = call_verb(
        "projection.trust.read", method_ctx(tmp_path / "runtime"), repo_root=str(canary.root)
    )
    session = Session()
    session.route = "trust"
    session.subj_id = MILESTONE
    w = 120
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=w,
        h=dict(SIZES)[w],
        linked=True,
        projection=build_verification_view(RouteProjection.model_validate(served)),
    )
    return render_route(view)


def _calibration(frame: list[str]) -> str:
    """Return the calibration line and the cohort line after it, joined."""
    index = next(i for i, row in enumerate(frame) if row.startswith(" CALIBRATION"))
    return f"{frame[index]} {frame[index + 1]}"


def _label(canary: CanaryProvision, tmp_path: Path, criterion: str, **extra: Any) -> dict[str, Any]:
    return call_verb(
        DELIVERY_LABEL_AUDIT_METHOD,
        method_ctx(tmp_path / "runtime"),
        repo_root=str(canary.root),
        urn=BATCH,
        actor="OPERATOR",
        idempotency_key=f"label-{criterion}",
        criterion_id=criterion,
        ground_truth=False,
        note=NOTE,
        **extra,
    )


def test_ui_020_a_merged_batch_the_jury_judged_right_earns_blocking_authority(
    merged: CanaryProvision, tmp_path: Path
) -> None:
    calibration = _calibration(_served(merged, tmp_path))

    assert "SCORED · Brier ~0.00 · co-error ~0.00" in calibration
    assert "n 22 of 20 scored · authority blocking · earned" in calibration


def test_ui_063_gold_labels_on_waved_through_subjects_refuse_on_the_co_error_rate(
    merged: CanaryProvision, tmp_path: Path
) -> None:
    for criterion in CLEARING[:3]:
        answer = _label(merged, tmp_path, criterion)
        assert (answer["criterion_id"], answer["ground_truth"]) == (criterion, False)

    calibration = _calibration(_served(merged, tmp_path))

    assert "SCORED · Brier ~0.14 · co-error ~0.60" in calibration
    assert "n 22 of 20 scored · authority refused · co-error" in calibration


def test_ui_063_the_repository_ceiling_is_the_one_the_gate_holds_the_jury_to(
    merged: CanaryProvision, tmp_path: Path
) -> None:
    for criterion in CLEARING[:3]:
        _label(merged, tmp_path, criterion)
    (merged.root / ".ea" / "config.yaml").write_text(
        "verify:\n  jury_max_co_error: 0.7\n", encoding="utf-8"
    )

    assert "authority blocking · earned" in _calibration(_served(merged, tmp_path))


def test_ui_063_a_label_is_filed_on_the_batch_ledger_and_a_retry_replays_it(
    merged: CanaryProvision, tmp_path: Path
) -> None:
    first = _label(merged, tmp_path, "CR-01")
    again = _label(merged, tmp_path, "CR-01")

    assert again == first
    lines = read_ledger_records(ledger_path(document_path(merged), Epoch2Collection.BATCH))
    labels = [item for item in lines if item.payload.get("payload_kind") == "audit_gold_label"]
    assert len(labels) == 1
    assert labels[0].payload["labeled_by"] == "OPERATOR"


def test_ui_063_a_label_on_a_criterion_no_cycle_judged_is_refused(
    merged: CanaryProvision, tmp_path: Path
) -> None:
    with pytest.raises(DaemonValidationError, match="gold_label_unanchored"):
        _label(merged, tmp_path, "CR-99")


def test_ui_063_a_label_whose_newest_verdict_is_unverified_is_refused(tmp_path: Path) -> None:
    """The cohort scores no unverified verdict, so a label on its subject would score nothing."""
    judged = cycle(audits=(audit("CR-01"),))
    reopened = cycle(
        audits=(audit("CR-01", verdict=AuditVerdict.UNVERIFIED, audit_id="BAU-900001"),)
    )
    canary = _reviewed(tmp_path, status="COMPLETED", lines=(judged, reopened))

    with pytest.raises(DaemonValidationError, match="gold_label_unscored"):
        _label(canary, tmp_path, "CR-01")


def test_ui_063_a_label_whose_newest_verdict_has_no_producer_is_refused(tmp_path: Path) -> None:
    """A verdict no ``(agent_role, runtime)`` answers for is never scored against one."""
    judged = cycle(audits=(audit("CR-01"),))
    canary = _reviewed(tmp_path, status="COMPLETED", lines=(judged,), harness=None)

    with pytest.raises(DaemonValidationError, match="gold_label_unscored"):
        _label(canary, tmp_path, "CR-01")


def test_ui_063_a_label_sent_against_a_stale_batch_revision_is_refused(
    merged: CanaryProvision, tmp_path: Path
) -> None:
    with pytest.raises(Exception, match="revision_conflict"):
        _label(merged, tmp_path, "CR-01", expected_revision=7)


def test_ui_020_a_batch_that_has_not_merged_still_reads_insufficient(tmp_path: Path) -> None:
    unsettled = cycle(audits=tuple(audit(criterion) for criterion in CLEARING))
    canary = _reviewed(tmp_path, status="ACTIVE", lines=(unsettled,))
    _label(canary, tmp_path, "CR-01")

    calibration = _calibration(_served(canary, tmp_path))

    assert "INSUFFICIENT · Brier ∅ unavailable" in calibration
    assert "n 1 of 20 scored · authority refused · n" in calibration


def _juror_row(frame: list[str]) -> str:
    """Return the reviewer's track-record row."""
    return next(row for row in frame if row.startswith(" reviewer · claude-code"))


def test_ui_063_the_track_record_scores_the_juror_on_its_settled_verdicts(
    merged: CanaryProvision, tmp_path: Path
) -> None:
    row = _juror_row(_served(merged, tmp_path))

    assert row.split()[3:6] == ["20", "2", "~0.91"]
    assert "~0.00 · 22 scored" in row


def test_ui_063_gold_labels_move_the_jurors_own_brier(
    merged: CanaryProvision, tmp_path: Path
) -> None:
    for criterion in CLEARING[:3]:
        _label(merged, tmp_path, criterion)

    assert "~0.14 · 22 scored" in _juror_row(_served(merged, tmp_path))


def test_ui_020_a_juror_under_the_floor_has_no_brier(tmp_path: Path) -> None:
    unsettled = cycle(audits=tuple(audit(criterion) for criterion in CLEARING))
    canary = _reviewed(tmp_path, status="ACTIVE", lines=(unsettled,))
    _label(canary, tmp_path, "CR-01")

    row = _juror_row(_served(canary, tmp_path))

    assert "∅ 1 of 20 scored" in row
