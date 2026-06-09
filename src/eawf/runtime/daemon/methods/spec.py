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
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.spec import cache as spec_cache
from eawf.kernel.spec import writer as spec_writer
from eawf.kernel.spec.common import (
    CriterionSpec,
    GateSpec,
    validate_criterion_gate_refs,
)
from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.spec.promotion import (
    SpecPromoteValidationError,
    validate_argv_gates,
)
from eawf.kernel.spec.wave_body import WAVE_BODY_FENCE, WaveSpecBody
from eawf.kernel.state.enums import StoreKind, WaveStatus
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.validate.strict import validate_state
from eawf.platform.lint.eawf021_measurable_criterion import (
    MeasurabilityViolation,
    check_criterion_spec,
)
from eawf.platform.lint.eawf022_propose_coverage import (
    CoverageGapViolation,
)
from eawf.runtime.daemon import wal
from eawf.runtime.daemon.methods import (
    DaemonValidationError,
    MethodContext,
    register,
)
from eawf.runtime.daemon.methods.state import (
    _read_state,
    _resolve_mutator_paths,
    _state_version,
)
from eawf.runtime.daemon.wal import WalRecord
from eawf.workflow.lifecycle.transitions import LifecycleError, edit_wave_plan
from eawf.workflow.propose.coverage import coverage_gaps, source_brief_coverage_gaps

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


class SyncParams(BaseModel):
    """Params for :func:`sync`.

    Attributes:
        wave_id: Canonical wave id (``P##-I##-W##``) whose typed
            ``success_criteria`` + ``gates`` are materialised from the
            spec body.
        spec_path: Optional repo-relative or absolute path of the spec
            markdown file. ``None`` resolves the default per-wave spec
            file (``.ea/specs/<phase>/<iter>/<wave>.md``) via
            :func:`eawf.kernel.spec.writer.spec_file_path`.
        repo_root: Optional absolute path of the repo working tree
            (default ``Path.cwd``). The CLI proxy forwards
            ``flags.workspace`` here so per-test ``tmp_path``-rooted
            repos resolve correctly.
        idempotency_key: Optional caller-supplied retry key.
    """

    model_config = ConfigDict(extra="forbid")

    wave_id: str = Field(min_length=1)
    spec_path: str | None = None
    repo_root: str | None = None
    idempotency_key: str | None = None


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


class SpecSyncResult(BaseModel):
    """Result shape for the :func:`sync` RPC.

    Distinct from :class:`SpecResult` because ``spec.sync`` mutates the
    wave's typed ``state.json`` row (not the spec cache) — it reports the
    materialised criteria / gate counts plus the canonical state event
    envelope, mirroring the ``state.mutate`` result fields the lifecycle
    surfaces already consume.
    """

    model_config = ConfigDict(extra="forbid")

    operation: str
    wave_id: str
    criteria_count: int
    gates_count: int
    before_version: str
    after_version: str
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


#: Matches the ``eawf-wave-body`` fenced YAML block in a markdown body.
#: ``re.DOTALL`` lets ``.`` span newlines so the captured group is the
#: whole block content; ``re.MULTILINE`` anchors the open/close fences to
#: the start of a line so a fence inside indented prose is not matched.
#: Tolerates optional trailing whitespace after the open info string and a
#: trailing newline before the close fence so an authored block round-trips.
_WAVE_BODY_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    rf"^```{re.escape(WAVE_BODY_FENCE)}[ \t]*\n(?P<yaml>.*?)\n?^```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def _decode_body(body: bytes | str) -> str:
    """Return *body* as text, decoding bytes as UTF-8.

    The promote handler feeds raw ``file_path.read_bytes()`` while tests
    and the ``eawf spec sync`` command (W05) pass already-decoded text;
    this helper accepts either so the extractors have one entry shape.

    Args:
        body: Raw markdown spec body, as bytes or text.

    Returns:
        The body decoded to ``str``.
    """
    if isinstance(body, bytes):
        return body.decode("utf-8")
    return body


def _parse_wave_body(body: bytes | str) -> WaveSpecBody | None:
    """Parse the ``eawf-wave-body`` fenced block of a spec body, if present.

    Locates the single fenced YAML block labelled
    :data:`~eawf.kernel.spec.wave_body.WAVE_BODY_FENCE`, deserialises its
    contents with :func:`yaml.safe_load`, and validates the mapping
    through :meth:`~eawf.kernel.spec.wave_body.WaveSpecBody.from_mapping`.
    A body with no such fence returns ``None`` so the legacy scaffold
    body (see :func:`spec_writer.scaffold_body`) stays a clean no-op
    (back-compat). A fenced block whose YAML is malformed, or whose
    mapping violates the strict :class:`WaveSpecBody` contract, surfaces
    the underlying error — the typed parse boundary is the single place
    an authoring mistake fails.

    Args:
        body: Raw markdown spec body, as bytes or text.

    Returns:
        The validated :class:`WaveSpecBody`, or ``None`` when the body
        carries no ``eawf-wave-body`` fenced block.

    Raises:
        ValueError: When the fenced block's YAML does not deserialise to
            a mapping (e.g. a bare scalar or a sequence).
        yaml.YAMLError: When the fenced block is not well-formed YAML.
        pydantic.ValidationError: When the mapping carries an unknown
            key, a malformed criterion / gate row, or a cross-reference
            to an absent criterion / gate id.
    """
    text = _decode_body(body)
    match = _WAVE_BODY_BLOCK_RE.search(text)
    if match is None:
        return None
    payload = yaml.safe_load(match.group("yaml"))
    if payload is None:
        # An empty fenced block is a valid, if uninteresting, document.
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError(
            f"wave-body block must deserialise to a mapping, got {type(payload).__name__}"
        )
    return WaveSpecBody.from_mapping(payload)


def _extract_gate_specs(body: bytes | str) -> list[GateSpec]:
    """Extract typed :class:`GateSpec` rows from a spec markdown body.

    Parses the ``eawf-wave-body`` fenced block (see
    :func:`_parse_wave_body`) and returns its ``gates`` list. A body with
    no such block returns an empty list so the legacy scaffold body stays
    a clean no-op. The W09 promote-side argv-policy check
    (:func:`eawf.kernel.spec.promotion.validate_argv_gates`) consumes
    whatever this helper returns.

    Args:
        body: Raw markdown spec body, as bytes or text.

    Returns:
        The typed gate rows, or an empty list when the body carries no
        ``eawf-wave-body`` fenced block.

    Raises:
        ValueError: When the fenced block's YAML is not a mapping.
        yaml.YAMLError: When the fenced block is not well-formed YAML.
        pydantic.ValidationError: When a gate / criterion row is
            malformed or a cross-reference does not resolve.
    """
    parsed = _parse_wave_body(body)
    if parsed is None:
        return []
    return parsed.gates


def _extract_criterion_specs(body: bytes | str) -> list[CriterionSpec]:
    """Extract typed :class:`CriterionSpec` rows from a spec markdown body.

    Sibling of :func:`_extract_gate_specs`: parses the same
    ``eawf-wave-body`` fenced block and returns its ``criteria`` list,
    each row carrying its ``evidence_kind`` and ``gate_ids``. A body with
    no such block returns an empty list (back-compat with the legacy
    scaffold). The ``eawf spec sync`` command (W05) materialises these
    rows onto the wave's typed ``success_criteria`` field.

    Args:
        body: Raw markdown spec body, as bytes or text.

    Returns:
        The typed criterion rows, or an empty list when the body carries
        no ``eawf-wave-body`` fenced block.

    Raises:
        ValueError: When the fenced block's YAML is not a mapping.
        yaml.YAMLError: When the fenced block is not well-formed YAML.
        pydantic.ValidationError: When a criterion / gate row is
            malformed or a cross-reference does not resolve.
    """
    parsed = _parse_wave_body(body)
    if parsed is None:
        return []
    return parsed.criteria


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
            # L0 argv-policy attaches at the spec-promote->READY
            # persistence seam. Walks the spec body's embedded GateSpec
            # rows and routes each argv-bearing gate's ``args['argv']``
            # through :func:`validate_gate_argv`. Atomicity: the check
            # runs BEFORE :func:`write_cache_entry`, so a reject leaves
            # the spec in its prior DRAFT status with no cache mutation.
            # A scaffold body with no ``eawf-wave-body`` fenced block
            # yields an empty list and the call is a no-op pass-through.
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


# ---- spec.sync helpers ----------------------------------------------------


def _run_measurability_lint(criteria: list[CriterionSpec]) -> list[MeasurabilityViolation]:
    """Return every EAWF021 measurability finding across *criteria*.

    Runs the EAWF021 entrypoint
    :func:`eawf.platform.lint.eawf021_measurable_criterion.check_criterion_spec`
    over each parsed criterion. A non-empty result means at least one criterion
    carries a banned-vague token (in its ``text`` or ``measurable_signal``) or
    lacks an observation contract -- the sync rejects rather than materialise an
    unfalsifiable criterion onto the wave row. The daemon parses authored typed
    criteria from the spec body (never legacy rows), so the grandfathered
    exemption the wave-plan transition applies is not needed here.

    Args:
        criteria: The parsed criterion rows from the spec body.

    Returns:
        Findings in criterion order; empty when every criterion is
        measurable.
    """
    findings: list[MeasurabilityViolation] = []
    for criterion in criteria:
        findings.extend(check_criterion_spec(criterion))
    return findings


def _run_coverage_lint(
    criteria: list[CriterionSpec],
    *,
    intent: IntentBrief | None,
    repo_root: Path,
) -> list[CoverageGapViolation]:
    """Return every EAWF022 coverage gap of a wave's brief detail by *criteria*.

    Thin daemon-side delegate to the
    :mod:`eawf.workflow.propose.coverage` diffs so the sync path and the
    ``/roadmap propose`` render run one implementation. Two diffs run:

    - :func:`~eawf.workflow.propose.coverage.coverage_gaps` over the wave's
      ``planned_steps``: a planned step no criterion topically addresses is a
      finding so a silently-dropped step fails the sync.
    - :func:`~eawf.workflow.propose.coverage.source_brief_coverage_gaps` over
      the referenced source-brief document(s): a source-brief deliverable the
      planner never wrote a step for is a finding too. This leg closes the
      boundary the ``planned_steps`` diff cannot see -- and, for a
      required-intent wave, an empty ``planned_steps`` no longer short-circuits
      the coverage check, because the source brief still enumerates
      deliverables.

    Args:
        criteria: The parsed criterion rows from the spec body.
        intent: The wave's :class:`~eawf.kernel.spec.intent.IntentBrief`, or
            ``None`` when the wave carries no intent.
        repo_root: The repo working-tree root the brief's ``source_brief_ids``
            paths resolve under.

    Returns:
        One finding per uncovered planned-step span and per uncovered
        source-brief unit; empty when every brief detail is covered.
    """
    planned_steps = list(intent.planned_steps) if intent is not None else []
    findings = coverage_gaps(criteria, planned_steps=planned_steps)
    if intent is not None:
        findings += source_brief_coverage_gaps(criteria, intent=intent, repo_root=repo_root)
    return findings


def _render_lint_findings(
    measurability: list[MeasurabilityViolation],
    coverage: list[CoverageGapViolation],
) -> str:
    """Render a combined ``validation_failed`` message for lint findings.

    Args:
        measurability: EAWF021 findings (may be empty).
        coverage: EAWF022 findings (may be empty).

    Returns:
        A single-line ``validation_failed: ...`` message listing each
        finding's ``code reason: snippet`` body, comma-joined.
    """
    bodies = [v.render() for v in measurability] + [v.render() for v in coverage]
    return "validation_failed: spec sync lint findings: " + "; ".join(bodies)


# ---- spec.sync handler ----------------------------------------------------


@register("spec.sync")
async def sync(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Materialise a wave spec body's criteria + gates onto the wave row.

    The authoring keystone: reads the per-wave spec markdown body, parses
    its ``eawf-wave-body`` fenced block into typed criteria + gates (via
    :func:`_extract_criterion_specs` / :func:`_extract_gate_specs`), runs
    the EAWF021 measurability lint + the EAWF022 coverage lint, and — only
    when both pass — replaces the target PENDING wave's typed
    :attr:`~eawf.kernel.state.models.Wave.success_criteria` +
    :attr:`~eawf.kernel.state.models.Wave.gates` through the daemon's
    canonical state-write transaction (portalock → WAL → atomic write →
    event append), per AGENTS rule 4.

    The criteria are written through the lifecycle
    :func:`~eawf.workflow.lifecycle.wave.edit_wave_plan` transition (which
    enforces the PENDING-only invariant of planned-scope-revisability);
    the gates are set on the same wave row and the combined criterion /
    gate cross-references are checked by
    :func:`~eawf.kernel.spec.common.validate_criterion_gate_refs` before the
    write commits.

    Args:
        ctx: Server context. ``ctx.wal_dir`` MUST be configured.
        params: JSON-RPC params per :class:`SyncParams`.

    Returns:
        Dict matching :class:`SpecSyncResult` with the materialised
        criteria / gate counts + the canonical state event envelope.

    Raises:
        DaemonValidationError: When the spec body fails to parse, a lint
            finding rejects the criteria, the target wave is not PENDING,
            the criterion / gate cross-references do not resolve, or the
            post-mutation state fails schema / invariant validation
            (mapped to ``-32002`` so the CLI exit code matches a
            rejected mutation).
        ValueError: When *wave_id* is not a wave scope, the wave is
            unknown, or the spec file is missing (mapped to ``-32602``).
        RuntimeError: When ``ctx.wal_dir`` is unset.
    """
    try:
        args = SyncParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    try:
        kind = spec_writer.classify_scope(args.wave_id)
    except ValueError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc
    if kind != "wave":
        raise ValueError(f"validation_failed: spec sync targets a wave scope, got {args.wave_id!r}")

    replay = _idempotent_replay(ctx, args.idempotency_key)
    if replay is not None:
        logger.info(f"sync idempotent_replay wave={args.wave_id!r}")
        return replay

    repo_root = _resolve_repo_root(args.repo_root)
    spec_file = _resolve_sync_spec_file(
        wave_id=args.wave_id,
        spec_path=args.spec_path,
        repo_root=repo_root,
    )
    body = spec_file.read_text(encoding="utf-8")
    try:
        criteria = _extract_criterion_specs(body)
        gates = _extract_gate_specs(body)
    except (ValueError, yaml.YAMLError, ValidationError) as exc:
        raise DaemonValidationError(f"validation_failed: spec body parse failed: {exc}") from exc

    state_path, event_path, wal_path = _resolve_mutator_paths(
        repo_root=args.repo_root,
        ctx=ctx,
    )

    from eawf.runtime.lock import portalock

    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(state_path, timeout=5.0):
            result = _apply_sync_locked(
                ctx,
                args=args,
                criteria=criteria,
                gates=gates,
                repo_root=repo_root,
                state_path=state_path,
                event_path=event_path,
                wal_path=wal_path,
            )
        _cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
        return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


def _resolve_sync_spec_file(*, wave_id: str, spec_path: str | None, repo_root: Path) -> Path:
    """Resolve + verify the on-disk spec file for a sync.

    Args:
        wave_id: The wave whose default spec path is derived when
            *spec_path* is ``None``.
        spec_path: Optional explicit path (repo-relative or absolute).
        repo_root: Repo working-tree root the relative path resolves under.

    Returns:
        The existing spec file path.

    Raises:
        ValueError: When the resolved spec file does not exist (mapped to
            ``-32602``).
    """
    if spec_path is not None:
        spec_file = Path(spec_path)
        if not spec_file.is_absolute():
            spec_file = repo_root / spec_file
    else:
        spec_file = spec_writer.spec_file_path(wave_id, repo_root=repo_root)
    if not spec_file.is_file():
        raise ValueError(f"validation_failed: spec file missing for wave={wave_id!r}: {spec_file}")
    return spec_file


def _apply_sync_locked(
    ctx: MethodContext,
    *,
    args: SyncParams,
    criteria: list[CriterionSpec],
    gates: list[GateSpec],
    repo_root: Path,
    state_path: Path,
    event_path: Path,
    wal_path: Path,
) -> dict[str, Any]:
    """Run the locked spec-sync transaction: lint, mutate, validate, write.

    The caller holds the state-path portalock. This helper reads + validates
    state, enforces the PENDING-only gate, runs the EAWF021 + EAWF022 lints
    (rejecting before any mutation), materialises the criteria via
    :func:`~eawf.workflow.lifecycle.wave.edit_wave_plan` + sets the gates,
    re-validates the post-mutation state, then commits through the canonical
    WAL → atomic-write → event-append sequence and publishes the envelope.

    Args:
        ctx: Server context (for the publish bus).
        args: The validated sync params.
        criteria: Parsed + lint-pending criterion rows.
        gates: Parsed gate rows.
        repo_root: The repo working-tree root the wave's
            ``IntentBrief.source_brief_ids`` paths resolve under (read by the
            EAWF022 source-brief coverage leg).
        state_path: Path to ``state.json``.
        event_path: Path to the event JSONL store.
        wal_path: Path to the daemon WAL directory.

    Returns:
        Dict matching :class:`SpecSyncResult`.

    Raises:
        DaemonValidationError: When the wave is not PENDING, a lint finding
            rejects the criteria, the cross-references do not resolve, or the
            post-mutation state fails validation (mapped to ``-32002``).
        ValueError: When the wave id is unknown (mapped to ``-32602``).
    """
    state, _payload = _read_state(state_path)
    before_version = _state_version(state.model_dump(mode="json"))
    wave = state.waves.get(args.wave_id)
    if wave is None:
        raise ValueError(f"validation_failed: unknown wave: {args.wave_id!r}")
    if wave.status != WaveStatus.PENDING:
        raise DaemonValidationError(
            f"validation_failed: wave {args.wave_id!r} is not pending "
            f"(status={wave.status.value!r}); only PENDING waves accept a spec sync"
        )

    measurability = _run_measurability_lint(criteria)
    coverage = _run_coverage_lint(criteria, intent=wave.intent, repo_root=repo_root)
    if measurability or coverage:
        raise DaemonValidationError(_render_lint_findings(measurability, coverage))

    # Referential integrity (criterion.gate_ids <-> gate.criterion_id,
    # deterministic-gate compile) BEFORE any in-place mutation so a malformed
    # pair leaves the wave row untouched.
    validate_criterion_gate_refs(criteria, gates)

    try:
        edit_wave_plan(state, wave_id=args.wave_id, success_criteria=criteria)
    except LifecycleError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    wave.gates = list(gates)

    state.updated_at = datetime.now(UTC)
    new_payload = state.model_dump(mode="json")
    after_version = _validate_post_sync(new_payload)

    mutation_id = uuid.uuid4().hex
    envelope = _build_sync_envelope(
        wave_id=args.wave_id,
        criteria_count=len(criteria),
        gates_count=len(gates),
        before_version=before_version,
        after_version=after_version,
    )
    record = WalRecord(
        record_id=mutation_id,
        envelope=envelope,
        idempotency_key=args.idempotency_key,
        written_at=datetime.now(UTC),
        before_state_version=before_version,
        after_state_version=after_version,
    )
    wal.write_pending(wal_path, record)
    atomic_write_json_locked(state_path, new_payload)
    wal.mark_applied(wal_path, mutation_id)
    append_envelope(event_path, envelope)
    wal.mark_fsynced(wal_path, mutation_id)

    _publish(ctx, envelope)
    logger.info(
        f"sync ok wave={args.wave_id} criteria={len(criteria)} gates={len(gates)} "
        f"before={before_version} after={after_version}"
    )
    return SpecSyncResult(
        operation="sync",
        wave_id=args.wave_id,
        criteria_count=len(criteria),
        gates_count=len(gates),
        before_version=before_version,
        after_version=after_version,
        envelope=envelope.model_dump(mode="json"),
    ).model_dump(mode="json")


def _validate_post_sync(new_payload: dict[str, Any]) -> str:
    """Re-validate the post-mutation state payload and return its version.

    Args:
        new_payload: The candidate ``state.json`` payload after the sync
            mutation applied.

    Returns:
        The post-mutation state version digest.

    Raises:
        DaemonValidationError: When the payload fails schema validation or
            trips an invariant (mapped to ``-32002``).
    """
    post = validate_state(new_payload, strict_optional=False)
    if post.state is None:
        raise DaemonValidationError(
            "validation_failed: post-mutation schema invalid: " + "; ".join(post.schema_errors[:3])
        )
    if post.violations:
        codes = ",".join(v.code for v in post.violations)
        raise DaemonValidationError(
            f"validation_failed: post-mutation invariants violated: {codes}"
        )
    return _state_version(new_payload)


def _build_sync_envelope(
    *,
    wave_id: str,
    criteria_count: int,
    gates_count: int,
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build the canonical ``StoreKind.EVENT`` envelope for a spec sync.

    Mirrors the ``state.mutate`` event shape (a ``state.mutate.spec_sync``
    ``event_type``) so subscribers and the event log cannot tell the sync
    write apart from any other canonical state mutation.

    Args:
        wave_id: The wave whose criteria / gates were materialised.
        criteria_count: Number of typed criteria written.
        gates_count: Number of typed gates written.
        before_version: State digest before the write.
        after_version: State digest after the write.

    Returns:
        The canonical event envelope, ready for the WAL + event log.
    """
    now = datetime.now(UTC)
    summary = f"spec.sync wave={wave_id} criteria={criteria_count} gates={gates_count}"
    payload = EventPayload(
        timestamp=now,
        event_type="state.mutate.spec_sync",
        actor="daemon",
        command="spec.sync",
        args_hash="",
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok",
        message=summary,
        extras={"criteria_count": criteria_count, "gates_count": gates_count},
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


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "archive",
    "init",
    "promote",
    "sync",
    "validate",
]
