"""Reconciling a merging Batch against what the target branch actually holds.

The suite drives the two answers a read-back can give and the one it
cannot. A refusal returns the Batch to work and must name why; a branch
that carries the Batch's pinned head completes it; and anything else --
a branch that could not be read, or one that was read and does not carry
it -- leaves the Batch merging, because inventing a status for "we do not
know" is how an unresolved merge becomes a decided one.

Two claims are checked structurally rather than by reading the code. The
path each outcome takes is looked up in the transition registry by the
test as well as by the module, so an edge that stops existing fails here.
And the registry is walked to show there is no way out of ``MERGING``
that an unknown outcome could take: every outgoing row needs either an
observed fact or a recorded reason, so none of them is reachable by a
caller that merely believes the merge happened.

Nothing here reads a clock, opens a socket, spawns a daemon, or writes
outside ``tmp_path``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.base import BranchName
from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.transitions import (
    OBSERVED_GUARD_FACTS,
    AmbiguityLabel,
    LifecycleEntity,
    ObservedFact,
    TransitionGuard,
    TransitionVerb,
    row_for,
    rows_from,
)
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery_acceptance import (
    MergeReconcileParams,
    reconcile_batch_merge,
)
from eawf.workflow.integration.reconcile import (
    MAX_OBSERVED_COMMITS,
    HostMergeObservation,
    MergeOutcome,
    MergeReconciliation,
    ReconciliationRefusal,
    ReconciliationRefusedError,
    reconcile_merge,
    reconciliation_record_key,
)
from tests.integration.workflow.delivery._completion_fixtures import (
    AT,
    BATCH,
    CONTAINER,
    OTHER_BATCH,
    REPOSITORY,
    SECOND_HEAD,
    THIRD_HEAD,
    TREE,
    native_tree,
)

MILESTONE: Final = f"{CONTAINER}/milestone/MLS-0030"
BRANCH: Final = "main"
OTHER_BRANCH: Final = "release/0.7"
DIGEST: Final = f"sha256:{'e' * 64}"

#: A commit that is on the target branch and is not the Batch's own head.
UNRELATED_HEAD: Final = "7f" * 20


def head_binding(head_sha: str = SECOND_HEAD) -> dict[str, Any]:
    """Return the exact head binding a mergeable Batch pins."""
    return {
        "head_sha": head_sha,
        "tree_sha": TREE,
        "contract_digest": DIGEST,
        "policy_revision": 1,
        "evidence_digest": DIGEST,
    }


def batch_row(
    *,
    status: BatchStatus = BatchStatus.MERGING,
    head_sha: str | None = SECOND_HEAD,
    branch: BranchName | None = BRANCH,
    urn: str = BATCH,
) -> dict[str, Any]:
    """Return one DeliveryBatch payload, ready to seed a document."""
    key = urn.rsplit("/", 1)[1]
    row: dict[str, Any] = {
        "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, urn)),
        "key": key,
        "urn": urn,
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
        "milestone_ref": MILESTONE,
        "repository_ref": REPOSITORY,
        "status": status.value,
    }
    if branch is not None:
        row["target_branch"] = branch
    if head_sha is not None:
        row["current_head_binding"] = head_binding(head_sha)
    return row


def batch(**overrides: Any) -> DeliveryBatch:
    """Return one DeliveryBatch, merging by default."""
    return DeliveryBatch.model_validate(batch_row(**overrides))


def cause(code: str = "checks-failed") -> dict[str, Any]:
    """Return the named cause a host refusal carries."""
    return {
        "code": code,
        "message": "the host refused the merge because a required check did not pass",
    }


def observation(
    *,
    urn: str = BATCH,
    branch: BranchName = BRANCH,
    target_head_sha: str | None = THIRD_HEAD,
    contained: tuple[str, ...] | None = None,
    refusal: dict[str, Any] | None = None,
) -> HostMergeObservation:
    """Return one read-back of the target branch."""
    shas: tuple[str, ...]
    if contained is not None:
        shas = contained
    elif target_head_sha is None:
        shas = ()
    else:
        shas = (target_head_sha,)
    return HostMergeObservation.model_validate(
        {
            "batch_ref": urn,
            "target_branch": branch,
            "observed_at": AT,
            "target_head_sha": target_head_sha,
            "contained_shas": shas,
            "refusal": refusal,
        }
    )


# ---------- a refusal returns the Batch to ACTIVE with a named cause ----------


def test_a_refused_merge_returns_the_batch_to_active() -> None:
    """The Batch goes back to work, not to a terminal state."""
    decision = reconcile_merge(batch(), observation(refusal=cause()))

    assert decision.outcome is MergeOutcome.REFUSED
    assert decision.from_status is BatchStatus.MERGING
    assert decision.to_status is BatchStatus.ACTIVE


def test_a_refused_merge_carries_the_named_cause() -> None:
    """A Batch pushed back into work with no reason is one nobody can act on."""
    decision = reconcile_merge(batch(), observation(refusal=cause("branch-protected")))

    assert decision.cause is not None
    assert decision.cause.code == "branch-protected"
    assert "branch-protected" in decision.reason


def test_an_observation_naming_no_cause_never_reaches_the_refused_outcome() -> None:
    """One half of the construction: no cause in, no refusal out."""
    for read in (observation(), observation(target_head_sha=None)):
        assert reconcile_merge(batch(), read).outcome is MergeOutcome.UNKNOWN


def test_a_refusing_decision_with_no_cause_does_not_validate() -> None:
    """The other half: the shape itself refuses an unexplained return to work."""
    unexplained = {
        "batch_ref": BATCH,
        "outcome": MergeOutcome.REFUSED.value,
        "from_status": BatchStatus.MERGING.value,
        "to_status": BatchStatus.ACTIVE.value,
        "path": [TransitionVerb.MERGE_REFUSED.value, TransitionVerb.REOPENED.value],
        "observed_facts": [ObservedFact.HOST_MERGE_REFUSED.value],
        "cause": None,
        "target_branch": BRANCH,
        "observed_at": AT,
        "reason": "the host said no",
    }
    with pytest.raises(ValidationError, match="with a named cause"):
        MergeReconciliation.model_validate(unexplained)


def test_the_named_cause_guard_has_teeth_because_the_same_row_validates_with_one() -> None:
    """A guard that cannot pass proves nothing about the rows it admits."""
    explained = {
        "batch_ref": BATCH,
        "outcome": MergeOutcome.REFUSED.value,
        "from_status": BatchStatus.MERGING.value,
        "to_status": BatchStatus.ACTIVE.value,
        "path": [TransitionVerb.MERGE_REFUSED.value, TransitionVerb.REOPENED.value],
        "observed_facts": [ObservedFact.HOST_MERGE_REFUSED.value],
        "cause": cause(),
        "target_branch": BRANCH,
        "observed_at": AT,
        "reason": "the host said no",
    }
    assert MergeReconciliation.model_validate(explained).cause is not None


def test_the_return_path_is_the_two_registered_edges() -> None:
    """The decision names the registry's own verbs, not invented ones."""
    decision = reconcile_merge(batch(), observation(refusal=cause()))

    assert decision.path == (TransitionVerb.MERGE_REFUSED, TransitionVerb.REOPENED)


def test_both_return_edges_exist_in_the_registry() -> None:
    """The path is looked up here too, so a dropped edge reds this test."""
    refused = row_for(
        LifecycleEntity.DELIVERY_BATCH, BatchStatus.MERGING, BatchStatus.READY_TO_MERGE
    )
    reopened = row_for(
        LifecycleEntity.DELIVERY_BATCH, BatchStatus.READY_TO_MERGE, BatchStatus.ACTIVE
    )
    assert refused is not None
    assert reopened is not None
    assert TransitionGuard.HOST_MERGE_REFUSED in refused.guards
    assert TransitionGuard.REASON_RECORDED in reopened.guards


def test_a_refusal_supplies_the_observed_fact_the_registry_demands() -> None:
    """The refusing edge sits behind an observation, and this is it."""
    decision = reconcile_merge(batch(), observation(refusal=cause()))

    assert decision.observed_facts == (ObservedFact.HOST_MERGE_REFUSED,)


def test_a_refusal_contradicted_by_the_branch_is_refused() -> None:
    """The host cannot have refused a merge the branch is carrying."""
    carrying = observation(contained=(THIRD_HEAD, SECOND_HEAD), refusal=cause())
    with pytest.raises(ReconciliationRefusedError) as caught:
        reconcile_merge(batch(), carrying)

    assert caught.value.code is ReconciliationRefusal.OBSERVATION_MISBOUND


# ---------- an unknown outcome keeps the Batch MERGING ----------


def test_a_branch_that_does_not_carry_the_head_leaves_the_batch_merging() -> None:
    """Read, and the commit is not there: not yet, rather than refused."""
    decision = reconcile_merge(batch(), observation(contained=(THIRD_HEAD, UNRELATED_HEAD)))

    assert decision.outcome is MergeOutcome.UNKNOWN
    assert decision.to_status is BatchStatus.MERGING
    assert decision.path == ()


def test_a_branch_that_could_not_be_read_leaves_the_batch_merging() -> None:
    """Unread and unreadable are the same answer: nothing is established."""
    decision = reconcile_merge(batch(), observation(target_head_sha=None))

    assert decision.outcome is MergeOutcome.UNKNOWN
    assert decision.to_status is BatchStatus.MERGING
    assert not decision.resolved


def test_an_unknown_outcome_names_the_commit_it_is_waiting_for() -> None:
    """Waiting for something unnamed is indistinguishable from stuck."""
    decision = reconcile_merge(batch(head_sha=SECOND_HEAD), observation())

    assert decision.awaited_head_sha == SECOND_HEAD
    assert decision.matched_head_sha is None


def test_an_unknown_outcome_supplies_no_observed_fact() -> None:
    """Supplying none is why both edges out of MERGING stay shut."""
    decision = reconcile_merge(batch(), observation())

    assert decision.observed_facts == ()


def test_an_unresolved_batch_keeps_the_merging_ambiguity_label() -> None:
    """A surface lists it as unresolved rather than as a status of its own."""
    decision = reconcile_merge(batch(), observation())

    assert decision.label is AmbiguityLabel.MERGING


def test_no_edge_out_of_merging_is_reachable_without_a_fact_or_a_reason() -> None:
    """Structural: an unknown outcome has nothing it could push through."""
    outgoing = rows_from(LifecycleEntity.DELIVERY_BATCH, BatchStatus.MERGING)
    assert outgoing
    for row in outgoing:
        needs_observation = any(guard in OBSERVED_GUARD_FACTS for guard in row.guards)
        names_reason = TransitionGuard.REASON_RECORDED in row.guards
        assert needs_observation or names_reason, row


def test_observation_of_the_head_is_what_reconciles_it() -> None:
    """The same Batch, one more read-back: the unknown becomes a landing."""
    merging = batch()
    before = reconcile_merge(merging, observation())
    after = reconcile_merge(merging, observation(contained=(THIRD_HEAD, SECOND_HEAD)))

    assert before.outcome is MergeOutcome.UNKNOWN
    assert after.outcome is MergeOutcome.LANDED
    assert after.matched_head_sha == SECOND_HEAD
    assert after.to_status is BatchStatus.COMPLETED


def test_a_landing_supplies_the_observed_fact_the_registry_demands() -> None:
    """Completion sits behind an observation of the host, and this is it."""
    decision = reconcile_merge(batch(), observation(contained=(THIRD_HEAD, SECOND_HEAD)))

    assert decision.observed_facts == (ObservedFact.HOST_MERGE_OBSERVED,)
    assert decision.path == (TransitionVerb.MERGE_OBSERVED, TransitionVerb.COMPLETED)


# ---------- the same observation twice is one standing answer ----------


def test_the_same_observation_decides_the_same_thing_twice() -> None:
    """The decision is a pure function of the head and what was read."""
    merging = batch()
    first = reconcile_merge(merging, observation())
    second = reconcile_merge(merging, observation())

    assert first == second


def test_the_same_observation_files_under_the_same_derived_key() -> None:
    """A repeat overwrites its own line rather than growing a pile."""
    merging = batch()
    first = reconcile_merge(merging, observation())
    second = reconcile_merge(merging, observation(target_head_sha=UNRELATED_HEAD))

    assert reconciliation_record_key(first) == reconciliation_record_key(second)


def test_a_different_outcome_files_under_a_different_key() -> None:
    """The key has teeth: a landing is not filed over an unresolved question."""
    merging = batch()
    unresolved = reconcile_merge(merging, observation())
    landed = reconcile_merge(merging, observation(contained=(THIRD_HEAD, SECOND_HEAD)))

    assert reconciliation_record_key(unresolved) != reconciliation_record_key(landed)


# ---------- what the reconciliation refuses ----------


@pytest.mark.parametrize(
    "status",
    [
        BatchStatus.PLANNED,
        BatchStatus.ACTIVE,
        BatchStatus.READY_TO_MERGE,
        BatchStatus.MERGED_PENDING_RECONCILIATION,
        BatchStatus.COMPLETED,
        BatchStatus.CANCELLED,
    ],
)
def test_a_batch_that_is_not_merging_is_refused(status: BatchStatus) -> None:
    """There is no merge in flight to reconcile from anywhere else."""
    head = None if status is BatchStatus.PLANNED else SECOND_HEAD
    branch = None if status is BatchStatus.PLANNED else BRANCH
    with pytest.raises(ReconciliationRefusedError) as caught:
        reconcile_merge(batch(status=status, head_sha=head, branch=branch), observation())

    assert caught.value.code is ReconciliationRefusal.BATCH_NOT_MERGING


def test_an_observation_of_another_batch_is_refused() -> None:
    """A read-back of somebody else's branch says nothing about this Batch."""
    with pytest.raises(ReconciliationRefusedError) as caught:
        reconcile_merge(batch(), observation(urn=OTHER_BATCH))

    assert caught.value.code is ReconciliationRefusal.OBSERVATION_MISBOUND


def test_an_observation_of_another_branch_is_refused() -> None:
    """A Batch merges into exactly one branch, and that is the one read."""
    with pytest.raises(ReconciliationRefusedError) as caught:
        reconcile_merge(batch(), observation(branch=OTHER_BRANCH))

    assert caught.value.code is ReconciliationRefusal.OBSERVATION_MISBOUND


def test_a_merging_batch_with_no_pinned_head_does_not_validate() -> None:
    """The head binding is required by the record, so the check is the type."""
    with pytest.raises(ValidationError, match="requires current_head_binding"):
        DeliveryBatch.model_validate(batch_row(head_sha=None))


# ---------- the observation's own boundaries ----------


def test_an_unread_branch_cannot_report_commits() -> None:
    """Commits read off a branch nobody read are commits from nowhere."""
    with pytest.raises(ValidationError, match="could not be read"):
        HostMergeObservation.model_validate(
            {
                "batch_ref": BATCH,
                "target_branch": BRANCH,
                "observed_at": AT,
                "target_head_sha": None,
                "contained_shas": (SECOND_HEAD,),
            }
        )


def test_the_observed_head_must_be_among_the_commits_read_back() -> None:
    """A branch not carrying its own head is a read that went wrong."""
    with pytest.raises(ValidationError, match="not among the commits"):
        observation(target_head_sha=THIRD_HEAD, contained=(SECOND_HEAD,))


def test_the_observation_refuses_a_repeated_commit() -> None:
    """A commit listed twice is a log that was pasted, not a set that was read."""
    with pytest.raises(ValidationError, match="the same commit twice"):
        observation(target_head_sha=THIRD_HEAD, contained=(THIRD_HEAD, THIRD_HEAD))


def test_the_observation_admits_exactly_the_maximum_commits() -> None:
    """The upper bound is admitted, not one short of it."""
    filler = tuple(f"{index:040x}" for index in range(MAX_OBSERVED_COMMITS - 1))
    read = observation(target_head_sha=THIRD_HEAD, contained=(THIRD_HEAD, *filler))

    assert len(read.contained_shas) == MAX_OBSERVED_COMMITS


def test_the_observation_refuses_one_commit_past_the_maximum() -> None:
    """Past the bound it is a pasted history rather than an answer."""
    filler = tuple(f"{index:040x}" for index in range(MAX_OBSERVED_COMMITS))
    with pytest.raises(ValidationError, match=r"at most|too_long"):
        observation(target_head_sha=THIRD_HEAD, contained=(THIRD_HEAD, *filler))


def test_the_observation_refuses_an_unknown_key() -> None:
    """Strict: a misspelled field is a rejection, not a silently dropped fact."""
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        HostMergeObservation.model_validate(
            {
                "batch_ref": BATCH,
                "target_branch": BRANCH,
                "observed_at": AT,
                "target_head": THIRD_HEAD,
            }
        )


def test_the_observation_is_frozen() -> None:
    """An observation edited afterwards is not an observation."""
    read = observation()
    with pytest.raises(ValidationError):
        read.target_head_sha = SECOND_HEAD  # type: ignore[misc]


# ---------- the verb files the decision on the Batch ledger ----------


def seeded(tmp_path: Path, row: dict[str, Any]) -> Epoch2RootContext:
    """Return a canary tree holding one DeliveryBatch."""
    context = native_tree(tmp_path / "repo", tmp_path / "runtime")
    with context.session([row["urn"]]) as session:
        path = session.document_path
    document = read_document(path)
    document.setdefault(Epoch2Collection.BATCH.value, {})[row["key"]] = row
    write_document(path, document)
    return context


def params(read: HostMergeObservation) -> MergeReconcileParams:
    """Return the validated request one reconciliation pass runs on."""
    return MergeReconcileParams.model_validate(
        {
            "urn": BATCH,
            "actor": "OP-0001",
            "idempotency_key": "req-reconcile-0001",
            "observation": read.model_dump(mode="json"),
        }
    )


def lines(context: Epoch2RootContext, key: str) -> list[dict[str, Any]]:
    """Return every Batch-ledger line filed under *key*."""
    with context.session([BATCH]) as session:
        path = session.ledger_path(Epoch2Collection.BATCH)
    return [item.payload for item in read_ledger_records(path) if item.record_key == key]


def test_the_verb_files_a_refusal_and_answers_with_the_return_path(tmp_path: Path) -> None:
    """The daemon half reports the same decision the workflow reached."""
    context = seeded(tmp_path, batch_row())

    answer = reconcile_batch_merge(context, params(observation(refusal=cause())))

    assert answer.outcome == MergeOutcome.REFUSED.value
    assert answer.to_status == BatchStatus.ACTIVE.value
    assert answer.path == ("merge_refused", "reopened")
    assert answer.cause_code == "checks-failed"
    assert answer.resolved
    assert lines(context, answer.record_key)


def test_the_verb_leaves_an_unresolved_batch_merging(tmp_path: Path) -> None:
    """The line records the open question; the Batch has not moved."""
    context = seeded(tmp_path, batch_row())

    answer = reconcile_batch_merge(context, params(observation()))

    assert answer.outcome == MergeOutcome.UNKNOWN.value
    assert answer.to_status == BatchStatus.MERGING.value
    assert answer.ambiguity == AmbiguityLabel.MERGING.value
    assert answer.awaited_head_sha == SECOND_HEAD
    assert not answer.resolved


def test_the_verb_replaying_one_observation_keeps_one_standing_line(
    tmp_path: Path,
) -> None:
    """Idempotent: the derived key means a repeat is the same line, not a second."""
    context = seeded(tmp_path, batch_row())

    first = reconcile_batch_merge(context, params(observation()))
    second = reconcile_batch_merge(context, params(observation()))

    assert first == second
    with context.session([BATCH]) as session:
        path = session.ledger_path(Epoch2Collection.BATCH)
    keys = {item.record_key for item in read_ledger_records(path)}
    assert keys == {first.record_key}


def test_the_verb_refuses_a_batch_the_tree_does_not_hold(tmp_path: Path) -> None:
    """A Batch nobody filed is not one a read-back can say anything about."""
    context = native_tree(tmp_path / "repo", tmp_path / "runtime")

    with pytest.raises(DaemonValidationError, match="reconcile_observation_misbound"):
        reconcile_batch_merge(context, params(observation()))


def test_the_verb_surfaces_the_workflow_refusal_as_a_typed_code(tmp_path: Path) -> None:
    """A refusal keeps its code on the way out of the handler."""
    context = seeded(tmp_path, batch_row(status=BatchStatus.ACTIVE))

    with pytest.raises(DaemonValidationError, match="reconcile_batch_not_merging"):
        reconcile_batch_merge(context, params(observation()))


def test_the_verb_refuses_a_request_that_does_not_parse() -> None:
    """The request model is strict, and an unknown key is a rejection."""
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        MergeReconcileParams.model_validate(
            {
                "urn": BATCH,
                "actor": "OP-0001",
                "idempotency_key": "req-reconcile-0001",
                "observation": observation().model_dump(mode="json"),
                "force": True,
            }
        )


def test_the_recorded_stamp_is_the_observation_not_the_wall_clock(tmp_path: Path) -> None:
    """The line is dated when the branch was read, which is the fact it holds."""
    context = seeded(tmp_path, batch_row())

    answer = reconcile_batch_merge(context, params(observation()))

    filed = lines(context, answer.record_key)
    assert filed
    assert filed[-1]["observed_at"] == AT.astimezone(UTC).isoformat().replace("+00:00", "Z")


def test_the_reconciliation_payload_round_trips_through_the_ledger(tmp_path: Path) -> None:
    """What the line holds is the decision, readable back without the request."""
    context = seeded(tmp_path, batch_row())

    answer = reconcile_batch_merge(context, params(observation(refusal=cause())))

    filed = lines(context, answer.record_key)[-1]
    assert filed["outcome"] == MergeOutcome.REFUSED.value
    assert filed["cause"]["code"] == "checks-failed"
    assert filed["observed_facts"] == [ObservedFact.HOST_MERGE_REFUSED.value]


def test_a_landed_merge_is_filed_as_completed(tmp_path: Path) -> None:
    """The one path that completes a Batch is the one the branch proves."""
    context = seeded(tmp_path, batch_row())

    answer = reconcile_batch_merge(
        context, params(observation(contained=(THIRD_HEAD, SECOND_HEAD)))
    )

    assert answer.outcome == MergeOutcome.LANDED.value
    assert answer.to_status == BatchStatus.COMPLETED.value
    assert answer.matched_head_sha == SECOND_HEAD
    assert answer.ambiguity is None


def test_the_decision_never_reports_a_datetime_it_invented(tmp_path: Path) -> None:
    """Every stamp on the answer comes from the observation that was presented."""
    context = seeded(tmp_path, batch_row())
    read = observation()

    answer = reconcile_batch_merge(context, params(read))

    filed = lines(context, answer.record_key)[-1]
    assert datetime.fromisoformat(filed["observed_at"]) == read.observed_at
