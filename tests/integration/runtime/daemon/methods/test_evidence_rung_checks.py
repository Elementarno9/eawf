"""The checks behind evidence rungs 2 to 4, run on filing, on demand and by attestation.

These cases dispatch the registered verbs over a disposable epoch-2 canary. Evidence and
claims are filed through their verbs; the only line written by hand is the gate receipt a
proof run would file, because running a real gate here would test the gate runner, not the
rung that reads its receipt.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.delivery.receipts import ProofReceipt
from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.state.epoch2.evidence_rung import (
    UNKNOWN_FINDING,
    ClaimLadder,
    RungBasis,
    RungOutcome,
    span_digest,
)
from eawf.kernel.store.ledger import append_ledger_record, read_ledger_records
from eawf.platform.install.canary import canary_ref, provision_canary
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusedError
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery_acceptance import DELIVERY_RECORD_EVIDENCE_METHOD
from eawf.runtime.daemon.methods.evidence_ladder import (
    ATTESTER,
    EVIDENCE_CLAIM_ATTEST_METHOD,
    EVIDENCE_CLAIM_CHECK_METHOD,
    EVIDENCE_CLAIM_FILE_METHOD,
    EVIDENCE_UNHELD,
    RUNG_NOT_RUNNABLE,
)
from eawf.workflow.evidence.claim_ladder import EVIDENCE_LADDER_METHOD
from tests.integration.runtime.daemon._delivery_verb_fixtures import proof_line
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
CLAIM_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/claim/CLM-0004"
AT: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
NOTES: Final = "docs/replay-notes.md"
NOTES_TEXT: Final = "# Replay\n\nreplaying run 12 kept every event in order\nno event was lost\n"
SUMMARY: Final = "replaying run 12 kept every event in order"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    provision_canary(repo_root=root, ref=canary_ref("CLM"), provisioned_at=AT)
    (root / "docs").mkdir(exist_ok=True)
    (root / NOTES).write_text(NOTES_TEXT, encoding="utf-8")
    return root


def _dispatch(repo: Path, method: str, **params: Any) -> dict[str, Any]:
    ctx = MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=repo.parent / "runtime" / "wal",
    )
    result: dict[str, Any] = asyncio.run(
        methods.dispatch(method, ctx, {"repo_root": str(repo), **params})
    )
    return result


def _evidence(repo: Path, summary: str = SUMMARY, key: str = "evidence-1") -> str:
    answer = _dispatch(
        repo,
        DELIVERY_RECORD_EVIDENCE_METHOD,
        urn=f"{CONTAINER}/milestone/MLS-0030",
        actor="OPERATOR",
        idempotency_key=key,
        kind="artifact",
        summary=summary,
    )
    return str(answer["evidence_ref"])


def _anchor(ref: str, *, start: int = 3, end: int = 4, text: str | None = None) -> dict[str, Any]:
    lines = NOTES_TEXT.splitlines()[start - 1 : end]
    return {
        "evidence_ref": ref,
        "path": NOTES,
        "start_line": start,
        "end_line": end,
        "anchor_digest": span_digest("\n".join(lines) if text is None else text),
    }


def _file(repo: Path, ref: str, *, key: str = "claim-1", **fields: Any) -> dict[str, Any]:
    fields.setdefault("description", "Replaying run 12 kept every event in order.")
    return _dispatch(
        repo,
        EVIDENCE_CLAIM_FILE_METHOD,
        urn=CLAIM_URN,
        actor="OPERATOR",
        idempotency_key=key,
        title="Replay keeps order",
        evidence_refs=[ref],
        **fields,
    )


def _ladder(repo: Path) -> ClaimLadder:
    return ClaimLadder.model_validate(_dispatch(repo, EVIDENCE_LADDER_METHOD, claim_key="CLM-0004"))


def _receipt(repo: Path, result: GateReceiptResult = GateReceiptResult.PASS) -> str:
    """File the receipt a proof run would, and return its key."""
    passing = world.receipts_at(
        world.compiled("CR-01"),
        revision_binding=world.delivering_generation().integrated_revision,
    )[0]
    receipt = ProofReceipt.model_validate(
        {
            **passing.model_dump(),
            "result": result,
            "exit_status": 0 if result is GateReceiptResult.PASS else 1,
        }
    )
    ledger = next((repo / ".ea").rglob("ledger/evidence.jsonl")).parent / "receipt.jsonl"
    append_ledger_record(ledger, proof_line(receipt))
    return receipt.id


def _check(repo: Path, fragment: str = "", *, revision: int = 1, key: str = "check-1") -> Any:
    return _dispatch(
        repo,
        EVIDENCE_CLAIM_CHECK_METHOD,
        urn=f"{CLAIM_URN}{fragment}",
        expected_revision=revision,
        actor="OPERATOR",
        idempotency_key=key,
    )


def _attest(repo: Path, ref: str, *, fragment: str = "#rung-4", key: str = "attest-1") -> Any:
    return _dispatch(
        repo,
        EVIDENCE_CLAIM_ATTEST_METHOD,
        urn=f"{CLAIM_URN}{fragment}",
        expected_revision=1,
        actor="OPERATOR",
        idempotency_key=key,
        evidence_ref=ref,
        outcome="passed",
        finding="the reviewer reproduced the replay by hand",
    )


# ---------- rung 2: anchor ----------


def test_rung_2_passes_over_an_anchored_span_that_still_matches(repo: Path) -> None:
    ref = _evidence(repo)
    answer = _file(repo, ref, anchors=[_anchor(ref)])
    assert answer["outcomes"][:2] == ["passed", "passed"]
    anchor = _ladder(repo).rungs[1]
    assert anchor.counts["spans matched"] == 1
    assert anchor.evaluated_at is not None
    assert anchor.input_refs[0].digest == _anchor(ref)["anchor_digest"]


def test_rung_2_fails_on_a_changed_span_and_holds_the_rungs_above(repo: Path) -> None:
    ref = _evidence(repo)
    answer = _file(repo, ref, anchors=[_anchor(ref, text="what the writer once read")])
    assert answer["outcomes"] == ["passed", "failed", "not_run", "not_run"]
    _resolve, anchor, screen, entail = _ladder(repo).rungs
    assert anchor.counts["spans changed"] == 1
    assert (screen.awaits_rung, entail.awaits_rung) == (2, 2)


def test_rung_2_fails_on_a_span_past_the_end_of_its_file(repo: Path) -> None:
    """Off-by-one: the file has four lines, so a span ending at line 5 is gone."""
    ref = _evidence(repo)
    assert _file(repo, ref, anchors=[_anchor(ref, start=4, end=5)])["outcomes"][1] == "failed"
    assert _ladder(repo).rungs[1].counts["spans missing"] == 1


def test_rung_2_stays_unknown_for_a_claim_that_anchors_nothing(repo: Path) -> None:
    _file(repo, _evidence(repo))
    anchor = _ladder(repo).rungs[1]
    assert (anchor.outcome, anchor.finding) == (RungOutcome.UNKNOWN, UNKNOWN_FINDING)


@pytest.mark.parametrize("path", ["../outside.md", "/etc/passwd", "docs\\notes.md"])
def test_an_anchor_that_leaves_the_repository_is_refused_at_the_verb(repo: Path, path: str) -> None:
    ref = _evidence(repo)
    with pytest.raises(DaemonValidationError):
        _file(repo, ref, anchors=[{**_anchor(ref), "path": path}])


def test_an_anchor_on_a_record_the_claim_does_not_cite_is_refused(repo: Path) -> None:
    ref = _evidence(repo)
    with pytest.raises(DaemonValidationError):
        _file(repo, ref, anchors=[_anchor(f"{CONTAINER}/evidence/EVD-0099")])


# ---------- rung 3: screen ----------


def test_rung_3_screens_the_claim_against_its_cited_records(repo: Path) -> None:
    ref = _evidence(repo)
    _file(repo, ref, anchors=[_anchor(ref)])
    screen = _ladder(repo).rungs[2]
    assert screen.outcome is RungOutcome.PASSED
    assert screen.finding.startswith("entailed")
    assert screen.counts["entailment per mille"] >= 700


def test_a_rung_3_negative_is_advisory_and_leaves_rung_4_runnable(repo: Path) -> None:
    ref = _evidence(repo)
    answer = _file(
        repo, ref, anchors=[_anchor(ref)], description="Compaction halves the ledger size."
    )
    assert answer["outcomes"][2:] == ["failed", "unknown"]
    assert _ladder(repo).rungs[3].awaits_rung is None


# ---------- rung 4: entail ----------


def test_rung_4_entails_over_a_passing_receipt_and_the_claim_is_supported(repo: Path) -> None:
    ref = _evidence(repo)
    receipt = _receipt(repo)
    answer = _file(repo, ref, anchors=[_anchor(ref)], gate_receipt=receipt)
    assert answer["outcomes"] == ["passed", "passed", "passed", "passed"]
    assert (answer["status"], answer["promotion_blockers"]) == ("SUPPORTED", [])
    entail = _ladder(repo).rungs[3]
    assert entail.basis is RungBasis.AUTOMATED
    assert entail.evidence_ref is not None
    assert receipt in entail.finding
    lines = read_ledger_records(next((repo / ".ea").rglob("ledger/evidence.jsonl")))
    filed = {line.record_key: line.payload for line in lines}
    assert filed[entail.evidence_ref.entity_key]["kind"] == "store_record"


def test_rung_4_fails_over_a_failing_receipt(repo: Path) -> None:
    ref = _evidence(repo)
    receipt = _receipt(repo, GateReceiptResult.FAIL)
    answer = _file(repo, ref, anchors=[_anchor(ref)], gate_receipt=receipt)
    assert answer["outcomes"][3] == "failed"
    assert answer["status"] == "OPEN"
    assert _ladder(repo).rungs[3].counts == {"exit status": 1}


def test_rung_4_fails_when_the_named_receipt_is_not_held(repo: Path) -> None:
    ref = _evidence(repo)
    answer = _file(repo, ref, anchors=[_anchor(ref)], gate_receipt="RCP-9999")
    assert answer["outcomes"][3] == "failed"
    assert "RCP-9999 the claim names is not held" in _ladder(repo).rungs[3].finding


# ---------- the check verb ----------


def test_check_reruns_the_named_rung_and_every_rung_above_it(repo: Path) -> None:
    ref = _evidence(repo)
    _file(repo, ref, anchors=[_anchor(ref)])
    (repo / NOTES).write_text("# Replay\n\nrewritten\n", encoding="utf-8")
    answer = _check(repo, "#rung-2")
    assert answer["outcomes"] == ["passed", "failed", "not_run", "not_run"]
    revisions = [r.revision for r in _ladder(repo).rungs]
    assert revisions == [0, 1, 1, 1]


def test_check_moves_the_claim_when_its_receipt_lands_later(repo: Path) -> None:
    ref = _evidence(repo)
    receipt = "RCP-0001"
    assert _file(repo, ref, anchors=[_anchor(ref)], gate_receipt=receipt)["status"] == "OPEN"
    assert _receipt(repo) == receipt
    answer = _check(repo, "#rung-4")
    assert (answer["status"], answer["outcomes"][3]) == ("SUPPORTED", "passed")
    ladder = _ladder(repo)
    assert (ladder.claim.revision, ladder.claim.status) == (2, "SUPPORTED")


def test_check_against_a_stale_revision_is_a_revision_conflict(repo: Path) -> None:
    _file(repo, _evidence(repo))
    with pytest.raises(TransactionRefusedError, match="revision_conflict"):
        _check(repo, revision=2)


def test_check_of_an_unfiled_claim_is_refused(repo: Path) -> None:
    with pytest.raises(DaemonValidationError, match="holds no CLM-0004"):
        _check(repo)


# ---------- the attest verb ----------


def test_an_attested_rung_4_attests_without_certifying(repo: Path) -> None:
    ref = _evidence(repo)
    _file(repo, ref, anchors=[_anchor(ref)])
    signed = _evidence(repo, "the reviewer reproduced replay run 12 by hand", key="evidence-2")
    answer = _attest(repo, signed)
    assert answer["outcomes"][3] == "passed"
    assert answer["status"] == "OPEN"
    assert any("attested" in b for b in answer["promotion_blockers"])
    entail = _ladder(repo).rungs[3]
    assert (entail.basis, entail.evaluator) == (RungBasis.ATTESTED, ATTESTER)
    assert str(entail.evidence_ref) == signed


def test_only_rung_4_is_attested(repo: Path) -> None:
    ref = _evidence(repo)
    _file(repo, ref, anchors=[_anchor(ref)])
    with pytest.raises(DaemonValidationError, match=RUNG_NOT_RUNNABLE):
        _attest(repo, ref, fragment="#rung-3")


def test_attestation_waits_on_an_unpassed_rung_2(repo: Path) -> None:
    ref = _evidence(repo)
    _file(repo, ref)
    with pytest.raises(DaemonValidationError, match=RUNG_NOT_RUNNABLE):
        _attest(repo, ref)


def test_attestation_cites_a_held_evidence_record(repo: Path) -> None:
    ref = _evidence(repo)
    _file(repo, ref, anchors=[_anchor(ref)])
    with pytest.raises(DaemonValidationError, match=EVIDENCE_UNHELD):
        _attest(repo, f"{CONTAINER}/evidence/EVD-0099")


# ---------- the evidence routes ----------


def test_the_evidence_route_states_each_claims_ladder(repo: Path) -> None:
    ref = _evidence(repo)
    _file(repo, ref, anchors=[_anchor(ref)], gate_receipt=_receipt(repo))
    rows = _dispatch(repo, "projection.evidence.read")["rows"]
    [claim] = [row for row in rows if row["collection"] == "claim"]
    assert claim["facts"]["outcome"] == "1 passed · 2 passed · 3 passed · 4 passed"
    assert claim["facts"]["checked"] == "yes"
    assert datetime.fromisoformat(claim["facts"]["as_of"]) == _ladder(repo).rungs[3].evaluated_at


def test_an_open_ladder_is_not_checked(repo: Path) -> None:
    _file(repo, _evidence(repo))
    rows = _dispatch(repo, "projection.evidence.read")["rows"]
    [claim] = [row for row in rows if row["collection"] == "claim"]
    assert claim["facts"]["checked"] == "no"


def test_the_rung_card_lists_what_each_rung_ran_over(repo: Path) -> None:
    ref = _evidence(repo)
    _file(repo, ref, anchors=[_anchor(ref)])
    rows = _dispatch(repo, "projection.evidence.digest.read", key="CLM-0004")["rows"]
    [evidence] = [row for row in rows if row["collection"] == "evidence"]
    assert evidence["key"] == ref.rsplit("/", 1)[-1]
    assert evidence["facts"]["found"].startswith("rung 3 passed")
    assert evidence["facts"]["input_digest"] == _ladder(repo).rungs[0].input_refs[0].digest


def test_the_rung_card_lists_nothing_without_a_claim(repo: Path) -> None:
    _file(repo, _evidence(repo))
    rows = _dispatch(repo, "projection.evidence.digest.read")["rows"]
    assert rows == []
