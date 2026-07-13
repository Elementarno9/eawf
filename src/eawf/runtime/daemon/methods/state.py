"""``state.*`` JSON-RPC methods: read / mutate / digest.

Wires the canonical mutator path for the daemon. The ``state.mutate``
handler is the **sole canonical writer** for ``state.json`` +
``event.jsonl`` (authority-map rows 1-4); every state-mutating CLI verb
routes through this RPC once ``daemon.proxy_enabled`` flips to ``true``.

Algorithm — the transaction lifecycle:

1. Idempotency-cache lookup keyed by :attr:`Mutation.idempotency_key`.
2. ``portalock(state.json, timeout=5)`` — defense-in-depth; AGENTS
   rule 4 retains portalocker as belt-and-braces under the daemon.
3. Read + decode + validate ``state.json`` → :class:`State`.
4. Dispatch the :class:`MutationKind` to its per-kind apply function;
   on success the candidate :class:`State` carries the mutation.
5. Re-validate the post-mutation state → on failure return
   ``-32002 validation_failed`` and leave ``state.json`` untouched.
6. Build the canonical event envelope (``EventPayload`` body) +
   write the WAL ``.pending.json`` record.
7. Atomic-write ``state.json`` (existing
   :func:`eawf.kernel.state.writer.atomic_write_json_locked`) — the point of
   no return (state.json is fsynced here).
8. WAL ``.pending`` → ``.applied`` rename, BEFORE the event append, so
   a crash in the state-write→event-append window leaves an APPLIED
   record. :func:`eawf.runtime.daemon.recovery.replay_wal` re-issues the
   captured envelope for an APPLIED record (idempotent on envelope id),
   whereas a PENDING record would be POISONED and the event row lost —
   diverging state from the event log.
9. Append the envelope to ``event.jsonl`` via
   :func:`eawf.kernel.store.append.append_envelope`, then WAL
   ``.applied`` → ``.fsynced`` (lock-free renames from
   :mod:`eawf.runtime.daemon.wal`).
10. Publish the envelope on the subscription bus
    (:meth:`eawf.runtime.daemon.bus.EventBus.publish`).
11. Release portalock; cache the result for the idempotency window;
    return ``{event, before_version, after_version}``.

The per-kind apply registry is loose-typed (the
:attr:`Mutation.params` dict is the contract). A later wave hardens each
variant into a Pydantic subclass per MutationKind.

Every :class:`MutationKind` now resolves to a real apply function — the
wave / phase / iter lifecycle kinds delegate to
:mod:`eawf.workflow.lifecycle.transitions`; ``ROADMAP_REVISE`` dispatches one of
the ``plan_wave`` / ``remove_wave_plan`` / ``set_wave_deps`` /
``edit_wave_plan`` transitions on its ``params['op']`` discriminator;
``ROADMAP_APPLY`` is a readiness check; ``ROADMAP_DROP`` archives the
phase; and ``EVENT_APPEND`` is a no-op on :class:`State` whose side
effect is the canonical event row the mutator always appends.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.config.schema import EuBasis
from eawf.kernel.spec.common import (
    CriterionSpec,
    grandfather_criterion,
    validate_criterion_gate_refs,
)
from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    AgentSessionRole,
    EffortBucket,
    PhaseStatus,
    StoreKind,
    TrackKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    CriteriaFloorWaiver,
    RuntimeBaseline,
    RuntimeCarry,
    RuntimeLatest,
    SessionAttempt,
    State,
    Track,
    Wave,
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
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventKind, EventPayload
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.kernel.validate.strict import validate_state
from eawf.observability.telemetry.join import (
    DEFAULT_EU_MINUTES,
    WaveSessionRollup,
    rollup_wave_sessions,
)
from eawf.observability.telemetry.models import TelemetrySession
from eawf.runtime.daemon import wal
from eawf.runtime.daemon.methods import (
    VALIDATION_FAILED,
    DaemonValidationError,
    MethodContext,
    register,
    require_bound_state_root,
)
from eawf.runtime.daemon.wal import WalRecord
from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters
from eawf.workflow.lifecycle.transitions import (
    LifecycleError,
    activate_phase,
    add_track,
    archive_phase,
    claim_wave,
    close_iter,
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
from eawf.workflow.lifecycle.wave import RuntimeDelta, compute_runtime_delta
from eawf.workflow.skills.needs_user import retract_wave_pauses
from eawf.workflow.verify.models import CloseReadiness
from eawf.workflow.verify.preflight import run_close_preflight

if TYPE_CHECKING:
    from eawf.observability.eval.jury import JurorBallot
    from eawf.observability.eval.jury_validation import BlockAuthority
    from eawf.platform.profiles.models import VerifyBlock

logger = logging.getLogger(__name__)


#: Module-level one-shot flag for the back-compat warning emitted when a
#: caller omits the ``repo_root`` param. Flipped True on the first emit;
#: never reset for the lifetime of the daemon process. The companion
#: helper :func:`_resolve_anchor` reads + writes this directly.
_ANCHOR_FALLBACK_WARN_EMITTED: bool = False


#: TTL for cached idempotency results (seconds). A repeat
#: ``state.mutate`` with the same ``idempotency_key`` inside this
#: window replays the cached envelope verbatim. Outside the window
#: the daemon treats the call as new (the WAL record carries the
#: durable replay guarantee).
IDEMPOTENCY_TTL_SECONDS: Final[float] = 60.0

#: Active-wave elapsed updates are coarse-grained to one event per wave per
#: elapsed minute. The in-memory cache suppresses repeated digest polls inside
#: the same minute; a daemon restart may re-emit the current minute, which is
#: acceptable for a live advisory stream.
_WAVE_ELAPSED_ACTIVE_STATUSES: Final[frozenset[WaveStatus]] = frozenset(
    {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
)
_WAVE_ELAPSED_WARN_FRACTION: Final[float] = 0.8
_WAVE_ELAPSED_ERROR_FRACTION: Final[float] = 1.0
_WAVE_ELAPSED_LAST_MINUTE: dict[int, dict[str, int]] = {}


# ---- Params + Result models ------------------------------------------------


class ReadParams(BaseModel):
    """Params for :func:`read`.

    Attributes:
        scope_id: Optional scope filter (not yet enforced; returns
            the full state — projection lands in a later wave).
        fields: Optional projection list (not yet enforced — see above).
        repo_root: Optional absolute path of the repo whose ``state.json``
            the daemon should read. The CLI proxy forwards ``flags.workspace``
            (or ``Path.cwd()``) here so the daemon — which is one per user,
            not one per repo — resolves the right anchor regardless of the
            boot-time cwd. Omitting falls back to ``ctx.state_path`` with a
            one-shot ``daemon_anchor_fallback`` warning.
    """

    model_config = ConfigDict(extra="forbid")
    scope_id: str | None = None
    fields: list[str] | None = None
    repo_root: str | None = None


class ReadResult(BaseModel):
    """Result of :func:`read`.

    The ``state`` field carries the full validated state payload as a
    JSON-mode dict; callers re-validate against
    :class:`eawf.kernel.state.models.State` if they need a typed object.
    """

    model_config = ConfigDict(extra="forbid")
    state: dict[str, Any]
    version: str


class MutateParams(BaseModel):
    """Params for :func:`mutate`.

    Attributes:
        mutation: Typed :class:`Mutation` payload.
        idempotency_key: Optional caller-supplied key. When supplied,
            shadows :attr:`Mutation.idempotency_key`; precedence matches
            ``DaemonClient.call(idempotency_key=...)`` which carries the
            key as a sibling field of ``params``.
        repo_root: Optional absolute path of the repo whose ``state.json``
            the daemon should mutate. Same semantics as the field on
            :class:`ReadParams`.
    """

    model_config = ConfigDict(extra="forbid")
    mutation: Mutation
    idempotency_key: str | None = None
    repo_root: str | None = None


class MutateResult(BaseModel):
    """Result of :func:`mutate`."""

    model_config = ConfigDict(extra="forbid")
    event: dict[str, Any]
    before_version: str
    after_version: str
    idempotent_replay: bool = False


class DigestParams(BaseModel):
    """Params for :func:`digest`.

    Attributes:
        repo_root: Optional absolute path of the repo whose ``state.json``
            digest the daemon should return. Same semantics as the field
            on :class:`ReadParams`.
    """

    model_config = ConfigDict(extra="forbid")
    repo_root: str | None = None


class DigestResult(BaseModel):
    """Result of :func:`digest`."""

    model_config = ConfigDict(extra="forbid")
    version: str


class RuntimeCaptureParams(RuntimeCounters):
    """Params for :func:`runtime_capture`.

    The runtime-owned counters are cumulative, so this RPC records the latest
    observed snapshot onto every active wave; the active wave set remains
    canonical state. ``session_id`` names the session the counters were read
    from, which is load-bearing rather than decorative: counters are cumulative
    *per session*, so it is what lets a capture from a new session rebase the
    wave's baseline onto that session's origin
    (:func:`_rebase_for_session`) and what dedupes the interactive
    :class:`~eawf.kernel.state.models.SessionAttempt`
    (:func:`_upsert_interactive_session_attempt`). It stays optional: a runtime
    that discloses no session id still captures, and the wave is then treated as
    single-session.
    """

    model_config = ConfigDict(extra="forbid")

    repo_root: str | None = None
    session_id: str | None = None
    captured_at: datetime | None = None


class RuntimeCaptureResult(BaseModel):
    """Result of :func:`runtime_capture`."""

    model_config = ConfigDict(extra="forbid")

    active_wave_ids: list[str]
    active_count: int
    before_version: str
    after_version: str
    event: dict[str, Any]


class WaveLandParams(BaseModel):
    """Params for :func:`wave_land_rpc`."""

    model_config = ConfigDict(extra="forbid")
    repo_root: str
    wave_id: str
    outcome: str | None = None
    keep_worktree: bool = False


class WaveLandRpcResult(BaseModel):
    """Result of :func:`wave_land_rpc`."""

    model_config = ConfigDict(extra="forbid")
    wave: str
    commits: list[str]
    outcome: str
    closed: bool
    worktree_cleaned: bool
    merged_commit: str


class WaveLandBatchParams(BaseModel):
    """Params for :func:`wave_land_batch_rpc`."""

    model_config = ConfigDict(extra="forbid")
    repo_root: str
    iter_id: str | None = None
    ready_only: bool = False
    keep_worktree: bool = False


class WaveLandBatchRpcResult(BaseModel):
    """Result of :func:`wave_land_batch_rpc`."""

    model_config = ConfigDict(extra="forbid")
    landed: list[dict[str, Any]]
    failed_wave: str | None
    error: str | None
    skipped: list[str]


class WaveAutolandParams(BaseModel):
    """Params for :func:`wave_autoland_rpc`."""

    model_config = ConfigDict(extra="forbid")
    repo_root: str
    iter_id: str | None = None
    keep_worktree: bool = False
    dry_run: bool = False


class WaveAutolandRpcResult(BaseModel):
    """Result of :func:`wave_autoland_rpc`."""

    model_config = ConfigDict(extra="forbid")
    order: list[str]
    landed: list[dict[str, Any]]
    failed_wave: str | None
    error: str | None
    remaining: list[str]
    dry_run: bool


# ---- track.* params + result -------------------------------------------------


class TrackSyncParams(BaseModel):
    """Params for :func:`track_sync_rpc`.

    ``track_id`` names an existing Track whose measured outcome statuses are
    recomputed from their samples (the same reducer the wave-close hook fires).
    An unknown id is a no-op (the reducer returns no changes). When omitted the
    daemon syncs the Track under :attr:`CurrentPointers.track_id`.
    """

    model_config = ConfigDict(extra="forbid")
    repo_root: str | None = None
    track_id: str | None = None


class TrackSyncRpcResult(BaseModel):
    """Result of :func:`track_sync_rpc`."""

    model_config = ConfigDict(extra="forbid")
    track_id: str | None
    changed_outcome_ids: list[str]
    changed: int


# ---- Idempotency cache ------------------------------------------------------


class _CachedMutation(BaseModel):
    """One row in the daemon's in-memory idempotency cache.

    Stored verbatim under :class:`MethodContext.idempotency_cache` (a
    plain dict keyed by ``idempotency_key``). Entries older than
    :data:`IDEMPOTENCY_TTL_SECONDS` are pruned on every lookup; the
    durable replay guarantee lives in the WAL, not here.

    Attributes:
        result: The :class:`MutateResult` dict returned to the original
            caller. On replay this is returned verbatim with
            ``idempotent_replay=True`` flipped on.
        cached_at: ``time.monotonic()`` value when the entry was
            written; used for TTL eviction.
    """

    model_config = ConfigDict(extra="forbid")
    result: dict[str, Any]
    cached_at: float = Field(ge=0.0)


def _idempotency_cache(ctx: MethodContext) -> dict[str, _CachedMutation]:
    """Return the in-memory idempotency cache attached to *ctx*.

    The cache is stored on the :class:`MethodContext` dataclass field
    set up by :mod:`eawf.runtime.daemon.main`; legacy contexts (unit tests,
    daemonless paths) get a fresh dict installed lazily. The cache
    lives only for the lifetime of the daemon process — restart wipes
    it; the WAL carries the durable replay guarantee.
    """
    if isinstance(ctx.idempotency_cache, dict):
        return ctx.idempotency_cache
    fresh: dict[str, _CachedMutation] = {}
    ctx.idempotency_cache = fresh
    return fresh


def _evict_expired(cache: dict[str, _CachedMutation], *, now: float) -> None:
    """Drop entries whose age exceeds :data:`IDEMPOTENCY_TTL_SECONDS`."""
    expired = [k for k, v in cache.items() if now - v.cached_at > IDEMPOTENCY_TTL_SECONDS]
    for k in expired:
        cache.pop(k, None)


# ---- Per-request repo anchor resolution -----------------------------------


def _emit_anchor_fallback_warning(ctx: MethodContext) -> None:
    """Log the one-shot ``daemon_anchor_fallback`` deprecation warning.

    Stays a no-op after the first call for the lifetime of the daemon
    process — mirrors the
    :data:`eawf.kernel.config.layered._LEGACY_RUNTIME_WARN_EMITTED` pattern so
    a stale CLI client does not spam the daemon log.
    """
    global _ANCHOR_FALLBACK_WARN_EMITTED
    if _ANCHOR_FALLBACK_WARN_EMITTED:
        return
    logger.warning(
        f"daemon_anchor_fallback state_path={ctx.state_path!r}; "
        f"caller omitted 'repo_root' param, resolving against the boot-time "
        f"state_path — update the caller to pass repo_root explicitly "
        f"(the boot-time fallback will be removed in a future wave)"
    )
    _ANCHOR_FALLBACK_WARN_EMITTED = True


def _resolve_state_path(*, repo_root: str | None, ctx: MethodContext) -> Path:
    """Return ``<repo>/.ea/state.json`` for the caller's repo.

    The daemon process owns one per-user UDS / named pipe and serves
    many repos. Path joins against ``<repo>/.ea/...`` MUST honour the
    caller's repo root, not the daemon's boot-time cwd — otherwise a
    daemon spawned from one directory will resolve a different repo's
    ``state.json`` against its own anchor and (on a read-only-root host)
    blow up with ``[Errno 30] Read-only file system: '/.ea'``.

    Precedence:

    1. Per-request *repo_root* param (the canonical, post-W03 callsite).
    2. Boot-time ``ctx.state_path`` (legacy fallback for callers that
       have not yet been rewired). Emits a one-shot
       ``daemon_anchor_fallback`` warning per process so stale clients
       surface in the daemon log without breaking CI.

    Raises:
        RuntimeError: When *repo_root* is ``None`` AND ``ctx.state_path``
            is also unset.
    """
    if repo_root:
        return Path(repo_root) / ".ea" / "state.json"
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    _emit_anchor_fallback_warning(ctx)
    return Path(ctx.state_path)


def _resolve_mutator_paths(
    *,
    repo_root: str | None,
    ctx: MethodContext,
) -> tuple[Path, Path, Path]:
    """Return ``(state_path, event_path, wal_dir)`` for the mutator path.

    Same precedence as :func:`_resolve_state_path` for *state_path*;
    *event_path* is always derived from the resolved *state_path* via
    :func:`eawf.kernel.store.paths.store_path` so a per-request ``repo_root``
    routes the event-jsonl append to the correct repo too. *wal_dir*
    stays daemon-process-local (one WAL per daemon).

    Raises:
        RuntimeError: When the state path cannot be resolved or
            ``ctx.wal_dir`` is unset.
    """
    require_bound_state_root(ctx, repo_root=repo_root, command="state mutation")
    state_path = _resolve_state_path(repo_root=repo_root, ctx=ctx)
    if repo_root:
        event_path = store_path(state_path, StoreKind.EVENT)
    else:
        event_path = (
            Path(ctx.event_path)
            if ctx.event_path is not None
            else store_path(state_path, StoreKind.EVENT)
        )
    if not isinstance(ctx.wal_dir, Path):
        raise RuntimeError("wal_dir not configured on daemon context")
    return state_path, event_path, ctx.wal_dir


# ---- State payload helpers --------------------------------------------------


def _state_version(payload: dict[str, Any]) -> str:
    """Stable 16-hex-char digest of a state payload.

    Mirrors :func:`eawf.surfaces.cli.commands.lifecycle._state_version` so the
    before/after-version strings stay comparable across the in-process
    and daemon-proxy paths.
    """
    raw = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(raw).hexdigest()[:16]


def _read_state(state_path: Path) -> tuple[State, dict[str, Any]]:
    """Load + validate ``state.json``; return ``(typed_state, payload)``.

    Raises:
        FileNotFoundError: when *state_path* does not exist.
        ValueError: when the on-disk payload fails schema validation.
            This is on-disk corruption (not a mutation rejection), so it
            stays a bare ``ValueError`` that the server maps to
            ``-32602 invalid_params`` — distinct from the typed
            :class:`~eawf.runtime.daemon.methods.DaemonValidationError` the
            mutator raises for a *rejected* mutation (``-32002``).
    """
    if not state_path.exists():
        raise FileNotFoundError(f"state file not found: {state_path!r}")
    raw = state_path.read_bytes()
    payload = orjson.loads(raw)
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise ValueError("state schema invalid: " + "; ".join(report.schema_errors[:3]))
    return report.state, payload


def _args_hash(mutation: Mutation) -> str:
    """Stable 16-hex-char digest of the mutation params."""
    raw = orjson.dumps(mutation.params, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(raw).hexdigest()[:16]


# ---- Apply registry ---------------------------------------------------------

#: Callable that mutates *state* in place per the supplied
#: :class:`Mutation` payload. Apply functions raise
#: :class:`LifecycleError` to signal a guard rejection
#: (mapped to ``-32002 validation_failed``).
ApplyFunc = Callable[[State, Mutation], None]


def _apply_wave_claim(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.WAVE_CLAIM` — delegate to ``claim_wave``."""
    params = mutation.params
    claim_wave(
        state,
        wave_id=str(params["wave_id"]),
        session_id=str(params["session_id"]),
        out_of_order=bool(params.get("out_of_order", False)),
    )


def _apply_wave_close(
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


def _sync_wave_close_track(state: State, mutation: Mutation) -> list[str]:
    """Recompute the closing wave's Track outcome statuses in place.

    The wave-close hook half of the Track outcome reducer (P30-I11-W07): once
    ``_apply_wave_close`` has flipped the wave to CLOSED, the Track that owns the
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


def _wave_close_elapsed_eu(
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


def _wave_runtime_delta(
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


def _wave_close_rollup_config(repo_root: Path) -> tuple[str, float, EuBasis]:
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


def _load_wave_session_rollup(
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

    db_kind, eu_minutes, _eu_basis = _wave_close_rollup_config(repo_root)
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


def _config_root_for_state_path(state_path: Path) -> Path:
    """Return the root that owns ``.ea/config.yaml`` for *state_path*."""
    return state_path.parent.parent if state_path.parent.name == ".ea" else state_path.parent


def _compute_wave_close_readiness(
    state: State,
    mutation: Mutation,
    *,
    state_path: Path,
    repo_root: Path,
    defer_verdict_kinds: bool = False,
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
    verify_block = resolve_wave_verify_block(
        load_active_verify_block(
            wave_id,
            state,
            repo_root=repo_root,
            config_root=_config_root_for_state_path(state_path),
        ),
        state.waves[wave_id],
    )
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
        config_root=_config_root_for_state_path(state_path),
        deferred_criterion_ids=deferred,
    )


def _runtime_zero_close_enforces(
    state: State,
    *,
    wave_id: str,
    state_path: Path,
    repo_root: Path,
) -> bool:
    """Return whether a zero-runtime close should block instead of warn."""
    from eawf.workflow.verify.readiness import load_active_verify_block, resolve_wave_verify_block

    wave = state.waves.get(wave_id)
    if wave is None:
        return True
    verify_block = resolve_wave_verify_block(
        load_active_verify_block(
            wave_id,
            state,
            repo_root=repo_root,
            config_root=_config_root_for_state_path(state_path),
        ),
        wave,
    )
    return True if verify_block is None else verify_block.enforce


def _enforce_nonzero_runtime_close(
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
    resets = wave.runtime_carry.counter_resets if wave and wave.runtime_carry else 0
    if resets > 0:
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


def _validate_wave_close_gate_refs(state: State, mutation: Mutation) -> None:
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


def _enforce_wave_verdict_gate(wave: Wave, *, state_path: Path) -> None:
    """Raise when the wave's single fresh-auditor verdict gate blocks close.

    The daemon-side hook into the dispatch-layer verdict producer
    (P29-I04-W07). It is the DEFAULT enforcing gate and the degrade target
    when the cross-vendor jury (P29-I04-W15) is unavailable or not opted in.
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
        f"_enforce_wave_verdict_gate wave={wave.id} requirement={gate.requirement} "
        f"blocked reasons=[{reasons}]"
    )
    raise LifecycleError(
        f"wave {wave.id!r} verdict gate blocked close (requirement={gate.requirement}): {reasons}"
    )


#: The three disjoint juror runtime families the cross-vendor jury convenes
#: one auditor from each of (plugin-manifest spelling). Mirrored here so the
#: lane-availability pre-check + the per-runtime spawn factory read the same
#: source as :data:`eawf.observability.eval.cross_vendor_jury.JURY_RUNTIME_FAMILIES`.
_JURY_RUNTIME_TRIPLE: dict[str, str] = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
}


def _cross_vendor_lanes_ready(*, quorum: int) -> bool:
    """Return whether enough juror CLI binaries resolve on PATH to convene.

    A real cross-vendor jury needs at least *quorum* of the three disjoint
    vendor CLIs installed on the host; a box with only the claude CLI cannot
    cast independent cross-vendor ballots, so the close path degrades to the
    single-auditor gate rather than forcing every enforcing close to the
    operator. Reads only binary presence (:func:`shutil.which`) -- never a
    credential -- so it does not weaken the env-scrub / jail floor.

    Args:
        quorum: Minimum number of juror lanes whose CLI must resolve.

    Returns:
        ``True`` when at least *quorum* of the juror CLI binaries resolve.
    """
    import shutil

    from eawf.runtime.runtimes.selector import select_adapter

    available = 0
    for runtime in _JURY_RUNTIME_TRIPLE:
        try:
            binary = select_adapter(runtime).cli_binary
        except ValueError:
            continue
        if shutil.which(binary) is not None:
            available += 1
    ready = available >= quorum
    logger.info(f"_cross_vendor_lanes_ready available={available} quorum={quorum} ready={ready}")
    return ready


def _jury_spawn_factory(
    state: State,
    wave: Wave,
    *,
    repo_root: Path,
    timeout_seconds: float = 600.0,
    events_path: Path | None = None,
) -> Any:
    """Return the production per-runtime spawn factory for the jury convener.

    Binds, per juror runtime, that vendor's
    :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.spawn_session` with the
    runtime's OWN per-tier model (resolved via
    :func:`eawf.workflow.dispatch.routing.model_for_runtime`) + the wave's
    sandbox deny-list, so each juror spawns its own vendor's CLI behind the
    safety floor. Tests monkeypatch this factory builder to return recording
    stubs so no real subprocess runs.

    When *events_path* is supplied, each juror spawn also streams its stdout
    LIVE to the auditor's Watch roster row (W21): the spawn binds an ``on_chunk``
    callback that batches output off the count / wall-clock budget
    (:func:`~eawf.runtime.daemon.dispatch_runner._chunk_should_flush`, W19) and
    persists each batch bus-less to the auditor session scope
    (:func:`~eawf.workflow.dispatch.verdict._auditor_scope_id`) via
    :func:`~eawf.runtime.daemon.dispatch_runner.persist_agent_output_chunk`. The
    close gate severs the :class:`MethodContext` (only paths cross into
    ``run_oracle``), so the store-poll tail -- not the bus -- surfaces the chunk;
    a call site that threads no *events_path* (the spec-jury builder) spawns
    unchanged, with no live tail.

    Args:
        state: Validated state -- read for the wave's sandbox deny-list + role.
        wave: The wave under audit (supplies role + effort for model routing).
        repo_root: Repository root the juror spawns run in.
        timeout_seconds: Per-juror spawn wall-clock ceiling.
        events_path: Optional ``event.jsonl`` path -- when set, juror stdout
            streams live to the auditor's Watch row; when ``None``, no live tail.

    Returns:
        A :data:`~eawf.observability.eval.cross_vendor_jury.SpawnFactory` -- a
        ``runtime -> SpawnFn`` callable.
    """
    from eawf.kernel.config.layered import resolve_runtime_tier_models
    from eawf.kernel.state.enums import AgentSessionRole as _Role
    from eawf.kernel.state.enums import EffortBucket as _Effort
    from eawf.runtime.daemon.dispatch_runner import (
        _chunk_should_flush,
        persist_agent_output_chunk,
    )
    from eawf.runtime.runtimes.adapter import SpawnResult
    from eawf.runtime.runtimes.selector import select_adapter
    from eawf.runtime.sandbox.policy import resolve_denied_tools
    from eawf.workflow.dispatch.llm_assist import SpawnFn
    from eawf.workflow.dispatch.routing import model_for_runtime
    from eawf.workflow.dispatch.verdict import _auditor_scope_id

    role = wave.agent_role if wave.agent_role is not None else _Role.AUDITOR
    effort = wave.effort_bucket if wave.effort_bucket is not None else _Effort.M
    denied = sorted(resolve_denied_tools(state.sandbox_policies, wave_id=wave.id))
    cwd = str(repo_root)
    runtime_models = resolve_runtime_tier_models(repo_root)
    chunk_scope = _auditor_scope_id(wave.id)

    def _factory(runtime: str) -> SpawnFn:
        triple = _JURY_RUNTIME_TRIPLE.get(runtime, "claude")
        model = model_for_runtime(role, effort, triple, runtime_models=runtime_models)
        adapter = select_adapter(runtime)

        async def _spawn(prompt: str) -> SpawnResult:
            if events_path is None:
                return await adapter.spawn_session(
                    prompt,
                    model=model,
                    cwd=cwd,
                    denied_tools=denied,
                    timeout=timeout_seconds,
                )
            # Live juror-stdout tail (W21): batch chunks off the W19 count /
            # time budget and persist each batch bus-less to the auditor session
            # scope so the Watch store-poll tail renders the juror's own words.
            chunk_buffer: list[str] = []
            chunk_seq = [0]
            last_chunk_flush = [time.monotonic()]

            def _flush_chunk_buffer() -> None:
                if not chunk_buffer:
                    return
                persist_agent_output_chunk(
                    events_path,
                    scope_id=chunk_scope,
                    session_id=None,
                    seq=chunk_seq[0],
                    text="".join(chunk_buffer),
                )
                chunk_seq[0] += 1
                chunk_buffer.clear()
                last_chunk_flush[0] = time.monotonic()

            async def _on_chunk(line: str) -> None:
                chunk_buffer.append(line)
                if _chunk_should_flush(
                    buffered=len(chunk_buffer),
                    elapsed_s=time.monotonic() - last_chunk_flush[0],
                ):
                    _flush_chunk_buffer()

            try:
                return await adapter.spawn_session(
                    prompt,
                    model=model,
                    cwd=cwd,
                    denied_tools=denied,
                    timeout=timeout_seconds,
                    on_chunk=_on_chunk,
                )
            finally:
                _flush_chunk_buffer()

        return _spawn

    return _factory


def _load_wave_spec(wave_id: str, *, repo_root: Path) -> Any:
    """Return the on-disk :class:`WaveSpec` for *wave_id*, or ``None``.

    Resolves ``.ea/specs/<phase>/<iter>/<wave>.md``
    (:func:`eawf.kernel.spec.writer.spec_file_path`) and validates its YAML
    frontmatter through :class:`~eawf.kernel.spec.wave.WaveSpec`. Returns
    ``None`` on any miss -- file absent, frontmatter unparseable, or schema
    invalid -- so a banded close with an authoring gap degrades to a
    safe-skip rather than raising out of the close path. The spec-jury
    producer treats a ``None`` spec as nothing to score.

    Args:
        wave_id: The canonical ``P##-I##-W##`` wave id.
        repo_root: Repository root the ``.ea/specs`` tree lives under.

    Returns:
        The validated :class:`~eawf.kernel.spec.wave.WaveSpec`, or ``None``.
    """
    from eawf.kernel.spec.wave import WaveSpec
    from eawf.kernel.spec.writer import spec_file_path
    from eawf.workflow.audit_dsl.kinds.verify_implements import _parse_frontmatter

    spec_path = spec_file_path(wave_id, repo_root=repo_root)
    if not spec_path.exists():
        logger.debug(f"_load_wave_spec wave={wave_id} status=skip reason=no-spec-file")
        return None
    try:
        frontmatter = _parse_frontmatter(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug(f"_load_wave_spec wave={wave_id} status=skip err={exc!s}")
        return None
    if frontmatter is None or frontmatter.get("kind") != "WaveSpec":
        return None
    try:
        return WaveSpec.model_validate(frontmatter)
    except ValidationError as exc:
        logger.warning(f"_load_wave_spec wave={wave_id} status=invalid errors={exc.error_count()}")
        return None


def _spec_jury_ballot_fn(state: State, wave: Wave, *, repo_root: Path) -> Any:
    """Return the live per-item ballot fn for the spec jury, or ``None``.

    The TRUST-5 live binding: it reuses the cross-vendor jury's per-runtime
    spawn factory (:func:`_jury_spawn_factory`) and the wave's on-disk rubric
    (:func:`_load_wave_spec` -> :func:`eawf.kernel.spec.rubric.rubric_items`)
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
    spec = _load_wave_spec(wave.id, repo_root=repo_root)
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

    spec = _load_wave_spec(wave.id, repo_root=repo_root)
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
) -> None:
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
    """
    from eawf.workflow.dispatch.verdict import (
        produce_wave_verdict,
        verify_wave_verdict_gate,
    )

    if verify_wave_verdict_gate(wave, state_path=state_path).passed:
        logger.debug(f"_produce_high_risk_verdict wave={wave.id} status=already-ready")
        return
    events_path = store_path(state_path, StoreKind.EVENT)
    # Thread events_path so the single fresh-auditor spawn streams its stdout
    # live to the auditor's Watch roster row (W21).
    spawn = _jury_spawn_factory(
        state,
        wave,
        repo_root=repo_root,
        timeout_seconds=wall_clock_seconds,
        events_path=events_path,
    )("claude-code")

    def _persist_live_auditor_session(registered: State) -> None:
        """Write the freshly-registered auditor session so Watch can see it.

        The close persists state only when it FINISHES. An audit runs for
        minutes and can fail, so without this the operator's Watch roster --
        which reads state -- showed no running agent for the whole audit, and
        none afterwards when the close failed, while the event Feed streamed the
        auditor's output the entire time. The close already holds the state lock,
        so the write goes through the lock-free ``_locked`` primitive.
        """
        atomic_write_json_locked(state_path, registered.model_dump(mode="json"))
        logger.info(f"_produce_high_risk_verdict wave={wave.id} status=auditor-session-persisted")

    await produce_wave_verdict(
        state=state,
        state_path=state_path,
        events_path=events_path,
        wave=wave,
        spawn=spawn,
        repo_root=repo_root,
        on_session_registered=_persist_live_auditor_session,
    )
    logger.info(f"_produce_high_risk_verdict wave={wave.id} status=produced")


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


def _resolve_jury_block_authority(
    state: State,
    *,
    state_path: Path,
    verify_block: VerifyBlock | None,
) -> BlockAuthority:
    """Compute the jury's earned block authority for the close gate -- pure read.

    The TRUST-4 staged gate: a cross-vendor jury earns the right to BLOCK a
    close (rather than merely log an advisory veto) only once it has cleared its
    trust floors on eawf's own distribution. This helper scores the jury against
    the ground-truth validation substrate and returns the resulting
    :class:`~eawf.observability.eval.jury_validation.BlockAuthority`:

    - it builds the validation cohort
      (:func:`~eawf.observability.eval.jury_validation.build_jury_validation_cohort`)
      and the verbosity-bias probe over the persisted substrate;
    - it scores the validation report
      (:func:`~eawf.observability.eval.jury_validation.validate_jury`) and the
      verbosity report
      (:func:`~eawf.observability.eval.jury_validation.measure_verbosity_bias`);
    - it maps the profile's ``verify.jury_authority`` leaf onto the eval-module
      :class:`~eawf.observability.eval.jury_validation.JuryAuthorityConfig` and
      runs the earned-authority gate
      (:func:`~eawf.observability.eval.jury_validation.jury_block_authority`).

    Default-advisory by construction: the validation substrate is empty today
    (no labelled cohort, no recorded ballots), so the cohort is honest-empty,
    the validation report is :attr:`JuryValidationStatus.INSUFFICIENT`, and the
    gate returns
    :attr:`~eawf.observability.eval.jury_validation.BlockAuthority.ADVISORY` --
    an enforcing close never blocks on an uncalibrated jury.

    Args:
        state: Loaded, validated state supplying the wave tree the cohort is
            anchored against. Read-only here.
        state_path: Path to ``state.json``; the verdict + gold-label stores
            resolve under its sibling ``store/`` directory.
        verify_block: The resolved verify block (a
            :class:`~eawf.platform.profiles.models.VerifyBlock`) whose
            ``jury_authority`` leaf supplies the trust floors. ``None`` (or a
            block with the default leaf) uses the safe advisory-leaning floors.

    Returns:
        The :class:`~eawf.observability.eval.jury_validation.BlockAuthority`
        the jury has earned -- ``BLOCKING`` only when every trust floor clears,
        else ``ADVISORY``.
    """
    from eawf.observability.eval.jury_validation import (
        BlockAuthority,
        build_jury_validation_cohort,
        jury_block_authority,
        measure_verbosity_bias,
        validate_jury,
    )
    from eawf.observability.eval.jury_validation import (
        JuryAuthorityConfig as EvalJuryAuthorityConfig,
    )

    if verify_block is None:
        return BlockAuthority.ADVISORY
    leaf = verify_block.jury_authority
    authority_config = EvalJuryAuthorityConfig(
        min_labeled_waves=leaf.min_labeled_waves,
        known_bad_catch_lb_floor=leaf.known_bad_catch_lb_floor,
        unanimous_pass_ceiling=leaf.unanimous_pass_ceiling,
    )
    cohort = build_jury_validation_cohort(state, state_path)
    # An empty cohort short-circuits to advisory rather than scoring
    # (validate_jury would otherwise need the ballot substrate a later wave
    # builds); this read stays honest-empty rather than fabricating a
    # calibrated jury.
    if not cohort.silver and not cohort.gold:
        return BlockAuthority.ADVISORY
    ballots_by_wave = _load_recorded_ballots(state_path)
    # A labelled cohort with NO recorded ballots means the jury has never
    # actually run on those waves -- uncalibrated, so advisory. Scoring it
    # instead would trip validate_jury's phantom-jury hard error and crash
    # every enforcing close the moment the first auditor verdict settles into
    # the silver cohort; that hard error stays reserved for the calibration
    # path, where ballots are expected on record.
    if not ballots_by_wave:
        return BlockAuthority.ADVISORY
    report = validate_jury(cohort, ballots_by_wave=ballots_by_wave)
    verbosity = measure_verbosity_bias([])
    return jury_block_authority(report, verbosity, authority_config)


def _load_recorded_ballots(state_path: Path) -> dict[str, tuple[JurorBallot, ...]]:
    """Return the persisted per-wave juror ballots from the ballot store.

    Un-idled by P30-I23-W17: the convener now appends one ballot row per
    juror to ``jury_ballot.jsonl``, so the calibration substrate accrues
    from every convened jury. The close-path caller still resolves
    advisory authority on an empty map, so a repo with no convened jury
    keeps the honest-empty behaviour.
    """
    from eawf.observability.eval.jury_validation import read_recorded_ballots

    return read_recorded_ballots(state_path)


async def _enforce_wave_close_gate(
    state: State,
    mutation: Mutation,
    *,
    state_path: Path,
    repo_root: Path,
    tier: str = "all",
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
    ``_apply_wave_close`` succeeds, so a wave whose close is later refused
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
        LifecycleError: When the high-risk single-auditor gate refuses close.
    """
    from eawf.kernel.store.kinds.evidence import deterministic_pass_record
    from eawf.observability.eval.jury_validation import BlockAuthority
    from eawf.workflow.dispatch.verdict import verdict_requirement
    from eawf.workflow.verify.oracle import run_oracle
    from eawf.workflow.verify.readiness import (
        _load_gate_specs,
        load_active_verify_block,
        resolve_wave_verify_block,
    )

    wave_id = str(mutation.params.get("wave_id", ""))
    if not wave_id or wave_id not in state.waves:
        return []
    wave = state.waves[wave_id]
    # Band-conditional enforcement: the merged block records the fleet
    # intent; the wave-aware resolver narrows ``enforce`` +
    # ``cross_vendor_jury`` to the UI/UX band so the gate fires for a band
    # wave and a non-band wave returns early on the advisory path.
    verify_block = resolve_wave_verify_block(
        load_active_verify_block(
            wave_id,
            state,
            repo_root=repo_root,
            config_root=_config_root_for_state_path(state_path),
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
    if verdict_requirement(wave) == "always" and not jury_replaces_auditor:
        # The whole wave is covered by the blocking single-auditor; the
        # deterministic tier defers to the verdict tier (the auditor spawn
        # is lock-scoped and W08-bounded under the D-LOCK-SPLIT ordering).
        if tier == "deterministic":
            return []
        await _produce_high_risk_verdict(
            state,
            wave,
            state_path=state_path,
            repo_root=repo_root,
            wall_clock_seconds=verify_block.juror_wall_clock_seconds,
        )
        _enforce_wave_verdict_gate(wave, state_path=state_path)
        logger.info(f"_enforce_wave_close_gate wave={wave_id} high_risk=single-auditor passed=True")
        return []
    # Whole-fleet enforce is risk-weighted: a fleet profile (no
    # ``uiux_bands``) gates only the high-risk ``"always"`` subset, so a
    # mechanical (``"sampled"`` / ``"skip"``) wave closes exactly as it does
    # under an advisory profile -- no oracle run, no block, no spawn. A
    # band-scoped profile keeps the run_oracle path below: the resolver has
    # already narrowed ``enforce`` to ``False`` for a non-band wave, so any
    # wave that reaches here under a banded block is in-band and is scored.
    if not verify_block.uiux_bands and verdict_requirement(wave) != "always":
        logger.debug(
            f"_enforce_wave_close_gate wave={wave_id} requirement=mechanical advisory=True"
        )
        return []
    # Past the enforce guard the ordered oracle scores each required
    # criterion. The events_path + spawn_factory mirror the cross-vendor
    # jury gate so the jury tier (run_oracle's last resort for an un-gated
    # criterion) convenes against the same store + per-runtime spawn map.
    events_path = store_path(state_path, StoreKind.EVENT)
    spawn_factory = _jury_spawn_factory(
        state,
        wave,
        repo_root=repo_root,
        timeout_seconds=verify_block.juror_wall_clock_seconds,
        events_path=events_path,
    )
    gate_specs = _load_gate_specs(wave_id, state)
    # The staged advisory-to-block gate (TRUST-4): block_authority (computed
    # once above) is threaded into every per-criterion run_oracle call; with
    # an empty validation substrate the jury stays advisory, so an enforcing
    # close never blocks on an uncalibrated jury.
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
    logger.info(
        f"_enforce_wave_close_gate wave={wave_id} oracle=pass "
        f"criteria={len(wave.success_criteria)} "
        f"deterministic_evidence={len(deterministic_evidence)}"
    )
    return deterministic_evidence


def _append_close_evidence(
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
            f"_append_close_evidence scope_id={record.scope_id!r} evidence_id={record.id!r} "
            f"evidence_kind={record.evidence_kind!r} status={record.status!r}"
        )


def _compute_wave_close_extras(
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
        state: In-memory state AFTER ``_apply_wave_close`` succeeds.
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
        upserted an :class:`ActualSummary` (P28-I02-W03) — the
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
            readiness = _compute_wave_close_readiness(
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
                config_root=_config_root_for_state_path(state_path),
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


def _retract_closed_wave_advisories(
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


def _apply_wave_fail(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.WAVE_FAIL` — delegate to ``fail_wave``."""
    params = mutation.params
    fail_wave(
        state,
        wave_id=str(params["wave_id"]),
        reason=str(params["reason"]),
    )


def _apply_phase_open(state: State, mutation: Mutation) -> None:
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


def _apply_phase_activate(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.PHASE_ACTIVATE` — delegate to ``activate_phase``."""
    activate_phase(state, phase_id=str(mutation.params["phase_id"]))


def _apply_phase_close(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.PHASE_CLOSE` — delegate to ``close_phase``."""
    params = mutation.params
    close_phase(
        state,
        phase_id=str(params["phase_id"]),
        audit_id=str(params["audit_id"]),
        checkpoint=params.get("checkpoint"),
    )


def _apply_iter_open(state: State, mutation: Mutation) -> None:
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


def _apply_iter_close(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.ITER_CLOSE` — delegate to ``close_iter``.

    The optional ``odr_floor`` / ``odr_blocking`` params are threaded in
    daemon-side by :func:`_thread_iter_close_verify_params` from the resolved
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
    )


def _thread_iter_close_verify_params(
    state: State,
    mutation: Mutation,
    *,
    state_path: Path,
    repo_root_override: str | None,
) -> None:
    """Thread the resolved verify block's ODR leaves into an ITER_CLOSE.

    Runs under the commit lock with the freshly read state, so the flags
    the applier consumes reflect the same config the close is about. Params
    already present on the mutation win (a caller may pin them explicitly);
    resolution failures leave the advisory defaults in place — threading is
    an enrichment, never a new failure mode for the close itself.
    """
    if "odr_floor" in mutation.params and "odr_blocking" in mutation.params:
        return
    from eawf.workflow.verify.readiness import load_active_verify_block

    iter_id = str(mutation.params.get("iter_id", ""))
    repo_root = Path(repo_root_override) if repo_root_override else state_path.parent.parent
    verify_block = load_active_verify_block(
        iter_id,
        state,
        repo_root=repo_root,
        config_root=_config_root_for_state_path(state_path),
    )
    if verify_block is None:
        return
    mutation.params.setdefault("odr_floor", verify_block.odr_floor)
    mutation.params.setdefault("odr_blocking", verify_block.odr_blocking)


def _apply_track_add(state: State, mutation: Mutation) -> None:
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


def _apply_track_switch(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.TRACK_SWITCH` — delegate to ``switch_track``."""
    switch_track(state, code=str(mutation.params["code"]))


def _apply_wave_release(state: State, mutation: Mutation) -> None:
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


def _apply_phase_archive(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.ROADMAP_DROP` — delegate to ``archive_phase``.

    ``roadmap drop`` archives a PLANNED phase (PLANNED → ARCHIVED) and
    cascades its non-terminal child iters / waves to ABANDONED.
    """
    archive_phase(state, phase_id=str(mutation.params["phase_id"]))


def _apply_roadmap_revise(state: State, mutation: Mutation) -> None:
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
            )
    else:
        raise LifecycleError(f"unknown roadmap revise op: {op!r}")


def _apply_roadmap_apply(state: State, mutation: Mutation) -> None:
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


def _apply_event_append(state: State, mutation: Mutation) -> None:
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


_APPLY_REGISTRY: Final[dict[MutationKind, ApplyFunc]] = {
    MutationKind.WAVE_CLAIM: _apply_wave_claim,
    MutationKind.WAVE_CLOSE: _apply_wave_close,
    MutationKind.WAVE_FAIL: _apply_wave_fail,
    MutationKind.WAVE_RELEASE: _apply_wave_release,
    MutationKind.PHASE_OPEN: _apply_phase_open,
    MutationKind.PHASE_ACTIVATE: _apply_phase_activate,
    MutationKind.PHASE_CLOSE: _apply_phase_close,
    MutationKind.ITER_OPEN: _apply_iter_open,
    MutationKind.ITER_CLOSE: _apply_iter_close,
    MutationKind.TRACK_ADD: _apply_track_add,
    MutationKind.TRACK_SWITCH: _apply_track_switch,
    MutationKind.EVENT_APPEND: _apply_event_append,
    MutationKind.ROADMAP_REVISE: _apply_roadmap_revise,
    MutationKind.ROADMAP_APPLY: _apply_roadmap_apply,
    MutationKind.ROADMAP_DROP: _apply_phase_archive,
    MutationKind.MEMORY_ADD: apply_memory_add,
    MutationKind.MEMORY_UPDATE: apply_memory_update,
    MutationKind.MEMORY_SUPERSEDE: apply_memory_supersede,
    MutationKind.MEMORY_PRUNE: apply_memory_prune,
    MutationKind.MEMORY_REVIEW: apply_memory_review,
    MutationKind.DECISION_OBSOLETE: apply_decision_obsolete,
}


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


# ---- Envelope construction --------------------------------------------------


#: Map :class:`MutationKind` -> closed :data:`EventKind` literal so the
#: post-mutation envelope carries a typed ``event_kind`` discriminator
#: (P28-I02-W03). Kinds not in this table land with ``event_kind=None``
#: during the v0.3-v0.5 migration window — the field is optional on
#: :class:`EventPayload` until every emitter is migrated, at which point
#: v0.5+ governance flips it to non-optional. Wave claim/close are wired
#: first because runtime subscribers use them to track active work and
#: close-time actuals.
_MUTATION_EVENT_KIND: Final[dict[MutationKind, EventKind]] = {
    MutationKind.WAVE_CLAIM: "wave_claimed",
    MutationKind.WAVE_CLOSE: "wave_closed",
}


def _bucket_drift_extras(state: State) -> dict[str, str | int | float | bool]:
    """Return bucket calibration drift extras, or empty when no drift fires."""
    from eawf.workflow.estimation.buckets import calibrate_buckets

    report = calibrate_buckets(state)
    nudged = [row for row in report.buckets if row.nudge]
    if not nudged:
        return {}
    max_drift = max(row.drift_pct or 0.0 for row in nudged)
    sample_count = sum(row.sample_count for row in report.buckets)
    return {
        "bucket_drift": True,
        "bucket_drift_count": len(nudged),
        "bucket_drift_max_pct": max_drift,
        "bucket_drift_samples": sample_count,
        "bucket_drift_buckets": ",".join(row.bucket.value for row in nudged),
    }


def _wave_elapsed_cache(ctx: MethodContext) -> dict[str, int]:
    """Return the daemon-local ``wave_id -> elapsed_minute`` publish cache."""
    return _WAVE_ELAPSED_LAST_MINUTE.setdefault(id(ctx), {})


def _wave_elapsed_budget_minutes(state: State, wave_id: str) -> float | None:
    """Return the time-burn budget for *wave_id*, preferring estimates."""
    estimates = state.estimates or {}
    estimate = estimates.get(wave_id)
    if estimate is not None and estimate.pessimistic_minutes > 0:
        return estimate.pessimistic_minutes
    wave = state.waves.get(wave_id)
    if wave is None or wave.effort_bucket is None:
        return None
    from eawf.workflow.estimation.buckets import EU_MINUTES, wave_estimate_eu

    minutes = wave_estimate_eu(wave) * EU_MINUTES
    return minutes if minutes > 0 else None


def _wave_elapsed_band(elapsed_minutes: float, budget_minutes: float | None) -> str:
    """Classify elapsed time against the 80% warning / 100% error bands."""
    if budget_minutes is None or budget_minutes <= 0:
        return "ok"
    fraction = elapsed_minutes / budget_minutes
    if fraction >= _WAVE_ELAPSED_ERROR_FRACTION:
        return "err"
    if fraction >= _WAVE_ELAPSED_WARN_FRACTION:
        return "warn"
    return "ok"


def _build_wave_elapsed_envelope(
    *,
    wave_id: str,
    elapsed_minute: int,
    elapsed_minutes: float,
    budget_minutes: float | None,
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build one ``wave_elapsed_update`` event envelope."""
    now = datetime.now(UTC)
    band = _wave_elapsed_band(elapsed_minutes, budget_minutes)
    status = "error" if band == "err" else band
    ratio = elapsed_minutes / budget_minutes if budget_minutes else 0.0
    args_raw = f"{wave_id}:{elapsed_minute}".encode()
    extras: dict[str, str | int | float | bool] = {
        "wave_id": wave_id,
        "elapsed_minute": elapsed_minute,
        "elapsed_minutes": round(elapsed_minutes, 4),
        "elapsed_band": band,
    }
    if budget_minutes is not None:
        extras["elapsed_budget_minutes"] = round(budget_minutes, 4)
        extras["elapsed_ratio"] = round(ratio, 4)
        extras["elapsed_percent"] = round(ratio * 100.0, 2)
    summary = f"wave_elapsed_update wave={wave_id} minute={elapsed_minute}"
    payload = EventPayload(
        timestamp=now,
        event_type="wave_elapsed_update",
        event_kind="wave_elapsed_update",
        actor="daemon",
        command="state.digest.wave_elapsed_update",
        args_hash=hashlib.sha256(args_raw).hexdigest()[:16],
        before_state_version=before_version,
        after_state_version=after_version,
        status=status,
        message=summary,
        extras=extras,
    ).model_dump(mode="json")
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=wave_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


def _publish_wave_elapsed_updates(
    *,
    ctx: MethodContext,
    state: State,
    event_path: Path,
    version: str,
    now: datetime,
) -> None:
    """Append + publish at most one elapsed update per active wave minute.

    Anchors on ``claimed_at`` (work-start), not ``opened_at``
    (plan/creation): a wave planned long before it is claimed must not
    publish an inflated elapsed clock. A wave without a ``claimed_at``
    (no work-start fact) is skipped, so no elapsed update fires for it.
    """
    cache = _wave_elapsed_cache(ctx)
    for wave in state.waves.values():
        if wave.status not in _WAVE_ELAPSED_ACTIVE_STATUSES or wave.claimed_at is None:
            continue
        elapsed_seconds = (now - wave.claimed_at).total_seconds()
        if elapsed_seconds < 60.0:
            continue
        elapsed_minute = int(elapsed_seconds // 60)
        if cache.get(wave.id) == elapsed_minute:
            continue
        cache[wave.id] = elapsed_minute
        elapsed_minutes = elapsed_seconds / 60.0
        envelope = _build_wave_elapsed_envelope(
            wave_id=wave.id,
            elapsed_minute=elapsed_minute,
            elapsed_minutes=elapsed_minutes,
            budget_minutes=_wave_elapsed_budget_minutes(state, wave.id),
            before_version=version,
            after_version=version,
        )
        append_envelope(event_path, envelope)
        if ctx.bus is not None and hasattr(ctx.bus, "publish"):
            ctx.bus.publish(envelope)
        ctx.last_event_id = envelope.id
        logger.info(f"wave_elapsed_update wave={wave.id!r} minute={elapsed_minute}")


def _build_event_envelope(
    *,
    mutation: Mutation,
    before_version: str,
    after_version: str,
    extras: dict[str, str | int | float | bool] | None = None,
) -> Envelope:
    """Build the canonical ``StoreKind.EVENT`` envelope for *mutation*.

    The envelope shape mirrors :func:`eawf.surfaces.cli.commands.lifecycle._append_event`
    so subscribers cannot tell whether the envelope was produced via
    the daemon or the daemonless fallback — both paths converge on
    the same on-disk row.

    Args:
        mutation: The mutation just applied; supplies ``kind`` /
            ``scope_id`` / params hash.
        before_version: State digest before the apply.
        after_version: State digest after the apply.
        extras: Optional rolled-up advisory metrics to surface on the
            envelope's :attr:`EventPayload.extras` map. Today the
            ``WAVE_CLOSE`` path populates this (W06
            ``readiness_warnings_count`` + the P28-I02-W03
            ``actual_tokens`` / ``actual_cost_usd`` rollup from the
            close-time ActualSummary); future verify-spine waves may
            extend the set (compile-gate fail count, waiver count,
            etc.). Additive — existing subscribers ignore unknown
            extras.
    """
    now = datetime.now(UTC)
    summary = f"state.mutate {mutation.kind.value} scope={mutation.scope_id}"
    payload = EventPayload(
        timestamp=now,
        event_type=f"state.mutate.{mutation.kind.value}",
        event_kind=_MUTATION_EVENT_KIND.get(mutation.kind),
        actor="daemon",
        command=f"state.mutate.{mutation.kind.value}",
        args_hash=_args_hash(mutation),
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok",
        message=summary,
        extras=dict(extras) if extras else {},
    ).model_dump(mode="json")
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=mutation.scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


def _build_bucket_drift_envelope(
    *,
    mutation: Mutation,
    before_version: str,
    after_version: str,
    extras: dict[str, str | int | float | bool],
) -> Envelope:
    """Build the ``bucket_drift_detected`` event envelope."""
    now = datetime.now(UTC)
    summary = f"bucket_drift_detected scope={mutation.scope_id}"
    payload = EventPayload(
        timestamp=now,
        event_type="bucket_drift_detected",
        event_kind="bucket_drift_detected",
        actor="daemon",
        command=f"state.mutate.{mutation.kind.value}",
        args_hash=_args_hash(mutation),
        before_state_version=before_version,
        after_state_version=after_version,
        status="warn",
        message=summary,
        extras=extras,
    ).model_dump(mode="json")
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=mutation.scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


def _wave_land_payload(result: Any) -> dict[str, Any]:
    """Return the JSON-mode result shape for one wave-land result."""
    return WaveLandRpcResult(
        wave=result.wave_id,
        commits=list(result.commits),
        outcome=result.outcome,
        closed=result.closed,
        worktree_cleaned=result.worktree_cleaned,
        merged_commit=result.merged_commit,
    ).model_dump(mode="json")


def _wave_land_batch_payload(result: Any) -> dict[str, Any]:
    """Return the JSON-mode result shape for a wave-land-batch result."""
    return WaveLandBatchRpcResult(
        landed=[_wave_land_payload(row) for row in result.landed],
        failed_wave=result.failed_wave,
        error=result.error,
        skipped=list(result.skipped),
    ).model_dump(mode="json")


def _wave_autoland_row_payload(row: Any) -> dict[str, Any]:
    """Return the JSON-mode result shape for one autoland row."""
    return {
        "wave": row.wave_id,
        "commits": list(row.commits),
        "merged_commit": row.merged_commit,
        "worktree_cleaned": row.worktree_cleaned,
    }


def _wave_autoland_payload(result: Any) -> dict[str, Any]:
    """Return the JSON-mode result shape for a wave-autoland result."""
    return WaveAutolandRpcResult(
        order=list(result.order),
        landed=[_wave_autoland_row_payload(row) for row in result.landed],
        failed_wave=result.failed_wave,
        error=result.error,
        remaining=list(result.remaining),
        dry_run=result.dry_run,
    ).model_dump(mode="json")


def _build_worktree_event_envelope(
    *,
    command: str,
    scope_id: str | None,
    params: dict[str, Any],
    result: dict[str, Any],
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build the canonical event row for daemon-owned worktree mutations."""
    now = datetime.now(UTC)
    summary = f"{command} scope={scope_id}"
    args_raw = orjson.dumps(params, option=orjson.OPT_SORT_KEYS)
    extras: dict[str, str | int | float | bool] = {}
    if command == "state.wave_land":
        extras = {
            "closed": bool(result.get("closed", False)),
            "worktree_cleaned": bool(result.get("worktree_cleaned", False)),
            "commit_count": len(result.get("commits", [])),
        }
    elif command == "state.wave_land_batch":
        extras = {
            "landed_count": len(result.get("landed", [])),
            "failed": result.get("failed_wave") is not None,
            "skipped_count": len(result.get("skipped", [])),
        }
    elif command == "state.wave_autoland":
        extras = {
            "landed_count": len(result.get("landed", [])),
            "failed": result.get("failed_wave") is not None,
            "remaining_count": len(result.get("remaining", [])),
            "dry_run": bool(result.get("dry_run", False)),
        }
    payload = EventPayload(
        timestamp=now,
        event_type=command,
        event_kind=None,
        actor="daemon",
        command=command,
        args_hash=hashlib.sha256(args_raw).hexdigest()[:16],
        before_state_version=before_version,
        after_state_version=after_version,
        status="warn" if result.get("failed_wave") is not None else "ok",
        message=summary,
        extras=extras,
    ).model_dump(mode="json")
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


def _build_runtime_capture_event_envelope(
    *,
    active_wave_ids: list[str],
    params: dict[str, Any],
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build the event row emitted after a runtime.capture write."""
    now = datetime.now(UTC)
    scope_id = ",".join(active_wave_ids)
    args_raw = orjson.dumps(params, option=orjson.OPT_SORT_KEYS)
    extras: dict[str, str | int | float | bool] = {
        "active_count": len(active_wave_ids),
        "active_wave_ids": scope_id,
    }
    session_id = params.get("session_id")
    if isinstance(session_id, str) and session_id:
        extras["session_id"] = session_id
    payload = EventPayload(
        timestamp=now,
        event_type="runtime.capture",
        event_kind=None,
        actor="daemon",
        command="runtime.capture",
        args_hash=hashlib.sha256(args_raw).hexdigest()[:16],
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok",
        message=f"runtime.capture active_count={len(active_wave_ids)}",
        extras=extras,
    ).model_dump(mode="json")
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=f"runtime.capture active_count={len(active_wave_ids)}",
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


def _runtime_latest_from_params(params: RuntimeCaptureParams) -> RuntimeLatest:
    """Convert capture params into the state-model runtime snapshot.

    Threads the parser-stamped ``harness`` + ``model`` attribution off the
    capture params (W19 added them to :class:`RuntimeCounters`, which
    :class:`RuntimeCaptureParams` extends) onto the persisted
    :class:`RuntimeLatest` so a recorded actual derived from this snapshot
    carries non-null attribution and becomes calibratable by harness+model.
    Both stay nullable -- a payload with no recognised model still persists.
    """
    captured_at = params.captured_at or datetime.now(UTC)
    cost_usd = float(params.cost_usd) if params.cost_usd is not None else None
    return RuntimeLatest(
        api_duration_ms=params.api_duration_ms,
        total_duration_ms=params.total_duration_ms,
        cost_usd=cost_usd,
        input_tokens=params.input_tokens,
        output_tokens=params.output_tokens,
        cache_creation_input_tokens=params.cache_creation_input_tokens,
        cache_read_input_tokens=params.cache_read_input_tokens,
        harness=params.harness,
        model=params.model,
        session_id=params.session_id,
        measure_version=params.measure_version,
        captured_at=captured_at,
    )


#: Every counter a runtime snapshot carries and a carry accumulates.
_RUNTIME_COUNTER_FIELDS: Final[tuple[str, ...]] = (
    "api_duration_ms",
    "total_duration_ms",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _fold_finished_session(
    carry: RuntimeCarry | None,
    baseline: RuntimeBaseline,
    latest: RuntimeLatest | None,
) -> RuntimeCarry:
    """Return *carry* with the finished session's total (``latest - baseline``) added.

    Counter deltas are clamped at zero: a session whose snapshots regressed (a
    reset counter source) contributes nothing rather than a negative total.
    """
    base = carry or RuntimeCarry()
    if latest is None:
        # The session ended without ever capturing, so there is nothing measured
        # to fold. Counting it as "folded" would claim a session's runtime was
        # accounted for when in truth it was never seen.
        return base
    folded: dict[str, float | int] = {}
    for field in _RUNTIME_COUNTER_FIELDS:
        latest_value = getattr(latest, field)
        if latest_value is None:
            continue
        baseline_value = getattr(baseline, field) or 0
        folded[field] = getattr(base, field) + max(0, latest_value - baseline_value)
    folded["sessions_folded"] = base.sessions_folded + 1
    return base.model_copy(update=folded)


def _counters_incomparable(baseline: RuntimeBaseline, incoming: RuntimeLatest) -> bool:
    """Return whether *incoming* cannot be differenced against *baseline*.

    Two ways that happens, and the second is the one that bites quietly:

    * **The counters went backwards.** Cumulative counters only grow, so a drop
      means the source changed under the wave -- a truncated transcript, a reset.
    * **The measure changed.** When the definition of the counter changes, the
      difference between two snapshots is not work, it is the redefinition. This
      is NOT detectable from the direction the number moved: a redefinition that
      lowers the figure looks like a regression and gets caught, but one that
      RAISES it looks exactly like a productive week and gets banked as runtime.
      Both happened inside P30-I25 -- the first redefinition stranded two claimed
      waves, the very next inflated three of them by thirteen hours apiece -- which
      is why the snapshots carry ``measure_version`` and this check reads it rather
      than inferring from the numbers.
    """
    base_version = baseline.measure_version
    new_version = incoming.measure_version
    if base_version is not None and new_version is not None and base_version != new_version:
        return True
    for field in _RUNTIME_COUNTER_FIELDS:
        base_value = getattr(baseline, field)
        new_value = getattr(incoming, field)
        if base_value is not None and new_value is not None and new_value < base_value:
            return True
    return False


def _reorigin_on_reset(wave: Wave, incoming: RuntimeLatest) -> None:
    """Re-origin the baseline on incomparable counters so the wave stays measurable.

    The alternative is what the close path used to do: raise on the backwards
    counter, which strands the wave FOREVER -- no retry can help, because the
    baseline is on disk and every future capture compares against it. The runtime
    the old basis measured cannot be recovered, so it is dropped (loudly); what
    matters is that the wave stays closable and keeps measuring forward.
    """
    baseline = wave.runtime_baseline
    if baseline is None:
        return
    logger.warning(
        f"reorigin_on_counter_reset wave={wave.id} session={incoming.session_id!r} "
        f"baseline_api_duration_ms={baseline.api_duration_ms!r} "
        f"incoming_api_duration_ms={incoming.api_duration_ms!r}; "
        "counters regressed (source reset or basis change) -- re-originating"
    )
    wave.runtime_baseline = baseline.model_copy(
        update={field: getattr(incoming, field) or 0 for field in _RUNTIME_COUNTER_FIELDS}
        | {"captured_at": datetime.now(UTC), "measure_version": incoming.measure_version}
    )
    wave.runtime_latest = None
    # Record WHY this wave's runtime is short. The measurement taken before the
    # reset cannot be re-derived, so the wave may close with less runtime than it
    # really spent -- or with none. Without the count, that close is
    # indistinguishable from a capture path that silently did nothing, and the
    # zero-runtime gate must then either refuse every reset or trust every zero.
    carry = wave.runtime_carry or RuntimeCarry()
    wave.runtime_carry = carry.model_copy(update={"counter_resets": carry.counter_resets + 1})


def _rebase_for_session(wave: Wave, incoming: RuntimeLatest, session_id: str | None) -> None:
    """Rebase the wave's runtime snapshots onto *session_id*'s counter origin.

    Runtime counters are cumulative *within* a session: session B's transcript
    starts from zero regardless of what session A already spent on the wave.
    Differencing B's counters against A's baseline is therefore meaningless --
    the delta goes backwards, and the close path clamps a backwards counter to
    zero, so the wave would close reporting no runtime at all. So on the first
    capture from a session other than the baseline's, the finished session's
    total is folded into ``wave.runtime_carry`` and the baseline is re-originated
    on the new session -- the close-time delta then sums every session's runtime.

    **The new origin is the capturing session's counters right now, not zero.**
    A zero origin is wrong in two ways, and both bite:

    * *Returning to a session double-counts it.* Sessions interleave (A -> B ->
      A). On the return to A, a zero origin makes the next delta A's ENTIRE
      cumulative -- including the work already folded into the carry when A was
      first left. The wave is then charged twice for it, without bound, once per
      alternation.
    * *It absorbs work the wave did not do.* Session B's counters cover
      everything the operator did in B, so a zero origin charges the wave for any
      unrelated work B did before the wave was resumed.

    Originating on the incoming counters costs at most the turn that just ended
    (its work lands before the first capture in the new session establishes the
    origin). That is a bounded under-count of one turn, against an unbounded
    over-count -- the safer error, and the honest one.

    A capture with no session id, or one matching the baseline's session, leaves
    the snapshots alone. A baseline predating the session stamp (schema < 1.15)
    adopts the capturing session when nothing has been captured against it yet.
    """
    baseline = wave.runtime_baseline
    if baseline is None or session_id is None:
        return
    if baseline.session_id == session_id:
        return
    if baseline.session_id is None and wave.runtime_latest is None:
        # A baseline predating the session stamp with nothing captured against it
        # yet: adopt the capturing session rather than treating the wave as
        # multi-session and folding a zero total.
        wave.runtime_baseline = baseline.model_copy(update={"session_id": session_id})
        return

    wave.runtime_carry = _fold_finished_session(
        wave.runtime_carry, baseline=baseline, latest=wave.runtime_latest
    )
    wave.runtime_baseline = RuntimeBaseline(
        api_duration_ms=incoming.api_duration_ms or 0,
        total_duration_ms=incoming.total_duration_ms or 0,
        cost_usd=incoming.cost_usd or 0.0,
        input_tokens=incoming.input_tokens or 0,
        output_tokens=incoming.output_tokens or 0,
        cache_creation_input_tokens=incoming.cache_creation_input_tokens or 0,
        cache_read_input_tokens=incoming.cache_read_input_tokens or 0,
        harness=incoming.harness or baseline.harness,
        model=incoming.model or baseline.model,
        session_id=session_id,
        measure_version=incoming.measure_version,
        captured_at=datetime.now(UTC),
    )
    wave.runtime_latest = None
    logger.info(
        f"rebase_runtime_counters wave={wave.id} session={session_id!r} "
        f"sessions_folded={wave.runtime_carry.sessions_folded}"
    )


#: Per-class token fields a runtime.capture merge must never null-clobber.
_RUNTIME_TOKEN_FIELDS: Final[tuple[str, ...]] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _merge_runtime_latest(existing: RuntimeLatest | None, incoming: RuntimeLatest) -> RuntimeLatest:
    """Merge a fresh capture over the existing snapshot without null-clobbering tokens.

    A ``runtime.capture`` payload can carry a priced cost + duration while
    omitting the per-class token counts (the ``context_window.current_usage``
    block is absent for some payloads). A blind overwrite would wipe token
    fields a prior headless snapshot populated, collapsing the close-time
    runtime-delta token tally to zero. Merge so a ``None`` incoming token field
    preserves the existing populated value; every other field takes the fresh
    capture's value.

    Args:
        existing: The wave's current ``runtime_latest`` snapshot, or ``None``.
        incoming: The freshly-parsed capture snapshot to fold in.

    Returns:
        ``incoming`` unchanged when there is no existing snapshot; otherwise a
        copy of ``incoming`` whose per-class token fields fall back to the
        existing value wherever ``incoming`` left them ``None``.
    """
    if existing is None:
        return incoming
    fallbacks = {
        field: getattr(existing, field)
        for field in _RUNTIME_TOKEN_FIELDS
        if getattr(incoming, field) is None and getattr(existing, field) is not None
    }
    if not fallbacks:
        return incoming
    return incoming.model_copy(update=fallbacks)


def _upsert_interactive_session_attempt(
    wave: Wave,
    *,
    latest: RuntimeLatest,
    session_id: str | None,
) -> None:
    """Mint (or update) the interactive-Claude ``SessionAttempt`` from a capture.

    The interactive-Claude lifecycle (claude CLI claim/close + the Stop hook)
    fires ``runtime.capture``, which stamps ``wave.runtime_latest`` -- but unlike
    a headless spawn (which stamps :attr:`SessionAttempt.cost_usd` in
    :func:`~eawf.runtime.daemon.methods.agent._persist_live_session_attempt`) it
    minted NO attempt, so an interactive wave carried cost only on the wave-level
    snapshot and never surfaced a per-attempt cost row like a headless wave does.
    This upsert records that attempt, restoring per-attempt-cost parity across
    the headless/interactive axis.

    The attempt carries the wave's **delta**, not the capture snapshot. The
    snapshot is cumulative for the whole session, so stamping it verbatim charged
    every active wave with the entire session's cost and token volume, and left
    the attempt with ``started_at == ended_at`` -- a zero-length span, which the
    wave-detail metrics tab (which derives EU from attempt spans) renders as
    ``0.00 EU`` even though the recorded actual carries real EU. The attempt
    therefore spans claim (the baseline capture) to this capture, and its cost and
    per-class tokens come from :func:`compute_runtime_delta`.

    Idempotency mirrors the headless attempt-counter handling: a repeated
    Stop-hook capture for the SAME interactive session UPDATES the existing
    attempt in place (preserving its ``attempt`` number + ``started_at``) rather
    than appending a duplicate. The dedup key is the capture ``session_id``,
    synthesised per-wave when the hook omits it so a session-less capture still
    dedupes onto a single attempt. A capture carrying no priced cost, or one with
    no baseline to difference against, is a no-op: there is nothing wave-scoped to
    surface, so the wave-level snapshot stays the only record.

    Args:
        wave: The active wave whose ``runtime_latest`` this capture stamped.
        latest: The runtime snapshot the same capture produced; the wave's delta
            against it feeds the attempt.
        session_id: The interactive Claude Code session id off the capture,
            or ``None`` when the Stop hook omitted it.
    """
    if latest.cost_usd is None:
        return
    # The snapshot is CUMULATIVE for the whole session, so stamping it verbatim
    # put the entire session's spend on every wave and left the attempt with a
    # zero-length span (started_at == ended_at), which the wave-detail metrics tab
    # renders as 0.00 EU. The wave's own delta is what belongs on its attempt row.
    delta = compute_runtime_delta(
        wave.runtime_baseline,
        latest,
        carry=wave.runtime_carry,
        eu_minutes=DEFAULT_EU_MINUTES,
    )
    if delta is None:
        return
    handle_id = session_id or f"interactive:{wave.id}"
    runtime = latest.harness or "claude-code"
    existing_no = next(
        (no for no, sess in wave.sessions.items() if sess.session_id == handle_id),
        None,
    )
    if existing_no is not None:
        attempt_no = existing_no
        started_at = wave.sessions[existing_no].started_at
        outcome = "update"
    else:
        attempt_no = (max(wave.sessions) if wave.sessions else 0) + 1
        # The attempt starts when the wave was baselined (its claim), not when the
        # capture fired, so the span is the wave's working window.
        started_at = (
            wave.runtime_baseline.captured_at
            if wave.runtime_baseline is not None
            else latest.captured_at
        )
        outcome = "mint"
    wave.sessions[attempt_no] = SessionAttempt(
        attempt=attempt_no,
        runtime=runtime,
        session_id=handle_id,
        session_log_handle=f"urn:eawf:v1:session-log:{runtime}:{handle_id}",
        started_at=started_at,
        ended_at=latest.captured_at,
        exit_status=0,
        input_tokens=delta.input_tokens,
        output_tokens=delta.output_tokens,
        cache_creation_input_tokens=delta.cache_creation_input_tokens,
        cache_read_input_tokens=delta.cache_read_input_tokens,
        cost_usd=delta.actual_cost_usd,
    )
    logger.info(
        f"_upsert_interactive_session_attempt wave={wave.id} attempt={attempt_no} "
        f"session={handle_id!r} cost_usd={delta.actual_cost_usd} "
        f"tokens={delta.actual_tokens} outcome={outcome}"
    )


def _commit_worktree_state(
    *,
    ctx: MethodContext,
    repo_root: Path | None,
    params: dict[str, Any],
    command: str,
    scope_id: str | None,
    apply_func: Callable[[State], dict[str, Any]],
) -> dict[str, Any]:
    """Run a daemon-owned mutator under canonical state persistence.

    When *repo_root* is ``None`` the mutator paths resolve via the
    boot-time ``ctx.state_path`` anchor (the legacy / in-process test
    fallback). A real *repo_root* routes the state + event writes to that
    repo, matching the per-request anchoring the worktree-land handlers use.
    """
    from eawf.runtime.lock import portalock
    from eawf.surfaces.cli import errors as cli_errors

    state_path, event_path, wal_path = _resolve_mutator_paths(
        repo_root=str(repo_root) if repo_root is not None else None,
        ctx=ctx,
    )
    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(state_path, timeout=5.0):
            state, payload = _read_state(state_path)
            before_version = _state_version(payload)
            try:
                result = apply_func(state)
            except cli_errors.ValidationError as exc:
                raise DaemonValidationError(f"validation_failed: {exc}") from exc
            except cli_errors.CliError as exc:
                raise ValueError(str(exc)) from exc

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
            after_version = _state_version(new_payload)
            envelope = _build_worktree_event_envelope(
                command=command,
                scope_id=scope_id,
                params=params,
                result=result,
                before_version=before_version,
                after_version=after_version,
            )
            record_id = uuid.uuid4().hex
            record = WalRecord(
                record_id=record_id,
                envelope=envelope,
                idempotency_key=None,
                written_at=datetime.now(UTC),
                before_state_version=before_version,
                after_state_version=after_version,
            )
            wal.write_pending(wal_path, record)
            atomic_write_json_locked(state_path, new_payload)
            wal.mark_applied(wal_path, record_id)
            append_envelope(event_path, envelope)
            wal.mark_fsynced(wal_path, record_id)
            if ctx.bus is not None and hasattr(ctx.bus, "publish"):
                ctx.bus.publish(envelope)
            ctx.last_event_id = envelope.id
            logger.info(
                f"_commit_worktree_state command={command!r} scope_id={scope_id!r} "
                f"before={before_version} "
                f"after={after_version} envelope_id={envelope.id!r}"
            )
            return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


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
    state_path = _resolve_state_path(repo_root=args.repo_root, ctx=ctx)
    _, payload = _read_state(state_path)
    version = _state_version(payload)
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
    state_path = _resolve_state_path(repo_root=args.repo_root, ctx=ctx)
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
        _publish_wave_elapsed_updates(
            ctx=ctx,
            state=state,
            event_path=event_path,
            version=version,
            now=datetime.now(UTC),
        )
    return DigestResult(version=version).model_dump(mode="json")


@register("runtime.capture")
async def runtime_capture(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Persist latest runtime counters onto every active wave.

    Args:
        ctx: Server context; state, event, and WAL paths are resolved the same
            way as ``state.mutate``.
        params: Strict :class:`RuntimeCaptureParams` payload.

    Returns:
        Dict matching :class:`RuntimeCaptureResult`.

    Raises:
        DaemonValidationError: When params fail validation, no active waves are
            registered, an active wave id is missing, or post-write state
            validation rejects the candidate payload.
    """
    try:
        args = RuntimeCaptureParams.model_validate(params)
    except ValidationError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc

    state_path, event_path, wal_path = _resolve_mutator_paths(
        repo_root=args.repo_root,
        ctx=ctx,
    )

    from eawf.runtime.lock import portalock

    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(state_path, timeout=5.0):
            state, payload = _read_state(state_path)
            before_version = _state_version(payload)
            active_wave_ids = list(state.current.active_wave_ids)
            if not active_wave_ids:
                raise DaemonValidationError(
                    "validation_failed: runtime.capture requires active waves"
                )

            latest = _runtime_latest_from_params(args)
            for wave_id in active_wave_ids:
                wave = state.waves.get(wave_id)
                if wave is None:
                    raise DaemonValidationError(
                        f"validation_failed: active wave missing: {wave_id!r}"
                    )
                # A capture from a session other than the baseline's measures a
                # fresh counter origin, so rebase (folding the finished session's
                # total into runtime_carry, and re-originating on THIS session's
                # counters) before merging this session's snapshot in.
                _rebase_for_session(wave, incoming=latest, session_id=args.session_id)
                # Same session, but the snapshot is not comparable to the baseline:
                # the counters went backwards, or the measure itself changed. Either
                # way the difference is not work, so re-origin rather than record it.
                if wave.runtime_baseline is not None and _counters_incomparable(
                    wave.runtime_baseline, latest
                ):
                    _reorigin_on_reset(wave, latest)
                wave.runtime_latest = _merge_runtime_latest(wave.runtime_latest, latest)
                # The interactive-Claude lifecycle mints no SessionAttempt on
                # its own (only the headless spawn does); record one here off the
                # same priced capture so an interactive wave surfaces per-attempt
                # cost the way a headless wave does. Idempotent per session id.
                _upsert_interactive_session_attempt(wave, latest=latest, session_id=args.session_id)
            if len(active_wave_ids) > 1:
                logger.warning(f"runtime_capture active_count={len(active_wave_ids)}")

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
            after_version = _state_version(new_payload)
            event_params = args.model_dump(mode="json", exclude={"repo_root"})
            envelope = _build_runtime_capture_event_envelope(
                active_wave_ids=active_wave_ids,
                params=event_params,
                before_version=before_version,
                after_version=after_version,
            )
            record_id = uuid.uuid4().hex
            record = WalRecord(
                record_id=record_id,
                envelope=envelope,
                idempotency_key=None,
                written_at=datetime.now(UTC),
                before_state_version=before_version,
                after_state_version=after_version,
            )
            wal.write_pending(wal_path, record)
            atomic_write_json_locked(state_path, new_payload)
            wal.mark_applied(wal_path, record_id)
            append_envelope(event_path, envelope)
            wal.mark_fsynced(wal_path, record_id)
            if ctx.bus is not None and hasattr(ctx.bus, "publish"):
                ctx.bus.publish(envelope)
            ctx.last_event_id = envelope.id
            logger.info(
                f"runtime_capture active_count={len(active_wave_ids)} "
                f"before={before_version} after={after_version} envelope_id={envelope.id!r}"
            )
            return RuntimeCaptureResult(
                active_wave_ids=active_wave_ids,
                active_count=len(active_wave_ids),
                before_version=before_version,
                after_version=after_version,
                event=envelope.model_dump(mode="json"),
            ).model_dump(mode="json")
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


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
            contract, a *closure-kind* lifecycle guard rejects the
            mutation, or the post-mutation state fails schema / invariant
            validation; mapped to ``-32002 validation_failed`` by the
            server. Non-closure lifecycle-guard rejections raise a plain
            ``ValueError`` (``-32602 INVALID_PARAMS``) so the exit code
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

    state_path, event_path, wal_path = _resolve_mutator_paths(
        repo_root=args.repo_root,
        ctx=ctx,
    )

    mutation = args.mutation
    idempotency_key = args.idempotency_key or mutation.idempotency_key
    cache = _idempotency_cache(ctx)
    now_mono = time.monotonic()
    _evict_expired(cache, now=now_mono)
    if idempotency_key is not None:
        cached = cache.get(idempotency_key)
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
            before_version = _state_version(payload)

            if mutation.kind is MutationKind.ITER_CLOSE:
                _thread_iter_close_verify_params(
                    state,
                    mutation,
                    state_path=state_path,
                    repo_root_override=args.repo_root,
                )

            try:
                apply_func(state, mutation)
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
            after_version = _state_version(new_payload)

            # W06 advisory: compute close-readiness AFTER the apply
            # succeeds and pin the rolled-up count on the envelope
            # extras. Wave-close only; non-wave mutations get an empty
            # extras dict so the envelope shape stays uniform.
            extras: dict[str, str | int | float | bool] = {}
            drift_extras: dict[str, str | int | float | bool] = {}

            envelope = _build_event_envelope(
                mutation=mutation,
                before_version=before_version,
                after_version=after_version,
                extras=extras,
            )
            drift_envelope = (
                _build_bucket_drift_envelope(
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
            if ctx.bus is not None and hasattr(ctx.bus, "publish"):
                ctx.bus.publish(envelope)
                if drift_envelope is not None:
                    ctx.bus.publish(drift_envelope)
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

            if idempotency_key is not None:
                cache[idempotency_key] = _CachedMutation(
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
    cache: dict[str, _CachedMutation],
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
       version, run the W06 :func:`run_close_preflight` bundle with the
       close gate restricted to the DETERMINISTIC tier, and take the
       rollup / runtime-delta reads.
    2. **Commit (lock, ms-scale)** — re-read state and compare versions;
       when the target wave row itself changed since pre-flight the close
       is REFUSED with a typed stale error (the caller retries, which
       re-runs pre-flight off-lock). Then the verdict / jury tier runs
       (W08-bounded spawns — the one deliberately lock-scoped long step,
       per D-LOCK-SPLIT), followed by apply, post-validate, WAL, atomic
       write, and event append.
    3. **Post-lock** — deterministic-evidence append (its own sibling
       portalock), bus publish, advisory retraction, idempotency cache.

    Args:
        ctx: Server context.
        mutation: The WAVE_CLOSE mutation.
        idempotency_key: Optional retry key (already cache-missed).
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

    repo_anchor = (
        Path(repo_root_override) if repo_root_override else _config_root_for_state_path(state_path)
    )
    wave_id = str(mutation.params.get("wave_id", ""))

    # ---- Phase 1: pre-flight, NO lock --------------------------------------
    state_pre, payload_pre = _read_state(state_path)
    preflight_version = _state_version(payload_pre)
    preflight_wave_row = (payload_pre.get("waves") or {}).get(wave_id)
    try:
        preflight = await run_close_preflight(
            state_pre,
            mutation,
            state_path=state_path,
            repo_root=repo_anchor,
            validate_gate_refs=_validate_wave_close_gate_refs,
            enforce_close_gate=partial(_enforce_wave_close_gate, tier="deterministic"),
            compute_readiness=partial(_compute_wave_close_readiness, defer_verdict_kinds=True),
        )
    except LifecycleError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    wave_close_readiness = preflight.readiness
    _, _close_eu_minutes, _close_eu_basis = _wave_close_rollup_config(repo_anchor)
    runtime_delta = _wave_runtime_delta(
        state_pre,
        mutation,
        eu_minutes=_close_eu_minutes,
        eu_basis=_close_eu_basis,
    )
    wave_close_rollup = _load_wave_session_rollup(
        state_pre,
        mutation,
        state_path=state_path,
        repo_root=repo_anchor,
    )
    # A ZERO delta must not suppress the rollup. A zero means the snapshots yielded
    # nothing (a reset re-originated them, or nothing was captured) -- it is an
    # absence of evidence, and the telemetry rollup may hold real evidence of the
    # wave's runtime. Preferring a manufactured 0.0 over a measured figure throws
    # away the better answer.
    measured_eu = runtime_delta.elapsed_eu if runtime_delta is not None else None
    wave_close_elapsed_eu = (
        measured_eu
        if measured_eu
        else _wave_close_elapsed_eu(
            wave_close_rollup,
            eu_minutes=_close_eu_minutes,
        )
    )

    # ---- Phase 2: commit under the lock (ms-scale + bounded verdict tier) --
    # The handle reset MUST ride a finally: a refused close (stale row,
    # gate refusal, post-validate reject) raises out of the with-block
    # after the handle is already released, and a dangling closed handle
    # kills the watchdog's next heartbeat (W35 review blocker).
    try:
        with portalock.acquire(state_path, timeout=5.0) as lock_handle:
            ctx.active_lock_handle = lock_handle
            state, payload = _read_state(state_path)
            before_version = _state_version(payload)
            if before_version != preflight_version:
                # The optimistic re-check: another writer moved state during
                # pre-flight. Only a change to the TARGET WAVE ROW invalidates
                # the pre-flight verdicts; unrelated rows moving is fine.
                commit_wave_row = (payload.get("waves") or {}).get(wave_id)
                if commit_wave_row != preflight_wave_row:
                    raise DaemonValidationError(
                        f"validation_failed: close_preflight_stale: wave {wave_id!r} "
                        "changed during the lock-free pre-flight; retry the close "
                        "(the retry re-runs pre-flight off-lock)"
                    )
            wave_close_evidence = list(preflight.evidence)
            actual_written_auto = bool(wave_id and wave_id not in (state.actuals or {}))
            try:
                # The verdict / jury tier is the one deliberately lock-scoped
                # long step (it mutates state in-memory and appends session
                # events; W08 bounds every spawn), per D-LOCK-SPLIT.
                wave_close_evidence.extend(
                    await _enforce_wave_close_gate(
                        state,
                        mutation,
                        state_path=state_path,
                        repo_root=repo_anchor,
                        tier="verdict",
                    )
                )
                _enforce_nonzero_runtime_close(
                    state,
                    mutation,
                    elapsed_eu=wave_close_elapsed_eu,
                    state_path=state_path,
                    repo_root=repo_anchor,
                )
                _apply_wave_close(
                    state,
                    mutation,
                    wave_session_rollup=wave_close_rollup,
                    elapsed_eu=wave_close_elapsed_eu,
                    runtime_delta=runtime_delta,
                )
                _sync_wave_close_track(state, mutation)
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
            after_version = _state_version(new_payload)

            extras = _compute_wave_close_extras(
                state,
                mutation,
                state_path=state_path,
                repo_root=repo_anchor,
                readiness=wave_close_readiness,
                actual_written_auto=actual_written_auto,
            )
            drift_extras = _bucket_drift_extras(state)
            envelope = _build_event_envelope(
                mutation=mutation,
                before_version=before_version,
                after_version=after_version,
                extras=extras,
            )
            drift_envelope = (
                _build_bucket_drift_envelope(
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
        _append_close_evidence(wave_close_evidence, state_path=state_path)
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
        if drift_envelope is not None:
            ctx.bus.publish(drift_envelope)
    _retract_closed_wave_advisories(state_path, wave_id=wave_id, bus=ctx.bus)
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
    if idempotency_key is not None:
        cache[idempotency_key] = _CachedMutation(
            result=result,
            cached_at=time.monotonic(),
        )
    return result


@register("state.wave_land")
async def wave_land_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Daemon-owned implementation of ``eawf wave land``."""
    args = WaveLandParams.model_validate(params)
    repo_root = Path(args.repo_root)

    from eawf.runtime.worktree import wave_land, worktree_registry_lock

    with worktree_registry_lock(repo_root, timeout=5.0):
        return _commit_worktree_state(
            ctx=ctx,
            repo_root=repo_root,
            params=params,
            command="state.wave_land",
            scope_id=args.wave_id,
            apply_func=lambda state: _wave_land_payload(
                wave_land(
                    state,
                    repo_root=repo_root,
                    wave_id=args.wave_id,
                    outcome=args.outcome,
                    keep_worktree=args.keep_worktree,
                )
            ),
        )


@register("state.wave_land_batch")
async def wave_land_batch_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Daemon-owned implementation of ``eawf wave land-batch``."""
    args = WaveLandBatchParams.model_validate(params)
    repo_root = Path(args.repo_root)

    from eawf.runtime.worktree import wave_land_batch, worktree_registry_lock

    with worktree_registry_lock(repo_root, timeout=5.0):
        return _commit_worktree_state(
            ctx=ctx,
            repo_root=repo_root,
            params=params,
            command="state.wave_land_batch",
            scope_id=args.iter_id,
            apply_func=lambda state: _wave_land_batch_payload(
                wave_land_batch(
                    state,
                    repo_root=repo_root,
                    iter_id=args.iter_id,
                    ready_only=args.ready_only,
                    keep_worktree=args.keep_worktree,
                )
            ),
        )


@register("state.wave_autoland")
async def wave_autoland_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Daemon-owned implementation of ``eawf wave autoland``."""
    args = WaveAutolandParams.model_validate(params)
    repo_root = Path(args.repo_root)

    from eawf.runtime.worktree import wave_autoland, worktree_registry_lock

    with worktree_registry_lock(repo_root, timeout=5.0):
        return _commit_worktree_state(
            ctx=ctx,
            repo_root=repo_root,
            params=params,
            command="state.wave_autoland",
            scope_id=args.iter_id,
            apply_func=lambda state: _wave_autoland_payload(
                wave_autoland(
                    state,
                    repo_root=repo_root,
                    iter_id=args.iter_id,
                    keep_worktree=args.keep_worktree,
                    dry_run=args.dry_run,
                )
            ),
        )


# ---- track.* mutators --------------------------------------------------------


def _tracks(state: State) -> dict[str, Track]:
    """Return ``state.tracks`` as a non-``None`` dict in place.

    Creates a fresh empty dict on the state when the field is currently
    ``None`` so a first ``track.add`` has somewhere to land.
    """
    if state.tracks is None:
        state.tracks = {}
    return state.tracks


def _apply_track_sync(state: State, args: TrackSyncParams) -> dict[str, Any]:
    """Recompute a Track's measured outcome statuses from their samples.

    Resolves the target Track (the explicit ``track_id`` param, else the
    :attr:`CurrentPointers.track_id` cursor) and runs the
    :func:`eawf.workflow.evidence.outcome.sync_track_outcomes` reducer -- the
    same reducer the wave-close hook fires -- so an operator can re-derive the
    standings on demand. An absent target Track (no id and no cursor) yields a
    typed no-op result with an empty change list rather than raising, so
    ``track sync`` on a repo with no Track in focus is harmless.

    Args:
        state: Loaded :class:`State`. Mutated in place by the reducer.
        args: Validated :class:`TrackSyncParams`.

    Returns:
        Result dict matching :class:`TrackSyncRpcResult`.
    """
    from eawf.workflow.evidence.outcome import sync_track_outcomes

    track_id = args.track_id if args.track_id else state.current.track_id
    changed = sync_track_outcomes(state, track_id=track_id) if track_id else []
    logger.info(f"_apply_track_sync track={track_id!r} changed={len(changed)}")
    return TrackSyncRpcResult(
        track_id=track_id,
        changed_outcome_ids=changed,
        changed=len(changed),
    ).model_dump(mode="json")


@register("track.sync")
async def track_sync_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Daemon-owned ``track.sync`` mutator.

    Recomputes a Track's measured outcome statuses from their samples via the
    same reducer the wave-close hook fires. The daemon is the sole canonical
    mutator (AGENTS rule 4); the CLI ``track sync`` shim routes here over
    JSON-RPC.
    """
    args = TrackSyncParams.model_validate(params)
    repo_root = Path(args.repo_root) if args.repo_root else None
    return _commit_worktree_state(
        ctx=ctx,
        repo_root=repo_root,
        params=params,
        command="track.sync",
        scope_id=args.track_id,
        apply_func=lambda state: _apply_track_sync(state, args),
    )


def event_store_path_for(state_path: Path) -> Path:
    """Return the ``event.jsonl`` path that pairs with *state_path*.

    Thin wrapper around :func:`eawf.kernel.store.paths.store_path` so callers
    in :mod:`eawf.runtime.daemon.main` keep a single import surface for the
    canonical pairing.
    """
    return store_path(state_path, StoreKind.EVENT)


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "VALIDATION_FAILED",
    "_APPLY_REGISTRY",
    "event_store_path_for",
    "runtime_capture",
    "track_sync_rpc",
    "wave_autoland_rpc",
    "wave_land_batch_rpc",
    "wave_land_rpc",
]
