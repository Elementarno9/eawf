"""Track lifecycle closure and the two work-in-progress limits.

Three things are pinned here. A Track has exactly two stored states, so a
state a domain profile invents is refused at the loader rather than
persisted and then found by a transition guard that has no edge for it.
The hard batch limit refuses an activation while the advisory milestone
limit only signals, and the two are separate predicates so a caller
cannot accidentally treat one as the other. The strict nested policy
document loads exactly as written, unknown nested keys included.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.state.epoch2 import (
    RepoTrackScope,
    Track,
    TrackCreateSpec,
    TrackPolicy,
    TrackStatus,
    WorkspaceTrackScope,
)
from eawf.kernel.state.epoch2.base import Epoch2Model

pytestmark = pytest.mark.unit

#: ``tests/fixtures/epoch2`` - three levels up lands on ``tests/``.
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "epoch2"

REPOSITORY_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
OTHER_REPOSITORY_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-DOCS/repository/REP-DOCS"
TRACK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-RUNTIME"


def _policy_document() -> dict[str, Any]:
    """Return the strict nested policy document as a fresh mapping."""
    parsed = yaml.safe_load((FIXTURES / "track_policy.yaml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _profiles() -> dict[str, list[str]]:
    """Return the admitted / refused Track status census."""
    parsed = yaml.safe_load((FIXTURES / "track_profiles.yaml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _create_document() -> dict[str, Any]:
    """Return the Track create document as a fresh mapping."""
    parsed = yaml.safe_load((FIXTURES / "track_create_spec.yaml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _track_fields(**overrides: Any) -> dict[str, Any]:
    """Build the field mapping of a valid ACTIVE Track."""
    fields: dict[str, Any] = {
        "uid": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "key": "TRK-RUNTIME",
        "urn": TRACK_URN,
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": "2026-09-08T00:00:00Z",
        "updated_at": "2026-09-08T00:00:00Z",
        "title": "Harden the runtime against a provider outage",
        "charter": "Keep a run recoverable when a provider stops responding.",
        "scope": {"scope_kind": "repository", "repository_ref": REPOSITORY_URN},
        "owner": {"principal_kind": "operator", "principal_id": "OP-0001"},
        "policy": _policy_document(),
        "status": "ACTIVE",
    }
    fields.update(overrides)
    return fields


# ---- the closed Track lifecycle ---------------------------------------------


def test_track_status_admits_exactly_the_census_states() -> None:
    assert {member.value for member in TrackStatus} == set(_profiles()["admitted"])


@pytest.mark.parametrize("status", _profiles()["admitted"])
def test_track_accepts_each_admitted_status(status: str) -> None:
    track = Track.model_validate(_track_fields(status=status))
    assert track.status.value == status


@pytest.mark.parametrize("status", _profiles()["refused"])
def test_track_rejects_a_profile_supplied_status(status: str) -> None:
    with pytest.raises(ValidationError):
        Track.model_validate(_track_fields(status=status))


def test_track_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        Track.model_validate(_track_fields(health="green"))


def test_track_rejects_a_urn_addressing_another_track() -> None:
    other = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-DOCS"
    with pytest.raises(ValidationError, match="carries a URN addressing"):
        Track.model_validate(_track_fields(urn=other))


def test_track_rejects_an_owner_disagreeing_with_the_policy() -> None:
    owner = {"principal_kind": "operator", "principal_id": "OP-0002"}
    with pytest.raises(ValidationError, match="differs from policy ownership_principal"):
        Track.model_validate(_track_fields(owner=owner))


def test_track_rejects_a_phrase_declared_both_in_and_out_of_scope() -> None:
    fields = _track_fields(in_scope=["Provider  failover"], out_of_scope=["provider failover"])
    with pytest.raises(ValidationError, match="in_scope/out_of_scope repeats"):
        Track.model_validate(fields)


# ---- the hard and advisory work-in-progress limits --------------------------


def test_track_policy_admits_batch_activation_below_the_hard_limit() -> None:
    policy = TrackPolicy.model_validate(_policy_document())
    assert policy.admits_batch_activation(active_batches_in_repo=0) is True


def test_track_policy_refuses_batch_activation_at_the_hard_limit() -> None:
    policy = TrackPolicy.model_validate(_policy_document())
    assert policy.admits_batch_activation(active_batches_in_repo=1) is False


def test_track_policy_refuses_batch_activation_above_the_hard_limit() -> None:
    document = _policy_document()
    document["wip"]["active_batches_per_repo"] = 2
    policy = TrackPolicy.model_validate(document)
    assert policy.admits_batch_activation(active_batches_in_repo=3) is False


def test_track_policy_exceeds_milestone_advisory_above_the_bound() -> None:
    policy = TrackPolicy.model_validate(_policy_document())
    assert policy.exceeds_milestone_advisory(active_milestones=2) is True


def test_track_policy_exceeds_milestone_advisory_is_false_at_the_bound() -> None:
    policy = TrackPolicy.model_validate(_policy_document())
    assert policy.exceeds_milestone_advisory(active_milestones=1) is False


def test_track_policy_accepts_a_zero_milestone_advisory_bound() -> None:
    document = _policy_document()
    document["wip"]["active_milestones"] = 0
    policy = TrackPolicy.model_validate(document)
    assert policy.exceeds_milestone_advisory(active_milestones=0) is False
    assert policy.exceeds_milestone_advisory(active_milestones=1) is True


def test_track_policy_rejects_a_negative_milestone_advisory_bound() -> None:
    document = _policy_document()
    document["wip"]["active_milestones"] = -1
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        TrackPolicy.model_validate(document)


def test_track_policy_rejects_a_zero_hard_batch_limit() -> None:
    document = _policy_document()
    document["wip"]["active_batches_per_repo"] = 0
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        TrackPolicy.model_validate(document)


def test_track_policy_rejects_a_boolean_coerced_into_the_hard_batch_limit() -> None:
    document = _policy_document()
    document["wip"]["active_batches_per_repo"] = True
    with pytest.raises(ValidationError):
        TrackPolicy.model_validate(document)


# ---- the strict nested policy document --------------------------------------


def test_track_policy_loads_the_strict_nested_document() -> None:
    policy = TrackPolicy.model_validate(_policy_document())
    assert policy.revision == 1
    assert policy.wip.active_batches_per_repo == 1
    assert policy.ownership_principal.principal_id == "OP-0001"
    assert [kind.value for kind in policy.permitted_milestone_kinds] == ["product"]
    assert policy.campaign_templates == ()
    assert policy.promotion_rules == ()
    assert policy.outcome_metrics[0].metric_id == "MET-INSTALLABLE"
    assert policy.outcome_metrics[0].measurement_locus == "milestone_acceptance"
    assert policy.integration_priority == 50
    assert policy.presentation.default_view == "roadmap"


def test_track_policy_rejects_an_unknown_nested_key() -> None:
    document = _policy_document()
    document["wip"]["active_runs"] = 3
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        TrackPolicy.model_validate(document)


def test_track_policy_rejects_an_empty_permitted_milestone_kinds() -> None:
    document = _policy_document()
    document["permitted_milestone_kinds"] = []
    with pytest.raises(ValidationError, match="must admit at least one kind"):
        TrackPolicy.model_validate(document)


def test_track_policy_rejects_an_unknown_milestone_kind() -> None:
    document = _policy_document()
    document["permitted_milestone_kinds"] = ["marketing"]
    with pytest.raises(ValidationError):
        TrackPolicy.model_validate(document)


def test_track_policy_rejects_duplicate_metric_ids() -> None:
    document = _policy_document()
    document["outcome_metrics"] = [document["outcome_metrics"][0]] * 2
    with pytest.raises(ValidationError, match="outcome_metrics repeats an identifier"):
        TrackPolicy.model_validate(document)


def test_track_policy_rejects_an_integration_priority_above_the_ceiling() -> None:
    document = _policy_document()
    document["integration_priority"] = 101
    with pytest.raises(ValidationError, match="less than or equal to 100"):
        TrackPolicy.model_validate(document)


def test_track_policy_accepts_the_integration_priority_bounds() -> None:
    for priority in (0, 100):
        document = _policy_document()
        document["integration_priority"] = priority
        assert TrackPolicy.model_validate(document).integration_priority == priority


# ---- the create document ----------------------------------------------------


def test_track_create_spec_loads_the_fixture_yaml_document() -> None:
    spec = TrackCreateSpec.model_validate(_create_document())
    assert spec.key == "TRK-RUNTIME"
    assert isinstance(spec.scope, RepoTrackScope)
    assert str(spec.scope.repository_ref) == REPOSITORY_URN
    assert spec.policy.integration_priority == 50


def test_track_create_spec_rejects_an_empty_document() -> None:
    with pytest.raises(ValidationError):
        TrackCreateSpec.model_validate(yaml.safe_load(""))


def test_track_create_spec_rejects_a_sequence_document() -> None:
    with pytest.raises(ValidationError):
        TrackCreateSpec.model_validate(yaml.safe_load("- key: TRK-RUNTIME\n"))


def test_track_create_spec_forbids_a_supplied_status() -> None:
    document = _create_document()
    document["status"] = "ACTIVE"
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        TrackCreateSpec.model_validate(document)


def test_track_create_spec_rejects_a_non_canonical_track_key() -> None:
    document = _create_document()
    document["key"] = "TRACK-RUNTIME"
    with pytest.raises(ValidationError, match="entity_key_invalid"):
        TrackCreateSpec.model_validate(document)


def test_track_create_spec_defaults_the_scope_phrase_lists_to_empty() -> None:
    document = _create_document()
    del document["in_scope"]
    del document["out_of_scope"]
    spec = TrackCreateSpec.model_validate(document)
    assert spec.in_scope == ()
    assert spec.out_of_scope == ()


# ---- the discriminated scope ------------------------------------------------


def test_workspace_track_scope_sorts_its_members_canonically() -> None:
    scope = WorkspaceTrackScope.model_validate(
        {
            "scope_kind": "workspace",
            "repository_refs": [REPOSITORY_URN, OTHER_REPOSITORY_URN],
        }
    )
    assert [str(ref) for ref in scope.repository_refs] == sorted(
        [REPOSITORY_URN, OTHER_REPOSITORY_URN]
    )


def test_workspace_track_scope_rejects_an_empty_membership() -> None:
    with pytest.raises(ValidationError, match="at least one member repository"):
        WorkspaceTrackScope.model_validate({"scope_kind": "workspace", "repository_refs": []})


def test_workspace_track_scope_rejects_a_repeated_member() -> None:
    with pytest.raises(ValidationError, match="the same repository twice"):
        WorkspaceTrackScope.model_validate(
            {
                "scope_kind": "workspace",
                "repository_refs": [REPOSITORY_URN, REPOSITORY_URN],
            }
        )


def test_repo_track_scope_rejects_a_track_urn_in_the_repository_slot() -> None:
    with pytest.raises(ValidationError, match="identity_kind_mismatch"):
        RepoTrackScope.model_validate({"scope_kind": "repository", "repository_ref": TRACK_URN})


def test_track_rejects_a_scope_without_its_discriminator() -> None:
    with pytest.raises(ValidationError):
        Track.model_validate(_track_fields(scope={"repository_ref": REPOSITORY_URN}))


def test_epoch2_model_base_forbids_extras() -> None:
    assert Epoch2Model.model_config["extra"] == "forbid"
