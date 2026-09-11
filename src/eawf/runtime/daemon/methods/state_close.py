"""Close-time readiness, runtime, and evidence helpers for a wave close.

The wave-close mutation is the only state mutation with a verification
phase in front of it. This module owns the reusable pieces of that phase
-- the telemetry and runtime rollups the close-time actual derives from,
the readiness compute, the structural gate-ref validation, the
per-criterion oracle loop, and the post-commit evidence tail -- while the
orchestration that sequences them stays on the facade module.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eawf.kernel.config.schema import EuBasis
from eawf.kernel.spec.common import (
    CriterionSpec,
    validate_criterion_gate_refs,
)
from eawf.kernel.state.enums import (
    CloseAttemptStatus,
    StoreKind,
)
from eawf.kernel.state.models import (
    State,
    Wave,
)
from eawf.kernel.state.mutations import (
    Mutation,
)
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.observability.telemetry.join import (
    DEFAULT_EU_MINUTES,
    WaveSessionRollup,
    rollup_wave_sessions,
)
from eawf.observability.telemetry.models import TelemetrySession
from eawf.runtime.daemon.methods import (
    DaemonValidationError,
    MethodContext,
)
from eawf.workflow.lifecycle.transitions import (
    LifecycleError,
)
from eawf.workflow.lifecycle.wave import RuntimeDelta, compute_runtime_delta
from eawf.workflow.skills.needs_user import retract_wave_pauses
from eawf.workflow.verify.models import CloseReadiness

if TYPE_CHECKING:
    pass
from dataclasses import dataclass

from eawf.runtime.daemon.methods.state_context import config_root_for_state_path
from eawf.runtime.daemon.methods.state_jury import build_durable_audit_context
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec

logger = logging.getLogger(__name__)


def wave_close_elapsed_eu(
    wave_session_rollup: WaveSessionRollup | None,
    *,
    eu_minutes: float,
) -> float | None:
    """Return the measured elapsed EU for a wave close, or ``None``.

    Derives elapsed EU from the telemetry rollup's aggregate session
    ``duration_ms`` (the measured agent runtime captured across the
    wave's sessions) via :func:`eawf.observability.telemetry.join._duration_ms_to_eu`.
    Returns ``None`` when no rollup is present or the rollup carries no
    duration — so a wave with no captured runtime keeps the honest
    zero-EU auto-actual.

    Args:
        wave_session_rollup: The telemetry rollup joined at close, or
            ``None`` when no telemetry matched the wave's sessions.
        eu_minutes: Minutes represented by one effort unit (the same
            ``estimation.eu_minutes`` used for the rollup join).

    Returns:
        The measured elapsed EU, or ``None`` when no runtime was captured.
    """
    from eawf.observability.telemetry.join import _duration_ms_to_eu

    if wave_session_rollup is None:
        return None
    return _duration_ms_to_eu(wave_session_rollup.duration_ms, eu_minutes=eu_minutes)


def wave_runtime_delta(
    state: State,
    mutation: Mutation,
    *,
    eu_minutes: float,
    eu_basis: EuBasis,
) -> RuntimeDelta | None:
    """Return the close-time runtime delta for the wave, when captured."""
    wave_id = str(mutation.params.get("wave_id", ""))
    if not wave_id:
        return None
    wave = state.waves.get(wave_id)
    if wave is None:
        return None
    return compute_runtime_delta(
        wave.runtime_baseline,
        wave.runtime_latest,
        carry=wave.runtime_carry,
        eu_minutes=eu_minutes,
        eu_basis=eu_basis,
    )


def wave_close_rollup_config(repo_root: Path) -> tuple[str, float, EuBasis]:
    """Return close-time telemetry DB, EU minutes, and runtime-basis config."""
    try:
        from eawf.kernel.config.layered import get_dotted, merge_config

        merged, _sources = merge_config(repo=repo_root)
        db_kind = str(get_dotted(merged, "telemetry.db_kind"))
        eu_minutes = float(get_dotted(merged, "estimation.eu_minutes"))
        eu_basis_raw = str(get_dotted(merged, "estimation.eu_basis"))
    except Exception as exc:
        logger.warning(f"wave_close_rollup config='default' err={exc!s}")
        return "sqlite", DEFAULT_EU_MINUTES, EuBasis.API_DURATION
    try:
        eu_basis = EuBasis(eu_basis_raw)
    except ValueError as exc:
        raise LifecycleError(f"invalid estimation.eu_basis: {eu_basis_raw!r}") from exc
    if eu_minutes <= 0.0:
        logger.warning(f"wave_close_rollup eu_minutes={eu_minutes!r} invalid; using default")
        eu_minutes = DEFAULT_EU_MINUTES
    return db_kind, eu_minutes, eu_basis


def load_wave_session_rollup(
    state: State,
    mutation: Mutation,
    *,
    state_path: Path,
    repo_root: Path,
) -> WaveSessionRollup | None:
    """Join projected telemetry sessions for the wave being closed."""
    wave_id = str(mutation.params.get("wave_id", ""))
    if not wave_id:
        return None
    wave = state.waves.get(wave_id)
    if wave is None or not wave.sessions:
        return None

    from eawf.observability.telemetry.store import metrics_db_path, open_store

    db_path = metrics_db_path(state_path)
    if not db_path.exists():
        return None

    db_kind, eu_minutes, _eu_basis = wave_close_rollup_config(repo_root)
    store = open_store(db_kind, db_path)  # type: ignore[arg-type]
    try:
        rows = store.fetch_all("telemetry_sessions", TelemetrySession)
    except Exception as exc:
        logger.warning(f"wave_close_rollup wave={wave_id!r} status='skip' err={exc!s}")
        return None
    finally:
        store.close()

    telemetry_sessions = [row for row in rows if isinstance(row, TelemetrySession)]
    rollup = rollup_wave_sessions(wave, telemetry_sessions, eu_minutes=eu_minutes)
    if rollup.attention_eu is None:
        return None
    logger.info(
        f"wave_close_rollup wave={wave_id!r} attempts={len(rollup.attempts)} "
        f"duration_ms={rollup.duration_ms} attention_eu={rollup.attention_eu}"
    )
    return rollup


def compute_wave_close_readiness(
    state: State,
    mutation: Mutation,
    *,
    state_path: Path,
    repo_root: Path,
    defer_verdict_kinds: bool = False,
    prevalidated_gate_ids: Collection[str] = (),
) -> CloseReadiness | None:
    """Return the enforcing pre-close readiness view for a wave-close mutation.

    Returns ``None`` when no active profile enforces verify (the advisory
    paths recompute their own view); otherwise the rolled-up
    :class:`~eawf.workflow.verify.models.CloseReadiness`. The verdict gate
    (single-auditor or cross-vendor jury) runs in the separate async step
    :func:`_enforce_wave_close_gate` so this helper stays a pure, sync
    readiness compute.

    Raises:
        LifecycleError: When ``profile.verify.enforce`` is active and the
            rolled-up readiness is not ready (criteria floor /
            evidence-row rollup). The daemon maps this onto
            ``validation_failed`` like every other wave-close lifecycle
            rejection.
    """
    from eawf.kernel.store.paths import store_dir as _store_dir
    from eawf.workflow.lifecycle._errors import check_disabled_waiver_policy
    from eawf.workflow.verify import compute as compute_readiness
    from eawf.workflow.verify.readiness import (
        load_active_verify_block,
        resolve_wave_verify_block,
    )

    wave_id = str(mutation.params.get("wave_id", ""))
    if not wave_id or wave_id not in state.waves:
        return None
    # Band-conditional enforcement: the merged block records the fleet
    # intent; the wave-aware resolver narrows ``enforce`` to the UI/UX band
    # so a non-band wave keeps the advisory close path even when a
    # band-scoped profile is enabled.
    policy_block = load_active_verify_block(
        wave_id,
        state,
        repo_root=repo_root,
        config_root=config_root_for_state_path(state_path),
    )
    check_disabled_waiver_policy(
        waiver_mode="B" if policy_block is None else policy_block.waiver_mode,
        scope_id=wave_id,
        criteria=list(state.waves[wave_id].success_criteria),
        criteria_floor_waiver=state.waves[wave_id].criteria_floor_waiver,
    )
    verify_block = resolve_wave_verify_block(policy_block, state.waves[wave_id])
    if verify_block is None or not verify_block.enforce:
        return None
    deferred: frozenset[str] = frozenset()
    if defer_verdict_kinds:
        # D-LOCK-SPLIT pre-flight: an un-gated verdict-kind criterion is
        # enforced by the under-lock verdict / jury tier (which writes the
        # auditor evidence this rollup reads), so its PENDING status must
        # not block the lock-free phase.
        deferred = frozenset(
            criterion.id
            for criterion in state.waves[wave_id].success_criteria
            if criterion.required
            and not criterion.gate_ids
            and criterion.evidence_kind != "deterministic"
        )
    return compute_readiness(
        wave_id,
        state=state,
        store_dir=_store_dir(state_path),
        repo_root=repo_root,
        config_root=config_root_for_state_path(state_path),
        deferred_criterion_ids=deferred,
        prevalidated_gate_ids=prevalidated_gate_ids,
    )


def _runtime_zero_close_enforces(
    state: State,
    *,
    wave_id: str,
    state_path: Path,
    repo_root: Path,
) -> bool:
    """Return whether a zero-runtime close should block instead of warn.

    Reads the FLEET verify block, not the band-narrowed one. The UI/UX band exists
    to scope *criteria* enforcement -- a spec jury judging a screen has nothing to
    say about a wave that touches no screen -- but runtime capture is not a property
    of the band: every wave burns agent runtime and every wave's actual feeds the
    same corpus. Narrowing this gate by band made it advisory for every wave outside
    the band, which in P30-I25 meant every wave in the iter: the gate that exists to
    refuse a silent zero could not refuse anything, and reported a pass while doing
    it. A gate that cannot fail is not a gate.
    """
    from eawf.workflow.verify.readiness import load_active_verify_block

    if wave_id not in state.waves:
        return True
    verify_block = load_active_verify_block(
        wave_id,
        state,
        repo_root=repo_root,
        config_root=config_root_for_state_path(state_path),
    )
    return True if verify_block is None else verify_block.enforce


def _zero_is_explained_by_a_reset(wave: Wave) -> bool:
    """Return whether this wave's missing runtime is explained by a counter reset.

    A reset drops the runtime measured before it, so it IS an honest reason for a
    zero -- but only while nothing has been MEASURED since. Once a capture reports
    counters beyond the re-originated baseline, the capture path has proven itself
    alive and productive, and any zero from that point on is unexplained again.

    Without that second condition the exemption is a standing pardon: one reset in a
    wave's first minute would excuse every zero it ever records, including the zeros
    of a capture path that silently dies forty turns later -- the precise failure the
    gate exists to catch, laundered through the mechanism meant to keep an honest
    reset from stranding a wave.

    The condition is deliberately the CLOCK, not the counters, and the choice is
    load-bearing. "An honest reset with a no-op capture after it" and "a capture path
    that is alive but reporting a frozen snapshot" are the same bytes in
    ``state.json``: identical counters, a later ``captured_at``. One rule has to lose.
    Pardoning on frozen counters would close the second case in silence -- which is
    the original defect of this whole iter, where every wave recorded 0.0 EU for
    months and nobody noticed. Refusing it costs an explicit ``--no-runtime`` waiver,
    which is recorded on the wave and visible in review.

    So a wave whose measure was re-originated and which then takes a capture that
    measures nothing does NOT close silently; it closes with a waiver, or it closes
    once a capture measures something. Neither strands it (:func:`reorigin_on_reset`
    guarantees it stays measurable going forward), and neither hides it.
    """
    carry = wave.runtime_carry
    baseline = wave.runtime_baseline
    if carry is None or carry.counter_resets <= 0 or baseline is None:
        return False
    latest = wave.runtime_latest
    return latest is None or latest.captured_at <= baseline.captured_at


def enforce_nonzero_runtime_close(
    state: State,
    mutation: Mutation,
    *,
    elapsed_eu: float | None,
    state_path: Path,
    repo_root: Path,
) -> None:
    """Reject SILENT zero-EU wave closes unless the profile is advisory or the zero is explained.

    The word doing the work is *silent*. The gate exists because a zero-EU close
    used to mean the capture path had quietly died -- which it had, for the whole
    of its life. It does not exist to punish a wave whose runtime is missing for a
    RECORDED reason.

    A counter reset is such a reason: the source was truncated or its basis
    changed, the capture path re-originated the wave, and the runtime measured
    before that point is gone for good. The wave records this on
    :attr:`~eawf.kernel.state.models.RuntimeCarry.counter_resets`. Refusing the
    close would strand it -- the baseline lives on disk, so every retry hits the
    same zero -- which is the same unrecoverable trap the gate was written to
    prevent, just wearing the gate's own uniform.
    """
    wave_id = str(mutation.params.get("wave_id", ""))
    if not wave_id or (elapsed_eu is not None and elapsed_eu > 0.0):
        return
    message = (
        f"wave {wave_id!r} has no captured runtime; refusing silent 0-EU close "
        "without a runtime waiver"
    )
    if mutation.params.get("no_runtime_waiver") is True:
        logger.warning(
            f"wave_close_runtime_zero wave={wave_id!r} mode='waived' message={message!r}"
        )
        return
    wave = state.waves.get(wave_id)
    if wave is not None and _zero_is_explained_by_a_reset(wave):
        resets = wave.runtime_carry.counter_resets if wave.runtime_carry else 0
        logger.warning(
            f"wave_close_runtime_zero wave={wave_id!r} mode='reset' counter_resets={resets}; "
            "runtime lost to a counter-source reset -- closing on the recorded reason"
        )
        return
    if _runtime_zero_close_enforces(
        state,
        wave_id=wave_id,
        state_path=state_path,
        repo_root=repo_root,
    ):
        raise LifecycleError(message)
    logger.warning(f"wave_close_runtime_zero wave={wave_id!r} mode='warn' message={message!r}")


def validate_wave_close_gate_refs(state: State, mutation: Mutation) -> None:
    """Reject a wave-close mutation whose criterion/gate refs do not resolve.

    Runs at the close-mutation model-validate boundary REGARDLESS of
    ``verify.enforce`` -- a malformed spec (an orphan ``gate_ids`` entry,
    a gate naming an unknown criterion, an un-compilable deterministic
    gate, or an author-set ``oracle_tier``) is a structural defect that
    must be rejected before any apply, independent of whether the active
    profile gates the close.

    The check is a deliberate no-op for the grandfathered common case
    (criteria with empty ``gate_ids`` + no gate rows), so every live and
    migration-grandfathered wave closes through this boundary unchanged.

    Args:
        state: Validated state the closing wave row is read from.
        mutation: The wave-close mutation; its ``wave_id`` param names
            the wave under validation.

    Raises:
        DaemonValidationError: When
            :func:`eawf.kernel.spec.common.validate_criterion_gate_refs`
            rejects the wave's criteria / gate refs.
    """
    from eawf.workflow.verify.readiness import _load_gate_specs

    wave_id = str(mutation.params.get("wave_id", ""))
    if not wave_id or wave_id not in state.waves:
        return
    wave = state.waves[wave_id]
    try:
        validate_criterion_gate_refs(
            list(wave.success_criteria),
            _load_gate_specs(wave_id, state),
            allow_computed_tier=True,
        )
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc


def enforce_wave_verdict_gate(wave: Wave, *, state_path: Path) -> None:
    """Raise when the wave's single fresh-auditor verdict gate blocks close.

    The daemon-side hook into the dispatch-layer verdict producer
    (P29-I04-W07). It is the DEFAULT enforcing gate and the degrade target
    when the cross-vendor jury is unavailable or not opted in.
    The caller (:func:`_enforce_wave_close_gate`) has already confirmed
    ``verify_block.enforce``, so the advisory-only close paths -- and every
    wave-close test that does not enable enforcement -- are unaffected. The
    gate blocks only the required subset: a high-risk (``"always"``) or
    sampled wave whose freshest auditor verdict is absent or not close-ready
    raises; a ``"skip"`` mechanical wave never blocks.

    Args:
        wave: The wave being closed.
        state_path: Path to ``state.json``; the auditor report store
            resolves under its sibling ``store/`` directory.

    Raises:
        LifecycleError: When the verdict gate refuses close.
    """
    from eawf.workflow.dispatch.verdict import verify_wave_verdict_gate

    gate = verify_wave_verdict_gate(wave, state_path=state_path)
    if gate.passed:
        return
    reasons = "; ".join(gate.reasons) if gate.reasons else "no reasons recorded"
    logger.warning(
        f"enforce_wave_verdict_gate wave={wave.id} requirement={gate.requirement} "
        f"blocked reasons=[{reasons}]"
    )
    raise LifecycleError(
        f"wave {wave.id!r} verdict gate blocked close (requirement={gate.requirement}): {reasons}"
    )


class WaveCloseRefusalError(LifecycleError):
    """Raised when the ordered oracle refuses a wave close on one criterion.

    A :class:`~eawf.workflow.lifecycle.transitions.LifecycleError` subclass so
    the existing CLI / daemon catch sites remap it to the same exit code, but
    structured so a repair caller is FED the grounding payload directly off the
    exception rather than re-parsing the message string. The refused criterion
    and the concrete failing-check output (the oracle
    :meth:`~eawf.workflow.verify.oracle.OracleResult.failing_detail`) are carried
    as attributes so a grounded repair re-dispatch
    (:func:`eawf.workflow.dispatch.retry.build_repair_prompt`) can be built
    without the failing payload going missing -- a content-free "drifted, redo"
    repair is impossible by construction because there is no path from a refusal
    to a repair that drops the criterion text or the failing detail.

    Attributes:
        wave_id: The wave whose close was refused.
        criterion: The refused success criterion (its text grounds the repair).
        failing_detail: The concrete failing-check output the oracle refused on
            -- non-empty, the grounding payload of the repair re-dispatch.
        tier: The integer oracle tier that produced the refusal.
        status: The closed non-pass status word the oracle scored.
    """

    def __init__(
        self,
        *,
        wave_id: str,
        criterion: CriterionSpec,
        failing_detail: str,
        tier: int,
        status: str,
    ) -> None:
        self.wave_id = wave_id
        self.criterion = criterion
        self.failing_detail = failing_detail
        self.tier = tier
        self.status = status
        super().__init__(
            f"wave {wave_id!r} oracle blocked close "
            f"(criterion={criterion.id!r} tier={tier} status={status}): {failing_detail}"
        )


#: The tier word :func:`resolve_close_gate_tier` returns when the close gate
#: has nothing left to score and the caller should return no evidence.
CLOSE_GATE_TIER_SKIP: str = "skip"


def resolve_close_gate_tier(
    tier: str,
    *,
    wave: Wave,
    uiux_bands: Collection[str],
) -> str:
    """Return the oracle tier *wave*'s close gate scores at.

    Whole-fleet ``verify.enforce`` (a profile declaring no ``uiux_bands``) is
    risk-weighted, and the weighting is about the EXPENSIVE tier: a mechanical
    wave -- one whose :func:`~eawf.workflow.dispatch.verdict.verdict_requirement`
    is ``"sampled"`` or ``"skip"`` rather than ``"always"`` -- earns no fresh
    auditor and no cross-vendor jury, because a judgment call on a small
    executor wave is not worth three vendor spawns.

    It must not also skip the wave's own deterministic gates. Those are the
    cheap rung of the same escalation ladder and they are the falsifiers the
    wave itself declared, so a close that skipped the whole scoring pass for a
    mechanical wave completed against a non-empty ``required_gate_ids`` with
    zero receipts: a wave that reads as verified on every surface while nothing
    ever observed its tree. The narrowing is therefore to the tier, not to the
    pass -- a mechanical wave scores ``"deterministic"``, where the tier filter
    in :func:`score_required_criteria` drops every un-gated criterion and so
    keeps the jury tier unreached.

    A band-scoped profile (non-empty *uiux_bands*) is untouched: its resolver
    has already narrowed ``enforce`` to ``False`` for a non-band wave, so any
    wave reaching here under a banded block is in-band and keeps *tier*.

    Args:
        tier: The tier the caller asked for (``"all"`` / ``"deterministic"`` /
            ``"verdict"``).
        wave: The closing wave, classified for its verdict requirement.
        uiux_bands: The resolved verify block's band tokens; empty means a
            whole-fleet (non-band-scoped) profile.

    Returns:
        The tier to score at, or :data:`CLOSE_GATE_TIER_SKIP` when there is
        nothing for this pass to score (a mechanical wave asked for the verdict
        tier it does not earn).
    """
    from eawf.workflow.dispatch.verdict import verdict_requirement

    if uiux_bands or verdict_requirement(wave) == "always":
        return tier
    resolved = CLOSE_GATE_TIER_SKIP if tier == "verdict" else "deterministic"
    logger.info(
        f"resolve_close_gate_tier wave={wave.id} requirement=mechanical "
        f"requested={tier} resolved={resolved} spawn=skipped"
    )
    return resolved


def enforce_close_gate_receipt_floor(
    wave: Wave,
    *,
    state_path: Path,
    gate_specs: list[Any],
    close_attempt_id: str,
) -> None:
    """Refuse a durable close whose obliged gates left no receipt.

    The terminal check of the close gate. Everything ahead of it decides what
    to run; this asks only whether the run left proof. Each scoring arm can
    return an empty evidence list for a reason of its own -- a gate that
    compiled to nothing, a criterion a tier filter dropped, a runner that died
    before observing -- and none of those is distinguishable at the close
    boundary from a wave that owed no deterministic proof at all. The floor
    makes them distinguishable by reading the persisted record instead of
    trusting the pass that produced it.

    Scoped to a durable close on purpose: an advisory or daemonless close binds
    no attempt, so it has no receipt ledger to be held to. A named attempt that
    does not resolve to a row is likewise skipped -- there is no ledger to read,
    and the under-lock apply snapshot refuses that close on its own terms, so
    refusing it here would only relabel a stale close as a receipt gap.

    Args:
        wave: The closing wave; its criteria say which gates are obliged.
        state_path: Path to the live ``state.json``. Re-read here rather than
            taken from the caller's snapshot: the receipt ids are bound by a
            separate commit from inside the scoring pass, so the pre-flight
            snapshot cannot see them.
        gate_specs: The wave's typed gate rows, as the scorer saw them.
        close_attempt_id: The durable attempt, or ``""`` for a non-durable
            close (the floor is then a no-op).

    Raises:
        GateReceiptFloorError: When an obliged gate has no receipt; the message
            names the receiptless gates.
    """
    from eawf.runtime.daemon.methods.state_context import read_state
    from eawf.workflow.verify.gate_receipt_floor import (
        enforce_gate_receipt_floor,
        receipted_gate_ids,
    )

    if not close_attempt_id:
        return
    attempt = read_state(state_path)[0].close_attempts.get(close_attempt_id)
    if attempt is None:
        logger.warning(
            f"enforce_close_gate_receipt_floor wave={wave.id} "
            f"attempt={close_attempt_id!r} status=skip reason=attempt-row-absent"
        )
        return
    enforce_gate_receipt_floor(
        scope_id=wave.id,
        criteria=wave.success_criteria,
        gates=gate_specs,
        receipted=receipted_gate_ids(state_path, receipt_ids=attempt.gate_receipt_ids),
    )


async def score_required_criteria(
    wave: Wave,
    *,
    wave_id: str,
    state: State,
    state_path: Path,
    events_path: Path,
    repo_root: Path,
    gate_specs: list[Any],
    spawn_factory: Any,
    block_authority: Any,
    freshness_inputs: dict[str, Any],
    tier: str,
    high_risk_single_auditor: bool,
    close_attempt_id: str,
    reusable_pass_gate_ids: set[str] | None,
    before_gate_execute: Callable[[str, str, CheckSpec, str], CheckResult | None] | None,
    on_gate_result: Callable[[str, str, CheckResult], None] | None,
    announce_auditing: Callable[[], None],
) -> list[EvidenceRecord]:
    """Score every required criterion of *wave* through the ordered oracle.

    The per-criterion half of the enforcing close gate: the caller has already
    resolved the verify block, the jury's earned authority, and the juror spawn
    factory, so this loop only escalates each required criterion from its
    cheapest deterministic gate upward.

    Returns:
        One deterministic-pass :class:`EvidenceRecord` per criterion that scored
        at a deterministic tier. The caller appends them only after the close
        apply commits, so a later refusal leaves no stray pass row behind.

    Raises:
        WaveCloseRefusalError: When the oracle refuses one required criterion;
            the refusal carries the criterion plus the grounded failing detail
            so a repair re-dispatch is fed the concrete falsifier.
    """
    from eawf.kernel.store.kinds.evidence import deterministic_pass_record
    from eawf.workflow.verify.oracle import run_oracle

    deterministic_evidence: list[EvidenceRecord] = []
    for criterion in wave.success_criteria:
        if not criterion.required:
            continue
        gates = [g for g in gate_specs if g.criterion_id == criterion.id]
        # D-LOCK-SPLIT tier filter: a gated criterion scores at the
        # deterministic tier (off-lock); an un-gated criterion falls to the
        # verdict / jury tier (under the lock, W08-bounded). tier="all"
        # keeps the pre-split single-pass behaviour for non-split callers.
        if tier == "deterministic" and not gates:
            continue
        if tier == "verdict" and gates:
            continue
        if (
            high_risk_single_auditor
            and close_attempt_id
            and (not gates or criterion.evidence_kind != "deterministic")
        ):
            continue
        if not gates:
            announce_auditing()
        criterion_freshness = {
            gate.id: freshness_inputs[gate.id].model_copy(update={"criterion_id": criterion.id})
            for gate in gates
            if gate.id in freshness_inputs
        }
        result = await run_oracle(
            criterion,
            gates,
            wave=wave,
            state=state,
            state_path=state_path,
            events_path=events_path,
            repo_root=repo_root,
            spawn_factory=spawn_factory,
            block_authority=block_authority,
            freshness_by_gate=criterion_freshness,
            reusable_pass_gate_ids=reusable_pass_gate_ids,
            before_gate_execute=before_gate_execute,
            after_gate_execute=on_gate_result,
            require_all_deterministic=bool(close_attempt_id),
            close_attempt_id=close_attempt_id,
        )
        if result.status != "pass":
            logger.warning(
                f"_enforce_wave_close_gate wave={wave_id} criterion={criterion.id!r} "
                f"tier={int(result.tier)} status={result.status} blocked"
            )
            # Carry the criterion + the GROUNDED failing-check output onto the
            # structured refusal so a repair re-dispatch is fed the concrete
            # falsifier (never re-parsed from the message string). failing_detail
            # is non-empty by construction, so a content-free repair cannot be
            # built downstream.
            raise WaveCloseRefusalError(
                wave_id=wave_id,
                criterion=criterion,
                failing_detail=result.failing_detail(),
                tier=int(result.tier),
                status=result.status,
            )
        # Only a deterministic gate carries a gate_id; the jury /
        # single-auditor fallthrough scores the whole wave (gate_id=None)
        # and is not a code-gated check, so it mints no deterministic row.
        if result.gate_id is not None:
            deterministic_evidence.append(
                deterministic_pass_record(
                    scope_id=wave_id,
                    criterion_id=result.criterion_id,
                    gate_id=result.gate_id,
                    tier=int(result.tier),
                    detail=result.detail,
                )
            )
    return deterministic_evidence


def append_close_evidence(
    records: list[EvidenceRecord],
    *,
    state_path: Path,
) -> None:
    """Append every deterministic-pass close-gate row to ``evidence.jsonl``.

    Each :class:`EvidenceRecord` is wrapped in a
    :class:`~eawf.kernel.store.envelope.Envelope` (``kind=StoreKind.EVIDENCE``)
    and written through :func:`eawf.kernel.store.append.append_envelope` so
    the on-disk shape is indistinguishable from a row written by the
    ``evidence.append`` RPC or the waiver path. The append acquires the
    sibling ``evidence.jsonl`` portalock — distinct from the ``state.json``
    lock the close mutation holds, so there is no deadlock — which is why
    this in-close append is safe (see
    :func:`eawf.kernel.store.append.append_envelope`).

    Args:
        records: The deterministic-pass rows minted by
            :func:`_enforce_wave_close_gate`. May be empty (no-op).
        state_path: Path to ``state.json``; anchors
            ``<state_dir>/store/evidence.jsonl``.
    """
    evidence_path = store_path(state_path, StoreKind.EVIDENCE)
    for record in records:
        envelope = Envelope(
            id=record.id,
            kind=StoreKind.EVIDENCE,
            scope_id=record.scope_id,
            created_at=record.created_at,
            summary=record.summary,
            payload=record.model_dump(mode="json"),
        )
        append_envelope(evidence_path, envelope)
        logger.info(
            f"append_close_evidence scope_id={record.scope_id!r} evidence_id={record.id!r} "
            f"evidence_kind={record.evidence_kind!r} status={record.status!r}"
        )


def compute_wave_close_extras(  # noqa: C901
    state: State,
    mutation: Mutation,
    *,
    state_path: Path,
    repo_root: Path,
    readiness: CloseReadiness | None = None,
    actual_written_auto: bool = False,
) -> dict[str, str | int | float | bool]:
    """Return the W06 close-readiness advisory metrics for *mutation*.

    Folds the rolled-up advisory tally into an
    :attr:`EventPayload.extras`-shaped dict. Failures are non-blocking
    unless the pre-close readiness pass already raised under
    ``profile.verify.enforce``.

    Args:
        state: In-memory state AFTER ``apply_wave_close`` succeeds.
        mutation: The wave_close mutation just applied; its
            ``scope_id`` names the wave under evaluation.
        state_path: Filesystem path to ``state.json``; the readiness
            compute uses this to locate ``<state_dir>/store/`` for
            evidence rows.
        repo_root: Repository root the evidence freshness check runs
            against (forwarded to
            :func:`eawf.workflow.lifecycle.wave_sha.derive_wave_sha`).
        readiness: Optional pre-close readiness view. When absent, the
            helper computes an advisory view itself.
        actual_written_auto: Whether this close created the
            :class:`ActualSummary` row instead of refreshing an existing
            operator-authored actual.

    Returns:
        Dict with the ``readiness_warnings_count`` key (always set;
        ``0`` on the happy path) and — when the wave's close path
        upserted an :class:`ActualSummary` — the
        ``actual_tokens`` + ``actual_cost_usd`` rollup so the
        ``wave_closed`` event publishes the close-time cost view
        without subscribers re-reading state.json. Empty dict on
        KeyError so the envelope-extras merge stays a no-op for
        non-wave scopes.
    """
    wave_id = str(mutation.params.get("wave_id", ""))
    if not wave_id:
        return {}
    if readiness is None:
        try:
            readiness = compute_wave_close_readiness(
                state,
                mutation,
                state_path=state_path,
                repo_root=repo_root,
            )
        except KeyError as exc:
            logger.warning(f"close_advisory wave={wave_id!r} status='skip' err={exc!s}")
            return {}
    if readiness is None:
        try:
            from eawf.kernel.store.paths import store_dir as _store_dir
            from eawf.workflow.verify import compute as compute_readiness

            readiness = compute_readiness(
                wave_id,
                state=state,
                store_dir=_store_dir(state_path),
                repo_root=repo_root,
                config_root=config_root_for_state_path(state_path),
                load_profile_verify=False,
            )
        except KeyError as exc:
            logger.warning(f"close_advisory wave={wave_id!r} status='skip' err={exc!s}")
            return {}
    count = len(readiness.warnings)
    for view in readiness.criteria:
        if view.status != "pass":
            logger.warning(
                f"close_advisory wave={wave_id!r} criterion={view.id!r} status={view.status!r}"
            )
    # The daemon mediates this close (it is the canonical writer running this
    # code), so the mechanism is always "daemon"; the daemonless-with-waiver
    # bypass stamps its own mechanism on the in-process fallback close event.
    # Stamping it here guarantees EVERY daemon close event carries
    # close_mechanism alongside the in-process path so an audit can tell the two
    # apart without re-deriving.
    extras: dict[str, str | int | float | bool] = {
        "readiness_warnings_count": count,
        "close_mechanism": "daemon",
    }
    # P28-I02-W03: surface the close-time token + cost rollup on the
    # event envelope. The wave_close apply (close_wave -> upsert
    # ActualSummary) populated these from Wave.tokens_consumed; cost
    # stays 0.0 until the per-model rate table lands.
    actuals = state.actuals or {}
    actual = actuals.get(wave_id)
    if actual is not None:
        extras["actual_written_auto"] = actual_written_auto
        extras["actual_tokens"] = actual.actual_tokens
        extras["actual_cost_usd"] = actual.actual_cost_usd
        if actual.attention_eu is not None:
            extras["actual_attention_eu"] = actual.attention_eu
        if actual.agent_runtime_eu is not None:
            extras["actual_agent_runtime_eu"] = actual.agent_runtime_eu
    return extras


def retract_closed_wave_advisories(
    state_path: Path,
    *,
    wave_id: str,
    bus: object | None,
) -> None:
    """Retract the closing wave's open over-budget advisory pauses.

    The daemon's stale-wave sweep raises a durable ``needs_user`` pause when
    an active wave runs past its time budget. Nothing paired that pause with
    a resume on close, so a CLOSED wave kept surfacing the over-budget prompt
    in the operator's needs_user feed forever. Pairing the retraction with
    the close mutation clears the advisory the moment the wave reaches its
    terminal state.

    Best-effort: the close itself is already durable by the time this runs,
    so a retraction failure is logged and swallowed rather than failing a
    committed close.

    Args:
        state_path: Filesystem path to ``state.json``.
        wave_id: The wave whose close just committed.
        bus: The daemon event bus (or ``None``); a resume envelope is
            published on it so live subscribers drop the cleared pause.
    """
    if not wave_id:
        return
    publish = bus.publish if bus is not None and hasattr(bus, "publish") else None
    try:
        retract_wave_pauses(state_path, wave_id=wave_id, publish=publish)
    except OSError as exc:
        logger.warning(f"retract_closed_wave_advisories wave={wave_id!r} status='skip' err={exc!s}")


@dataclass(frozen=True)
class CloseAttemptHooks:
    """The durable-close callbacks a wave close threads into its pre-flight.

    Attributes:
        on_auditing: Announce that the attempt entered the auditing stage.
        on_audit_result: Bind the produced audit report to the attempt.
        before_gate_execute: Claim one gate execution, returning a reusable
            result when a concurrent attempt already ran it.
        on_gate_result: Persist one gate receipt and complete its claim.
        prevalidated_gate_ids: Gate ids whose receipts passed during this
            attempt; read back by the readiness compute.
    """

    on_auditing: Callable[[], None] | None
    on_audit_result: Callable[[str], None] | None
    before_gate_execute: Callable[[str, str, CheckSpec, str], CheckResult | None] | None
    on_gate_result: Callable[[str, str, CheckResult], None] | None
    prevalidated_gate_ids: set[str]


def build_close_attempt_hooks(
    ctx: MethodContext,
    *,
    state_path: Path,
    canonical_repo_root: Path,
    execution_root: Path,
    close_attempt_id: str,
) -> CloseAttemptHooks:
    """Return the durable close-attempt callbacks, moving it to CHECKING.

    An empty *close_attempt_id* is the non-durable close path: every hook stays
    ``None`` and no attempt stage transitions, so a legacy close runs exactly as
    it did before durable attempts existed.
    """
    on_auditing: Callable[[], None] | None = None
    on_audit_result: Callable[[str], None] | None = None
    before_gate_execute: Callable[[str, str, CheckSpec, str], CheckResult | None] | None = None
    on_gate_result: Callable[[str, str, CheckResult], None] | None = None
    prevalidated_gate_ids: set[str] = set()
    if close_attempt_id:
        from eawf.runtime.daemon.gate_execution import (
            claim_gate_execution,
            complete_gate_execution,
        )
        from eawf.runtime.daemon.methods.close import (
            persist_gate_receipt,
            transition_attempt_stage,
        )

        transition_attempt_stage(
            ctx,
            repo_root=canonical_repo_root,
            attempt_id=close_attempt_id,
            status=CloseAttemptStatus.CHECKING,
        )

        def _on_auditing() -> None:
            transition_attempt_stage(
                ctx,
                repo_root=canonical_repo_root,
                attempt_id=close_attempt_id,
                status=CloseAttemptStatus.AUDITING,
            )

        def _before_gate_execute(
            criterion_id: str,
            gate_id: str,
            spec: CheckSpec,
            freshness_key: str,
        ) -> CheckResult | None:
            return claim_gate_execution(
                state_path,
                attempt_id=close_attempt_id,
                criterion_id=criterion_id,
                gate_id=gate_id,
                spec=spec,
                freshness_key=freshness_key,
            )

        def _on_gate_result(
            criterion_id: str,
            gate_id: str,
            result: CheckResult,
        ) -> None:
            receipt_id = persist_gate_receipt(
                ctx,
                repo_root=canonical_repo_root,
                execution_root=execution_root,
                attempt_id=close_attempt_id,
                criterion_id=criterion_id,
                gate_id=gate_id,
                result=result,
            )
            if receipt_id is not None and result.freshness_key is not None:
                complete_gate_execution(
                    state_path,
                    attempt_id=close_attempt_id,
                    freshness_key=result.freshness_key,
                    receipt_id=receipt_id,
                    result=result,
                )
                if result.status == "pass" or (result.status is None and result.passed):
                    prevalidated_gate_ids.add(gate_id)

        def _on_audit_result(report_id: str) -> None:
            from eawf.runtime.daemon.methods.close import commit_attempt

            commit_attempt(
                ctx,
                repo_root=canonical_repo_root,
                attempt_id=close_attempt_id,
                updates={"audit_report_id": report_id},
                command="close.audit_receipt",
            )

        on_auditing = _on_auditing
        on_audit_result = _on_audit_result
        before_gate_execute = _before_gate_execute
        on_gate_result = _on_gate_result

    return CloseAttemptHooks(
        on_auditing=on_auditing,
        on_audit_result=on_audit_result,
        before_gate_execute=before_gate_execute,
        on_gate_result=on_gate_result,
        prevalidated_gate_ids=prevalidated_gate_ids,
    )


def validate_close_apply_snapshot(
    state: State,
    *,
    state_path: Path,
    repo_root: Path,
    attempt_id: str,
) -> None:
    """Reject durable close inputs or proof that drifted after READY.

    Args:
        state: Canonical state reloaded under the final mutation lock.
        state_path: Canonical state file used to resolve durable proof.
        repo_root: Repository whose effective close policy is authoritative.
        attempt_id: Durable close attempt entering APPLYING.

    Raises:
        DaemonValidationError: When the attempt, its governing inputs, or its
            bound gate/audit proof changed after READY.
    """
    from eawf.runtime.daemon.methods.close import (
        attempt_invalidation_causes,
        reusable_bound_audit_report_id,
    )

    attempt = state.close_attempts.get(attempt_id)
    if attempt is None:
        raise DaemonValidationError(
            f"validation_failed: close_preflight_stale: close attempt "
            f"{attempt_id!r} disappeared before apply"
        )
    governing_drift = attempt_invalidation_causes(
        state,
        repo_root=repo_root,
        attempt=attempt,
    )
    if governing_drift:
        raise DaemonValidationError(
            "validation_failed: close_preflight_stale: governing inputs "
            f"changed after READY: {'; '.join(governing_drift)}"
        )
    if not attempt.gate_receipt_ids and attempt.audit_report_id is None:
        return

    current_wave = state.waves.get(attempt.wave_id)
    if current_wave is None:
        raise DaemonValidationError(
            f"validation_failed: close_preflight_stale: wave "
            f"{attempt.wave_id!r} disappeared before proof CAS"
        )
    try:
        durable_context = build_durable_audit_context(
            state_path=state_path,
            close_attempt_id=attempt.id,
            wave=current_wave,
        )
        if attempt.audit_report_id is not None:
            reusable_bound_audit_report_id(
                state_path,
                attempt_id=attempt.id,
                durable_context=durable_context,
            )
    except (LifecycleError, ValueError) as exc:
        raise DaemonValidationError(
            "validation_failed: close_preflight_stale: bound gate/audit "
            f"proof changed after READY: {exc!s}"
        ) from exc
