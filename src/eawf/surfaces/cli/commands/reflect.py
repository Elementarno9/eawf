"""``eawf reflect``: report where a tree's Runs went, without a canonical write.

The group carries ``run``, ``show``, ``serve``, ``export``, ``prune`` and ``prices check``.
``prices check`` writes nothing; with ``--online`` it fetches the public vendor pricing
pages and sends nothing about the tree. The other verbs read the tree and write only to
the local reflection collection under ``.ea/local/reflect``, its title cache, or an output
path the operator names; an output path inside the canonical store is refused before
anything is read. Of those, ``run`` is the one verb that may call out: it fills session
titles from a scrubbed structural digest, and ``--local-only`` turns that off.
``show``, ``serve``, ``export`` and ``prune`` never call out under any flag. ``run`` also
indexes the local Claude and Codex session history incrementally into the collection and writes its
analysis there beside the report, with offline overview, finder and session pages over
the sessions ``--scope`` covers. Since the index holds every project's sessions, an
output directory git would commit is refused before anything is written into it.

The handlers parse, dispatch to :mod:`eawf.observability.reflect` and format.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

reflect_app = typer.Typer(
    name="reflect",
    help="Report where effort, time and money went; no canonical write.",
    no_args_is_help=True,
)

_OutOption = Annotated[
    Path | None,
    typer.Option("--out", help="Output path; refused inside the canonical store."),
]


class _ReportFormat(StrEnum):
    """What ``reflect run`` prints on stdout."""

    TEXT = "text"
    JSON = "json"


def _tree_root(flags: GlobalFlags) -> Path:
    """Return the tree's ``.ea`` directory."""
    try:
        return resolve_state_path(flags.workspace).parent
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)


def _output(tree_root: Path, out: Path | None, *, flags: GlobalFlags) -> Path | None:
    """Return ``out`` resolved, or exit before anything is read when it is canonical."""
    from eawf.observability.reflect.report import (
        CanonicalWriteRefusedError,
        resolve_output_path,
    )

    if out is None:
        return None
    try:
        return resolve_output_path(tree_root, out)
    except CanonicalWriteRefusedError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)


def _progress(stage: str, done: int, total: int) -> None:
    """Print one progress line on stderr, so stdout stays the report alone."""
    typer.echo(f"reflect: {stage} {done}/{total}", err=True)


def _index_wait() -> None:
    """Say on stderr that this run waits for another one holding the history index."""
    typer.echo("reflect: waiting for another reflect run to release the history index", err=True)


@reflect_app.command("run")
def reflect_run(
    ctx: typer.Context,
    local_only: Annotated[
        bool,
        typer.Option("--local-only", help="Make no call off this machine; titles fall back."),
    ] = False,
    out: _OutOption = None,
    window: Annotated[
        str | None,
        typer.Option("--window", help="Keep only Runs started in the last <N>d, such as 7d."),
    ] = None,
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="project, workspace, user (or all_local), milestone:<KEY> or track:<KEY>.",
        ),
    ] = "project",
    fmt: Annotated[
        _ReportFormat,
        typer.Option("--format", help="Print the text report, or the report JSON."),
    ] = _ReportFormat.TEXT,
) -> None:
    """Read the tree's Runs and local session history, fill titles, and write the report."""
    from eawf.observability.reflect import report as rp
    from eawf.observability.reflect import scopes as sc
    from eawf.observability.reflect import titles as tt
    from eawf.observability.reflect.runs import read_tree_runs
    from eawf.observability.reflect.sessions import reflect_sessions
    from eawf.observability.telemetry.projector import IndexBusyError, LocalHistory
    from eawf.observability.telemetry.sources.session_history import claude_history_root

    try:
        selected_scope = rp.parse_scope(scope)
        window_days = None if window is None else rp.parse_window(window)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    flags: GlobalFlags = ctx.obj
    tree_root = _tree_root(flags)
    target = _output(tree_root, out, flags=flags)
    root = rp.reflect_root(tree_root)
    area = sc.OutputArea.of(root if target is None else target.parent, tree_root=tree_root)
    now = datetime.now(UTC)
    kept = read_tree_runs(
        tree_root,
        admits=selected_scope.admits,
        started_since=None if window_days is None else now - timedelta(days=window_days),
        progress=_progress,
    )
    cohort = rp.select_cohort(kept, scope=selected_scope, window_days=window_days, now=now)
    readings = cohort.sessions
    cache_path = root / rp.TITLE_CACHE_FILENAME
    provider = None if local_only else tt.ClaudeTitleProvider.discover()
    budget = tt.CallBudget()
    titles, account, cache = tt.fill_titles(
        readings,
        cache=tt.load_title_cache(cache_path),
        provider=provider,
        local_only=local_only,
        now=now,
        budget=budget,
    )
    try:
        sessions = reflect_sessions(
            tree_root,
            report_scope=selected_scope,
            area=area,
            history=LocalHistory.discover(),
            harness_store=claude_history_root(tree_root.parent),
            cache=cache,
            provider=provider,
            budget=budget,
            local_only=local_only,
            now=now,
            on_wait=_index_wait,
            progress=_progress,
        )
    except sc.ForeignRowsRefusedError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
    except IndexBusyError as exc:
        cli_errors.emit_error(cli_errors.StateConflict(str(exc), kind="IndexBusy"), flags=flags)
    if not local_only:
        tt.save_title_cache(cache_path, sessions.cache)
    skipped = account.digests_skipped + sessions.account.digests_skipped
    if skipped:
        typer.echo(
            f"reflect: titles {budget.used} calls made, {skipped} left for a later run"
            f" (cap {budget.limit} calls per run)",
            err=True,
        )
    _progress("render", 0, 2)
    report = rp.build_report(
        cohort, titles, scope=selected_scope, window_days=window_days, local_only=local_only
    )
    _progress("render", 1, 2)
    manifest = rp.fill_manifest(account, sessions=len(readings))
    stored = rp.store_report(root, report, manifest, on=now.date(), out=target)
    _progress("render", 2, 2)
    if fmt is _ReportFormat.JSON:
        typer.echo(report.model_dump_json(indent=2))
        return
    emit_json_or_text(
        {
            "report": str(stored.text),
            "document": str(stored.document),
            "manifest": manifest.model_dump(mode="json"),
            "analysis": str(sessions.analysis),
            "pages": str(sessions.overview),
        },
        f"{rp.render_text(report)}sessions: {sessions.analysis}\npages: {sessions.overview}\n"
        f"report: {stored.text}",
        flags=flags,
    )


@reflect_app.command("show")
def reflect_show(ctx: typer.Context) -> None:
    """Print the newest report in the local collection."""
    from eawf.observability.reflect import report as rp

    flags: GlobalFlags = ctx.obj
    root = rp.reflect_root(_tree_root(flags))
    document = rp.latest_report(root)
    if document is None:
        cli_errors.emit_error(
            cli_errors.UserError("no report yet; run `eawf reflect run`", kind="NotFound"),
            flags=flags,
        )
    report = rp.load_report(document)
    emit_json_or_text(report.model_dump(mode="json"), rp.render_text(report).rstrip(), flags=flags)


@reflect_app.command("export")
def reflect_export(ctx: typer.Context, out: _OutOption = None) -> None:
    """Write the newest report as a static page that opens from the filesystem."""
    from eawf.observability.reflect import report as rp

    flags: GlobalFlags = ctx.obj
    tree_root = _tree_root(flags)
    target = _output(tree_root, out, flags=flags)
    root = rp.reflect_root(tree_root)
    document = rp.latest_report(root)
    if document is None:
        cli_errors.emit_error(
            cli_errors.UserError("no report yet; run `eawf reflect run`", kind="NotFound"),
            flags=flags,
        )
    page = rp.export_report(root, document, dest=target)
    emit_json_or_text({"page": str(page)}, f"page: {page}", flags=flags)


@reflect_app.command("serve")
def reflect_serve(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host", help="Loopback address to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to bind; 0 picks one.")] = 0,
    root: Annotated[
        Path | None,
        typer.Option("--root", help="Directory to serve, inside the local collection."),
    ] = None,
) -> None:
    """Serve the local collection read-only on loopback until interrupted."""
    from eawf.observability.reflect import report as rp
    from eawf.observability.reflect.serve import ServeRefusedError, open_report_server

    flags: GlobalFlags = ctx.obj
    collection = rp.reflect_root(_tree_root(flags))
    try:
        server = open_report_server(
            collection if root is None else root, report_path=collection, host=host, port=port
        )
    except ServeRefusedError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
    emit_json_or_text({"url": server.url}, f"serving {server.url} · Ctrl-C stops it", flags=flags)
    server.serve_until_interrupted()


@reflect_app.command("prune")
def reflect_prune(ctx: typer.Context) -> None:
    """Remove local reports and cached titles past their retention class."""
    from eawf.observability.reflect import report as rp
    from eawf.observability.reflect import titles as tt

    flags: GlobalFlags = ctx.obj
    root = rp.reflect_root(_tree_root(flags))
    cache_path = root / rp.TITLE_CACHE_FILENAME
    result = rp.prune_collection(root, tt.load_title_cache(cache_path), now=datetime.now(UTC))
    if result.titles_removed:
        tt.save_title_cache(cache_path, result.cache)
    emit_json_or_text(
        {"removed": list(result.removed), "titles_removed": result.titles_removed},
        f"removed {len(result.removed)} entries and {result.titles_removed} cached titles",
        flags=flags,
    )


prices_app = typer.Typer(
    name="prices",
    help="Check the shipped price files.",
    no_args_is_help=True,
)
reflect_app.add_typer(prices_app)


@prices_app.command("check")
def prices_check(
    ctx: typer.Context,
    online: Annotated[
        bool,
        typer.Option("--online", help="Fetch the vendor pricing pages and compare every rate."),
    ] = False,
) -> None:
    """Check the price files; exit 0 when current, 1 on a difference, 2 when a price is unchecked.

    Without ``--online`` the check stays offline: every Anthropic row must bill
    cache writes at the vendor's stated multipliers of its input rate.
    """
    from eawf.observability.telemetry.price_check import check_prices_online, render_price_check
    from eawf.observability.telemetry.pricing import check_pricing_currency

    flags: GlobalFlags = ctx.obj
    if online:
        report = check_prices_online()
        emit_json_or_text(report.model_dump(mode="json"), render_price_check(report), flags=flags)
        raise typer.Exit(code=report.exit_status)
    offline = check_pricing_currency()
    lines = [
        f"Prices: {'the file is consistent' if offline.is_current else 'rates drift'} "
        f"({offline.model_count} rows, offline)."
    ]
    lines.extend(
        f"- {finding.model_id} {finding.field}: expected {finding.expected}, file {finding.actual}"
        for finding in offline.findings
    )
    emit_json_or_text(offline.model_dump(mode="json"), "\n".join(lines), flags=flags)
    raise typer.Exit(code=0 if offline.is_current else 1)


__all__ = [
    "prices_check",
    "reflect_app",
    "reflect_export",
    "reflect_prune",
    "reflect_run",
    "reflect_serve",
    "reflect_show",
]
