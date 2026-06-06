"""Visual-fidelity contract types (VisualContract / DeterminismRecipe).

A visual contract types the per-surface fidelity spec: the criteria one
rendered surface (TUI / SVG / CLI) must satisfy, the gates that score
those criteria (one per oracle tier), the committed goldens keyed by
render state, the determinism recipe that makes a render byte-stable
across hosts, and the jury rubric for the perceptual tier.

This module is types-only: the oracle runner and the SVG render stack
that consume these models land in later waves. Defining the typed shape
first lets the producers and consumers agree on a single contract before
either side exists.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from eawf.kernel.spec.common import CriterionSpec, GateSpec, _StrictModel
from eawf.kernel.spec.wave import WaveBehavior

#: Closed set of rendered surfaces a :class:`VisualContract` can cover.
#: An unknown value fails validation at the ingestion boundary.
VisualSurface = Literal["tui", "svg", "cli"]


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
