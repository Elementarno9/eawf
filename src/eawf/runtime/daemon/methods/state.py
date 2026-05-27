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
from typing import Any, Final

import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    AgentSessionRole,
    EffortBucket,
    PhaseStatus,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.models import State
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
    activate_phase,
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
)
from eawf.workflow.verify.models import CloseReadiness

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


def _apply_wave_close(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.WAVE_CLOSE` — delegate to ``close_wave``.

    Optionally pins ``Wave.commit`` when the params carry a resolved
    SHA; the CLI side (in :mod:`eawf.surfaces.cli.commands.lifecycle`) resolves
    ``--commit <ref>`` BEFORE calling the daemon so the daemon never
    has to invoke git.
    """
    params = mutation.params
    tokens_raw = params.get("tokens_consumed")
    wave = close_wave(
        state,
        wave_id=str(params["wave_id"]),
        outcome=str(params["outcome"]),
        tokens_consumed=int(tokens_raw) if tokens_raw is not None else None,
    )
    commit = params.get("commit")
    if commit is not None:
        wave.commit = str(commit)


def _config_root_for_state_path(state_path: Path) -> Path:
    """Return the root that owns ``.ea/config.yaml`` for *state_path*."""
    return state_path.parent.parent if state_path.parent.name == ".ea" else state_path.parent


def _compute_wave_close_readiness(
    state: State,
    mutation: Mutation,
    *,
    state_path: Path,
    repo_root: Path,
) -> CloseReadiness | None:
    """Return the pre-close readiness view for a wave-close mutation."""
    from eawf.kernel.store.paths import store_dir as _store_dir
    from eawf.workflow.verify import compute as compute_readiness
    from eawf.workflow.verify.readiness import load_active_verify_block

    wave_id = str(mutation.params.get("wave_id", ""))
    if not wave_id or wave_id not in state.waves:
        return None
    verify_block = load_active_verify_block(
        wave_id,
        state,
        repo_root=repo_root,
        config_root=_config_root_for_state_path(state_path),
    )
    if verify_block is None or not verify_block.enforce:
        return None
    return compute_readiness(
        wave_id,
        state=state,
        store_dir=_store_dir(state_path),
        repo_root=repo_root,
        config_root=_config_root_for_state_path(state_path),
    )


def _compute_wave_close_extras(
    state: State,
    mutation: Mutation,
    *,
    state_path: Path,
    repo_root: Path,
    readiness: CloseReadiness | None = None,
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
    extras: dict[str, str | int | float | bool] = {"readiness_warnings_count": count}
    # P28-I02-W03: surface the close-time token + cost rollup on the
    # event envelope. The wave_close apply (close_wave -> upsert
    # ActualSummary) populated these from Wave.tokens_consumed; cost
    # stays 0.0 until the per-model rate table lands.
    actuals = state.actuals or {}
    actual = actuals.get(wave_id)
    if actual is not None:
        extras["actual_tokens"] = actual.actual_tokens
        extras["actual_cost_usd"] = actual.actual_cost_usd
    return extras


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
    """Apply :attr:`MutationKind.ITER_CLOSE` — delegate to ``close_iter``."""
    params = mutation.params
    close_iter(
        state,
        iter_id=str(params["iter_id"]),
        audit_id=str(params["audit_id"]),
    )


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
        LifecycleError: when ``op`` is missing or unknown, or the
            underlying wave transition rejects the edit.
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
                list(params["success_criteria"])
                if params.get("success_criteria") is not None
                else None
            ),
            agent_role=role,
            effort_bucket=bucket,
            description=description_str,
            intent=intent,
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
    """Append + publish at most one elapsed update per active wave minute."""
    cache = _wave_elapsed_cache(ctx)
    for wave in state.waves.values():
        if wave.status not in _WAVE_ELAPSED_ACTIVE_STATUSES or wave.opened_at is None:
            continue
        elapsed_seconds = (now - wave.opened_at).total_seconds()
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
                f"scope={mutation.scope_id!r} key={idempotency_key!r}"
            )
            return result

    apply_func = _resolve_apply(mutation.kind)

    # The portalock keeps the daemon's defense-in-depth guard live
    # (rule 4 V1 carve-out); concurrent CLI fallback writers serialise
    # against it just like they did pre-daemon.
    from eawf.runtime.lock import portalock

    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(state_path, timeout=5.0):
            state, payload = _read_state(state_path)
            before_version = _state_version(payload)
            wave_close_readiness: CloseReadiness | None = None
            repo_anchor = (
                Path(args.repo_root) if args.repo_root else _config_root_for_state_path(state_path)
            )

            try:
                if mutation.kind == MutationKind.WAVE_CLOSE:
                    wave_close_readiness = _compute_wave_close_readiness(
                        state,
                        mutation,
                        state_path=state_path,
                        repo_root=repo_anchor,
                    )
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
            if mutation.kind == MutationKind.WAVE_CLOSE:
                extras = _compute_wave_close_extras(
                    state,
                    mutation,
                    state_path=state_path,
                    repo_root=repo_anchor,
                    readiness=wave_close_readiness,
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

            if ctx.bus is not None and hasattr(ctx.bus, "publish"):
                ctx.bus.publish(envelope)
                if drift_envelope is not None:
                    ctx.bus.publish(drift_envelope)
            ctx.last_event_id = envelope.id

            logger.info(
                f"mutate ok mutation_kind={mutation.kind.value} scope={mutation.scope_id!r} "
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
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


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
]
