"""``spec.*`` JSON-RPC methods: init / validate / promote / archive.

Daemon-side canonical writer for spec files
(authority-map row 9 — ``.ea/specs/<phase>/[<iter>/]<wave|spec>.md``)
and the daemon-resident spec cache (authority-map row 10 — per-phase
JSON document under ``<runtime_dir>/spec-cache/``): every spec mutation
that previously went through ad-hoc shell commands now proxies through
one of four JSON-RPC methods so the daemon owns the lifecycle.

Algorithm — mirrors the ``state.mutate`` / ``registry.update``
lifecycle:

1. Idempotency-cache lookup keyed by ``params['idempotency_key']``
   (when supplied).
2. Resolve the target scope id, spec file path, and per-phase cache
   document.
3. Apply the named operation (``init`` / ``validate`` / ``promote``
   / ``archive``) under ``ctx.in_flight_mutations`` so concurrent
   spec.* calls serialise inside the daemon process.
4. Atomic-write the per-phase cache document via
   :func:`eawf.kernel.state.writer.atomic_write_json_locked`.
5. Build the canonical ``StoreKind.SPEC_UPDATED`` envelope + publish
   on the subscription bus.
6. Cache the result; return ``{operation, scope_id, spec_urn, ...,
   envelope}``.

The ``init`` operation refuses to overwrite an existing spec file —
re-init the same scope returns the cached entry untouched
(idempotent). Status transitions ride a small DAG: DRAFT → READY →
IMPLEMENTED → ARCHIVED (no skips, no backward steps). ``archive``
atomically ``git rm``'s the source file AND writes a cache entry with
``file_sha`` pre-populated so :func:`eawf spec show <urn> --from-git`
can recover the body via ``git log -- <path>``.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.spec import cache as spec_cache
from eawf.kernel.spec import writer as spec_writer
from eawf.kernel.spec.common import GateSpec
from eawf.kernel.spec.promotion import (
    SpecPromoteValidationError,
    validate_argv_gates,
)
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.methods import MethodContext, register

logger = logging.getLogger(__name__)


#: TTL for cached idempotency results (seconds).
IDEMPOTENCY_TTL_SECONDS: Final[float] = 60.0


#: Allowed forward graduations. Backward / skip transitions are rejected.
_PROMOTE_TARGETS: Final[dict[str, str]] = {
    "DRAFT": "READY",
    "READY": "IMPLEMENTED",
}


# ---- Params + Result models ------------------------------------------------


class InitParams(BaseModel):
    """Params for :func:`init`.

    Attributes:
        scope_id: ``P##`` / ``P##-I##`` / ``P##-I##-W##``.
        title: Required scaffold title written into the spec body.
        repo_code: Project code symbol used as the URN owner.
        repo_root: Optional absolute path of the repo working tree
            (default ``Path.cwd``). The CLI proxy forwards
            ``flags.workspace`` here so per-test ``tmp_path``-rooted
            repos resolve correctly.
        idempotency_key: Optional caller-supplied retry key.
        cache_dir: Optional cache root override (``EAWF_SPEC_CACHE_DIR``
            takes precedence when this is None).
    """

    model_config = ConfigDict(extra="forbid")

    scope_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    repo_code: str = Field(min_length=1)
    repo_root: str | None = None
    idempotency_key: str | None = None
    cache_dir: str | None = None


class ValidateParams(BaseModel):
    """Params for :func:`validate`."""

    model_config = ConfigDict(extra="forbid")

    scope_id: str = Field(min_length=1)
    repo_code: str = Field(min_length=1)
    repo_root: str | None = None
    cache_dir: str | None = None


class PromoteParams(BaseModel):
    """Params for :func:`promote`.

    ``target_status`` is closed to the two forward graduations the
    W03 lifecycle supports. ``ARCHIVED`` goes through :func:`archive`
    so the ``git rm`` step is explicit on the wire.
    """

    model_config = ConfigDict(extra="forbid")

    scope_id: str = Field(min_length=1)
    repo_code: str = Field(min_length=1)
    target_status: Literal["READY", "IMPLEMENTED"]
    repo_root: str | None = None
    idempotency_key: str | None = None
    cache_dir: str | None = None


class ArchiveParams(BaseModel):
    """Params for :func:`archive`."""

    model_config = ConfigDict(extra="forbid")

    scope_id: str = Field(min_length=1)
    repo_code: str = Field(min_length=1)
    repo_root: str | None = None
    idempotency_key: str | None = None
    cache_dir: str | None = None


class SpecResult(BaseModel):
    """Common result shape for the four spec.* RPCs."""

    model_config = ConfigDict(extra="forbid")

    operation: str
    scope_id: str
    spec_urn: str
    status: str
    file_path: str
    file_sha: str
    envelope: dict[str, Any]
    idempotent_replay: bool = False


# ---- Idempotency cache ----------------------------------------------------


class _CachedSpecMutation(BaseModel):
    """One row in the daemon's spec idempotency cache."""

    model_config = ConfigDict(extra="forbid")
    result: dict[str, Any]
    cached_at: float = Field(ge=0.0)


def _idempotency_cache(ctx: MethodContext) -> dict[str, _CachedSpecMutation]:
    """Return the per-process spec idempotency cache.

    Shares :attr:`MethodContext.idempotency_cache` with the state /
    config / registry mutators — one dict per daemon process; the
    namespace separator is the idempotency key shape, not the
    handler.
    """
    if isinstance(ctx.idempotency_cache, dict):
        return ctx.idempotency_cache
    fresh: dict[str, _CachedSpecMutation] = {}
    ctx.idempotency_cache = fresh
    return fresh


def _evict_expired(cache: dict[str, Any], *, now: float) -> None:
    """Drop entries older than :data:`IDEMPOTENCY_TTL_SECONDS`."""
    expired = [
        k
        for k, v in cache.items()
        if hasattr(v, "cached_at") and now - v.cached_at > IDEMPOTENCY_TTL_SECONDS
    ]
    for k in expired:
        cache.pop(k, None)


# ---- Helpers --------------------------------------------------------------


def _resolve_repo_root(override: str | None) -> Path:
    """Resolve the repo root for a spec.* call.

    Precedence: ``override`` argument → ``Path.cwd()``. Used by all
    four handlers so the ``--workspace`` flag from the CLI threads
    through unchanged.
    """
    if override:
        return Path(override)
    return Path.cwd()


def _extract_gate_specs(_body: bytes) -> list[GateSpec]:
    """Extract typed :class:`GateSpec` rows from a spec markdown body.

    v0.4.0 seam — returns an empty list because the spec body is a
    free-form markdown scaffold (see :func:`spec_writer.scaffold_body`)
    that does not yet carry typed GateSpec rows in a parseable block.
    P28-I01-W08 lands the body schema + parser that yields real
    GateSpec rows; the W09 promote-side argv-policy check
    (:func:`eawf.kernel.spec.promotion.validate_argv_gates`) already
    consumes whatever this helper returns, so W08 only needs to
    replace the body of this function.

    Args:
        _body: Raw markdown spec body bytes (unused in v0.4.0).

    Returns:
        Empty list — a placeholder until W08 lands the parser.
    """
    return []


def _resolve_cache_dir(override: str | None) -> Path | None:
    """Return *override* as a Path or None.

    ``None`` defers resolution to :func:`spec_cache.default_cache_dir`,
    which honours the ``EAWF_SPEC_CACHE_DIR`` env-var test seam.
    """
    if override:
        return Path(override)
    return None


def _build_envelope(
    *,
    operation: str,
    scope_id: str,
    spec_urn: str,
    status: str,
    file_path: str,
    file_sha: str,
) -> Envelope:
    """Build the canonical ``SPEC_UPDATED`` envelope."""
    now = datetime.now(UTC)
    summary = f"spec.{operation} scope_id={scope_id} status={status}"
    payload: dict[str, Any] = {
        "operation": operation,
        "scope_id": scope_id,
        "spec_urn": spec_urn,
        "status": status,
        "file_path": file_path,
        "file_sha": file_sha,
    }
    return Envelope(
        schema_version="1.0",
        id=f"SPEC-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.SPEC_UPDATED,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


def _publish(ctx: MethodContext, envelope: Envelope) -> None:
    """Publish *envelope* on the subscription bus if one is attached."""
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id


def _result_dict(
    *,
    operation: str,
    scope_id: str,
    spec_urn: str,
    status: str,
    file_path: str,
    file_sha: str,
    envelope: Envelope,
    replay: bool = False,
) -> dict[str, Any]:
    """Build the JSON-mode result dict for a spec.* return."""
    return SpecResult(
        operation=operation,
        scope_id=scope_id,
        spec_urn=spec_urn,
        status=status,
        file_path=file_path,
        file_sha=file_sha,
        envelope=envelope.model_dump(mode="json"),
        idempotent_replay=replay,
    ).model_dump(mode="json")


def _idempotent_replay(
    ctx: MethodContext,
    idempotency_key: str | None,
) -> dict[str, Any] | None:
    """Return the cached result for *idempotency_key*, or ``None``."""
    cache = _idempotency_cache(ctx)
    _evict_expired(cache, now=time.monotonic())
    if idempotency_key is None:
        return None
    cached = cache.get(idempotency_key)
    if cached is None or not hasattr(cached, "result"):
        return None
    result = dict(cached.result)
    result["idempotent_replay"] = True
    return result


def _cache_replay(
    ctx: MethodContext,
    *,
    idempotency_key: str | None,
    result: dict[str, Any],
) -> None:
    """Store *result* under *idempotency_key* for replay (when supplied)."""
    if idempotency_key is None:
        return
    cache = _idempotency_cache(ctx)
    cache[idempotency_key] = _CachedSpecMutation(
        result=result,
        cached_at=time.monotonic(),
    )


# ---- Handlers --------------------------------------------------------------


@register("spec.init")
async def init(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Scaffold a new spec file under daemon authority.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`InitParams`.

    Returns:
        Dict matching :class:`SpecResult` with ``status='DRAFT'`` and
        the freshly-allocated ``file_sha``. Re-init on the same scope
        returns the cached row untouched (idempotent).

    Raises:
        ValueError: When *scope_id* fails parsing (mapped to
            ``-32602``).
    """
    try:
        args = InitParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    try:
        spec_writer.classify_scope(args.scope_id)
    except ValueError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    replay = _idempotent_replay(ctx, args.idempotency_key)
    if replay is not None:
        logger.info(f"init idempotent_replay scope_id={args.scope_id!r}")
        return replay

    repo_root = _resolve_repo_root(args.repo_root)
    cache_dir = _resolve_cache_dir(args.cache_dir)
    phase_id = spec_writer.phase_of(args.scope_id)
    spec_urn = spec_writer.build_spec_urn(args.scope_id, repo_code=args.repo_code)
    file_path = spec_writer.spec_file_path(args.scope_id, repo_root=repo_root)

    ctx.in_flight_mutations += 1
    try:
        existing = spec_cache.find_cached_entry(
            spec_urn,
            phase_id=phase_id,
            cache_dir=cache_dir,
        )
        if existing is not None and file_path.is_file():
            envelope = _build_envelope(
                operation="init",
                scope_id=args.scope_id,
                spec_urn=spec_urn,
                status=existing.status,
                file_path=existing.file_path,
                file_sha=existing.file_sha,
            )
            _publish(ctx, envelope)
            result = _result_dict(
                operation="init",
                scope_id=args.scope_id,
                spec_urn=spec_urn,
                status=existing.status,
                file_path=existing.file_path,
                file_sha=existing.file_sha,
                envelope=envelope,
            )
            _cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
            return result

        body = spec_writer.scaffold_body(
            scope_id=args.scope_id,
            title=args.title,
            spec_urn=spec_urn,
        )
        file_sha = spec_writer.write_spec_file(file_path, body)
        entry = spec_writer.build_entry(
            spec_urn=spec_urn,
            file_sha=file_sha,
            file_path=file_path,
            repo_root=repo_root,
            status="DRAFT",
        )
        spec_writer.write_cache_entry(
            phase_id=phase_id,
            entry=entry,
            cache_dir=cache_dir,
        )
        envelope = _build_envelope(
            operation="init",
            scope_id=args.scope_id,
            spec_urn=spec_urn,
            status="DRAFT",
            file_path=entry.file_path,
            file_sha=entry.file_sha,
        )
        _publish(ctx, envelope)
        logger.info(f"init ok scope_id={args.scope_id!r} urn={spec_urn!r} sha={file_sha[:8]}")
        result = _result_dict(
            operation="init",
            scope_id=args.scope_id,
            spec_urn=spec_urn,
            status="DRAFT",
            file_path=entry.file_path,
            file_sha=entry.file_sha,
            envelope=envelope,
        )
        _cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
        return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


@register("spec.validate")
async def validate(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Re-hash the on-disk spec body and refresh the cache entry.

    W03 ships the structural pass — the typed-model validators
    (PSV/ISV/WSV) land in P25-W05. Validate confirms the spec file
    exists, recomputes the blob SHA, and refreshes
    ``last_modified`` so subscribers detect external edits.
    """
    try:
        args = ValidateParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    try:
        spec_writer.classify_scope(args.scope_id)
    except ValueError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    repo_root = _resolve_repo_root(args.repo_root)
    cache_dir = _resolve_cache_dir(args.cache_dir)
    phase_id = spec_writer.phase_of(args.scope_id)
    spec_urn = spec_writer.build_spec_urn(args.scope_id, repo_code=args.repo_code)
    file_path = spec_writer.spec_file_path(args.scope_id, repo_root=repo_root)

    if not file_path.is_file():
        raise ValueError(
            f"validation_failed: spec file missing for scope_id={args.scope_id!r}: {file_path}"
        )

    ctx.in_flight_mutations += 1
    try:
        existing = spec_cache.find_cached_entry(
            spec_urn,
            phase_id=phase_id,
            cache_dir=cache_dir,
        )
        if existing is None:
            raise ValueError(
                f"validation_failed: scope_id={args.scope_id!r} not initialised; "
                "run spec.init first"
            )
        body = file_path.read_bytes()
        file_sha = spec_writer.blob_sha_for(body)
        entry = spec_writer.build_entry(
            spec_urn=spec_urn,
            file_sha=file_sha,
            file_path=file_path,
            repo_root=repo_root,
            status=existing.status,
            archived_commit=existing.archived_commit,
        )
        spec_writer.write_cache_entry(
            phase_id=phase_id,
            entry=entry,
            cache_dir=cache_dir,
        )
        envelope = _build_envelope(
            operation="validate",
            scope_id=args.scope_id,
            spec_urn=spec_urn,
            status=existing.status,
            file_path=entry.file_path,
            file_sha=entry.file_sha,
        )
        _publish(ctx, envelope)
        logger.info(f"validate ok scope_id={args.scope_id!r} urn={spec_urn!r} sha={file_sha[:8]}")
        return _result_dict(
            operation="validate",
            scope_id=args.scope_id,
            spec_urn=spec_urn,
            status=existing.status,
            file_path=entry.file_path,
            file_sha=entry.file_sha,
            envelope=envelope,
        )
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


@register("spec.promote")
async def promote(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Graduate a spec DRAFT → READY → IMPLEMENTED.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`PromoteParams`.

    Returns:
        Dict matching :class:`SpecResult` reflecting the new status.

    Raises:
        ValueError: When the requested target_status is not the
            forward step from the current cached status (mapped to
            ``-32602``).
    """
    try:
        args = PromoteParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    replay = _idempotent_replay(ctx, args.idempotency_key)
    if replay is not None:
        logger.info(f"promote idempotent_replay scope_id={args.scope_id!r}")
        return replay

    try:
        spec_writer.classify_scope(args.scope_id)
    except ValueError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    repo_root = _resolve_repo_root(args.repo_root)
    cache_dir = _resolve_cache_dir(args.cache_dir)
    phase_id = spec_writer.phase_of(args.scope_id)
    spec_urn = spec_writer.build_spec_urn(args.scope_id, repo_code=args.repo_code)
    file_path = spec_writer.spec_file_path(args.scope_id, repo_root=repo_root)

    ctx.in_flight_mutations += 1
    try:
        existing = spec_cache.find_cached_entry(
            spec_urn,
            phase_id=phase_id,
            cache_dir=cache_dir,
        )
        if existing is None:
            raise ValueError(
                f"validation_failed: scope_id={args.scope_id!r} not initialised; "
                "run spec.init first"
            )
        expected_target = _PROMOTE_TARGETS.get(existing.status)
        if expected_target is None:
            raise ValueError(f"validation_failed: cannot promote from status={existing.status!r}")
        if args.target_status != expected_target:
            raise ValueError(
                f"validation_failed: invalid graduation from {existing.status!r} "
                f"to {args.target_status!r}; expected {expected_target!r}"
            )
        if not file_path.is_file():
            raise ValueError(
                f"validation_failed: spec file missing for scope_id={args.scope_id!r}: {file_path}"
            )
        body = file_path.read_bytes()
        file_sha = spec_writer.blob_sha_for(body)
        if args.target_status == "READY":
            # W09 — L0 argv-policy attaches at the spec-promote→READY
            # persistence seam. Walks the spec body's embedded GateSpec
            # rows and routes each argv-bearing gate's ``args['argv']``
            # through :func:`validate_gate_argv`. Atomicity: the check
            # runs BEFORE :func:`write_cache_entry`, so a reject leaves
            # the spec in its prior DRAFT status with no cache mutation.
            # The body parser that yields typed GateSpec rows lands in
            # W08; until then the iterable is empty and the call is a
            # no-op pass-through — the seam exists so W08 has a single
            # call site to feed.
            gates_in_body: list[GateSpec] = _extract_gate_specs(body)
            try:
                validate_argv_gates(gates_in_body)
            except SpecPromoteValidationError as exc:
                raise ValueError(f"validation_failed: {exc}") from exc
        entry = spec_writer.build_entry(
            spec_urn=spec_urn,
            file_sha=file_sha,
            file_path=file_path,
            repo_root=repo_root,
            status=args.target_status,
        )
        spec_writer.write_cache_entry(
            phase_id=phase_id,
            entry=entry,
            cache_dir=cache_dir,
        )
        envelope = _build_envelope(
            operation="promote",
            scope_id=args.scope_id,
            spec_urn=spec_urn,
            status=args.target_status,
            file_path=entry.file_path,
            file_sha=entry.file_sha,
        )
        _publish(ctx, envelope)
        logger.info(
            f"promote ok scope_id={args.scope_id!r} urn={spec_urn!r} status={args.target_status}"
        )
        result = _result_dict(
            operation="promote",
            scope_id=args.scope_id,
            spec_urn=spec_urn,
            status=args.target_status,
            file_path=entry.file_path,
            file_sha=entry.file_sha,
            envelope=envelope,
        )
        _cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
        return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


@register("spec.archive")
async def archive(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Atomically ``git rm`` the spec file + write the archived cache entry.

    Requires the source spec to be in IMPLEMENTED state. The ``git
    rm`` step uses ``subprocess.run`` with a fixed argv — the daemon
    refuses to run when the path is outside the repo root. After
    ``git rm`` succeeds the cache entry is written with the blob SHA
    of the body that was just removed so :func:`eawf spec show
    <urn> --from-git` can locate the body via ``git log -- <path>``.
    """
    try:
        args = ArchiveParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    replay = _idempotent_replay(ctx, args.idempotency_key)
    if replay is not None:
        logger.info(f"archive idempotent_replay scope_id={args.scope_id!r}")
        return replay

    try:
        spec_writer.classify_scope(args.scope_id)
    except ValueError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    repo_root = _resolve_repo_root(args.repo_root)
    cache_dir = _resolve_cache_dir(args.cache_dir)
    phase_id = spec_writer.phase_of(args.scope_id)
    spec_urn = spec_writer.build_spec_urn(args.scope_id, repo_code=args.repo_code)
    file_path = spec_writer.spec_file_path(args.scope_id, repo_root=repo_root)

    ctx.in_flight_mutations += 1
    try:
        existing = spec_cache.find_cached_entry(
            spec_urn,
            phase_id=phase_id,
            cache_dir=cache_dir,
        )
        if existing is None:
            raise ValueError(
                f"validation_failed: scope_id={args.scope_id!r} not initialised; "
                "run spec.init first"
            )
        if existing.status != "IMPLEMENTED":
            raise ValueError(
                f"validation_failed: cannot archive from status={existing.status!r}; "
                "expected 'IMPLEMENTED'"
            )
        if not file_path.is_file():
            raise ValueError(
                f"validation_failed: spec file missing for scope_id={args.scope_id!r}: {file_path}"
            )
        body = file_path.read_bytes()
        file_sha = spec_writer.blob_sha_for(body)
        rel_path = Path(existing.file_path)
        spec_writer.git_rm_spec(
            repo_root=repo_root,
            repo_relative_path=rel_path,
        )
        entry = spec_writer.build_entry(
            spec_urn=spec_urn,
            file_sha=file_sha,
            file_path=file_path,
            repo_root=repo_root,
            status="ARCHIVED",
        )
        spec_writer.write_cache_entry(
            phase_id=phase_id,
            entry=entry,
            cache_dir=cache_dir,
        )
        envelope = _build_envelope(
            operation="archive",
            scope_id=args.scope_id,
            spec_urn=spec_urn,
            status="ARCHIVED",
            file_path=entry.file_path,
            file_sha=entry.file_sha,
        )
        _publish(ctx, envelope)
        logger.info(f"archive ok scope_id={args.scope_id!r} urn={spec_urn!r} sha={file_sha[:8]}")
        result = _result_dict(
            operation="archive",
            scope_id=args.scope_id,
            spec_urn=spec_urn,
            status="ARCHIVED",
            file_path=entry.file_path,
            file_sha=entry.file_sha,
            envelope=envelope,
        )
        _cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
        return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "archive",
    "init",
    "promote",
    "validate",
]
