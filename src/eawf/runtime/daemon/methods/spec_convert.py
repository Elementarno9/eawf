"""``spec.convert_legacy`` JSON-RPC method: the live legacy-to-typed converter.

Split out of :mod:`eawf.runtime.daemon.methods.spec` (EAWF010 module-length
cap): the converter verb owns its params / result models, the per-row
conversion + honest-refusal logic, and the locked write transaction. The
shared spec-method plumbing (idempotency cache, mutator-path resolution,
post-mutation validation, publish bus) is imported from the sibling
modules so both verbs ride one implementation.

Every ``kind == legacy`` criterion row under the scope is pushed through
:func:`eawf.kernel.spec.common.convert_legacy_criterion` with the EAWF021
measurability lint applied per converted row; a row that cannot be made
measurable is REFUSED and stays legacy with a named reason (no silent
lossy conversion).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.spec import writer as spec_writer
from eawf.kernel.spec.common import (
    GRANDFATHERED_KIND,
    CriterionSpec,
    GateSpec,
    convert_legacy_criterion,
    validate_criterion_gate_refs,
)
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.runtime.daemon import wal
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.daemon.methods.spec import (
    _cache_replay,
    _idempotent_replay,
    _publish,
    _validate_post_sync,
)
from eawf.runtime.daemon.methods.spec_sync_lints import measure_criteria
from eawf.runtime.daemon.methods.state import (
    _read_state,
    _resolve_mutator_paths,
    _state_version,
)
from eawf.runtime.daemon.wal import WalRecord

logger = logging.getLogger(__name__)


class ConvertLegacyParams(BaseModel):
    """Params for :func:`convert_legacy`.

    Attributes:
        scope_id: ``P##`` / ``P##-I##`` / ``P##-I##-W##`` — every wave under
            the scope with at least one legacy criterion row is a candidate.
        dry_run: When ``True`` the converter computes + reports the
            would-convert set and writes nothing.
        repo_root: Optional absolute repo working-tree path (default cwd).
        idempotency_key: Optional caller-supplied retry key.
    """

    model_config = ConfigDict(extra="forbid")

    scope_id: str = Field(min_length=1)
    dry_run: bool = False
    repo_root: str | None = None
    idempotency_key: str | None = None


class ConvertRowReport(BaseModel):
    """One per-criterion row of the conversion report."""

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    criterion_id: str
    disposition: Literal["converted", "refused"]
    reason: str | None = None
    gate_kind: str | None = None


class SpecConvertLegacyResult(BaseModel):
    """Result shape for the :func:`convert_legacy` RPC."""

    model_config = ConfigDict(extra="forbid")

    operation: str
    scope_id: str
    dry_run: bool
    converted_count: int
    refused_count: int
    rows: list[dict[str, Any]]
    before_version: str | None
    after_version: str | None
    envelope: dict[str, Any] | None
    idempotent_replay: bool = False


def _waves_for_convert_scope(state: Any, scope_id: str, kind: str) -> list[str]:
    """Return the wave ids under *scope_id*, sorted, for the converter.

    Args:
        state: Loaded, validated state.
        scope_id: The convert scope (phase / iter / wave id).
        kind: The classified scope kind.

    Returns:
        Sorted wave ids in scope.

    Raises:
        ValueError: When a wave-kind *scope_id* names no wave in *state*.
    """
    if kind == "wave":
        if scope_id not in state.waves:
            raise ValueError(f"validation_failed: unknown wave: {scope_id!r}")
        return [scope_id]
    if kind == "iter":
        return sorted(w for w, row in state.waves.items() if row.iter_id == scope_id)
    return sorted(w for w, row in state.waves.items() if row.iter_id.startswith(f"{scope_id}-I"))


def _convert_wave_rows(
    wave: Any,
) -> tuple[list[CriterionSpec], list[GateSpec], list[ConvertRowReport]]:
    """Convert one wave's legacy criterion rows, refusing what stays honest.

    Every ``kind == legacy`` row is pushed through
    :func:`~eawf.kernel.spec.common.convert_legacy_criterion` and the EAWF021
    measurability lint. A row the converter cannot make measurable (no file
    scopes to anchor the falsifying gate, a sub-floor signal, a gate-id
    collision, or a cross-reference failure) is REFUSED: it stays ``legacy``
    with a named reason instead of being lossily converted. The converted row
    keeps its original criterion id so downstream references stay stable.

    Args:
        wave: The state wave row under conversion.

    Returns:
        ``(criteria, gates, reports)``: the wave's full post-conversion
        criteria list (non-legacy rows untouched), the full gates list
        (converted gates appended), and one report row per legacy criterion.
    """
    reports: list[ConvertRowReport] = []
    new_criteria: list[CriterionSpec] = list(wave.success_criteria)
    new_gates: list[GateSpec] = list(wave.gates)
    existing_gate_ids = {gate.id for gate in wave.gates}
    staged: list[tuple[int, CriterionSpec, GateSpec]] = []

    for position, criterion in enumerate(wave.success_criteria, start=1):
        if criterion.kind != GRANDFATHERED_KIND:
            continue
        attempt = _convert_one_row(
            wave,
            position=position,
            criterion=criterion,
            existing_gate_ids=existing_gate_ids,
        )
        if isinstance(attempt, ConvertRowReport):
            reports.append(attempt)
            # No silent legacy rows: a refused row STAYS legacy but carries
            # its named non-convertible reason on waiver_reason so committed
            # state explains why the row was never retyped.
            if not criterion.waiver_reason and attempt.reason:
                new_criteria[position - 1] = criterion.model_copy(
                    update={"waiver_reason": f"non-convertible: {attempt.reason}"}
                )
            continue
        converted, gate = attempt
        staged.append((position - 1, converted, gate))

    if staged:
        try:
            validate_criterion_gate_refs(
                [criterion for _, criterion, _ in staged],
                [gate for _, _, gate in staged],
            )
        except ValueError as exc:
            for index, criterion, _ in staged:
                reports.append(
                    ConvertRowReport(
                        wave_id=wave.id,
                        criterion_id=criterion.id,
                        disposition="refused",
                        reason=f"cross-reference validation failed: {exc}",
                    )
                )
                original = wave.success_criteria[index]
                if not original.waiver_reason:
                    new_criteria[index] = original.model_copy(
                        update={
                            "waiver_reason": (
                                f"non-convertible: cross-reference validation failed: {exc}"
                            )
                        }
                    )
            staged = []

    for index, criterion, gate in staged:
        new_criteria[index] = criterion
        new_gates.append(gate)
        existing_gate_ids.add(gate.id)
        reports.append(
            ConvertRowReport(
                wave_id=wave.id,
                criterion_id=criterion.id,
                disposition="converted",
                gate_kind=gate.kind,
            )
        )
    return new_criteria, new_gates, reports


def _convert_one_row(
    wave: Any,
    *,
    position: int,
    criterion: CriterionSpec,
    existing_gate_ids: set[str],
) -> tuple[CriterionSpec, GateSpec] | ConvertRowReport:
    """Attempt one legacy row's conversion, or return its refusal report.

    Honest-refusal legs, in order: a wave with no ``file_scopes`` (the
    falsifying file-grep gate has nowhere to anchor), a converter
    :class:`ValueError`, an EAWF021 measurability finding on the converted
    row, and a gate-id collision with an existing wave gate. The converted
    row keeps its original criterion id so downstream references stay
    stable; the gate id follows the same suffix.

    Args:
        wave: The state wave row under conversion.
        position: 1-based position of *criterion* within the wave.
        criterion: The legacy criterion row.
        existing_gate_ids: Gate ids already present on the wave.

    Returns:
        The staged ``(criterion, gate)`` pair, or the refusal
        :class:`ConvertRowReport`.
    """

    def _refused(reason: str) -> ConvertRowReport:
        return ConvertRowReport(
            wave_id=wave.id,
            criterion_id=criterion.id,
            disposition="refused",
            reason=reason,
        )

    if not wave.file_scopes:
        return _refused("wave has no file_scopes: the falsifying file-grep gate cannot resolve")
    try:
        converted, gate = convert_legacy_criterion(
            criterion.text,
            index=position,
            file_scopes=list(wave.file_scopes),
        )
    except ValueError as exc:
        return _refused(str(exc))
    gate_id = (
        f"GATE-{criterion.id.removeprefix('CR-')}" if criterion.id.startswith("CR-") else gate.id
    )
    converted = converted.model_copy(update={"id": criterion.id, "gate_ids": [gate_id]})
    gate = gate.model_copy(update={"id": gate_id, "criterion_id": criterion.id})
    findings = measure_criteria([converted])
    if findings:
        summary = "; ".join(f"{finding.snippet}: {finding.reason}" for finding in findings)
        return _refused(f"EAWF021 measurability: {summary}")
    if gate.id in existing_gate_ids:
        return _refused(f"gate id collision: {gate.id!r} already exists on the wave")
    return converted, gate


@register("spec.convert_legacy")
async def convert_legacy(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Convert a scope's legacy criterion rows to typed, gated rows.

    The live legacy-to-typed converter (rule 4: the daemon is the canonical
    writer). Every wave under the scope with ``kind == legacy`` criterion
    rows is pushed through
    :func:`~eawf.kernel.spec.common.convert_legacy_criterion` with the
    EAWF021 measurability lint applied per converted row; a row that cannot
    be made measurable is REFUSED and stays legacy with a named reason (no
    silent lossy conversion). Converted rows carry ``kind == converted``
    with a falsifying blocking gate attached.

    Unlike ``spec.sync`` this mutation deliberately accepts NON-PENDING
    waves: the drain corpus is historical (closed) waves whose criteria were
    grandfathered at migration, and retro-typing them never alters their
    text — only the ``kind`` / gate attachment changes.

    Args:
        ctx: Server context. ``ctx.wal_dir`` MUST be configured for a
            non-dry-run call.
        params: JSON-RPC params per :class:`ConvertLegacyParams`.

    Returns:
        Dict matching :class:`SpecConvertLegacyResult` with the per-row
        conversion report; ``dry_run=True`` reports the would-convert set
        with no state write (``before_version`` / ``after_version`` /
        ``envelope`` are ``None``).

    Raises:
        ValueError: When the scope id does not classify, or a wave-kind
            scope names no wave (mapped to ``-32602``).
        DaemonValidationError: When the post-mutation state fails schema /
            invariant validation (mapped to ``-32002``).
    """
    try:
        args = ConvertLegacyParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc
    try:
        kind = spec_writer.classify_scope(args.scope_id)
    except ValueError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    replay = _idempotent_replay(ctx, args.idempotency_key)
    if replay is not None:
        logger.info(f"convert_legacy idempotent_replay scope_id={args.scope_id!r}")
        return replay

    state_path, event_path, wal_path = _resolve_mutator_paths(
        repo_root=args.repo_root,
        ctx=ctx,
    )

    if args.dry_run:
        state, _payload = _read_state(state_path)
        rows: list[ConvertRowReport] = []
        for wave_id in _waves_for_convert_scope(state, args.scope_id, kind):
            _criteria, _gates, reports = _convert_wave_rows(state.waves[wave_id])
            rows.extend(reports)
        return _convert_result(args, rows, before=None, after=None, envelope=None)

    from eawf.runtime.lock import portalock

    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(state_path, timeout=5.0):
            result = _apply_convert_legacy_locked(
                ctx,
                args=args,
                kind=kind,
                state_path=state_path,
                event_path=event_path,
                wal_path=wal_path,
            )
        _cache_replay(ctx, idempotency_key=args.idempotency_key, result=result)
        return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


def _apply_convert_legacy_locked(
    ctx: MethodContext,
    *,
    args: ConvertLegacyParams,
    kind: str,
    state_path: Path,
    event_path: Path,
    wal_path: Path,
) -> dict[str, Any]:
    """Run the locked convert-legacy transaction: convert, validate, write.

    The caller holds the state-path portalock. Mirrors the spec-sync
    transaction (WAL -> atomic write -> event append -> publish); when no
    row converts, nothing is written and the report alone is returned.

    Args:
        ctx: Server context (publish bus).
        args: Validated convert params.
        kind: The classified scope kind.
        state_path: Path to ``state.json``.
        event_path: Path to the event JSONL store.
        wal_path: Path to the daemon WAL directory.

    Returns:
        Dict matching :class:`SpecConvertLegacyResult`.

    Raises:
        ValueError: When a wave-kind scope names no wave (mapped to -32602).
        DaemonValidationError: When the post-mutation state fails schema /
            invariant validation (mapped to -32002).
    """
    state, _payload = _read_state(state_path)
    before_version = _state_version(state.model_dump(mode="json"))
    rows: list[ConvertRowReport] = []
    touched = 0
    for wave_id in _waves_for_convert_scope(state, args.scope_id, kind):
        wave = state.waves[wave_id]
        criteria, gates, reports = _convert_wave_rows(wave)
        rows.extend(reports)
        changed = criteria != list(wave.success_criteria) or gates != list(wave.gates)
        if changed:
            wave.success_criteria = criteria
            wave.gates = gates
            touched += 1

    converted_count = sum(1 for row in rows if row.disposition == "converted")
    if touched == 0:
        return _convert_result(
            args, rows, before=before_version, after=before_version, envelope=None
        )

    state.updated_at = datetime.now(UTC)
    new_payload = state.model_dump(mode="json")
    after_version = _validate_post_sync(new_payload)

    mutation_id = uuid.uuid4().hex
    envelope = _build_convert_legacy_envelope(
        scope_id=args.scope_id,
        converted_count=converted_count,
        refused_count=len(rows) - converted_count,
        waves_touched=touched,
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
        f"convert_legacy ok scope={args.scope_id} converted={converted_count} "
        f"refused={len(rows) - converted_count} waves={touched} "
        f"before={before_version} after={after_version}"
    )
    return _convert_result(
        args,
        rows,
        before=before_version,
        after=after_version,
        envelope=envelope.model_dump(mode="json"),
    )


def _convert_result(
    args: ConvertLegacyParams,
    rows: list[ConvertRowReport],
    *,
    before: str | None,
    after: str | None,
    envelope: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the :class:`SpecConvertLegacyResult` payload dict."""
    converted_count = sum(1 for row in rows if row.disposition == "converted")
    return SpecConvertLegacyResult(
        operation="convert_legacy",
        scope_id=args.scope_id,
        dry_run=args.dry_run,
        converted_count=converted_count,
        refused_count=len(rows) - converted_count,
        rows=[row.model_dump(mode="json") for row in rows],
        before_version=before,
        after_version=after,
        envelope=envelope,
    ).model_dump(mode="json")


def _build_convert_legacy_envelope(
    *,
    scope_id: str,
    converted_count: int,
    refused_count: int,
    waves_touched: int,
    before_version: str,
    after_version: str,
) -> Envelope:
    """Build the canonical event envelope for a convert-legacy mutation."""
    now = datetime.now(UTC)
    summary = (
        f"spec.convert_legacy scope={scope_id} converted={converted_count} "
        f"refused={refused_count} waves={waves_touched}"
    )
    payload = EventPayload(
        timestamp=now,
        event_type="state.mutate.spec_convert_legacy",
        actor="daemon",
        command="spec.convert_legacy",
        args_hash="",
        before_state_version=before_version,
        after_state_version=after_version,
        status="ok",
        message=summary,
        extras={
            "converted_count": converted_count,
            "refused_count": refused_count,
            "waves_touched": waves_touched,
        },
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


__all__ = [
    "ConvertLegacyParams",
    "ConvertRowReport",
    "SpecConvertLegacyResult",
    "convert_legacy",
]
