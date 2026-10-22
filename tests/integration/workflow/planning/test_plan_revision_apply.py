"""PLAN-008: a matching apply lands whole, and every drift writes nothing.

The positive case is driven end to end over a freshly provisioned canary:
submit, approve, apply. What it asserts is that the Milestone, its Batch
and its PLANNED Task are all present after one call, keyed and linked the
way the plan described them, and that the revision itself is ``APPLIED``.

The drift cases are the same walk with one edit to the tree between the
approval and the apply. Each edit is a fixture under
``tests/fixtures/planning/drift_cases`` naming the path it moves and the
conflict it must produce, so the four bound inputs -- state, head, policy
and content -- are each proved separately rather than through one
catch-all refusal. Every drift case asserts the document is byte-for-byte
what it was, no firehose row was appended and no WAL record was filed.

Two further walks cover what a partial apply would look like if one were
possible. The document replace is failed first, which must leave no
Milestone, no Batch and no Task; then the receipt write after it is
failed, which must leave all three and a retry that refuses rather than
applying them twice.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.models import State
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import RootSession
from eawf.runtime.daemon.methods.planning import (
    PLAN_APPLY_METHOD,
    PLAN_APPROVE_METHOD,
    PLAN_SUBMIT_METHOD,
)
from eawf.runtime.daemon.wal import list_records
from eawf.workflow.evidence._io import MEASURED_CONTRACT_STATUS
from eawf.workflow.evidence.measured_contract import PREFLIGHT_CONTRACTS, promote_measured_contract
from eawf.workflow.planning.apply import apply_plan_revision
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    document_path,
    firehose_path,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
    tree_root,
)
from tests.integration.workflow.planning._plan_approval import seal_plan_approval

pytestmark = pytest.mark.integration

DRIFT_CASES = Path(__file__).resolve().parents[3] / "fixtures" / "planning" / "drift_cases"
#: The scope a contract is promoted into below.
V1_STATE_SCOPE = "QR"

SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
TRACK_URN = f"{SLOT}/track/TRK-RUNTIME"
REPOSITORY_URN = f"{SLOT}/repository/REP-EAWF"
ACTION_URN = f"{SLOT}/pending-action/ACT-0001"
FINDING_URN = f"{SLOT}/campaign-finding/CFN-0001"
HEAD = "a" * 40

ACTOR = "OP-0001"
REVISION_KEY = "PRV-0001"
OPERATOR = {"principal_kind": "operator", "principal_id": ACTOR}


class DriftCase(BaseModel):
    """One recorded way the world moves between approval and apply.

    Attributes:
        case: The bound input the edit moves.
        description: Why that edit invalidates the approval.
        path: The document path the edit writes, from the root down.
        value: The JSON value written there.
        expected_code: The stable code the apply must refuse with.
        expected_guard: The exact rule that must name itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case: str
    description: str
    path: tuple[str, ...] = Field(min_length=2)
    value: Any
    expected_code: str
    expected_guard: str


def load_drift_cases() -> tuple[DriftCase, ...]:
    """Return every drift fixture, in file-name order.

    Returns:
        The validated cases.

    Raises:
        ValidationError: A fixture does not satisfy the case model.
    """
    return tuple(
        DriftCase.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(DRIFT_CASES.glob("*.json"))
    )


CASES = load_drift_cases()

CRITERION: dict[str, Any] = {
    "id": "CR-01",
    "text": "the published wheel installs into a clean environment",
    "kind": "functional_suitability",
    "acceptance_style": "binary",
    "evidence_kind": "deterministic",
    "quality_dimension": "functional_suitability",
    "measurable_signal": "uv run pytest tests/unit/kernel/state exits zero",
    "grounding": "measured",
    "contract_refs": ["MCT-26081303"],
}

#: The measured contract every canary here has promoted (see
#: ``planned_canary``), matching ``CRITERION["contract_refs"]`` above so
#: the default plan resolves against the generation's real store rather than an
#: unresolvable placeholder.
DEFAULT_PROMOTED_CONTRACT: Final = "MCT-26081303"

BODY: dict[str, Any] = {
    "milestone_urn": MILESTONE_URN,
    "milestone": {
        "key": "MLS-0030",
        "primary_track_ref": TRACK_URN,
        "title": "Publish an installable wheel",
        "outcome": "An operator installs the published wheel and the CLI answers.",
        "appetite": "M",
        "exclusions": ["platform packaging for Windows"],
        "acceptance_journey": [
            {
                "step_id": "AS-01",
                "actor": "operator",
                "action": "install the published wheel into a clean environment",
                "expected_observation": "the install completes and reports the version",
                "evidence_kinds": ["artifact"],
            }
        ],
        "required_batch_refs": [BATCH_URN],
    },
    "batches": [{"urn": BATCH_URN, "repository_ref": REPOSITORY_URN}],
    "tasks": [
        {
            "urn": TASK_URN,
            "batch_ref": BATCH_URN,
            "priority": "P1",
            "intent": "build and publish the wheel",
            "criteria": [CRITERION],
        }
    ],
    "citations": [{"finding_ref": FINDING_URN, "note": "the wheel build was measured here"}],
}


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def planned_canary(tmp_path: Path, *, code: str) -> CanaryProvision:
    """Provision a canary holding the Track and repository the plan binds.

    Also files :data:`DEFAULT_PROMOTED_CONTRACT` as already promoted into
    the generation, so ``CRITERION``'s ``measured`` grade resolves
    against the real store every approval here checks -- PLAN-025's
    evidence path, not an unresolvable placeholder id.
    """
    canary = provision(tmp_path / code.lower(), code=code)
    seed(
        canary,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "repository": {"REP-EAWF": {"key": "REP-EAWF", "head_sha": HEAD}},
        },
    )
    seed_generation_store(canary, promote=DEFAULT_PROMOTED_CONTRACT)
    return canary


def decision_row(decision_id: str) -> dict[str, Any]:
    """Return one loader-valid, recorded-active Decision payload keyed *decision_id*."""
    return {
        "id": decision_id,
        "scope_id": V1_STATE_SCOPE,
        "title": "Ship the accepted risk unverified",
        "rationale": "verifying it now costs more than the risk of shipping it unverified",
        "status": "active",
        "created_at": "2026-09-25T00:00:00Z",
    }


def seed_generation_store(
    canary: CanaryProvision,
    *,
    promote: str | None = None,
    decisions: dict[str, dict[str, Any]] | None = None,
) -> None:
    """File promoted contracts and Decisions into *canary*'s selected generation.

    This is the store ``approve_plan_revision`` resolves ``contract_refs``
    and ``accepted_risk_decision_ref`` citations against (PLAN-025, H1):
    a promotion is one line of the evidence ledger, as ``submit_evidence``
    files it, and a Decision is one line of the decision ledger in the
    shape the cutover imports a v1 row in.

    Args:
        canary: The canary to seed.
        promote: A :data:`PREFLIGHT_CONTRACTS` id to promote, or ``None``.
        decisions: Decision rows to record, keyed by id, or ``None``.
    """
    document = document_path(canary)
    if promote is not None:
        contract = PREFLIGHT_CONTRACTS[promote]
        view = State.model_construct(project=None, artifacts={}, updated_at=AT)
        promote_measured_contract(
            view,
            contract=contract,
            scope_id=V1_STATE_SCOPE,
            required_band=contract.environment.scale_band,
        )
        file_contract_rows(canary, [view.artifacts[promote].model_dump(mode="json")])
    for decision_id, row in (decisions or {}).items():
        append_ledger_record(
            ledger_path(document, Epoch2Collection.DECISION),
            LedgerRecord(
                collection=Epoch2Collection.DECISION,
                record_key=decision_id,
                status="imported",
                recorded_at=AT,
                payload={"payload": row},
            ),
        )


def file_contract_rows(canary: CanaryProvision, rows: list[dict[str, Any]]) -> None:
    """File *rows* as one promotion line of *canary*'s generation evidence ledger."""
    append_ledger_record(
        ledger_path(document_path(canary), Epoch2Collection.EVIDENCE),
        LedgerRecord(
            collection=Epoch2Collection.EVIDENCE,
            record_key=f"RPT-{rows[0]['id']}",
            status=MEASURED_CONTRACT_STATUS,
            recorded_at=AT,
            payload={"spike_report_ref": "artifact://spike/seeded", "contracts": rows},
        ),
    )


def dispatch(
    method: str, canary: CanaryProvision, tmp_path: Path, *, params: dict[str, Any]
) -> dict[str, Any]:
    """Drive one planning verb against *canary* and return the envelope."""
    ctx = method_context(tmp_path / "runtime")
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def submit_body(
    canary: CanaryProvision, tmp_path: Path, *, body: dict[str, Any], key: str = "req-submit"
) -> dict[str, Any]:
    """Submit *body* under the canonical revision key and return the envelope."""
    return dispatch(
        PLAN_SUBMIT_METHOD,
        canary,
        tmp_path,
        params={
            "proposal": {"key": REVISION_KEY, "author": OPERATOR, "body": body},
            "actor": ACTOR,
            "idempotency_key": key,
        },
    )


def submit(canary: CanaryProvision, tmp_path: Path, *, key: str = "req-submit") -> dict[str, Any]:
    """Submit the plan and return the envelope."""
    return submit_body(canary, tmp_path, body=BODY, key=key)


def with_criterion_grounding(**overrides: Any) -> dict[str, Any]:
    """Return a deep copy of BODY with its sole criterion regraded.

    ``grounding``, ``contract_refs`` and ``accepted_risk_decision_ref`` are
    cleared first so *overrides* fully determines the grade rather than
    layering onto BODY's own ``measured`` citation.
    """
    body = json.loads(json.dumps(BODY))
    criterion = body["tasks"][0]["criteria"][0]
    for field in ("grounding", "contract_refs", "accepted_risk_decision_ref"):
        criterion.pop(field, None)
    criterion.update(overrides)
    return body


def approve(
    canary: CanaryProvision, tmp_path: Path, *, expected_revision: int = 2
) -> dict[str, Any]:
    """Seal the operator approval and return the envelope."""
    action_ref = seal_plan_approval(
        canary.root, tmp_path / "runtime", subject=MILESTONE_URN, tag=REVISION_KEY
    )
    return dispatch(
        PLAN_APPROVE_METHOD,
        canary,
        tmp_path,
        params={
            "key": REVISION_KEY,
            "expected_revision": expected_revision,
            "action_ref": action_ref,
            "approved_by": OPERATOR,
            "actor": ACTOR,
            "idempotency_key": "req-approve",
        },
    )


def apply_plan(
    canary: CanaryProvision,
    tmp_path: Path,
    *,
    expected_revision: int = 3,
    key: str = "req-apply",
) -> dict[str, Any]:
    """Apply the plan and return the envelope."""
    return dispatch(
        PLAN_APPLY_METHOD,
        canary,
        tmp_path,
        params={
            "key": REVISION_KEY,
            "expected_revision": expected_revision,
            "actor": ACTOR,
            "idempotency_key": key,
        },
    )


def approved(tmp_path: Path, *, code: str) -> CanaryProvision:
    """Return a canary whose plan revision is sealed and ready to apply."""
    canary = planned_canary(tmp_path, code=code)
    assert submit(canary, tmp_path)["status"] == "ok"
    assert approve(canary, tmp_path)["status"] == "ok"
    return canary


def firehose_rows(canary: CanaryProvision) -> list[dict[str, Any]]:
    """Return every plan-revision row the canary's firehose holds.

    The approval question and its answer write rows of their own; only the
    plan verbs' rows are what these tests count.
    """
    path = firehose_path(canary)
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [row for row in rows if row["payload"]["name"].startswith("planning.plan_revision.")]


def wal_records(canary: CanaryProvision, tmp_path: Path) -> list[Path]:
    """Return every WAL record filed under the canary's native namespace."""
    context = method_context(tmp_path / "runtime").native_root_context(tree_root(canary))
    return list(list_records(context.wal_dir))


def collection(canary: CanaryProvision, name: str) -> dict[str, Any]:
    """Return one collection of the canary's document."""
    rows = read_document(document_path(canary)).get(name, {})
    assert isinstance(rows, dict)
    return rows


def write_at(document: dict[str, Any], *, path: tuple[str, ...], value: Any) -> None:
    """Set *value* at *path* inside *document*, creating containers as needed."""
    cursor: Any = document
    for step in path[:-1]:
        cursor = cursor.setdefault(step, {})
    cursor[path[-1]] = value


def test_a_matching_apply_lands_the_milestone_its_batch_and_its_task(tmp_path: Path) -> None:
    """One call materialises all three kinds of record, linked as planned."""
    canary = approved(tmp_path, code="APPLYOK")

    answer = apply_plan(canary, tmp_path)

    assert answer["errors"] == []
    assert answer["status"] == "ok"
    assert answer["result"]["event_name"] == "planning.plan_revision.applied"
    assert (answer["revision_before"], answer["revision_after"]) == (3, 4)

    milestone = collection(canary, "milestone")["MLS-0030"]
    assert milestone["status"] == "PLANNED"
    assert milestone["urn"] == MILESTONE_URN
    assert milestone["primary_track_ref"] == TRACK_URN
    assert milestone["required_batch_refs"] == [BATCH_URN]
    assert milestone["accepted_binding"] is None

    batch = collection(canary, "batch")["BAT-0007"]
    assert batch["status"] == "PLANNED"
    assert batch["milestone_ref"] == MILESTONE_URN
    assert batch["repository_ref"] == REPOSITORY_URN
    assert batch["task_refs"] == [TASK_URN]
    assert batch["target_branch"] is None

    task = collection(canary, "task")["EAWF-0042"]
    assert task["status"] == "PLANNED"
    assert task["batch_ref"] == BATCH_URN
    assert task["due_scope"] == MILESTONE_URN
    assert [item["id"] for item in task["criteria"]] == ["CR-01"]

    assert collection(canary, "plan_revision")[REVISION_KEY]["status"] == "APPLIED"


def test_the_apply_event_names_what_it_created(tmp_path: Path) -> None:
    """The firehose row carries the three record sets and the approval it used."""
    canary = approved(tmp_path, code="APPLYEVT")

    apply_plan(canary, tmp_path)

    names = [row["payload"]["name"] for row in firehose_rows(canary)]
    assert names == [
        "planning.plan_revision.validated",
        "planning.plan_revision.approved",
        "planning.plan_revision.applied",
    ]
    payload = firehose_rows(canary)[-1]["payload"]
    assert payload["milestone_ref"] == MILESTONE_URN
    assert payload["batch_refs"] == [BATCH_URN]
    assert payload["task_refs"] == [TASK_URN]
    assert payload["citation_refs"] == [FINDING_URN]
    assert payload["binding_refs"] == [ACTION_URN]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case)
def test_each_drift_case_refuses_with_its_own_conflict_and_writes_nothing(
    case: DriftCase, tmp_path: Path
) -> None:
    """One moved binding, one specific conflict, and a byte-identical tree."""
    canary = approved(tmp_path, code=f"DRIFT{case.case.upper()}")
    path = document_path(canary)
    document = read_document(path)
    write_at(document, path=case.path, value=case.value)
    write_document(path, document)
    before = path.read_bytes()
    rows_before = len(firehose_rows(canary))
    wal_before = len(wal_records(canary, tmp_path))

    answer = apply_plan(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["result"] is None
    row = answer["errors"][0]
    assert row["code"] == case.expected_code
    assert row["guard"] == case.expected_guard
    assert row["entity_ref"] == REVISION_KEY
    assert row["remediation"]
    assert path.read_bytes() == before
    assert len(firehose_rows(canary)) == rows_before
    assert len(wal_records(canary, tmp_path)) == wal_before


def test_every_drift_case_names_a_distinct_guard() -> None:
    """The four bound inputs are proved separately, never by one catch-all."""
    assert len({case.expected_guard for case in CASES}) == len(CASES)
    assert {case.case for case in CASES} >= {"state", "head", "policy", "content"}


def test_an_unapproved_revision_cannot_be_applied(tmp_path: Path) -> None:
    """A VALIDATED plan has no apply edge, so nothing it describes is created."""
    canary = planned_canary(tmp_path, code="NOAPPR")
    assert submit(canary, tmp_path)["status"] == "ok"
    before = document_path(canary).read_bytes()

    answer = apply_plan(canary, tmp_path, expected_revision=2)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "illegal_transition"
    assert answer["errors"][0]["guard"] == "plan_revision_edge_registered"
    assert document_path(canary).read_bytes() == before
    assert collection(canary, "milestone") == {}
    assert collection(canary, "task") == {}


def test_a_stale_expected_revision_is_a_conflict(tmp_path: Path) -> None:
    """The compare-and-swap token is checked under the locks, not before them."""
    canary = approved(tmp_path, code="STALECAS")
    before = document_path(canary).read_bytes()

    answer = apply_plan(canary, tmp_path, expected_revision=2)

    assert answer["errors"][0]["code"] == "revision_conflict"
    assert answer["errors"][0]["guard"] == "plan_revision_cas"
    assert document_path(canary).read_bytes() == before


def test_a_retry_under_one_key_replays_the_receipt(tmp_path: Path) -> None:
    """A re-apply under the original key answers the original receipt exactly."""
    canary = approved(tmp_path, code="REPLAY")

    first = apply_plan(canary, tmp_path)
    second = apply_plan(canary, tmp_path)

    assert second["status"] == "ok"
    assert second["result"] == first["result"]
    assert len(firehose_rows(canary)) == 3
    assert list(collection(canary, "task")) == ["EAWF-0042"]
    assert collection(canary, "milestone")["MLS-0030"]["revision"] == 1


def test_one_key_naming_two_requests_is_an_idempotency_conflict(tmp_path: Path) -> None:
    """A key that already committed other parameters is not a retry of them."""
    canary = approved(tmp_path, code="REUSED")
    apply_plan(canary, tmp_path)
    before = document_path(canary).read_bytes()

    answer = apply_plan(canary, tmp_path, expected_revision=9)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "idempotency_conflict"
    assert answer["errors"][0]["guard"] == "idempotency_key_reused"
    assert document_path(canary).read_bytes() == before


def test_a_second_apply_under_another_key_is_refused(tmp_path: Path) -> None:
    """An applied revision has no apply edge, so it cannot land twice."""
    canary = approved(tmp_path, code="TWICE")
    apply_plan(canary, tmp_path)
    before = document_path(canary).read_bytes()

    answer = apply_plan(canary, tmp_path, expected_revision=4, key="req-apply-again")

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "illegal_transition"
    assert document_path(canary).read_bytes() == before
    assert len(firehose_rows(canary)) == 3


def test_a_failed_document_replace_leaves_no_fragment(tmp_path: Path) -> None:
    """The write that would land the plan is the only write that lands any of it."""
    canary = approved(tmp_path, code="NOWRITE")
    context = root_context(canary, tmp_path / "runtime")

    def refuse(self: RootSession, document: dict[str, Any]) -> None:
        raise OSError("the document replace failed")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(RootSession, "write_document", refuse)
        with pytest.raises(OSError, match="document replace failed"):
            apply_plan_revision(
                context,
                key=REVISION_KEY,
                expected_revision=3,
                actor=ACTOR,
                idempotency_key="req-apply",
                at=AT,
            )

    assert collection(canary, "milestone") == {}
    assert collection(canary, "batch") == {}
    assert collection(canary, "task") == {}
    assert collection(canary, "plan_revision")[REVISION_KEY]["status"] == "APPROVED"
    assert len(firehose_rows(canary)) == 2


def test_a_crash_after_the_replace_leaves_a_whole_plan_and_refuses_a_retry(
    tmp_path: Path,
) -> None:
    """Every record lands together, and the retry meets a revision with no apply edge."""
    canary = approved(tmp_path, code="AFTERWR")
    context = root_context(canary, tmp_path / "runtime")

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("the receipt write failed")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("eawf.workflow.planning.apply.record_idempotency_receipt", refuse)
        with pytest.raises(OSError, match="receipt write failed"):
            apply_plan_revision(
                context,
                key=REVISION_KEY,
                expected_revision=3,
                actor=ACTOR,
                idempotency_key="req-apply",
                at=AT,
            )

    assert collection(canary, "milestone")["MLS-0030"]["status"] == "PLANNED"
    assert collection(canary, "batch")["BAT-0007"]["status"] == "PLANNED"
    assert collection(canary, "task")["EAWF-0042"]["status"] == "PLANNED"
    assert collection(canary, "plan_revision")[REVISION_KEY]["status"] == "APPLIED"

    before = document_path(canary).read_bytes()
    answer = apply_plan(canary, tmp_path, expected_revision=4, key="req-apply")

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "illegal_transition"
    assert document_path(canary).read_bytes() == before


def test_a_plan_over_an_unknown_track_is_refused_at_submission(tmp_path: Path) -> None:
    """A plan whose container the document does not hold never becomes a record."""
    canary = provision(tmp_path / "notrack", code="NOTRACK")
    seed(canary, {"repository": {"REP-EAWF": {"key": "REP-EAWF", "head_sha": HEAD}}})
    before = document_path(canary).read_bytes()

    answer = submit(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "identity_not_found"
    assert answer["errors"][0]["guard"] == "primary_track_resolved"
    assert document_path(canary).read_bytes() == before


def test_a_plan_over_a_headless_repository_is_refused_at_submission(tmp_path: Path) -> None:
    """A head binding cannot be invented, so an unreadable head shuts submission."""
    canary = provision(tmp_path / "nohead", code="NOHEAD")
    seed(canary, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})

    answer = submit(canary, tmp_path)

    assert answer["errors"][0]["code"] == "identity_not_found"
    assert answer["errors"][0]["guard"] == "repository_head_readable"
    assert collection(canary, "plan_revision") == {}


def test_a_second_submission_under_one_key_is_refused(tmp_path: Path) -> None:
    """Two plans under one key would make the key address two bodies."""
    canary = planned_canary(tmp_path, code="DUPKEY")
    assert submit(canary, tmp_path)["status"] == "ok"

    answer = submit(canary, tmp_path, key="req-submit-again")

    assert answer["errors"][0]["code"] == "revision_conflict"
    assert answer["errors"][0]["guard"] == "plan_revision_key_free"


def test_an_unknown_request_field_is_refused_before_any_lock(tmp_path: Path) -> None:
    """The parameter models forbid extras like every other strict model."""
    canary = approved(tmp_path, code="EXTRA")
    before = document_path(canary).read_bytes()

    answer = dispatch(
        PLAN_APPLY_METHOD,
        canary,
        tmp_path,
        params={
            "key": REVISION_KEY,
            "expected_revision": 3,
            "actor": ACTOR,
            "idempotency_key": "req-apply",
            "content_digest": f"sha256:{'c' * 64}",
        },
    )

    assert answer["errors"][0]["code"] == "schema_validation_failed"
    assert document_path(canary).read_bytes() == before


def test_an_approval_by_a_service_principal_is_refused(tmp_path: Path) -> None:
    """Only a human principal accepts the consequences of a plan."""
    canary = planned_canary(tmp_path, code="SERVICE")
    assert submit(canary, tmp_path)["status"] == "ok"
    before = document_path(canary).read_bytes()

    answer = dispatch(
        PLAN_APPROVE_METHOD,
        canary,
        tmp_path,
        params={
            "key": REVISION_KEY,
            "expected_revision": 2,
            "action_ref": ACTION_URN,
            "approved_by": {"principal_kind": "service", "principal_id": "SVC-0001"},
            "actor": ACTOR,
            "idempotency_key": "req-approve",
        },
    )

    assert answer["errors"][0]["code"] == "schema_validation_failed"
    assert document_path(canary).read_bytes() == before


def test_an_approval_receipt_pointed_at_a_milestone_is_refused(tmp_path: Path) -> None:
    """The receipt must address a PendingAction, whatever else resolves."""
    canary = planned_canary(tmp_path, code="NOTACT")
    assert submit(canary, tmp_path)["status"] == "ok"

    answer = dispatch(
        PLAN_APPROVE_METHOD,
        canary,
        tmp_path,
        params={
            "key": REVISION_KEY,
            "expected_revision": 2,
            "action_ref": MILESTONE_URN,
            "approved_by": OPERATOR,
            "actor": ACTOR,
            "idempotency_key": "req-approve",
        },
    )

    assert answer["errors"][0]["code"] == "schema_validation_failed"
    assert collection(canary, "plan_revision")[REVISION_KEY]["status"] == "VALIDATED"


def test_approving_a_revision_nobody_submitted_is_refused(tmp_path: Path) -> None:
    """An absent revision is the empty boundary of every verb that reads one."""
    canary = planned_canary(tmp_path, code="ABSENT")

    answer = approve(canary, tmp_path, expected_revision=1)

    assert answer["errors"][0]["code"] == "identity_not_found"
    assert answer["errors"][0]["guard"] == "plan_revision_resolved"


# ---- PLAN-031: the grounding gate, driven through the real approval verb ---


def test_approval_refuses_an_acceptance_criterion_still_graded_assumed(tmp_path: Path) -> None:
    """The daemon's own approve verb refuses a plan resting on an assumed claim."""
    canary = planned_canary(tmp_path, code="ASSUMED")
    assert submit_body(canary, tmp_path, body=with_criterion_grounding())["status"] == "ok"
    seal_plan_approval(canary.root, tmp_path / "runtime", subject=MILESTONE_URN, tag=REVISION_KEY)
    before = document_path(canary).read_bytes()

    answer = approve(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "transition_guard_failed"
    assert answer["errors"][0]["guard"] == "criteria_grounded_at_approval"
    assert document_path(canary).read_bytes() == before
    assert collection(canary, "plan_revision")[REVISION_KEY]["status"] == "VALIDATED"


def test_approval_admits_the_same_plan_regraded_accepted_risk(tmp_path: Path) -> None:
    """Regrading the criterion accepted-risk, citing a recorded Decision, clears approval."""
    canary = planned_canary(tmp_path, code="ACCRISK")
    seed_generation_store(canary, decisions={"DEC-0001": decision_row("DEC-0001")})
    body = with_criterion_grounding(
        grounding="accepted-risk", accepted_risk_decision_ref="DEC-0001"
    )
    assert submit_body(canary, tmp_path, body=body)["status"] == "ok"

    answer = approve(canary, tmp_path)

    assert answer["status"] == "ok"
    assert collection(canary, "plan_revision")[REVISION_KEY]["status"] == "APPROVED"


def test_approval_refuses_an_accepted_risk_criterion_whose_decision_ref_does_not_resolve(
    tmp_path: Path,
) -> None:
    """H1 gate-fire proof: the generation ``planned_canary`` seeds records no such Decision.

    Reverting :func:`~eawf.workflow.planning.apply._build_citation_resolvers`
    to skip Decision resolution reds this test: an unrecorded id such as
    ``DEC-bogus`` would otherwise reach APPROVED unconditionally.
    """
    canary = planned_canary(tmp_path, code="DECBOGUS")
    seed_generation_store(canary, decisions={"DEC-0001": decision_row("DEC-0001")})
    body = with_criterion_grounding(
        grounding="accepted-risk", accepted_risk_decision_ref="DEC-BOGUS"
    )
    assert submit_body(canary, tmp_path, body=body)["status"] == "ok"

    answer = approve(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "transition_guard_failed"
    assert answer["errors"][0]["guard"] == "criteria_grounded_at_approval"
    assert collection(canary, "plan_revision")[REVISION_KEY]["status"] == "VALIDATED"


# ---- PLAN-025: the measured grade resolves against the real contract store


def test_approval_refuses_a_measured_criterion_whose_contract_ref_does_not_resolve(
    tmp_path: Path,
) -> None:
    """The generation ``planned_canary`` seeds refuses a citation it never promoted."""
    canary = planned_canary(tmp_path, code="UNRESOLVD")
    body = with_criterion_grounding(grounding="measured", contract_refs=["MCT-99999999"])
    assert submit_body(canary, tmp_path, body=body)["status"] == "ok"

    answer = approve(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "transition_guard_failed"
    assert answer["errors"][0]["guard"] == "criteria_grounded_at_approval"
    assert collection(canary, "plan_revision")[REVISION_KEY]["status"] == "VALIDATED"


def test_approval_admits_a_measured_criterion_whose_contract_ref_resolves(
    tmp_path: Path,
) -> None:
    """The default plan body cites the contract ``planned_canary`` already promoted."""
    canary = planned_canary(tmp_path, code="RESOLVED")
    assert submit_body(canary, tmp_path, body=BODY)["status"] == "ok"

    answer = approve(canary, tmp_path)

    assert answer["status"] == "ok"
    assert collection(canary, "plan_revision")[REVISION_KEY]["status"] == "APPROVED"


def test_approval_of_an_assumed_criterion_is_unaffected_by_the_contract_store(
    tmp_path: Path,
) -> None:
    """A resolver that exists still flags assumed the same way."""
    canary = planned_canary(tmp_path, code="ASSUMEDV1")
    assert submit_body(canary, tmp_path, body=with_criterion_grounding())["status"] == "ok"

    answer = approve(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "criteria_grounded_at_approval"


# ---- re-plan: a child of an APPLIED revision rewrites its PLANNED Milestone


PARENT_KEY: Final = "PRV-0010"
CHILD_KEY: Final = "PRV-0011"
GRANDCHILD_KEY: Final = "PRV-0012"
BATCH_A: Final = f"{SLOT}/batch/BAT-0001"
BATCH_B: Final = f"{SLOT}/batch/BAT-0002"
BATCH_C: Final = f"{SLOT}/batch/BAT-0003"
TASK_1: Final = f"{SLOT}/task/EAWF-0001"
TASK_2: Final = f"{SLOT}/task/EAWF-0002"
TASK_3: Final = f"{SLOT}/task/EAWF-0003"
TASK_4: Final = f"{SLOT}/task/EAWF-0004"
OTHER_MILESTONE_URN: Final = f"{SLOT}/milestone/MLS-0031"
DEFAULT_BRANCH: Final = "trunk"

#: The project row a migrated tree carries: the import envelope nests the
#: v1 payload, default branch included, two levels down.
IMPORTED_PROJECT_ROW: Final[dict[str, Any]] = {
    "payload": {"payload": {"code": "EAWF", "default_branch": DEFAULT_BRANCH}},
    "status": "imported",
}


def plan_body(
    *,
    batches: dict[str, tuple[tuple[str, str], ...]],
    title: str = "Publish an installable wheel",
    target_date: str | None = None,
    milestone_urn: str = MILESTONE_URN,
    dropped: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return a plan body placing Tasks under Batches.

    Args:
        batches: Each Batch URN mapped to its ``(task_urn, criterion_id)``
            entries, in order.
        title: The Milestone title.
        target_date: The Milestone target date, ISO formatted.
        milestone_urn: The Milestone the plan writes.
        dropped: Parent Task URNs the plan drops with a typed reason.

    Returns:
        A body the submit verb accepts.
    """
    body: dict[str, Any] = json.loads(json.dumps(BODY))
    body["milestone_urn"] = milestone_urn
    body["milestone"].update(
        key=milestone_urn.rsplit("/", 1)[1],
        title=title,
        target_date=target_date,
        required_batch_refs=list(batches),
    )
    body["batches"] = [{"urn": urn, "repository_ref": REPOSITORY_URN} for urn in batches]
    body["tasks"] = [
        {
            "urn": task_urn,
            "batch_ref": batch_urn,
            "priority": "P1",
            "intent": f"deliver {task_urn.rsplit('/', 1)[1]}",
            "criteria": [{**CRITERION, "id": criterion_id}],
        }
        for batch_urn, entries in batches.items()
        for task_urn, criterion_id in entries
    ]
    body["dropped_tasks"] = [{"task_ref": ref, "reason": "re-planned away"} for ref in dropped]
    return body


PARENT_BODY: Final = plan_body(
    batches={
        BATCH_A: ((TASK_1, "CR-01"), (TASK_2, "CR-01")),
        BATCH_B: ((TASK_3, "CR-01"),),
    }
)

CHILD_BODY: Final = plan_body(
    batches={BATCH_C: ((TASK_1, "CR-02"), (TASK_4, "CR-01"))},
    title="Publish a signed wheel",
    target_date="2026-12-01",
    dropped=(TASK_2, TASK_3),
)


def drive(
    canary: CanaryProvision,
    tmp_path: Path,
    *,
    key: str,
    body: dict[str, Any],
    parent_key: str | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Submit and approve one revision, then apply it unless told not to.

    Returns:
        The envelope of the last verb driven.
    """
    proposal: dict[str, Any] = {"key": key, "author": OPERATOR, "body": body}
    if parent_key is not None:
        proposal["parent_key"] = parent_key
    submitted = dispatch(
        PLAN_SUBMIT_METHOD,
        canary,
        tmp_path,
        params={"proposal": proposal, "actor": ACTOR, "idempotency_key": f"submit-{key}"},
    )
    assert submitted["status"] == "ok", submitted["errors"]
    action_ref = seal_plan_approval(
        canary.root, tmp_path / "runtime", subject=body["milestone_urn"], tag=key
    )
    approved_answer = dispatch(
        PLAN_APPROVE_METHOD,
        canary,
        tmp_path,
        params={
            "key": key,
            "expected_revision": 2,
            "action_ref": action_ref,
            "approved_by": OPERATOR,
            "actor": ACTOR,
            "idempotency_key": f"approve-{key}",
        },
    )
    assert approved_answer["status"] == "ok", approved_answer["errors"]
    if not apply:
        return approved_answer
    return dispatch(
        PLAN_APPLY_METHOD,
        canary,
        tmp_path,
        params={
            "key": key,
            "expected_revision": 3,
            "actor": ACTOR,
            "idempotency_key": f"apply-{key}",
        },
    )


def applied_parent(
    tmp_path: Path, *, code: str, project: dict[str, Any] | None = IMPORTED_PROJECT_ROW
) -> CanaryProvision:
    """Return a canary whose parent plan is APPLIED and whose project row is *project*."""
    canary = planned_canary(tmp_path, code=code)
    if project is not None:
        seed(canary, {"project": {"EAWF": project}})
    answer = drive(canary, tmp_path, key=PARENT_KEY, body=PARENT_BODY)
    assert answer["status"] == "ok", answer["errors"]
    return canary


def edit_row(canary: CanaryProvision, collection_name: str, key: str, **fields: Any) -> None:
    """Overwrite *fields* of one stored row in place, bypassing every lifecycle edge."""
    path = document_path(canary)
    document = read_document(path)
    document[collection_name][key].update(fields)
    write_document(path, document)


def apply_child(canary: CanaryProvision, tmp_path: Path) -> dict[str, Any]:
    """Approve the child plan, then apply it, returning the apply envelope."""
    return drive(canary, tmp_path, key=CHILD_KEY, body=CHILD_BODY, parent_key=PARENT_KEY)


@pytest.mark.parametrize(
    ("project", "branch"),
    [
        pytest.param(IMPORTED_PROJECT_ROW, DEFAULT_BRANCH, id="imported-row"),
        pytest.param({"key": "EAWF", "default_branch": "main"}, "main", id="native-row"),
    ],
)
def test_a_replan_rewrites_the_planned_milestone_in_one_apply(
    tmp_path: Path, project: dict[str, Any], branch: str
) -> None:
    """The child rewrites what it keeps, cancels what it drops and adds the rest."""
    canary = applied_parent(tmp_path, code="REPLAN", project=project)
    milestone_before = collection(canary, "milestone")["MLS-0030"]
    task_before = collection(canary, "task")["EAWF-0001"]

    answer = apply_child(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
    milestone = collection(canary, "milestone")["MLS-0030"]
    assert milestone["status"] == "PLANNED"
    assert milestone["revision"] == 2
    assert (milestone["uid"], milestone["created_at"], milestone["origin"]) == (
        milestone_before["uid"],
        milestone_before["created_at"],
        milestone_before["origin"],
    )
    assert milestone["title"] == "Publish a signed wheel"
    assert milestone["target_date"] == "2026-12-01"
    assert milestone["required_batch_refs"] == [BATCH_C]

    batches = collection(canary, "batch")
    for dropped in ("BAT-0001", "BAT-0002"):
        assert batches[dropped]["status"] == "CANCELLED"
        assert batches[dropped]["target_branch"] == branch
        assert batches[dropped]["revision"] == 2
    assert batches["BAT-0003"]["status"] == "PLANNED"
    assert batches["BAT-0003"]["revision"] == 1
    assert batches["BAT-0003"]["task_refs"] == [TASK_1, TASK_4]

    tasks = collection(canary, "task")
    assert tasks["EAWF-0002"]["status"] == "CANCELLED"
    assert tasks["EAWF-0003"]["status"] == "CANCELLED"
    kept = tasks["EAWF-0001"]
    assert kept["status"] == "PLANNED"
    assert (kept["revision"], kept["contract_revision"]) == (2, 2)
    assert (kept["uid"], kept["created_at"]) == (task_before["uid"], task_before["created_at"])
    assert kept["batch_ref"] == BATCH_C
    assert kept["due_scope"] == MILESTONE_URN
    assert [item["id"] for item in kept["criteria"]] == ["CR-02"]
    assert (tasks["EAWF-0004"]["status"], tasks["EAWF-0004"]["revision"]) == ("PLANNED", 1)

    revisions = collection(canary, "plan_revision")
    assert revisions[CHILD_KEY]["status"] == "APPLIED"
    assert revisions[PARENT_KEY]["status"] == "APPLIED"


def test_a_replan_keeping_every_batch_rewrites_them_and_needs_no_branch(
    tmp_path: Path,
) -> None:
    """With nothing to cancel, a tree without a project row re-plans in place."""
    canary = applied_parent(tmp_path, code="KEEPALL", project=None)
    body = plan_body(
        batches={BATCH_A: ((TASK_1, "CR-02"), (TASK_2, "CR-01")), BATCH_B: ((TASK_3, "CR-01"),)},
        title="Publish a signed wheel",
    )

    answer = drive(canary, tmp_path, key=CHILD_KEY, body=body, parent_key=PARENT_KEY)

    assert answer["status"] == "ok", answer["errors"]
    batches = collection(canary, "batch")
    assert {key: (row["status"], row["revision"]) for key, row in batches.items()} == {
        "BAT-0001": ("PLANNED", 2),
        "BAT-0002": ("PLANNED", 2),
    }
    assert batches["BAT-0001"]["target_branch"] is None
    tasks = collection(canary, "task")
    assert {key: row["contract_revision"] for key, row in tasks.items()} == {
        "EAWF-0001": 2,
        "EAWF-0002": 2,
        "EAWF-0003": 2,
    }
    assert collection(canary, "milestone")["MLS-0030"]["revision"] == 2


def test_a_dropped_parent_task_already_settled_is_left_untouched(tmp_path: Path) -> None:
    """Only a still-PLANNED leftover is cancelled; a settled one keeps its row."""
    canary = applied_parent(tmp_path, code="SETTLED")
    edit_row(canary, "task", "EAWF-0003", status="CANCELLED", revision=2)
    settled = collection(canary, "task")["EAWF-0003"]

    answer = apply_child(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
    assert collection(canary, "task")["EAWF-0003"] == settled
    assert collection(canary, "task")["EAWF-0002"]["status"] == "CANCELLED"


def test_a_replan_of_a_milestone_no_longer_planned_is_refused(tmp_path: Path) -> None:
    """An activated Milestone changes through its own lifecycle, not a re-plan."""
    canary = applied_parent(tmp_path, code="ACTIVEMS")
    edit_row(canary, "milestone", "MLS-0030", status="ACTIVE")
    child_ready = drive(
        canary, tmp_path, key=CHILD_KEY, body=CHILD_BODY, parent_key=PARENT_KEY, apply=False
    )
    assert child_ready["status"] == "ok"
    before = document_path(canary).read_bytes()

    answer = apply_plan_key(canary, tmp_path, key=CHILD_KEY)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "transition_guard_failed"
    assert answer["errors"][0]["guard"] == "replanned_milestone_planned"
    assert answer["errors"][0]["remediation"]
    assert document_path(canary).read_bytes() == before


def apply_plan_key(canary: CanaryProvision, tmp_path: Path, *, key: str) -> dict[str, Any]:
    """Apply the approved revision *key* and return the envelope."""
    return dispatch(
        PLAN_APPLY_METHOD,
        canary,
        tmp_path,
        params={
            "key": key,
            "expected_revision": 3,
            "actor": ACTOR,
            "idempotency_key": f"apply-{key}",
        },
    )


@pytest.mark.parametrize("status", ["CLAIMED", "RUNNING"])
def test_a_replan_carrying_a_parent_task_past_planned_is_refused(
    tmp_path: Path, status: str
) -> None:
    """A started Task stays claimed, so the re-plan cannot re-place it."""
    canary = applied_parent(tmp_path, code=f"CARRY{status}")
    edit_row(canary, "task", "EAWF-0001", status=status)
    drive(canary, tmp_path, key=CHILD_KEY, body=CHILD_BODY, parent_key=PARENT_KEY, apply=False)
    before = document_path(canary).read_bytes()

    answer = apply_plan_key(canary, tmp_path, key=CHILD_KEY)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "plan_keys_unclaimed"
    assert "task/EAWF-0001" in answer["errors"][0]["message"]
    assert "milestone/MLS-0030" not in answer["errors"][0]["message"]
    assert document_path(canary).read_bytes() == before


def test_a_replan_dropping_a_batch_without_a_default_branch_is_refused(
    tmp_path: Path,
) -> None:
    """A cancelled Batch records its branch, which cannot be invented."""
    canary = applied_parent(tmp_path, code="NOBRANCH", project=None)
    drive(canary, tmp_path, key=CHILD_KEY, body=CHILD_BODY, parent_key=PARENT_KEY, apply=False)
    before = document_path(canary).read_bytes()

    answer = apply_plan_key(canary, tmp_path, key=CHILD_KEY)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "identity_not_found"
    assert answer["errors"][0]["guard"] == "project_default_branch_resolved"
    assert answer["errors"][0]["remediation"]
    assert document_path(canary).read_bytes() == before


def test_a_replan_under_a_parent_that_is_not_applied_is_refused(tmp_path: Path) -> None:
    """A parent that placed nothing gives its child nothing to re-plan."""
    canary = applied_parent(tmp_path, code="UNAPPLIED")
    drive(canary, tmp_path, key=CHILD_KEY, body=CHILD_BODY, parent_key=PARENT_KEY, apply=False)
    drive(
        canary,
        tmp_path,
        key=GRANDCHILD_KEY,
        body=CHILD_BODY,
        parent_key=CHILD_KEY,
        apply=False,
    )
    before = document_path(canary).read_bytes()

    answer = apply_plan_key(canary, tmp_path, key=GRANDCHILD_KEY)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "transition_guard_failed"
    assert answer["errors"][0]["guard"] == "parent_plan_revision_applied"
    assert document_path(canary).read_bytes() == before
    assert collection(canary, "plan_revision")[CHILD_KEY]["status"] == "APPROVED"


def test_a_child_of_an_unapplied_parent_creates_a_milestone_nobody_placed(
    tmp_path: Path,
) -> None:
    """With no Milestone to rewrite, a repair of an unapplied parent plans as before."""
    canary = planned_canary(tmp_path, code="REPAIR")
    drive(canary, tmp_path, key=PARENT_KEY, body=PARENT_BODY, apply=False)

    answer = apply_child(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
    assert collection(canary, "milestone")["MLS-0030"]["revision"] == 1
    assert sorted(collection(canary, "batch")) == ["BAT-0003"]
    assert collection(canary, "plan_revision")[PARENT_KEY]["status"] == "APPROVED"


def test_a_child_of_a_parent_on_another_milestone_still_refuses_a_reused_key(
    tmp_path: Path,
) -> None:
    """A parent on another Milestone only informs the lenses, as before."""
    canary = applied_parent(tmp_path, code="OTHERMS")
    body = plan_body(
        batches={BATCH_C: ((TASK_1, "CR-02"), (TASK_4, "CR-01"))},
        milestone_urn=OTHER_MILESTONE_URN,
        dropped=(TASK_2, TASK_3),
    )
    drive(canary, tmp_path, key=CHILD_KEY, body=body, parent_key=PARENT_KEY, apply=False)
    before = document_path(canary).read_bytes()

    answer = apply_plan_key(canary, tmp_path, key=CHILD_KEY)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "plan_keys_unclaimed"
    assert "task/EAWF-0001" in answer["errors"][0]["message"]
    assert document_path(canary).read_bytes() == before


#: The title ``eawf milestone create`` gave the bare Milestone below, which
#: the first plan's contract replaces.
BARE_TITLE: Final = "Bare Milestone awaiting its first plan"


def bare_milestone_canary(tmp_path: Path, *, code: str, status: str = "PLANNED") -> CanaryProvision:
    """Return a canary holding a Milestone made outside a plan, in *status*."""
    canary = planned_canary(tmp_path, code=code)
    seed(
        canary, {"milestone": {"MLS-0030": {**seed_row("milestone", status), "title": BARE_TITLE}}}
    )
    return canary


def test_a_first_plan_applies_over_a_bare_planned_milestone(tmp_path: Path) -> None:
    """A Milestone made by the create verb takes its first plan without a key conflict."""
    canary = bare_milestone_canary(tmp_path, code="BARE")
    bare = collection(canary, "milestone")["MLS-0030"]

    answer = drive(canary, tmp_path, key=PARENT_KEY, body=PARENT_BODY)

    assert answer["status"] == "ok", answer["errors"]
    milestone = collection(canary, "milestone")["MLS-0030"]
    assert (milestone["key"], milestone["uid"], milestone["urn"], milestone["created_at"]) == (
        bare["key"],
        bare["uid"],
        bare["urn"],
        bare["created_at"],
    )
    assert milestone["revision"] == bare["revision"] + 1
    assert milestone["status"] == "PLANNED"
    assert milestone["title"] == PARENT_BODY["milestone"]["title"]
    assert milestone["required_batch_refs"] == [BATCH_A, BATCH_B]
    assert sorted(collection(canary, "batch")) == ["BAT-0001", "BAT-0002"]
    tasks = collection(canary, "task")
    assert sorted(tasks) == ["EAWF-0001", "EAWF-0002", "EAWF-0003"]
    assert {task["status"] for task in tasks.values()} == {"PLANNED"}
    assert collection(canary, "plan_revision")[PARENT_KEY]["status"] == "APPLIED"


def test_a_first_plan_over_a_milestone_an_applied_plan_placed_is_refused(tmp_path: Path) -> None:
    """Only a re-plan under the applied plan may rewrite the Milestone it placed."""
    canary = applied_parent(tmp_path, code="PLACEDMS")
    body = plan_body(batches={BATCH_C: ((TASK_4, "CR-01"),)})
    drive(canary, tmp_path, key=CHILD_KEY, body=body, apply=False)
    before = document_path(canary).read_bytes()

    answer = apply_plan_key(canary, tmp_path, key=CHILD_KEY)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "plan_keys_unclaimed"
    assert "milestone/MLS-0030" in answer["errors"][0]["message"]
    assert document_path(canary).read_bytes() == before


@pytest.mark.parametrize("status", ["ACTIVE", "CANCELLED"])
def test_a_first_plan_over_a_milestone_past_planned_is_refused(tmp_path: Path, status: str) -> None:
    """A started or closed Milestone is never filled by a plan."""
    canary = bare_milestone_canary(tmp_path, code=f"PAST{status[:3]}", status=status)
    drive(canary, tmp_path, key=PARENT_KEY, body=PARENT_BODY, apply=False)
    before = document_path(canary).read_bytes()

    answer = apply_plan_key(canary, tmp_path, key=PARENT_KEY)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "plan_keys_unclaimed"
    assert "milestone/MLS-0030" in answer["errors"][0]["message"]
    assert document_path(canary).read_bytes() == before


def test_a_first_plan_over_a_milestone_with_a_batch_is_refused(tmp_path: Path) -> None:
    """A Batch created outside a plan already gives the Milestone work to protect."""
    canary = bare_milestone_canary(tmp_path, code="HASBATCH")
    seed(canary, {"batch": {"BAT-0007": seed_row("batch", "PLANNED")}})
    drive(canary, tmp_path, key=PARENT_KEY, body=PARENT_BODY, apply=False)
    before = document_path(canary).read_bytes()

    answer = apply_plan_key(canary, tmp_path, key=PARENT_KEY)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "plan_keys_unclaimed"
    assert answer["errors"][0]["message"].endswith("milestone/MLS-0030")
    assert document_path(canary).read_bytes() == before


@pytest.mark.parametrize(
    ("collection_name", "key", "row"),
    [
        pytest.param(
            "batch",
            "BAT-0001",
            {**seed_row("batch", "PLANNED"), "milestone_ref": OTHER_MILESTONE_URN},
            id="batch",
        ),
        pytest.param("task", "EAWF-0001", seed_row("task", "PLANNED"), id="task"),
    ],
)
def test_a_first_plan_over_a_bare_milestone_still_refuses_a_key_collision(
    tmp_path: Path, collection_name: str, key: str, row: dict[str, Any]
) -> None:
    """Claiming the Milestone exempts no Batch or Task the plan would overwrite."""
    canary = bare_milestone_canary(tmp_path, code=f"COLLIDE{collection_name.upper()}")
    seed(canary, {collection_name: {key: row}})
    drive(canary, tmp_path, key=PARENT_KEY, body=PARENT_BODY, apply=False)
    before = document_path(canary).read_bytes()

    answer = apply_plan_key(canary, tmp_path, key=PARENT_KEY)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "plan_keys_unclaimed"
    assert f"{collection_name}/{key}" in answer["errors"][0]["message"]
    assert "milestone/MLS-0030" not in answer["errors"][0]["message"]
    assert document_path(canary).read_bytes() == before
