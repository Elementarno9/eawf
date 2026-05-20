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
   :func:`eawf.state.writer.atomic_write_json_locked`).
8. Append the envelope to ``event.jsonl`` via
   :func:`eawf.store.append.append_envelope`.
9. WAL ``.pending`` → ``.applied`` → ``.fsynced`` (lock-free renames
   from :mod:`eawf.daemon.wal`).
10. Publish the envelope on the subscription bus
    (:meth:`eawf.daemon.bus.EventBus.publish`).
11. Release portalock; cache the result for the idempotency window;
    return ``{event, before_version, after_version}``.

The per-kind apply registry is loose-typed (the
:attr:`Mutation.params` dict is the contract). A later wave hardens each
variant into a Pydantic subclass per MutationKind.

The current apply table covers ``wave_close`` end-to-end (the canary
callsite); other lifecycle kinds dispatch to existing
:mod:`eawf.lifecycle.transitions` functions. Not-yet-wired kinds raise
:class:`NotImplementedError` so the CLI falls back to the daemonless
``state_transaction`` path.
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

from eawf.daemon import wal
from eawf.daemon.methods import MethodContext, register
from eawf.daemon.wal import WalRecord
from eawf.lifecycle.transitions import (
    LifecycleError,
    activate_phase,
    claim_wave,
    close_iter,
    close_phase,
    close_wave,
    fail_wave,
    open_iter,
    open_phase,
)
from eawf.state.enums import StoreKind
from eawf.state.models import State
from eawf.state.mutations import Mutation, MutationKind
from eawf.state.writer import atomic_write_json_locked
from eawf.store.append import append_envelope
from eawf.store.envelope import Envelope
from eawf.store.kinds.event import EventPayload
from eawf.store.paths import store_path
from eawf.validate.strict import validate_state

logger = logging.getLogger(__name__)


#: Module-level one-shot flag for the back-compat warning emitted when a
#: caller omits the ``repo_root`` param. Flipped True on the first emit;
#: never reset for the lifetime of the daemon process. The companion
#: helper :func:`_resolve_anchor` reads + writes this directly.
_ANCHOR_FALLBACK_WARN_EMITTED: bool = False


#: JSON-RPC error code raised when the post-mutation state fails
#: validation (or when the mutation body itself is rejected by the
#: lifecycle guard). This code is reserved for ``validation_failed``.
VALIDATION_FAILED: Final[int] = -32002

#: TTL for cached idempotency results (seconds). A repeat
#: ``state.mutate`` with the same ``idempotency_key`` inside this
#: window replays the cached envelope verbatim. Outside the window
#: the daemon treats the call as new (the WAL record carries the
#: durable replay guarantee).
IDEMPOTENCY_TTL_SECONDS: Final[float] = 60.0


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
    :class:`eawf.state.models.State` if they need a typed object.
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
    set up by :mod:`eawf.daemon.main`; legacy contexts (unit tests,
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
    :data:`eawf.config.layered._LEGACY_RUNTIME_WARN_EMITTED` pattern so
    a stale CLI client does not spam the daemon log.
    """
    global _ANCHOR_FALLBACK_WARN_EMITTED
    if _ANCHOR_FALLBACK_WARN_EMITTED:
        return
    logger.warning(
        f"daemon_anchor_fallback caller omitted 'repo_root' param; "
        f"resolving against boot-time state_path={ctx.state_path!r}. "
        f"Update the caller to pass repo_root explicitly — the boot-"
        f"time fallback will be removed in a future wave."
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
    :func:`eawf.store.paths.store_path` so a per-request ``repo_root``
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

    Mirrors :func:`eawf.cli.commands.lifecycle._state_version` so the
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
            The handler maps this to ``-32002 validation_failed``.
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
    SHA; the CLI side (in :mod:`eawf.cli.commands.lifecycle`) resolves
    ``--commit <ref>`` BEFORE calling the daemon so the daemon never
    has to invoke git.
    """
    params = mutation.params
    wave = close_wave(
        state,
        wave_id=str(params["wave_id"]),
        outcome=str(params["outcome"]),
    )
    commit = params.get("commit")
    if commit is not None:
        wave.commit = str(commit)


def _apply_wave_fail(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.WAVE_FAIL` — delegate to ``fail_wave``."""
    params = mutation.params
    fail_wave(
        state,
        wave_id=str(params["wave_id"]),
        reason=str(params["reason"]),
    )


def _apply_phase_open(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.PHASE_OPEN` — delegate to ``open_phase``."""
    params = mutation.params
    open_phase(
        state,
        phase_id=str(params["phase_id"]),
        title=str(params["title"]),
        scope_id=params.get("scope_id"),
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
    """Apply :attr:`MutationKind.ITER_OPEN` — delegate to ``open_iter``."""
    params = mutation.params
    open_iter(
        state,
        iter_id=str(params["iter_id"]),
        phase_id=str(params["phase_id"]),
        title=str(params["title"]),
    )


def _apply_iter_close(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.ITER_CLOSE` — delegate to ``close_iter``."""
    params = mutation.params
    close_iter(
        state,
        iter_id=str(params["iter_id"]),
        audit_id=str(params["audit_id"]),
    )


def _apply_not_yet_wired(state: State, mutation: Mutation) -> None:
    """Apply stub for kinds whose lifecycle helper is not yet wired.

    Raises :class:`NotImplementedError` so the CLI wrapper detects the
    gap and falls back to the in-process ``state_transaction`` path
    (daemonless carve-out). ``WAVE_RELEASE`` falls here because the
    lifecycle helper itself is unimplemented; the roadmap- and event-
    append kinds fall here because their multi-step compositions need
    the spec catalogue before they can be wired.
    """
    raise NotImplementedError(
        f"mutation kind {mutation.kind.value!r} not yet wired in W09 MVP; "
        "falls back to daemonless in-process state_transaction"
    )


_APPLY_REGISTRY: Final[dict[MutationKind, ApplyFunc]] = {
    MutationKind.WAVE_CLAIM: _apply_wave_claim,
    MutationKind.WAVE_CLOSE: _apply_wave_close,
    MutationKind.WAVE_FAIL: _apply_wave_fail,
    MutationKind.WAVE_RELEASE: _apply_not_yet_wired,
    MutationKind.PHASE_OPEN: _apply_phase_open,
    MutationKind.PHASE_ACTIVATE: _apply_phase_activate,
    MutationKind.PHASE_CLOSE: _apply_phase_close,
    MutationKind.ITER_OPEN: _apply_iter_open,
    MutationKind.ITER_CLOSE: _apply_iter_close,
    MutationKind.EVENT_APPEND: _apply_not_yet_wired,
    MutationKind.ROADMAP_REVISE: _apply_not_yet_wired,
    MutationKind.ROADMAP_APPLY: _apply_not_yet_wired,
    MutationKind.ROADMAP_DROP: _apply_not_yet_wired,
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


def _build_event_envelope(
    *,
    mutation: Mutation,
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build the canonical ``StoreKind.EVENT`` envelope for *mutation*.

    The envelope shape mirrors :func:`eawf.cli.commands.lifecycle._append_event`
    so subscribers cannot tell whether the envelope was produced via
    the daemon or the daemonless fallback — both paths converge on
    the same on-disk row.
    """
    now = datetime.now(UTC)
    summary = f"state.mutate {mutation.kind.value} scope={mutation.scope_id}"
    payload = EventPayload(
        timestamp=now,
        event_type=f"state.mutate.{mutation.kind.value}",
        actor="daemon",
        command=f"state.mutate.{mutation.kind.value}",
        args_hash=_args_hash(mutation),
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok",
        message=summary,
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
            :func:`eawf.daemon.server._process_frame`.
    """
    args = ReadParams.model_validate(params)
    state_path = _resolve_state_path(repo_root=args.repo_root, ctx=ctx)
    _, payload = _read_state(state_path)
    version = _state_version(payload)
    return ReadResult(state=payload, version=version).model_dump(mode="json")


@register("state.digest")
async def digest(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the digest of the on-disk state.

    Used by the TUI mtime-poll fallback.

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
    return DigestResult(version=hashlib.sha256(raw).hexdigest()[:16]).model_dump(mode="json")


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
        ValueError: when the mutation body or post-mutation state fails
            validation; mapped to ``-32002 validation_failed`` by the
            server.
    """
    try:
        args = MutateParams.model_validate(params)
    except ValidationError as exc:
        # The server maps ValueError → -32602; we surface validation
        # rejections as -32002 instead because the param shape itself
        # was syntactically fine — the body failed the typed contract.
        raise ValueError(f"validation_failed: {exc}") from exc

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
    from eawf.lock import portalock

    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(state_path, timeout=5.0):
            state, payload = _read_state(state_path)
            before_version = _state_version(payload)

            try:
                apply_func(state, mutation)
            except LifecycleError as exc:
                raise ValueError(f"validation_failed: {exc}") from exc
            except KeyError as exc:
                raise ValueError(f"validation_failed: missing param {exc!s}") from exc

            state.updated_at = datetime.now(UTC)
            new_payload = state.model_dump(mode="json")
            post = validate_state(new_payload, strict_optional=False)
            if post.state is None:
                raise ValueError(
                    "validation_failed: post-mutation schema invalid: "
                    + "; ".join(post.schema_errors[:3])
                )
            if post.violations:
                violation_codes = ",".join(v.code for v in post.violations)
                raise ValueError(
                    f"validation_failed: post-mutation invariants violated: {violation_codes}"
                )
            after_version = _state_version(new_payload)

            envelope = _build_event_envelope(
                mutation=mutation,
                before_version=before_version,
                after_version=after_version,
            )

            # Outcome-WAL pending record carries the post-apply envelope
            # so startup replay (see :mod:`eawf.daemon.recovery`) can
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

            atomic_write_json_locked(state_path, new_payload)
            append_envelope(event_path, envelope)
            wal.mark_applied(wal_path, mutation.mutation_id)
            wal.mark_fsynced(wal_path, mutation.mutation_id)

            if ctx.bus is not None and hasattr(ctx.bus, "publish"):
                ctx.bus.publish(envelope)
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

    Thin wrapper around :func:`eawf.store.paths.store_path` so callers
    in :mod:`eawf.daemon.main` keep a single import surface for the
    canonical pairing.
    """
    return store_path(state_path, StoreKind.EVENT)


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "VALIDATION_FAILED",
    "_APPLY_REGISTRY",
    "event_store_path_for",
]
