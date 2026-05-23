"""WaveSpec — typed deliverable document for a wave.

A wave is the unit of work an agent claims, executes, and ships in one
commit (one cherry-pick into the parent feature branch). The WaveSpec
records the wave's behaviours (B1..Bn observable outcomes), the
verdicts it implements, the file scopes it edits, the test refs it
satisfies, optional mockups (ASCII + optional Mermaid), and the agent
role + effort bucket the planner sized it at.

Pydantic-enforced invariants:

- ``id`` / ``iter_id`` / ``phase_id`` are linked by prefix (WSV-09 /
  ``_consistent_ids`` model_validator).
- ``behaviors``, ``failure_modes``, ``file_scopes``, ``implements``
  are non-empty (WSV-01..WSV-04).

Cross-state invariants (WSV-05 / WSV-06 / WSV-08 / WSV-10) live in the
loader and CLI validators because they need disk + state-tree lookups.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from eawf.spec.common import (
    FileScopeRef,
    TestRef,
    VerdictCitation,
    _StrictModel,
)
from eawf.state.enums import AgentSessionRole, EffortBucket
from eawf.state.models import IterIdStr, PhaseIdStr, WaveIdStr


class WaveBehavior(_StrictModel):
    """One observable behaviour the wave delivers (B1..Bn).

    ``id`` is the ``B<n>`` label the spec uses in commit messages and
    audit reports; ``latency_budget_ms`` (optional) records the wall-
    clock budget for the behaviour when it has a measurable runtime
    cost.
    """

    id: Annotated[str, Field(pattern=r"^B\d+$")]
    text: str = Field(min_length=20, max_length=1000)
    latency_budget_ms: int | None = Field(default=None, ge=0)
    test_refs: list[TestRef] = Field(default_factory=list)


class WaveMockup(_StrictModel):
    """ASCII mockup with optional Mermaid diagram.

    UI-scope waves (file_scopes touching ``src/eawf/tui/`` or
    ``src/eawf/render/``) require either a mockup or a non-empty
    ``mockup_waiver_reason`` (WSV-07).
    """

    ascii: str = Field(min_length=1)
    mermaid: str | None = None
    note: str | None = None


class WaveSpec(_StrictModel):
    """Wave deliverable spec.

    The ``_consistent_ids`` model validator enforces that the wave id,
    iter id, and phase id nest by prefix (``P25-I01-W01`` lives under
    iter ``P25-I01`` which lives under phase ``P25``). Mismatches fail
    at load time so the spec cannot drift away from the state tree.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["WaveSpec"] = "WaveSpec"

    id: WaveIdStr
    iter_id: IterIdStr
    phase_id: PhaseIdStr
    title: str = Field(min_length=1, max_length=120)
    agent_role: AgentSessionRole
    effort_bucket: EffortBucket
    deps: list[WaveIdStr] = Field(default_factory=list)
    file_scopes: list[FileScopeRef] = Field(min_length=1)
    implements: list[VerdictCitation] = Field(min_length=1)
    behaviors: list[WaveBehavior] = Field(min_length=1)
    failure_modes: list[str] = Field(min_length=1)
    tests: list[TestRef] = Field(default_factory=list)
    mockup: WaveMockup | None = None
    mockup_waiver_reason: str | None = None

    @model_validator(mode="after")
    def _consistent_ids(self) -> WaveSpec:
        """Enforce wave id nests under iter id nests under phase id.

        Raises:
            ValueError: when ``id`` does not start with ``{iter_id}-W``
                or ``iter_id`` does not start with ``{phase_id}-I``.
        """
        if not self.id.startswith(f"{self.iter_id}-W"):
            raise ValueError(
                f"wave id does not nest under iter: id={self.id!r} iter_id={self.iter_id!r}"
            )
        if not self.iter_id.startswith(f"{self.phase_id}-I"):
            raise ValueError(
                f"iter id does not nest under phase: iter_id={self.iter_id!r} "
                f"phase_id={self.phase_id!r}"
            )
        return self

    @model_validator(mode="after")
    def _mockup_required(self) -> WaveSpec:
        """Enforce WSV-07: UI-scope waves cite a mockup or a waiver.

        Fires only when at least one ``file_scopes`` entry lives under
        ``src/eawf/tui/`` or ``src/eawf/render/`` (per the D11
        heuristic). When the heuristic fires the wave MUST carry either
        a non-None ``mockup`` block OR a non-empty
        ``mockup_waiver_reason`` string. The check delegates to
        :func:`eawf.spec.heuristics.requires_mockup_reference` so unit
        tests can exercise the heuristic without building a full
        WaveSpec.

        Raises:
            ValueError: when the heuristic fires and neither ``mockup``
                nor ``mockup_waiver_reason`` is set.
        """
        # Local import avoids a circular dependency: ``heuristics`` does
        # NOT import any spec model, but importing it at module top would
        # make ``eawf.spec.wave`` depend on a sibling that itself may
        # later need spec types for typing.
        from eawf.spec.heuristics import requires_mockup_reference

        if requires_mockup_reference(
            file_scopes=self.file_scopes,
            mockup_present=self.mockup is not None,
            mockup_waiver_reason=self.mockup_waiver_reason,
        ):
            raise ValueError(
                "ui-scope wave requires mockup reference: "
                f"id={self.id!r} file_scopes={self.file_scopes!r} "
                "(set 'mockup' or 'mockup_waiver_reason')"
            )
        return self
