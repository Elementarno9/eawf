"""The Batch, the Task and the proofs both completion suites are driven on.

One shape is shared: a Batch whose base is generation one, a generation
two that delivers the Task under test, and a generation three that
delivers somebody else and names exactly one of the Task's criteria as
invalidated. Everything the two suites disagree about is a keyword on
these builders, so a rerun case and a refusal case are the same world
with one fact moved rather than two worlds that drifted apart.

Nothing here reads a clock, opens a socket or writes outside ``tmp_path``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from eawf.kernel.delivery.integration import (
    IntegrationGeneration,
    IntegrationGenerationLedger,
)
from eawf.kernel.delivery.receipts import (
    ProofFreshnessKey,
    ProofReceipt,
    RevisionBinding,
    RevisionRefKind,
    canonical_digest,
)
from eawf.kernel.runtime.candidate import CandidateBundle, SealCheck, candidate_identity
from eawf.kernel.spec.common import CriterionEvidenceKind, CriterionSpec, GateSpec, QualityDimension
from eawf.kernel.state.enums import AgentReportVerdict, GateReceiptResult
from eawf.kernel.state.epoch2.task import Task, TaskStatus
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import canary_ref, provision_canary
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.verification.receipts import (
    ProofRuntimeFacts,
    VerificationLeg,
    build_verification_leg,
)
from eawf.workflow.delivery.criteria import (
    CriteriaAuthoring,
    ExecutionContract,
    ExecutionContractSet,
    compile_execution_contracts,
)

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
REPOSITORY: Final = f"{CONTAINER}/repository/REP-EAWF"
BATCH: Final = f"{CONTAINER}/batch/BAT-0007"
OTHER_BATCH: Final = f"{CONTAINER}/batch/BAT-0008"
TASK: Final = f"{CONTAINER}/task/EAWF-0042"
OTHER_TASK: Final = f"{CONTAINER}/task/EAWF-0043"
DUE_SCOPE: Final = f"{CONTAINER}/milestone/MLS-0030"
RUN: Final = f"{CONTAINER}/run/RUN-00000010"

AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
BASE_COMMIT: Final = "9f" * 20
SECOND_HEAD: Final = "1a" * 20
THIRD_HEAD: Final = "2b" * 20
LOSING_HEAD: Final = "4d" * 20
TREE: Final = "3c" * 20
DIGEST: Final = canonical_digest("delivery")

#: The gate each criterion is proved by; the suites speak criterion ids.
GATE_OF: Final[dict[str, str]] = {"CR-01": "G-01", "CR-02": "G-02"}


def criterion(
    criterion_id: str, *, evidence_kind: CriterionEvidenceKind, gated: bool = True
) -> CriterionSpec:
    """Return one authored criterion, bound to its gate unless *gated* is off."""
    return CriterionSpec(
        id=criterion_id,
        text=f"the delivery holds for case {criterion_id}",
        kind="behavioral",
        acceptance_style="binary",
        evidence_kind=evidence_kind,
        gate_ids=[GATE_OF[criterion_id]] if gated else [],
        quality_dimension=QualityDimension.RELIABILITY,
        measurable_signal=f"pytest over the {criterion_id} suite exits zero",
    )


def gate(criterion_id: str) -> GateSpec:
    """Return the gate that proves *criterion_id*."""
    return GateSpec(
        id=GATE_OF[criterion_id],
        criterion_id=criterion_id,
        kind="command_exit_zero",
        args={"argv": ["uv", "run", "pytest", f"tests/unit/sample/test_{criterion_id}.py"]},
        policy="block",
        cadence="every-wave",
    )


def criteria(
    *criterion_ids: str, jury: Sequence[str] = (), attested: Sequence[str] = ()
) -> tuple[CriterionSpec, ...]:
    """Return the authored criteria.

    Args:
        criterion_ids: The criteria the scope carries.
        jury: Which of them a jury settles rather than a runner, so no
            deterministic decision can reuse or rerun them.
        attested: Which of them reference no gate at all, so nothing
            compiles a contract for them.
    """
    return tuple(
        criterion(
            criterion_id,
            evidence_kind=(
                "attested"
                if criterion_id in attested
                else "jury"
                if criterion_id in jury
                else "deterministic"
            ),
            gated=criterion_id not in attested,
        )
        for criterion_id in criterion_ids
    )


def compiled(
    *criterion_ids: str, jury: Sequence[str] = (), attested: Sequence[str] = ()
) -> ExecutionContractSet:
    """Return the compiled contracts of *criterion_ids*."""
    authored = criteria(*criterion_ids, jury=jury, attested=attested)
    return compile_execution_contracts(
        CriteriaAuthoring(
            scope_id="EAWF-0042",
            criteria=authored,
            gates=tuple(gate(item.id) for item in authored if item.gate_ids),
        )
    )


def task(
    *,
    criterion_ids: Sequence[str] = ("CR-01", "CR-02"),
    jury: Sequence[str] = (),
    attested: Sequence[str] = (),
    urn: str = TASK,
    batch_ref: str | None = BATCH,
    status: TaskStatus = TaskStatus.READY_TO_INTEGRATE,
) -> Task:
    """Return one Task ready to be judged for completion."""
    return Task.model_validate(
        task_row(
            criterion_ids=criterion_ids,
            jury=jury,
            attested=attested,
            urn=urn,
            batch_ref=batch_ref,
            status=status,
        )
    )


def task_row(
    *,
    criterion_ids: Sequence[str] = ("CR-01", "CR-02"),
    jury: Sequence[str] = (),
    attested: Sequence[str] = (),
    urn: str = TASK,
    batch_ref: str | None = BATCH,
    status: TaskStatus = TaskStatus.READY_TO_INTEGRATE,
) -> dict[str, Any]:
    """Return the stored payload of :func:`task`, ready to seed a document."""
    key = urn.rsplit("/", 1)[1]
    row: dict[str, Any] = {
        "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, urn)),
        "key": key,
        "urn": urn,
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
        "priority": "P1",
        "intent": "Publish the wheel to the index",
        "contract_revision": 1,
        "status": status.value,
    }
    if batch_ref is not None:
        row["batch_ref"] = batch_ref
        row["due_scope"] = DUE_SCOPE
        row["criteria"] = [
            item.model_dump(mode="json")
            for item in criteria(*criterion_ids, jury=jury, attested=attested)
        ]
    return row


def bundle(*, task_ref: str = TASK, tree: str = "a") -> CandidateBundle:
    """Return one sealed candidate for *task_ref*."""
    resulting = f"sha256:{tree * 64}"
    return CandidateBundle.model_validate(
        {
            "candidate_ref": candidate_identity(task_ref=task_ref, resulting_tree_digest=resulting),
            "run_ref": RUN,
            "task_ref": task_ref,
            "submission_ref": "artifact://candidate/executor-success",
            "report_digest": f"sha256:{'b' * 64}",
            "verdict": AgentReportVerdict.PASS.value,
            "changed_paths": ["src/eawf/sample.py"],
            "resulting_tree_digest": resulting,
            "base_commit": BASE_COMMIT,
            "workspace_generation": 1,
            "checks_passed": [item.value for item in SealCheck],
            "sealed_at": AT.isoformat(),
        }
    )


def bundle_row(*, task_ref: str = TASK, tree: str = "a") -> dict[str, Any]:
    """Return the run-ledger payload of :func:`bundle`, discriminator included."""
    return bundle(task_ref=task_ref, tree=tree).model_dump(mode="json")


def binding(
    *,
    generation: int,
    head_sha: str,
    affected: Sequence[str] = (),
    batch_ref: str = BATCH,
    ref_kind: RevisionRefKind = RevisionRefKind.INTEGRATION,
) -> RevisionBinding:
    """Return the revision one ordinal of the Batch is bound at.

    ``criteria_digest`` is the digest of *affected*, which is exactly what
    the integration writes: the criterion component of a generation's key
    is the set that generation invalidated.
    """
    return RevisionBinding(
        repository_ref=REPOSITORY,
        ref_kind=ref_kind,
        head_sha=head_sha,
        tree_sha=TREE,
        parent_sha=None,
        batch_ref=batch_ref,
        integration_generation=generation,
        manifest_digest=DIGEST,
        criteria_digest=canonical_digest(list(affected)),
        policy_digest=DIGEST,
        environment_digest=None,
        bound_at=AT,
    )


BASE: Final = binding(generation=1, head_sha=BASE_COMMIT)


def generation(
    *,
    ordinal: int,
    head_sha: str,
    target_base: RevisionBinding,
    tasks: Sequence[str],
    affected: Sequence[str] = (),
    selected: bool = False,
    integrated: RevisionBinding | None = None,
) -> IntegrationGeneration:
    """Return one selected result of integrating work onto the Batch."""
    return IntegrationGeneration(
        id=f"ING-{ordinal:06d}",
        batch_ref=BATCH,
        generation=ordinal,
        parent_generation_id=(
            None
            if target_base.integration_generation == 1
            else f"ING-{target_base.integration_generation:06d}"
        ),
        source_candidate_bundle_id="CB-00000001",
        source_base=BASE,
        target_base=target_base,
        integrated_revision=(
            binding(generation=ordinal, head_sha=head_sha, affected=affected)
            if integrated is None
            else integrated
        ),
        patch_digest=DIGEST,
        diff_digest=DIGEST,
        tree_digest=DIGEST,
        changed_paths=("src/eawf/sample.py",),
        affected_task_refs=tuple(tasks),
        affected_criterion_ids=tuple(affected),
        integration_policy_digest=DIGEST,
        selected=selected,
        created_at=AT,
    )


def delivering_generation(**overrides: Any) -> IntegrationGeneration:
    """Return generation two, the one that carried the Task under test."""
    return generation(
        ordinal=2,
        head_sha=SECOND_HEAD,
        target_base=BASE,
        tasks=(TASK,),
        affected=("CR-01", "CR-02"),
        **overrides,
    )


def following_generation(
    *, affected: Sequence[str] = ("CR-01",), **overrides: Any
) -> IntegrationGeneration:
    """Return generation three, which delivers another Task onto the head."""
    return generation(
        ordinal=3,
        head_sha=THIRD_HEAD,
        target_base=delivering_generation().integrated_revision,
        tasks=(OTHER_TASK,),
        affected=affected,
        **overrides,
    )


def ledger(*generations: IntegrationGeneration) -> IntegrationGenerationLedger:
    """Return the Batch's history with the newest generation selected."""
    ordered = [
        item.model_copy(update={"selected": index == len(generations) - 1})
        for index, item in enumerate(generations)
    ]
    return IntegrationGenerationLedger(batch_ref=BATCH, generations=tuple(ordered))


def facts() -> ProofRuntimeFacts:
    """Return the runner, environment, selector and policy of every proof."""
    return ProofRuntimeFacts(
        selector_digest=canonical_digest("selector"),
        policy_digest=canonical_digest("policy"),
        runner_digest=canonical_digest("runner"),
        environment_digest=canonical_digest("environment"),
    )


def leg(contract: ExecutionContract, *, revision_binding: RevisionBinding) -> VerificationLeg:
    """Return the leg *contract* must be proved at on *revision_binding*."""
    return build_verification_leg(contract, revision_binding=revision_binding, facts=facts())


def receipt(
    contract: ExecutionContract,
    *,
    revision_binding: RevisionBinding,
    receipt_id: str = "RCP-0001",
    result: GateReceiptResult = GateReceiptResult.PASS,
    key: ProofFreshnessKey | None = None,
) -> ProofReceipt:
    """Return one proof receipt for *contract*, taken at *revision_binding*."""
    expected = leg(contract, revision_binding=revision_binding).expected if key is None else key
    return ProofReceipt(
        id=receipt_id,
        scope_id=contract.scope_id,
        gate_id=contract.gate_id,
        criterion_ids=contract.criterion_ids,
        evidence_kind=contract.evidence_kind,
        freshness=expected,
        freshness_key=expected.digest(),
        result=result,
        exit_status=0 if result is GateReceiptResult.PASS else 1,
        started_at=AT,
        ended_at=AT,
    )


def receipts_at(
    contracts: ExecutionContractSet, *, revision_binding: RevisionBinding
) -> tuple[ProofReceipt, ...]:
    """Return one passing receipt per compiled contract, all at one revision."""
    return tuple(
        receipt(
            item,
            revision_binding=revision_binding,
            receipt_id=f"RCP-{index:04d}",
        )
        for index, item in enumerate(contracts.contracts, start=1)
    )


def native_tree(repo_root: Path, runtime_root: Path) -> Epoch2RootContext:
    """Return the native context of a disposable epoch-2 canary at *repo_root*."""
    provisioned = provision_canary(repo_root=repo_root, ref=canary_ref("CMP"), provisioned_at=AT)
    context = MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=runtime_root / "wal",
    )
    return context.native_root_context(provisioned.root / ".ea")


def seed_task(context: Epoch2RootContext, row: dict[str, Any]) -> None:
    """Place one Task payload into the canary's selected generation document."""
    with context.session([row["urn"]]) as session:
        path = session.document_path
    document = read_document(path)
    document.setdefault(Epoch2Collection.TASK.value, {})[row["key"]] = row
    write_document(path, document)


def seed_lines(context: Epoch2RootContext, urn: str, lines: Sequence[LedgerRecord]) -> None:
    """Append *lines* to the ledgers of the collections they declare."""
    with context.session([urn]) as session:
        for line in lines:
            append_ledger_record(session.ledger_path(line.collection), line)


def bundle_line(row: dict[str, Any]) -> LedgerRecord:
    """Return the run-ledger line one sealed bundle is filed as."""
    return LedgerRecord(
        collection=Epoch2Collection.RUN,
        record_key=row["candidate_ref"],
        status="sealed",
        recorded_at=AT,
        payload=row,
    )


def generation_line(item: IntegrationGeneration) -> LedgerRecord:
    """Return the Batch-ledger line one selected generation is filed as."""
    return LedgerRecord(
        collection=Epoch2Collection.BATCH,
        record_key=f"{item.id}-{item.batch_ref.entity_key}",
        status="selected",
        recorded_at=item.created_at,
        payload=item.model_dump(mode="json"),
    )


__all__ = [
    "AT",
    "BASE",
    "BASE_COMMIT",
    "BATCH",
    "CONTAINER",
    "DUE_SCOPE",
    "GATE_OF",
    "LOSING_HEAD",
    "OTHER_BATCH",
    "OTHER_TASK",
    "REPOSITORY",
    "RUN",
    "SECOND_HEAD",
    "TASK",
    "THIRD_HEAD",
    "binding",
    "bundle",
    "bundle_line",
    "bundle_row",
    "compiled",
    "criteria",
    "criterion",
    "delivering_generation",
    "facts",
    "following_generation",
    "gate",
    "generation",
    "generation_line",
    "ledger",
    "leg",
    "native_tree",
    "receipt",
    "receipts_at",
    "seed_lines",
    "seed_task",
    "task",
    "task_row",
]
