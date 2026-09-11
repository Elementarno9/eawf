"""``state.*`` JSON-RPC methods: read / mutate / digest.

Wires the canonical mutator path for the daemon. The ``state.mutate``
handler is the **sole canonical writer** for ``state.json`` +
``event.jsonl`` (authority-map rows 1-4); every state-mutating CLI verb
routes through this RPC once ``daemon.proxy_enabled`` flips to ``true``.

Algorithm -- the transaction lifecycle:

1. Idempotency-cache lookup keyed by :attr:`Mutation.idempotency_key`.
2. ``portalock(state.json, timeout=5)`` -- defense-in-depth; AGENTS
   rule 4 retains portalocker as belt-and-braces under the daemon.
3. Read + decode + validate ``state.json`` -> :class:`State`.
4. Dispatch the :class:`MutationKind` to its per-kind apply function;
   on success the candidate :class:`State` carries the mutation.
5. Re-validate the post-mutation state -> on failure return
   ``-32002 validation_failed`` and leave ``state.json`` untouched.
6. Build the canonical event envelope (``EventPayload`` body) +
   write the WAL ``.pending.json`` record.
7. Atomic-write ``state.json`` (existing
   :func:`eawf.kernel.state.writer.atomic_write_json_locked`) -- the point of
   no return (state.json is fsynced here).
8. WAL ``.pending`` -> ``.applied`` rename, BEFORE the event append, so
   a crash in the state-write->event-append window leaves an APPLIED
   record. :func:`eawf.runtime.daemon.recovery.replay_wal` re-issues the
   captured envelope for an APPLIED record (idempotent on envelope id),
   whereas a PENDING record would be POISONED and the event row lost --
   diverging state from the event log.
9. Append the envelope to ``event.jsonl`` via
   :func:`eawf.kernel.store.append.append_envelope`, then WAL
   ``.applied`` -> ``.fsynced`` (lock-free renames from
   :mod:`eawf.runtime.daemon.wal`).
10. Publish the envelope on the subscription bus
    (:meth:`eawf.runtime.daemon.bus.EventBus.publish`).
11. Release portalock; cache the result for the idempotency window;
    return ``{event, before_version, after_version}``.

This module is the facade of the state-method family: it owns the
registered handlers, the wave-close orchestration, and the auditor / jury
gate that sequences the rest. The per-concern collaborators live beside
it and are re-exported here so the module's import surface is unchanged:

* :mod:`~eawf.runtime.daemon.methods.state_models` -- typed params / results.
* :mod:`~eawf.runtime.daemon.methods.state_context` -- repo anchoring, state
  read/digest, idempotency cache.
* :mod:`~eawf.runtime.daemon.methods.state_apply` -- per-kind appliers and the
  dispatch table.
* :mod:`~eawf.runtime.daemon.methods.state_close` -- close readiness, runtime
  rollups, the per-criterion oracle loop, close evidence.
* :mod:`~eawf.runtime.daemon.methods.state_jury` -- auditor / juror spawn
  plumbing and durable audit context.
* :mod:`~eawf.runtime.daemon.methods.state_events` -- event-envelope builders.
* :mod:`~eawf.runtime.daemon.methods.state_codex` -- ``runtime.codex_lifecycle``.
* :mod:`~eawf.runtime.daemon.methods.state_runtime` -- ``runtime.capture``.
* :mod:`~eawf.runtime.daemon.methods.state_worktree` -- ``wave land`` /
  ``wave autoland`` / ``track.sync``.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import orjson
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AgentSessionRole,
    CloseAttemptStatus,
    StoreKind,
)
from eawf.kernel.state.models import (
    State,
    Wave,
)
from eawf.kernel.state.mutations import (
    Mutation,
    MutationKind,
)
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.kernel.validate.strict import validate_state
from eawf.runtime.daemon import wal
from eawf.runtime.daemon.methods import (
    VALIDATION_FAILED,
    DaemonValidationError,
    MethodContext,
    register,
)
from eawf.runtime.daemon.wal import WalRecord
from eawf.workflow.lifecycle.transitions import (
    LifecycleError,
    LifecycleGuardError,
    close_iter,
)
from eawf.workflow.verify.preflight import run_close_preflight

if TYPE_CHECKING:
    from eawf.platform.profiles.models import VerifyBlock
    from eawf.workflow.dispatch.verdict import DurableAuditContext
from eawf.runtime.daemon.methods.state_apply import (
    ApplyFunc,
    apply_iter_open,
    apply_mutation_under_lock,
    apply_phase_open,
    apply_roadmap_revise,
    apply_wave_close,
    build_apply_registry,
    log_guard_rejection,
    sync_wave_close_track,
)
from eawf.runtime.daemon.methods.state_close import (
    WaveCloseRefusalError,
    append_close_evidence,
    build_close_attempt_hooks,
    compute_wave_close_extras,
    compute_wave_close_readiness,
    enforce_close_gate_receipt_floor,
    enforce_nonzero_runtime_close,
    enforce_wave_verdict_gate,
    load_wave_session_rollup,
    resolve_close_gate_tier,
    retract_closed_wave_advisories,
    score_required_criteria,
    validate_close_apply_snapshot,
    validate_wave_close_gate_refs,
    wave_close_elapsed_eu,
    wave_close_rollup_config,
    wave_runtime_delta,
)
from eawf.runtime.daemon.methods.state_codex import codex_lifecycle
from eawf.runtime.daemon.methods.state_context import (
    IDEMPOTENCY_TTL_SECONDS,
    bus_for_root,
    config_root_for_state_path,
    event_store_path_for,
    evict_expired,
    idempotency_cache,
    read_state,
    resolve_mutator_paths,
    resolve_state_path,
    state_version,
)
from eawf.runtime.daemon.methods.state_events import (
    MUTATION_EVENT_KIND,
    bucket_drift_extras,
    build_bucket_drift_envelope,
    build_event_envelope,
    mutation_event_extras,
    publish_wave_elapsed_updates,
)
from eawf.runtime.daemon.methods.state_jury import (
    build_durable_audit_context,
    cross_vendor_lanes_ready,
    jury_spawn_factory,
    load_wave_spec,
    persist_auditor_session_snapshot,
    resolve_jury_block_authority,
)
from eawf.runtime.daemon.methods.state_models import (
    CachedMutation,
    DigestParams,
    DigestResult,
    MutateParams,
    MutateResult,
    ReadParams,
    ReadResult,
)
from eawf.runtime.daemon.methods.state_runtime import (
    counters_incomparable,
    merge_runtime_latest,
    rebase_for_session,
    reorigin_on_reset,
    runtime_capture,
    upsert_interactive_session_attempt,
)
from eawf.runtime.daemon.methods.state_worktree import (
    commit_worktree_state,
    track_sync_rpc,
    wave_autoland_rpc,
    wave_land_batch_rpc,
    wave_land_rpc,
)
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec

logger = logging.getLogger(__name__)


# Pre-split private spellings, kept resolvable on the facade: callers
# monkeypatch several of them through this module and one source-scanning gate
# reads them off this file, so both names must land on the same object.
_CachedMutation = CachedMutation
_MUTATION_EVENT_KIND = MUTATION_EVENT_KIND
_apply_iter_open = apply_iter_open
_apply_phase_open = apply_phase_open
_apply_roadmap_revise = apply_roadmap_revise
_build_durable_audit_context = build_durable_audit_context
_build_event_envelope = build_event_envelope
_commit_worktree_state = commit_worktree_state
_compute_wave_close_extras = compute_wave_close_extras
_compute_wave_close_readiness = compute_wave_close_readiness
_counters_incomparable = counters_incomparable
_cross_vendor_lanes_ready = cross_vendor_lanes_ready
_enforce_wave_verdict_gate = enforce_wave_verdict_gate
_jury_spawn_factory = jury_spawn_factory
_merge_runtime_latest = merge_runtime_latest
_read_state = read_state
_rebase_for_session = rebase_for_session
_reorigin_on_reset = reorigin_on_reset
_resolve_jury_block_authority = resolve_jury_block_authority
_resolve_state_path = resolve_state_path
_state_version = state_version
_upsert_interactive_session_attempt = upsert_interactive_session_attempt
_validate_wave_close_gate_refs = validate_wave_close_gate_refs
_wave_close_elapsed_eu = wave_close_elapsed_eu
_wave_close_rollup_config = wave_close_rollup_config


def _spec_jury_ballot_fn(state: State, wave: Wave, *, repo_root: Path) -> Any:
    """Return the live per-item ballot fn for the spec jury, or ``None``.

    The TRUST-5 live binding: it reuses the cross-vendor jury's per-runtime
    spawn factory (:func:`_jury_spawn_factory`) and the wave's on-disk rubric
    (:func:`load_wave_spec` -> :func:`eawf.kernel.spec.rubric.rubric_items`)
    to bind :func:`eawf.workflow.dispatch.spec_jury.live_per_item_ballot_fn`,
    which drives each disjoint juror runtime through the bounded re-ask loop
    and parses one per-item ballot per juror. Returns ``None`` only when fewer
    than :data:`~eawf.observability.eval.cross_vendor_jury.JURY_QUORUM` of the
    disjoint vendor CLIs resolve on the host -- a box that cannot cast
    independent cross-vendor ballots keeps the producer idle and degrades to
    the single-auditor / cross-vendor gate rather than spawning a degenerate
    jury. Tests monkeypatch this builder to return a canned ballot fn so the
    gate -> producer -> report-write wiring is exercised without a real spawn.

    Args:
        state: Validated state -- read for the wave's sandbox deny-list +
            role (forwarded to :func:`_jury_spawn_factory`).
        wave: The banded wave under audit (supplies role + effort for model
            routing).
        repo_root: Repository root the juror spawns run in + the spec anchor.

    Returns:
        A :data:`~eawf.workflow.dispatch.spec_jury.PerItemBallotFn` bound to
        the live jury, or ``None`` when too few vendor CLIs resolve to convene.
    """
    from eawf.kernel.spec.rubric import rubric_items
    from eawf.observability.eval.cross_vendor_jury import JURY_QUORUM
    from eawf.workflow.dispatch.spec_jury import live_per_item_ballot_fn

    if not _cross_vendor_lanes_ready(quorum=JURY_QUORUM):
        logger.info(f"_spec_jury_ballot_fn wave={wave.id} status=idle reason=sub-quorum-lanes")
        return None
    spec = load_wave_spec(wave.id, repo_root=repo_root)
    rubric = rubric_items(spec) if spec is not None else ()
    spawn_factory = _jury_spawn_factory(state, wave, repo_root=repo_root)
    return live_per_item_ballot_fn(spawn_factory=spawn_factory, rubric=rubric)


async def _enforce_spec_jury_gate(
    state: State,
    wave: Wave,
    *,
    state_path: Path,
    repo_root: Path,
    verify_block: VerifyBlock | None = None,
) -> bool:
    """Route a UI/UX-banded wave through the spec-jury producer + map its verdict.

    The spec-jury flavour of the close gate (P29-I08-W05 / TRUST-5). It loads
    the wave's :class:`~eawf.kernel.spec.wave.WaveSpec`, resolves a fresh
    AUDITOR session, computes the jury's earned block authority
    (:func:`_resolve_jury_block_authority`), and runs the per-rubric-item
    producer (:func:`eawf.workflow.dispatch.spec_jury.produce_spec_jury_verdict`)
    with the LIVE ballot fn from :func:`_spec_jury_ballot_fn`. When the producer
    is idle (no ballot fn bound -- too few vendor lanes) or the rubric is empty
    the producer returns a typed ``"skipped"`` result and this helper returns
    ``False`` so the caller falls through to the existing single-auditor /
    cross-vendor gate -- the banded path is a NON-breaking addition.

    Advisory-until-blocking: a non-close-ready verdict (FAIL / BLOCKED) is held
    ADVISORY by default -- the producer writes the verdict for the operator and
    returns it, and this helper returns ``False`` so the close still falls
    through to the default gate. Only once the jury has EARNED BLOCKING
    authority does the producer raise :class:`LifecycleError` and block close;
    a close-ready verdict returns ``True`` so the caller treats the band gate
    as satisfied.

    Args:
        state: Validated state -- mutated in place by the auditor session
            registration; the close path persists it.
        wave: The banded wave being closed.
        state_path: Path to ``state.json``; the auditor report store + the
            session-start event resolve under its sibling ``store/``.
        repo_root: Repository root for the spec-file anchor + diff-base
            derivation.
        verify_block: The resolved verify block whose ``jury_authority`` leaf
            supplies the trust floors for the earned-authority computation.
            ``None`` keeps the jury advisory.

    Returns:
        ``True`` when the spec jury scored a close-ready verdict (the band
        gate is satisfied); ``False`` when the producer was idle / skipped, or
        scored a non-close-ready verdict held advisory (the caller falls
        through to the default gate).

    Raises:
        LifecycleError: When the spec jury scored a non-close-ready verdict AND
            the jury has earned BLOCKING authority.
    """
    from eawf.kernel.state.enums import AgentReportVerdict as _Verdict
    from eawf.workflow.dispatch.spec_jury import produce_spec_jury_verdict
    from eawf.workflow.dispatch.verdict import _resolve_auditor_session

    ballot_fn = _spec_jury_ballot_fn(state, wave, repo_root=repo_root)
    if ballot_fn is None:
        logger.info(f"_enforce_spec_jury_gate wave={wave.id} status=idle degrade=default-gate")
        return False

    spec = load_wave_spec(wave.id, repo_root=repo_root)
    events_path = store_path(state_path, StoreKind.EVENT)
    auditor_session = _resolve_auditor_session(
        state=state,
        events_path=events_path,
        wave=wave,
        runtime="claude-code",
        now=None,
    )
    block_authority = _resolve_jury_block_authority(
        state, state_path=state_path, verify_block=verify_block
    )
    result = await produce_spec_jury_verdict(
        state=state,
        state_path=state_path,
        wave=wave,
        spec=spec,
        auditor_session_id=auditor_session.id,
        per_item_ballot_fn=ballot_fn,
        block_authority=block_authority,
        repo_root=repo_root,
    )
    if not result.scored:
        logger.info(
            f"_enforce_spec_jury_gate wave={wave.id} status=skipped "
            f"reason={result.reason!r} degrade=default-gate"
        )
        return False
    close_ready = {_Verdict.PASS, _Verdict.PASS_WITH_FOLLOWUPS}
    if result.verdict in close_ready:
        logger.info(
            f"_enforce_spec_jury_gate wave={wave.id} status=scored "
            f"verdict={result.verdict.value if result.verdict else 'none'} passed=True"
        )
        return True
    # A scored non-close-ready verdict that did NOT raise means the producer
    # held it advisory (the jury has not earned blocking authority): the
    # verdict is recorded but the close falls through to the default gate.
    verdict_value = result.verdict.value if result.verdict is not None else "none"
    logger.info(
        f"_enforce_spec_jury_gate wave={wave.id} status=scored "
        f"verdict={verdict_value} advisory=True degrade=default-gate"
    )
    return False


async def _produce_high_risk_verdict(
    state: State,
    wave: Wave,
    *,
    state_path: Path,
    repo_root: Path,
    wall_clock_seconds: float,
    reuse_existing: bool = True,
    durable_context: DurableAuditContext | None = None,
) -> str | None:
    """Write the single fresh-auditor verdict the high-risk close gate reads.

    The producer half of the high-risk single-auditor close gate: it spawns
    a fresh-context auditor (via
    :func:`eawf.workflow.dispatch.verdict.produce_wave_verdict`) so the
    verdict :func:`_enforce_wave_verdict_gate` then reads is actually
    persisted. The spawn is scoped to the high-risk subset and is
    idempotent against an existing close-ready verdict:

    * the caller invokes this ONLY for an ``"always"`` (high-risk) wave, so
      a non-high-risk close never reaches the producer and never spawns an
      auditor;
    * a wave whose freshest persisted auditor verdict is already close-ready
      is a no-op -- the gate would pass on the read alone, so re-spawning
      would burn a redundant auditor.

    Only an ``"always"`` wave with an absent or non-close-ready verdict
    spawns. The single juror runtime is bound from
    :func:`_jury_spawn_factory` at the ``claude-code`` family so the produce
    stays a single-auditor gate -- the cross-vendor jury is a separate
    opt-in path the close gate routes to only when ``cross_vendor_jury`` is
    set.

    Args:
        state: Validated state -- mutated in place by the auditor session
            registration; the close path persists it.
        wave: The high-risk wave being closed.
        state_path: Path to ``state.json``; the auditor report store +
            events resolve under its sibling ``store/`` directory.
        repo_root: Repository root forwarded to the auditor's diff-base
            derivation + spawn cwd.
        wall_clock_seconds: Ceiling for the auditor spawn, taken from the
            active verify block. It is threaded rather than defaulted because a
            thorough audit of a large wave can outrun the factory's 600s
            default, and a killed auditor writes no verdict -- which the gate
            reads as "no verdict" and refuses the close, so the wave can never
            close no matter how many times the operator retries.
        durable_context: Optional exact close-attempt evidence context, built
            only after deterministic GateReceipts have been persisted.
    """
    from eawf.workflow.dispatch.verdict import (
        produce_wave_verdict,
        verify_wave_verdict_gate,
    )

    if reuse_existing and verify_wave_verdict_gate(wave, state_path=state_path).passed:
        from eawf.workflow.agent_report.rollup import iter_agent_reports

        rows = iter_agent_reports(
            state_path,
            role=AgentSessionRole.AUDITOR,
            base_id=wave.id,
        )
        logger.debug(f"_produce_high_risk_verdict wave={wave.id} status=already-ready")
        return rows[-1].envelope.id if rows else None
    events_path = store_path(state_path, StoreKind.EVENT)
    # Thread events_path so the single fresh-auditor spawn streams its stdout
    # live to the auditor's Watch roster row.
    spawn = _jury_spawn_factory(
        state,
        wave,
        repo_root=repo_root,
        timeout_seconds=wall_clock_seconds,
        events_path=events_path,
    )("claude-code")

    def _persist_live_auditor_session(registered: State) -> None:
        """Write the freshly-registered auditor session so Watch can see it.

        Verification owns a snapshot and intentionally holds no state lock
        while the auditor runs. Merge only the qualified auditor session into
        the latest canonical state so concurrent lifecycle mutations survive.
        """
        persist_auditor_session_snapshot(
            registered,
            state_path=state_path,
            wave_id=wave.id,
        )
        logger.info(f"_produce_high_risk_verdict wave={wave.id} status=auditor-session-persisted")

    def _persist_terminal_auditor_session(terminal: State) -> None:
        """Persist the auditor terminal row before later close guards run."""
        persist_auditor_session_snapshot(
            terminal,
            state_path=state_path,
            wave_id=wave.id,
        )
        logger.info(f"_produce_high_risk_verdict wave={wave.id} status=auditor-session-terminal")

    verdict_result = await produce_wave_verdict(
        state=state,
        state_path=state_path,
        events_path=events_path,
        wave=wave,
        spawn=spawn,
        repo_root=repo_root,
        on_session_registered=_persist_live_auditor_session,
        on_session_terminalized=_persist_terminal_auditor_session,
        durable_context=durable_context,
    )
    if verdict_result is None:
        logger.warning(f"_produce_high_risk_verdict wave={wave.id} status=no-report")
        return None
    logger.info(f"_produce_high_risk_verdict wave={wave.id} status=produced")
    return verdict_result.append_result.envelope.id


async def _enforce_wave_close_gate(
    state: State,
    mutation: Mutation,
    *,
    state_path: Path,
    repo_root: Path,
    tier: str = "all",
    on_auditing: Callable[[], None] | None = None,
    on_audit_result: Callable[[str], None] | None = None,
    before_gate_execute: Callable[
        [str, str, CheckSpec, str],
        CheckResult | None,
    ]
    | None = None,
    on_gate_result: Callable[[str, str, CheckResult], None] | None = None,
    reusable_pass_gate_ids: set[str] | None = None,
) -> list[EvidenceRecord]:
    """Run the enforcing wave-close gate via the ordered oracle.

    The async daemon-side hook the close path awaits before applying a
    wave-close mutation. It loads the active verify block and runs the gate
    ONLY when an enabled profile sets ``verify.enforce`` -- so the
    advisory-only close paths (and every wave-close test that does not enable
    enforcement) are byte-unchanged: the early-return guard short-circuits
    before the per-criterion loop runs.

    Past the guard, each REQUIRED criterion on the wave is scored through
    :func:`eawf.workflow.verify.oracle.run_oracle`, which escalates the
    criterion's gates from the cheapest deterministic tier upward and only
    consults the jury / single-auditor tier last. A criterion whose
    :class:`~eawf.workflow.verify.oracle.OracleResult` status is not
    ``"pass"`` blocks the close; all required criteria passing lets close
    proceed. A criterion's gates are gathered from
    :func:`eawf.workflow.verify.readiness._load_gate_specs` (which reads the
    wave's typed ``gates`` rows) filtered to that criterion; a criterion with
    a passing deterministic gate scores at that gate's tier, while an un-gated
    criterion falls through to the verdict / jury tier -- preserving the prior
    single-auditor / cross-vendor-jury behaviour.

    Each criterion that PASSES at a deterministic tier (the
    :class:`~eawf.workflow.verify.oracle.OracleResult` carries a non-None
    ``gate_id`` only on the deterministic-gate branch) mints one
    ``deterministic`` / ``pass`` :class:`EvidenceRecord`. The records are
    BUILT here but NOT yet persisted -- the caller appends them only after
    ``apply_wave_close`` succeeds, so a wave whose close is later refused
    on a different criterion never leaves a stray pass row behind. The
    jury / single-auditor fallthrough has ``gate_id is None`` and mints no
    deterministic row (it is not a code-gated check).

    Args:
        state: Validated state -- mutated in place when a jury registers its
            auditor sessions; the close path persists it.
        mutation: The wave-close mutation; its ``wave_id`` param names the
            wave under the gate.
        state_path: Path to ``state.json``; report stores + events resolve
            under its sibling ``store/``.
        repo_root: Repository root for the verify-block config anchor + the
            juror diff-base / spawn cwd.

    Returns:
        The deterministic-pass :class:`EvidenceRecord` rows the caller
        appends to ``evidence.jsonl`` after the apply commits. Empty on
        every advisory / early-return path and for a wave whose required
        criteria were all scored by the jury / single-auditor tier.

    Raises:
        WaveCloseRefusalError: When the ordered oracle refuses a wave close on a
            required criterion -- a :class:`LifecycleError` subclass carrying
            the refused criterion + the grounded failing-check output so a
            repair re-dispatch is fed the concrete falsifier.
        GateReceiptFloorError: When a durable close reaches the end of the
            scoring pass with a required blocking gate that left no receipt.
        LifecycleError: When the high-risk single-auditor gate refuses close.
    """
    from eawf.observability.eval.jury_validation import BlockAuthority
    from eawf.workflow.dispatch.verdict import verdict_requirement
    from eawf.workflow.verify.readiness import (
        _load_gate_specs,
        load_active_verify_block,
        resolve_wave_verify_block,
    )

    wave_id = str(mutation.params.get("wave_id", ""))
    if not wave_id or wave_id not in state.waves:
        return []
    wave = state.waves[wave_id]
    close_attempt_id = str(mutation.params.get("close_attempt_id", ""))
    auditing_announced = False

    def _announce_auditing() -> None:
        nonlocal auditing_announced
        if on_auditing is not None and not auditing_announced:
            on_auditing()
            auditing_announced = True

    # Band-conditional enforcement: the merged block records the fleet
    # intent; the wave-aware resolver narrows ``enforce`` +
    # ``cross_vendor_jury`` to the UI/UX band so the gate fires for a band
    # wave and a non-band wave returns early on the advisory path.
    verify_block = resolve_wave_verify_block(
        load_active_verify_block(
            wave_id,
            state,
            repo_root=repo_root,
            config_root=config_root_for_state_path(state_path),
        ),
        wave,
    )
    if verify_block is None or not verify_block.enforce:
        return []
    # High-risk single-auditor gate. The verdict gate is a READ -- it only
    # blocks close when a fresh auditor verdict is already persisted -- so
    # the close path must WRITE that verdict first, but only for the
    # high-risk subset and only when the cross-vendor jury is not opted in.
    # A high-risk wave under an opted-in jury falls through to run_oracle's
    # jury tier; a mechanical wave takes the risk-weighted early-return or
    # the run_oracle path below depending on whether the profile is banded.
    # The jury's earned authority is computed BEFORE the single-auditor
    # branch: with the config merge OR-ing cross_vendor_jury across enabled
    # profiles, a bare cross_vendor_jury check let an ADVISORY jury displace
    # the one oracle that blocks today (the A4 OR-fold bypass; 40 of P30's
    # 98 verdict-always waves lost their gate). The jury replaces the
    # blocking single-auditor only once it has EARNED blocking authority.
    block_authority = _resolve_jury_block_authority(
        state, state_path=state_path, verify_block=verify_block
    )
    jury_replaces_auditor = (
        verify_block.cross_vendor_jury and block_authority is BlockAuthority.BLOCKING
    )
    high_risk_single_auditor = verdict_requirement(wave) == "always" and not jury_replaces_auditor
    if high_risk_single_auditor and not close_attempt_id:
        # The whole wave is covered by the blocking single-auditor; the
        # deterministic tier defers to the verdict tier (the auditor spawn
        # is lock-scoped and W08-bounded under the D-LOCK-SPLIT ordering).
        if tier == "deterministic":
            return []
        _announce_auditing()
        audit_report_id = await _produce_high_risk_verdict(
            state,
            wave,
            state_path=state_path,
            repo_root=repo_root,
            wall_clock_seconds=verify_block.juror_wall_clock_seconds,
            reuse_existing=on_audit_result is None,
        )
        if audit_report_id is not None and on_audit_result is not None:
            on_audit_result(audit_report_id)
        _enforce_wave_verdict_gate(wave, state_path=state_path)
        logger.info(f"_enforce_wave_close_gate wave={wave_id} high_risk=single-auditor passed=True")
        return []
    # Whole-fleet enforce is risk-weighted for the EXPENSIVE tier only; the
    # resolver below narrows the tier a mechanical wave scores at rather than
    # letting its risk band skip the scoring pass outright.
    events_path = store_path(state_path, StoreKind.EVENT)
    spawn_factory = _jury_spawn_factory(
        state,
        wave,
        repo_root=repo_root,
        timeout_seconds=verify_block.juror_wall_clock_seconds,
        events_path=events_path,
    )
    gate_specs = _load_gate_specs(wave_id, state)
    tier = resolve_close_gate_tier(tier, wave=wave, uiux_bands=verify_block.uiux_bands)
    if tier == "skip":
        return []
    freshness_inputs: dict[str, Any] = {}
    if close_attempt_id:
        from eawf.runtime.daemon.methods.close import gate_freshness_inputs

        freshness_inputs = gate_freshness_inputs(
            state,
            attempt_id=close_attempt_id,
        )
    # The staged advisory-to-block gate (TRUST-4): block_authority (computed
    # once above) is threaded into every per-criterion run_oracle call; with
    # an empty validation substrate the jury stays advisory, so an enforcing
    # close never blocks on an uncalibrated jury.
    deterministic_evidence = await score_required_criteria(
        wave,
        wave_id=wave_id,
        state=state,
        state_path=state_path,
        events_path=events_path,
        repo_root=repo_root,
        gate_specs=gate_specs,
        spawn_factory=spawn_factory,
        block_authority=block_authority,
        freshness_inputs=freshness_inputs,
        tier=tier,
        high_risk_single_auditor=high_risk_single_auditor,
        close_attempt_id=close_attempt_id,
        reusable_pass_gate_ids=reusable_pass_gate_ids,
        before_gate_execute=before_gate_execute,
        on_gate_result=on_gate_result,
        announce_auditing=_announce_auditing,
    )
    if high_risk_single_auditor and close_attempt_id and tier in {"all", "verdict"}:
        durable_context = _build_durable_audit_context(
            state_path=state_path,
            close_attempt_id=close_attempt_id,
            wave=wave,
        )
        _announce_auditing()
        from eawf.runtime.daemon.methods.close import (
            reusable_bound_audit_report_id,
        )

        try:
            audit_report_id = reusable_bound_audit_report_id(
                state_path,
                attempt_id=close_attempt_id,
                durable_context=durable_context,
            )
        except ValueError as exc:
            raise LifecycleError(f"bound audit report invalid: {exc!s}") from exc
        if audit_report_id is None:
            audit_report_id = await _produce_high_risk_verdict(
                state,
                wave,
                state_path=state_path,
                repo_root=repo_root,
                wall_clock_seconds=verify_block.juror_wall_clock_seconds,
                reuse_existing=False,
                durable_context=durable_context,
            )
        if audit_report_id is not None and on_audit_result is not None:
            on_audit_result(audit_report_id)
        _enforce_wave_verdict_gate(wave, state_path=state_path)
        logger.info(
            f"_enforce_wave_close_gate wave={wave_id} "
            "high_risk=deterministic-then-single-auditor passed=True"
        )
    enforce_close_gate_receipt_floor(
        wave,
        state_path=state_path,
        gate_specs=gate_specs,
        close_attempt_id=close_attempt_id,
    )
    logger.info(
        f"_enforce_wave_close_gate wave={wave_id} oracle=pass "
        f"criteria={len(wave.success_criteria)} "
        f"deterministic_evidence={len(deterministic_evidence)}"
    )
    return deterministic_evidence


def _apply_iter_close(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.ITER_CLOSE` — delegate to ``close_iter``.

    The optional ``odr_floor`` / ``odr_blocking`` params are threaded in
    daemon-side by :func:`thread_iter_close_verify_params` from the resolved
    verify block, so the repo's ``verify.odr_blocking`` opt-in actually
    reaches the ODR gate instead of dying at the default arguments.
    """
    from eawf.observability.metrics.odr import DEFAULT_ODR_FLOOR

    params = mutation.params
    close_iter(
        state,
        iter_id=str(params["iter_id"]),
        audit_id=str(params["audit_id"]),
        odr_floor=float(params.get("odr_floor", DEFAULT_ODR_FLOOR)),
        odr_blocking=bool(params.get("odr_blocking", False)),
        require_audit_accepted=bool(params.get("require_audit_accepted", False)),
    )


#: Closed ``MutationKind -> apply`` dispatch table for :func:`mutate`.
_APPLY_REGISTRY: Final[dict[MutationKind, ApplyFunc]] = build_apply_registry(
    apply_iter_close=_apply_iter_close,
)


def _resolve_apply(kind: MutationKind) -> ApplyFunc:
    """Look up the apply function for *kind*.

    Raises:
        NotImplementedError: when the kind is enumerated but no apply
            is wired yet. The handler treats this as a clean RPC error
            so the CLI wrapper can fall back to the in-process path.
    """
    func = _APPLY_REGISTRY.get(kind)
    if func is None:
        raise NotImplementedError(f"no apply registered for mutation kind {kind.value!r}")
    return func


# ---- Handlers ---------------------------------------------------------------


@register("state.read")
async def read(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the full ``state.json`` payload + digest version.

    Args:
        ctx: Server context — ``ctx.state_path`` is consulted only as a
            legacy fallback when *params* omits ``repo_root``.
        params: JSON-RPC params per :class:`ReadParams`.

    Returns:
        Dict matching :class:`ReadResult` — state payload as JSON-mode
        dict and the 16-hex-char digest.

    Raises:
        RuntimeError: when neither *params* nor the legacy ``ctx``
            fields resolve to a state path.
        ValueError: when the on-disk payload fails schema validation;
            the server maps this to ``-32602 invalid_params`` per
            :func:`eawf.runtime.daemon.server._process_frame`.
    """
    args = ReadParams.model_validate(params)
    state_path = resolve_state_path(repo_root=args.repo_root, ctx=ctx)
    _, payload = _read_state(state_path)
    version = state_version(payload)
    return ReadResult(state=payload, version=version).model_dump(mode="json")


@register("state.digest")
async def digest(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the digest of the on-disk state and emit elapsed ticks.

    Used by the TUI mtime-poll fallback. The same poll cadence is also
    the lightweight live-clock source for active-wave elapsed updates:
    once an active wave crosses a new elapsed-minute boundary, the daemon
    appends and publishes a ``wave_elapsed_update`` event without
    mutating ``state.json``.

    Args:
        ctx: Server context — ``ctx.state_path`` is consulted only as a
            legacy fallback when *params* omits ``repo_root``.
        params: JSON-RPC params per :class:`DigestParams`.

    Returns:
        Dict matching :class:`DigestResult`.
    """
    args = DigestParams.model_validate(params)
    state_path = resolve_state_path(repo_root=args.repo_root, ctx=ctx)
    if not state_path.exists():
        # An absent state file is a digest of empty bytes — keeps the
        # TUI poll path from faulting on an uninitialised project.
        return DigestResult(version=hashlib.sha256(b"").hexdigest()[:16]).model_dump(mode="json")
    raw = state_path.read_bytes()
    version = hashlib.sha256(raw).hexdigest()[:16]
    try:
        payload = orjson.loads(raw)
        state = State.model_validate(payload)
    except (orjson.JSONDecodeError, ValidationError) as exc:
        logger.warning(f"digest_elapsed_update status='skip' err={exc!r}")
    else:
        event_path = (
            Path(ctx.event_path)
            if args.repo_root is None and ctx.event_path is not None
            else store_path(state_path, StoreKind.EVENT)
        )
        publish_wave_elapsed_updates(
            ctx=ctx,
            state=state,
            state_path=state_path,
            event_path=event_path,
            version=version,
            now=datetime.now(UTC),
        )
    return DigestResult(version=version).model_dump(mode="json")


@register("state.mutate")
async def mutate(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Canonical state mutator — see module docstring for the algorithm.

    Args:
        ctx: Server context. ``ctx.state_path`` + ``ctx.event_path``
            MUST be configured; ``ctx.wal_dir`` (W09 field) names the
            WAL directory the mutator writes into.
        params: JSON-RPC params per :class:`MutateParams`.

    Returns:
        Dict matching :class:`MutateResult`.

    Raises:
        RuntimeError: when ``ctx.state_path``, ``ctx.event_path``, or
            ``ctx.wal_dir`` is missing.
        DaemonValidationError: when the mutation body fails the typed
            contract, a closure-kind or coded claim-session lifecycle guard
            rejects the mutation, or the post-mutation state fails schema /
            invariant validation; mapped to ``-32002 validation_failed`` by
            the server. Other non-closure lifecycle-guard rejections raise a
            plain ``ValueError`` (``-32602 INVALID_PARAMS``) so the exit code
            matches the in-process fallback.
    """
    try:
        args = MutateParams.model_validate(params)
    except ValidationError as exc:
        # The server maps a bare ValueError → -32602; we raise the typed
        # DaemonValidationError so the server emits -32002 instead,
        # because the param envelope was syntactically fine — the body
        # failed the typed Mutation contract.
        raise DaemonValidationError(f"validation_failed: {exc}") from exc

    state_path, event_path, wal_path = resolve_mutator_paths(
        repo_root=args.repo_root,
        ctx=ctx,
    )

    mutation = args.mutation
    idempotency_key = args.idempotency_key or mutation.idempotency_key
    # Cache entries are namespaced per state root: the machine-global daemon
    # serves many repos and a bare client key must not replay another
    # repo's result. The WAL record keeps the raw key (wire contract).
    cache_key = f"{state_path}:{idempotency_key}" if idempotency_key is not None else None
    cache = idempotency_cache(ctx)
    now_mono = time.monotonic()
    evict_expired(cache, now=now_mono)
    if cache_key is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            result = dict(cached.result)
            result["idempotent_replay"] = True
            logger.info(
                f"mutate idempotent_replay mutation_kind={mutation.kind.value} "
                f"scope_id={mutation.scope_id!r} key={idempotency_key!r}"
            )
            return result

    apply_func = _resolve_apply(mutation.kind)

    # WAVE_CLOSE rides the D-LOCK-SPLIT path: lock-free pre-flight
    # (deterministic gates + floor pack, the minutes-long shell-outs) then
    # an optimistic ms-scale commit under the lock. Every other mutation
    # kind keeps the single-lock path below.
    if mutation.kind == MutationKind.WAVE_CLOSE:
        ctx.in_flight_mutations += 1
        ctx.mutation_started(mutation.mutation_id, mutation.kind.value)
        try:
            return await _mutate_wave_close(
                ctx,
                mutation=mutation,
                idempotency_key=idempotency_key,
                cache_key=cache_key,
                cache=cache,
                state_path=state_path,
                event_path=event_path,
                wal_path=wal_path,
                repo_root_override=args.repo_root,
            )
        finally:
            duration_ms = ctx.mutation_finished(mutation.mutation_id)
            ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)
            if duration_ms is not None:
                logger.info(
                    f"mutate finished mutation_kind={mutation.kind.value} "
                    f"scope_id={mutation.scope_id!r} duration_ms={duration_ms:.1f}"
                )

    # The portalock keeps the daemon's defense-in-depth guard live
    # (rule 4 V1 carve-out); concurrent recovery writers serialise
    # against it through the same lock path.
    from eawf.runtime.lock import portalock

    ctx.in_flight_mutations += 1
    ctx.mutation_started(mutation.mutation_id, mutation.kind.value)
    try:
        with portalock.acquire(state_path, timeout=5.0) as generic_lock_handle:
            ctx.active_lock_handle = generic_lock_handle
            state, payload = _read_state(state_path)
            before_version = state_version(payload)

            apply_mutation_under_lock(
                state,
                mutation,
                apply_func=apply_func,
                state_path=state_path,
                repo_root_override=args.repo_root,
            )

            state.updated_at = datetime.now(UTC)
            new_payload = state.model_dump(mode="json")
            post = validate_state(new_payload, strict_optional=False)
            if post.state is None:
                raise DaemonValidationError(
                    "validation_failed: post-mutation schema invalid: "
                    + "; ".join(post.schema_errors[:3])
                )
            if post.violations:
                violation_codes = ",".join(v.code for v in post.violations)
                raise DaemonValidationError(
                    f"validation_failed: post-mutation invariants violated: {violation_codes}"
                )
            after_version = state_version(new_payload)

            # W06 advisory: compute close-readiness AFTER the apply
            # succeeds and pin the rolled-up count on the envelope
            # extras. Wave-close only; non-wave mutations get an empty
            # extras dict so the envelope shape stays uniform.
            extras = mutation_event_extras(state, mutation)
            drift_extras: dict[str, str | int | float | bool] = {}

            envelope = build_event_envelope(
                mutation=mutation,
                before_version=before_version,
                after_version=after_version,
                extras=extras,
            )
            drift_envelope = (
                build_bucket_drift_envelope(
                    mutation=mutation,
                    before_version=before_version,
                    after_version=after_version,
                    extras=drift_extras,
                )
                if drift_extras
                else None
            )

            # Outcome-WAL pending record carries the post-apply envelope
            # so startup replay (see :mod:`eawf.runtime.daemon.recovery`) can
            # re-issue verbatim if we crash between this point and the
            # event-jsonl append.
            record = WalRecord(
                record_id=mutation.mutation_id,
                envelope=envelope,
                idempotency_key=idempotency_key,
                written_at=datetime.now(UTC),
                before_state_version=before_version,
                after_state_version=after_version,
                state_path=str(state_path),
            )
            wal.write_pending(wal_path, record)

            # ``atomic_write_json_locked`` fsyncs state.json — the point of
            # no return. Mark the record APPLIED immediately after, BEFORE
            # the event append, so a crash in the state-write→event-append
            # window leaves an APPLIED record (not a PENDING one). Replay
            # then re-issues the captured envelope (idempotent on envelope
            # id); a PENDING record would instead be POISONED and the
            # event row silently lost, diverging state from the event log.
            atomic_write_json_locked(state_path, new_payload)
            wal.mark_applied(wal_path, mutation.mutation_id)
            append_envelope(event_path, envelope)
            if drift_envelope is not None:
                append_envelope(event_path, drift_envelope)
            wal.mark_fsynced(wal_path, mutation.mutation_id)

            # Persist the deterministic-pass evidence rows the close gate
            # minted, AFTER the state write commits. Each row lands in the
            # sibling ``evidence.jsonl`` (its own portalock, distinct from
            # the state lock — no deadlock) so the deterministic-evidence
            # pipeline is no longer write-idle: the trust scorecard reads
            # these ``deterministic`` / ``pass`` rows to label the wave
            # ``verified``. Empty on every advisory / non-enforcing close.
            bus = bus_for_root(ctx, state_path)
            if bus is not None:
                bus.publish(envelope)
                if drift_envelope is not None:
                    bus.publish(drift_envelope)
            ctx.last_event_id = envelope.id

            logger.info(
                f"mutate ok mutation_kind={mutation.kind.value} scope_id={mutation.scope_id!r} "
                f"before={before_version} after={after_version} envelope_id={envelope.id!r}"
            )

            result = MutateResult(
                event=envelope.model_dump(mode="json"),
                before_version=before_version,
                after_version=after_version,
                idempotent_replay=False,
            ).model_dump(mode="json")

            if cache_key is not None:
                cache[cache_key] = CachedMutation(
                    result=result,
                    cached_at=time.monotonic(),
                )
            return result
    finally:
        ctx.active_lock_handle = None
        duration_ms = ctx.mutation_finished(mutation.mutation_id)
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)
        if duration_ms is not None:
            logger.info(
                f"mutate finished mutation_kind={mutation.kind.value} "
                f"scope_id={mutation.scope_id!r} duration_ms={duration_ms:.1f}"
            )


async def _mutate_wave_close(
    ctx: MethodContext,
    *,
    mutation: Mutation,
    idempotency_key: str | None,
    cache_key: str | None,
    cache: dict[str, CachedMutation],
    state_path: Path,
    event_path: Path,
    wal_path: Path,
    repo_root_override: str | None,
) -> dict[str, Any]:
    """Run a WAVE_CLOSE as lock-free pre-flight + optimistic ms-scale commit.

    The D-LOCK-SPLIT restructure (ZD-R1, the root wedge): the whole close
    previously ran inside one ``portalock.acquire`` hold, so a close whose
    deterministic gates shell out to pytest / pre-commit / mypy held the
    state lock for minutes and every concurrent mutator LockTimeouted.

    Three phases:

    1. **Pre-flight (no lock)** — snapshot state, capture the pre-flight
       version, run the full :func:`run_close_preflight` bundle (deterministic
       gates plus any required auditor/jury verdict), and take the rollup /
       runtime-delta reads. Auditor session callbacks merge only their own
       qualified session row through short independent lock holds.
    2. **Commit (lock, ms-scale)** — re-read state and compare versions;
       when the target wave row itself changed since pre-flight the close
       is REFUSED with a typed stale error (the caller retries, which
       re-runs pre-flight off-lock). Apply, post-validate, WAL, atomic write,
       and event append are the only work inside the close lock.
    3. **Post-lock** — deterministic-evidence append (its own sibling
       portalock), bus publish, advisory retraction, idempotency cache.

    Args:
        ctx: Server context.
        mutation: The WAVE_CLOSE mutation.
        idempotency_key: Optional raw client retry key, stamped on the WAL
            record (wire contract).
        cache_key: Root-namespaced cache key (``<state_path>:<key>``), or
            ``None`` when no idempotency key was supplied.
        cache: The daemon idempotency cache to populate on success.
        state_path: Path to ``state.json``.
        event_path: Path to the event JSONL store.
        wal_path: Path to the daemon WAL directory.
        repo_root_override: The caller-supplied repo root, or ``None``.

    Returns:
        Dict matching :class:`MutateResult`.

    Raises:
        DaemonValidationError: On a lifecycle / gate / schema rejection, or
            when the optimistic re-check finds the wave row changed during
            pre-flight (``close_preflight_stale``).
    """
    from functools import partial

    from eawf.runtime.lock import portalock

    verification_repo_root = mutation.params.get("verification_repo_root")
    repo_anchor = (
        Path(str(verification_repo_root))
        if verification_repo_root
        else (
            Path(repo_root_override)
            if repo_root_override
            else config_root_for_state_path(state_path)
        )
    )
    canonical_repo_root = (
        Path(repo_root_override) if repo_root_override else config_root_for_state_path(state_path)
    )
    wave_id = str(mutation.params.get("wave_id", ""))
    close_attempt_id = str(mutation.params.get("close_attempt_id", ""))
    hooks = build_close_attempt_hooks(
        ctx,
        state_path=state_path,
        canonical_repo_root=canonical_repo_root,
        execution_root=repo_anchor,
        close_attempt_id=close_attempt_id,
    )
    prevalidated_gate_ids = hooks.prevalidated_gate_ids

    # ---- Phase 1: pre-flight, NO lock --------------------------------------
    state_pre, payload_pre = _read_state(state_path)
    preflight_version = state_version(payload_pre)
    preflight_wave_row = (payload_pre.get("waves") or {}).get(wave_id)
    try:
        preflight = await run_close_preflight(
            state_pre,
            mutation,
            state_path=state_path,
            repo_root=repo_anchor,
            validate_gate_refs=_validate_wave_close_gate_refs,
            enforce_close_gate=partial(
                _enforce_wave_close_gate,
                tier="all",
                on_auditing=hooks.on_auditing,
                on_audit_result=hooks.on_audit_result,
                before_gate_execute=hooks.before_gate_execute,
                on_gate_result=hooks.on_gate_result,
            ),
            compute_readiness=partial(
                _compute_wave_close_readiness,
                defer_verdict_kinds=True,
                prevalidated_gate_ids=prevalidated_gate_ids,
            ),
        )
    except LifecycleGuardError as exc:
        log_guard_rejection(mutation, exc)
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    except LifecycleError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    wave_close_readiness = preflight.readiness
    _, _close_eu_minutes, _close_eu_basis = wave_close_rollup_config(repo_anchor)
    runtime_delta = wave_runtime_delta(
        state_pre,
        mutation,
        eu_minutes=_close_eu_minutes,
        eu_basis=_close_eu_basis,
    )
    wave_close_rollup = load_wave_session_rollup(
        state_pre,
        mutation,
        state_path=state_path,
        repo_root=repo_anchor,
    )
    if close_attempt_id:
        from eawf.runtime.daemon.methods.close import mark_attempt_ready

        mark_attempt_ready(
            ctx,
            repo_root=canonical_repo_root,
            attempt_id=close_attempt_id,
            expected_wave_payload=preflight_wave_row,
        )
    # A ZERO delta must not suppress the rollup. A zero means the snapshots yielded
    # nothing (a reset re-originated them, or nothing was captured) -- it is an
    # absence of evidence, and the telemetry rollup may hold real evidence of the
    # wave's runtime. Preferring a manufactured 0.0 over a measured figure throws
    # away the better answer.
    measured_eu = runtime_delta.elapsed_eu if runtime_delta is not None else None
    close_elapsed_eu = (
        measured_eu
        if measured_eu
        else wave_close_elapsed_eu(
            wave_close_rollup,
            eu_minutes=_close_eu_minutes,
        )
    )

    # ---- Phase 2: commit under the lock (ms-scale only) ---------------------
    # The handle reset MUST ride a finally: a refused close (stale row,
    # gate refusal, post-validate reject) raises out of the with-block
    # after the handle is already released, and a dangling closed handle
    # kills the watchdog's next heartbeat (W35 review blocker).
    if close_attempt_id:
        from eawf.runtime.daemon.methods.close import transition_attempt_stage

        transition_attempt_stage(
            ctx,
            repo_root=canonical_repo_root,
            attempt_id=close_attempt_id,
            status=CloseAttemptStatus.APPLYING,
        )
    try:
        with portalock.acquire(state_path, timeout=5.0) as lock_handle:
            ctx.active_lock_handle = lock_handle
            state, payload = _read_state(state_path)
            before_version = state_version(payload)
            if before_version != preflight_version:
                # The optimistic re-check: another writer moved state during
                # pre-flight. A target-Wave change invalidates immediately;
                # unrelated rows may move, subject to the durable attempt's
                # governing-input CAS below.
                commit_wave_row = (payload.get("waves") or {}).get(wave_id)
                if commit_wave_row != preflight_wave_row:
                    raise DaemonValidationError(
                        f"validation_failed: close_preflight_stale: wave {wave_id!r} "
                        "changed during the lock-free pre-flight; retry the close "
                        "(the retry re-runs pre-flight off-lock)"
                    )
            if close_attempt_id:
                validate_close_apply_snapshot(
                    state,
                    state_path=state_path,
                    repo_root=canonical_repo_root,
                    attempt_id=close_attempt_id,
                )
            wave_close_evidence = list(preflight.evidence)
            actual_written_auto = bool(wave_id and wave_id not in (state.actuals or {}))
            try:
                enforce_nonzero_runtime_close(
                    state,
                    mutation,
                    elapsed_eu=close_elapsed_eu,
                    state_path=state_path,
                    repo_root=repo_anchor,
                )
                apply_wave_close(
                    state,
                    mutation,
                    wave_session_rollup=wave_close_rollup,
                    elapsed_eu=close_elapsed_eu,
                    runtime_delta=runtime_delta,
                )
                sync_wave_close_track(state, mutation)
            except LifecycleGuardError as exc:
                log_guard_rejection(mutation, exc)
                raise DaemonValidationError(f"validation_failed: {exc}") from exc
            except LifecycleError as exc:
                raise DaemonValidationError(f"validation_failed: {exc}") from exc
            except ValidationError as exc:
                raise DaemonValidationError(f"validation_failed: {exc}") from exc
            except KeyError as exc:
                raise DaemonValidationError(f"validation_failed: missing param {exc!s}") from exc

            state.updated_at = datetime.now(UTC)
            new_payload = state.model_dump(mode="json")
            post = validate_state(new_payload, strict_optional=False)
            if post.state is None:
                raise DaemonValidationError(
                    "validation_failed: post-mutation schema invalid: "
                    + "; ".join(post.schema_errors[:3])
                )
            if post.violations:
                violation_codes = ",".join(v.code for v in post.violations)
                raise DaemonValidationError(
                    f"validation_failed: post-mutation invariants violated: {violation_codes}"
                )
            after_version = state_version(new_payload)

            extras = _compute_wave_close_extras(
                state,
                mutation,
                state_path=state_path,
                repo_root=repo_anchor,
                readiness=wave_close_readiness,
                actual_written_auto=actual_written_auto,
            )
            drift_extras = bucket_drift_extras(state)
            envelope = build_event_envelope(
                mutation=mutation,
                before_version=before_version,
                after_version=after_version,
                extras=extras,
            )
            drift_envelope = (
                build_bucket_drift_envelope(
                    mutation=mutation,
                    before_version=before_version,
                    after_version=after_version,
                    extras=drift_extras,
                )
                if drift_extras
                else None
            )
            record = WalRecord(
                record_id=mutation.mutation_id,
                envelope=envelope,
                idempotency_key=idempotency_key,
                written_at=datetime.now(UTC),
                before_state_version=before_version,
                after_state_version=after_version,
                state_path=str(state_path),
            )
            wal.write_pending(wal_path, record)
            atomic_write_json_locked(state_path, new_payload)
            wal.mark_applied(wal_path, mutation.mutation_id)
            append_envelope(event_path, envelope)
            if drift_envelope is not None:
                append_envelope(event_path, drift_envelope)
            wal.mark_fsynced(wal_path, mutation.mutation_id)
    finally:
        ctx.active_lock_handle = None

    # ---- Phase 3: post-lock tail --------------------------------------------
    if wave_close_evidence:
        append_close_evidence(wave_close_evidence, state_path=state_path)
    bus = bus_for_root(ctx, state_path)
    if bus is not None:
        bus.publish(envelope)
        if drift_envelope is not None:
            bus.publish(drift_envelope)
    retract_closed_wave_advisories(state_path, wave_id=wave_id, bus=bus)
    ctx.last_event_id = envelope.id

    logger.info(
        f"mutate ok mutation_kind={mutation.kind.value} scope_id={mutation.scope_id!r} "
        f"before={before_version} after={after_version} envelope_id={envelope.id!r} "
        f"lock_split=True"
    )
    result = MutateResult(
        event=envelope.model_dump(mode="json"),
        before_version=before_version,
        after_version=after_version,
        idempotent_replay=False,
    ).model_dump(mode="json")
    if cache_key is not None:
        cache[cache_key] = CachedMutation(
            result=result,
            cached_at=time.monotonic(),
        )
    return result


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "VALIDATION_FAILED",
    "_APPLY_REGISTRY",
    "WaveCloseRefusalError",
    "codex_lifecycle",
    "event_store_path_for",
    "runtime_capture",
    "track_sync_rpc",
    "wave_autoland_rpc",
    "wave_land_batch_rpc",
    "wave_land_rpc",
]
