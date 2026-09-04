"""Per-:class:`MutationKind` apply functions and their dispatch table.

Each applier is a thin adapter that unpacks the loose ``Mutation.params``
dict and delegates to the typed lifecycle transition owning the rule.
Keeping them together makes the registry a closed, auditable table:
every enumerated kind resolves to exactly one apply function.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from eawf.kernel.config.schema import VerifyWaiverMode
from eawf.kernel.spec.common import (
    grandfather_criterion,
)
from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    AgentSessionRole,
    EffortBucket,
    PhaseStatus,
    TrackKind,
)
from eawf.kernel.state.models import (
    CriteriaFloorWaiver,
    State,
)
from eawf.kernel.state.mutations import (
    DecisionMutationError,
    MemoryMutationError,
    Mutation,
    MutationKind,
    apply_decision_obsolete,
    apply_memory_add,
    apply_memory_prune,
    apply_memory_review,
    apply_memory_supersede,
    apply_memory_update,
)
from eawf.observability.telemetry.join import (
    WaveSessionRollup,
)
from eawf.runtime.daemon.methods import (
    DaemonValidationError,
)
from eawf.workflow.lifecycle._capacity import (
    DEFAULT_MAX_PARALLEL_WAVES,
    resolve_max_parallel_waves,
)
from eawf.workflow.lifecycle.transitions import (
    LifecycleError,
    LifecycleGuardError,
    activate_phase,
    add_track,
    archive_phase,
    claim_wave,
    close_phase,
    close_wave,
    edit_iter_plan,
    edit_wave_plan,
    fail_wave,
    open_iter,
    open_phase,
    plan_wave,
    release_wave,
    remove_wave_plan,
    set_wave_deps,
    switch_track,
)
from eawf.workflow.lifecycle.wave import RuntimeDelta

if TYPE_CHECKING:
    pass
from eawf.runtime.daemon.methods.state_context import config_root_for_state_path

logger = logging.getLogger(__name__)


# ---- Apply registry ---------------------------------------------------------

#: Callable that mutates *state* in place per the supplied
#: :class:`Mutation` payload. Apply functions raise
#: :class:`LifecycleError` to signal a guard rejection
#: (mapped to ``-32002 validation_failed``).
ApplyFunc = Callable[[State, Mutation], None]


def apply_wave_claim(
    state: State,
    mutation: Mutation,
    *,
    max_parallel_waves: int = DEFAULT_MAX_PARALLEL_WAVES,
) -> None:
    """Apply :attr:`MutationKind.WAVE_CLAIM` — delegate to ``claim_wave``."""
    params = mutation.params
    claim_wave(
        state,
        wave_id=str(params["wave_id"]),
        session_id=str(params["session_id"]),
        out_of_order=bool(params.get("out_of_order", False)),
        max_parallel_waves=max_parallel_waves,
        waiver_mode=mutation_waiver_mode(mutation),
    )


def mutation_waiver_mode(mutation: Mutation) -> VerifyWaiverMode:
    """Return the daemon-injected waiver mode, defaulting for direct tests."""
    value = mutation.params.get("waiver_mode", "B")
    if value not in {"A", "B", "C", "disabled"}:
        raise LifecycleError(f"invalid waiver_mode: {value!r}")
    return cast("VerifyWaiverMode", value)


def thread_mutation_waiver_mode(
    state: State,
    mutation: Mutation,
    *,
    state_path: Path,
    repo_root_override: str | None,
) -> None:
    """Overwrite caller input with the strict config-derived waiver policy."""
    from eawf.workflow.verify.readiness import load_active_waiver_mode

    config_root = config_root_for_state_path(state_path)
    repo_root = Path(repo_root_override) if repo_root_override else config_root
    scope_id = str(mutation.params.get("wave_id") or mutation.scope_id)
    mutation.params["waiver_mode"] = load_active_waiver_mode(
        scope_id,
        state,
        repo_root=repo_root,
        config_root=config_root,
    )


def apply_wave_close(
    state: State,
    mutation: Mutation,
    *,
    wave_session_rollup: WaveSessionRollup | None = None,
    elapsed_eu: float | None = None,
    runtime_delta: RuntimeDelta | None = None,
) -> None:
    """Apply :attr:`MutationKind.WAVE_CLOSE` — delegate to ``close_wave``.

    Optionally pins ``Wave.commit`` when the params carry a resolved
    SHA; the CLI side (in :mod:`eawf.surfaces.cli.commands.lifecycle`) resolves
    ``--commit <ref>`` BEFORE calling the daemon so the daemon never
    has to invoke git.

    *elapsed_eu* is the measured runtime EU that the auto-created
    :class:`ActualSummary` records on ``elapsed_eu``; it may come from the
    runtime baseline/latest delta or from the legacy telemetry rollup.
    ``None`` leaves the auto-created elapsed at ``0.0``.
    """
    params = mutation.params
    tokens_raw = params.get("tokens_consumed")
    close_tokens = None
    if runtime_delta is not None:
        close_tokens = runtime_delta.actual_tokens
    elif tokens_raw is not None:
        close_tokens = int(tokens_raw)
    # WaveSessionRollup only carries ``attention_eu`` today (see
    # :class:`eawf.observability.telemetry.join.WaveSessionRollup`). Until the
    # rollup gains a separate runtime-EU column, runtime EU stays ``None`` so
    # the close path never substitutes attention for runtime — the two metrics
    # measure different things and a conflated value would mis-rollup the
    # WaveSessionRollup variance / velocity numbers downstream.
    rollup_attention_eu = (
        wave_session_rollup.attention_eu if wave_session_rollup is not None else None
    )
    wave = close_wave(
        state,
        wave_id=str(params["wave_id"]),
        outcome=str(params["outcome"]),
        tokens_consumed=close_tokens,
        actual_attention_eu=rollup_attention_eu,
        actual_agent_runtime_eu=(
            runtime_delta.agent_runtime_eu if runtime_delta is not None else None
        ),
        actual_elapsed_eu=elapsed_eu,
        actual_cost_usd=runtime_delta.actual_cost_usd if runtime_delta is not None else None,
    )
    commit = params.get("commit")
    if commit is not None:
        wave.commit = str(commit)
        identity_digest = params.get("commit_identity_digest")
        wave.commit_identity_digest = str(identity_digest) if identity_digest is not None else None


def _resolve_wave_track_id(state: State, wave_id: str) -> str | None:
    """Return the Track id that owns *wave_id*, or ``None``.

    Resolves the ``Wave -> Iter -> Phase`` chain and reads the
    :attr:`Phase.track_id` the phase was stamped with when it opened while a
    Track was in focus (the P30-I11-W03 silent phase-tag binding). Falls back to
    :attr:`CurrentPointers.track_id` when the chain does not resolve a tag so a
    close fired with a Track in focus but an un-tagged phase still syncs the
    active Track. ``None`` means no Track owns the wave -- the close-time sync is
    then a no-op.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        return state.current.track_id
    iter_row = (state.iters or {}).get(wave.iter_id)
    if iter_row is not None:
        phase = (state.phases or {}).get(iter_row.phase_id)
        if phase is not None and phase.track_id:
            return phase.track_id
    return state.current.track_id


def sync_wave_close_track(state: State, mutation: Mutation) -> list[str]:
    """Recompute the closing wave's Track outcome statuses in place.

    The wave-close hook half of the Track outcome reducer: once
    ``apply_wave_close`` has flipped the wave to CLOSED, the Track that owns the
    wave has its measured outcome statuses re-derived from their samples via
    :func:`eawf.workflow.evidence.outcome.sync_track_outcomes`, so closing work
    that moves a metric updates the Track's standings without a manual
    ``outcome set`` re-run. Resolving no Track (an un-tagged wave with no Track
    in focus) makes the hook a no-op.

    Returns:
        The ids of the outcomes whose status changed (empty on a no-op).
    """
    from eawf.workflow.evidence.outcome import sync_track_outcomes

    wave_id = str(mutation.params.get("wave_id", ""))
    if not wave_id:
        return []
    track_id = _resolve_wave_track_id(state, wave_id)
    if track_id is None:
        return []
    return sync_track_outcomes(state, track_id=track_id)


def apply_wave_fail(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.WAVE_FAIL` — delegate to ``fail_wave``."""
    params = mutation.params
    fail_wave(
        state,
        wave_id=str(params["wave_id"]),
        reason=str(params["reason"]),
    )


def apply_phase_open(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.PHASE_OPEN` — delegate to ``open_phase``.

    Optionally seeds :attr:`Phase.intent` from a typed
    :class:`IntentBrief` dict on ``params['intent']``. Additive +
    replay-safe — omitting it leaves the phase intent unset.
    """
    params = mutation.params
    description = params.get("description")
    intent_raw = params.get("intent")
    intent = IntentBrief.model_validate(intent_raw) if intent_raw is not None else None
    open_phase(
        state,
        phase_id=str(params["phase_id"]),
        title=str(params["title"]),
        scope_id=params.get("scope_id"),
        description=str(description) if description is not None else None,
        intent=intent,
    )


def apply_phase_activate(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.PHASE_ACTIVATE` — delegate to ``activate_phase``."""
    activate_phase(state, phase_id=str(mutation.params["phase_id"]))


def apply_phase_close(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.PHASE_CLOSE` — delegate to ``close_phase``."""
    params = mutation.params
    close_phase(
        state,
        phase_id=str(params["phase_id"]),
        audit_id=str(params["audit_id"]),
        checkpoint=params.get("checkpoint"),
    )


def apply_iter_open(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.ITER_OPEN` — delegate to ``open_iter``.

    Optionally seeds :attr:`Iter.intent` from a typed
    :class:`IntentBrief` dict on ``params['intent']``. Additive +
    replay-safe — omitting it leaves the iter intent unset.
    """
    params = mutation.params
    description = params.get("description")
    intent_raw = params.get("intent")
    intent = IntentBrief.model_validate(intent_raw) if intent_raw is not None else None
    open_iter(
        state,
        iter_id=str(params["iter_id"]),
        phase_id=str(params["phase_id"]),
        title=str(params["title"]),
        description=str(description) if description is not None else None,
        intent=intent,
    )


def thread_iter_close_verify_params(
    state: State,
    mutation: Mutation,
    *,
    state_path: Path,
    repo_root_override: str | None,
) -> None:
    """Thread resolved verify leaves into an ITER_CLOSE without weakening.

    Runs under the commit lock with the freshly read state, so the flags
    the applier consumes reflect the same config the close is about. Params
    Caller-supplied ODR dials retain their historical precedence. Audit
    acceptance is tighten-only: a caller may opt in, but cannot override an
    enforcing profile or repo leaf with ``False``. Resolution failures leave
    caller/default behaviour in place.
    """
    from eawf.workflow.verify.readiness import load_active_verify_block

    iter_id = str(mutation.params.get("iter_id", ""))
    caller_requires_audit = bool(mutation.params.get("require_audit_accepted", False))
    repo_root = Path(repo_root_override) if repo_root_override else state_path.parent.parent
    verify_block = load_active_verify_block(
        iter_id,
        state,
        repo_root=repo_root,
        config_root=config_root_for_state_path(state_path),
    )
    if verify_block is None:
        mutation.params["require_audit_accepted"] = caller_requires_audit
        return
    mutation.params.setdefault("odr_floor", verify_block.odr_floor)
    mutation.params.setdefault("odr_blocking", verify_block.odr_blocking)
    mutation.params["require_audit_accepted"] = (
        caller_requires_audit or verify_block.require_iter_audit_accepted
    )


def apply_track_add(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.TRACK_ADD` — delegate to ``add_track``."""
    params = mutation.params
    domains = params.get("domains") or []
    add_track(
        state,
        code=str(params["code"]),
        kind=TrackKind(str(params["kind"])),
        title=str(params["title"]),
        domains=list(domains),
    )


def apply_track_switch(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.TRACK_SWITCH` — delegate to ``switch_track``."""
    switch_track(state, code=str(mutation.params["code"]))


def apply_wave_release(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.WAVE_RELEASE` — delegate to ``release_wave``.

    Releases a claimed/in-progress wave back to ``pending`` so another
    runtime can re-claim it (the inverse of ``WAVE_CLAIM``). The
    optional ``reason`` is recorded on the lifecycle log line only.
    """
    params = mutation.params
    release_wave(
        state,
        wave_id=str(params["wave_id"]),
        reason=str(params["reason"]) if params.get("reason") is not None else None,
    )


def apply_phase_archive(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.ROADMAP_DROP` — delegate to ``archive_phase``.

    ``roadmap drop`` archives a PLANNED phase (PLANNED → ARCHIVED) and
    cascades its non-terminal child iters / waves to ABANDONED.
    """
    archive_phase(state, phase_id=str(mutation.params["phase_id"]))


def apply_roadmap_revise(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.ROADMAP_REVISE` — dispatch one revise op.

    ``roadmap revise`` is the structured-flag editor for PENDING waves
    under a PLANNED or ACTIVE phase. Exactly one operation per mutation,
    keyed by ``params['op']`` (one of ``add_wave`` / ``remove_wave`` /
    ``set_deps`` / ``retitle``), delegating to the matching
    :mod:`eawf.workflow.lifecycle.wave` transition. The CLI side resolves bare
    ``W##`` ids to full ``P##-I##-W##`` ids before calling the daemon, so
    the apply works with already-canonical ids. The ``retitle`` op routes
    to :func:`eawf.workflow.lifecycle.iter_.edit_iter_plan` when ``params`` carries
    an ``iter_id``; otherwise it retitles the wave named by ``wave_id``.

    The ``description`` param is wired into both ``add_wave`` (passed to
    :func:`eawf.workflow.lifecycle.wave.plan_wave`) and ``retitle`` (routed to
    the appropriate ``edit_*_plan`` transition alongside the optional
    title). Omitting it leaves the underlying field unchanged; a supplied
    string is bound-checked at ≤500 chars by the model.

    The ``intent`` param (a dict matching :class:`IntentBrief`) is also
    wired into ``add_wave`` and ``retitle`` (both wave + iter forms).
    Omitting it leaves the existing intent untouched; a supplied dict is
    validated against :class:`IntentBrief` (which raises
    :class:`pydantic.ValidationError` on a bound or unknown-field
    failure). Additive + replay-safe per the AGENTS "state vs specs"
    rule — on-disk state without an ``intent`` field re-validates.

    Raises:
        LifecycleError: when ``op`` is missing or unknown, the ``add_wave``
            op carries no ``intent`` param (authored waves must attach an
            IntentBrief), or the underlying wave transition rejects the
            edit.
        pydantic.ValidationError: when the ``intent`` param payload
            fails the :class:`IntentBrief` typed contract.
    """
    params = mutation.params
    op = params.get("op")
    description = params.get("description")
    description_str = str(description) if description is not None else None
    intent_raw = params.get("intent")
    intent = IntentBrief.model_validate(intent_raw) if intent_raw is not None else None
    if op == "add_wave":
        if intent is None:
            raise LifecycleError(
                f"add_wave for {params.get('wave_id')!r} requires an intent param; "
                "authored waves carry an IntentBrief"
            )
        role = AgentSessionRole(params["agent_role"]) if params.get("agent_role") else None
        bucket = EffortBucket(params["effort_bucket"]) if params.get("effort_bucket") else None
        plan_wave(
            state,
            wave_id=str(params["wave_id"]),
            iter_id=str(params["iter_id"]),
            title=str(params["title"]),
            file_scopes=list(params.get("file_scopes", [])),
            deps=list(params["deps"]) if params.get("deps") is not None else None,
            success_criteria=(
                [
                    grandfather_criterion(str(text), index=idx)
                    for idx, text in enumerate(params["success_criteria"], start=1)
                ]
                if params.get("success_criteria") is not None
                else None
            ),
            agent_role=role,
            effort_bucket=bucket,
            description=description_str,
            intent=intent,
            criteria_floor_waiver=(
                CriteriaFloorWaiver(
                    reason=str(params["criteria_floor_waiver_reason"]),
                    waived_at=datetime.now(UTC),
                )
                if params.get("criteria_floor_waiver_reason")
                else None
            ),
            waiver_mode=mutation_waiver_mode(mutation),
        )
    elif op == "remove_wave":
        remove_wave_plan(state, wave_id=str(params["wave_id"]))
    elif op == "set_deps":
        set_wave_deps(state, wave_id=str(params["wave_id"]), deps=list(params["deps"]))
    elif op == "retitle":
        title_raw = params.get("title")
        title_str = str(title_raw) if title_raw is not None else None
        if params.get("iter_id") is not None:
            edit_iter_plan(
                state,
                iter_id=str(params["iter_id"]),
                title=title_str,
                description=description_str,
                intent=intent,
            )
        else:
            edit_wave_plan(
                state,
                wave_id=str(params["wave_id"]),
                title=title_str,
                description=description_str,
                intent=intent,
                waiver_mode=mutation_waiver_mode(mutation),
            )
    else:
        raise LifecycleError(f"unknown roadmap revise op: {op!r}")


def apply_roadmap_apply(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.ROADMAP_APPLY` — validate apply readiness.

    ``roadmap apply`` is informational: ``roadmap propose`` already
    persists the PLANNED scope, so this op only confirms the phase is
    PLANNED with at least one wave before ``/prep`` activates it. It
    makes no structural state change beyond the ``updated_at`` bump the
    mutator stamps on every call.

    Raises:
        LifecycleError: when the phase is unknown, not PLANNED, or has no
            waves planned under it.
    """
    phase_id = str(mutation.params["phase_id"])
    phase = state.phases.get(phase_id)
    if phase is None:
        raise LifecycleError(f"unknown phase {phase_id!r}")
    if phase.status != PhaseStatus.PLANNED:
        raise LifecycleError(
            f"phase {phase_id!r} has status {phase.status.value!r}; only planned phases can apply"
        )
    iter_ids = set(phase.iter_ids)
    wave_count = sum(1 for w in state.waves.values() if w.iter_id in iter_ids)
    if wave_count == 0:
        raise LifecycleError(f"phase {phase_id!r} has no waves; revise --add-wave before apply")


def apply_event_append(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.EVENT_APPEND` — append-only audit row.

    EVENT_APPEND records an out-of-band audit event without any
    structural ``state.json`` change. The canonical event envelope the
    mutator always builds + appends to ``event.jsonl`` *is* the side
    effect, so this apply is a deliberate no-op on the :class:`State`
    (the ``updated_at`` bump the mutator stamps afterwards keeps the
    before/after digests distinct). Validating ``event_type`` here gives
    a clear rejection for a malformed append rather than a silent empty
    row.

    Raises:
        LifecycleError: when the required ``event_type`` param is missing
            or empty.
    """
    event_type = mutation.params.get("event_type")
    if not event_type or not str(event_type).strip():
        raise LifecycleError("event_append requires a non-empty 'event_type' param")


def log_guard_rejection(mutation: Mutation, exc: LifecycleGuardError) -> None:
    """Log one coded lifecycle rejection without emitting a durable event."""
    logger.warning(
        f"mutate rejected mutation_kind={mutation.kind.value} "
        f"scope_id={mutation.scope_id!r} guard_code={exc.code}"
    )


def build_apply_registry(*, apply_iter_close: ApplyFunc) -> dict[MutationKind, ApplyFunc]:
    """Return the closed ``MutationKind -> apply`` dispatch table.

    *apply_iter_close* is injected rather than imported so the ``ITER_CLOSE``
    applier can stay beside the lifecycle transition it delegates to.
    """
    return {
        MutationKind.WAVE_CLAIM: apply_wave_claim,
        MutationKind.WAVE_CLOSE: apply_wave_close,
        MutationKind.WAVE_FAIL: apply_wave_fail,
        MutationKind.WAVE_RELEASE: apply_wave_release,
        MutationKind.PHASE_OPEN: apply_phase_open,
        MutationKind.PHASE_ACTIVATE: apply_phase_activate,
        MutationKind.PHASE_CLOSE: apply_phase_close,
        MutationKind.ITER_OPEN: apply_iter_open,
        MutationKind.ITER_CLOSE: apply_iter_close,
        MutationKind.TRACK_ADD: apply_track_add,
        MutationKind.TRACK_SWITCH: apply_track_switch,
        MutationKind.EVENT_APPEND: apply_event_append,
        MutationKind.ROADMAP_REVISE: apply_roadmap_revise,
        MutationKind.ROADMAP_APPLY: apply_roadmap_apply,
        MutationKind.ROADMAP_DROP: apply_phase_archive,
        MutationKind.MEMORY_ADD: apply_memory_add,
        MutationKind.MEMORY_UPDATE: apply_memory_update,
        MutationKind.MEMORY_SUPERSEDE: apply_memory_supersede,
        MutationKind.MEMORY_PRUNE: apply_memory_prune,
        MutationKind.MEMORY_REVIEW: apply_memory_review,
        MutationKind.DECISION_OBSOLETE: apply_decision_obsolete,
    }


def apply_mutation_under_lock(
    state: State,
    mutation: Mutation,
    *,
    apply_func: ApplyFunc,
    state_path: Path,
    repo_root_override: str | None,
) -> None:
    """Run one mutation's apply step and map its rejection onto the wire taxonomy.

    Runs with the state lock held and the freshly-read *state*, so the policy
    leaves threaded into the mutation reflect the same config the apply is
    about.

    Raises:
        DaemonValidationError: On a coded guard rejection, a closure-kind
            lifecycle rejection, a model-bound rejection, or a missing param.
        ValueError: On every other lifecycle / memory / decision rejection, so
            the exit code matches the in-process fallback taxonomy.
    """
    if mutation.kind is MutationKind.ITER_CLOSE:
        thread_iter_close_verify_params(
            state,
            mutation,
            state_path=state_path,
            repo_root_override=repo_root_override,
        )

    try:
        if mutation.kind in {
            MutationKind.WAVE_CLAIM,
            MutationKind.ROADMAP_REVISE,
        }:
            thread_mutation_waiver_mode(
                state,
                mutation,
                state_path=state_path,
                repo_root_override=repo_root_override,
            )
        if mutation.kind is MutationKind.WAVE_CLAIM:
            repo_anchor = (
                Path(repo_root_override)
                if repo_root_override
                else config_root_for_state_path(state_path)
            )
            apply_wave_claim(
                state,
                mutation,
                max_parallel_waves=resolve_max_parallel_waves(repo_anchor),
            )
        else:
            apply_func(state, mutation)
    except LifecycleGuardError as exc:
        log_guard_rejection(mutation, exc)
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    except LifecycleError as exc:
        # Closure-kind (*_CLOSE) rejections surface as -32002
        # (ValidationError, exit 2); every other lifecycle-guard
        # rejection surfaces as a plain ValueError -> -32602
        # (UserError kind="InvalidInput", exit 1). This mirrors the
        # in-process fallback taxonomy so the daemon path and the
        # daemon-down fallback agree on the exit code for the same
        # rejection: phase/iter close pass closure_kind=True to
        # ``_state_transaction`` and wave close maps -32002 in its
        # bespoke ``_wave_close_via_daemon`` proxy (both ->
        # ValidationError), while every other verb maps a lifecycle
        # rejection to UserError (kind="InvalidInput").
        if mutation.kind in (
            MutationKind.PHASE_CLOSE,
            MutationKind.ITER_CLOSE,
            MutationKind.WAVE_CLOSE,
        ):
            raise DaemonValidationError(f"validation_failed: {exc}") from exc
        raise ValueError(str(exc)) from exc
    except (MemoryMutationError, DecisionMutationError) as exc:
        # Memory/decision apply rejections (duplicate id, unknown id,
        # already-pruned/obsolete) surface the same way non-closure
        # lifecycle rejections do: plain ValueError -> -32602
        # (UserError kind="InvalidInput", exit 1). These kinds are
        # not closure kinds, so no -32002 mapping is needed.
        raise ValueError(str(exc)) from exc
    except ValidationError as exc:
        # Model-level bound rejections (e.g. the ≤500-char Wave /
        # Iter / Phase description cap) trip on Pydantic before
        # any lifecycle guard fires. Surface them as
        # ``validation_failed`` so the wire-error matches the
        # post-mutation schema rejection at line ~847 and the
        # CLI exit code stays consistent.
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    except KeyError as exc:
        raise DaemonValidationError(f"validation_failed: missing param {exc!s}") from exc
