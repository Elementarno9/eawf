"""DOM-034: a promoted Campaign finding is its own record and its own URN kind.

One case per finding family proves each validates as itself, and one denial
per pair proves none of the three is accepted where another is required.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.identity import IdentityError, IdentityRejection
from eawf.kernel.state.epoch2 import urns
from eawf.kernel.state.epoch2.finding import CampaignFinding, FindingDisposition
from eawf.kernel.state.epoch2.plan_revision import CampaignCitation
from eawf.kernel.store.kinds.agent_report import ReviewFinding
from eawf.workflow.planning.lenses.structure import PlanFinding, PlanFindingCode, PlanLens

pytestmark = pytest.mark.unit

SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
FINDING_URN = f"{SLOT}/campaign-finding/CFN-0001"
EVIDENCE_URN = f"{SLOT}/evidence/EVD-0001"
MILESTONE_URN = f"{SLOT}/milestone/MLS-0030"


def _finding(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "uid": "3b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "key": "CFN-0001",
        "urn": FINDING_URN,
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": "2026-09-08T00:00:00Z",
        "updated_at": "2026-09-08T00:00:00Z",
        "statement": "replay preserves event order across restarts",
    }
    fields.update(overrides)
    return fields


def _plan_finding() -> PlanFinding:
    return PlanFinding(
        lens=PlanLens.SCHEMA_REFERENCE,
        severity="blocking",
        code=PlanFindingCode.CROSS_SCOPE_MISMATCH,
        entity_refs=(MILESTONE_URN,),
        message="a reference does not resolve",
        remediation="cite a promoted record",
    )


def _kind_code(exc: ValidationError) -> IdentityRejection:
    cause = exc.errors()[0]["ctx"]["error"]
    assert isinstance(cause, IdentityError)
    return cause.code


# ---- one case per family ------------------------------------------------------


def test_dom_034_campaign_finding_validates_held_with_no_consumer() -> None:
    finding = CampaignFinding.model_validate(_finding())
    assert finding.key == "CFN-0001"
    assert finding.disposition is FindingDisposition.HELD
    assert finding.consumed_by_milestone_ref is None


def test_dom_034_campaign_finding_consumed_by_a_milestone() -> None:
    finding = CampaignFinding.model_validate(
        _finding(
            disposition="consumed",
            consumed_by_milestone_ref=MILESTONE_URN,
            consumed_at="2026-09-09T00:00:00Z",
        )
    )
    assert finding.disposition is FindingDisposition.CONSUMED


def test_dom_034_plan_finding_is_its_own_family() -> None:
    assert _plan_finding().code is PlanFindingCode.CROSS_SCOPE_MISMATCH


def test_dom_034_review_finding_is_its_own_family() -> None:
    review = ReviewFinding(severity="must-fix", message="the loader drops a key")
    assert review.severity == "must-fix"


def test_dom_034_no_shared_finding_urn_or_name_exists() -> None:
    assert not hasattr(urns, "FindingUrn")
    assert CampaignFinding.__name__ != "Finding"


# ---- cross-family denials, one per pair ---------------------------------------


def test_dom_034_campaign_finding_urn_refuses_another_kind() -> None:
    with pytest.raises(ValidationError) as caught:
        CampaignFinding.model_validate(_finding(urn=EVIDENCE_URN))
    assert _kind_code(caught.value) is IdentityRejection.IDENTITY_KIND_MISMATCH


def test_dom_034_citation_refuses_a_non_campaign_finding_with_kind_mismatch() -> None:
    with pytest.raises(ValidationError) as caught:
        CampaignCitation.model_validate({"finding_ref": EVIDENCE_URN, "note": "not a finding"})
    assert _kind_code(caught.value) is IdentityRejection.IDENTITY_KIND_MISMATCH


def test_dom_034_campaign_finding_refuses_a_plan_finding_shape() -> None:
    with pytest.raises(ValidationError):
        CampaignFinding.model_validate({**_finding(), **asdict(_plan_finding())})


def test_dom_034_campaign_finding_refuses_a_review_finding_shape() -> None:
    review = ReviewFinding(severity="nit", message="rename the helper")
    with pytest.raises(ValidationError):
        CampaignFinding.model_validate({**_finding(), **review.model_dump()})


def test_dom_034_review_finding_refuses_a_plan_finding_shape() -> None:
    with pytest.raises(ValidationError):
        ReviewFinding.model_validate(asdict(_plan_finding()))


def test_dom_034_review_finding_refuses_a_campaign_finding_shape() -> None:
    with pytest.raises(ValidationError):
        ReviewFinding.model_validate(_finding())


# ---- boundaries and error paths ------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"consumed_by_milestone_ref": MILESTONE_URN},
        {"consumed_at": "2026-09-09T00:00:00Z"},
        {"disposition": "consumed"},
        {
            "consumed_by_milestone_ref": MILESTONE_URN,
            "consumed_at": "2026-09-09T00:00:00Z",
        },
    ],
)
def test_dom_034_disposition_and_consumption_move_together(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CampaignFinding.model_validate(_finding(**overrides))


@pytest.mark.parametrize("statement", ["", "   ", "two\nlines", "x" * 201])
def test_dom_034_statement_is_one_bounded_line(statement: str) -> None:
    with pytest.raises(ValidationError):
        CampaignFinding.model_validate(_finding(statement=statement))


def test_dom_034_statement_at_its_bound_validates() -> None:
    assert len(CampaignFinding.model_validate(_finding(statement="x" * 200)).statement) == 200


def test_dom_034_key_and_urn_must_agree() -> None:
    with pytest.raises(ValidationError, match="CFN-0002"):
        CampaignFinding.model_validate(_finding(key="CFN-0002"))


def test_dom_034_a_non_string_urn_is_refused() -> None:
    with pytest.raises(ValidationError):
        CampaignFinding.model_validate(_finding(urn=7))
