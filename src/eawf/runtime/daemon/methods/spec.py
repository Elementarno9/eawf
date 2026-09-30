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
``file_sha`` pre-populated so the retired ``spec show`` verb
can recover the body via ``git log -- <path>``.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.spec import cache as spec_cache
from eawf.kernel.spec import writer as spec_writer
from eawf.kernel.spec.common import (
    GateSpec,
)
from eawf.kernel.spec.promotion import (
    SpecPromoteValidationError,
    validate_argv_gates,
)
from eawf.kernel.spec.wave_body import WAVE_BODY_FENCE, WaveSpecBody
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.runtime.daemon.methods import (
    MethodContext,
    register,
)
from eawf.runtime.daemon.methods.spec_context import (
    cache_replay,
    idempotent_replay,
    publish_envelope,
)
from eawf.workflow.lifecycle.wave_sha import derive_wave_sha
from eawf.workflow.verify.gate_conventions import GateConventionError, validate_gate_conventions

logger = logging.getLogger(__name__)


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
    """Params for :func:`archive`.

    Attributes:
        force: When ``True``, bypass the IMPLEMENTED-status gate so a spec
            in an earlier lifecycle status (e.g. DRAFT) can be archived.
            The blob-SHA recording and the cache-entry lookup are
            unaffected — a forced archive still records the body's blob SHA
            for ``spec show --from-git`` recovery and still raises when the
            scope was never initialised.
    """

    model_config = ConfigDict(extra="forbid")

    scope_id: str = Field(min_length=1)
    repo_code: str = Field(min_length=1)
    repo_root: str | None = None
    idempotency_key: str | None = None
    cache_dir: str | None = None
    force: bool = False


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
    and the retired ``spec sync`` verb pass already-decoded text;
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

    replay = idempotent_replay(ctx, args.idempotency_key)
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
            publish_envelope(ctx, envelope)
            result = _result_dict(
                operation="init",
                scope_id=args.scope_id,
                spec_urn=spec_urn,
                status=existing.status,
                file_path=existing.file_path,
                file_sha=existing.file_sha,
                envelope=envelope,
            )
            cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
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
        publish_envelope(ctx, envelope)
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
        cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
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
        publish_envelope(ctx, envelope)
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

    replay = idempotent_replay(ctx, args.idempotency_key)
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
            parsed = _parse_wave_body(body)
            is_wave = spec_writer.classify_scope(args.scope_id) == "wave"
            try:
                validate_argv_gates(gates_in_body)
                validate_gate_conventions(
                    gates_in_body,
                    wave_has_commit=(
                        is_wave and derive_wave_sha(args.scope_id, repo_root=repo_root) is not None
                    ),
                    timeout_exception=None if parsed is None else parsed.timeout_exception,
                )
            except (SpecPromoteValidationError, GateConventionError) as exc:
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
        publish_envelope(ctx, envelope)
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
        cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
        return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


@register("spec.archive")
async def archive(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Atomically ``git rm`` the spec file + write the archived cache entry.

    Requires the source spec to be in IMPLEMENTED state unless
    ``force=True`` relaxes the gate — a forced archive may run from an
    earlier lifecycle status (e.g. DRAFT). The ``git rm`` step uses
    ``subprocess.run`` with a fixed argv — the daemon refuses to run when
    the path is outside the repo root. After ``git rm`` succeeds the cache
    entry is written with the blob SHA of the body that was just removed so
    the retired ``spec show`` verb could locate the body via ``git
    log -- <path>``. ``force`` only bypasses the status gate; the
    cache-entry lookup still raises when the scope was never initialised.
    """
    try:
        args = ArchiveParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    replay = idempotent_replay(ctx, args.idempotency_key)
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
        if not args.force and existing.status != "IMPLEMENTED":
            raise ValueError(
                f"validation_failed: cannot archive from status={existing.status!r}; "
                "expected 'IMPLEMENTED' (pass force=True to override)"
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
        publish_envelope(ctx, envelope)
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
        cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
        return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


# ---- spec.sync handler ----------------------------------------------------


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
    "archive",
    "init",
    "promote",
    "validate",
]
