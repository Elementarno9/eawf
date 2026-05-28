"""Build shared narrative bundles for outbound render surfaces."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import Iter, Phase, State, Wave


class NarrativeNotFoundError(LookupError):
    """Raised when a requested narrative scope is not present in state."""


class NarrativeBundle(BaseModel):
    """Small shared narrative consumed by PR and release renderers."""

    model_config = ConfigDict(extra="forbid")

    scope_id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1, max_length=120)]
    what: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1, max_length=8)]
    why: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1, max_length=6)]
    validation: Annotated[
        list[Annotated[str, Field(min_length=1)]],
        Field(min_length=1, max_length=8),
    ]
    risks: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1, max_length=6)]
    changelog: Annotated[list[Annotated[str, Field(min_length=1)]], Field(max_length=8)] = Field(
        default_factory=list
    )


def _bounded(items: Iterable[str], *, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = " ".join(item.split())
        if not cleaned or cleaned in seen:
            continue
        out.append(cleaned)
        seen.add(cleaned)
        if len(out) == limit:
            break
    return out


def _phase_iters(state: State, phase_id: str) -> list[Iter]:
    return [
        iter_record
        for iter_record in sorted(
            (state.iters or {}).values(), key=lambda item: natural_key(item.id)
        )
        if iter_record.phase_id == phase_id
    ]


def _phase_waves(state: State, iter_ids: set[str]) -> list[Wave]:
    return [
        wave
        for wave in sorted((state.waves or {}).values(), key=lambda item: natural_key(item.id))
        if wave.iter_id in iter_ids
    ]


def _why_lines(phase: Phase) -> list[str]:
    intent = phase.intent
    candidates: list[str] = []
    if intent is not None:
        # W24-audited intent fields drive the why list: the optional
        # priority_rationale leads (it names the prioritization
        # trade-off), then the required problem and desired_outcome
        # pair frame the gap and target state.
        for item in (intent.priority_rationale, intent.problem, intent.desired_outcome):
            if item:
                candidates.append(item)
    if phase.description:
        candidates.append(phase.description)
    candidates.append("No explicit motivation recorded.")
    return _bounded(candidates, limit=6)


def _validation_lines(state: State, phase: Phase, waves: list[Wave]) -> list[str]:
    lines: list[str] = []
    if phase.audit_id:
        audit = (state.audits or {}).get(phase.audit_id)
        if audit is not None and audit.verdict is not None:
            lines.append(f"Audit `{phase.audit_id}` verdict: {audit.verdict.value}.")
        else:
            lines.append(f"Audit `{phase.audit_id}` recorded.")
    closed = [wave for wave in waves if wave.status.value == "closed"]
    if waves:
        lines.append(f"{len(closed)}/{len(waves)} wave(s) closed.")
    committed = [wave for wave in waves if wave.commit]
    if committed:
        lines.append(f"{len(committed)} wave commit(s) pinned in state.")
    if not lines:
        lines.append("No validation record available.")
    return _bounded(lines, limit=8)


def _risk_lines(phase: Phase, waves: list[Wave]) -> list[str]:
    lines: list[str] = []
    open_waves = [wave for wave in waves if wave.status.value != "closed"]
    if open_waves:
        ids = ", ".join(f"`{wave.id}`" for wave in open_waves[:4])
        lines.append(f"Open wave(s): {ids}.")
    if not phase.audit_id:
        lines.append("No phase audit recorded.")
    if not lines:
        lines.append("No open risks recorded.")
    return _bounded(lines, limit=6)


def build_narrative(state: State, scope_id: str) -> NarrativeBundle:
    """Build a shared narrative bundle for a phase scope.

    Args:
        state: Loaded, validated state.
        scope_id: Phase id to render.

    Raises:
        NarrativeNotFoundError: When *scope_id* is not a known phase.
    """
    phase = (state.phases or {}).get(scope_id)
    if phase is None:
        raise NarrativeNotFoundError(f"phase not found: {scope_id!r}")

    iters = _phase_iters(state, scope_id)
    waves = _phase_waves(state, {iter_record.id for iter_record in iters})
    what = _bounded(
        [
            f"`{phase.id}` {phase.title} ({phase.status.value}).",
            *(f"`{wave.id}` {wave.outcome or wave.title}." for wave in waves),
        ],
        limit=8,
    )
    changelog = _bounded(
        [
            f"{phase.title}.",
            *(wave.outcome or wave.title for wave in waves),
        ],
        limit=8,
    )
    return NarrativeBundle(
        scope_id=scope_id,
        title=f"{phase.id}: {phase.title}",
        what=what,
        why=_why_lines(phase),
        validation=_validation_lines(state, phase, waves),
        risks=_risk_lines(phase, waves),
        changelog=changelog,
    )


def render_narrative_bundle(bundle: NarrativeBundle) -> str:
    """Render a bundle as What/Why/Validation/Risks markdown sections."""
    lines: list[str] = []
    for heading, bullets in (
        ("What", bundle.what),
        ("Why", bundle.why),
        ("Validation", bundle.validation),
        ("Risks", bundle.risks),
    ):
        lines.extend([f"## {heading}", ""])
        lines.extend(f"- {bullet}" for bullet in bullets)
        lines.append("")
    return "\n".join(lines).rstrip()


def generated_changelog_lines(bundle: NarrativeBundle) -> list[str]:
    """Return changelog bullets generated from a narrative bundle."""
    return [f"- {item}" for item in bundle.changelog]


__all__ = [
    "NarrativeBundle",
    "NarrativeNotFoundError",
    "build_narrative",
    "generated_changelog_lines",
    "render_narrative_bundle",
]
