"""Milestone leaf objects, the exclusion marker, and the create document.

Three refusals carry most of the weight here. An exclusion list that is
empty, that repeats a phrase, or that puts the reserved marker beside a
real exclusion is refused, because each of those makes the answer to
"what did the author decide was out of scope" unreadable. An acceptance
journey whose steps repeat or run out of order is refused, because the
step ids are what an acceptance bundle cites. And the create document
refuses status, identity and revision outright, so a caller cannot
declare a Milestone into existence already accepted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.state.epoch2 import (
    EXCLUSIONS_NONE_MARKER,
    AcceptanceStep,
    DurationBudget,
    Milestone,
    MilestoneCreateSpec,
    MilestoneStatus,
    validate_acceptance_journey,
    validate_exclusions,
)

pytestmark = pytest.mark.unit

#: ``tests/fixtures/epoch2`` - three levels up lands on ``tests/``.
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "epoch2"

MILESTONE_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030"
TRACK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-RUNTIME"
OTHER_TRACK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-DOCS"
BATCH_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"

BINDING: dict[str, Any] = {
    "head_sha": "a" * 40,
    "tree_sha": "b" * 40,
    "contract_digest": "sha256:" + "c" * 64,
    "policy_revision": 1,
    "evidence_digest": "sha256:" + "d" * 64,
}


def _step(step_id: str = "AS-01", **overrides: Any) -> dict[str, Any]:
    """Build the field mapping of a valid acceptance step."""
    fields: dict[str, Any] = {
        "step_id": step_id,
        "actor": "operator",
        "action": "install the published wheel into a clean environment",
        "expected_observation": "the install completes and reports the version",
        "evidence_kinds": ["artifact"],
    }
    fields.update(overrides)
    return fields


def _create_document() -> dict[str, Any]:
    """Return the Milestone create document as a fresh mapping."""
    parsed = yaml.safe_load((FIXTURES / "milestone_create_spec.yaml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _milestone_fields(**overrides: Any) -> dict[str, Any]:
    """Build the field mapping of a valid PLANNED Milestone."""
    fields: dict[str, Any] = {
        "uid": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "key": "MLS-0030",
        "urn": MILESTONE_URN,
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": "2026-09-08T00:00:00Z",
        "updated_at": "2026-09-08T00:00:00Z",
        "primary_track_ref": TRACK_URN,
        "title": "Publish an installable wheel",
        "outcome": "An operator installs the published wheel and the CLI answers.",
        "appetite": "M",
        "exclusions": ["platform packaging for Windows"],
        "acceptance_journey": [_step()],
        "status": "PLANNED",
    }
    fields.update(overrides)
    return fields


# ---- exclusions -------------------------------------------------------------


def test_validate_exclusions_rejects_an_empty_tuple() -> None:
    with pytest.raises(ValueError, match="exclusions must be non-empty"):
        validate_exclusions(())


def test_validate_exclusions_accepts_the_sole_reserved_marker() -> None:
    assert validate_exclusions((EXCLUSIONS_NONE_MARKER,)) == (EXCLUSIONS_NONE_MARKER,)


def test_validate_exclusions_accepts_a_single_real_exclusion() -> None:
    assert validate_exclusions(("windows packaging",)) == ("windows packaging",)


def test_validate_exclusions_rejects_a_normalized_duplicate() -> None:
    with pytest.raises(ValueError, match="exclusions repeats"):
        validate_exclusions(("Windows  packaging", "windows packaging"))


def test_validate_exclusions_rejects_the_marker_beside_a_real_exclusion() -> None:
    with pytest.raises(ValueError, match="cannot sit beside a real exclusion"):
        validate_exclusions(("windows packaging", EXCLUSIONS_NONE_MARKER))


def test_validate_exclusions_rejects_the_marker_in_any_casing_beside_a_real_one() -> None:
    with pytest.raises(ValueError, match="cannot sit beside a real exclusion"):
        validate_exclusions(("None", "windows packaging"))


def test_milestone_rejects_an_empty_exclusion_list() -> None:
    with pytest.raises(ValidationError, match="exclusions must be non-empty"):
        Milestone.model_validate(_milestone_fields(exclusions=[]))


def test_milestone_rejects_a_blank_exclusion_entry() -> None:
    with pytest.raises(ValidationError):
        Milestone.model_validate(_milestone_fields(exclusions=["   "]))


def test_milestone_retains_the_marker_in_canonical_serialisation() -> None:
    milestone = Milestone.model_validate(_milestone_fields(exclusions=[EXCLUSIONS_NONE_MARKER]))
    assert milestone.model_dump(mode="json")["exclusions"] == [EXCLUSIONS_NONE_MARKER]


# ---- acceptance steps -------------------------------------------------------


def test_acceptance_step_rejects_empty_evidence_kinds() -> None:
    with pytest.raises(ValidationError, match="at least one kind of proof"):
        AcceptanceStep.model_validate(_step(evidence_kinds=[]))


def test_acceptance_step_rejects_a_repeated_evidence_kind() -> None:
    with pytest.raises(ValidationError, match="repeats a kind"):
        AcceptanceStep.model_validate(_step(evidence_kinds=["artifact", "artifact"]))


def test_acceptance_step_rejects_an_unknown_evidence_kind() -> None:
    with pytest.raises(ValidationError):
        AcceptanceStep.model_validate(_step(evidence_kinds=["vibes"]))


def test_acceptance_step_rejects_a_malformed_step_id() -> None:
    with pytest.raises(ValidationError):
        AcceptanceStep.model_validate(_step(step_id="STEP-1"))


def test_acceptance_step_defaults_to_required() -> None:
    step = AcceptanceStep.model_validate(_step())
    assert step.required is True


def test_acceptance_step_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        AcceptanceStep.model_validate(_step(owner="OP-0001"))


def test_validate_acceptance_journey_rejects_an_empty_journey() -> None:
    with pytest.raises(ValueError, match="at least one step"):
        validate_acceptance_journey(())


def test_validate_acceptance_journey_rejects_a_repeated_step_id() -> None:
    steps = tuple(AcceptanceStep.model_validate(_step("AS-01")) for _ in range(2))
    with pytest.raises(ValueError, match="repeats a step id"):
        validate_acceptance_journey(steps)


def test_validate_acceptance_journey_rejects_steps_out_of_order() -> None:
    steps = (
        AcceptanceStep.model_validate(_step("AS-02")),
        AcceptanceStep.model_validate(_step("AS-01")),
    )
    with pytest.raises(ValueError, match="out of id order"):
        validate_acceptance_journey(steps)


def test_validate_acceptance_journey_accepts_an_ordered_journey() -> None:
    steps = (
        AcceptanceStep.model_validate(_step("AS-01")),
        AcceptanceStep.model_validate(_step("AS-02")),
    )
    assert validate_acceptance_journey(steps) == steps


def test_milestone_rejects_an_empty_acceptance_journey() -> None:
    with pytest.raises(ValidationError, match="at least one step"):
        Milestone.model_validate(_milestone_fields(acceptance_journey=[]))


# ---- the create document ----------------------------------------------------


def test_milestone_create_spec_loads_the_fixture_yaml_document() -> None:
    spec = MilestoneCreateSpec.model_validate(_create_document())
    assert spec.key == "MLS-0030"
    assert str(spec.primary_track_ref) == TRACK_URN
    assert len(spec.acceptance_journey) == 2
    assert [str(ref) for ref in spec.required_batch_refs] == [BATCH_URN]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "PLANNED"),
        ("uid", "1b4e28ba-2fa1-11d2-883f-0016d3cca427"),
        ("urn", MILESTONE_URN),
        ("revision", 1),
    ],
)
def test_milestone_create_spec_forbids_a_derived_field(field: str, value: Any) -> None:
    document = _create_document()
    document[field] = value
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        MilestoneCreateSpec.model_validate(document)


def test_milestone_create_spec_defaults_its_optional_fields() -> None:
    document = _create_document()
    del document["contributing_track_refs"]
    del document["description"]
    del document["required_batch_refs"]
    spec = MilestoneCreateSpec.model_validate(document)
    assert spec.contributing_track_refs == ()
    assert spec.description is None
    assert spec.required_batch_refs == ()


def test_milestone_create_spec_rejects_a_contributing_track_repeating_the_primary() -> None:
    document = _create_document()
    document["contributing_track_refs"] = [TRACK_URN]
    with pytest.raises(ValidationError, match="repeats the primary Track"):
        MilestoneCreateSpec.model_validate(document)


def test_milestone_create_spec_rejects_a_repeated_contributing_track() -> None:
    document = _create_document()
    document["contributing_track_refs"] = [OTHER_TRACK_URN, OTHER_TRACK_URN]
    with pytest.raises(ValidationError, match="contributing_track_refs names the same"):
        MilestoneCreateSpec.model_validate(document)


def test_milestone_create_spec_rejects_a_non_canonical_key() -> None:
    document = _create_document()
    document["key"] = "MLS-30"
    with pytest.raises(ValidationError, match="entity_key_invalid"):
        MilestoneCreateSpec.model_validate(document)


def test_milestone_create_spec_accepts_a_duration_appetite() -> None:
    document = _create_document()
    document["appetite"] = {"budget_kind": "duration", "amount": 2, "unit": "weeks"}
    spec = MilestoneCreateSpec.model_validate(document)
    assert isinstance(spec.appetite, DurationBudget)
    assert spec.appetite.unit == "weeks"


def test_milestone_create_spec_rejects_a_zero_duration_appetite() -> None:
    document = _create_document()
    document["appetite"] = {"budget_kind": "duration", "amount": 0, "unit": "weeks"}
    with pytest.raises(ValidationError):
        MilestoneCreateSpec.model_validate(document)


# ---- the stored record ------------------------------------------------------


def test_milestone_rejects_a_urn_addressing_another_milestone() -> None:
    other = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0031"
    with pytest.raises(ValidationError, match="carries a URN addressing"):
        Milestone.model_validate(_milestone_fields(urn=other))


def test_milestone_acceptance_review_requires_a_bundle_revision() -> None:
    with pytest.raises(ValidationError, match="requires acceptance_bundle_revision"):
        Milestone.model_validate(_milestone_fields(status="ACCEPTANCE_REVIEW"))


def test_milestone_completed_requires_an_accepted_binding() -> None:
    fields = _milestone_fields(status="COMPLETED", acceptance_bundle_revision=2)
    with pytest.raises(ValidationError, match="requires accepted_binding"):
        Milestone.model_validate(fields)


def test_milestone_completed_validates_with_its_full_acceptance_proof() -> None:
    milestone = Milestone.model_validate(
        _milestone_fields(
            status="COMPLETED",
            acceptance_bundle_revision=2,
            accepted_binding=BINDING,
        )
    )
    assert milestone.status is MilestoneStatus.COMPLETED
    assert milestone.accepted_binding is not None


def test_milestone_rejects_an_accepted_binding_before_completion() -> None:
    with pytest.raises(ValidationError, match="belongs to a COMPLETED Milestone"):
        Milestone.model_validate(_milestone_fields(accepted_binding=BINDING))


def test_milestone_rejects_a_batch_urn_as_its_primary_track() -> None:
    with pytest.raises(ValidationError, match="identity_kind_mismatch"):
        Milestone.model_validate(_milestone_fields(primary_track_ref=BATCH_URN))


def test_milestone_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        Milestone.model_validate(_milestone_fields(progress=0.5))


def test_milestone_rejects_an_over_long_title() -> None:
    with pytest.raises(ValidationError):
        Milestone.model_validate(_milestone_fields(title="x" * 81))


def test_milestone_accepts_a_maximum_length_title() -> None:
    milestone = Milestone.model_validate(_milestone_fields(title="x" * 80))
    assert len(milestone.title) == 80
