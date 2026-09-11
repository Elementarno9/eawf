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
* :func:`eawf.workflow.verify.sandboxed_checks.run_checks_out_of_process`
  (advisory scoring) and
  :func:`eawf.runtime.daemon.gate_execution.run_gate_out_of_process`
  (durable scoring) execute a compiled spec against the checkout from a
  child interpreter pinned to a throwaway runtime directory and ledger.
  Deterministic gates are routinely whole test suites, and a suite that
  exercises eawf's own RPCs drives whichever runtime pair its process
  points at; scoring in the calling process would make that the LIVE
  pair, so no branch here executes a check itself.
* the jury tier consults either the async cross-vendor jury
  (:func:`eawf.observability.eval.cross_vendor_jury.convene_cross_vendor_jury`)
  when the wave's :func:`eawf.workflow.dispatch.verdict.verdict_requirement`
  is ``"always"``, or the sync single-auditor gate
  (:func:`eawf.workflow.dispatch.verdict.verify_wave_verdict_gate`)
  otherwise.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator

from eawf.kernel.spec.common import (
    CriterionSpec,
    GateSpec,
    OracleTier,
    _StrictModel,
    _tier_for_gate_kind,
)
from eawf.kernel.spec.falsifiability import unfalsifiable_argv
from eawf.kernel.state.enums import RiskTier
from eawf.kernel.state.models import IdStr, State, Wave
from eawf.observability.eval.cross_vendor_jury import (
    SpawnFactory,
    convene_cross_vendor_jury,
)
from eawf.observability.eval.jury import JuryAggregateOutcome
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec, GateFreshnessInput
from eawf.workflow.dispatch.verdict import (
    verdict_requirement,
    verify_wave_verdict_gate,
)
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.verify.compile import compile_gate
from eawf.workflow.verify.gate_receipts import (
    append_gate_execution_receipt,
    gate_execution_receipt,
)
from eawf.workflow.verify.sandboxed_checks import run_checks_out_of_process
from eawf.workflow.verify.vacuous_gate import vacuous_gate_run

logger = logging.getLogger(__name__)

BeforeGateExecute = Callable[
    [str, str, CheckSpec, str],
    CheckResult | None,
]
AfterGateExecute = Callable[[str, str, CheckResult], None]

#: Character bound on :attr:`OracleResult.detail`. The detail rides into
#: the close-refusal message and into the state-resident evidence row, so
#: it is a summary field with a deliberate bound.
DETAIL_MAX_CHARS: int = 2000

#: Suffix marking a detail that was clamped to :data:`DETAIL_MAX_CHARS`,
#: so a reader can tell a short detail from the head slice of a long one
#: and knows the untruncated output is still on ``check_result``.
DETAIL_TRUNCATION_MARKER: str = "... (truncated; full output on check_result)"


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
    detail: Annotated[str, Field(max_length=DETAIL_MAX_CHARS)] = ""
    check_result: CheckResult | None = None

    @field_validator("detail", mode="before")
    @classmethod
    def _clamp_detail(cls, value: object) -> object:
        """Clamp an over-long detail rather than refusing to build the result.

        A gate's own output is unbounded: a failing pytest run dumps kilobytes,
        and a golden mismatch inlines a unified diff of a full-width screen.
        Letting that overflow the field turns a gate the oracle SCORED into a
        ``ValidationError`` raised out of the scorer, which crashes the close
        instead of blocking it -- the loudest possible way to lose a verdict
        that had already been reached. The head slice is kept because the
        load-bearing part of every detail (the summary sentence, the diff
        region marker, the first changed line) sits at the front, and the
        untruncated output stays on ``check_result``.

        Args:
            value: The raw ``detail`` input, of any type.

        Returns:
            *value* unchanged when it is not an over-long string, else its
            head slice with :data:`DETAIL_TRUNCATION_MARKER` appended. A
            non-``str`` passes through so the field's own type error is the
            one the caller sees.
        """
        if not isinstance(value, str) or len(value) <= DETAIL_MAX_CHARS:
            return value
        head = DETAIL_MAX_CHARS - len(DETAIL_TRUNCATION_MARKER)
        return value[:head] + DETAIL_TRUNCATION_MARKER

    def failing_detail(self) -> str:
        """Return the concrete failing-check output for a refused criterion.

        The repair re-dispatch must be GROUNDED in the actual falsifier output,
        never a content-free "drifted, redo" hint. This accessor surfaces the
        :attr:`detail` (the deterministic check output or the jury detail) the
        oracle recorded when it refused, so a repair builder is fed the same
        concrete payload the close gate refused on. When the oracle scored a
        non-pass without any detail, a typed fallback string is returned so the
        payload is never empty -- the repair builder's non-empty guard then
        always has something to ground on.

        Returns:
            The recorded :attr:`detail`, or a typed fallback naming the tier +
            status when the oracle refused without a detail string.

        Raises:
            ValueError: when called on a passing result (a pass has no failing
                check to ground a repair on).
        """
        if self.status == "pass":
            raise ValueError(
                f"oracle result for criterion {self.criterion_id!r} passed: no failing detail"
            )
        if self.detail:
            return self.detail
        return f"criterion {self.criterion_id!r} scored {self.status} at tier {int(self.tier)}"


def _check_result_status(result: CheckResult) -> Literal["pass", "fail", "blocked"]:
    """Return the closed status for one deterministic check result."""
    if result.status is not None:
        return result.status
    return "pass" if result.passed else "fail"


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


def _reused_pass_result(
    criterion: CriterionSpec,
    gate: GateSpec,
    *,
    tier: int,
    reusable_pass_gate_ids: set[str] | None,
) -> OracleResult | None:
    """Return a passing oracle result when a complete receipt is reusable."""
    if gate.id not in (reusable_pass_gate_ids or set()):
        return None
    logger.info(
        f"run_oracle status=receipt-reuse criterion={criterion.id!r} gate={gate.id!r} tier={tier}"
    )
    return OracleResult(
        tier=OracleTier(tier),
        status="pass",
        criterion_id=criterion.id,
        gate_id=gate.id,
        detail="reused freshness-matched gate receipt",
    )


def _gate_argv(gate: GateSpec) -> list[str]:
    """Return the gate's argv vector, empty for a gate kind that runs none.

    Args:
        gate: The gate row whose ``args`` may carry an ``argv`` vector.

    Returns:
        The argv tokens as strings; empty when the kind carries no argv.
    """
    argv = gate.args.get("argv")
    if not isinstance(argv, list):
        return []
    return [str(token) for token in argv]


def _record_gate_execution(
    state_path: Path,
    *,
    scope_id: str,
    criterion_id: str,
    gate_id: str,
    result: CheckResult | None,
    status: Literal["pass", "fail", "blocked"],
    argv: list[str] | None = None,
) -> None:
    """Persist one durable receipt for a gate this close just executed.

    A receipt is best-effort with respect to the close: a store that
    refuses the append must not convert a scored gate into a crashed
    close, so the failure is logged and the scoring continues.

    Args:
        state_path: Path to ``state.json``; anchors the evidence store.
        scope_id: Wave URN the gate scored.
        criterion_id: Criterion the gate was attached to.
        gate_id: Id of the gate that ran.
        result: The runner's result, or ``None`` when the gate died
            before producing one.
        status: The closed outcome word recorded on the receipt.
        argv: Fallback argv, used when *result* is ``None``.
    """
    try:
        append_gate_execution_receipt(
            state_path,
            gate_execution_receipt(
                scope_id=scope_id,
                criterion_id=criterion_id,
                gate_id=gate_id,
                argv=(result.argv if result is not None else argv),
                exit_status=(result.exit_status if result is not None else None),
                status=status,
                executed_at=(result.ended_at if result is not None else None),
            ),
        )
    except Exception as exc:
        logger.warning(
            f"_record_gate_execution status=skip scope_id={scope_id!r} "
            f"gate={gate_id!r} detail={exc!s}"
        )


def _score_gate_run(
    criterion: CriterionSpec,
    gate: GateSpec,
    *,
    result: CheckResult,
    tier: int,
    scope_id: str,
    state_path: Path,
    after_gate_execute: AfterGateExecute | None,
) -> OracleResult | None:
    """Turn one completed gate run into a decisive result, or into nothing.

    Receipts the run, then classifies it. ``None`` means the run was not
    decisive for the criterion and the caller moves to the next gate: either a
    non-blocking verdict, which the close may ignore, or a run that observed
    nothing, which there is no honest way to score.

    A run can exit non-zero having asserted nothing at all -- a pytest selector
    that matched no test, a named path that does not resolve. Reading that as
    the criterion failing accuses the wave's work of a defect the gate never
    looked for, so it is refused the way an argv that cannot red is refused, and
    its receipt records ``blocked`` rather than claiming a tree observation.

    Args:
        criterion: The criterion the gate is attached to.
        gate: The gate that ran.
        result: The runner's terminal result.
        tier: The gate's oracle tier.
        scope_id: Wave URN the receipt scopes to.
        state_path: Path to ``state.json``; anchors the receipt store.
        after_gate_execute: The durable receipt-persisting hook, or ``None``.

    Returns:
        The decisive :class:`OracleResult`, or ``None`` when this gate settles
        nothing for the criterion.
    """
    if after_gate_execute is not None and result.started_at is not None:
        after_gate_execute(criterion.id, gate.id, result)
    blocking = gate.required and gate.policy == "block"
    vacuous = vacuous_gate_run(
        result.argv if result.argv is not None else _gate_argv(gate),
        exit_status=result.exit_status,
    )
    gate_status: Literal["pass", "fail", "blocked"] = (
        "blocked" if vacuous is not None else _check_result_status(result)
    )
    _record_gate_execution(
        state_path,
        scope_id=scope_id,
        criterion_id=criterion.id,
        gate_id=gate.id,
        result=result,
        status=gate_status,
    )
    if vacuous is not None:
        logger.warning(
            f"run_oracle status=gate-vacuous criterion={criterion.id!r} gate={gate.id!r} "
            f"blocking={blocking} exit_status={result.exit_status} detail={vacuous.reason!r}"
        )
        if not blocking:
            return None
        return OracleResult(
            tier=OracleTier(tier),
            status="blocked",
            criterion_id=criterion.id,
            gate_id=gate.id,
            detail=f"gate argv observed nothing: {vacuous.message()}",
            check_result=result,
        )
    if gate_status != "pass" and not blocking:
        logger.debug(
            f"run_oracle mode=deterministic criterion={criterion.id!r} gate={gate.id!r} "
            f"tier={tier} status={gate_status} blocking=False"
        )
        return None
    logger.info(
        f"run_oracle mode=deterministic criterion={criterion.id!r} gate={gate.id!r} "
        f"tier={tier} status={gate_status} blocking={blocking}"
    )
    return OracleResult(
        tier=OracleTier(tier),
        status=gate_status,
        criterion_id=criterion.id,
        gate_id=gate.id,
        detail=result.details or "",
        check_result=result,
    )


async def _run_deterministic_gates(  # noqa: C901
    criterion: CriterionSpec,
    ordered: list[GateSpec],
    *,
    scope_id: str,
    repo_root: Path,
    state_path: Path,
    freshness_by_gate: dict[str, GateFreshnessInput] | None,
    reusable_pass_gate_ids: set[str] | None,
    before_gate_execute: BeforeGateExecute | None,
    after_gate_execute: AfterGateExecute | None,
    require_all_deterministic: bool,
    close_attempt_id: str,
) -> OracleResult | None:
    """Run ordered deterministic gates and return the first decisive result."""
    from eawf.runtime.daemon.gate_execution import (
        GateChildCrashError,
        GateExecutionContext,
        current_gate_context,
        run_gate_out_of_process,
    )

    gate_context = current_gate_context()
    durable = before_gate_execute is not None
    if gate_context is None and durable and close_attempt_id:
        # A durable close whose identity arrived as a claim CALLBACK rather
        # than as bound context still has to execute out of process. The
        # callback cannot be pickled into a child, but the identity behind it
        # -- the live ledger path plus the attempt id -- is plain data, so it
        # is rebuilt here and handed down as the child's claim descriptor. The
        # child then re-runs the identical claim against the identical live
        # ledger, which is why the callback itself is NOT invoked in this
        # process: a parent-side claim would be seen by the child as an
        # already-claimed key and block every gate as indeterminate.
        gate_context = GateExecutionContext(
            state_path=state_path,
            attempt_id=close_attempt_id,
        )
    last_pass: OracleResult | None = None
    for gate in ordered:
        tier = _gate_sort_key(gate)
        unfalsifiable = unfalsifiable_argv(_gate_argv(gate))
        if unfalsifiable is not None:
            # The gate names a command whose exit status cannot depend on the
            # tree, so running it would record a pass that proves nothing. A
            # required blocking gate refuses the close outright; a non-blocking
            # one is dropped, because a verdict it cannot fail is not evidence.
            if gate.required and gate.policy == "block":
                logger.warning(
                    f"run_oracle status=gate-unfalsifiable criterion={criterion.id!r} "
                    f"gate={gate.id!r} detail={unfalsifiable.reason!r}"
                )
                return OracleResult(
                    tier=OracleTier(tier),
                    status="blocked",
                    criterion_id=criterion.id,
                    gate_id=gate.id,
                    detail=f"gate argv cannot fail: {unfalsifiable.message()}",
                )
            logger.warning(
                f"run_oracle status=gate-unfalsifiable-skipped criterion={criterion.id!r} "
                f"gate={gate.id!r} detail={unfalsifiable.reason!r}"
            )
            continue
        if not durable:
            reused = _reused_pass_result(
                criterion,
                gate,
                tier=tier,
                reusable_pass_gate_ids=reusable_pass_gate_ids,
            )
            if reused is not None:
                if not require_all_deterministic:
                    return reused
                last_pass = reused
                continue
        try:
            gate_freshness = (freshness_by_gate or {}).get(gate.id)
            if gate_freshness is None:
                spec = compile_gate(gate, criterion=criterion)
            else:
                spec = compile_gate(
                    gate,
                    criterion=criterion,
                    freshness=gate_freshness,
                )
            if spec is None:
                continue
            if gate_context is not None:
                # A durable close runs its heavy gates in a child interpreter.
                # A check kind that hard-exits would otherwise kill the daemon
                # and every other in-flight close with it; the child also owns
                # its own claim, so a crash leaves an orphaned claim rather
                # than a silently rerunnable gate.
                result = await asyncio.to_thread(
                    run_gate_out_of_process,
                    spec,
                    cwd=repo_root,
                    context=gate_context,
                    criterion_id=criterion.id,
                    gate_id=gate.id,
                )
            elif not durable:
                # Advisory scoring claims nothing, so the cheaper batch runner
                # is enough -- but it still runs in a sandboxed child, because
                # the gate itself is the hazard, not the bookkeeping around it.
                results = await asyncio.to_thread(
                    run_checks_out_of_process,
                    [spec],
                    cwd=repo_root,
                    live_state_path=state_path,
                )
                result = results[0]
            else:
                # Fail closed. A durable close that names no attempt cannot
                # claim its freshness key in the child, and running it here
                # instead would point a gate suite at the live runtime pair --
                # the one thing this branch must never do.
                raise ValueError(
                    "durable gate execution has no claimable attempt identity: "
                    f"gate={gate.id!r} criterion={criterion.id!r}"
                )
        except GateChildCrashError:
            # A crashed runner proved nothing about the wave, so it must not be
            # recorded as a gate verdict; it surfaces as a harness fault that
            # re-queues the attempt for resume.
            raise
        except Exception as exc:
            logger.warning(
                f"run_oracle status=gate-blocked criterion={criterion.id!r} "
                f"gate={gate.id!r} detail={exc!s}"
            )
            # A gate that died before returning a verdict still ran, and a
            # verification history that silently omits it is the hole this
            # receipt closes: the row records the argv that was attempted and
            # a blocked outcome with no exit status.
            _record_gate_execution(
                state_path,
                scope_id=scope_id,
                criterion_id=criterion.id,
                gate_id=gate.id,
                result=None,
                status="blocked",
                argv=_gate_argv(gate),
            )
            if durable and gate.required and gate.policy == "block":
                return OracleResult(
                    tier=OracleTier(tier),
                    status="blocked",
                    criterion_id=criterion.id,
                    gate_id=gate.id,
                    detail=f"durable gate execution blocked: {exc!s}",
                )
            continue
        scored = _score_gate_run(
            criterion,
            gate,
            result=result,
            tier=tier,
            scope_id=scope_id,
            state_path=state_path,
            after_gate_execute=after_gate_execute,
        )
        if scored is None:
            continue
        if scored.status == "pass" and require_all_deterministic:
            last_pass = scored
            continue
        return scored
    return last_pass


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
    block_authority: BlockAuthority = BlockAuthority.ADVISORY,
    freshness_by_gate: dict[str, GateFreshnessInput] | None = None,
    reusable_pass_gate_ids: set[str] | None = None,
    before_gate_execute: BeforeGateExecute | None = None,
    after_gate_execute: AfterGateExecute | None = None,
    require_all_deterministic: bool = False,
    close_attempt_id: str = "",
) -> OracleResult:
    """Score *criterion* by escalating its gates from cheapest tier upward.

    The algorithm enforces the escalation invariant -- every
    deterministic tier is tried before any jury spawn:

    1. Sort *gates* ascending by their oracle tier
       (:func:`eawf.kernel.spec.common._tier_for_gate_kind`); an unknown
       kind sorts last so it never crashes the sort or jumps a known
       deterministic gate.
    2. For each gate, when ``criterion.evidence_kind == "deterministic"``,
       compile it (:func:`compile_gate`) and run it in a SANDBOXED CHILD --
       :func:`~eawf.runtime.daemon.gate_execution.run_gate_out_of_process`
       under a durable identity, else
       :func:`~eawf.workflow.verify.sandboxed_checks.run_checks_out_of_process`
       -- offloaded to a worker thread via
       :func:`asyncio.to_thread` so the subprocess-bearing gate never starves
       the daemon event loop. The FIRST required/blocking deterministic gate
       that yields ``status in {"fail", "blocked"}`` returns a non-pass
       result at that gate's tier; the first deterministic ``pass`` returns a
       pass at that tier. A gate that raises is caught and recorded as a
       ``blocked`` skip (it never aborts the escalation). Every gate that
       reaches execution leaves one durable receipt
       (:func:`eawf.workflow.verify.gate_receipts.gate_execution_receipt`)
       carrying its argv, exit status and timestamp, and a gate whose argv
       cannot exit non-zero
       (:func:`eawf.kernel.spec.falsifiability.unfalsifiable_argv`) is
       refused before it runs rather than scored as a pass it could not
       have failed.
    3. When no deterministic gate produced a blocking result or pass (or none
       exist), consult the
       jury tier: the async cross-vendor jury when
       :func:`verdict_requirement` is ``"always"``, else the sync
       single-auditor :func:`verify_wave_verdict_gate`. A cross-vendor jury
       veto (``FAIL`` / ``NEEDS_USER``) is held ADVISORY -- logged at WARNING,
       close proceeds as ``"pass"`` -- unless the caller passes a *blocking*
       *block_authority*, in which case the veto raises
       :class:`LifecycleError` and blocks the close. The TRUST-4
       earned-authority computation
       (:func:`eawf.observability.eval.jury_validation.jury_block_authority`)
       runs in the daemon close path and decides which authority to pass in; a
       jury that has not cleared its trust floors stays advisory by default.

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
            under its sibling ``store/`` directory, the deterministic tier
            snapshots it into each gate child's sandbox ledger, and a durable
            close claims its freshness keys against it.
        events_path: Path to ``event.jsonl`` for per-juror session-start
            events.
        repo_root: Repository root the deterministic checks run against
            and the jury's diff base derives from.
        spawn_factory: Per-runtime spawn factory forwarded to the
            cross-vendor jury.
        block_authority: Whether a cross-vendor jury veto may BLOCK the close
            (:attr:`~eawf.observability.eval.jury_validation.BlockAuthority.BLOCKING`,
            the veto raises :class:`LifecycleError`) or is held merely advisory
            (:attr:`~eawf.observability.eval.jury_validation.BlockAuthority.ADVISORY`,
            the default, the veto is logged and the close proceeds). The daemon
            close path computes the earned authority and passes it in; an
            uncalibrated jury stays advisory.
        freshness_by_gate: Optional immutable receipt facts keyed by gate id.
            Only the selected deterministic gate's facts reach the result.
        reusable_pass_gate_ids: Gates whose persisted pass receipts match the
            caller's complete frozen-input identity.
        before_gate_execute: Optional durable pre-execution claim callback.
            Its PRESENCE marks the close durable; the claim itself is made by
            the gate child under the same identity, so the callback is not
            invoked here (claiming in both processes would read as a
            double-claim and block every gate).
        after_gate_execute: Optional terminal-result callback. Called only for
            results with execution timestamps, including receipt reuse.
        require_all_deterministic: Run every required deterministic gate
            instead of returning after the first pass.
        close_attempt_id: Durable close attempt the gate child claims its
            freshness keys under. Required whenever *before_gate_execute* is
            set and no gate context is bound; an empty value there refuses the
            gate rather than falling back to in-process execution.

    Returns:
        An :class:`OracleResult` carrying the tier that produced the
        verdict, the closed status, and the producing gate id when a
        deterministic gate scored it.

    Raises:
        LifecycleError: When the cross-vendor jury vetoes (``FAIL`` /
            ``NEEDS_USER``) AND *block_authority* is
            :attr:`~eawf.observability.eval.jury_validation.BlockAuthority.BLOCKING`
            -- the calibrated jury blocks the close.
    """
    ordered = sorted(gates, key=_gate_sort_key)
    logger.debug(
        f"run_oracle criterion={criterion.id!r} gates={len(ordered)} "
        f"evidence_kind={criterion.evidence_kind!r}"
    )

    if criterion.evidence_kind == "deterministic":
        deterministic = await _run_deterministic_gates(
            criterion,
            ordered,
            scope_id=wave.id,
            repo_root=repo_root,
            state_path=state_path,
            freshness_by_gate=freshness_by_gate,
            reusable_pass_gate_ids=reusable_pass_gate_ids,
            before_gate_execute=before_gate_execute,
            after_gate_execute=after_gate_execute,
            require_all_deterministic=require_all_deterministic,
            close_attempt_id=close_attempt_id,
        )
        if deterministic is not None:
            return deterministic

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
        # The staged advisory-to-block gate (TRUST-4). A non-pass jury outcome
        # -- a substantive veto (FAIL) or an unresolved split / sub-quorum
        # (NEEDS_USER) -- BLOCKS the close only when the caller passes BLOCKING
        # authority: a calibrated jury that has cleared its trust floors raises
        # LifecycleError so the close is refused. Under ADVISORY authority (the
        # default, an uncalibrated / thin / biased jury) the same non-pass is
        # logged at WARNING and the close proceeds as "pass" -- the non-pass
        # signal is preserved in the log so an audit still sees the veto.
        if status != "pass":
            if block_authority is BlockAuthority.BLOCKING:
                logger.warning(
                    f"run_oracle jury_veto_blocking criterion={criterion.id!r} wave={wave.id} "
                    f"outcome={jr.outcome.value} authority=blocking close_blocked=True"
                )
                raise LifecycleError(
                    f"cross-vendor jury vetoed close: criterion={criterion.id!r} "
                    f"wave={wave.id} outcome={jr.outcome.value}"
                )
            logger.warning(
                f"run_oracle jury_veto_advisory criterion={criterion.id!r} wave={wave.id} "
                f"outcome={jr.outcome.value} authority=advisory close_proceeds=True"
            )
            return OracleResult(
                tier=OracleTier.T7_JURY,
                status="pass",
                criterion_id=criterion.id,
                detail=(
                    f"cross-vendor jury outcome={jr.outcome.value} held advisory "
                    "until the jury earns blocking authority"
                ),
            )
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


def jury_block_authority(wave: Wave) -> Literal["advisory", "blocking"]:
    """Return the wave-keyed default jury authority -- always ``"advisory"``.

    The earned-authority decision proper now lives in
    :func:`eawf.observability.eval.jury_validation.jury_block_authority`, which
    scores the jury's validation report + verbosity probe against the trust
    floors and returns
    :class:`~eawf.observability.eval.jury_validation.BlockAuthority`; the daemon
    close path runs that computation and threads the result into
    :func:`run_oracle` as ``block_authority``. This wave-keyed helper is the
    safe default for a call site that has NO validation report to score yet (no
    labelled cohort, an empty trust substrate): with no evidence a jury cannot
    have EARNED blocking, so its veto is held advisory. It is retained as the
    documented default so the absence-of-evidence path is explicit rather than
    implicit.

    Args:
        wave: The wave whose close is being scored. Read-only.

    Returns:
        ``"advisory"`` -- a jury with no validation evidence never earns
        blocking authority.
    """
    return "advisory"


def _jury_outcome_status(
    outcome: JuryAggregateOutcome,
) -> Literal["pass", "fail", "needs_user"]:
    """Map a reduced jury outcome onto an :class:`OracleResult` status."""
    if outcome is JuryAggregateOutcome.PASS:
        return "pass"
    if outcome is JuryAggregateOutcome.FAIL:
        return "fail"
    return "needs_user"


# --- RiskTier classifier (P30-I12-W05 / DL-5) -----------------------------
#
# The fleet auto-drain loop needs to know, BEFORE a lane closes, whether the
# wave it drove can self-close on a deterministic pass or must fork to a human
# / jury. That answer is the wave's :class:`~eawf.kernel.state.enums.RiskTier`,
# classified purely from the wave's gate KINDS:
#
# - every gate is a deterministic falsifier (the ``T1``-``T5`` band) -> MECH,
#   the wave self-closes on a deterministic pass with no human in the loop;
# - the wave carries a UI / visual-band gate (a ``tui_flow`` / ``svg`` /
#   ``mockup`` / affordance / transition-coverage surface, whose ground truth
#   ultimately escalates to the visual jury) -> UI;
# - the wave carries a non-UI jury gate -> HIGH;
# - the wave carries an auditor / human-approval gate (and no jury / UI gate)
#   -> MED.
#
# The classifier is a TOTAL, PURE function of the gate-kind strings: it reads
# no IO, mutates nothing, and an empty gate set classifies MECH (a wave with no
# gates has nothing that needs human judgement, so it is the least-risk band).

#: UI / visual-band gate kinds. A wave carrying any of these gates is a UI-band
#: wave: its ground truth is a visual / interaction surface whose final oracle
#: is the cross-vendor visual jury (per the spec-as-layered-oracle VFL stack),
#: so it is held to the same earned-blocking-authority bar as a jury wave.
_UI_BAND_GATE_KINDS: frozenset[str] = frozenset(
    {
        "tui_flow",
        "svg_well_formed",
        "svg_pixel_diff",
        "mockup_golden_diff",
        "affordance_parity",
        "transition_coverage",
    }
)

#: Jury gate kinds -- a gate whose verdict is a cross-vendor jury vote. A wave
#: carrying one (and no UI-band gate) classifies :attr:`RiskTier.HIGH`.
_JURY_GATE_KINDS: frozenset[str] = frozenset(
    {
        "jury_verdict",
        "cross_vendor_jury",
    }
)

#: Auditor / human-approval gate kinds -- a gate whose verdict is a single
#: auditor sign-off or operator attestation. A wave carrying one (and no jury /
#: UI-band gate) classifies :attr:`RiskTier.MED`.
_AUDITOR_GATE_KINDS: frozenset[str] = frozenset(
    {
        "auditor_verdict",
        "human_approval",
    }
)


def classify_risk_tier(gates: list[GateSpec]) -> RiskTier:
    """Classify a wave's auto-close :class:`RiskTier` from its gate kinds.

    A TOTAL, PURE function of the gate-kind strings -- it reads no IO, mutates
    nothing, and returns the wave's risk band so the fleet auto-drain loop can
    decide whether the wave self-closes or forks. The band is the HIGHEST
    judgement need across the wave's gates:

    1. any UI / visual-band gate (:data:`_UI_BAND_GATE_KINDS`) -> :attr:`RiskTier.UI`
       -- the wave's ground truth is a visual surface whose final oracle is the
       cross-vendor visual jury;
    2. else any non-UI jury gate (:data:`_JURY_GATE_KINDS`) -> :attr:`RiskTier.HIGH`;
    3. else any auditor / human-approval gate (:data:`_AUDITOR_GATE_KINDS`) ->
       :attr:`RiskTier.MED` -- the wave auto-closes only on a passing auditor
       verdict;
    4. else (every gate is a deterministic falsifier, or the wave carries no
       gate at all) -> :attr:`RiskTier.MECH` -- the wave self-closes on a
       deterministic pass with no human in the loop.

    The precedence is deliberate: a wave that mixes a deterministic gate with a
    jury gate is classified by its riskiest gate, never its cheapest, so the
    deterministic floor can never mask an unmet jury requirement.

    Args:
        gates: The wave's typed gate rows. May be empty (an empty set
            classifies :attr:`RiskTier.MECH`).

    Returns:
        The wave's :class:`RiskTier`.
    """
    kinds = {gate.kind for gate in gates}
    if kinds & _UI_BAND_GATE_KINDS:
        return RiskTier.UI
    if kinds & _JURY_GATE_KINDS:
        return RiskTier.HIGH
    if kinds & _AUDITOR_GATE_KINDS:
        return RiskTier.MED
    return RiskTier.MECH


def risk_tier_auto_closes(
    risk_tier: RiskTier,
    *,
    block_authority: BlockAuthority,
) -> bool:
    """Return whether a *risk_tier* wave may auto-close, or must fork.

    The auto-close / fork gate the fleet loop consults once a lane reports its
    wave reached a passing terminal. The LOAD-BEARING SAFETY INVARIANT lives
    here: a :attr:`RiskTier.HIGH` or :attr:`RiskTier.UI` wave may auto-close
    ONLY when the jury has earned :attr:`BlockAuthority.BLOCKING`; while the
    jury is :attr:`BlockAuthority.ADVISORY` (the uncalibrated default) such a
    wave always forks -- it never silently auto-closes on an unearned jury.

    The bands:

    - :attr:`RiskTier.MECH` -- always auto-closes (a deterministic pass is
      complete ground truth, no human needed);
    - :attr:`RiskTier.MED` -- always auto-closes (the auditor verdict the lane
      already carries IS the human sign-off);
    - :attr:`RiskTier.HIGH` / :attr:`RiskTier.UI` -- auto-closes IFF
      *block_authority* is :attr:`BlockAuthority.BLOCKING` (the jury has earned
      the right to gate the close); else forks.

    Args:
        risk_tier: The wave's classified :class:`RiskTier`.
        block_authority: The jury's earned authority for this close. An
            uncalibrated jury is :attr:`BlockAuthority.ADVISORY`, so a
            high / ui wave forks under it.

    Returns:
        ``True`` when the wave may auto-close, ``False`` when it must fork.
    """
    if risk_tier in {RiskTier.MECH, RiskTier.MED}:
        return True
    return block_authority is BlockAuthority.BLOCKING


__all__ = [
    "OracleResult",
    "classify_risk_tier",
    "jury_block_authority",
    "risk_tier_auto_closes",
    "run_oracle",
]
