"""Delivery requests are assembled from records, and a dangling record refuses.

The integrate verb takes a request whose every field used to be the
caller's to fill in. The assembler derives them instead: from the Batch
record, the Tasks its plan lists, the Run behind each sealed candidate,
and the typed references it resolves against the same tree. This suite
builds the request from one resolved world, sends it to the verb to show
the verb accepts what was assembled, and then removes one fact at a time
-- a planned Task, a Run, an exit, the evidence, the base -- to show each
refuses before anything is written.

Nothing sleeps, polls or reaches outside ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.delivery.integration import ConflictExitKind
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import integrate_delivery
from eawf.runtime.daemon.methods.delivery_assembly import (
    DELIVERY_ASSEMBLE_METHOD,
    DeliveryAssembleParams,
    assemble_delivery,
)
from eawf.workflow.delivery.request_assembly import (
    AssemblyRefusal,
    AssemblyRefusedError,
    BatchPlan,
    DeliveryReferences,
    assemble_integrate_request,
    resolve_batch_plan,
)
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

ACTOR: Final = "SKILL-INTEGRATE"
REPAIR_TASK: Final = f"{world.CONTAINER}/task/EAWF-0050"
EVIDENCE: Final = f"{world.CONTAINER}/evidence/EVD-0001"
PENDING_ACTION: Final = f"{world.CONTAINER}/pending-action/ACT-0001"
BRANCH: Final = "feature/canary-delivery"
LONG_INTENT: Final = " ".join(["Publish"] * 20) + "."


def _head(urn: str) -> dict[str, Any]:
    """Return the identity head every stored record carries."""
    return {
        "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, urn)),
        "key": urn.rsplit("/", 1)[1],
        "urn": urn,
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": world.AT.isoformat(),
        "updated_at": world.AT.isoformat(),
    }


def batch_row(
    *, task_refs: tuple[str, ...] = (world.TASK,), target_branch: str | None = BRANCH
) -> dict[str, Any]:
    """Return the stored payload of the Batch under test."""
    row: dict[str, Any] = {
        **_head(world.BATCH),
        "milestone_ref": world.DUE_SCOPE,
        "repository_ref": world.REPOSITORY,
        "task_refs": list(task_refs),
        "status": "ACTIVE" if target_branch is not None else "PLANNED",
    }
    if target_branch is not None:
        row["target_branch"] = target_branch
    return row


def run_row(*, task_ref: str = world.TASK) -> dict[str, Any]:
    """Return the stored payload of the Run that sealed the candidate."""
    return {
        **_head(world.RUN),
        "scope": {
            "scope_kind": "task",
            "task_ref": task_ref,
            "purpose": "implement",
            "write_set": ["src/eawf/sample.py"],
        },
        "status": "COMPLETED",
        "started_at": world.AT.isoformat(),
        "ended_at": world.AT.isoformat(),
    }


def repair_task_row() -> dict[str, Any]:
    """Return a backlog Task a repair exit may land on."""
    return {
        **_head(REPAIR_TASK),
        "priority": "P1",
        "intent": "Repair a conflicting delivery",
        "contract_revision": 1,
        "status": "DRAFT",
    }


def seed_row(context: Epoch2RootContext, collection: Epoch2Collection, row: dict[str, Any]) -> None:
    """Place one record payload into the canary's selected generation document."""
    with context.session([row["urn"]]) as session:
        path = session.document_path
    document = read_document(path)
    document.setdefault(collection.value, {})[row["key"]] = row
    write_document(path, document)


def evidence_line() -> LedgerRecord:
    """Return the evidence-ledger line the diagnostic reference resolves to."""
    return LedgerRecord(
        collection=Epoch2Collection.EVIDENCE,
        record_key="EVD-0001",
        status="recorded",
        recorded_at=world.AT,
        payload=_head(EVIDENCE),
    )


def seeded(
    tmp_path: Path,
    *,
    batch: dict[str, Any] | None = None,
    task: dict[str, Any] | None = None,
    run: dict[str, Any] | None = None,
    with_run: bool = True,
    seal: bool = True,
    evidence: bool = True,
) -> Epoch2RootContext:
    """Return a canary tree holding one resolvable Batch plan, minus what is switched off."""
    context = world.native_tree(tmp_path / "repo", tmp_path / "runtime")
    seed_row(context, Epoch2Collection.BATCH, batch_row() if batch is None else batch)
    world.seed_task(context, world.task_row(jury=("CR-02",)) if task is None else task)
    world.seed_task(context, repair_task_row())
    if with_run:
        seed_row(context, Epoch2Collection.RUN, run_row() if run is None else run)
    if seal:
        world.seed_lines(context, world.TASK, [world.bundle_line(world.bundle_row())])
    if evidence:
        world.seed_lines(context, EVIDENCE, [evidence_line()])
    return context


def references(**overrides: Any) -> DeliveryReferences:
    """Return the typed references the requests carry."""
    fields: dict[str, Any] = {
        "batch_ref": world.BATCH,
        "base": world.BASE,
        "exit_refs": {ConflictExitKind.REPAIR_TASK: REPAIR_TASK},
        "diagnostic_ref": EVIDENCE,
    }
    return DeliveryReferences.model_validate(fields | overrides)


def snapshot(context: Epoch2RootContext) -> dict[str, str]:
    """Return a digest of every non-lock file under the native root."""
    root = context.identity.tree_root
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not any("lock" in part for part in path.relative_to(root).parts)
    }


def plan(tmp_path: Path) -> BatchPlan:
    """Return the resolved plan of the fully seeded world."""
    return resolve_batch_plan(seeded(tmp_path), references())


def refused(context: Epoch2RootContext, code: AssemblyRefusal, **overrides: Any) -> None:
    """Assert resolving the plan refuses with *code* and writes nothing."""
    before = snapshot(context)
    assert before, "the snapshot must cover the tree it guards"
    with pytest.raises(AssemblyRefusedError) as caught:
        resolve_batch_plan(context, references(**overrides))
    assert caught.value.code is code
    assert snapshot(context) == before


def test_resolve_batch_plan_resolves_batch_tasks_runs_and_candidates(tmp_path: Path) -> None:
    """The positive control: every record the plan names is read back typed."""
    resolved = plan(tmp_path)
    assert resolved.batch.key == "BAT-0007"
    assert [task.key for task in resolved.tasks] == ["EAWF-0042"]
    assert [item.candidate_ref for item in resolved.bundles] == [world.bundle().candidate_ref]
    assert resolved.runs[world.bundle().candidate_ref].key == "RUN-00000010"


def test_assemble_integrate_request_derives_every_field_from_records(tmp_path: Path) -> None:
    """Branch, subjects and affected criteria come from the Batch and its Task."""
    resolved = plan(tmp_path)
    request = assemble_integrate_request(resolved, actor=ACTOR)
    assert str(request.urn) == world.BATCH
    assert request.branch == BRANCH
    assert request.subjects == {world.bundle().candidate_ref: "Publish the wheel to the index"}
    assert request.subject == "Deliver BAT-0007 (1 task(s))"
    assert request.affected_criterion_ids == ("CR-01", "CR-02")
    assert {kind: str(ref) for kind, ref in request.exit_refs.items()} == {
        ConflictExitKind.REPAIR_TASK: REPAIR_TASK
    }
    assert str(request.diagnostic_ref) == EVIDENCE
    assert request.base == world.BASE


def test_assemble_integrate_request_names_one_request_per_plan(tmp_path: Path) -> None:
    """Assembling the same plan twice yields the same idempotency key."""
    resolved = plan(tmp_path)
    first = assemble_integrate_request(resolved, actor=ACTOR)
    assert (
        first.idempotency_key == assemble_integrate_request(resolved, actor=ACTOR).idempotency_key
    )
    assert first.idempotency_key.startswith("integrate-")
    assert len(first.idempotency_key) <= 128


def test_assembled_integrate_request_clears_the_verb_up_to_its_workspace(
    tmp_path: Path,
) -> None:
    """The verb accepts the assembled request and stops only at the unset workspace."""
    context = seeded(tmp_path)
    request = assemble_integrate_request(resolve_batch_plan(context, references()), actor=ACTOR)
    with pytest.raises(DaemonValidationError, match="integration_workspace_absent"):
        integrate_delivery(context, request, workspace=None, now=world.AT)


def test_resolve_batch_plan_refuses_a_dangling_task_reference(tmp_path: Path) -> None:
    """Gate-fire proof: a plan listing a Task no record holds refuses with no write."""
    context = seeded(tmp_path, batch=batch_row(task_refs=(world.TASK, world.OTHER_TASK)))
    refused(context, AssemblyRefusal.TASK_REFERENCE_UNRESOLVED)


def test_resolve_batch_plan_refuses_an_absent_batch(tmp_path: Path) -> None:
    """A Batch no record holds has no plan to read."""
    context = seeded(tmp_path)
    refused(
        context,
        AssemblyRefusal.BATCH_ABSENT,
        batch_ref=world.OTHER_BATCH,
        base=world.binding(generation=1, head_sha=world.BASE_COMMIT, batch_ref=world.OTHER_BATCH),
    )


def test_resolve_batch_plan_refuses_a_task_placed_elsewhere(tmp_path: Path) -> None:
    """A planned Task whose record sits in another Batch is not this Batch's."""
    context = seeded(tmp_path, task=world.task_row(batch_ref=world.OTHER_BATCH))
    refused(context, AssemblyRefusal.TASK_PLACEMENT_MISMATCH)


def test_resolve_batch_plan_refuses_a_placed_task_the_plan_omits(tmp_path: Path) -> None:
    """A Task placed in the Batch but missing from its plan would ship unsubjected."""
    context = seeded(tmp_path)
    world.seed_task(context, world.task_row(urn=world.OTHER_TASK))
    refused(context, AssemblyRefusal.TASK_PLACEMENT_MISMATCH)


def test_resolve_batch_plan_refuses_a_candidate_whose_run_is_absent(tmp_path: Path) -> None:
    """A candidate naming a Run no record holds has no producer on record."""
    context = seeded(tmp_path, with_run=False)
    refused(context, AssemblyRefusal.RUN_REFERENCE_UNRESOLVED)


def test_resolve_batch_plan_refuses_a_run_scoped_to_another_task(tmp_path: Path) -> None:
    """A Run scoped to another Task cannot have produced this Task's candidate."""
    context = seeded(tmp_path, run=run_row(task_ref=world.OTHER_TASK))
    refused(context, AssemblyRefusal.RUN_REFERENCE_UNRESOLVED)


def test_resolve_batch_plan_refuses_an_exit_no_record_holds(tmp_path: Path) -> None:
    """An operator-decision exit naming a PendingAction nobody queued is refused."""
    context = seeded(tmp_path)
    refused(
        context,
        AssemblyRefusal.EXIT_REFERENCE_UNRESOLVED,
        exit_refs={ConflictExitKind.OPERATOR_DECISION: PENDING_ACTION},
    )


def test_resolve_batch_plan_refuses_an_exit_of_the_wrong_kind(tmp_path: Path) -> None:
    """A repair exit must land on a Task, so an evidence reference is refused."""
    context = seeded(tmp_path)
    refused(
        context,
        AssemblyRefusal.EXIT_REFERENCE_UNRESOLVED,
        exit_refs={ConflictExitKind.REPAIR_TASK: EVIDENCE},
    )


def test_resolve_batch_plan_refuses_unrecorded_diagnostic_evidence(tmp_path: Path) -> None:
    """The diagnostic reference must name evidence the tree holds."""
    context = seeded(tmp_path, evidence=False)
    refused(context, AssemblyRefusal.DIAGNOSTIC_REFERENCE_UNRESOLVED)


def test_resolve_batch_plan_refuses_a_base_the_candidates_did_not_start_from(
    tmp_path: Path,
) -> None:
    """A base on another commit than the sealed candidates is unbound."""
    context = seeded(tmp_path)
    refused(
        context,
        AssemblyRefusal.BASE_UNBOUND,
        base=world.binding(generation=1, head_sha=world.LOSING_HEAD),
    )


def test_resolve_batch_plan_refuses_a_base_of_another_batch(tmp_path: Path) -> None:
    """A base binding addressing another Batch does not bind this one."""
    context = seeded(tmp_path)
    refused(
        context,
        AssemblyRefusal.BASE_UNBOUND,
        base=world.binding(generation=1, head_sha=world.BASE_COMMIT, batch_ref=world.OTHER_BATCH),
    )


def test_assemble_integrate_request_refuses_an_untargeted_batch(tmp_path: Path) -> None:
    """A PLANNED Batch has no branch a conflict could be seen on."""
    context = seeded(tmp_path, batch=batch_row(target_branch=None))
    with pytest.raises(AssemblyRefusedError) as caught:
        assemble_integrate_request(resolve_batch_plan(context, references()), actor=ACTOR)
    assert caught.value.code is AssemblyRefusal.BATCH_UNTARGETED


def test_assemble_integrate_request_refuses_an_empty_plan(tmp_path: Path) -> None:
    """Boundary: a Batch planning no Task has nothing to deliver."""
    context = seeded(tmp_path, batch=batch_row(task_refs=()), task=world.task_row(batch_ref=None))
    resolved = resolve_batch_plan(context, references())
    assert resolved.tasks == ()
    with pytest.raises(AssemblyRefusedError) as caught:
        assemble_integrate_request(resolved, actor=ACTOR)
    assert caught.value.code is AssemblyRefusal.CANDIDATES_ABSENT


def test_assemble_integrate_request_fits_a_long_intent_to_one_subject(tmp_path: Path) -> None:
    """Boundary: an intent past the subject width is cut on a word, with no period."""
    row = world.task_row()
    row["intent"] = LONG_INTENT
    request = assemble_integrate_request(
        resolve_batch_plan(seeded(tmp_path, task=row), references()), actor=ACTOR
    )
    subject = request.subjects[world.bundle().candidate_ref]
    assert len(subject) <= 72
    assert subject.startswith("Publish")
    assert not subject.endswith((".", " "))


def test_delivery_references_reject_an_empty_exit_map() -> None:
    """Error path: a request with no exit at all is a schema failure."""
    with pytest.raises(ValueError, match="exit_refs"):
        references(exit_refs={})


def assemble_params(**overrides: Any) -> DeliveryAssembleParams:
    """Return the assembly verb's request over the fully seeded world."""
    fields: dict[str, Any] = {
        "urn": world.BATCH,
        "actor": ACTOR,
        "base": world.BASE.model_dump(mode="json"),
        "exit_refs": {"repair_task": REPAIR_TASK},
        "diagnostic_ref": EVIDENCE,
    }
    return DeliveryAssembleParams.model_validate(fields | overrides)


def test_assemble_verb_answers_the_request_the_builder_assembles(tmp_path: Path) -> None:
    """The verb's answer is the assembled request, so a skill can send it verbatim."""
    context = seeded(tmp_path)
    expected = assemble_integrate_request(resolve_batch_plan(context, references()), actor=ACTOR)
    before = snapshot(context)

    answer = assemble_delivery(context, assemble_params())

    assert answer == expected.model_dump(mode="json")
    assert snapshot(context) == before


def test_assemble_verb_is_registered_behind_the_native_fence(tmp_path: Path) -> None:
    """The verb a skill reaches is registered, and an epoch-one tree is refused."""
    plain = tmp_path / "plain"
    (plain / ".ea").mkdir(parents=True)
    ctx = MethodContext(
        started_at=world.AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=tmp_path / "wal",
    )
    with pytest.raises(DaemonValidationError, match="native_authority_required"):
        asyncio.run(methods.dispatch(DELIVERY_ASSEMBLE_METHOD, ctx, {"repo_root": str(plain)}))


def test_assemble_verb_refuses_a_dangling_plan_with_its_code(tmp_path: Path) -> None:
    """Gate-fire: a diagnostic nobody filed refuses with the assembly code, writing nothing."""
    context = seeded(tmp_path, evidence=False)
    before = snapshot(context)

    with pytest.raises(DaemonValidationError, match="diagnostic_reference_unresolved"):
        assemble_delivery(context, assemble_params())

    assert snapshot(context) == before


def test_assemble_params_refuse_an_empty_exit_map() -> None:
    """Error path: a request naming no exit is a schema failure at the boundary."""
    with pytest.raises(ValueError, match="exit_refs"):
        assemble_params(exit_refs={})
