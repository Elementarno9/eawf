"""``eawf store`` — JSONL store maintenance.

Currently exposes a single subcommand:

- ``eawf store compact [--kind <kind>]``

The command thinly wraps :func:`eawf.kernel.store.compact.compact_store`. The
``--kind`` argument selects which JSONL file under the canonical
``<state_dir>/store/<kind>.jsonl`` path is targeted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.cli import errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

store_app = typer.Typer(
    name="store",
    help="JSONL store maintenance (compact, ...).",
    no_args_is_help=True,
    add_completion=False,
)


@store_app.command(name="compact")
def compact_cmd(
    ctx: typer.Context,
    kind: Annotated[
        StoreKind,
        typer.Option(
            "--kind",
            help="Store kind to compact (selects <state_dir>/store/<kind>.jsonl).",
        ),
    ] = StoreKind.MEMORY,
    workspace: Annotated[
        Path | None,
        typer.Option(
            "-w",
            "--workspace",
            help="Workspace root for state.json resolution (overrides pwd-upward).",
        ),
    ] = None,
) -> None:
    """Compact the JSONL store for *kind* and emit the dedup report."""
    from eawf.kernel.store.compact import compact_store
    from eawf.kernel.store.paths import store_path as _canonical_store_path

    flags: GlobalFlags = ctx.obj
    effective_ws = workspace if workspace is not None else flags.workspace

    state_path, _reason = resolve_with_reason(workspace=effective_ws)
    state_dir = state_path.parent
    if not state_dir.exists():
        errors.emit_error(
            errors.UserError(f"state directory not found: {state_dir}", kind="NotFound"),
            flags=flags,
        )
        return

    target_path = _canonical_store_path(state_path, kind)
    report = compact_store(target_path)

    payload: dict[str, Any] = {
        "kind": kind.value,
        "path": str(target_path),
        "records_in": report.records_in,
        "records_out": report.records_out,
        "dedup_count": report.dedup_count,
    }
    text = (
        f"compact: kind={kind.value} path={target_path} "
        f"in={report.records_in} out={report.records_out} dedup={report.dedup_count}"
    )
    emit_json_or_text(payload, text, flags=flags)
