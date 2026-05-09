"""Skill execution orchestrator.

The :class:`Skill` ABC documents the canonical ``probe → action →
envelope`` contract. :func:`run_skill` is the single entry-point all
runtime adapters (Claude plugin, ``eawf skill run``) call: it executes
the lifecycle, catches every body-level exception, and ALWAYS returns a
fully-populated :class:`OutputEnvelope`.

Lifecycle:

1. Build the :class:`SkillContext` (caller-supplied; carries scope, session,
   instrument-probe report).
2. ``skill.probe(ctx) -> ProbeOutcome``. Hard-tool absent → ``status=blocked``;
   the engine emits an envelope with ``footer.repair_commands`` populated by
   the probe outcome and short-circuits without calling ``skill.action``.
3. ``skill.action(ctx) -> SkillResult``. The result tells the engine the
   terminal status, body, and the sidecar fields the engine needs to fold
   into the footer.
4. Engine assembles :class:`OutputEnvelope` with frozen ``started_at`` and
   ``finished_at`` timestamps and returns it. If ``skill.action`` raises any
   :class:`Exception`, the engine builds a ``status=failed`` envelope with
   ``footer.repair_commands = ["see body for traceback"]`` (callers can
   override via ``ctx.failure_repair_commands``) and the exception text in
   the body.

This wave (W01) defines the contract only. W02/W03 add the six core +
four meta skill subclasses.
"""

from __future__ import annotations

import logging
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from eawf.render.envelope import (
    EnvelopeBody,
    EnvelopeFooter,
    EnvelopeHeader,
    EnvelopeStatus,
    EnvelopeWarning,
    InstrumentStatus,
    OutputEnvelope,
    SkillName,
)

logger = logging.getLogger(__name__)


@dataclass
class SkillContext:
    """Context object passed to every :class:`Skill` lifecycle hook.

    Attributes:
        scope: Eä state-scope URN.
        session: Eä session URN.
        instrument_probe: Map of instrument-name → status. Filled in by
            the runtime adapter from the cached probe report.
        args: Free-form CLI args parsed for the skill (Pydantic body
            models live on the result side, not the input side, in W01).
        failure_repair_commands: Optional override for the engine's
            default ``["see body for traceback"]`` repair command list
            on the action-raised path.
    """

    scope: str
    session: str
    instrument_probe: dict[str, InstrumentStatus] = field(default_factory=dict)
    args: dict[str, Any] = field(default_factory=dict)
    failure_repair_commands: list[str] | None = None


@dataclass
class ProbeOutcome:
    """Result of :meth:`Skill.probe`.

    Attributes:
        ok: ``True`` when every hard requirement is satisfied. ``False``
            triggers the engine's blocked-envelope short-circuit.
        instrument_probe: Per-instrument status map; copied into the
            envelope header verbatim.
        repair_commands: When ``ok=False``, the list of CLI commands the
            agent should run to install the missing instrument(s).
        warnings: Optional warnings to fold into the envelope footer.
    """

    ok: bool
    instrument_probe: dict[str, InstrumentStatus] = field(default_factory=dict)
    repair_commands: list[str] = field(default_factory=list)
    warnings: list[EnvelopeWarning] = field(default_factory=list)


@dataclass
class SkillResult:
    """Result of :meth:`Skill.action`.

    Attributes:
        status: Terminal envelope status (``ok``, ``needs_user``,
            ``blocked``, ``failed``, or ``partial``).
        body: Body payload — string for raw markdown, dict for typed
            body models from :mod:`eawf.skills.bodies`.
        persisted_artifacts: URNs of artifacts persisted by the skill.
        persisted_store_records: URNs of store records appended by the
            skill (events.jsonl, research briefs, …).
        state_mutations: List of JSONPath-ish strings naming each state
            mutation the skill performed.
        evidence_refs: URNs of supporting evidence.
        next_valid_actions: CLI command strings the user/agent can run
            next.
        warnings: Warnings to fold into the envelope footer.
        repair_commands: Required when ``status in {blocked, failed}``;
            optional otherwise. The strict validator enforces this.
    """

    status: EnvelopeStatus
    body: EnvelopeBody = ""
    persisted_artifacts: list[str] = field(default_factory=list)
    persisted_store_records: list[str] = field(default_factory=list)
    state_mutations: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    next_valid_actions: list[str] = field(default_factory=list)
    warnings: list[EnvelopeWarning] = field(default_factory=list)
    repair_commands: list[str] | None = None


class Skill(ABC):
    """Base class for an Eä workflow skill.

    Subclasses fill in three pieces of behaviour:

    - :attr:`name` — frozen :data:`~eawf.render.envelope.SkillName` literal
      identifying the skill (``"/research"``, ``"/audit"``, …).
    - :meth:`probe` — verify every hard instrument is present and return a
      :class:`ProbeOutcome`. Cheap; no state mutation.
    - :meth:`action` — execute the skill body. Returns a :class:`SkillResult`
      describing the terminal status and what was persisted.

    The engine guarantees the returned envelope's ``header.skill`` matches
    :attr:`name` and that timestamps are monotonic.
    """

    #: Frozen literal for the skill name.
    name: SkillName

    @abstractmethod
    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        """Verify instruments and return a :class:`ProbeOutcome`.

        Implementations MUST be cheap and side-effect-free.
        """

    @abstractmethod
    def action(self, ctx: SkillContext) -> SkillResult:
        """Execute the skill body and return a :class:`SkillResult`.

        The engine wraps every call in a try/except; raising any exception
        is permitted and is mapped onto a ``status=failed`` envelope.
        """


def _build_envelope(
    *,
    skill_name: SkillName,
    ctx: SkillContext,
    started_at: datetime,
    finished_at: datetime,
    instrument_probe: dict[str, InstrumentStatus],
    status: EnvelopeStatus,
    body: EnvelopeBody,
    persisted_artifacts: list[str],
    persisted_store_records: list[str],
    state_mutations: list[str],
    evidence_refs: list[str],
    next_valid_actions: list[str],
    warnings: list[EnvelopeWarning],
    repair_commands: list[str] | None,
) -> OutputEnvelope:
    """Helper: assemble the :class:`OutputEnvelope` with the typed pieces.

    Centralised so the success, blocked, and failed paths all build the
    envelope through the same shape — the only differences are the
    status, body, and footer fields.
    """
    header = EnvelopeHeader(
        skill=skill_name,
        scope=ctx.scope,
        session=ctx.session,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        instrument_probe=instrument_probe,
    )
    footer = EnvelopeFooter(
        persisted_artifacts=persisted_artifacts,
        persisted_store_records=persisted_store_records,
        state_mutations=state_mutations,
        evidence_refs=evidence_refs,
        next_valid_actions=next_valid_actions,
        warnings=warnings,
        repair_commands=repair_commands,
    )
    return OutputEnvelope(header=header, body=body, footer=footer)


def _failed_traceback_body(exc: BaseException) -> str:
    """Render an exception traceback as the body of a failed envelope.

    The format is plain text (not markdown) so callers can grep stable
    keys (``Traceback``, the exception class name) without writing a
    markdown parser.
    """
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def run_skill(skill: Skill, ctx: SkillContext) -> OutputEnvelope:
    """Execute *skill* against *ctx* and return a fully-populated envelope.

    Lifecycle (always returns; never raises):

    1. Capture ``started_at``.
    2. Run :meth:`Skill.probe`. If ``not outcome.ok`` → return
       ``status=blocked`` envelope with ``footer.repair_commands`` from the
       probe outcome.
    3. Run :meth:`Skill.action`. If it raises → return ``status=failed``
       envelope with the traceback in the body and
       ``footer.repair_commands`` set to ``ctx.failure_repair_commands``
       (or a default if unset).
    4. Otherwise → return an envelope built from the action result.

    Args:
        skill: The :class:`Skill` to execute.
        ctx: Per-run context.

    Returns:
        A populated :class:`OutputEnvelope`. The envelope's header
        guarantees ``status``, ``started_at <= finished_at``, and the
        instrument-probe map.
    """
    started_at = datetime.now(UTC)
    skill_name: SkillName = skill.name

    # Probe phase.
    try:
        probe_outcome = skill.probe(ctx)
    except Exception as exc:
        # Probe must not crash the engine; surface as a failed envelope.
        logger.exception(f"run_skill: probe raised for skill {skill_name}")
        finished_at = datetime.now(UTC)
        return _build_envelope(
            skill_name=skill_name,
            ctx=ctx,
            started_at=started_at,
            finished_at=finished_at,
            instrument_probe={},
            status="failed",
            body=_failed_traceback_body(exc),
            persisted_artifacts=[],
            persisted_store_records=[],
            state_mutations=[],
            evidence_refs=[],
            next_valid_actions=[],
            warnings=[],
            repair_commands=ctx.failure_repair_commands or ["see body for traceback"],
        )

    if not probe_outcome.ok:
        finished_at = datetime.now(UTC)
        return _build_envelope(
            skill_name=skill_name,
            ctx=ctx,
            started_at=started_at,
            finished_at=finished_at,
            instrument_probe=probe_outcome.instrument_probe,
            status="blocked",
            body="instrument probe failed",
            persisted_artifacts=[],
            persisted_store_records=[],
            state_mutations=[],
            evidence_refs=[],
            next_valid_actions=[],
            warnings=probe_outcome.warnings,
            repair_commands=probe_outcome.repair_commands or ["install missing instrument"],
        )

    # Action phase. Any exception flips status to failed.
    try:
        result = skill.action(ctx)
    except Exception as exc:
        # Action must never crash the engine; surface as a failed envelope.
        logger.exception(f"run_skill: action raised for skill {skill_name}")
        finished_at = datetime.now(UTC)
        return _build_envelope(
            skill_name=skill_name,
            ctx=ctx,
            started_at=started_at,
            finished_at=finished_at,
            instrument_probe=probe_outcome.instrument_probe,
            status="failed",
            body=_failed_traceback_body(exc),
            persisted_artifacts=[],
            persisted_store_records=[],
            state_mutations=[],
            evidence_refs=[],
            next_valid_actions=[],
            warnings=probe_outcome.warnings,
            repair_commands=ctx.failure_repair_commands or ["see body for traceback"],
        )

    # Success path. Carry the action result into the envelope.
    finished_at = datetime.now(UTC)
    # If the result raised the failed/blocked status without
    # supplying repair commands, fall back to the engine's default so
    # the strict validator does not reject the envelope.
    repair = result.repair_commands
    if result.status in {"blocked", "failed"} and not repair:
        repair = ctx.failure_repair_commands or ["see body for details"]
    combined_warnings = list(probe_outcome.warnings) + list(result.warnings)
    return _build_envelope(
        skill_name=skill_name,
        ctx=ctx,
        started_at=started_at,
        finished_at=finished_at,
        instrument_probe=probe_outcome.instrument_probe,
        status=result.status,
        body=result.body,
        persisted_artifacts=result.persisted_artifacts,
        persisted_store_records=result.persisted_store_records,
        state_mutations=result.state_mutations,
        evidence_refs=result.evidence_refs,
        next_valid_actions=result.next_valid_actions,
        warnings=combined_warnings,
        repair_commands=repair,
    )


__all__ = [
    "ProbeOutcome",
    "Skill",
    "SkillContext",
    "SkillResult",
    "run_skill",
]
