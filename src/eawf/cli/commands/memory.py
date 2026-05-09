"""Typer sub-app for ``eawf memory ...``.

Sub-commands:

- ``add``            — write a new memory entry (JSONL + state cache).
- ``promote``        — promote a JSONL store record to a memory entry.
- ``list``           — list memory entries from the cache (optionally filtered).
- ``compact``        — wrap :func:`eawf.store.compact.compact_store` for ``memory.jsonl``.
- ``render-context`` — produce a token-budgeted context block.
- ``view``           — show one memory entry (cache + envelope body).
- ``stale``          — list stale candidates (low-confidence + over-age).

Mutation handlers follow the canonical sequence: load → mutate → validate →
atomic_write (sibling-locked) → append store record → append event. The
atomic-write helper acquires its own sibling lock; appends use sibling locks on
the store files.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.memory.promotion import PromotionError, promote_record
from eawf.memory.render_context import DEFAULT_BUDGET, render_context
from eawf.memory.staleness import find_stale
from eawf.memory.store import add_memory, find_envelope, read_envelopes
from eawf.session.store import append_event
from eawf.state.enums import Confidence, MemoryStatus, StoreKind
from eawf.state.models import State
from eawf.store.compact import compact_store
from eawf.store.paths import store_path
from eawf.validate.strict import validate_state

logger = logging.getLogger(__name__)

memory_app = typer.Typer(
    name="memory",
    help="Manage curated durable memory entries.",
    no_args_is_help=True,
)


_CONFIDENCE_FROM_FLAG: dict[str, Confidence] = {
    "h": Confidence.HIGH,
    "high": Confidence.HIGH,
    "m": Confidence.MEDIUM,
    "medium": Confidence.MEDIUM,
    "l": Confidence.LOW,
    "low": Confidence.LOW,
}


def _memory_path_for(state_path: Path) -> Path:
    """Return the canonical memory-store JSONL location next to ``state.json``."""
    return store_path(state_path, StoreKind.MEMORY)


def _events_path_for(state_path: Path) -> Path:
    """Return the canonical events-store JSONL location next to ``state.json``."""
    return store_path(state_path, StoreKind.EVENT)


def _load_state(state_path: Path) -> State:
    """Read + schema-validate a state document. Used by read-only handlers."""
    if not state_path.exists():
        raise cli_errors.NotFound(f"state file not found: {state_path}")
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationFailed(
            f"state validation failed: {'; '.join(report.schema_errors)}"
        )
    if report.violations:
        raise cli_errors.ValidationFailed(
            f"state invariant violations: {[v.code for v in report.violations]}"
        )
    return report.state


def _resolve_confidence(raw: str | None) -> Confidence:
    if raw is None:
        return Confidence.MEDIUM
    key = raw.strip().lower()
    if key not in _CONFIDENCE_FROM_FLAG:
        raise cli_errors.InvalidInput(
            f"--confidence must be one of h/m/l (or high/medium/low); got {raw!r}"
        )
    return _CONFIDENCE_FROM_FLAG[key]


def _resolve_status(raw: str | None) -> MemoryStatus | None:
    if raw is None:
        return None
    try:
        return MemoryStatus(raw.strip().lower())
    except ValueError as exc:
        raise cli_errors.InvalidInput(
            f"--status must be one of {[s.value for s in MemoryStatus]}; got {raw!r}",
        ) from exc


def _args_hash(args: dict[str, object]) -> str:
    return hashlib.sha256(orjson.dumps(args, option=orjson.OPT_SORT_KEYS)).hexdigest()


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
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


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
            help="Source store filename without .jsonl (e.g. research, decisions).",
        ),
    ] = "research",
    confidence: Annotated[
        str | None, typer.Option("--confidence", help="h/m/l (default medium)")
    ] = None,
) -> None:
    """Promote a store record into a memory entry."""
    flags: GlobalFlags = ctx.obj
    try:
        conf = _resolve_confidence(confidence)
        state_path = resolve_state_path(flags.workspace)
        memory_path = _memory_path_for(state_path)
        events_path = _events_path_for(state_path)
        try:
            source_kind_enum = StoreKind(source_kind)
        except ValueError as exc:
            raise cli_errors.InvalidInput(
                f"--source-kind must be one of {[k.value for k in StoreKind]}; got {source_kind!r}"
            ) from exc
        source_path = store_path(state_path, source_kind_enum)
        with state_transaction(state_path) as state:
            if session not in state.agent_sessions:
                raise cli_errors.NotFound(f"session {session!r} not in agent_sessions")
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
                raise cli_errors.NotFound(str(exc)) from exc
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
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@memory_app.command("list")
def memory_list(
    ctx: typer.Context,
    scope: Annotated[str | None, typer.Option("--scope", help="Filter by scope ID.")] = None,
    status: Annotated[str | None, typer.Option("--status", help="Filter by memory status.")] = None,
) -> None:
    """List memory entries from ``state.memory_index`` (the cache)."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        state = _load_state(state_path)
        status_filter = _resolve_status(status)
        index = state.memory_index or {}
        entries = []
        for mid, summary in sorted(index.items()):
            if scope is not None and summary.scope_id != scope:
                continue
            if status_filter is not None and summary.status != status_filter:
                continue
            entries.append(
                {
                    "id": mid,
                    "scope_id": summary.scope_id,
                    "status": summary.status.value,
                    "confidence": summary.confidence.value,
                    "summary": summary.summary,
                }
            )
        text_lines = [
            (f"{e['id']}\t{e['scope_id']}\t{e['status']}\t{e['confidence']}\t{e['summary']}")
            for e in entries
        ]
        emit_json_or_text(
            payload={"entries": entries, "count": len(entries)},
            text="\n".join(text_lines) if text_lines else "(no entries)",
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


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
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@memory_app.command("render-context")
def memory_render_context(
    ctx: typer.Context,
    scope: Annotated[
        str | None, typer.Option("--scope", help="Anchor scope ID for ranking.")
    ] = None,
    budget: Annotated[
        int, typer.Option("--budget", help="Token budget. Default 4096.")
    ] = DEFAULT_BUDGET,
) -> None:
    """Produce a token-budgeted Markdown rendering of memory entries."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        memory_path = _memory_path_for(state_path)
        state = _load_state(state_path)
        result = render_context(
            state=state,
            memory_path=memory_path,
            anchor_scope=scope,
            budget=budget,
        )
        emit_json_or_text(
            payload={
                "text": result.text,
                "included_ids": result.included_ids,
                "skipped_ids": result.skipped_ids,
                "skipped_count": len(result.skipped_ids),
                "tokens_used": result.tokens_used,
                "budget": result.budget,
            },
            text=result.text
            + (f"\n[skipped: {len(result.skipped_ids)}]" if result.skipped_ids else ""),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@memory_app.command("view")
def memory_view(
    ctx: typer.Context,
    mem_id: Annotated[str, typer.Argument(help="Memory entry ID, e.g. MEM-...")],
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Optional scope guard; rejects mismatch."),
    ] = None,
) -> None:
    """Show a single memory entry: cache summary + JSONL body."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        memory_path = _memory_path_for(state_path)
        state = _load_state(state_path)
        summary = (state.memory_index or {}).get(mem_id)
        if summary is None:
            raise cli_errors.NotFound(f"memory entry {mem_id!r} not in state.memory_index")
        if scope is not None and summary.scope_id != scope:
            raise cli_errors.InvalidInput(
                f"scope {scope!r} does not match entry scope {summary.scope_id!r}"
            )
        env = find_envelope(memory_path, mem_id)
        body = env.payload.get("body", "") if env is not None else ""
        emit_json_or_text(
            payload={
                "id": mem_id,
                "scope_id": summary.scope_id,
                "status": summary.status.value,
                "confidence": summary.confidence.value,
                "summary": summary.summary,
                "body": body,
                "store_record_id": summary.store_record_id,
            },
            text=(
                f"{mem_id}\t{summary.scope_id}\t{summary.status.value}\t"
                f"{summary.confidence.value}\n{summary.summary}\n\n{body}"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@memory_app.command("stale")
def memory_stale(
    ctx: typer.Context,
    scope: Annotated[str | None, typer.Option("--scope", help="Filter by scope ID.")] = None,
    age: Annotated[int, typer.Option("--age", help="Age threshold in days.")] = 30,
) -> None:
    """List memory entries that exceed ``--age`` days and are below high confidence."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        memory_path = _memory_path_for(state_path)
        state = _load_state(state_path)
        stale = find_stale(
            state=state,
            memory_path=memory_path,
            age_days=age,
            scope_id=scope,
        )
        entries = [
            {
                "id": e.id,
                "scope_id": e.scope_id,
                "confidence": e.confidence.value,
                "age_days": round(e.age_days, 2),
            }
            for e in stale
        ]
        text = (
            "\n".join(
                f"{e['id']}\t{e['scope_id']}\t{e['confidence']}\t{e['age_days']:.1f}d"
                for e in entries
            )
            or "(no stale entries)"
        )
        emit_json_or_text(
            payload={"entries": entries, "count": len(entries), "age_days": age},
            text=text,
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


# Re-export to keep the linter quiet about the imported helpers used only above.
__all__ = ["memory_app", "read_envelopes"]
