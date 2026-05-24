"""``eawf store`` — JSONL store maintenance.

Currently exposes a single subcommand:

- ``eawf store compact [--kind <kind>] [--scope <id>] [--budget <bytes>]``

The command thinly wraps :func:`eawf.kernel.store.compact.compact_store`. The
``--kind`` argument selects which JSONL file under the canonical
``<state_dir>/store/<kind>.jsonl`` path is targeted. The ``--scope`` and
``--budget`` flags are accepted for v0.1 surface-stability and surfaced
in the JSON envelope; the underlying compactor does not yet enforce a
budget, so ``--budget`` is informational only and a TODO is logged when
set.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.cli import errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)

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
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Optional scope ID (informational; surfaced in envelope)."),
    ] = None,
    budget: Annotated[
        int | None,
        typer.Option(
            "--budget",
            help="Informational byte budget (not enforced by the compactor in v0.1).",
        ),
    ] = None,
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
    if budget is not None:
        logger.info(f"compact_cmd budget={budget!r}; accepted but not enforced (v0.1)")

    report = compact_store(target_path)

    payload: dict[str, Any] = {
        "kind": kind.value,
        "scope": scope,
        "path": str(target_path),
        "records_in": report.records_in,
        "records_out": report.records_out,
        "dedup_count": report.dedup_count,
        "budget": budget,
    }
    text = (
        f"compact: kind={kind.value} path={target_path} "
        f"in={report.records_in} out={report.records_out} dedup={report.dedup_count}"
    )
    emit_json_or_text(payload, text, flags=flags)
