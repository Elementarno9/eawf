"""The render skill registry carries only skills in the closed catalog.

A retired skill's body left in the registry reads as a live instruction to
anyone who greps for it, and a tool gate that walks the registry keeps
validating prose no operator can invoke. The registry therefore holds a body
only while its skill is a catalog entry, and the jury-calibration contract
carries no plan-site authority leaf that nothing reads.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from pydantic import ValidationError

from eawf.platform.profiles.models import JuryCalibration
from eawf.surfaces.render.skills.registry import SKILL_REGISTRY
from eawf.surfaces.render.skills.render import SkillSpec, render_skill_md_from_spec
from eawf.workflow.skills.catalog import SKILL_CATALOG

pytestmark = pytest.mark.unit


def _non_catalog(specs: Iterable[SkillSpec]) -> list[str]:
    """Return the rendered skill names that are not catalog entries."""
    return sorted(spec.skill_name for spec in specs if SKILL_CATALOG.entry(spec.skill_name) is None)


def _planted(skill_name: str) -> SkillSpec:
    return SkillSpec(
        skill_name=skill_name,
        description="A planted body.",
        argument_hint="",
        user_invocable=True,
        disable_model_invocation=False,
        body=f"# /{skill_name}\n",
    )


def test_registry_holds_only_catalog_skills() -> None:
    assert _non_catalog(SKILL_REGISTRY) == []


def test_registry_holds_no_retired_skill() -> None:
    retired = {row.skill_id for row in SKILL_CATALOG.retired}
    assert retired.isdisjoint(spec.skill_name for spec in SKILL_REGISTRY)


def test_every_registry_skill_renders_under_its_catalog_name() -> None:
    for spec in SKILL_REGISTRY:
        assert f"\nname: {spec.skill_name}\n" in render_skill_md_from_spec(spec)


def test_registry_is_not_empty() -> None:
    assert len(SKILL_REGISTRY) >= 1


@pytest.mark.parametrize("retired", ["prep", "ship", "math-explainer"])
def test_planted_retired_body_reds(retired: str) -> None:
    """A retired body planted back into the registry is flagged."""
    assert SKILL_CATALOG.retired_row(retired) is not None
    assert _non_catalog((*SKILL_REGISTRY, _planted(retired))) == [retired]


def test_planted_unknown_body_reds() -> None:
    """A body under a name the catalog never knew is flagged too."""
    assert _non_catalog((_planted("no-such-skill"),)) == ["no-such-skill"]


def test_jury_calibration_has_no_plan_authority_leaf() -> None:
    assert "plan_authority" not in JuryCalibration.model_fields


def test_jury_calibration_refuses_plan_authority_key() -> None:
    with pytest.raises(ValidationError, match="plan_authority"):
        JuryCalibration.model_validate({"plan_authority": "advisory"})
