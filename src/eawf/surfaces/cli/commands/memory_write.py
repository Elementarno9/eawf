"""Mutating ``eawf memory`` verbs (add / promote / prune / gc / tier / compact).

Split out of :mod:`eawf.surfaces.cli.commands.memory`. The
:data:`memory_app` Typer group and the shared helpers (store-path
resolvers, the confidence parser, the args-hash helper, the native RPC
helper, the note loader) live in the parent module; this module attaches
the mutating command bodies via ``@memory_app.command(...)`` and owns the
``--older-than`` ISO-8601 duration parser.

Each note-writing handler is dispatch only: it parses its flags, sends one
native ``memory.*`` verb, and renders the answer. The daemon reads the
generation's memory ledger under its locks, decides the revisions, and
commits each as a ledger line. A ``--dry-run`` reads the ledger here and
writes nothing.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter

from eawf.kernel.state.enums import MemoryStatus, StoreKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.memory import (
    _args_hash,
    _events_path_for,
    _load_notes,
    _memory_path_for,
    _memory_rpc,
    _resolve_confidence,
    memory_app,
)
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

logger = logging.getLogger(__name__)

#: The daemon verbs the handlers send. Spelled here so the Typer tree
#: builds without the daemon method registry on the path.
MEMORY_ADD: str = "memory.add"
MEMORY_PROMOTE: str = "memory.promote"
MEMORY_LINK: str = "memory.link"
MEMORY_PRUNE: str = "memory.prune"
MEMORY_GC: str = "memory.gc"
MEMORY_TIER: str = "memory.tier"

#: The id lists a prune or gc answer carries, validated where they enter.
_IDS: TypeAdapter[list[str]] = TypeAdapter(list[str])


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
    """File a new memory note on the generation's memory ledger."""
    flags: GlobalFlags = ctx.obj
    try:
        conf = _resolve_confidence(confidence)
        answer = _memory_rpc(
            MEMORY_ADD,
            {"scope_id": scope, "title": title, "body": body, "confidence": conf.value},
            state_path=resolve_state_path(flags.workspace),
            flags=flags,
            verb_text="memory add",
        )
        emit_json_or_text(payload=answer, text=f"memory added: {answer['id']}", flags=flags)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


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
            help="Promotion target. 'memory' (default) = source record -> memory note; "
            "'artifact' = memory note -> a decision the decision ledger holds.",
        ),
    ] = "memory",
    artifact_kind: Annotated[
        str,
        typer.Option(
            "--artifact-kind",
            help="Artifact target kind (only 'decision' is supported).",
        ),
    ] = "decision",
    artifact_id: Annotated[
        str | None,
        typer.Option(
            "--artifact-id",
            help="The key of the decision the note is retired into; required with --to artifact.",
        ),
    ] = None,
) -> None:
    """Promote a record. ``--to memory`` (default) or ``--to artifact``."""
    flags: GlobalFlags = ctx.obj
    try:
        target = to.strip().lower()
        if target not in {"memory", "artifact"}:
            raise cli_errors.UserError(
                f"--to must be one of memory|artifact; got {to!r}", kind="InvalidInput"
            )
        state_path = resolve_state_path(flags.workspace)
        if target == "artifact":
            _promote_to_artifact(
                flags=flags,
                state_path=state_path,
                session=session,
                source=source,
                source_kind=source_kind,
                artifact_kind=artifact_kind,
                artifact_id=artifact_id,
            )
            return
        conf = _resolve_confidence(confidence)
        try:
            kind = StoreKind(source_kind)
        except ValueError as exc:
            raise cli_errors.UserError(
                f"--source-kind must be one of {[k.value for k in StoreKind]}; got {source_kind!r}",
                kind="InvalidInput",
            ) from exc
        answer = _memory_rpc(
            MEMORY_PROMOTE,
            {
                "session": session,
                "source": source,
                "source_kind": kind.value,
                "scope_id": scope,
                "confidence": conf.value,
            },
            state_path=state_path,
            flags=flags,
            verb_text="memory promote",
        )
        emit_json_or_text(
            payload={**answer, "session": session},
            text=f"memory promoted: {answer['id']} (from {source})",
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


def _promote_to_artifact(
    *,
    flags: GlobalFlags,
    state_path: Path,
    session: str,
    source: str,
    source_kind: str,
    artifact_kind: str,
    artifact_id: str | None,
) -> None:
    """Handle ``eawf memory promote --to artifact``.

    The note is retired into a decision the decision ledger already holds:
    a decision carries the options it weighed and the evidence it stands
    on, which a note never recorded, so the decision is filed on its own
    and named here by its key.

    Raises:
        UserError: ``--source-kind`` is not ``memory``, ``--artifact-kind``
            is not ``decision``, or ``--artifact-id`` is missing.
    """
    if source_kind.strip().lower() != StoreKind.MEMORY.value:
        raise cli_errors.UserError(
            f"--to artifact requires --source-kind memory; got {source_kind!r}",
            kind="InvalidInput",
        )
    if artifact_kind.strip().lower() != "decision":
        raise cli_errors.UserError(
            f"--artifact-kind must be 'decision'; got {artifact_kind!r}", kind="InvalidInput"
        )
    if artifact_id is None:
        raise cli_errors.UserError(
            "--to artifact needs --artifact-id: file the decision with `eawf record append` "
            "first, then name its key here",
            kind="InvalidInput",
        )
    answer = _memory_rpc(
        MEMORY_LINK,
        {"session": session, "source": source, "artifact_id": artifact_id},
        state_path=state_path,
        flags=flags,
        verb_text="memory promote",
    )
    emit_json_or_text(
        payload={**answer, "artifact_kind": "decision", "session": session},
        text=f"memory promoted to artifact: {answer['id']} -> {artifact_id} (decision)",
        flags=flags,
    )


@memory_app.command("compact")
def memory_compact(ctx: typer.Context) -> None:
    """Compact ``memory.jsonl`` (dedup by content; idempotent)."""
    from eawf.kernel.store.compact import compact_store
    from eawf.runtime.session.store import append_event

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
            scope_id=None,
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
            help="Report which IDs would flip without writing anything.",
        ),
    ] = False,
) -> None:
    """Soft-delete prune. Flips status to PRUNED; the prior revision stays on the ledger."""
    from eawf.platform.memory.book import select_prunable

    flags: GlobalFlags = ctx.obj
    try:
        age_days = _parse_age_days(older_than)
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
                "pruning live entries retires them from every read.",
                kind="UserDeclined",
            )
        state_path = resolve_state_path(flags.workspace)
        if dry_run:
            selection = select_prunable(
                _load_notes(state_path),
                age_days=age_days,
                status_filter=status_filter,
                scope_id=scope,
                now=datetime.now(UTC),
            )
            pruned_ids, skipped_ids = selection.selected, selection.skipped
        else:
            answer = _memory_rpc(
                MEMORY_PRUNE,
                {"age_days": age_days, "status": status_filter.value, "scope_id": scope},
                state_path=state_path,
                flags=flags,
                verb_text="memory prune",
            )
            pruned_ids, skipped_ids = (
                _IDS.validate_python(answer["pruned_ids"]),
                _IDS.validate_python(answer["skipped_ids"]),
            )
        payload = {
            "pruned_ids": pruned_ids,
            "skipped_ids": skipped_ids,
            "dry_run": dry_run,
            "older_than_days": age_days,
            "scope_id": scope,
            "status_filter": status_filter.value,
        }
        verb = "would prune" if dry_run else "pruned"
        text = (
            f"{verb} {len(pruned_ids)} entries "
            f"(scope={scope}, older_than_days={age_days}, status={status_filter.value})"
        )
        emit_json_or_text(payload=payload, text=text, flags=flags)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


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
            help="Report which IDs would archive without writing anything.",
        ),
    ] = False,
) -> None:
    """Archive matched memory notes by moving them to the ARCHIVAL tier."""
    from eawf.platform.memory.book import select_archivable

    flags: GlobalFlags = ctx.obj
    try:
        if threshold_days < 0:
            raise cli_errors.UserError(
                f"--threshold-days must be >= 0; got {threshold_days}", kind="InvalidInput"
            )
        state_path = resolve_state_path(flags.workspace)
        if dry_run:
            selection = select_archivable(
                _load_notes(state_path), threshold_days=threshold_days, now=datetime.now(UTC)
            )
            archived_ids, skipped_ids = selection.selected, selection.skipped
        else:
            answer = _memory_rpc(
                MEMORY_GC,
                {"threshold_days": threshold_days},
                state_path=state_path,
                flags=flags,
                verb_text="memory gc",
            )
            archived_ids, skipped_ids = (
                _IDS.validate_python(answer["archived_ids"]),
                _IDS.validate_python(answer["skipped_ids"]),
            )
        payload = {
            "archived_ids": archived_ids,
            "skipped_ids": skipped_ids,
            "dry_run": dry_run,
            "threshold_days": threshold_days,
        }
        verb = "would archive" if dry_run else "archived"
        text = f"{verb} {len(archived_ids)} entries (threshold_days={threshold_days})"
        emit_json_or_text(payload=payload, text=text, flags=flags)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


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
    """Set the tier on a single memory note."""
    from eawf.kernel.state.enums import MemoryTier

    flags: GlobalFlags = ctx.obj
    try:
        try:
            target_tier = MemoryTier(tier.strip().lower())
        except ValueError as exc:
            raise cli_errors.UserError(
                f"--tier must be one of {[t.value for t in MemoryTier]}; got {tier!r}",
                kind="InvalidInput",
            ) from exc
        answer = _memory_rpc(
            MEMORY_TIER,
            {"id": mem_id, "tier": target_tier.value},
            state_path=resolve_state_path(flags.workspace),
            flags=flags,
            verb_text="memory tier",
        )
        emit_json_or_text(
            payload={"id": mem_id, "tier": answer["tier"], "prior_tier": answer["prior_tier"]},
            text=f"memory {mem_id} tier: {answer['prior_tier']} -> {answer['tier']}",
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
