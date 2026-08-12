"""Read-only ``eawf memory`` verbs (list / render-context / view / stale).

Split out of :mod:`eawf.surfaces.cli.commands.memory`. The
:data:`memory_app` Typer group and the shared helpers (store-path
resolvers, the read-only state loader, the status parser, the
inlined default budget) live in the parent module; this module attaches
the four query command bodies via ``@memory_app.command(...)``. Every
``eawf.platform.memory.*`` import stays inside the handler bodies so the
command-tree build path stays off the import-budget heavy graph.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated

import typer

from eawf.surfaces.cli import errors as cli_errors

if TYPE_CHECKING:
    from eawf.platform.memory.digest import DigestEntry
from eawf.surfaces.cli.commands.memory import (
    _DEFAULT_BUDGET,
    _load_state,
    _memory_path_for,
    _resolve_status,
    memory_app,
)
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

logger = logging.getLogger(__name__)


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
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)


@memory_app.command("render-context")
def memory_render_context(
    ctx: typer.Context,
    scope: Annotated[
        str | None, typer.Option("--scope", help="Anchor scope ID for ranking.")
    ] = None,
    budget: Annotated[
        int, typer.Option("--budget", help="Token budget. Default 4096.")
    ] = _DEFAULT_BUDGET,
    include_superseded: Annotated[
        bool,
        typer.Option(
            "--include-superseded",
            help="Include SUPERSEDED entries (default: only ACTIVE).",
        ),
    ] = False,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            help="Output format: 'markdown' (default) or 'json'. "
            "When 'json', the rendered text is omitted from the JSON envelope; "
            "callers consume included_ids structurally.",
        ),
    ] = "markdown",
    max_entries: Annotated[
        int | None,
        typer.Option(
            "--max-entries",
            help="Cap on the count of included entries; the budget still wins.",
        ),
    ] = None,
) -> None:
    """Produce a token-budgeted Markdown rendering of memory entries."""
    from eawf.platform.memory.render_context import render_context

    flags: GlobalFlags = ctx.obj
    fmt_norm = fmt.strip().lower()
    if fmt_norm not in {"markdown", "json"}:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--format must be markdown|json; got {fmt!r}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    if max_entries is not None and max_entries < 0:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--max-entries must be >= 0; got {max_entries}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
        memory_path = _memory_path_for(state_path)
        state = _load_state(state_path)
        result = render_context(
            state=state,
            memory_path=memory_path,
            anchor_scope=scope,
            budget=budget,
            include_superseded=include_superseded,
            max_entries=max_entries,
        )
        payload: dict[str, object] = {
            "included_ids": result.included_ids,
            "skipped_ids": result.skipped_ids,
            "skipped_count": len(result.skipped_ids),
            "tokens_used": result.tokens_used,
            "budget": result.budget,
            "include_superseded": include_superseded,
            "format": fmt_norm,
            "max_entries": max_entries,
        }
        if fmt_norm == "markdown":
            payload["text"] = result.text
        emit_json_or_text(
            payload=payload,
            text=result.text
            + (f"\n[skipped: {len(result.skipped_ids)}]" if result.skipped_ids else ""),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)


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
    from eawf.platform.memory.store import find_envelope

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        memory_path = _memory_path_for(state_path)
        state = _load_state(state_path)
        summary = (state.memory_index or {}).get(mem_id)
        if summary is None:
            raise cli_errors.UserError(
                f"memory entry {mem_id!r} not in state.memory_index", kind="NotFound"
            )
        if scope is not None and summary.scope_id != scope:
            raise cli_errors.UserError(
                f"scope {scope!r} does not match entry scope {summary.scope_id!r}",
                kind="InvalidInput",
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
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)


@memory_app.command("digest")
def memory_digest(
    ctx: typer.Context,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            help="Output format: 'md' (default Markdown standup) or 'json'.",
        ),
    ] = "md",
    md: Annotated[
        bool,
        typer.Option("--md", help="Shorthand for --format md (the default)."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Shorthand for --format json."),
    ] = False,
) -> None:
    """Emit a state-derived standup: current focus, recent closes, decisions.

    A pure projection of ``state.json`` — it writes nothing, so the state
    document and the stores are byte-equal before and after the command. The
    Markdown branch is one line per paragraph and glosses every lifecycle id
    so it reads as a glance-clear newcomer standup.
    """
    from eawf.platform.memory.digest import build_digest, render_digest_md

    flags: GlobalFlags = ctx.obj
    fmt_norm = fmt.strip().lower()
    # --md / --json are shorthands for --format; --json (or the root --json
    # flag) wins so a machine caller always gets the structured surface.
    if json_output or flags.json_output:
        fmt_norm = "json"
    elif md:
        fmt_norm = "md"
    if fmt_norm not in {"md", "json"}:
        cli_errors.emit_error(
            cli_errors.UserError(f"--format must be md|json; got {fmt!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
        state = _load_state(state_path)
        digest = build_digest(state)
        text = render_digest_md(digest)
        payload: dict[str, object] = {
            "format": fmt_norm,
            "phase": _entry_payload(digest.phase),
            "iter": _entry_payload(digest.iter),
            "recently_closed": [_entry_payload(e) for e in digest.recently_closed],
            "recent_decisions": [_entry_payload(e) for e in digest.recent_decisions],
        }
        if fmt_norm == "md":
            payload["text"] = text
        effective_flags = GlobalFlags(
            json_output=fmt_norm == "json",
            plain_output=flags.plain_output,
            no_input=flags.no_input,
            workspace=flags.workspace,
        )
        emit_json_or_text(payload=payload, text=text, flags=effective_flags)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)


def _entry_payload(entry: DigestEntry | None) -> dict[str, str] | None:
    """Project a :class:`~eawf.platform.memory.digest.DigestEntry` into a JSON dict.

    Returns ``None`` for a ``None`` entry (an absent phase / iter pointer) so
    the JSON surface mirrors the typed digest's optional fields.
    """
    if entry is None:
        return None
    return {
        "ref_id": entry.ref_id,
        "title": entry.title,
        "detail": entry.detail,
    }


@memory_app.command("stale")
def memory_stale(
    ctx: typer.Context,
    scope: Annotated[str | None, typer.Option("--scope", help="Filter by scope ID.")] = None,
    age: Annotated[int, typer.Option("--age", help="Age threshold in days.")] = 30,
) -> None:
    """List memory entries that exceed ``--age`` days and are below high confidence."""
    from eawf.platform.memory.staleness import find_stale

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
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)
