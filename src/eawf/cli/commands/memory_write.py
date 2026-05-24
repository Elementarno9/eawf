"""Mutating ``eawf memory`` verbs (add / promote / prune / gc / tier / compact).

Split out of :mod:`eawf.cli.commands.memory` (P27-I05-W09). The
:data:`memory_app` Typer group and the shared helpers (store-path
resolvers, the confidence parser, the args-hash helper, the read-only
state loader) live in the parent module; this module attaches the
mutating command bodies via ``@memory_app.command(...)`` and owns the
``--older-than`` ISO-8601 duration parser.

Each mutation handler follows the canonical sequence: load → mutate →
validate → atomic_write (sibling-locked) → append store record →
append event.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Annotated

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.commands.memory import (
    _args_hash,
    _events_path_for,
    _load_state,
    _memory_path_for,
    _resolve_confidence,
    memory_app,
)
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.kernel.state.enums import MemoryStatus, StoreKind

logger = logging.getLogger(__name__)


@memory_app.command("add")
def memory_add(
    ctx: typer.Context,
    scope: Annotated[str, typer.Option("--scope", help="Scope ID anchor.")],
    title: Annotated[str, typer.Option("--title", help="Short title.")],
    body: Annotated[str, typer.Option("--body", help="Memory body text.")],
    confidence: Annotated[
        str | None,
        typer.Option("--confidence", help="One of h/m/l (default medium)."),
    ] = None,
) -> None:
    """Write a new memory entry to ``memory.jsonl`` + ``state.memory_index``."""
    from eawf.cli._mutation import state_transaction
    from eawf.memory.store import add_memory
    from eawf.session.store import append_event

    flags: GlobalFlags = ctx.obj
    try:
        conf = _resolve_confidence(confidence)
        state_path = resolve_state_path(flags.workspace)
        memory_path = _memory_path_for(state_path)
        events_path = _events_path_for(state_path)
        with state_transaction(state_path) as state:
            record = add_memory(
                state=state,
                memory_path=memory_path,
                scope_id=scope,
                title=title,
                body=body,
                confidence=conf,
            )
        append_event(
            events_path=events_path,
            event_id=f"{record.summary.id}-event",
            event_type="memory.add",
            actor="cli",
            command="memory add",
            args_hash=_args_hash({"scope": scope, "title": title, "confidence": conf.value}),
            status="ok",
            message=f"memory added: {record.summary.id}",
            scope_id=scope,
            occurred_at=datetime.now(UTC),
        )
        emit_json_or_text(
            payload={
                "id": record.summary.id,
                "scope_id": scope,
                "confidence": conf.value,
                "summary": record.summary.summary,
            },
            text=f"memory added: {record.summary.id}",
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)


@memory_app.command("promote")
def memory_promote(
    ctx: typer.Context,
    session: Annotated[str, typer.Option("--session", help="Session ID requesting promotion.")],
    source: Annotated[str, typer.Option("--source", help="Source store record ID.")],
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Override scope (defaults to source scope)."),
    ] = None,
    source_kind: Annotated[
        str,
        typer.Option(
            "--source-kind",
            help="Source store filename without .jsonl (e.g. research, memory).",
        ),
    ] = "research",
    confidence: Annotated[
        str | None, typer.Option("--confidence", help="h/m/l (default medium)")
    ] = None,
    to: Annotated[
        str,
        typer.Option(
            "--to",
            help="Promotion target. 'memory' (default) = source record -> memory entry; "
            "'artifact' = memory entry -> durable artifact (Decision in v0.1).",
        ),
    ] = "memory",
    artifact_kind: Annotated[
        str,
        typer.Option(
            "--artifact-kind",
            help="Artifact target kind (only 'decision' is supported in v0.1).",
        ),
    ] = "decision",
    artifact_id: Annotated[
        str | None,
        typer.Option(
            "--artifact-id",
            help="Optional pre-allocated artifact ID; auto-allocated when omitted.",
        ),
    ] = None,
) -> None:
    """Promote a record. ``--to memory`` (default) or ``--to artifact``."""
    from eawf.cli._mutation import state_transaction
    from eawf.kernel.store.paths import store_path
    from eawf.memory.promotion import PromotionError, promote_record
    from eawf.session.store import append_event

    flags: GlobalFlags = ctx.obj
    target = to.strip().lower()
    if target not in {"memory", "artifact"}:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--to must be one of memory|artifact; got {to!r}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    if target == "artifact":
        _memory_promote_to_artifact(
            ctx=ctx,
            session=session,
            source=source,
            source_kind=source_kind,
            artifact_kind=artifact_kind,
            artifact_id=artifact_id,
        )
        return
    try:
        conf = _resolve_confidence(confidence)
        state_path = resolve_state_path(flags.workspace)
        memory_path = _memory_path_for(state_path)
        events_path = _events_path_for(state_path)
        try:
            source_kind_enum = StoreKind(source_kind)
        except ValueError as exc:
            raise cli_errors.UserError(
                f"--source-kind must be one of {[k.value for k in StoreKind]}; got {source_kind!r}",
                kind="InvalidInput",
            ) from exc
        source_path = store_path(state_path, source_kind_enum)
        with state_transaction(state_path) as state:
            if session not in state.agent_sessions:
                raise cli_errors.UserError(
                    f"session {session!r} not in agent_sessions", kind="NotFound"
                )
            try:
                result = promote_record(
                    state=state,
                    source_store_path=source_path,
                    source_id=source,
                    memory_path=memory_path,
                    scope_id=scope,
                    confidence=conf,
                )
            except PromotionError as exc:
                raise cli_errors.UserError(str(exc), kind="NotFound") from exc
        append_event(
            events_path=events_path,
            event_id=f"{result.record.summary.id}-promote",
            event_type="memory.promote",
            actor=session,
            command="memory promote",
            args_hash=_args_hash({"session": session, "source": source, "kind": source_kind}),
            status="ok",
            message=(f"promoted source={source} to memory={result.record.summary.id}"),
            scope_id=result.record.summary.scope_id,
            occurred_at=datetime.now(UTC),
        )
        emit_json_or_text(
            payload={
                "id": result.record.summary.id,
                "scope_id": result.record.summary.scope_id,
                "source_id": source,
                "session": session,
            },
            text=f"memory promoted: {result.record.summary.id} (from {source})",
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)


def _memory_promote_to_artifact(
    *,
    ctx: typer.Context,
    session: str,
    source: str,
    source_kind: str,
    artifact_kind: str,
    artifact_id: str | None,
) -> None:
    """Handle ``eawf memory promote --to artifact``.

    Memory entries (``MEM-…`` IDs) are canonised into a durable
    :class:`~eawf.kernel.state.models.Decision` row. The implementation lives in
    :func:`eawf.memory.promotion.promote_to_artifact`; this CLI shim:

    1. Validates that ``--source-kind memory`` is set (the inverse direction
       requires the source to be a memory entry, not a store record).
    2. Routes errors to the canonical exit codes (3 INVALID_INPUT / 4
       VALIDATION_FAILED / 2 NOT_FOUND).
    3. Emits a ``memory.promote`` event with the artifact ID linked.
    """
    from eawf.cli._mutation import state_transaction
    from eawf.kernel.store.paths import store_path
    from eawf.memory.promotion import PromotionError, promote_to_artifact
    from eawf.session.store import append_event

    flags: GlobalFlags = ctx.obj
    try:
        if source_kind.strip().lower() != StoreKind.MEMORY.value:
            raise cli_errors.UserError(
                f"--to artifact requires --source-kind memory; got {source_kind!r}",
                kind="InvalidInput",
            )
        if artifact_kind.strip().lower() != "decision":
            raise cli_errors.UserError(
                f"--artifact-kind must be 'decision' in v0.1; got {artifact_kind!r}",
                kind="InvalidInput",
            )
        state_path = resolve_state_path(flags.workspace)
        memory_path = _memory_path_for(state_path)
        decisions_path = store_path(state_path, StoreKind.DECISION)
        events_path = _events_path_for(state_path)
        with state_transaction(state_path) as state:
            if session not in state.agent_sessions:
                raise cli_errors.UserError(
                    f"session {session!r} not in agent_sessions", kind="NotFound"
                )
            try:
                result = promote_to_artifact(
                    state=state,
                    memory_path=memory_path,
                    decisions_path=decisions_path,
                    source_id=source,
                    artifact_kind=artifact_kind,
                    artifact_id=artifact_id,
                )
            except PromotionError as exc:
                msg = str(exc)
                if "not in state.memory_index" in msg or "not found" in msg:
                    raise cli_errors.UserError(msg, kind="NotFound") from exc
                raise cli_errors.UserError(msg, kind="InvalidInput") from exc
        append_event(
            events_path=events_path,
            event_id=f"{result.artifact_id}-from-{source}",
            event_type="memory.promote",
            actor=session,
            command="memory promote --to artifact",
            args_hash=_args_hash(
                {
                    "session": session,
                    "source": source,
                    "to": "artifact",
                    "artifact_kind": artifact_kind,
                }
            ),
            status="ok",
            message=(
                f"promoted memory={source} to artifact={result.artifact_id} ({artifact_kind})"
            ),
            scope_id=result.scope_id,
            occurred_at=datetime.now(UTC),
        )
        emit_json_or_text(
            payload={
                "id": result.memory_id,
                "scope_id": result.scope_id,
                "promoted_to_artifact_id": result.artifact_id,
                "artifact_kind": result.artifact_kind,
                "session": session,
            },
            text=(
                f"memory promoted to artifact: {result.memory_id} -> "
                f"{result.artifact_id} ({result.artifact_kind})"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)


@memory_app.command("compact")
def memory_compact(
    ctx: typer.Context,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Filter (no-op currently — compacts whole store)."),
    ] = None,
    budget: Annotated[
        int | None,
        typer.Option("--budget", help="Token budget hint (advisory only)."),
    ] = None,
) -> None:
    """Compact ``memory.jsonl`` (dedup by content; idempotent)."""
    from eawf.kernel.store.compact import compact_store
    from eawf.session.store import append_event

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        memory_path = _memory_path_for(state_path)
        events_path = _events_path_for(state_path)
        report = compact_store(memory_path)
        append_event(
            events_path=events_path,
            event_id=f"memory-compact-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
            event_type="memory.compact",
            actor="cli",
            command="memory compact",
            args_hash=_args_hash({}),
            status="ok",
            message=(
                f"compacted memory.jsonl: in={report.records_in} "
                f"out={report.records_out} dedup={report.dedup_count}"
            ),
            scope_id=scope,
            occurred_at=datetime.now(UTC),
        )
        emit_json_or_text(
            payload={
                "records_in": report.records_in,
                "records_out": report.records_out,
                "dedup_count": report.dedup_count,
            },
            text=(
                f"memory.jsonl compacted: in={report.records_in} "
                f"out={report.records_out} dedup={report.dedup_count}"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)


_ISO_DURATION_RE: re.Pattern[str] = re.compile(
    r"^P(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?$"
)


def _parse_age_days(raw: str | int) -> int:
    """Return an integer day count from *raw* (ISO 8601 duration or int).

    Supported shapes:

    - bare integer (``"30"`` or ``30``) → that many days.
    - ISO 8601 duration ``P<n>D``, ``P<n>W``, ``P<n>M``, ``P<n>Y`` (or
      combinations such as ``P1M15D``). Months and years use 30/365 day
      approximations because v0.1 has no calendar engine.

    Raises:
        UserError: Empty, negative, or malformed input (``kind="InvalidInput"``).
    """
    if isinstance(raw, int):
        if raw < 0:
            raise cli_errors.UserError(f"--older-than must be >= 0; got {raw}", kind="InvalidInput")
        return raw
    text = raw.strip()
    if not text:
        raise cli_errors.UserError("--older-than must not be empty", kind="InvalidInput")
    if text.isdigit():
        return int(text)
    match = _ISO_DURATION_RE.match(text)
    if match is None:
        raise cli_errors.UserError(
            f"--older-than {text!r} is not a valid ISO 8601 duration "
            "(expected like P30D, P3M, P1Y) or integer day count",
            kind="InvalidInput",
        )
    parts = {k: int(v) if v is not None else 0 for k, v in match.groupdict().items()}
    days = parts["days"] + 7 * parts["weeks"] + 30 * parts["months"] + 365 * parts["years"]
    if days <= 0 and text != "P0D":
        raise cli_errors.UserError(
            f"--older-than {text!r} resolved to 0 days; specify a positive duration",
            kind="InvalidInput",
        )
    return days


@memory_app.command("prune")
def memory_prune(
    ctx: typer.Context,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Filter by scope ID."),
    ] = None,
    older_than: Annotated[
        str,
        typer.Option(
            "--older-than",
            help="Age threshold (ISO 8601 duration like P30D, or integer days). Default P30D.",
        ),
    ] = "P30D",
    status: Annotated[
        str,
        typer.Option(
            "--status",
            help="Status filter — only entries currently in this status are pruned. "
            "Default 'stale'. Use 'active' (with --no-input) to prune live entries.",
        ),
    ] = "stale",
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Report which IDs would flip without mutating state or jsonl.",
        ),
    ] = False,
) -> None:
    """Soft-delete prune. Flips status to PRUNED; preserves the prior record."""
    from eawf.cli._mutation import state_transaction
    from eawf.memory.prune import PruneError, prune_memory
    from eawf.session.store import append_event

    flags: GlobalFlags = ctx.obj
    try:
        try:
            age_days = _parse_age_days(older_than)
        except cli_errors.CliError:
            raise
        try:
            status_filter = MemoryStatus(status.strip().lower())
        except ValueError as exc:
            raise cli_errors.UserError(
                f"--status must be one of {[s.value for s in MemoryStatus]}; got {status!r}",
                kind="InvalidInput",
            ) from exc
        if status_filter == MemoryStatus.ACTIVE and not flags.no_input:
            raise cli_errors.UserError(
                "--status active requires --no-input (or an explicit confirm) — "
                "pruning live entries is irreversible without compaction.",
                kind="UserDeclined",
            )
        state_path = resolve_state_path(flags.workspace)
        memory_path = _memory_path_for(state_path)
        events_path = _events_path_for(state_path)

        if dry_run:
            # Read-only path: load state, run prune in dry-run mode, emit
            # report. No state-transaction needed because nothing is written.
            state_ro = _load_state(state_path)
            try:
                result = prune_memory(
                    state=state_ro,
                    memory_path=memory_path,
                    age_days=age_days,
                    status_filter=status_filter,
                    scope_id=scope,
                    dry_run=True,
                )
            except PruneError as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            payload = {
                "pruned_ids": result.pruned_ids,
                "skipped_ids": result.skipped_ids,
                "dry_run": True,
                "older_than_days": result.older_than_days,
                "scope_id": result.scope_id,
                "status_filter": status_filter.value,
            }
            text = (
                f"would prune: {len(result.pruned_ids)} entries "
                f"(scope={scope}, older_than_days={age_days}, status={status_filter.value})"
            )
            emit_json_or_text(payload=payload, text=text, flags=flags)
            return

        with state_transaction(state_path) as state:
            try:
                result = prune_memory(
                    state=state,
                    memory_path=memory_path,
                    age_days=age_days,
                    status_filter=status_filter,
                    scope_id=scope,
                    dry_run=False,
                )
            except PruneError as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
        append_event(
            events_path=events_path,
            event_id=f"memory-prune-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
            event_type="memory.prune",
            actor="cli",
            command="memory prune",
            args_hash=_args_hash(
                {
                    "scope": scope,
                    "older_than_days": age_days,
                    "status": status_filter.value,
                }
            ),
            status="ok",
            message=(
                f"pruned {len(result.pruned_ids)} entries "
                f"(scope={scope}, older_than_days={age_days})"
            ),
            scope_id=scope,
            occurred_at=datetime.now(UTC),
        )
        payload = {
            "pruned_ids": result.pruned_ids,
            "skipped_ids": result.skipped_ids,
            "dry_run": False,
            "older_than_days": result.older_than_days,
            "scope_id": result.scope_id,
            "status_filter": status_filter.value,
        }
        text = (
            f"pruned {len(result.pruned_ids)} entries "
            f"(scope={scope}, older_than_days={age_days}, status={status_filter.value})"
        )
        emit_json_or_text(payload=payload, text=text, flags=flags)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)


@memory_app.command("gc")
def memory_gc(
    ctx: typer.Context,
    threshold_days: Annotated[
        int,
        typer.Option(
            "--threshold-days",
            help="Age threshold in days; STALE entries older than this flip tier to ARCHIVAL.",
        ),
    ] = 30,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Report which IDs would archive without mutating state.",
        ),
    ] = False,
) -> None:
    """Archive matched memory entries by flipping their ``tier`` to ARCHIVAL."""
    from eawf.cli._mutation import state_transaction
    from eawf.memory.gc import GcError, gc_memory
    from eawf.session.store import append_event

    flags: GlobalFlags = ctx.obj
    try:
        if threshold_days < 0:
            raise cli_errors.UserError(
                f"--threshold-days must be >= 0; got {threshold_days}", kind="InvalidInput"
            )
        state_path = resolve_state_path(flags.workspace)
        memory_path = _memory_path_for(state_path)
        events_path = _events_path_for(state_path)

        if dry_run:
            state_ro = _load_state(state_path)
            try:
                report = gc_memory(
                    state=state_ro,
                    memory_path=memory_path,
                    threshold_days=threshold_days,
                    dry_run=True,
                )
            except GcError as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            payload = {
                "archived_ids": report.archived_ids,
                "skipped_ids": report.skipped_ids,
                "dry_run": True,
                "threshold_days": report.threshold_days,
            }
            text = (
                f"would archive {len(report.archived_ids)} entries "
                f"(threshold_days={threshold_days})"
            )
            emit_json_or_text(payload=payload, text=text, flags=flags)
            return

        with state_transaction(state_path) as state:
            try:
                report = gc_memory(
                    state=state,
                    memory_path=memory_path,
                    threshold_days=threshold_days,
                    dry_run=False,
                )
            except GcError as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
        append_event(
            events_path=events_path,
            event_id=f"memory-gc-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
            event_type="memory.gc",
            actor="cli",
            command="memory gc",
            args_hash=_args_hash({"threshold_days": threshold_days}),
            status="ok",
            message=(
                f"archived {len(report.archived_ids)} entries (threshold_days={threshold_days})"
            ),
            scope_id=None,
            occurred_at=datetime.now(UTC),
        )
        payload = {
            "archived_ids": report.archived_ids,
            "skipped_ids": report.skipped_ids,
            "dry_run": False,
            "threshold_days": report.threshold_days,
        }
        text = f"archived {len(report.archived_ids)} entries (threshold_days={threshold_days})"
        emit_json_or_text(payload=payload, text=text, flags=flags)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)


@memory_app.command("tier")
def memory_tier(
    ctx: typer.Context,
    mem_id: Annotated[str, typer.Argument(help="Memory entry ID, e.g. MEM-...")],
    tier: Annotated[
        str,
        typer.Option(
            "--tier",
            help="Target tier: working / archival / retrieval.",
        ),
    ],
) -> None:
    """Set the tier on a single memory entry."""
    from eawf.cli._mutation import state_transaction
    from eawf.kernel.state.enums import MemoryTier
    from eawf.session.store import append_event

    flags: GlobalFlags = ctx.obj
    try:
        try:
            target_tier = MemoryTier(tier.strip().lower())
        except ValueError as exc:
            raise cli_errors.UserError(
                f"--tier must be one of {[t.value for t in MemoryTier]}; got {tier!r}",
                kind="InvalidInput",
            ) from exc
        state_path = resolve_state_path(flags.workspace)
        events_path = _events_path_for(state_path)
        with state_transaction(state_path) as state:
            index = state.memory_index or {}
            summary = index.get(mem_id)
            if summary is None:
                raise cli_errors.UserError(f"memory entry not found: {mem_id}", kind="NotFound")
            prior = summary.tier
            index[mem_id] = summary.model_copy(update={"tier": target_tier})
            state.memory_index = index
        append_event(
            events_path=events_path,
            event_id=f"memory-tier-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
            event_type="memory.tier",
            actor="cli",
            command="memory tier",
            args_hash=_args_hash({"id": mem_id, "tier": target_tier.value}),
            status="ok",
            message=f"tier {prior.value} -> {target_tier.value} for {mem_id}",
            scope_id=summary.scope_id,
            occurred_at=datetime.now(UTC),
        )
        emit_json_or_text(
            payload={
                "id": mem_id,
                "tier": target_tier.value,
                "prior_tier": prior.value,
            },
            text=f"memory {mem_id} tier: {prior.value} -> {target_tier.value}",
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)
