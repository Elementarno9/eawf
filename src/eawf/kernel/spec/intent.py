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

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from eawf.kernel.spec.common import SourceUnit

logger = logging.getLogger(__name__)


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

    @property
    def is_required_intent(self) -> bool:
        """Return whether the brief was distilled from a source-brief document.

        A required-intent brief carries at least one ``source_brief_ids``
        entry: the wave was synthesised ``--from-briefs`` so the source-brief
        document is itself an authoritative deliverable list the wave's
        criteria must account for. For such a wave, an empty ``planned_steps``
        is not a clean no-op -- the source brief still enumerates deliverables
        -- so the source-brief coverage gate runs against the brief document
        rather than short-circuiting on the empty step list.

        Returns:
            ``True`` when ``source_brief_ids`` is non-empty, else ``False``.
        """
        return bool(self.source_brief_ids)


def has_authoring_body(brief: IntentBrief) -> bool:
    """Return whether *brief* carries any non-blank intent body field.

    An authored brief is expected to say *why* the wave earned its slot:
    a non-blank ``priority_rationale``, at least one ``planned_steps``
    entry, or at least one ``risks`` entry. A brief distilled from a
    source brief (non-empty ``source_brief_ids``) also counts as bodied
    -- the source-brief document is itself the authoritative deliverable
    list, so the rationale lives there by reference rather than inline
    (this is the ``is_required_intent`` case). A brief carrying only the
    required ``problem`` + ``desired_outcome`` pair (blank rationale,
    empty steps, empty risks, no source brief) has an empty body and is
    rejected at the authoring path by
    :func:`eawf.workflow.lifecycle.wave.plan_wave`.

    This predicate is intentionally a free function, not a model
    validator: the model itself stays permissive so legacy on-disk
    briefs with empty body fields still validate at load / replay. Only
    the authoring path consults the predicate.

    Args:
        brief: The brief to inspect.

    Returns:
        ``True`` when at least one body field is populated, else
        ``False``.
    """
    rationale = brief.priority_rationale
    has_rationale = rationale is not None and rationale.strip() != ""
    return (
        has_rationale or bool(brief.planned_steps) or bool(brief.risks) or brief.is_required_intent
    )


def source_brief_units(intent: IntentBrief, *, repo_root: Path) -> list[SourceUnit]:
    """Extract source units from each referenced source-brief document.

    Reads every ``source_brief_ids`` entry that resolves to an on-disk file
    under *repo_root* (a repo-relative or absolute path) and splits each
    document into :class:`~eawf.kernel.spec.common.SourceUnit` rows via
    :func:`~eawf.workflow.propose.generator.extract_units`. The per-document
    units are concatenated in ``source_brief_ids`` order and re-minted with a
    single monotonic span id sequence so two briefs never collide on a span
    id and a coverage finding traces back to a stable ordinal.

    A ``source_brief_ids`` entry that does not resolve to a file (a bare URN
    or an id with no on-disk document) contributes no units rather than
    raising: the entry is a pointer, not a guaranteed local artifact, and the
    coverage gate keys on the documents it can actually read.

    Args:
        intent: The brief whose ``source_brief_ids`` documents are read.
        repo_root: The repo working-tree root that a repo-relative
            ``source_brief_ids`` entry resolves under.

    Returns:
        The concatenated source units across every resolvable source-brief
        document, with re-minted monotonic span ids. An empty list when the
        brief names no resolvable document.
    """
    from eawf.kernel.spec.common import SourceUnit
    from eawf.workflow.propose.generator import extract_units

    units: list[SourceUnit] = []
    index = 0
    for raw_ref in intent.source_brief_ids:
        ref_path = Path(raw_ref)
        if not ref_path.is_absolute():
            ref_path = repo_root / ref_path
        if not ref_path.is_file():
            logger.debug(f"source_brief_units skip_unresolvable ref={raw_ref!r}")
            continue
        text = ref_path.read_text(encoding="utf-8")
        for unit in extract_units(text):
            units.append(
                SourceUnit(
                    span_id=f"U-{index:03d}",
                    quote=unit.quote,
                    char_offset=unit.char_offset,
                )
            )
            index += 1
    logger.debug(f"source_brief_units refs={len(intent.source_brief_ids)} units={len(units)}")
    return units


__all__ = [
    "IntentBrief",
    "has_authoring_body",
    "source_brief_units",
]
