"""Visual-fidelity contract types (VisualContract / DeterminismRecipe).

A visual contract types the per-surface fidelity spec: the criteria one
rendered surface (TUI / SVG / CLI) must satisfy, the gates that score
those criteria (one per oracle tier), the committed goldens keyed by
render state, the determinism recipe that makes a render byte-stable
across hosts, and the jury rubric for the perceptual tier.

This module is types-only: the oracle runner and the SVG render stack
that consume these models land later. Defining the typed shape
first lets the producers and consumers agree on a single contract before
either side exists.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from eawf.kernel.spec.common import (
    CriterionSpec,
    GateSpec,
    QualityDimension,
    TestRef,
    _StrictModel,
)

#: Closed set of rendered surfaces a :class:`VisualContract` can cover.
#: An unknown value fails validation at the ingestion boundary.
VisualSurface = Literal["tui", "svg", "cli"]


class WaveBehavior(_StrictModel):
    """One observable behaviour a rubric scores (B1..Bn).

    ``id`` is the ``B<n>`` label the spec uses in commit messages and
    audit reports; ``latency_budget_ms`` (optional) records the wall-
    clock budget for the behaviour when it has a measurable runtime
    cost.

    ``jury_scorable`` marks the behaviour as a rubric item the
    spec-jury scores; when set, ``quality_dimension`` MUST name the
    ISO-25010 axis the score is taken on (the rubric item IS a typed
    measurable criterion). Both fields default off so existing specs
    validate unchanged.
    """

    id: Annotated[str, Field(pattern=r"^B\d+$")]
    text: str = Field(min_length=20, max_length=1000)
    latency_budget_ms: int | None = Field(default=None, ge=0)
    test_refs: list[TestRef] = Field(default_factory=list)
    quality_dimension: QualityDimension | None = None
    jury_scorable: bool = False

    @model_validator(mode="after")
    def _jury_scorable_needs_dimension(self) -> WaveBehavior:
        """Require a quality_dimension on every jury-scorable behaviour.

        A rubric item the jury scores has to declare the axis the
        score lives on; a scorable behaviour with no dimension is an
        authoring bug the jury cannot act on.

        Raises:
            ValueError: when ``jury_scorable`` is True but
                ``quality_dimension`` is None.
        """
        if self.jury_scorable and self.quality_dimension is None:
            raise ValueError(f"jury-scorable behavior requires a quality_dimension: id={self.id!r}")
        return self


class DeterminismRecipe(_StrictModel):
    """The recipe that makes a visual render byte-deterministic.

    Captures the knobs the pilot-harness clock-normalization and the SVG
    font-vendoring rely on so a golden is reproducible across hosts. The
    runner consumes these knobs to freeze the render clock, vendor the
    fonts, disable host system fonts, and pin the tool versions before
    comparing a fresh render against its committed golden.
    """

    fonts: list[str] = Field(default_factory=list)
    clock_frozen: bool = True
    disable_system_fonts: bool = True
    env: dict[str, str] = Field(default_factory=dict)
    tool_versions: dict[str, str] = Field(default_factory=dict)


class VisualContract(_StrictModel):
    """A typed visual-fidelity contract over one rendered surface.

    Generalizes the per-surface visual spec: the criteria the surface
    must satisfy, the gates that score them (one per oracle tier), the
    committed goldens keyed by render state, the determinism recipe, and
    the jury rubric for the perceptual tier. The ``goldens`` map is keyed
    by render-state name (e.g. a TUI mode or an SVG state) and valued by
    the repo-relative golden path the runner diffs against.
    """

    surface: VisualSurface
    criteria: list[CriterionSpec] = Field(default_factory=list)
    gates: list[GateSpec] = Field(default_factory=list)
    goldens: dict[str, str] = Field(default_factory=dict)
    recipe: DeterminismRecipe = Field(default_factory=DeterminismRecipe)
    rubric: list[WaveBehavior] = Field(default_factory=list)
