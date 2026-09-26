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

from eawf.kernel.state.models import Decision
from eawf.kernel.store.compaction import read_document, write_document
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import RootSession
from eawf.runtime.daemon.methods.planning import (
    PLAN_APPLY_METHOD,
    PLAN_APPROVE_METHOD,
    PLAN_SUBMIT_METHOD,
)
from eawf.runtime.daemon.wal import list_records
from eawf.workflow.evidence._io import load_state
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

pytestmark = pytest.mark.integration

DRIFT_CASES = Path(__file__).resolve().parents[3] / "fixtures" / "planning" / "drift_cases"
V1_STATE_FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)
#: The fixture project code, and the scope a contract is promoted into below.
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
#: the default plan resolves against the real v1 store rather than an
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

    Also seeds a v1 ``state.json`` with :data:`DEFAULT_PROMOTED_CONTRACT`
    already promoted, so ``CRITERION``'s ``measured`` grade resolves
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
    seed_v1_state(canary, promote=DEFAULT_PROMOTED_CONTRACT)
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


def seed_v1_state(
    canary: CanaryProvision,
    *,
    promote: str | None = None,
    decisions: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Write a v1 ``state.json`` beside *canary*'s epoch-2 tree.

    This is the store ``approve_plan_revision`` resolves ``contract_refs``
    and ``accepted_risk_decision_ref`` citations against (PLAN-025, H1); a
    canary that never calls this has no such file, which the resolvers
    read as "nothing to check against" rather than "nothing resolves".

    Args:
        canary: The canary to seed. Its own ``.ea`` directory already
            exists, provisioned as an epoch-2 tree.
        promote: A :data:`PREFLIGHT_CONTRACTS` id to promote into the
            written state, or ``None`` to leave the state with nothing
            promoted.
        decisions: Decision rows to record, keyed by id, or ``None`` to
            leave ``decisions`` empty.
    """
    state_path = tree_root(canary) / "state.json"
    state_path.write_text(V1_STATE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    if promote is None and decisions is None:
        return
    state = load_state(state_path)
    if promote is not None:
        contract = PREFLIGHT_CONTRACTS[promote]
        promote_measured_contract(
            state,
            contract=contract,
            scope_id=V1_STATE_SCOPE,
            required_band=contract.environment.scale_band,
        )
    if decisions is not None:
        for decision_id, row in decisions.items():
            state.decisions[decision_id] = Decision.model_validate(row)
    state_path.write_text(json.dumps(state.model_dump(mode="json"), indent=2), encoding="utf-8")


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
    return dispatch(
        PLAN_APPROVE_METHOD,
        canary,
        tmp_path,
        params={
            "key": REVISION_KEY,
            "expected_revision": expected_revision,
            "action_ref": ACTION_URN,
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
    """Return every row the canary's firehose holds."""
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


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
    seed_v1_state(canary, decisions={"DEC-0001": decision_row("DEC-0001")})
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
    """H1 gate-fire proof: the v1 state ``planned_canary`` seeds records no such Decision.

    Reverting :func:`~eawf.workflow.planning.apply._build_citation_resolvers`
    to skip Decision resolution reds this test: an unrecorded id such as
    ``DEC-bogus`` would otherwise reach APPROVED unconditionally.
    """
    canary = planned_canary(tmp_path, code="DECBOGUS")
    seed_v1_state(canary, decisions={"DEC-0001": decision_row("DEC-0001")})
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
    """The v1 state ``planned_canary`` seeds refuses a citation it never promoted."""
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


def test_approval_of_an_assumed_criterion_is_unaffected_by_an_empty_v1_state(
    tmp_path: Path,
) -> None:
    """A resolver that exists but resolves nothing still flags assumed the same way."""
    canary = planned_canary(tmp_path, code="ASSUMEDV1")
    seed_v1_state(canary)
    assert submit_body(canary, tmp_path, body=with_criterion_grounding())["status"] == "ok"

    answer = approve(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "criteria_grounded_at_approval"
