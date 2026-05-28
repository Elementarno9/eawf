"""IntentBrief — typed intent doc attached to lifecycle entities.

An :class:`IntentBrief` is the minimal typed intent record a v0.4
``/research`` or ``/spike`` brief promotes onto a :class:`Wave` / :class:`Iter` /
:class:`Phase` / :class:`BacklogItem`. It captures the *problem* the
entity is solving and the *desired outcome* it converges on plus the
evidence + source brief refs that back the intent so a downstream
planner / executor / auditor can read the intent without re-parsing
the underlying narrative.

The model is strict-mode (``ConfigDict(extra="forbid")``) — unknown keys
fail at ingestion. Every field is bounded so an over-cap value fails
:class:`pydantic.ValidationError` rather than silently truncating in the
renderer.

The :class:`State` field :attr:`Wave.intent` / :attr:`Iter.intent` /
:attr:`Phase.intent` / :attr:`BacklogItem.intent` is ``IntentBrief |
None`` and defaults to ``None`` so on-disk state written before this
field existed re-validates without a schema bump (additive, replay-safe
per the AGENTS "state vs specs" rule).

The companion :class:`NarrativeBundle` (memory ref
``project_v04_foundation_and_enforcement``) wraps an IntentBrief with
provenance + the originating narrative; that wrapper lands in a follow-up
wave once the brief promotion surface is wired. This module ships only
the IntentBrief leaf — the smallest typed-intent surface every
downstream consumer can depend on today.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class IntentBrief(BaseModel):
    """Typed intent doc carried by a lifecycle entity.

    Attributes:
        problem: One-line statement of the problem the entity is
            solving — the W24-audited counterpart to the prior
            ``goal`` field that names the gap, not the action.
            Bounded at 200 characters so the field fits a single
            rendered row in dispatch + detail surfaces.
        desired_outcome: One-line description of the state of the
            world after the intent is satisfied — the W24-audited
            counterpart to the prior ``success_signal`` field that
            names the steady state, not the observable. Bounded at
            200 characters so the field fits the same single-row
            render slot as ``problem``.
        planned_steps: Ordered list of the planner's intended steps
            toward the desired outcome. Each step is bounded at 500
            characters and the list itself is bounded at 10 entries so
            a brief stays scannable in the dispatch / detail surfaces.
            Default empty list; the EviBound gate is not applied here.
        risks: Known risks or trade-offs the planner accepted. Each
            risk is bounded at 500 characters and the list at 10
            entries with the same scannability rationale as
            ``planned_steps``. Default empty list.
        priority_rationale: Optional long-form explanation of why this
            intent earned its slot — the W24-audited counterpart to
            the prior ``motivation`` field that names the prioritization
            trade-off rather than the underlying *why*. Bounded at 1000
            characters; ``None`` when the prioritization is self-evident.
        evidence_refs: Repo-relative / URN / external-URL strings that
            ratify the brief's claims. Used by the v0.4 EviBound verify
            gate to score "every claim resolves" on the brief. Default
            empty list so a brief whose claims are not yet sourced
            still validates (the EviBound gate fails the brief at
            promotion time, not at ingestion time).
        source_brief_ids: Ids / repo-relative paths of the originating
            research / spike briefs this intent was distilled from.
            Lets the renderer surface "see brief X" alongside the
            intent so the reader can follow the chain. Default empty
            list.
    """

    model_config = ConfigDict(extra="forbid")

    problem: Annotated[str, Field(min_length=1, max_length=200)]
    desired_outcome: Annotated[str, Field(min_length=1, max_length=200)]
    planned_steps: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=10
    )
    risks: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=10
    )
    priority_rationale: Annotated[str, Field(max_length=1000)] | None = None
    evidence_refs: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    source_brief_ids: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)


__all__ = [
    "IntentBrief",
]
