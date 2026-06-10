"""Strict schema + loader for ``roadmap propose --from-plan`` files."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import AgentSessionRole, EffortBucket
from eawf.kernel.state.ids import is_iter_id, is_phase_id, is_wave_id


class _StrictModel(BaseModel):
    """Roadmap-plan strict model base."""

    model_config = ConfigDict(extra="forbid")


class RoadmapPlanWave(_StrictModel):
    """One PENDING wave to stage from a roadmap plan file."""

    id: str
    title: Annotated[str, Field(min_length=1, max_length=72)]
    description: Annotated[str, Field(max_length=500)] | None = None
    deps: list[str] = Field(default_factory=list)
    file_scopes: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    success_criteria: list[CriterionSpec] = Field(default_factory=list)
    agent_role: AgentSessionRole | None = None
    effort_bucket: EffortBucket
    intent: IntentBrief | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        """Validate wave id grammar."""
        if not is_wave_id(value):
            raise ValueError(f"invalid wave id: {value!r}")
        return value

    @field_validator("deps")
    @classmethod
    def _validate_deps(cls, value: list[str]) -> list[str]:
        """Validate wave dep id grammar."""
        invalid = [dep for dep in value if not is_wave_id(dep)]
        if invalid:
            raise ValueError(f"invalid wave dep id(s): {invalid}")
        return value


class RoadmapPlanIter(_StrictModel):
    """One PLANNED iter and its child waves."""

    id: str
    title: Annotated[str, Field(min_length=1, max_length=72)]
    description: Annotated[str, Field(max_length=500)] | None = None
    waves: list[RoadmapPlanWave] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        """Validate iter id grammar."""
        if not is_iter_id(value):
            raise ValueError(f"invalid iter id: {value!r}")
        return value


class RoadmapPlanPhase(_StrictModel):
    """PLANNED phase metadata staged by a roadmap plan file."""

    id: str
    title: Annotated[str, Field(min_length=1, max_length=72)]
    description: Annotated[str, Field(max_length=500)] | None = None
    depends_on: list[str] = Field(default_factory=list)
    source_brief_ids: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        """Validate phase id grammar."""
        if not is_phase_id(value):
            raise ValueError(f"invalid phase id: {value!r}")
        return value

    @field_validator("depends_on")
    @classmethod
    def _validate_depends_on(cls, value: list[str]) -> list[str]:
        """Validate phase dep id grammar."""
        invalid = [dep for dep in value if not is_phase_id(dep)]
        if invalid:
            raise ValueError(f"invalid phase dep id(s): {invalid}")
        return value


class RoadmapPlan(_StrictModel):
    """Whole roadmap plan payload consumed by ``roadmap propose --from-plan``."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["RoadmapPlan"] = "RoadmapPlan"
    phase: RoadmapPlanPhase
    iters: list[RoadmapPlanIter] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_tree(self) -> RoadmapPlan:
        """Enforce unique ids, phase nesting, dep references, and acyclic waves.

        Raises:
            ValueError: when an iter/wave id does not nest under its parent,
                ids duplicate, a dep points outside the plan, or wave deps cycle.
        """
        iter_ids: set[str] = set()
        wave_ids: set[str] = set()
        deps_by_wave: dict[str, set[str]] = {}
        for iter_plan in self.iters:
            if iter_plan.id in iter_ids:
                raise ValueError(f"duplicate iter id: {iter_plan.id!r}")
            iter_ids.add(iter_plan.id)
            if not iter_plan.id.startswith(f"{self.phase.id}-I"):
                raise ValueError(
                    f"iter id does not nest under phase: "
                    f"id={iter_plan.id!r} phase_id={self.phase.id!r}"
                )
            for wave_plan in iter_plan.waves:
                if wave_plan.id in wave_ids:
                    raise ValueError(f"duplicate wave id: {wave_plan.id!r}")
                wave_ids.add(wave_plan.id)
                if not wave_plan.id.startswith(f"{iter_plan.id}-W"):
                    raise ValueError(
                        f"wave id does not nest under iter: "
                        f"id={wave_plan.id!r} iter_id={iter_plan.id!r}"
                    )
                if wave_plan.id in wave_plan.deps:
                    raise ValueError(f"wave {wave_plan.id!r} cannot depend on itself")
                deps_by_wave[wave_plan.id] = set(wave_plan.deps)
        for wave_id, deps in deps_by_wave.items():
            unknown = sorted(dep for dep in deps if dep not in wave_ids)
            if unknown:
                raise ValueError(f"wave {wave_id!r} has unknown deps: {unknown}")
        _raise_on_dep_cycle(deps_by_wave)
        return self


def load_roadmap_plan(path: Path) -> RoadmapPlan:
    """Load and validate a strict YAML/JSON roadmap plan file.

    Args:
        path: Plan file path. ``.json`` uses :mod:`orjson`; all other
            suffixes use ``yaml.safe_load``.

    Raises:
        OSError: when the file cannot be read.
        ValueError: when the decoded payload is not a mapping.
        yaml.YAMLError: when YAML parsing fails.
        orjson.JSONDecodeError: when JSON parsing fails.
        pydantic.ValidationError: when strict schema validation fails.
    """
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload: Any = orjson.loads(raw.encode("utf-8"))
    else:
        import yaml

        payload = yaml.safe_load(raw)
    if not isinstance(payload, dict):
        raise ValueError("roadmap plan must be a mapping")
    return RoadmapPlan.model_validate(payload)


def _raise_on_dep_cycle(deps_by_wave: dict[str, set[str]]) -> None:
    """Reject cyclic wave deps inside a plan file."""
    remaining = {wave_id: set(deps) for wave_id, deps in deps_by_wave.items()}
    while remaining:
        ready = [wave_id for wave_id, deps in remaining.items() if not deps.intersection(remaining)]
        if not ready:
            cycle_nodes = sorted(remaining)
            raise ValueError(f"roadmap plan wave deps contain a cycle: {cycle_nodes}")
        for wave_id in ready:
            del remaining[wave_id]
