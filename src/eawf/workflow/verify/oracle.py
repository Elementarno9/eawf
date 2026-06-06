"""Ordered-escalation verification runner (FS05).

The :func:`run_oracle` runner is the keystone of the enforcement layer:
it scores one :class:`~eawf.kernel.spec.common.CriterionSpec` by trying
its gates in ascending oracle-tier order, so the cheapest deterministic
falsifier is always consulted before any jury spawn. The escalation
invariant -- deterministic tiers MUST be exhausted before T7 -- is the
whole point: a jury call is expensive (three cross-vendor spawns) and
non-deterministic, so it is the last resort, never the first.

The runner is a thin orchestrator over three pre-existing seams:

* :func:`eawf.workflow.verify.compile.compile_gate` turns a typed gate +
  criterion into a runnable :class:`~eawf.workflow.audit_dsl.models.CheckSpec`
  (only ``evidence_kind == "deterministic"`` compiles; else ``None``).
* :func:`eawf.workflow.audit_dsl.runner.run_checks` executes a compiled
  spec against the checkout.
* the jury tier consults either the async cross-vendor jury
  (:func:`eawf.observability.eval.cross_vendor_jury.convene_cross_vendor_jury`)
  when the wave's :func:`eawf.workflow.dispatch.verdict.verdict_requirement`
  is ``"always"``, or the sync single-auditor gate
  (:func:`eawf.workflow.dispatch.verdict.verify_wave_verdict_gate`)
  otherwise.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from eawf.kernel.spec.common import (
    CriterionSpec,
    GateSpec,
    OracleTier,
    _StrictModel,
    _tier_for_gate_kind,
)
from eawf.kernel.state.models import IdStr, State, Wave
from eawf.observability.eval.cross_vendor_jury import (
    SpawnFactory,
    convene_cross_vendor_jury,
)
from eawf.observability.eval.jury import JuryAggregateOutcome
from eawf.workflow.audit_dsl.runner import run_checks
from eawf.workflow.dispatch.verdict import (
    verdict_requirement,
    verify_wave_verdict_gate,
)
from eawf.workflow.verify.compile import compile_gate

logger = logging.getLogger(__name__)


class OracleResult(_StrictModel):
    """Outcome of scoring one criterion through :func:`run_oracle`.

    ``tier`` records which oracle tier produced the verdict so an audit
    can confirm the cheapest falsifier ran first. ``status`` is the
    closed outcome word; ``needs_user`` is reserved for an unresolvable
    jury split that routes to the operator-pause surface. ``gate_id`` is
    set only when a deterministic gate produced the result (it is
    ``None`` for the jury / single-auditor fallthrough, which scores the
    whole wave rather than one gate).
    """

    tier: OracleTier
    status: Literal["pass", "fail", "blocked", "needs_user"]
    criterion_id: IdStr
    gate_id: IdStr | None = None
    detail: Annotated[str, Field(max_length=2000)] = ""


def _gate_sort_key(gate: GateSpec) -> int:
    """Return the ascending sort key for *gate* by its oracle tier.

    A gate whose ``kind`` is not in the gate-kind map sorts last (a
    sentinel one past the highest real tier) so an unknown kind never
    crashes the sort and never jumps ahead of a known deterministic
    gate.
    """
    try:
        return int(_tier_for_gate_kind(gate.kind))
    except ValueError:
        return int(OracleTier.T7_JURY) + 1


async def run_oracle(
    criterion: CriterionSpec,
    gates: list[GateSpec],
    *,
    wave: Wave,
    state: State,
    state_path: Path,
    events_path: Path,
    repo_root: Path,
    spawn_factory: SpawnFactory,
) -> OracleResult:
    """Score *criterion* by escalating its gates from cheapest tier upward.

    The algorithm enforces the escalation invariant -- every
    deterministic tier is tried before any jury spawn:

    1. Sort *gates* ascending by their oracle tier
       (:func:`eawf.kernel.spec.common._tier_for_gate_kind`); an unknown
       kind sorts last so it never crashes the sort or jumps a known
       deterministic gate.
    2. For each gate, when ``criterion.evidence_kind == "deterministic"``,
       compile it (:func:`compile_gate`) and run it
       (:func:`run_checks`). The FIRST gate that yields ``status == "pass"``
       short-circuits and returns at that gate's tier. A gate that raises
       is caught and recorded as a ``blocked`` skip (it never aborts the
       escalation).
    3. When no deterministic gate passed (or none exist), consult the
       jury tier: the async cross-vendor jury when
       :func:`verdict_requirement` is ``"always"``, else the sync
       single-auditor :func:`verify_wave_verdict_gate`.

    Args:
        criterion: The criterion being scored. Read-only.
        gates: The gate rows attached to *criterion*. May be empty (the
            zero-gate criterion routes straight to the jury tier).
        wave: The wave under verification. Read-only here; forwarded to
            the jury tier.
        state: Loaded, validated state forwarded to the cross-vendor
            jury (mutated in place by the convener as each juror
            registers its session).
        state_path: Path to ``state.json``; the verdict stores resolve
            under its sibling ``store/`` directory.
        events_path: Path to ``event.jsonl`` for per-juror session-start
            events.
        repo_root: Repository root the deterministic checks run against
            and the jury's diff base derives from.
        spawn_factory: Per-runtime spawn factory forwarded to the
            cross-vendor jury.

    Returns:
        An :class:`OracleResult` carrying the tier that produced the
        verdict, the closed status, and the producing gate id when a
        deterministic gate scored it.
    """
    ordered = sorted(gates, key=_gate_sort_key)
    logger.debug(
        f"run_oracle criterion={criterion.id!r} gates={len(ordered)} "
        f"evidence_kind={criterion.evidence_kind!r}"
    )

    if criterion.evidence_kind == "deterministic":
        for gate in ordered:
            tier = _gate_sort_key(gate)
            try:
                spec = compile_gate(gate, criterion=criterion)
                if spec is None:
                    continue
                result = run_checks([spec], cwd=repo_root)[0]
            except Exception as exc:
                logger.warning(
                    f"run_oracle gate_blocked criterion={criterion.id!r} gate={gate.id!r} "
                    f"detail={exc!s}"
                )
                continue
            if result.status == "pass":
                logger.info(
                    f"run_oracle pass criterion={criterion.id!r} gate={gate.id!r} tier={tier}"
                )
                return OracleResult(
                    tier=OracleTier(tier),
                    status="pass",
                    criterion_id=criterion.id,
                    gate_id=gate.id,
                    detail=result.details or "",
                )

    requirement = verdict_requirement(wave)
    if requirement == "always":
        jr = await convene_cross_vendor_jury(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn_factory=spawn_factory,
            repo_root=repo_root,
        )
        status = _jury_outcome_status(jr.outcome)
        logger.info(
            f"run_oracle jury criterion={criterion.id!r} wave={wave.id} "
            f"outcome={jr.outcome.value} status={status}"
        )
        return OracleResult(
            tier=OracleTier.T7_JURY,
            status=status,
            criterion_id=criterion.id,
            detail=f"cross-vendor jury outcome={jr.outcome.value}",
        )

    verdict_gate = verify_wave_verdict_gate(wave, state_path=state_path)
    status = "pass" if verdict_gate.passed else "fail"
    logger.info(
        f"run_oracle single_auditor criterion={criterion.id!r} wave={wave.id} "
        f"requirement={requirement} passed={verdict_gate.passed}"
    )
    return OracleResult(
        tier=OracleTier.T7_JURY,
        status=status,
        criterion_id=criterion.id,
        detail=(
            f"single-auditor gate requirement={verdict_gate.requirement} "
            f"passed={verdict_gate.passed}"
        ),
    )


def _jury_outcome_status(
    outcome: JuryAggregateOutcome,
) -> Literal["pass", "fail", "needs_user"]:
    """Map a reduced jury outcome onto an :class:`OracleResult` status."""
    if outcome is JuryAggregateOutcome.PASS:
        return "pass"
    if outcome is JuryAggregateOutcome.FAIL:
        return "fail"
    return "needs_user"


__all__ = ["OracleResult", "run_oracle"]
