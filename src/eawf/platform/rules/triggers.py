"""Practice triggers evaluated at the skill engine's declared decision points.

A practice rule applies at a moment, not continuously, so recalling it from
memory is exactly how it gets missed. Each trigger here binds one conduct
obligation to an observable condition on a skill's terminal result, at one of
three decision points:

- ``dispatch``: a coordination report that planned two or more units of work.
- ``choice``: a result that stops to put a question to the operator.
- ``report``: a verification report that claims the subject passed.

When a trigger fires, one ``practice_trigger`` event records the evaluation
whether or not the obligation was met, so a practice that always holds is
visible as fired-and-met rather than absent. A fired trigger whose obligation
is unmet also records a conduct deviation in the machine-local store, which
is what makes the per-rule miss rate countable.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, get_args

from eawf.kernel.state.enums import IncidentSeverity
from eawf.kernel.store.kinds.events.base import RuntimeTriple
from eawf.platform.rules.conduct import record_conduct_deviation
from eawf.runtime.runtimes.coauthor import resolve_runtime_explicit
from eawf.surfaces.render.envelope import EnvelopeBody, EnvelopeStatus, EnvelopeWarning

logger = logging.getLogger(__name__)

DecisionPoint = Literal["dispatch", "choice", "report"]

#: Every declared decision point, in the order a run reaches them.
DECISION_POINTS: Final[tuple[DecisionPoint, ...]] = get_args(DecisionPoint)

#: ``event_type`` of the event recording one fired trigger.
PRACTICE_TRIGGER_EVENT_TYPE: Final = "practice_trigger"

#: Footer-warning code attached when a fired trigger's obligation is unmet.
PRACTICE_MISS_CODE: Final = "practice_miss"

#: The eawf skills ship as a Claude plugin, so a run that names no runtime
#: explicitly is attributed to it rather than left unrecorded.
_DEFAULT_RUNTIME: Final[RuntimeTriple] = "claude"


@dataclass(frozen=True)
class PracticeTrigger:
    """One conduct obligation bound to the moment it applies.

    Attributes:
        obligation_id: The conduct obligation the trigger guards.
        decision_point: Where in a run the trigger is evaluated.
        severity: How much a miss matters.
        fires: Whether a terminal result reaches the trigger's moment.
        met: Whether a result the trigger fired on satisfies the obligation.
    """

    obligation_id: str
    decision_point: DecisionPoint
    severity: IncidentSeverity
    fires: Callable[[EnvelopeStatus, EnvelopeBody], bool]
    met: Callable[[EnvelopeBody], bool]


@dataclass(frozen=True)
class TriggerEvaluation:
    """One trigger that fired on a result, with its verdict.

    Attributes:
        trigger: The trigger that fired.
        met: Whether its obligation was satisfied.
    """

    trigger: PracticeTrigger
    met: bool


@dataclass(frozen=True)
class DecisionPointOutcome:
    """What evaluating the triggers on one result recorded.

    Attributes:
        evaluations: Every trigger that fired, in declaration order.
        records: Ids of the ``practice_trigger`` events appended.
        warnings: One ``practice_miss`` warning per unmet obligation.
    """

    evaluations: tuple[TriggerEvaluation, ...] = ()
    records: tuple[str, ...] = ()
    warnings: tuple[EnvelopeWarning, ...] = ()


def _body_kind(body: EnvelopeBody, kind: str) -> Mapping[str, Any] | None:
    """Return *body* when it is a typed dict body of the given ``kind``."""
    if isinstance(body, dict) and body.get("kind") == kind:
        return body
    return None


def _plans_several_units(status: EnvelopeStatus, body: EnvelopeBody) -> bool:
    """Fire when a coordination report planned two or more units of work."""
    report = _body_kind(body, "coordination_report")
    if report is None:
        return False
    plan = report.get("plan") or {}
    return len(plan.get("parallel") or ()) + len(plan.get("sequential") or ()) >= 2


def _sequenced_only_with_reason(body: EnvelopeBody) -> bool:
    """Hold when nothing was serialized, or the serialization names its constraint.

    A sequential arm with no stated constraint is units the plan could have
    fanned out and ran in order anyway.
    """
    report = _body_kind(body, "coordination_report") or {}
    plan = report.get("plan") or {}
    return not plan.get("sequential") or bool(str(plan.get("constraint") or "").strip())


def _asks_operator(status: EnvelopeStatus, body: EnvelopeBody) -> bool:
    """Fire when the run stops to put a question to the operator."""
    return status == "needs_user"


def _question_is_typed(body: EnvelopeBody) -> bool:
    """Hold when the question is typed and every option says what it does.

    A string body is a free-text question, which is the shape the obligation
    exists to prevent.
    """
    if not isinstance(body, dict):
        return False
    question = body.get("user_question")
    if not isinstance(question, dict):
        return False
    options = question.get("options") or ()
    return bool(options) and all(str(option.get("description") or "").strip() for option in options)


def _claims_pass(status: EnvelopeStatus, body: EnvelopeBody) -> bool:
    """Fire when a verification report claims its subject passed."""
    report = _body_kind(body, "verification_report")
    return report is not None and report.get("outcome") == "passed"


def _every_row_evidenced(body: EnvelopeBody) -> bool:
    """Hold when every criterion row passed and names what was run against it."""
    report = _body_kind(body, "verification_report") or {}
    rows = report.get("rows") or ()
    return bool(rows) and all(
        row.get("verdict") == "passed" and str(row.get("falsifier") or "").strip() for row in rows
    )


#: The practice triggers the skill engine evaluates, one for each decision point.
PRACTICE_TRIGGERS: Final[tuple[PracticeTrigger, ...]] = (
    PracticeTrigger(
        obligation_id="conduct.concurrent-independent-dispatch",
        decision_point="dispatch",
        severity=IncidentSeverity.MEDIUM,
        fires=_plans_several_units,
        met=_sequenced_only_with_reason,
    ),
    PracticeTrigger(
        obligation_id="conduct.typed-choice",
        decision_point="choice",
        severity=IncidentSeverity.MEDIUM,
        fires=_asks_operator,
        met=_question_is_typed,
    ),
    PracticeTrigger(
        obligation_id="conduct.verdict-needs-evidence",
        decision_point="report",
        severity=IncidentSeverity.HIGH,
        fires=_claims_pass,
        met=_every_row_evidenced,
    ),
)


def evaluate_practice_triggers(
    status: EnvelopeStatus,
    body: EnvelopeBody,
    *,
    triggers: tuple[PracticeTrigger, ...] = PRACTICE_TRIGGERS,
) -> tuple[TriggerEvaluation, ...]:
    """Evaluate every trigger against one terminal result, without side effects.

    Args:
        status: The result's terminal envelope status.
        body: The result's already-serialized body.
        triggers: The triggers to evaluate.

    Returns:
        One evaluation per trigger that fired, in declaration order; empty
        when the result reaches no decision point.
    """
    return tuple(
        TriggerEvaluation(trigger=trigger, met=trigger.met(body))
        for trigger in triggers
        if trigger.fires(status, body)
    )


def resolve_trigger_runtime(env: Mapping[str, str]) -> RuntimeTriple:
    """Resolve the runtime a deviation is attributed to.

    Args:
        env: The process environment.

    Returns:
        The explicitly configured runtime when it is one the deviation store
        splits by, else the default runtime.
    """
    resolved = resolve_runtime_explicit(env=env)
    if resolved == "codex":
        return "codex"
    if resolved == "opencode":
        return "opencode"
    return _DEFAULT_RUNTIME


def fire_practice_triggers(
    *,
    status: EnvelopeStatus,
    body: EnvelopeBody,
    scope_id: str,
    run_id: str,
    state_path: Path | None = None,
) -> DecisionPointOutcome:
    """Evaluate the triggers on one result and record what fired.

    Nothing is read or written when no trigger fires, so a run that reaches
    no decision point costs nothing.

    Args:
        status: The result's terminal envelope status.
        body: The result's already-serialized body.
        scope_id: The lifecycle scope the run served.
        run_id: The run the result came from.
        state_path: The tree's ``state.json``; resolved from the active scope
            when ``None``.

    Returns:
        The evaluations, the trigger event ids and the miss warnings.

    Raises:
        pydantic.ValidationError: When a trigger names an obligation the
            conduct module does not define.
        StateConflict: When a store's append lock cannot be acquired.
    """
    evaluations = evaluate_practice_triggers(status, body)
    if not evaluations:
        return DecisionPointOutcome()
    from eawf.workflow.skills._common import emit_event, resolve_active_state_path

    path = state_path if state_path is not None else resolve_active_state_path()
    runtime = resolve_trigger_runtime(os.environ)
    records: list[str] = []
    warnings: list[EnvelopeWarning] = []
    for evaluation in evaluations:
        trigger = evaluation.trigger
        event_id = emit_event(
            state_path=path,
            scope_id=scope_id,
            event_type=PRACTICE_TRIGGER_EVENT_TYPE,
            summary=(
                f"practice trigger {trigger.obligation_id} at {trigger.decision_point}: "
                f"{'met' if evaluation.met else 'missed'}"
            ),
            payload={
                "extras": {
                    "decision_point": trigger.decision_point,
                    "obligation_id": trigger.obligation_id,
                    "met": evaluation.met,
                }
            },
        )
        records.append(event_id)
        if evaluation.met:
            continue
        record_conduct_deviation(
            path,
            obligation_id=trigger.obligation_id,
            scope_id=scope_id,
            run_id=run_id,
            runtime=runtime,
            detection="practice_trigger",
            severity=trigger.severity,
            evidence_ref=event_id,
        )
        warnings.append(
            EnvelopeWarning(
                code=PRACTICE_MISS_CODE,
                detail=(
                    f"{trigger.obligation_id} was not met at the {trigger.decision_point} "
                    f"decision point (evidence {event_id})"
                ),
            )
        )
    return DecisionPointOutcome(
        evaluations=evaluations, records=tuple(records), warnings=tuple(warnings)
    )


__all__ = [
    "DECISION_POINTS",
    "PRACTICE_MISS_CODE",
    "PRACTICE_TRIGGERS",
    "PRACTICE_TRIGGER_EVENT_TYPE",
    "DecisionPoint",
    "DecisionPointOutcome",
    "PracticeTrigger",
    "TriggerEvaluation",
    "evaluate_practice_triggers",
    "fire_practice_triggers",
    "resolve_trigger_runtime",
]
