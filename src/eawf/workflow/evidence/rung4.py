"""EviBound rung-4 attested render + the closed criterion-verdict vocabulary.

This module is the FINAL rung of the EviBound evidence chain and the home
of the closed verdict StrEnum the whole chain reports against. It sits
above the W07 reference resolver
(:func:`eawf.workflow.evidence.resolve.resolve`), the W08 rung-1
deterministic gate (:func:`eawf.workflow.evidence.evibound.run_rung1_gate`),
and the W09 rung-2 in-process NLI scorer
(:func:`eawf.workflow.evidence.rung2.run_rung2_gate`). The rung-3 spawned
jury is a later iter; rung-4 here is the attested floor.

Rung ladder, by assurance (high -> low)
---------------------------------------
The four EviBound rungs form an assurance ladder. Rung-1 is the highest
assurance (an automated bit nobody can fudge); rung-4 is the LOWEST (a
sign-off with no automated check behind it):

* rung-1 deterministic gate  -> :attr:`CriterionVerdict.CERTIFIED`
* rung-2 / rung-3 entailment  -> :attr:`CriterionVerdict.SUPPORTED`
  (passing) or :attr:`CriterionVerdict.REFUTED` (confident contradiction)
  or :attr:`CriterionVerdict.UNRESOLVED` (the uncertain band that never
  silently certifies)
* rung-4 attested             -> :attr:`CriterionVerdict.ATTESTED`

Rung-4 is RENDER-ONLY — the landmine
------------------------------------
:func:`render_attested_verdict` runs **no automated check**: no
subprocess, no git diff-base, no reference resolution, no NLI score. An
``attested`` criterion's evidence is an operator / agent sign-off, so the
"verification" is the act of recording who attested and why. The function
therefore RENDERS the attestation into an
:class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` rather than
gating against anything. That is exactly why ``attested`` is the lowest
assurance rung: nothing was checked, so the verdict is only as good as
the attester. Routing this flavor through a gate runner (as rung-1 does)
would be a category error — there is no command whose exit code answers
an attestation question. The W07/W08 routing landmine applies in reverse
here: an ``attested`` :data:`~eawf.kernel.spec.common.CriterionEvidenceKind`
must NOT be dispatched to the deterministic gate; it terminates at this
render.

The closed verdict vocabulary + refute-first ordering
------------------------------------------------------
:class:`CriterionVerdict` is the closed StrEnum every EviBound rung
reports a criterion against. The members are declared high-to-low by
assurance, but the load-bearing semantic is the *refute-first* combine
ordering used when one criterion accrues evidence from several rungs (or
several refs at one rung): see :func:`dominant_verdict` and
:data:`_REFUTE_FIRST_PRECEDENCE`. A confident ``REFUTED`` and an
``UNRESOLVED`` both DOMINATE the merely-uncertain / lower-assurance cases
so an unsure or contradicted criterion is never reported as certified on
the strength of a co-resident pass. The burden of proof is on the
positive verdict; the default for a mixed bag is *do not certify*.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.store.kinds.evidence import (
    EvidenceRecord,
    EvidenceStatus,
    ProducedBy,
    mint_evidence_id,
)

logger = logging.getLogger(__name__)


class CriterionVerdict(StrEnum):
    """The closed verdict an EviBound rung reports a criterion against.

    Exactly five members, declared high-to-low by assurance. The string
    values are the wire form a verdict serialises to. The combine
    precedence (which verdict wins when a criterion carries several) is
    NOT this declaration order — it is the refute-first order in
    :data:`_REFUTE_FIRST_PRECEDENCE`, surfaced via
    :func:`dominant_verdict`.

    Members:
        CERTIFIED: The rung-1 deterministic gate passed — the highest
            assurance verdict (an automated bit: a command exited 0, a
            schema validated, a regex matched). Certification is the only
            verdict backed by a check nobody can fudge.
        SUPPORTED: A rung-2 in-process NLI score (or the later rung-3
            jury) found the resolved evidence entails the claim. Weaker
            than ``CERTIFIED`` because entailment is a graded judgement,
            not a deterministic bit, but still a positive verdict.
        REFUTED: A rung confidently contradicted the claim (a rung-2
            entailment probability at/below the refute floor, or a jury
            refutation). Under refute-first this DOMINATES the uncertain
            and lower-assurance verdicts.
        UNRESOLVED: No rung reached a positive or refuting verdict — the
            rung-2 uncertain band that escalates, an unfilled rung-3
            jury, or an unresolved reference. Under refute-first this
            also DOMINATES the merely-attested / lower-assurance cases:
            an unresolved criterion is not certified on the strength of a
            co-resident attestation.
        ATTESTED: A rung-4 operator / agent sign-off with NO automated
            check behind it — the LOWEST assurance verdict. Recorded by
            :func:`render_attested_verdict`, which renders the
            attestation rather than gating against anything.
    """

    CERTIFIED = "certified"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    UNRESOLVED = "unresolved"
    ATTESTED = "attested"


#: Refute-first combine precedence: lower rank wins when a criterion
#: accrues several verdicts. ``REFUTED`` and ``UNRESOLVED`` outrank the
#: positive ``CERTIFIED`` / ``SUPPORTED`` and the lowest-assurance
#: ``ATTESTED`` so a contradicted or uncertain criterion is never
#: reported as certified on the strength of a co-resident pass. This is
#: deliberately NOT the assurance order of the enum declaration: the
#: positive verdicts sort by assurance among themselves, but a negative
#: verdict pre-empts them all.
#:
#:   REFUTED (0) < UNRESOLVED (1) < CERTIFIED (2) < SUPPORTED (3) < ATTESTED (4)
_REFUTE_FIRST_PRECEDENCE: Final[dict[CriterionVerdict, int]] = {
    CriterionVerdict.REFUTED: 0,
    CriterionVerdict.UNRESOLVED: 1,
    CriterionVerdict.CERTIFIED: 2,
    CriterionVerdict.SUPPORTED: 3,
    CriterionVerdict.ATTESTED: 4,
}

#: Map each closed verdict onto the persisted binary
#: :class:`~eawf.kernel.store.kinds.evidence.EvidenceStatus`. ``CERTIFIED``
#: / ``SUPPORTED`` are passing; ``REFUTED`` is a failing gate;
#: ``UNRESOLVED`` is ``"blocked"`` (a later rung or a human owns it);
#: ``ATTESTED`` persists as ``"pass"`` because a recorded sign-off is the
#: attested floor's affirmative outcome (its low assurance lives in the
#: verdict + ``evidence_kind``, not in the binary status).
_VERDICT_STATUS: Final[dict[CriterionVerdict, EvidenceStatus]] = {
    CriterionVerdict.CERTIFIED: "pass",
    CriterionVerdict.SUPPORTED: "pass",
    CriterionVerdict.REFUTED: "fail",
    CriterionVerdict.UNRESOLVED: "blocked",
    CriterionVerdict.ATTESTED: "pass",
}


def verdict_to_status(verdict: CriterionVerdict) -> EvidenceStatus:
    """Map a closed :class:`CriterionVerdict` onto the persisted binary status.

    The verdict vocabulary is richer than the binary
    :class:`~eawf.kernel.store.kinds.evidence.EvidenceStatus` the store
    persists, so a row written for any rung collapses its verdict to a
    status here. ``CERTIFIED`` / ``SUPPORTED`` / ``ATTESTED`` -> ``"pass"``
    (each is an affirmative outcome at its own assurance level);
    ``REFUTED`` -> ``"fail"``; ``UNRESOLVED`` -> ``"blocked"``.

    Args:
        verdict: The closed criterion verdict to persist.

    Returns:
        The :class:`~eawf.kernel.store.kinds.evidence.EvidenceStatus` to
        stamp on the row.
    """
    return _VERDICT_STATUS[verdict]


def dominant_verdict(verdicts: list[CriterionVerdict]) -> CriterionVerdict:
    """Return the refute-first dominant verdict among *verdicts*.

    When a criterion accrues several verdicts — from several EviBound
    rungs, or several evidence refs at one rung — this reduces them to the
    single verdict the criterion is reported under. The reduction is
    refute-first (:data:`_REFUTE_FIRST_PRECEDENCE`): a confident
    ``REFUTED`` wins over everything, then ``UNRESOLVED`` over the
    positive / attested verdicts, so an uncertain or contradicted
    criterion is never reported as certified just because one co-resident
    rung passed. Among the non-dominating verdicts the higher-assurance
    one wins (``CERTIFIED`` over ``SUPPORTED`` over ``ATTESTED``).

    Args:
        verdicts: One or more verdicts the criterion accrued, in any
            order.

    Returns:
        The single refute-first dominant verdict.

    Raises:
        ValueError: When *verdicts* is empty — there is no verdict to
            report and an empty reduction has no defined answer.
    """
    if not verdicts:
        raise ValueError("cannot reduce an empty verdict list")
    dominant = min(verdicts, key=lambda verdict: _REFUTE_FIRST_PRECEDENCE[verdict])
    inputs = ",".join(verdict.value for verdict in verdicts)
    logger.debug(f"dominant_verdict inputs={inputs} dominant={dominant.value}")
    return dominant


def render_attested_verdict(
    criterion: CriterionSpec,
    *,
    scope_id: str,
    attested_by: ProducedBy = "human",
    note: str | None = None,
) -> EvidenceRecord:
    """RENDER an attested (rung-4) evidence row for *criterion* — no check runs.

    Rung-4 is the LOWEST assurance rung: an ``attested`` criterion's
    evidence is an operator / agent sign-off, not an automated result. So
    this function runs NO gate — no subprocess, no diff-base, no reference
    resolution, no NLI score. It RENDERS the attestation into an
    :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` whose
    verdict is :attr:`CriterionVerdict.ATTESTED` and whose ``status`` is
    ``"pass"`` (the attested floor's affirmative outcome; its low
    assurance lives in the verdict + ``evidence_kind``, not the status).

    This is the rung-4 analogue of
    :func:`eawf.workflow.evidence.evibound.run_rung1_gate` and
    :func:`eawf.workflow.evidence.rung2.run_rung2_gate`, but it
    intentionally takes no runner / scorer: there is nothing to run. An
    ``attested`` :data:`~eawf.kernel.spec.common.CriterionEvidenceKind`
    terminates here rather than being dispatched to the deterministic
    gate.

    Args:
        criterion: The criterion being attested. Its ``evidence_kind``
            MUST be ``"attested"`` — rendering an attestation for a
            ``deterministic`` / ``jury`` criterion would mask a missing
            automated check, so it fails fast.
        scope_id: URN of the scope (wave / iter / phase / decision) the
            attestation backs — recorded on the row so close-readiness
            can filter by scope.
        attested_by: Who signed off — ``"human"`` for an operator (the
            default) or ``"agent"`` for a subagent attestation. A
            deterministic checker (``"tool"``) or synthetic seed
            (``"canary"``) cannot attest, because an attestation is a
            judgement, not a measured result.
        note: Optional operator-supplied rationale folded into the row
            summary. The summary names the attester and that NO automated
            check ran regardless.

    Returns:
        An :class:`EvidenceRecord` with ``evidence_kind="attested"``,
        ``status="pass"``, and a summary marking the row as a render-only
        attestation.

    Raises:
        ValueError: When *criterion* is not an ``attested`` criterion, or
            when *attested_by* is not an attesting producer
            (``"human"`` / ``"agent"``).
    """
    if criterion.evidence_kind != "attested":
        raise ValueError(
            f"render_attested_verdict requires an attested criterion, "
            f"got {criterion.evidence_kind!r} for {criterion.id!r}"
        )
    if attested_by not in ("human", "agent"):
        raise ValueError(f"attested_by must be 'human' or 'agent', got {attested_by!r}")

    rationale = f": {note}" if note else ""
    summary = (
        f"rung-4 attested verdict for criterion {criterion.id!r} "
        f"by {attested_by} (render-only, no automated check){rationale}"
    )
    record = EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=scope_id,
        produced_by=attested_by,
        evidence_kind="attested",
        status=verdict_to_status(CriterionVerdict.ATTESTED),
        summary=summary[:500],
        refs=[criterion.id],
        created_at=datetime.now(UTC),
    )
    logger.debug(
        f"render_attested_verdict criterion={criterion.id!r} by={attested_by!r} "
        f"verdict={CriterionVerdict.ATTESTED.value}"
    )
    return record


__all__ = [
    "CriterionVerdict",
    "dominant_verdict",
    "render_attested_verdict",
    "verdict_to_status",
]
