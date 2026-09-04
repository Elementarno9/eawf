"""Schema tests for the typed VisualContract / DeterminismRecipe (FS15).

Covers the typed criteria CR-1..CR-2 of the FS15 spec:

* CR-1 (validates, T1): a minimal VisualContract validates, and a
  fully-populated one (criteria + gates + goldens + recipe + rubric)
  round-trips through ``model_dump`` / ``model_validate`` identically.
* CR-2 (raises, T4): a VisualContract with an unknown ``surface`` literal
  fails validation, an extra/unknown field fails the inherited
  ``extra="forbid"`` config, and a DeterminismRecipe with a wrong-typed
  field fails validation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import (
    CriterionSpec,
    GateSpec,
    ObserveVerb,
    OracleTier,
    ProofLocus,
    QualityDimension,
    ResponseClause,
)
from eawf.kernel.spec.visual import DeterminismRecipe, VisualContract
from eawf.kernel.spec.wave import WaveBehavior


def _populated_contract() -> VisualContract:
    """Build a fully-populated VisualContract for round-trip coverage."""
    criterion = CriterionSpec(
        id="CR-01",
        text="the tui header renders the brand token flush-left",
        kind="visual",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension=QualityDimension.INTERACTION_CAPABILITY,
        measurable_signal="the rendered header line begins with the brand token",
        response=ResponseClause(
            observe=ObserveVerb.RENDERS_TOKEN,
            object="brand token",
            locus=ProofLocus.TUI_SNAPSHOT,
            expected="Ea",
        ),
        oracle_tier=OracleTier.T3_SNAPSHOT,
    )
    gate = GateSpec(
        id="GATE-01",
        criterion_id="CR-01",
        kind="snapshot_match",
        args={"golden": "tests/snapshots/header.svg"},
        policy="block",
        cadence="every-wave",
    )
    rubric_item = WaveBehavior(
        id="B1",
        text="the header surface reads cleanly at the perceptual tier",
        quality_dimension=QualityDimension.INTERACTION_CAPABILITY,
        jury_scorable=True,
    )
    recipe = DeterminismRecipe(
        fonts=["JetBrainsMono Nerd Font"],
        clock_frozen=True,
        disable_system_fonts=True,
        env={"TZ": "UTC"},
        tool_versions={"resvg": "0.45.0"},
    )
    return VisualContract(
        surface="svg",
        criteria=[criterion],
        gates=[gate],
        goldens={"default": "tests/snapshots/header.svg"},
        recipe=recipe,
        rubric=[rubric_item],
    )


# --------------------------------------------------------------------------- #
# CR-1 — a minimal contract validates; a populated one round-trips.
# --------------------------------------------------------------------------- #
def test_visual_contract_minimal_validates() -> None:
    """A minimal VisualContract(surface='svg') validates with empty defaults."""
    contract = VisualContract(surface="svg")
    assert contract.surface == "svg"
    assert contract.criteria == []
    assert contract.gates == []
    assert contract.goldens == {}
    assert contract.rubric == []
    assert isinstance(contract.recipe, DeterminismRecipe)
    assert contract.recipe.clock_frozen is True
    assert contract.recipe.disable_system_fonts is True


def test_visual_contract_full_roundtrips() -> None:
    """A fully-populated contract round-trips through model_dump/model_validate."""
    original = _populated_contract()
    restored = VisualContract.model_validate(original.model_dump())
    assert restored == original


def test_determinism_recipe_defaults() -> None:
    """DeterminismRecipe defaults: frozen clock, system fonts off, empty maps."""
    recipe = DeterminismRecipe()
    assert recipe.fonts == []
    assert recipe.clock_frozen is True
    assert recipe.disable_system_fonts is True
    assert recipe.env == {}
    assert recipe.tool_versions == {}


# --------------------------------------------------------------------------- #
# CR-2 — error paths: bad surface, extra field, wrong-typed recipe field.
# --------------------------------------------------------------------------- #
def test_visual_contract_unknown_surface_raises() -> None:
    """An unknown surface literal fails validation."""
    with pytest.raises(ValidationError, match="surface"):
        VisualContract(surface="bogus")  # type: ignore[arg-type]


def test_visual_contract_extra_field_raises() -> None:
    """An extra/unknown field fails the inherited extra='forbid' config."""
    with pytest.raises(ValidationError, match="extra"):
        VisualContract(surface="svg", unexpected="x")  # type: ignore[call-arg]


def test_determinism_recipe_extra_field_raises() -> None:
    """An extra/unknown field on DeterminismRecipe fails extra='forbid'."""
    with pytest.raises(ValidationError, match="extra"):
        DeterminismRecipe(unexpected="x")  # type: ignore[call-arg]


def test_determinism_recipe_wrong_typed_field_raises() -> None:
    """A wrong-typed field on DeterminismRecipe fails validation."""
    with pytest.raises(ValidationError, match="clock_frozen"):
        DeterminismRecipe(clock_frozen="not-a-bool")  # type: ignore[arg-type]
