"""The ``0.7.0.dev5`` checkpoint is release-ready at merge, pending its tag.

The receipt file committed beside the dev5 canary export binds each of the
six pre-merge canary-window gates to the evidence of a run on this
repository. Read here over the files this checkout ships, it must support
exactly "release-ready pending post-merge observation": everything a merge
can hold is bound, and the tag observation -- which only a pushed tag can
produce -- is reported outstanding rather than missing.

Each receipt has to be load-bearing, so each is removed in turn and the
verdict must drop to not ready, and ``release create`` must refuse the
rung by name. The package is the checkpoint too: the version module reads
dev5 and the changelog carries the section the tag chokepoint mines.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf import __version__
from eawf.kernel.release.gate_binding import (
    PRODUCT_CANARY_POST_MERGE_GATES,
    PRODUCT_CANARY_PRE_MERGE_GATES,
)
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.release.canary_receipts import (
    CanaryReadinessVerdict,
    CanaryReceiptSet,
    assert_pre_merge_receipts_bound,
    assess_canary_receipts,
    canary_receipts_path,
    load_canary_receipts,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
DEV5_VERSION = "0.7.0.dev5"
DEV5_KEY = f"REL-{DEV5_VERSION}"

#: The committed record of this repository's live epoch-2 cutover.
LIVE_CUTOVER_RECORD = ".ea/artifacts/evidence/2026-09-dev5-live-cutover/live-cutover.json"

#: The real-daemon proof that a filed report seals its candidate.
INTEGRATE_SEAL_PROOF = "tests/integration/runtime/daemon/test_integrate_seal_real_daemon.py"

#: The pending actions the operator answered to approve PRV-0101, PRV-0201
#: and PRV-0301, the rc1, rc2 and stable plan revisions.
LIVE_PLAN_APPROVALS = tuple(
    f"eawf://EAWF/EAWF/EAWF/pending-action/ACT-0{rung}01" for rung in (1, 2, 3)
)

#: The native plan revisions of the rungs after this one.
ROADMAP_PLANS = tuple(f"docs/roadmap/v0.7/{rung}.json" for rung in ("rc1", "rc2", "stable"))


def committed() -> CanaryReceiptSet:
    """Return the receipt set this checkout commits for dev5."""
    receipts = load_canary_receipts(REPO_ROOT, DEV5_KEY)
    assert receipts is not None, "no dev5 canary-window receipt file is committed"
    return receipts


def committed_document() -> dict[str, Any]:
    """Return the committed receipt file, decoded and mutable."""
    document: dict[str, Any] = json.loads(
        canary_receipts_path(REPO_ROOT, DEV5_KEY).read_text(encoding="utf-8")
    )
    return document


def without(gate: ReleaseGateName) -> CanaryReceiptSet:
    """Return the committed receipt set with *gate*'s receipt removed."""
    document = committed_document()
    document["receipts"] = [row for row in document["receipts"] if row["gate"] != gate.value]
    return CanaryReceiptSet.model_validate(document)


def evidence_of(gate: ReleaseGateName) -> tuple[str, ...]:
    """Return the evidence refs the committed receipt of *gate* names."""
    for receipt in committed().receipts:
        if receipt.gate is gate:
            return receipt.evidence_refs
    raise AssertionError(f"no committed receipt for {gate.value}")


def test_the_committed_record_binds_the_six_pre_merge_receipts() -> None:
    receipts = committed()

    assert receipts.release_key == DEV5_KEY
    assert tuple(receipt.gate for receipt in receipts.receipts) == PRODUCT_CANARY_PRE_MERGE_GATES


def test_the_record_is_ready_pending_post_merge_observation() -> None:
    readiness = assess_canary_receipts(committed())

    assert readiness.verdict is CanaryReadinessVerdict.PENDING_POST_MERGE
    assert readiness.unbound_pre_merge == ()
    assert readiness.pending_post_merge == (ReleaseGateName.RELEASE_TAGGED_OBSERVED,)
    assert_pre_merge_receipts_bound(DEV5_KEY, committed())


def test_every_repository_evidence_ref_names_a_committed_file() -> None:
    for receipt in committed().receipts:
        for ref in (*receipt.evidence_refs, *receipt.live_refs):
            if "://" not in ref:
                assert (REPO_ROOT / ref).is_file(), f"{receipt.gate.value}: {ref}"


def test_the_live_cutover_receipt_binds_the_committed_cutover_record() -> None:
    assert LIVE_CUTOVER_RECORD in evidence_of(ReleaseGateName.MIGRATION_RERUN_IDENTICAL)


def test_the_integration_receipt_binds_the_real_daemon_seal_proof() -> None:
    assert INTEGRATE_SEAL_PROOF in evidence_of(ReleaseGateName.EXACT_HEAD_INTEGRATION)


def test_the_plan_receipt_binds_the_native_roadmap_plans() -> None:
    assert set(ROADMAP_PLANS) <= set(evidence_of(ReleaseGateName.PLAN_REVISION_APPROVED))


def test_the_plan_receipt_carries_the_live_approvals_of_the_three_plans() -> None:
    """The operator's approvals of PRV-0101, PRV-0201 and PRV-0301, filed as data."""
    receipt = next(
        receipt
        for receipt in committed().receipts
        if receipt.gate is ReleaseGateName.PLAN_REVISION_APPROVED
    )

    assert receipt.live_refs == LIVE_PLAN_APPROVALS


def test_live_plan_approvals_are_accepted_as_data() -> None:
    """Approval refs the operator appends later validate without a code change."""
    document = committed_document()
    for row in document["receipts"]:
        if row["gate"] == ReleaseGateName.PLAN_REVISION_APPROVED.value:
            row["live_refs"] = [
                "eawf://WSP-EAWF/PRJ-EAWF/REP-EAWF/plan/PLR-0001#approval",
                "eawf://WSP-EAWF/PRJ-EAWF/REP-EAWF/plan/PLR-0002#approval",
            ]

    receipts = CanaryReceiptSet.model_validate(document)

    assert assess_canary_receipts(receipts).verdict is CanaryReadinessVerdict.PENDING_POST_MERGE


@pytest.mark.parametrize("gate", PRODUCT_CANARY_PRE_MERGE_GATES, ids=lambda gate: gate.value)
def test_a_removed_pre_merge_receipt_is_not_ready(gate: ReleaseGateName) -> None:
    """Gate-fire proof: every one of the six receipts is load-bearing."""
    readiness = assess_canary_receipts(without(gate))

    assert readiness.verdict is CanaryReadinessVerdict.NOT_READY
    assert readiness.unbound_pre_merge == (gate,)


def test_a_removed_live_cutover_receipt_refuses_the_create() -> None:
    """Gate-fire proof: the create admission reds on the missing re-run receipt."""
    with pytest.raises(UserError) as caught:
        assert_pre_merge_receipts_bound(
            DEV5_KEY, without(ReleaseGateName.MIGRATION_RERUN_IDENTICAL)
        )

    assert caught.value.kind == "canary_receipts_unbound"
    assert "migration_rerun_identical" in str(caught.value)


def test_no_committed_record_refuses_every_pre_merge_gate() -> None:
    with pytest.raises(UserError) as caught:
        assert_pre_merge_receipts_bound(DEV5_KEY, None)

    assert caught.value.kind == "canary_receipts_unbound"
    for gate in PRODUCT_CANARY_PRE_MERGE_GATES:
        assert gate.value in str(caught.value)


def test_the_tag_observation_completes_the_record() -> None:
    document = committed_document()
    document["receipts"].append(
        {
            "gate": PRODUCT_CANARY_POST_MERGE_GATES[0].value,
            "evidence_refs": [f"https://pypi.org/project/eawf/{DEV5_VERSION}/"],
        }
    )

    readiness = assess_canary_receipts(CanaryReceiptSet.model_validate(document))

    assert readiness.verdict is CanaryReadinessVerdict.RELEASE_READY
    assert readiness.pending_post_merge == ()


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        ({"gate": "version_consistency", "evidence_refs": ["CHANGELOG.md"]}, "canary-window"),
        ({"gate": "parallel_dispatch", "evidence_refs": []}, "at least 1"),
        ({"gate": "parallel_dispatch", "evidence_refs": ["/abs/record.json"]}, "relative"),
        ({"gate": "parallel_dispatch", "evidence_refs": ["../outside.json"]}, "relative"),
        ({"gate": "parallel_dispatch", "evidence_refs": ["a"], "extra": 1}, "extra"),
    ],
)
def test_a_malformed_receipt_is_refused(row: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        CanaryReceiptSet.model_validate({"release_key": DEV5_KEY, "receipts": [row]})


def test_a_gate_filed_twice_is_refused() -> None:
    document = committed_document()
    document["receipts"].append(document["receipts"][0])

    with pytest.raises(ValidationError, match="more than one receipt"):
        CanaryReceiptSet.model_validate(document)


def test_a_record_filed_for_another_rung_does_not_load(tmp_path: Path) -> None:
    document = committed_document()
    document["release_key"] = "REL-0.7.0.dev4"
    path = canary_receipts_path(tmp_path, DEV5_KEY)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match=r"was filed for REL-0\.7\.0\.dev4"):
        load_canary_receipts(tmp_path, DEV5_KEY)


def test_an_absent_record_and_an_unmapped_rung_read_as_none(tmp_path: Path) -> None:
    assert load_canary_receipts(tmp_path, DEV5_KEY) is None
    assert load_canary_receipts(REPO_ROOT, "REL-0.7.0rc1") is None


def test_the_package_version_is_dev5() -> None:
    assert __version__ == DEV5_VERSION


def test_the_changelog_section_states_its_migration_and_limitations() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    heading = f"## [{DEV5_VERSION}]"
    assert heading in text, f"CHANGELOG.md has no {heading} section"
    start = text.index(heading)
    end = text.index("## [0.7.0.dev4]")
    assert start < end
    section = text[start:end]

    assert "### Migration" in section
    assert "### Limitations" in section
    assert section.count("\n- ") >= 3
