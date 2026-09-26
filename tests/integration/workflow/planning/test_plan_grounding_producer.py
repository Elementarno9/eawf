"""Criterion grounding is graded where a plan producer's proposal is admitted.

A planner emits criteria without a grounding grade, and the grade defaults
to ``assumed``, which the approval gate refuses. These tests submit a plan
exactly as a planner emits it -- no ``grounding`` key anywhere -- through
the real ``PLAN-SUBMIT`` and ``PLAN-APPROVE`` daemon verbs, and assert that
a criterion bound to a probe gate is graded ``measured`` and approves while
a criterion with no probe stays ``assumed`` and is refused.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import (
    CriterionGrounding,
    CriterionSpec,
    grade_criterion_grounding,
)
from eawf.kernel.store.compaction import read_document
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.planning import PLAN_APPROVE_METHOD, PLAN_SUBMIT_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)

pytestmark = pytest.mark.integration

SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
REPOSITORY_URN: Final = f"{SLOT}/repository/REP-EAWF"
ACTION_URN: Final = f"{SLOT}/pending-action/ACT-0061"
HEAD: Final = "b" * 40
ACTOR: Final = "OP-0001"
REVISION_KEY: Final = "PRV-0061"
OPERATOR: Final[dict[str, str]] = {"principal_kind": "operator", "principal_id": ACTOR}
V1_STATE_FIXTURE: Final = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _planned_canary(tmp_path: Path, *, code: str) -> CanaryProvision:
    """Provision a canary holding the Track, the repository and a v1 state.json.

    The v1 state makes the approval gate's citation resolvers live, as
    they are in a production ``.ea/`` tree.
    """
    canary = provision(tmp_path / code.lower(), code=code)
    seed(
        canary,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "repository": {"REP-EAWF": {"key": "REP-EAWF", "head_sha": HEAD}},
        },
    )
    (tree_root(canary) / "state.json").write_text(
        V1_STATE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return canary


def _criterion(criterion_id: str, *, gate_kind: str | None, **overrides: Any) -> dict[str, Any]:
    """Return a planner-emitted criterion payload, with no grounding grade.

    Args:
        criterion_id: The criterion id.
        gate_kind: The gate kind the response clause names, or ``None``
            for a criterion that binds no gate at all.
        **overrides: Fields replacing the defaults.
    """
    payload: dict[str, Any] = {
        "id": criterion_id,
        "text": "the planning suite exits zero",
        "kind": "behavioral",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "quality_dimension": "functional_suitability",
        "measurable_signal": "uv run pytest tests/unit/workflow/planning -q exits zero",
    }
    if gate_kind is not None:
        payload["gate_ids"] = [f"G-{criterion_id[-2:]}"]
        payload["response"] = {
            "observe": "exits",
            "object": "zero from the planning suite",
            "locus": "pytest",
            "gate_ref": gate_kind,
        }
    payload.update(overrides)
    return payload


def _body(*criteria: dict[str, Any]) -> dict[str, Any]:
    """Return a single-Task plan body payload carrying *criteria*."""
    return {
        "milestone_urn": MILESTONE_URN,
        "milestone": {
            "key": "MLS-0030",
            "primary_track_ref": f"{SLOT}/track/TRK-RUNTIME",
            "title": "Ship a gated plan",
            "outcome": "An operator observes the described behaviour end to end.",
            "appetite": "S",
            "exclusions": ["none"],
            "acceptance_journey": [
                {
                    "step_id": "AS-01",
                    "actor": "operator",
                    "action": "observe the described behaviour",
                    "expected_observation": "the behaviour is present",
                    "evidence_kinds": ["artifact"],
                }
            ],
        },
        "batches": [{"urn": BATCH_URN, "repository_ref": REPOSITORY_URN}],
        "tasks": [
            {
                "urn": TASK_URN,
                "batch_ref": BATCH_URN,
                "priority": "P1",
                "intent": "do the described work",
                "criteria": list(criteria),
            }
        ],
        "citations": [],
    }


def _dispatch(
    method: str, canary: CanaryProvision, tmp_path: Path, *, params: dict[str, Any]
) -> dict[str, Any]:
    """Drive one planning verb against *canary* and return the envelope."""
    ctx = method_context(tmp_path / "runtime")
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def _submit_and_approve(
    canary: CanaryProvision, tmp_path: Path, *, body: dict[str, Any]
) -> dict[str, Any]:
    """Submit *body*, assert the submission lands, and return the approval envelope."""
    submitted = _dispatch(
        PLAN_SUBMIT_METHOD,
        canary,
        tmp_path,
        params={
            "proposal": {"key": REVISION_KEY, "author": OPERATOR, "body": body},
            "actor": ACTOR,
            "idempotency_key": "req-submit",
        },
    )
    assert submitted["status"] == "ok", submitted
    return _dispatch(
        PLAN_APPROVE_METHOD,
        canary,
        tmp_path,
        params={
            "key": REVISION_KEY,
            "expected_revision": 2,
            "action_ref": ACTION_URN,
            "approved_by": OPERATOR,
            "actor": ACTOR,
            "idempotency_key": "req-approve",
        },
    )


def _stored_grades(canary: CanaryProvision) -> dict[str, str]:
    """Return each stored criterion's grounding grade, keyed by criterion id."""
    rows = read_document(document_path(canary))["plan_revision"]
    criteria = rows[REVISION_KEY]["body"]["tasks"][0]["criteria"]
    return {row["id"]: row["grounding"] for row in criteria}


def _assert_refused(answer: dict[str, Any], *, naming: str) -> None:
    """Assert *answer* is the grounding refusal and names criterion *naming*."""
    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "transition_guard_failed"
    assert answer["errors"][0]["guard"] == "criteria_grounded_at_approval"
    assert naming in json.dumps(answer)


# ---- gate-fire proof: submit grades, approve admits or refuses -------------


def test_submit_plan_revision_grades_a_probe_gated_criterion_measured_and_approves(
    tmp_path: Path,
) -> None:
    """Gate-fire proof: a planner's gated criterion reaches APPROVED.

    Removing the grading from ``validate_plan_proposal`` leaves the row
    ``assumed`` and the approval refuses, which reds this test.
    """
    canary = _planned_canary(tmp_path, code="GRDPROBE")

    answer = _submit_and_approve(
        canary, tmp_path, body=_body(_criterion("CR-01", gate_kind="command_exit_zero"))
    )

    assert _stored_grades(canary) == {"CR-01": "measured"}
    assert answer["status"] == "ok", answer


def test_submit_plan_revision_leaves_an_unprobed_criterion_assumed_and_refuses(
    tmp_path: Path,
) -> None:
    """A criterion with no gate has nothing to be measured by, so it stays refused."""
    canary = _planned_canary(tmp_path, code="GRDNONE")

    answer = _submit_and_approve(canary, tmp_path, body=_body(_criterion("CR-01", gate_kind=None)))

    assert _stored_grades(canary) == {"CR-01": "assumed"}
    _assert_refused(answer, naming="CR-01")


def test_submit_plan_revision_refuses_a_plan_mixing_a_probed_and_an_unprobed_criterion(
    tmp_path: Path,
) -> None:
    """One unprobed criterion still blocks the whole plan, and only it is named."""
    canary = _planned_canary(tmp_path, code="GRDMIXED")
    body = _body(
        _criterion("CR-01", gate_kind="command_exit_zero"),
        _criterion("CR-02", gate_kind=None),
    )

    answer = _submit_and_approve(canary, tmp_path, body=body)

    assert _stored_grades(canary) == {"CR-01": "measured", "CR-02": "assumed"}
    _assert_refused(answer, naming="CR-02")
    assert f"{TASK_URN}/CR-01" not in json.dumps(answer)


def test_submit_plan_revision_leaves_a_static_gate_criterion_assumed(tmp_path: Path) -> None:
    """Boundary: a T1 static gate only reads source text, so it is not a probe."""
    canary = _planned_canary(tmp_path, code="GRDSTATIC")
    criterion = _criterion(
        "CR-01",
        gate_kind="regex_in_file",
        response={
            "observe": "matches_pattern",
            "object": "the handler name in the module",
            "locus": "source",
            "gate_ref": "regex_in_file",
        },
    )

    answer = _submit_and_approve(canary, tmp_path, body=_body(criterion))

    assert _stored_grades(canary) == {"CR-01": "assumed"}
    _assert_refused(answer, naming="CR-01")


def test_submit_plan_revision_keeps_an_explicit_accepted_risk_grade(tmp_path: Path) -> None:
    """An authored grade is the author's; grading never overwrites it."""
    canary = _planned_canary(tmp_path, code="GRDKEEP")
    criterion = _criterion(
        "CR-01",
        gate_kind="command_exit_zero",
        grounding="accepted-risk",
        accepted_risk_decision_ref="DEC-UNRECORDED",
    )

    answer = _submit_and_approve(canary, tmp_path, body=_body(criterion))

    assert _stored_grades(canary) == {"CR-01": "accepted-risk"}
    _assert_refused(answer, naming="CR-01")


# ---- the grading predicate: boundaries and error paths ---------------------


def _spec(**overrides: Any) -> CriterionSpec:
    """Return a validated criterion bound to a command gate, with *overrides*."""
    return CriterionSpec.model_validate(
        _criterion("CR-01", gate_kind="command_exit_zero", **overrides)
    )


def test_grade_criterion_grounding_grades_measured_for_a_command_gate() -> None:
    assert grade_criterion_grounding(_spec()).grounding is CriterionGrounding.MEASURED


def test_grade_criterion_grounding_stays_assumed_with_empty_gate_ids() -> None:
    """Boundary: a response clause naming a kind binds no gate without a gate id."""
    assert grade_criterion_grounding(_spec(gate_ids=[])).grounding is CriterionGrounding.ASSUMED


def test_grade_criterion_grounding_stays_assumed_for_a_waived_criterion() -> None:
    """A waived row never runs its gate."""
    assert (
        grade_criterion_grounding(_spec(waiver_reason="deferred to the next wave")).grounding
        is CriterionGrounding.ASSUMED
    )


def test_grade_criterion_grounding_stays_assumed_for_jury_evidence() -> None:
    assert (
        grade_criterion_grounding(_spec(evidence_kind="jury")).grounding
        is CriterionGrounding.ASSUMED
    )


def test_grade_criterion_grounding_stays_assumed_for_an_unknown_gate_kind() -> None:
    """Boundary: a gate kind outside the tier table is not trusted as a probe."""
    response = {"observe": "exits", "object": "zero", "locus": "pytest", "gate_ref": "no_such"}
    assert (
        grade_criterion_grounding(_spec(response=response)).grounding is CriterionGrounding.ASSUMED
    )


def test_grade_criterion_grounding_upgrades_only_an_assumed_probed_row() -> None:
    graded = grade_criterion_grounding(_spec())
    assert graded.grounding is CriterionGrounding.MEASURED
    unprobed = CriterionSpec.model_validate(_criterion("CR-01", gate_kind=None))
    assert grade_criterion_grounding(unprobed) is unprobed


def test_criterion_spec_measured_without_contract_or_probe_raises() -> None:
    """Error path: measured with neither a contract nor a probe gate is refused."""
    with pytest.raises(ValidationError, match="nor binds a probe gate"):
        CriterionSpec.model_validate(_criterion("CR-01", gate_kind=None, grounding="measured"))


def test_criterion_spec_measured_by_probe_gate_validates_without_contract_refs() -> None:
    """A stored measured-by-probe row round-trips through validation."""
    criterion = _spec(grounding="measured")
    assert criterion.contract_refs == ()
    assert CriterionSpec.model_validate(criterion.model_dump(mode="json")) == criterion
