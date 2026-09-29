"""``eawf reflect``: report where a tree's Runs went, without a canonical write.

The group carries ``run``, ``show``, ``serve``, ``export`` and ``prune``. Every verb reads
the tree and writes only to the local reflection collection under ``.ea/local/reflect``,
its title cache, or an output path the operator names; an output path inside the
canonical store is refused before anything is read. ``run`` is the one verb that may call
out: it fills session titles from a scrubbed structural digest, and ``--local-only``
turns that off. The other four never call out under any flag.

The handlers parse, dispatch to :mod:`eawf.observability.reflect` and format.
"""

from __future__ import annotations

from datetime import UTC, datetime
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


@reflect_app.command("run")
def reflect_run(
    ctx: typer.Context,
    local_only: Annotated[
        bool,
        typer.Option("--local-only", help="Make no call off this machine; titles fall back."),
    ] = False,
    out: _OutOption = None,
) -> None:
    """Read the tree's Runs, fill their titles, and write the report."""
    from eawf.observability.reflect import report as rp
    from eawf.observability.reflect import titles as tt
    from eawf.observability.reflect.runs import read_tree_runs

    flags: GlobalFlags = ctx.obj
    tree_root = _tree_root(flags)
    target = _output(tree_root, out, flags=flags)
    root = rp.reflect_root(tree_root)
    now = datetime.now(UTC)
    readings = read_tree_runs(tree_root)
    cache_path = root / rp.TITLE_CACHE_FILENAME
    provider = None if local_only else tt.ClaudeTitleProvider.discover()
    titles, account, cache = tt.fill_titles(
        readings,
        cache=tt.load_title_cache(cache_path),
        provider=provider,
        local_only=local_only,
        now=now,
    )
    if not local_only:
        tt.save_title_cache(cache_path, cache)
    report = rp.build_report(readings, titles, local_only=local_only)
    manifest = rp.fill_manifest(account, sessions=len(readings))
    stored = rp.store_report(root, report, manifest, on=now.date(), out=target)
    emit_json_or_text(
        {
            "report": str(stored.text),
            "document": str(stored.document),
            "manifest": manifest.model_dump(mode="json"),
        },
        f"{rp.render_text(report)}report: {stored.text}",
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


__all__ = [
    "reflect_app",
    "reflect_export",
    "reflect_prune",
    "reflect_run",
    "reflect_serve",
    "reflect_show",
]
