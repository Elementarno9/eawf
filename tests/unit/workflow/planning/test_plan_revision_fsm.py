"""The plan-revision machine, its approval rule, and the DOM-035 guard.

Three things are asserted here and nothing else needs a daemon to check
them, because every rule under test is a pure function of a record.

The machine admits exactly the registered edges. ``APPLIED`` is reachable
from ``APPROVED`` and from nowhere else, which is what makes "an
unapproved plan cannot be applied" a property of the code rather than of
the caller's discipline.

Approval is bound to content. The receipt names a digest, every check
recomputes the digest from the stored body, and a body edited after
approval therefore fails without anyone having to spot the edit.

A citation is provenance. Adding any number of cited Campaign findings to
a plan leaves every Milestone count byte-identical, and a citation that
addresses anything but a promoted finding is refused at the loader.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.plan_revision import (
    PLAN_REVISION_EDGES,
    TERMINAL_PLAN_REVISION_STATUSES,
    CampaignCitation,
    PlanApproval,
    PlanBody,
    PlanRevision,
    PlanRevisionStatus,
    milestone_counts,
    plan_content_digest,
)
from eawf.workflow.planning.revision import (
    ObservedPlanWorld,
    PlanRefusal,
    PlanRefusalCode,
    PlanRevisionAdvanced,
    advance_plan_revision,
    citations_are_uncounted,
    detect_plan_drift,
)

pytestmark = pytest.mark.unit

AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
TRACK = f"{SLOT}/track/TRK-RUNTIME"
MILESTONE = f"{SLOT}/milestone/MLS-0030"
BATCH = f"{SLOT}/batch/BAT-0007"
TASK = f"{SLOT}/task/EAWF-0042"
REPOSITORY = f"{SLOT}/repository/REP-EAWF"
FINDING = f"{SLOT}/campaign-finding/CFN-0001"
ACTION = f"{SLOT}/pending-action/ACT-0001"
HEAD = "a" * 40

CRITERION: dict[str, Any] = {
    "id": "CR-01",
    "text": "the published wheel installs into a clean environment",
    "kind": "functional_suitability",
    "acceptance_style": "binary",
    "evidence_kind": "deterministic",
    "quality_dimension": "functional_suitability",
    "measurable_signal": "uv run pytest tests/unit/kernel/state exits zero",
    "grounding": "measured",
    "contract_refs": ["MCT-0001"],
}

OPERATOR: dict[str, str] = {"principal_kind": "operator", "principal_id": "OP-0001"}


def body_payload(**overrides: Any) -> dict[str, Any]:
    """Return a loader-valid plan body payload with *overrides* applied."""
    payload: dict[str, Any] = {
        "milestone_urn": MILESTONE,
        "milestone": {
            "key": "MLS-0030",
            "primary_track_ref": TRACK,
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
            "required_batch_refs": [BATCH],
        },
        "batches": [{"urn": BATCH, "repository_ref": REPOSITORY}],
        "tasks": [
            {
                "urn": TASK,
                "batch_ref": BATCH,
                "priority": "P1",
                "intent": "build and publish the wheel",
                "criteria": [CRITERION],
            }
        ],
        "citations": [],
    }
    payload.update(overrides)
    return payload


def make_body(**overrides: Any) -> PlanBody:
    """Return a validated plan body."""
    return PlanBody.model_validate(body_payload(**overrides))


def make_revision(
    status: PlanRevisionStatus = PlanRevisionStatus.DRAFT,
    *,
    body: PlanBody | None = None,
    approval: PlanApproval | None = None,
    **overrides: Any,
) -> PlanRevision:
    """Return a validated revision at *status*."""
    content = body if body is not None else make_body()
    payload: dict[str, Any] = {
        "key": "PRV-0001",
        "revision": 1,
        "status": status.value,
        "author": OPERATOR,
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
        "content_digest": plan_content_digest(content),
        "base_state_revision": 1,
        "policy_revision": 1,
        "head_bindings": [{"repository_ref": REPOSITORY, "head_sha": HEAD}],
        "body": content.model_dump(mode="json"),
        "approval": None if approval is None else approval.model_dump(mode="json"),
    }
    payload.update(overrides)
    return PlanRevision.model_validate(payload)


def make_approval(revision: PlanRevision, **overrides: Any) -> PlanApproval:
    """Return the receipt a human principal seals over *revision*."""
    payload: dict[str, Any] = {
        "action_ref": ACTION,
        "approved_by": OPERATOR,
        "approved_at": AT.isoformat(),
        "content_digest": plan_content_digest(revision.body),
        "base_state_revision": revision.base_state_revision,
        "policy_revision": revision.policy_revision,
        "head_bindings": [item.model_dump(mode="json") for item in revision.head_bindings],
    }
    payload.update(overrides)
    return PlanApproval.model_validate(payload)


def matching_world(revision: PlanRevision, **overrides: Any) -> ObservedPlanWorld:
    """Return the observation under which no binding of *revision* drifted."""
    fields: dict[str, Any] = {
        "track_revision": revision.base_state_revision,
        "track_status": "ACTIVE",
        "policy_revision": revision.policy_revision,
        "heads": {str(item.repository_ref): item.head_sha for item in revision.head_bindings},
        "claimed_keys": (),
    }
    fields.update(overrides)
    return ObservedPlanWorld(**fields)


def approved_revision(**body_overrides: Any) -> PlanRevision:
    """Return a revision sitting at APPROVED with a matching receipt."""
    validated = make_revision(PlanRevisionStatus.VALIDATED, body=make_body(**body_overrides))
    approval = make_approval(validated)
    outcome = advance_plan_revision(
        validated, to=PlanRevisionStatus.APPROVED, at=AT, approval=approval
    )
    assert isinstance(outcome, PlanRevisionAdvanced)
    return outcome.record


# ---- the machine -------------------------------------------------------------


def test_advance_plan_revision_walks_draft_to_applied() -> None:
    """The one legal path runs end to end and numbers each step."""
    draft = make_revision(PlanRevisionStatus.DRAFT)

    validated = advance_plan_revision(draft, to=PlanRevisionStatus.VALIDATED, at=AT)
    assert isinstance(validated, PlanRevisionAdvanced)
    assert validated.event_name == "planning.plan_revision.validated"
    assert (validated.record.status, validated.record.revision) == (
        PlanRevisionStatus.VALIDATED,
        2,
    )

    approval = make_approval(validated.record)
    approved = advance_plan_revision(
        validated.record, to=PlanRevisionStatus.APPROVED, at=AT, approval=approval
    )
    assert isinstance(approved, PlanRevisionAdvanced)
    assert approved.record.approval == approval
    assert approved.record.revision == 3

    applied = advance_plan_revision(approved.record, to=PlanRevisionStatus.APPLIED, at=AT)
    assert isinstance(applied, PlanRevisionAdvanced)
    assert applied.event_name == "planning.plan_revision.applied"
    assert (applied.record.status, applied.record.revision) == (PlanRevisionStatus.APPLIED, 4)
    assert applied.record.approval == approval


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (PlanRevisionStatus.DRAFT, PlanRevisionStatus.APPROVED),
        (PlanRevisionStatus.DRAFT, PlanRevisionStatus.APPLIED),
        (PlanRevisionStatus.VALIDATED, PlanRevisionStatus.APPLIED),
        (PlanRevisionStatus.VALIDATED, PlanRevisionStatus.DRAFT),
        (PlanRevisionStatus.APPROVED, PlanRevisionStatus.VALIDATED),
        (PlanRevisionStatus.APPLIED, PlanRevisionStatus.APPLIED),
    ],
)
def test_advance_plan_revision_refuses_an_unregistered_edge(
    source: PlanRevisionStatus, target: PlanRevisionStatus
) -> None:
    """An edge the table does not carry does not exist."""
    sealed = source in {PlanRevisionStatus.APPROVED, PlanRevisionStatus.APPLIED}
    record = make_revision(source) if not sealed else approved_revision()
    if source is PlanRevisionStatus.APPLIED:
        moved = advance_plan_revision(record, to=PlanRevisionStatus.APPLIED, at=AT)
        assert isinstance(moved, PlanRevisionAdvanced)
        record = moved.record

    refused = advance_plan_revision(record, to=target, at=AT)

    assert isinstance(refused, PlanRefusal)
    assert refused.code is PlanRefusalCode.ILLEGAL_TRANSITION
    assert refused.guard == "plan_revision_edge_registered"
    assert refused.revision == record.revision


@pytest.mark.parametrize("terminal", sorted(TERMINAL_PLAN_REVISION_STATUSES))
def test_advance_plan_revision_refuses_every_move_off_a_terminal(
    terminal: PlanRevisionStatus,
) -> None:
    """A finished revision moves nowhere at all."""
    record = make_revision(terminal)

    for target in PlanRevisionStatus:
        refused = advance_plan_revision(record, to=target, at=AT)
        assert isinstance(refused, PlanRefusal)
        assert refused.code is PlanRefusalCode.ILLEGAL_TRANSITION


def test_superseding_an_approved_revision_keeps_its_receipt() -> None:
    """Who approved a superseded plan stays readable after it is replaced."""
    approved = approved_revision()

    superseded = advance_plan_revision(approved, to=PlanRevisionStatus.SUPERSEDED, at=AT)

    assert isinstance(superseded, PlanRevisionAdvanced)
    assert superseded.record.status is PlanRevisionStatus.SUPERSEDED
    assert superseded.record.approval == approved.approval
    assert superseded.event_name == "planning.plan_revision.superseded"


def test_superseding_an_unapproved_revision_records_no_receipt() -> None:
    """A plan replaced before approval carries none, and gains none."""
    validated = make_revision(PlanRevisionStatus.VALIDATED)

    superseded = advance_plan_revision(validated, to=PlanRevisionStatus.SUPERSEDED, at=AT)

    assert isinstance(superseded, PlanRevisionAdvanced)
    assert superseded.record.approval is None


def test_rejecting_a_draft_records_no_receipt() -> None:
    """A rejected plan is a record of the refusal, never of an approval."""
    draft = make_revision(PlanRevisionStatus.DRAFT)

    rejected = advance_plan_revision(draft, to=PlanRevisionStatus.REJECTED, at=AT)

    assert isinstance(rejected, PlanRevisionAdvanced)
    assert rejected.record.status is PlanRevisionStatus.REJECTED
    assert rejected.event_name == "planning.plan_revision.rejected"


def test_plan_revision_edges_cover_every_status() -> None:
    """The table is total, so no status is a hole the machine falls into."""
    assert set(PLAN_REVISION_EDGES) == set(PlanRevisionStatus)


# ---- approval ----------------------------------------------------------------


def test_approving_without_a_receipt_is_refused() -> None:
    """An approval edge with no receipt records consent nobody gave."""
    validated = make_revision(PlanRevisionStatus.VALIDATED)

    refused = advance_plan_revision(validated, to=PlanRevisionStatus.APPROVED, at=AT)

    assert isinstance(refused, PlanRefusal)
    assert refused.code is PlanRefusalCode.PROTECTED_APPROVAL_REQUIRED
    assert refused.guard == "human_approval_sealed"


def test_a_receipt_on_another_edge_is_refused() -> None:
    """An approval arriving with a validation records consent nobody asked for."""
    draft = make_revision(PlanRevisionStatus.DRAFT)

    refused = advance_plan_revision(
        draft, to=PlanRevisionStatus.VALIDATED, at=AT, approval=make_approval(draft)
    )

    assert isinstance(refused, PlanRefusal)
    assert refused.code is PlanRefusalCode.SCHEMA_VALIDATION_FAILED
    assert refused.guard == "approval_only_on_approval_edge"


@pytest.mark.parametrize("principal_kind", ["team", "service"])
def test_approval_requires_a_human_principal(principal_kind: str) -> None:
    """A machine cannot accept the consequences of a plan."""
    revision = make_revision(PlanRevisionStatus.VALIDATED)

    with pytest.raises(ValidationError, match="operator principal"):
        make_approval(
            revision,
            approved_by={"principal_kind": principal_kind, "principal_id": "OP-0001"},
        )


def test_approval_must_address_a_pending_action() -> None:
    """A receipt pointed at anything else seals no protected action."""
    revision = make_revision(PlanRevisionStatus.VALIDATED)

    with pytest.raises(ValidationError, match="pending-action"):
        make_approval(revision, action_ref=MILESTONE)


def test_approval_bound_to_another_digest_is_refused() -> None:
    """A receipt naming a digest the body does not have approves nothing."""
    validated = make_revision(PlanRevisionStatus.VALIDATED)
    elsewhere = make_approval(validated, content_digest=f"sha256:{'b' * 64}")

    refused = advance_plan_revision(
        validated, to=PlanRevisionStatus.APPROVED, at=AT, approval=elsewhere
    )

    assert isinstance(refused, PlanRefusal)
    assert refused.code is PlanRefusalCode.PROTECTED_APPROVAL_REQUIRED
    assert refused.guard == "content_digest_bound"


def test_a_body_edited_after_approval_no_longer_matches_its_receipt() -> None:
    """Editing the plan after approval leaves the receipt bound to nothing."""
    approved = approved_revision()
    tampered = approved.model_copy(
        update={"body": make_body(tasks=[*body_payload()["tasks"], *_second_task()])}
    )

    drifted = detect_plan_drift(tampered, world=matching_world(tampered))

    assert drifted is not None
    assert drifted.code is PlanRefusalCode.PROTECTED_APPROVAL_REQUIRED
    assert drifted.guard == "content_digest_bound"


def test_a_relabelled_digest_field_does_not_rescue_an_edited_body() -> None:
    """The digest is recomputed, so rewriting the recorded one changes nothing."""
    approved = approved_revision()
    edited = make_body(tasks=[*body_payload()["tasks"], *_second_task()])
    tampered = approved.model_copy(
        update={"body": edited, "content_digest": plan_content_digest(edited)}
    )

    drifted = detect_plan_drift(tampered, world=matching_world(tampered))

    assert drifted is not None
    assert drifted.guard == "content_digest_bound"


def _second_task() -> list[dict[str, Any]]:
    """Return one extra Task payload, used to make a body differ."""
    return [
        {
            "urn": f"{SLOT}/task/EAWF-0043",
            "batch_ref": BATCH,
            "priority": "P2",
            "intent": "smoke the published wheel",
            "criteria": [CRITERION],
        }
    ]


def test_plan_revision_requires_an_approval_from_approved_onward() -> None:
    """An approved record with no receipt is not a record at all."""
    with pytest.raises(ValidationError, match="requires an approval receipt"):
        make_revision(PlanRevisionStatus.APPROVED)


def test_plan_revision_refuses_a_receipt_below_approved() -> None:
    """A validated record carrying a receipt claims an approval nobody gave."""
    validated = make_revision(PlanRevisionStatus.VALIDATED)

    with pytest.raises(ValidationError, match="belongs to an approved revision"):
        make_revision(PlanRevisionStatus.VALIDATED, approval=make_approval(validated))


def test_detect_plan_drift_refuses_a_revision_with_no_approval() -> None:
    """Drift has no meaning without the bindings an approval carries."""
    validated = make_revision(PlanRevisionStatus.VALIDATED)

    with pytest.raises(ValueError, match="has no approval"):
        detect_plan_drift(validated, world=matching_world(validated))


# ---- DOM-035: a citation is provenance, never progress ----------------------


@pytest.mark.parametrize("count", [0, 1, 3])
def test_citations_never_move_a_milestone_count(count: int) -> None:
    """Every Milestone figure is derived from owned work alone."""
    citations = [
        {"finding_ref": f"{SLOT}/campaign-finding/CFN-{index:04d}", "note": "read the finding"}
        for index in range(1, count + 1)
    ]
    body = make_body(citations=citations)

    assert len(body.citations) == count
    assert milestone_counts(body) == milestone_counts(make_body())
    assert citations_are_uncounted(body)


def test_milestone_counts_read_the_owned_records() -> None:
    """The single-Batch, single-Task boundary counts one of each."""
    counts = milestone_counts(make_body(citations=[{"finding_ref": FINDING, "note": "cited"}]))

    assert counts.batch_count == 1
    assert counts.task_count == 1
    assert counts.required_batch_count == 1
    assert counts.acceptance_step_count == 1


@pytest.mark.parametrize("ref", [TASK, BATCH, MILESTONE, TRACK])
def test_a_citation_addressing_delivery_work_is_refused(ref: str) -> None:
    """A delivery edge wearing a provenance label is refused at the loader."""
    with pytest.raises(ValidationError, match="campaign-finding"):
        CampaignCitation.model_validate({"finding_ref": ref, "note": "not a finding"})


def test_a_repeated_citation_is_refused() -> None:
    """One finding cited twice would double-count itself in any traversal."""
    twice = [{"finding_ref": FINDING, "note": "first"}, {"finding_ref": FINDING, "note": "again"}]

    with pytest.raises(ValidationError, match="citations names the same record twice"):
        make_body(citations=twice)


# ---- plan body resolution ---------------------------------------------------


def test_a_task_naming_an_undeclared_batch_is_refused() -> None:
    """Every reference inside a plan resolves inside the plan."""
    orphan = [{**body_payload()["tasks"][0], "batch_ref": f"{SLOT}/batch/BAT-0099"}]

    with pytest.raises(ValidationError, match="a Batch the plan does not create"):
        make_body(tasks=orphan)


@pytest.mark.parametrize("field", ["batches", "tasks"])
def test_a_plan_creating_nothing_is_refused(field: str) -> None:
    """The empty boundary: a plan that materialises nothing is not a plan."""
    with pytest.raises(ValidationError, match="at least one"):
        make_body(**{field: []})


def test_a_milestone_urn_disagreeing_with_its_key_is_refused() -> None:
    """A plan that addresses one Milestone and keys another resolves to two."""
    with pytest.raises(ValidationError, match="but addresses"):
        make_body(milestone_urn=f"{SLOT}/milestone/MLS-0031")


def test_a_required_batch_the_plan_does_not_create_is_refused() -> None:
    """Acceptance cannot depend on a Batch nobody creates."""
    payload = body_payload()
    payload["milestone"]["required_batch_refs"] = [f"{SLOT}/batch/BAT-0099"]

    with pytest.raises(ValidationError, match="Batches the plan does not create"):
        PlanBody.model_validate(payload)


@pytest.mark.parametrize("key", ["PRV-1", "PRV-", "prv-0001", "MLS-0030", ""])
def test_a_malformed_plan_revision_key_is_refused(key: str) -> None:
    """The key grammar is closed, so a near-miss is a rejection."""
    with pytest.raises(ValidationError):
        make_revision(PlanRevisionStatus.DRAFT, key=key)


@pytest.mark.parametrize("key", ["PRV-0001", "PRV-99999"])
def test_a_wider_ordinal_is_still_a_plan_revision_key(key: str) -> None:
    """The ordinal is at least four digits and grows without a regrammar."""
    assert make_revision(PlanRevisionStatus.DRAFT, key=key).key == key


def test_a_revision_cannot_be_its_own_parent() -> None:
    """A repair chain of length one is a cycle, not a chain."""
    with pytest.raises(ValidationError, match="names itself as its parent"):
        make_revision(PlanRevisionStatus.DRAFT, parent_key="PRV-0001")


# ---- the four bound inputs ---------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "code", "guard"),
    [
        (
            {"track_revision": 2},
            PlanRefusalCode.REVISION_CONFLICT,
            "base_state_revision_pinned",
        ),
        (
            {"track_revision": None},
            PlanRefusalCode.REVISION_CONFLICT,
            "base_state_revision_pinned",
        ),
        (
            {"track_status": "RETIRED"},
            PlanRefusalCode.TRANSITION_GUARD_FAILED,
            "track_active",
        ),
        (
            {"policy_revision": 2},
            PlanRefusalCode.TRANSITION_GUARD_FAILED,
            "policy_revision_pinned",
        ),
        (
            {"heads": {REPOSITORY: "b" * 40}},
            PlanRefusalCode.PROOF_STALE,
            "base_head_bindings_pinned",
        ),
        (
            {"heads": {}},
            PlanRefusalCode.PROOF_STALE,
            "base_head_bindings_pinned",
        ),
        (
            {"claimed_keys": ("milestone/MLS-0030",)},
            PlanRefusalCode.REVISION_CONFLICT,
            "plan_keys_unclaimed",
        ),
    ],
)
def test_each_drifted_binding_has_its_own_conflict(
    overrides: dict[str, Any], code: PlanRefusalCode, guard: str
) -> None:
    """One drifted input, one specific answer."""
    approved = approved_revision()

    drifted = detect_plan_drift(approved, world=matching_world(approved, **overrides))

    assert drifted is not None
    assert drifted.code is code
    assert drifted.guard == guard
    assert drifted.revision == approved.revision


def test_a_matching_world_drifts_nowhere() -> None:
    """The positive case: every binding still holds and the apply may run."""
    approved = approved_revision()

    assert detect_plan_drift(approved, world=matching_world(approved)) is None


# ---- the digest -------------------------------------------------------------


def test_the_digest_moves_with_any_change_to_the_body() -> None:
    """Two plans that differ anywhere digest differently."""
    first = make_body()
    second = make_body(citations=[{"finding_ref": FINDING, "note": "cited"}])

    assert plan_content_digest(first) != plan_content_digest(second)


def test_the_digest_is_stable_across_two_spellings_of_one_plan() -> None:
    """One plan digests the same however its payload keys were ordered."""
    payload = body_payload()
    reordered = dict(reversed(list(payload.items())))

    assert plan_content_digest(PlanBody.model_validate(payload)) == plan_content_digest(
        PlanBody.model_validate(reordered)
    )
