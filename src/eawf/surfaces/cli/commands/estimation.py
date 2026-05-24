"""Typer sub-apps for ``eawf estimate`` and ``eawf actual`` per the v0.1 EU spec.

This module exposes two Typer groups:

- :data:`estimate_app` — bound to ``eawf estimate`` (with ``estimate update``).
- :data:`actual_app` — bound to ``eawf actual`` (``start`` / ``stop`` / ``recover``).

Both groups follow the cross-cutting Phase 2 mutation pattern:

    with portalock.acquire(state_path):
        state = load_state(path)
        ...mutate...
        validate_state(state, strict_optional=False)
        atomic_write_json(path, state.model_dump(mode="json"))
        store.append(envelope)
        events.append(event)

Defaults for the ``estimation`` config block come from the v0.1 EU spec since
``eawf config`` (Phase 2 W06) is not yet wired. When W06 lands, this module
will read the merged config instead of the hard-coded defaults below.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import orjson
import typer

from eawf.kernel.state.enums import ActualStatus, Confidence, StoreKind
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.runtime.lock import portalock
from eawf.runtime.lock.stale import is_stale
from eawf.surfaces.cli import errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.kernel.store.kinds.actual import ActualPayload

logger = logging.getLogger(__name__)

# ---- Defaults from the v0.1 EU spec (until P02-W06 lands config) -----------------

DEFAULT_EU_MINUTES: Decimal = Decimal("30")
DEFAULT_CENTRAL_MULTIPLIER: Decimal = Decimal("0.50")
DEFAULT_PESSIMISTIC_MULTIPLIER: Decimal = Decimal("1.8")
DEFAULT_EU_QUANTUM: Decimal = Decimal("0.25")
DEFAULT_REFERENCE_CLASS: str = "core_swe"
DEFAULT_COEFFICIENTS_PROFILE: str = "eawf_v0_lockbox_2026_05"
DEFAULT_RAW_MINUTES: Decimal = Decimal("60")  # placeholder until P03 wires user-supplied raw

CONFIDENCE_LITERAL = {"h": Confidence.HIGH, "m": Confidence.MEDIUM, "l": Confidence.LOW}


# ---- Typer groups ------------------------------------------------------------

estimate_app = typer.Typer(
    name="estimate",
    help="Create or update EU estimates for a scope.",
    no_args_is_help=True,
)

actual_app = typer.Typer(
    name="actual",
    help="Open / close / recover actual segments for a scope.",
    no_args_is_help=True,
)


# ---- Helpers ----------------------------------------------------------------


def _coerce_confidence(raw: str | None, *, default: Confidence = Confidence.MEDIUM) -> Confidence:
    """Convert ``--confidence h|m|l`` to a :class:`Confidence` value.

    Accepts the long form (``high``/``medium``/``low``) too. ``None`` returns
    *default*. Anything else raises :class:`errors.UserError`
    (``kind="InvalidInput"``).
    """
    if raw is None:
        return default
    normalised = raw.lower().strip()
    if normalised in CONFIDENCE_LITERAL:
        return CONFIDENCE_LITERAL[normalised]
    try:
        return Confidence(normalised)
    except ValueError as exc:
        raise errors.UserError(
            f"--confidence must be h/m/l (or high/medium/low); got {raw!r}", kind="InvalidInput"
        ) from exc


def _load_state(path: Path) -> State:
    """Read and validate ``state.json``; convert errors to CLI exit codes."""
    from eawf.kernel.validate.strict import validate_state

    if not path.exists():
        raise errors.UserError(f"state file not found: {path}", kind="NotFound")
    raw = path.read_bytes()
    payload = orjson.loads(raw)
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise errors.ValidationError("state schema invalid: " + "; ".join(report.schema_errors[:3]))
    if report.violations:
        # Pre-existing invariant violations are surfaced but not fatal — the
        # CLI continues on a best-effort basis. Strict validation is reserved
        # for ``eawf validate``.
        logger.warning(
            f"_load_state pre-existing-violations count={len(report.violations)}; "
            "continuing on best-effort basis"
        )
    return report.state


def _commit_state(
    state: State,
    *,
    state_path: Path,
) -> None:
    """Validate and atomically persist *state*.

    Raises :class:`errors.ValidationError` on schema/invariant errors so the
    mutation never lands on disk in an invalid form.
    """
    from eawf.kernel.validate.strict import validate_state

    payload = state.model_dump(mode="json")
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise errors.ValidationError(
            "post-mutation schema invalid: " + "; ".join(report.schema_errors[:3])
        )
    if report.violations:
        codes = sorted({v.code for v in report.violations})
        raise errors.ValidationError(f"post-mutation invariant violations: {', '.join(codes)}")
    atomic_write_json_locked(state_path, payload)


def _emit_event(
    *,
    state_path: Path,
    event_type: str,
    scope_id: str,
    command: str,
    actor: str,
    status: str,
    message: str,
    occurred_at: datetime,
) -> None:
    """Append an event record to the canonical events store for audit/provenance."""
    from eawf.kernel.store.append import append_envelope as _append_jsonl
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.event import EventPayload
    from eawf.kernel.store.paths import store_path

    envelope = Envelope(
        id=f"EVT-{event_type}-{occurred_at.strftime('%Y%m%dT%H%M%SZ')}-{scope_id}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=occurred_at,
        updated_at=None,
        summary=f"{event_type} {scope_id} -> {status}",
        payload=EventPayload(
            timestamp=occurred_at,
            event_type=event_type,
            actor=actor,
            command=command,
            args_hash="",
            status=status,
            message=message,
        ).model_dump(mode="json"),
    )
    _append_jsonl(store_path(state_path, StoreKind.EVENT), envelope)


# ---- estimate ----------------------------------------------------------------
#
# NOTE on Typer/Click constraints: a Typer group cannot accept a positional
# argument *and* dispatch to subcommands without ambiguity (the parser would
# need to decide whether the first token is the positional or the subcommand
# name). The proposal's matrix shape ``eawf estimate <scope>`` is therefore
# rendered as the subcommand ``eawf estimate set <scope>`` here. ``update``
# remains a sibling subcommand. Operator-facing UX is preserved because both
# paths are equally explicit and the help text spells them out.


@estimate_app.command("set")
def estimate_set(
    ctx: typer.Context,
    scope: Annotated[str, typer.Argument(help="Scope ID (e.g. P01-I01-W01).")],
    source: Annotated[
        str | None,
        typer.Option("--source", help="Estimate source label (e.g. prep, replan, ...)."),
    ] = None,
    confidence: Annotated[
        str | None,
        typer.Option("--confidence", "-c", help="Confidence h/m/l."),
    ] = None,
    reference_class: Annotated[
        str | None,
        typer.Option("--reference-class", help="Reference class id from coefficients."),
    ] = None,
) -> None:
    """Create (or replace) the estimate for *scope*."""
    flags: GlobalFlags = ctx.obj
    _do_estimate(
        ctx,
        scope=scope,
        source=source or "prep",
        confidence_raw=confidence,
        reference_class=reference_class or DEFAULT_REFERENCE_CLASS,
        flags=flags,
        update=False,
    )


@estimate_app.command("update")
def estimate_update(
    ctx: typer.Context,
    scope: Annotated[str, typer.Argument(help="Scope ID (e.g. P01-I01-W01).")],
    source: Annotated[
        str,
        typer.Option("--source", help="Estimate source label (required for updates)."),
    ],
    confidence: Annotated[
        str | None,
        typer.Option("--confidence", "-c", help="Confidence h/m/l."),
    ] = None,
) -> None:
    """Update the estimate for *scope*, replacing the current summary record."""
    flags: GlobalFlags = ctx.obj
    _do_estimate(
        ctx,
        scope=scope,
        source=source,
        confidence_raw=confidence,
        reference_class=DEFAULT_REFERENCE_CLASS,
        flags=flags,
        update=True,
    )


def _do_estimate(
    ctx: typer.Context,
    *,
    scope: str,
    source: str,
    confidence_raw: str | None,
    reference_class: str,
    flags: GlobalFlags,
    update: bool,
) -> None:
    """Shared implementation for ``estimate`` and ``estimate update``."""
    from eawf.kernel.state.models import EstimateSummary
    from eawf.kernel.store.append import append_envelope as _append_jsonl
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.estimate import EstimatePayload
    from eawf.kernel.store.paths import store_path
    from eawf.workflow.estimation.eu import expected_eu as calc_expected_eu
    from eawf.workflow.estimation.eu import pessimistic_eu as calc_pessimistic_eu
    from eawf.workflow.estimation.eu import quantize, render_display

    try:
        confidence = _coerce_confidence(confidence_raw)
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return  # unreachable — emit_error raises typer.Exit

    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        errors.emit_error(errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return

    try:
        with portalock.acquire(state_path):
            state = _load_state(state_path)
            if update and (state.estimates is None or scope not in state.estimates):
                raise errors.UserError(
                    f"no estimate exists for scope {scope!r}; use `eawf estimate set`",
                    kind="NotFound",
                )

            now = datetime.now(UTC)
            expected = quantize(
                calc_expected_eu(
                    DEFAULT_RAW_MINUTES,
                    DEFAULT_CENTRAL_MULTIPLIER,
                    DEFAULT_EU_MINUTES,
                ),
                DEFAULT_EU_QUANTUM,
            )
            pessimistic = quantize(
                calc_pessimistic_eu(
                    DEFAULT_RAW_MINUTES, DEFAULT_PESSIMISTIC_MULTIPLIER, DEFAULT_EU_MINUTES
                ),
                DEFAULT_EU_QUANTUM,
            )
            expected_minutes = expected * DEFAULT_EU_MINUTES
            pessimistic_minutes = pessimistic * DEFAULT_EU_MINUTES
            display = render_display(expected, pessimistic, eu_minutes=DEFAULT_EU_MINUTES)

            estimate_id = f"EST-{scope}"
            store_record_id = f"{estimate_id}-{now.strftime('%Y%m%dT%H%M%SZ')}"

            summary = EstimateSummary(
                id=estimate_id,
                scope_id=scope,
                expected_eu=float(expected),
                pessimistic_eu=float(pessimistic),
                expected_minutes=float(expected_minutes),
                pessimistic_minutes=float(pessimistic_minutes),
                display=display,
                reference_class=reference_class,
                confidence=confidence,
                current_store_record_id=store_record_id,
                updated_at=now,
            )
            estimates: dict[str, EstimateSummary] = dict(state.estimates or {})
            estimates[scope] = summary
            state = state.model_copy(update={"estimates": estimates, "updated_at": now})

            # Atomicity ordering: append the audit-trail JSONL records
            # BEFORE mutating state.json. A crash between the JSONL write
            # and the state.json write leaves an envelope with no state
            # mutation (recoverable / replayable) rather than a state
            # mutation that references a non-existent envelope.
            envelope = Envelope(
                id=store_record_id,
                kind=StoreKind.ESTIMATE,
                scope_id=scope,
                created_at=now,
                updated_at=now,
                summary=display,
                payload=EstimatePayload(
                    scope_type="wave",
                    source=source,
                    grain="known_wave",
                    expected_eu=float(expected),
                    pessimistic_eu=float(pessimistic),
                    expected_minutes=float(expected_minutes),
                    pessimistic_minutes=float(pessimistic_minutes),
                    display=display,
                    display_category="",
                    reference_class=reference_class,
                    confidence=confidence,
                    basis=[],
                    coefficients_profile=DEFAULT_COEFFICIENTS_PROFILE,
                ).model_dump(mode="json"),
            )
            _append_jsonl(store_path(state_path, StoreKind.ESTIMATE), envelope)
            _emit_event(
                state_path=state_path,
                event_type="estimate.updated" if update else "estimate.created",
                scope_id=scope,
                command="estimate update" if update else "estimate",
                actor="cli",
                status="ok",
                message=display,
                occurred_at=now,
            )
            _commit_state(state, state_path=state_path)

    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    except portalock.LockTimeout as exc:
        errors.emit_error(errors.StateConflict(str(exc), kind="LockConflict"), flags=flags)
        return

    payload: dict[str, Any] = {
        "ok": True,
        "scope": scope,
        "estimate_id": estimate_id,
        "expected_eu": float(expected),
        "pessimistic_eu": float(pessimistic),
        "display": display,
        "store_record_id": store_record_id,
    }
    emit_json_or_text(payload, f"estimate {scope}: {display}", flags=flags)


# ---- actual ------------------------------------------------------------------


@actual_app.command("start")
def actual_start(
    ctx: typer.Context,
    scope: Annotated[str, typer.Argument(help="Scope ID (e.g. P01-I01-W01).")],
    session: Annotated[
        str,
        typer.Option("--session", help="Session id holding the segment open."),
    ],
) -> None:
    """Open a new actual segment for ``(scope, session)``.

    Rejects with :data:`exit_codes.VALIDATION_FAILED` when a segment is already
    open for the same ``(scope, session)`` pair — this matches the
    *audit-evidence-style* invariant guarding actuals integrity.
    """
    from eawf.kernel.state.models import ActualSummary
    from eawf.kernel.store.append import append_envelope as _append_jsonl
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.actual import ActualPayload
    from eawf.kernel.store.paths import store_path
    from eawf.workflow.estimation.segments import is_open_for, open_segment

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        errors.emit_error(errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return

    try:
        with portalock.acquire(state_path):
            state = _load_state(state_path)
            now = datetime.now(UTC)
            actuals: dict[str, ActualSummary] = dict(state.actuals or {})
            existing = actuals.get(scope)

            previous_segments: list[Any] = []
            if existing is not None:
                payload = _read_latest_actual_payload(
                    store_path(state_path, StoreKind.ACTUAL),
                    record_id=existing.current_store_record_id,
                )
                if payload is not None:
                    if is_open_for(payload.segments, session_id=session):
                        raise errors.ValidationError(
                            f"actual segment already open for scope={scope!r} session={session!r}"
                        )
                    previous_segments = list(payload.segments)

            new_segment = open_segment(session_id=session, started_at=now)
            actual_id = existing.id if existing is not None else f"ACT-{scope}"
            # Microsecond + 4-hex-nonce suffix avoids ms-collision when two
            # mutations on the same scope land in the same millisecond — the
            # outer portalock makes that rare in-process but the format itself
            # is the durable guarantee.
            now_us = int(now.timestamp() * 1_000_000)
            nonce = secrets.token_hex(2)
            store_record_id = f"{actual_id}-{now_us}-{nonce}"

            new_payload = ActualPayload(
                segments=[*previous_segments, new_segment],
                elapsed_eu=float(sum((s.eu for s in previous_segments), 0.0)),
                attention_eu=None,
                agent_runtime_eu=None,
                ratio_actual_over_estimate=None,
                inside_pessimistic=None,
                calibration_eligible=False,
                outcome="active",
                idle_policy="D30_non_agent_gap",
            )

            summary = ActualSummary(
                id=actual_id,
                scope_id=scope,
                status=ActualStatus.ACTIVE,
                elapsed_eu=new_payload.elapsed_eu,
                attention_eu=None,
                agent_runtime_eu=None,
                current_store_record_id=store_record_id,
                updated_at=now,
            )
            actuals[scope] = summary
            state = state.model_copy(update={"actuals": actuals, "updated_at": now})

            # Atomicity ordering: jsonl-first, then state.json. A crash
            # mid-flow leaves an audit envelope without a state mutation
            # rather than a state.summary referencing a missing record.
            envelope = Envelope(
                id=store_record_id,
                kind=StoreKind.ACTUAL,
                scope_id=scope,
                created_at=now,
                updated_at=now,
                summary=f"actual segment opened for {session}",
                payload=new_payload.model_dump(mode="json"),
            )
            _append_jsonl(store_path(state_path, StoreKind.ACTUAL), envelope)
            _emit_event(
                state_path=state_path,
                event_type="actual.start",
                scope_id=scope,
                command="actual start",
                actor=session,
                status="ok",
                message=f"segment opened at {now.isoformat()}",
                occurred_at=now,
            )
            _commit_state(state, state_path=state_path)

    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    except portalock.LockTimeout as exc:
        errors.emit_error(errors.StateConflict(str(exc), kind="LockConflict"), flags=flags)
        return

    out_payload: dict[str, Any] = {
        "ok": True,
        "scope": scope,
        "session": session,
        "actual_id": actual_id,
        "store_record_id": store_record_id,
        "started_at": now.isoformat(),
    }
    emit_json_or_text(
        out_payload,
        f"actual start {scope} session={session}",
        flags=flags,
    )


@actual_app.command("stop")
def actual_stop(
    ctx: typer.Context,
    scope: Annotated[str, typer.Argument(help="Scope ID (e.g. P01-I01-W01).")],
    status: Annotated[
        str,
        typer.Option(
            "--status",
            help="closed|abandoned (default: closed -> ActualStatus.DONE).",
        ),
    ] = "closed",
) -> None:
    """Close the latest open segment for *scope* and write the elapsed EU."""
    from eawf.kernel.state.models import ActualSummary
    from eawf.kernel.store.append import append_envelope as _append_jsonl
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.paths import store_path
    from eawf.workflow.estimation.segments import close_segment, latest_open_segment

    flags: GlobalFlags = ctx.obj

    try:
        if status == "closed":
            close_status = ActualStatus.DONE
        elif status == "abandoned":
            close_status = ActualStatus.ABANDONED
        else:
            try:
                close_status = ActualStatus(status)
            except ValueError as exc:
                raise errors.UserError(
                    f"--status must be 'closed' or 'abandoned' (got {status!r})",
                    kind="InvalidInput",
                ) from exc

        try:
            state_path = resolve_state_path(flags.workspace)
        except FileNotFoundError as exc:
            raise errors.UserError(str(exc), kind="NotFound") from exc

        with portalock.acquire(state_path):
            state = _load_state(state_path)
            actuals: dict[str, ActualSummary] = dict(state.actuals or {})
            existing = actuals.get(scope)
            if existing is None:
                raise errors.UserError(f"no active actual for scope {scope!r}", kind="NotFound")

            payload = _read_latest_actual_payload(
                store_path(state_path, StoreKind.ACTUAL),
                record_id=existing.current_store_record_id,
            )
            if payload is None:
                raise errors.UserError(
                    f"actuals.jsonl missing record {existing.current_store_record_id!r}",
                    kind="NotFound",
                )

            open_seg = latest_open_segment(payload.segments)
            if open_seg is None:
                raise errors.ValidationError(f"no open segment to close for scope {scope!r}")

            now = datetime.now(UTC)
            closed = close_segment(
                open_seg,
                ended_at=now,
                eu_minutes=DEFAULT_EU_MINUTES,
                status=close_status,
            )
            new_segments = [closed if seg is open_seg else seg for seg in payload.segments]
            new_total_eu = sum(s.eu for s in new_segments)
            new_payload = payload.model_copy(
                update={
                    "segments": new_segments,
                    "elapsed_eu": float(new_total_eu),
                    "outcome": close_status.value,
                }
            )

            now_us = int(now.timestamp() * 1_000_000)
            nonce = secrets.token_hex(2)
            new_record_id = f"{existing.id}-{now_us}-{nonce}"
            summary = existing.model_copy(
                update={
                    "status": close_status,
                    "elapsed_eu": float(new_total_eu),
                    "current_store_record_id": new_record_id,
                    "updated_at": now,
                }
            )
            actuals[scope] = summary
            state = state.model_copy(update={"actuals": actuals, "updated_at": now})

            # Atomicity ordering: jsonl-first, then state.json. A crash
            # mid-flow leaves an audit envelope without a state mutation
            # rather than a state.summary referencing a missing record.
            envelope = Envelope(
                id=new_record_id,
                kind=StoreKind.ACTUAL,
                scope_id=scope,
                created_at=now,
                updated_at=now,
                summary=f"actual segment {close_status.value} for {open_seg.session_id}",
                payload=new_payload.model_dump(mode="json"),
            )
            _append_jsonl(store_path(state_path, StoreKind.ACTUAL), envelope)
            _emit_event(
                state_path=state_path,
                event_type="actual.stop",
                scope_id=scope,
                command="actual stop",
                actor=open_seg.session_id,
                status="ok",
                message=f"elapsed_eu={new_total_eu:.4f}",
                occurred_at=now,
            )
            _commit_state(state, state_path=state_path)

    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    except portalock.LockTimeout as exc:
        errors.emit_error(errors.StateConflict(str(exc), kind="LockConflict"), flags=flags)
        return

    out_payload: dict[str, Any] = {
        "ok": True,
        "scope": scope,
        "status": close_status.value,
        "elapsed_eu": float(new_total_eu),
        "session": open_seg.session_id,
        "ended_at": now.isoformat(),
    }
    emit_json_or_text(
        out_payload,
        f"actual stop {scope}: status={close_status.value} elapsed_eu={new_total_eu:.4f}",
        flags=flags,
    )


@actual_app.command("recover")
def actual_recover(
    ctx: typer.Context,
    scope: Annotated[
        str | None,
        typer.Argument(help="Optional scope filter; recover only this scope."),
    ] = None,
) -> None:
    """Walk active actuals and abandon any segment held by a stale lock holder.

    The cap on ``elapsed_eu`` is :data:`STALE_HEARTBEAT_SECONDS` so a crashed
    overnight session does not record an inflated wall-clock interval.
    """
    from eawf.kernel.state.models import ActualSummary
    from eawf.kernel.store.append import append_envelope as _append_jsonl
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.paths import store_path
    from eawf.workflow.estimation.recovery import cap_elapsed
    from eawf.workflow.estimation.segments import latest_open_segment

    flags: GlobalFlags = ctx.obj
    recovered: list[dict[str, Any]] = []

    try:
        try:
            state_path = resolve_state_path(flags.workspace)
        except FileNotFoundError as exc:
            raise errors.UserError(str(exc), kind="NotFound") from exc

        with portalock.acquire(state_path):
            state = _load_state(state_path)
            actuals_in: dict[str, ActualSummary] = dict(state.actuals or {})
            actuals_out: dict[str, ActualSummary] = dict(actuals_in)

            now = datetime.now(UTC)
            for scope_id, summary in actuals_in.items():
                if summary.status != ActualStatus.ACTIVE:
                    continue
                if scope is not None and scope_id != scope:
                    continue
                lock_path = state_path.parent / "locks" / f"actual-{scope_id}.lock"
                if not is_stale(lock_path):
                    continue

                payload = _read_latest_actual_payload(
                    store_path(state_path, StoreKind.ACTUAL),
                    record_id=summary.current_store_record_id,
                )
                if payload is None:
                    continue
                open_seg = latest_open_segment(payload.segments)
                if open_seg is None:
                    continue

                capped_ended_at, elapsed_eu = cap_elapsed(
                    open_seg.started_at,
                    now=now,
                    eu_minutes=DEFAULT_EU_MINUTES,
                )
                closed = open_seg.model_copy(
                    update={
                        "ended_at": capped_ended_at,
                        "eu": float(elapsed_eu),
                        "active_minutes": float(
                            (capped_ended_at - open_seg.started_at).total_seconds() / 60.0
                        ),
                        "agent_runtime_minutes": float(
                            (capped_ended_at - open_seg.started_at).total_seconds() / 60.0
                        ),
                        "status": ActualStatus.ABANDONED,
                    }
                )
                new_segments = [closed if seg is open_seg else seg for seg in payload.segments]
                new_total_eu = sum(s.eu for s in new_segments)
                new_payload = payload.model_copy(
                    update={
                        "segments": new_segments,
                        "elapsed_eu": float(new_total_eu),
                        "outcome": "abandoned",
                    }
                )

                now_us = int(now.timestamp() * 1_000_000)
                nonce = secrets.token_hex(2)
                new_record_id = f"{summary.id}-{now_us}-{nonce}"
                actuals_out[scope_id] = summary.model_copy(
                    update={
                        "status": ActualStatus.ABANDONED,
                        "elapsed_eu": float(new_total_eu),
                        "current_store_record_id": new_record_id,
                        "updated_at": now,
                    }
                )

                envelope = Envelope(
                    id=new_record_id,
                    kind=StoreKind.ACTUAL,
                    scope_id=scope_id,
                    created_at=now,
                    updated_at=now,
                    summary=f"actual segment abandoned (recover) for {open_seg.session_id}",
                    payload=new_payload.model_dump(mode="json"),
                )
                _append_jsonl(store_path(state_path, StoreKind.ACTUAL), envelope)
                _emit_event(
                    state_path=state_path,
                    event_type="actual.recover",
                    scope_id=scope_id,
                    command="actual recover",
                    actor=open_seg.session_id,
                    status="ok",
                    message=f"capped_elapsed_eu={float(elapsed_eu):.4f}",
                    occurred_at=now,
                )
                recovered.append(
                    {
                        "scope": scope_id,
                        "session": open_seg.session_id,
                        "elapsed_eu": float(elapsed_eu),
                        "capped_ended_at": capped_ended_at.isoformat(),
                    }
                )

            if actuals_out != actuals_in:
                state = state.model_copy(update={"actuals": actuals_out, "updated_at": now})
                _commit_state(state, state_path=state_path)

    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    except portalock.LockTimeout as exc:
        errors.emit_error(errors.StateConflict(str(exc), kind="LockConflict"), flags=flags)
        return

    out_payload: dict[str, Any] = {
        "ok": True,
        "recovered_count": len(recovered),
        "recovered": recovered,
    }
    emit_json_or_text(
        out_payload,
        f"actual recover: {len(recovered)} segment(s) abandoned",
        flags=flags,
    )


# ---- helpers re-used across actual commands ----------------------------------


def _read_latest_actual_payload(
    jsonl_path: Path,
    *,
    record_id: str,
) -> ActualPayload | None:
    """Return the latest ``ActualPayload`` for *record_id* in *jsonl_path*.

    Compaction-aware: when multiple envelopes share the same id, the *last*
    line wins (matches :func:`eawf.kernel.store.compact.compact_store` semantics).
    """
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.actual import ActualPayload

    if not jsonl_path.exists():
        return None
    last_payload: ActualPayload | None = None
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        env = Envelope.model_validate_json(line)
        if env.id != record_id or env.kind != StoreKind.ACTUAL:
            continue
        last_payload = ActualPayload.model_validate(env.payload)
    return last_payload
