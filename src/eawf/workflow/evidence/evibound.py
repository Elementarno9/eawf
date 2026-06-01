"""EviBound rung-1 deterministic gate (Keystone-B body).

This module is the rung-1 layer of the EviBound evidence chain. It sits
directly on top of the W07 reference resolver
(:func:`eawf.workflow.evidence.resolve.resolve`) and the v0.4 verify-spine
gate machinery, and it un-idles the
:attr:`eawf.kernel.spec.intent.IntentBrief.evidence_refs` contract: until
this wave the field existed on the model but nothing ever ran the gate
that the field's docstring promises ("the EviBound gate fails the brief
at promotion time").

Two public surfaces
-------------------
* :func:`run_rung1_gate` — the criterion-certification rung. It reuses
  the verify-spine gate machinery
  ``compile_gate -> _run_deterministic_gate -> run_checks`` (the same
  chain the deterministic floor in
  :mod:`eawf.workflow.verify.readiness` runs) so the rung-1 pass/fail
  bit is ``passed == (returncode == 0)`` by construction — no second
  copy of the subprocess + diff-base + scope-resolution logic. A
  ``"pass"`` status CERTIFIES the criterion: the function returns a
  :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` with
  ``status="pass"`` that a caller appends to the evidence store. A
  ``"fail"`` / ``"blocked"`` status returns the matching non-certifying
  record rather than raising.

* :func:`check_brief_promotable` — the brief-promotion gate. It walks
  an :class:`IntentBrief`'s ``evidence_refs`` and routes each ref
  through :func:`resolve` (the rung-1 deterministic resolution layer).
  A brief is promotable **iff** every ref resolves
  (``ResolveStatus.RESOLVED``). The result is a typed
  :class:`BriefPromotionGate` carrying the per-ref breakdown plus a
  single ``promotable`` bit and human-readable ``reasons`` the
  promotion call-site folds into its rejection message.

Routing key — the landmine
---------------------------
:func:`check_brief_promotable` resolves each ``evidence_refs`` entry as
a :data:`~eawf.kernel.spec.common.CriterionEvidenceKind` of
``"deterministic"`` — the criterion-verification flavor the rung-1
deterministic check understands. It does NOT route on
:data:`~eawf.kernel.spec.common.EvidenceKind` (the reference-target
vocabulary ``audit | artifact | decision | store_record |
external_url``). The two enums are deliberately distinct (see
``common.py`` + the W07 resolver docstring); routing a brief's refs on
``EvidenceKind`` would mis-dispatch the verification flavor onto the
reference-target vocabulary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from eawf.kernel.spec.common import CriterionEvidenceKind, CriterionSpec, GateSpec
from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.store.kinds.evidence import EvidenceRecord, EvidenceStatus, mint_evidence_id
from eawf.workflow.evidence.resolve import ResolveResult, ResolveStatus, resolve
from eawf.workflow.verify.readiness import _run_deterministic_gate

logger = logging.getLogger(__name__)

#: The criterion-verification flavor an ``IntentBrief.evidence_refs``
#: entry is resolved under. Brief refs are plain strings (repo-relative
#: path / URN / external URL) with no jury vote or operator sign-off, so
#: they route through the deterministic rung-1 resolver.
_BRIEF_REF_EVIDENCE_KIND: Final[CriterionEvidenceKind] = "deterministic"


def run_rung1_gate(
    gate: GateSpec,
    criterion: CriterionSpec,
    *,
    scope_id: str,
    runner_cwd: Path,
) -> EvidenceRecord:
    """Run *gate* through the rung-1 deterministic machinery and certify *criterion*.

    Reuses the verify-spine chain
    :func:`eawf.workflow.verify.readiness._run_deterministic_gate`, which
    itself delegates to
    :func:`eawf.workflow.verify.compile.compile_gate` (shape transform)
    and :func:`eawf.workflow.audit_dsl.runner.run_checks` (live
    subprocess). The audit-DSL ``command_exit_zero`` check maps
    ``passed = completed.returncode == 0``, so the rung-1 pass bit is
    ``returncode == 0`` by construction — this function does not parse a
    returncode itself, it inherits the mapping from the single
    authoritative gate runner.

    On a ``"pass"`` status the criterion is CERTIFIED: the returned
    :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` carries
    ``status="pass"`` and references the gate id, so the caller appends
    it to the evidence store as proof the criterion's deterministic
    floor held at this SHA. A ``"fail"`` / ``"blocked"`` status returns
    the matching non-certifying record rather than raising — a failing
    gate is data, not an exception, on the verify-spine read path.

    Args:
        gate: Typed gate row attached to *criterion*. Only
            ``evidence_kind == "deterministic"`` criteria have a rung-1
            gate (the caller is responsible for that precondition; a
            non-deterministic criterion compiles to ``None`` and yields
            a ``"blocked"`` record).
        criterion: Parent criterion the gate scores. Its ``id`` is the
            certification target.
        scope_id: URN of the scope (wave / iter / phase / decision) the
            evidence row backs — recorded on the
            :class:`EvidenceRecord` so close-readiness can filter by
            scope.
        runner_cwd: Working directory threaded into the gate runner for
            the subprocess + git diff-base + scope resolution.

    Returns:
        An :class:`EvidenceRecord` whose ``status`` mirrors the rung-1
        gate outcome (``pass`` certifies; ``fail`` / ``blocked`` do
        not).
    """
    # ``_run_deterministic_gate`` is typed ``-> str`` but only ever
    # returns a ``GateStatus`` literal (``pass`` / ``fail`` / ``blocked``),
    # each a member of the wider ``EvidenceStatus`` set — the cast aligns
    # the static type with that runtime guarantee.
    status = cast(EvidenceStatus, _run_deterministic_gate(gate, criterion, runner_cwd=runner_cwd))
    certified = status == "pass"
    summary = (
        f"rung-1 deterministic gate {gate.id!r} certified criterion {criterion.id!r}"
        if certified
        else f"rung-1 deterministic gate {gate.id!r} {status} for criterion {criterion.id!r}"
    )
    record = EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=scope_id,
        produced_by="tool",
        evidence_kind="deterministic",
        status=status,
        summary=summary,
        refs=[gate.id, criterion.id],
        created_at=datetime.now(UTC),
    )
    logger.debug(
        f"run_rung1_gate gate={gate.id!r} criterion={criterion.id!r} "
        f"status={status!r} certified={certified}"
    )
    return record


@dataclass(frozen=True)
class BriefRefOutcome:
    """One ``evidence_refs`` entry's rung-1 resolution outcome.

    Attributes:
        ref: The evidence reference that was resolved.
        result: The :class:`~eawf.workflow.evidence.resolve.ResolveResult`
            the rung-1 resolver returned for *ref*.
        passed: ``True`` iff ``result.status is ResolveStatus.RESOLVED``.
            A ``DEFERRED`` or ``UNRESOLVED`` ref does NOT pass rung-1.
    """

    ref: str
    result: ResolveResult
    passed: bool


@dataclass(frozen=True)
class BriefPromotionGate:
    """Typed verdict of the brief-promotion EviBound gate.

    Produced by :func:`check_brief_promotable`. The gate is the bit a
    promotion call-site checks before letting a brief promote: a brief
    is promotable iff every ``evidence_refs`` entry passed rung-1
    resolution.

    Attributes:
        promotable: ``True`` iff every ref in :attr:`outcomes` passed
            rung-1 (``ResolveStatus.RESOLVED``). An empty
            ``evidence_refs`` list is promotable trivially — the brief
            simply carries no claims to gate, matching the
            :class:`IntentBrief` contract that an unsourced brief still
            ingests (the gate, not ingestion, is where unsourced claims
            are caught, and a brief with zero refs has no claim to
            catch).
        outcomes: One :class:`BriefRefOutcome` per ``evidence_refs``
            entry, in declaration order.
        reasons: Human-readable rejection lines — one per non-passing
            ref. Empty when :attr:`promotable` is ``True``. The
            promotion call-site folds these into its error envelope.
    """

    promotable: bool
    outcomes: tuple[BriefRefOutcome, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)


def check_brief_promotable(
    brief: IntentBrief,
    *,
    project_root: Path,
) -> BriefPromotionGate:
    """Run rung-1 resolution over *brief*'s ``evidence_refs`` for promotion.

    Walks :attr:`IntentBrief.evidence_refs` and routes each entry
    through :func:`eawf.workflow.evidence.resolve.resolve` under the
    ``"deterministic"`` criterion-evidence flavor (see
    ``_BRIEF_REF_EVIDENCE_KIND``). A brief is promotable **iff** every
    ref resolves (``ResolveStatus.RESOLVED``); a single ``UNRESOLVED``
    (or ``DEFERRED``) ref blocks promotion and contributes a line to
    :attr:`BriefPromotionGate.reasons`.

    This is the call the
    :attr:`eawf.kernel.spec.intent.IntentBrief.evidence_refs` field's
    docstring promised but no code ran until this wave: the EviBound
    gate fails the brief at promotion time, not at ingestion time.

    Args:
        brief: The typed intent brief whose claims are being gated.
        project_root: Absolute path the rung-1 disk-exists check
            resolves repo-relative refs against (threaded into
            :func:`resolve`).

    Returns:
        A :class:`BriefPromotionGate`. An empty ``evidence_refs`` list
        returns ``promotable=True`` with no outcomes (an unsourced
        brief carries no claim to gate).
    """
    outcomes: list[BriefRefOutcome] = []
    reasons: list[str] = []
    for ref in brief.evidence_refs:
        result = resolve(ref, _BRIEF_REF_EVIDENCE_KIND, project_root=project_root)
        passed = result.status is ResolveStatus.RESOLVED
        outcomes.append(BriefRefOutcome(ref=ref, result=result, passed=passed))
        if not passed:
            detail = result.reason or result.status.value
            reasons.append(f"evidence ref {ref!r} failed rung-1: {detail}")
    promotable = not reasons
    logger.debug(
        f"check_brief_promotable refs={len(outcomes)} promotable={promotable} failed={len(reasons)}"
    )
    return BriefPromotionGate(
        promotable=promotable,
        outcomes=tuple(outcomes),
        reasons=tuple(reasons),
    )


__all__ = [
    "BriefPromotionGate",
    "BriefRefOutcome",
    "check_brief_promotable",
    "run_rung1_gate",
]
