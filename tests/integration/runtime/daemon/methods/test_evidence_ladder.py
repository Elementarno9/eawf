"""PLAN-048, PLAN-007: filing a claim writes one rung record per rung, and the ladder reads back.

These cases dispatch the registered verbs over a disposable epoch-2 canary: an evidence
row is filed through the delivery verb, a claim citing it is filed through the claim
verb, and the route read and the ladder read answer what the ledger holds. Nothing is
seeded by hand, so every record the assertions read was written by the producer.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.state.epoch2.evidence_rung import (
    RUNG_NAMES,
    UNKNOWN_FINDING,
    ClaimLadder,
    RungOutcome,
)
from eawf.kernel.store.ledger import read_ledger_records
from eawf.platform.install.canary import canary_ref, provision_canary
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery_acceptance import DELIVERY_RECORD_EVIDENCE_METHOD
from eawf.runtime.daemon.methods.evidence_ladder import (
    CLAIM_ALREADY_FILED,
    EVIDENCE_CLAIM_FILE_METHOD,
)
from eawf.workflow.evidence.claim_ladder import EVIDENCE_LADDER_METHOD, SCORER

pytestmark = pytest.mark.integration

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
CLAIM_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/claim/CLM-0004"
AT: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    provision_canary(repo_root=tmp_path / "repo", ref=canary_ref("CLM"), provisioned_at=AT)
    return tmp_path / "repo"


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


def _evidence(repo: Path) -> str:
    answer = _dispatch(
        repo,
        DELIVERY_RECORD_EVIDENCE_METHOD,
        urn=f"{CONTAINER}/milestone/MLS-0030",
        actor="OPERATOR",
        idempotency_key="evidence-1",
        kind="artifact",
        summary="replaying run 12 kept every event in order",
    )
    return str(answer["evidence_ref"])


def _file(repo: Path, *refs: str, key: str = "claim-1", **prose: str) -> dict[str, Any]:
    return _dispatch(
        repo,
        EVIDENCE_CLAIM_FILE_METHOD,
        urn=CLAIM_URN,
        actor="OPERATOR",
        idempotency_key=key,
        title="Replay keeps order",
        evidence_refs=list(refs),
        **prose,
    )


def _ladder(repo: Path) -> ClaimLadder:
    return ClaimLadder.model_validate(_dispatch(repo, EVIDENCE_LADDER_METHOD, claim_key="CLM-0004"))


def test_plan_048_filing_a_claim_writes_one_record_per_rung(repo: Path) -> None:
    """Evidence that resolves: rung 1 passes over the held digest, 2 is unknown, 3-4 not run."""
    ref = _evidence(repo)
    answer = _file(repo, ref, description="Replay any log and every event arrives in order.")
    assert answer["outcomes"] == ["passed", "unknown", "not_run", "not_run"]
    ladder = _ladder(repo)
    assert [r.rung for r in ladder.rungs] == [1, 2, 3, 4]
    assert [r.name for r in ladder.rungs] == list(RUNG_NAMES.values())
    resolve, anchor, screen, entail = ladder.rungs
    assert str(resolve.input_refs[0].ref) == ref
    assert resolve.input_refs[0].digest is not None
    assert resolve.input_refs[0].digest.startswith("sha256:")
    assert resolve.counts == {"references cited": 1, "references resolved": 1}
    assert resolve.evaluated_at is not None
    assert anchor.outcome is RungOutcome.UNKNOWN and anchor.finding == UNKNOWN_FINDING
    assert (screen.awaits_rung, entail.awaits_rung) == (2, 2)
    assert {r.evaluator for r in ladder.rungs} == {SCORER}
    assert ladder.claim.description == "Replay any log and every event arrives in order."


def test_plan_048_each_rung_states_the_sequence_its_own_append_allocated(repo: Path) -> None:
    _evidence(repo)
    _file(repo)
    sequences = [r.written_at_sequence for r in _ladder(repo).rungs]
    # the evidence row took 1, the claim row 2, and the rungs the four after it
    assert sequences == [3, 4, 5, 6]
    lines = list((repo / ".ea").rglob("ledger/claim.jsonl"))
    assert len(read_ledger_records(lines[0])) == 5


def test_plan_048_an_unheld_reference_fails_rung_1_and_runs_nothing_above(repo: Path) -> None:
    """Error path: a cited evidence record nobody filed leaves its digest unfetched."""
    answer = _file(repo, f"{CONTAINER}/evidence/EVD-0099")
    assert answer["outcomes"] == ["failed", "not_run", "not_run", "not_run"]
    resolve, *above = _ladder(repo).rungs
    assert resolve.input_refs[0].digest is None
    assert {r.awaits_rung for r in above} == {1}


def test_plan_048_a_claim_citing_nothing_resolves_nothing(repo: Path) -> None:
    """Boundary: zero references is a returned negative at rung 1, never a vacuous pass."""
    assert _file(repo)["outcomes"][0] == "failed"


def test_plan_007_an_uncertified_claim_is_filed_open_with_its_blockers(repo: Path) -> None:
    """The promotion gate decides the filed lifecycle: rung 4 has not passed, so it stays OPEN."""
    answer = _file(repo, _evidence(repo))
    assert answer["status"] == "OPEN"
    assert any("rung 4 (entail) has not passed" in b for b in answer["promotion_blockers"])
    assert _ladder(repo).claim.status == "OPEN"


def test_plan_048_a_key_already_filed_is_refused(repo: Path) -> None:
    _file(repo)
    with pytest.raises(DaemonValidationError, match=CLAIM_ALREADY_FILED):
        _file(repo, key="claim-2")


def test_plan_048_a_retry_under_one_key_replays_rather_than_refiling(repo: Path) -> None:
    first = _file(repo)
    assert _file(repo) == first


def test_plan_044_prose_over_its_bound_is_refused_at_the_verb(repo: Path) -> None:
    """Off-by-one: 300 characters of falsifier file, 301 are refused."""
    assert _file(repo, falsifier="x" * 300)["status"] == "OPEN"
    with pytest.raises(DaemonValidationError):
        _file(repo, key="claim-2", falsifier="x" * 301)


def test_ui_064_the_evidence_route_lists_a_filed_claim_from_its_ledger(repo: Path) -> None:
    _file(repo, _evidence(repo))
    rows = _dispatch(repo, "projection.evidence.read")["rows"]
    claims = [row for row in rows if row["collection"] == "claim"]
    assert [(c["key"], c["title"]) for c in claims] == [("CLM-0004", "Replay keeps order")]


def test_ui_051_a_ladder_read_for_an_unfiled_claim_is_refused(repo: Path) -> None:
    with pytest.raises(DaemonValidationError, match="holds no CLM-0004"):
        _ladder(repo)
