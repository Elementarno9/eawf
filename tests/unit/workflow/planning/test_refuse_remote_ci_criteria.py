"""Criteria and acceptance steps proved only by remote CI, a PR, a push or a tag.

A Milestone closes before its branch is pushed, so a criterion whose proof
reads CI runs, a pull request, a push or an observed tag can never be
evidenced inside the work. The detector names why; plan submit and apply
and Task promotion refuse on it; reading a stored record that already
carries one still validates.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.spec.remote_proof import criteria_needing_remote_ci, remote_proof_reason
from eawf.kernel.state.epoch2.lifecycle import (
    TransitionAccepted,
    TransitionDenied,
    apply_transition,
)
from eawf.kernel.state.epoch2.plan_revision import (
    PlanApproval,
    PlanBody,
    PlanRevision,
    PlanRevisionStatus,
    plan_content_digest,
)
from eawf.kernel.state.epoch2.task import Task, TaskStatus
from eawf.kernel.state.epoch2.transitions import (
    DENIAL_REMEDIATION,
    DenialCode,
    LifecycleEntity,
    row_for,
)
from eawf.workflow.planning.revision import (
    PlanRefusal,
    PlanRefusalCode,
    PlanRevisionAdvanced,
    advance_plan_revision,
    remote_ci_dependencies,
)
from tests.unit.kernel.state._epoch2_transition_fixtures import (
    seed_record,
    updates_for,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "planning"
    / "grounding"
    / "single_criterion_body.json"
)
AT = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
REPOSITORY = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
ACTION = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0031"
OPERATOR: dict[str, str] = {"principal_kind": "operator", "principal_id": "OP-0001"}

#: The two signals of the dropped Task that slipped through, verbatim.
EAWF_0398_SIGNAL_1 = (
    "`gh run list --workflow ci.yaml --event pull_request -L 10 --json createdAt,updatedAt` "
    "with the computed median recorded in the log."
)
EAWF_0398_SIGNAL_2 = (
    "`gh run list --workflow ci.yaml --branch main --event push -L 10 --json conclusion` "
    "prints 10 `success`."
)
AS_02_OBSERVATION = "the pre-flight passes and 0.7.0rc2 is tagged and observed"

LOCAL_SIGNAL = "`uv run pytest -q tests/unit/workflow/planning` exits 0."


def criterion(
    criterion_id: str = "CR-01",
    *,
    text: str = "the planning lens refuses a remote-only criterion",
    signal: str = LOCAL_SIGNAL,
) -> dict[str, Any]:
    """Return a criterion payload with the given text and signal."""
    return {
        "id": criterion_id,
        "text": text,
        "kind": "functional_suitability",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "quality_dimension": "functional_suitability",
        "measurable_signal": signal,
    }


def plan_body(
    *, criteria: list[dict[str, Any]] | None = None, observation: str | None = None
) -> PlanBody:
    """Return the single-criterion fixture plan with its criteria or step replaced."""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if criteria is not None:
        payload["tasks"][0]["criteria"] = criteria
    if observation is not None:
        payload["milestone"]["acceptance_journey"][0]["expected_observation"] = observation
    return PlanBody.model_validate(payload)


def revision(body: PlanBody, *, status: PlanRevisionStatus) -> PlanRevision:
    """Return a revision over *body* at *status*, sealed when the status needs it."""
    approval = (
        PlanApproval.model_validate(
            {
                "action_ref": ACTION,
                "approved_by": OPERATOR,
                "approved_at": AT.isoformat(),
                "content_digest": plan_content_digest(body),
                "base_state_revision": 1,
                "policy_revision": 1,
                "head_bindings": [{"repository_ref": REPOSITORY, "head_sha": "a" * 40}],
            }
        ).model_dump(mode="json")
        if status is PlanRevisionStatus.APPROVED
        else None
    )
    return PlanRevision.model_validate(
        {
            "key": "PRV-0446",
            "revision": 1,
            "status": status.value,
            "author": OPERATOR,
            "created_at": AT.isoformat(),
            "updated_at": AT.isoformat(),
            "content_digest": plan_content_digest(body),
            "base_state_revision": 1,
            "policy_revision": 1,
            "head_bindings": [{"repository_ref": REPOSITORY, "head_sha": "a" * 40}],
            "body": body.model_dump(mode="json"),
            "approval": approval,
        }
    )


# ---- the detector -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "matched"),
    [
        (EAWF_0398_SIGNAL_1, "gh run"),
        (EAWF_0398_SIGNAL_2, "gh run"),
        (AS_02_OBSERVATION, "0.7.0rc2 is tagged"),
        ("`gh pr view 12 --json state` prints `MERGED`", "gh pr"),
        ("`gh workflow view ci.yaml` lists the job", "gh workflow"),
        ("`git push origin feature/eawf-v0.7` exits 0", "git push"),
        (
            "The last 10 consecutive `push` runs on `main` concluded success.",
            "last 10 consecutive `push` runs",
        ),
        (
            "The median wall time of the last 10 PR runs of `ci.yaml` is under 15 min",
            "last 10 PR runs",
        ),
        ("the last 3 CI runs on main took under 15 min", "last 3 CI runs"),
        ("CI is green on the pushed branch", "CI is green"),
        ("requires CI to stay green", "CI to stay green"),
        ("CI stays green after the merge", "CI stays green"),
        ("CI must be green before the tag", "CI must be green"),
        ("one dispatched CI run shows 3 ubuntu shard jobs", "dispatched CI run"),
        ("`gh workflow run ci.yaml` dispatches the job", "gh workflow"),
        ("GitHub Actions run conclusions read success", "GitHub Actions run conclusions"),
        ("`git push -u origin main` exits 0", "git push"),
        ("the wheel is published to PyPI", "published to PyPI"),
        ("the plugin is published to npm", "published to npm"),
        ("`git merge-base --is-ancestor HEAD origin/main` exits 0", "--is-ancestor HEAD origin/"),
        ("publish 0.7.0", "publish 0.7.0"),
        ("every publication target is observed", "publication target is observed"),
    ],
)
def test_remote_shapes_are_named_with_the_matched_text(text: str, matched: str) -> None:
    reason = remote_proof_reason(text)
    assert reason is not None
    assert repr(matched) in reason


@pytest.mark.parametrize(
    "text",
    [
        LOCAL_SIGNAL,
        "`uv run pytest -q tests/unit/kernel/spec/test_remote_proof.py -k push` exits 0",
        "the down arrow pushes the cursor one row",
        "a push notification appears when the Run settles",
        "Land the Batch commits before a push",
        "`commit-prefix-lint` is registered at `stages: [pre-push]`",
        "a pre-push runs the lint over the range",
        "The pushed-commit lint accepts a Batch commit with one Batch trailer",
        "`git diff --quiet origin/main -- src/eawf/schemas` exits 0",
        "`uv run eawf hook ledger-history --range origin/main..HEAD` exits 0",
        "an official Python SDK if one is published",
        "`eawf publish` leaves exactly three new commits on the branch",
        "the commit body states whether the CI flake reproduced",
        "a workflow-shape test parsing `ci.yaml` asserts the condition",
        "asserts ci.yaml declares 3 CI jobs",
        "CI build matrix covers 3.14",
        "push CI jobs keep 3 shards",
        "publishes 2.0 s",
        "the dispatched Run settles and its report binds",
        "`git push --dry-run` is never invoked",
        "`git push -n origin main` lists what would be sent",
        "the last 5 runs of the local benchmark agree within 2 %",
    ],
)
def test_local_prose_and_signals_are_not_named(text: str) -> None:
    assert remote_proof_reason(text) is None


def test_an_empty_signal_and_no_text_are_not_named() -> None:
    assert remote_proof_reason("") is None
    assert remote_proof_reason() is None


@pytest.mark.parametrize("text", ["GH RUN list -L 1", "Git Push origin main", "gh  pr checks"])
def test_mixed_case_and_spacing_still_match(text: str) -> None:
    assert remote_proof_reason(text) is not None


@pytest.mark.parametrize("text", ["ghrun list", "gh runner list", "git pushd /tmp"])
def test_a_command_word_boundary_is_required(text: str) -> None:
    assert remote_proof_reason(text) is None


def test_a_match_at_the_end_of_a_max_length_signal_is_found() -> None:
    tail = " then `gh run view` prints success"
    signal = "x" * (300 - len(tail)) + tail
    assert len(signal) == 300
    spec = CriterionSpec.model_validate(criterion(signal=signal))
    assert criteria_needing_remote_ci([spec]) == (f"CR-01: {remote_proof_reason(signal)}",)


def test_any_of_several_texts_is_enough() -> None:
    assert remote_proof_reason("run the rc2 release pre-flight", AS_02_OBSERVATION) is not None


def test_criteria_scan_is_empty_for_none_and_names_only_offenders_in_order() -> None:
    assert criteria_needing_remote_ci([]) == ()
    specs = [
        CriterionSpec.model_validate(criterion("CR-1", signal=EAWF_0398_SIGNAL_2)),
        CriterionSpec.model_validate(criterion("CR-2")),
        CriterionSpec.model_validate(criterion("CR-3", text="CI is green on main")),
    ]
    named = criteria_needing_remote_ci(specs)
    assert [entry.split(":")[0] for entry in named] == ["CR-1", "CR-3"]


def test_a_non_string_text_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        remote_proof_reason(42)  # type: ignore[arg-type]


# ---- plan submit and apply -------------------------------------------------


def test_plan_submit_refuses_naming_the_criterion() -> None:
    body = plan_body(criteria=[criterion("CR-0398-1", signal=EAWF_0398_SIGNAL_1)])
    refused = advance_plan_revision(
        revision(body, status=PlanRevisionStatus.DRAFT), to=PlanRevisionStatus.VALIDATED, at=AT
    )
    assert isinstance(refused, PlanRefusal)
    assert refused.code is PlanRefusalCode.TRANSITION_GUARD_FAILED
    assert refused.guard == "criterion_needs_remote_ci"
    assert "EAWF-0031/CR-0398-1" in refused.detail
    assert "gh run" in refused.detail


def test_plan_apply_refuses_naming_the_criterion() -> None:
    body = plan_body(criteria=[criterion("CR-0398-2", signal=EAWF_0398_SIGNAL_2)])
    refused = advance_plan_revision(
        revision(body, status=PlanRevisionStatus.APPROVED), to=PlanRevisionStatus.APPLIED, at=AT
    )
    assert isinstance(refused, PlanRefusal)
    assert refused.guard == "criterion_needs_remote_ci"
    assert "EAWF-0031/CR-0398-2" in refused.detail


def test_plan_submit_refuses_naming_the_acceptance_step() -> None:
    body = plan_body(observation=AS_02_OBSERVATION)
    refused = advance_plan_revision(
        revision(body, status=PlanRevisionStatus.DRAFT), to=PlanRevisionStatus.VALIDATED, at=AT
    )
    assert isinstance(refused, PlanRefusal)
    assert refused.guard == "criterion_needs_remote_ci"
    assert refused.detail.startswith("AS-01: needs an observed tag or publication")


def test_a_locally_provable_plan_submits() -> None:
    body = plan_body(criteria=[criterion()])
    assert remote_ci_dependencies(body) == ()
    advanced = advance_plan_revision(
        revision(body, status=PlanRevisionStatus.DRAFT), to=PlanRevisionStatus.VALIDATED, at=AT
    )
    assert isinstance(advanced, PlanRevisionAdvanced)


def test_superseding_a_plan_is_not_refused_for_its_criteria() -> None:
    body = plan_body(criteria=[criterion(signal=EAWF_0398_SIGNAL_1)])
    advanced = advance_plan_revision(
        revision(body, status=PlanRevisionStatus.VALIDATED), to=PlanRevisionStatus.SUPERSEDED, at=AT
    )
    assert isinstance(advanced, PlanRevisionAdvanced)


# ---- Task promotion ---------------------------------------------------------


def promote(criteria: list[dict[str, Any]]) -> tuple[Task, Any]:
    """Promote the seeded DRAFT Task with *criteria*, returning it and the outcome."""
    draft = seed_record(LifecycleEntity.TASK, TaskStatus.DRAFT)
    assert isinstance(draft, Task)
    row = row_for(LifecycleEntity.TASK, TaskStatus.DRAFT, TaskStatus.PLANNED)
    assert row is not None
    updates = {**updates_for(row), "criteria": criteria}
    return draft, apply_transition(draft, to=TaskStatus.PLANNED, at=AT, updates=updates)


def test_promotion_refuses_naming_the_criterion() -> None:
    draft, outcome = promote(
        [criterion("CR-0398-1", signal=EAWF_0398_SIGNAL_1), criterion("CR-0398-2")]
    )
    assert isinstance(outcome, TransitionDenied)
    assert outcome.code is DenialCode.CRITERION_NEEDS_REMOTE_CI
    assert outcome.guard is None
    assert outcome.remediation == DENIAL_REMEDIATION[DenialCode.CRITERION_NEEDS_REMOTE_CI]
    assert "CR-0398-1" in outcome.message
    assert "CR-0398-2" not in outcome.message
    assert draft.status is TaskStatus.DRAFT


def test_promotion_with_local_criteria_is_accepted() -> None:
    _, outcome = promote([criterion()])
    assert isinstance(outcome, TransitionAccepted)
    assert outcome.record.status is TaskStatus.PLANNED


# ---- reading stays permissive -----------------------------------------------


def test_a_stored_task_carrying_a_remote_criterion_still_validates() -> None:
    planned = seed_record(LifecycleEntity.TASK, TaskStatus.PLANNED).model_dump(mode="json")
    planned["criteria"] = [
        criterion("CR-0398-1", signal=EAWF_0398_SIGNAL_1),
        criterion("CR-0398-2", signal=EAWF_0398_SIGNAL_2),
    ]
    stored = Task.model_validate(planned)
    assert [item.id for item in stored.criteria] == ["CR-0398-1", "CR-0398-2"]


def test_a_stored_plan_revision_carrying_a_remote_step_still_validates() -> None:
    body = plan_body(criteria=[criterion(signal=EAWF_0398_SIGNAL_2)], observation=AS_02_OBSERVATION)
    stored = revision(body, status=PlanRevisionStatus.VALIDATED)
    assert len(remote_ci_dependencies(stored.body)) == 2
