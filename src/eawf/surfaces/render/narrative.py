"""Build shared narrative bundles for outbound render surfaces.

The renderer exposes one entry point — :func:`build_narrative` — that
dispatches to a per-scope-kind builder based on the resolved entity
(:class:`Wave` / :class:`Iter` / :class:`Phase` / :class:`BacklogItem`).
The bundle shape is uniform (``what`` / ``why`` / ``validation`` /
``risks`` plus an optional ``changelog`` list) so downstream consumers
(release notes, changelog, TUI ``d`` tab) can render any scope through
the same pipeline. The kind-specific shape diverges per builder so two
sibling waves under the same phase produce demonstrably different
preview bodies.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    DispatchNote,
    WaveStatus,
)
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import (
    ActualSummary,
    BacklogItem,
    Iter,
    Phase,
    State,
    Wave,
)


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


def _iter_waves(state: State, iter_id: str) -> list[Wave]:
    return [
        wave
        for wave in sorted((state.waves or {}).values(), key=lambda item: natural_key(item.id))
        if wave.iter_id == iter_id
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


# ---------------------------------------------------------------------------
# wave / iter / backlog narrative builders
# ---------------------------------------------------------------------------


def _wave_actual(state: State, wave_id: str) -> ActualSummary | None:
    """Return the latest :class:`ActualSummary` scoped to *wave_id*, if any."""
    actuals = state.actuals or {}
    matches = [actual for actual in actuals.values() if actual.scope_id == wave_id]
    if not matches:
        return None
    return max(matches, key=lambda actual: actual.updated_at)


def _wave_dispatch_error_count(wave: Wave) -> int:
    """Count dispatch-history rows annotating an error-driven transition."""
    error_notes = {
        DispatchNote.SWITCH_ON_ERROR,
        DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH,
    }
    return sum(1 for ann in wave.dispatch_history if ann.note in error_notes)


def _wave_blocked_report_count(reports: Iterable[object]) -> int:
    """Count agent-report rows whose verdict is :attr:`AgentReportVerdict.BLOCKED`.

    The reports may be raw rollup rows (carrying ``payload.body.verdict``).
    Untyped to avoid an import cycle with the workflow layer.
    """
    count = 0
    for report in reports:
        payload = getattr(report, "payload", None)
        body = getattr(payload, "body", None)
        verdict = getattr(body, "verdict", None)
        if verdict is AgentReportVerdict.BLOCKED:
            count += 1
    return count


def _wave_what(wave: Wave) -> list[str]:
    """Return the ``what`` candidates for a wave bundle."""
    intent = wave.intent
    candidates: list[str] = []
    if intent is not None:
        candidates.append(intent.problem)
        candidates.append(intent.desired_outcome)
    if wave.status is WaveStatus.CLOSED and wave.outcome:
        candidates.append(f"Outcome: {wave.outcome}.")
    if not candidates:
        candidates.append(f"`{wave.id}` {wave.title} ({wave.status.value}).")
    return candidates


def _wave_why(wave: Wave) -> list[str]:
    """Return the ``why`` candidates for a wave bundle."""
    intent = wave.intent
    candidates: list[str] = []
    if intent is not None:
        if intent.priority_rationale:
            candidates.append(intent.priority_rationale)
        if intent.planned_steps:
            candidates.append(f"Plan starts: {intent.planned_steps[0]}")
    if not candidates:
        candidates.append("No explicit motivation recorded.")
    return candidates


def _wave_validation(state: State, wave: Wave) -> list[str]:
    """Return the ``validation`` candidates for a wave bundle."""
    lines: list[str] = []
    lines.append(
        f"Commit `{wave.commit[:12]}` pinned in state."
        if wave.commit is not None
        else "No commit pinned."
    )
    attempt_count = len(wave.sessions)
    lines.append(
        f"{attempt_count} claim attempt(s) recorded."
        if attempt_count
        else "No claim attempts recorded."
    )
    actual = _wave_actual(state, wave.id)
    if actual is None:
        lines.append("No rollup yet.")
    else:
        lines.append(
            f"Actual `{actual.id}` status: {actual.status.value}; "
            f"elapsed EU: {actual.elapsed_eu:.2f}."
        )
    return lines


def _wave_risks(wave: Wave, reports: Iterable[object]) -> list[str]:
    """Return the ``risks`` candidates for a wave bundle."""
    risks: list[str] = []
    if wave.intent is not None:
        risks.extend(wave.intent.risks)
    risks.append(f"Dispatch errors: {_wave_dispatch_error_count(wave)}.")
    risks.append(f"Blocked agent reports: {_wave_blocked_report_count(reports)}.")
    return risks


def _wave_changelog(wave: Wave) -> list[str]:
    """Return the ``changelog`` candidates for a wave bundle."""
    if wave.outcome:
        return [wave.outcome]
    if wave.title:
        return [f"{wave.title}."]
    return []


def _build_wave_narrative(
    state: State,
    wave: Wave,
    *,
    reports: Iterable[object] = (),
) -> NarrativeBundle:
    """Build a :class:`NarrativeBundle` for a single wave.

    The wave bundle is distinct from its parent iter / phase bundle: it
    quotes the wave's :class:`IntentBrief` directly, surfaces the pinned
    commit + claim attempts + the latest :class:`ActualSummary`, and
    folds dispatch-error + blocked-report counts into the risks block so
    two waves under one phase render demonstrably different bodies.
    """
    changelog = _wave_changelog(wave)
    return NarrativeBundle(
        scope_id=wave.id,
        title=f"{wave.id}: {wave.title}",
        what=_bounded(_wave_what(wave), limit=8),
        why=_bounded(_wave_why(wave), limit=6),
        validation=_bounded(_wave_validation(state, wave), limit=8),
        risks=_bounded(_wave_risks(wave, reports), limit=6),
        changelog=_bounded(changelog, limit=8) if changelog else [],
    )


def _iter_what(iter_record: Iter, waves: list[Wave]) -> list[str]:
    """Return the ``what`` candidates for an iter bundle."""
    intent = iter_record.intent
    candidates: list[str] = [
        f"`{iter_record.id}` {iter_record.title} ({iter_record.status.value}).",
    ]
    if intent is not None:
        candidates.append(intent.problem)
        candidates.append(intent.desired_outcome)
    for wave in waves:
        candidates.append(f"`{wave.id}` {wave.title}.")
    return candidates


def _iter_why(iter_record: Iter) -> list[str]:
    """Return the ``why`` candidates for an iter bundle."""
    intent = iter_record.intent
    candidates: list[str] = []
    if intent is not None:
        if intent.priority_rationale:
            candidates.append(intent.priority_rationale)
        if intent.planned_steps:
            candidates.append(f"Plan starts: {intent.planned_steps[0]}")
    if iter_record.description:
        candidates.append(iter_record.description)
    if not candidates:
        candidates.append("No explicit motivation recorded.")
    return candidates


def _iter_validation(state: State, iter_record: Iter, waves: list[Wave]) -> list[str]:
    """Return the ``validation`` candidates for an iter bundle."""
    lines: list[str] = []
    if iter_record.audit_id:
        audit = (state.audits or {}).get(iter_record.audit_id)
        if audit is not None and audit.verdict is not None:
            lines.append(f"Audit `{iter_record.audit_id}` verdict: {audit.verdict.value}.")
        else:
            lines.append(f"Audit `{iter_record.audit_id}` recorded.")
    closed = [wave for wave in waves if wave.status is WaveStatus.CLOSED]
    lines.append(
        f"{len(closed)}/{len(waves)} wave(s) closed." if waves else "No waves under this iter."
    )
    return lines


def _iter_risks(iter_record: Iter, waves: list[Wave]) -> list[str]:
    """Return the ``risks`` candidates for an iter bundle."""
    risks: list[str] = []
    if iter_record.intent is not None:
        risks.extend(iter_record.intent.risks)
    open_waves = [wave for wave in waves if wave.status is not WaveStatus.CLOSED]
    if open_waves:
        ids = ", ".join(f"`{wave.id}`" for wave in open_waves[:4])
        risks.append(f"Open wave(s): {ids}.")
    if not iter_record.audit_id:
        risks.append("No iter audit recorded.")
    if not risks:
        risks.append("No open risks recorded.")
    return risks


def _build_iter_narrative(state: State, iter_record: Iter) -> NarrativeBundle:
    """Build a :class:`NarrativeBundle` for a single iter.

    The iter bundle aggregates its child waves' titles into the ``what``
    list, names the parent phase, and surfaces the closed-vs-total
    progress tally so the iter view differs from its parent phase
    bundle (which aggregates across all child iters).
    """
    waves = _iter_waves(state, iter_record.id)
    return NarrativeBundle(
        scope_id=iter_record.id,
        title=f"{iter_record.id}: {iter_record.title}",
        what=_bounded(_iter_what(iter_record, waves), limit=8),
        why=_bounded(_iter_why(iter_record), limit=6),
        validation=_bounded(_iter_validation(state, iter_record, waves), limit=8),
        risks=_bounded(_iter_risks(iter_record, waves), limit=6),
    )


def _backlog_what(item: BacklogItem) -> list[str]:
    """Return the ``what`` candidates for a backlog bundle."""
    candidates: list[str] = [f"`{item.id}` {item.title} ({item.status.value})."]
    if item.description:
        candidates.append(item.description)
    if item.intent is not None:
        candidates.append(item.intent.problem)
        candidates.append(item.intent.desired_outcome)
    return candidates


def _backlog_why(item: BacklogItem) -> list[str]:
    """Return the ``why`` candidates for a backlog bundle."""
    candidates: list[str] = [f"Priority: {item.priority.value}."]
    intent = item.intent
    if intent is not None:
        if intent.priority_rationale:
            candidates.append(intent.priority_rationale)
        if intent.planned_steps:
            candidates.append(f"Plan starts: {intent.planned_steps[0]}")
    return candidates


def _backlog_validation(item: BacklogItem) -> list[str]:
    """Return the ``validation`` candidates for a backlog bundle."""
    lines: list[str] = []
    if item.resolution is not None:
        lines.append(f"Resolution: {item.resolution}")
    if item.commit is not None:
        lines.append(f"Commit `{item.commit[:12]}` pinned in state.")
    if item.closed_at is not None:
        lines.append(f"Closed at {item.closed_at.isoformat()}.")
    if not lines:
        lines.append("No validation record available.")
    return lines


def _backlog_risks(item: BacklogItem) -> list[str]:
    """Return the ``risks`` candidates for a backlog bundle."""
    risks: list[str] = []
    if item.intent is not None:
        risks.extend(item.intent.risks)
    if item.status.value != "closed":
        risks.append(f"Item still {item.status.value}; priority {item.priority.value}.")
    if not risks:
        risks.append("No open risks recorded.")
    return risks


def _build_backlog_narrative(item: BacklogItem) -> NarrativeBundle:
    """Build a :class:`NarrativeBundle` for a single backlog item.

    The backlog bundle leads with the item's priority/status and (when
    set) its resolution, so the view is visibly distinct from a wave
    / iter / phase bundle. Validation quotes the item's commit when
    pinned; risks reflect the priority / open-state.
    """
    return NarrativeBundle(
        scope_id=item.id,
        title=f"{item.id}: {item.title}",
        what=_bounded(_backlog_what(item), limit=8),
        why=_bounded(_backlog_why(item), limit=6),
        validation=_bounded(_backlog_validation(item), limit=8),
        risks=_bounded(_backlog_risks(item), limit=6),
    )


def _build_phase_narrative(state: State, phase: Phase) -> NarrativeBundle:
    """Build a :class:`NarrativeBundle` for a single phase (legacy path)."""
    iters = _phase_iters(state, phase.id)
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
        scope_id=phase.id,
        title=f"{phase.id}: {phase.title}",
        what=what,
        why=_why_lines(phase),
        validation=_validation_lines(state, phase, waves),
        risks=_risk_lines(phase, waves),
        changelog=changelog,
    )


def build_narrative(
    state: State,
    scope_id: str,
    *,
    reports: Iterable[object] = (),
) -> NarrativeBundle:
    """Build a shared narrative bundle for *scope_id*.

    Dispatches on the resolved entity kind: a wave id yields a
    wave-shaped bundle, an iter id yields an iter bundle, a phase id
    yields the phase rollup, and a backlog id yields a backlog bundle.
    The phase path stays the canonical fallback the legacy callers
    (release notes / changelog) consume.

    Args:
        state: Loaded, validated state.
        scope_id: Wave / iter / phase / backlog id to render.
        reports: Optional agent-report rows scoped to the wave (only
            consumed by the wave builder; ignored for the other kinds).

    Raises:
        NarrativeNotFoundError: When *scope_id* matches no known scope.
    """
    waves = state.waves or {}
    wave = waves.get(scope_id)
    if wave is not None:
        return _build_wave_narrative(state, wave, reports=reports)
    iters = state.iters or {}
    iter_record = iters.get(scope_id)
    if iter_record is not None:
        return _build_iter_narrative(state, iter_record)
    phases = state.phases or {}
    phase = phases.get(scope_id)
    if phase is not None:
        return _build_phase_narrative(state, phase)
    backlog = state.backlog or {}
    item = backlog.get(scope_id)
    if item is not None:
        return _build_backlog_narrative(item)
    raise NarrativeNotFoundError(f"scope not found: {scope_id!r}")


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
