"""Filing a claim natively: the daemon allocates its key, digests its anchors and scores it.

These cases dispatch ``runtime.evidence.claim.file`` over a disposable epoch-2 canary
holding one seeded Milestone the claims are about. Evidence is filed through its verb; the
claim names only keys and ``path:start-end`` spans, so every key, URN and digest asserted
below was produced by the filing verb.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.state.epoch2.evidence_rung import ClaimLadder, span_digest
from eawf.kernel.store.compaction import read_document
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY, TransactionRefusedError
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery_acceptance import DELIVERY_RECORD_EVIDENCE_METHOD
from eawf.runtime.daemon.methods.evidence_ladder import (
    ANCHOR_UNREADABLE,
    EVIDENCE_CLAIM_FILE_METHOD,
)
from eawf.workflow.evidence.claim_ladder import EVIDENCE_LADDER_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    MILESTONE_URN,
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

AT: Final = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
NOTES: Final = "docs/replay-notes.md"
NOTES_TEXT: Final = "# Replay\n\nreplaying run 12 kept every event in order\nno event was lost\n"


_CANARIES: dict[Path, CanaryProvision] = {}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    canary = provision(tmp_path / "repo", code="CLF")
    seed(canary, {"milestone": {"MLS-0030": seed_row("milestone", "ACTIVE")}})
    (canary.root / "docs").mkdir(exist_ok=True)
    (canary.root / NOTES).write_text(NOTES_TEXT, encoding="utf-8")
    _CANARIES[canary.root] = canary
    return canary.root


def _dispatch(repo: Path, method: str, **params: Any) -> dict[str, Any]:
    answer: dict[str, Any] = asyncio.run(
        methods.dispatch(
            method, method_context(repo.parent / "runtime"), {"repo_root": str(repo), **params}
        )
    )
    return answer


def _cursor(repo: Path) -> int:
    """Return the tree's committed canonical sequence, as a caller reads it."""
    cursor: int = read_document(document_path(_CANARIES[repo])).get(CANONICAL_SEQUENCE_KEY, 0)
    return cursor


def _evidence(repo: Path) -> str:
    answer = _dispatch(
        repo,
        DELIVERY_RECORD_EVIDENCE_METHOD,
        urn=MILESTONE_URN,
        actor="OPERATOR",
        idempotency_key="evidence-1",
        kind="artifact",
        summary="replaying run 12 kept every event in order",
    )
    return str(answer["evidence_ref"]).rsplit("/", 1)[-1]


def _file(repo: Path, *, key: str = "claim-1", **fields: Any) -> dict[str, Any]:
    fields.setdefault("expected_revision", _cursor(repo))
    return _dispatch(
        repo,
        EVIDENCE_CLAIM_FILE_METHOD,
        urn=MILESTONE_URN,
        actor="OPERATOR",
        idempotency_key=key,
        title="Replay keeps order",
        **fields,
    )


def _ladder(repo: Path, key: str) -> ClaimLadder:
    return ClaimLadder.model_validate(_dispatch(repo, EVIDENCE_LADDER_METHOD, claim_key=key))


def _span(path: str = NOTES, start: int = 3, end: int = 4) -> dict[str, Any]:
    return {"path": path, "start_line": start, "end_line": end}


def test_filing_allocates_the_first_claim_key_under_the_subjects_project(repo: Path) -> None:
    answer = _file(repo)
    assert answer["claim_ref"] == "eawf://WSP-MAIN/PRJ-EAWF/_/claim/CLM-0001"
    claim = _ladder(repo, "CLM-0001").claim
    assert str(claim.subject_ref) == MILESTONE_URN


def test_each_new_filing_takes_the_next_key(repo: Path) -> None:
    _file(repo)
    assert _file(repo, key="claim-2")["claim_ref"].endswith("/claim/CLM-0002")


def test_a_retry_under_one_key_replays_without_allocating(repo: Path) -> None:
    cursor = _cursor(repo)
    first = _file(repo, expected_revision=cursor)
    assert _file(repo, expected_revision=cursor) == first
    assert _file(repo, key="claim-2")["claim_ref"].endswith("/claim/CLM-0002")


def test_a_stale_tree_cursor_is_a_revision_conflict(repo: Path) -> None:
    with pytest.raises(TransactionRefusedError, match="revision_conflict"):
        _file(repo, expected_revision=7)


def test_a_subject_the_tree_does_not_hold_is_refused(repo: Path) -> None:
    with pytest.raises(TransactionRefusedError, match="identity_not_found"):
        _dispatch(
            repo,
            EVIDENCE_CLAIM_FILE_METHOD,
            urn=MILESTONE_URN.replace("MLS-0030", "MLS-0099"),
            expected_revision=_cursor(repo),
            actor="OPERATOR",
            idempotency_key="claim-1",
            title="Replay keeps order",
        )


def test_a_run_is_not_a_claim_subject(repo: Path) -> None:
    """Error path: only a Task, Batch or Milestone is what a claim is about."""
    with pytest.raises(DaemonValidationError):
        _dispatch(
            repo,
            EVIDENCE_CLAIM_FILE_METHOD,
            urn="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000001",
            expected_revision=0,
            actor="OPERATOR",
            idempotency_key="claim-1",
            title="Replay keeps order",
        )


def test_the_anchor_digest_is_computed_from_the_working_tree(repo: Path) -> None:
    evidence = _evidence(repo)
    answer = _file(repo, evidence=[evidence], anchors=[_span()])
    # rung 2 passes because the digest was taken from the file as filed
    assert answer["outcomes"][:2] == ["passed", "passed"]
    [anchor] = _ladder(repo, "CLM-0001").claim.anchors
    expected = span_digest("replaying run 12 kept every event in order\nno event was lost")
    assert (anchor.anchor_digest, anchor.evidence_ref.entity_key) == (expected, evidence)


def test_a_one_line_span_at_the_last_line_is_digested(repo: Path) -> None:
    """Boundary: start equals end, on the file's final line."""
    evidence = _evidence(repo)
    _file(repo, evidence=[evidence], anchors=[_span(start=4, end=4)])
    [anchor] = _ladder(repo, "CLM-0001").claim.anchors
    assert anchor.anchor_digest == span_digest("no event was lost")


def test_a_span_past_the_last_line_is_refused(repo: Path) -> None:
    """Off-by-one: the file has four lines, so line 5 is not there."""
    evidence = _evidence(repo)
    with pytest.raises(DaemonValidationError, match=ANCHOR_UNREADABLE):
        _file(repo, evidence=[evidence], anchors=[_span(start=4, end=5)])


def test_a_missing_file_is_refused(repo: Path) -> None:
    evidence = _evidence(repo)
    with pytest.raises(DaemonValidationError, match=ANCHOR_UNREADABLE):
        _file(repo, evidence=[evidence], anchors=[_span(path="docs/absent.md")])


@pytest.mark.parametrize("path", ["/etc/hosts", "../outside.md", "docs/../../outside.md"])
def test_a_path_outside_the_repository_is_refused(repo: Path, path: str) -> None:
    evidence = _evidence(repo)
    with pytest.raises(DaemonValidationError):
        _file(repo, evidence=[evidence], anchors=[_span(path=path)])


def test_a_symlink_out_of_the_repository_is_refused(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(NOTES_TEXT, encoding="utf-8")
    (repo / "docs" / "link.md").symlink_to(outside)
    evidence = _evidence(repo)
    with pytest.raises(DaemonValidationError, match=ANCHOR_UNREADABLE):
        _file(repo, evidence=[evidence], anchors=[_span(path="docs/link.md")])


def test_an_anchor_with_no_cited_evidence_is_refused(repo: Path) -> None:
    with pytest.raises(DaemonValidationError):
        _file(repo, anchors=[_span()])


def test_a_receipt_key_is_filed_as_the_gate_receipt(repo: Path) -> None:
    evidence = _evidence(repo)
    _file(repo, evidence=[evidence, "RCP-0003"])
    claim = _ladder(repo, "CLM-0001").claim
    assert claim.gate_receipt == "RCP-0003"
    assert [ref.entity_key for ref in claim.evidence_refs] == [evidence]


def test_two_receipt_keys_are_refused(repo: Path) -> None:
    with pytest.raises(DaemonValidationError):
        _file(repo, evidence=["RCP-0001", "RCP-0002"])


def test_filing_scores_the_ladder_at_once(repo: Path) -> None:
    answer = _file(repo, evidence=[_evidence(repo)])
    assert answer["outcomes"] == ["passed", "unknown", "not_run", "not_run"]
    assert len(_ladder(repo, "CLM-0001").rungs) == 4


def test_both_evidence_routes_list_the_filed_claim(repo: Path) -> None:
    evidence = _evidence(repo)
    _file(repo, evidence=[evidence], anchors=[_span()])
    rows = _dispatch(repo, "projection.evidence.read")["rows"]
    claims = [row for row in rows if row["collection"] == "claim"]
    assert [(c["key"], c["title"]) for c in claims] == [("CLM-0001", "Replay keeps order")]
    digest = _dispatch(repo, "projection.evidence.digest.read", key="CLM-0001")["rows"]
    assert [row["key"] for row in digest if row["collection"] == "evidence"] == [evidence]
