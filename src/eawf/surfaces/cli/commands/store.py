"""``eawf store`` — JSONL store maintenance.

- ``eawf store compact [--kind <kind>]`` thinly wraps
  :func:`eawf.kernel.store.compact.compact_store`; ``--kind`` selects which
  JSONL file under ``<state_dir>/store/<kind>.jsonl`` is targeted.
- ``eawf store redact-content`` sends ``runtime.store.redact_content``, which
  withholds leaked lines from the artifact ledger's content rows that no
  commit holds yet.
- ``eawf store drop-unpublished-row`` sends ``runtime.store.drop_unpublished_row``,
  which removes one artifact-ledger line no commit holds and nothing else names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Final

import typer

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.cli import errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

#: The daemon verb ``store redact-content`` sends, spelled here so the Typer tree
#: builds without the daemon method registry on the path.
STORE_REDACT_CONTENT: Final = "runtime.store.redact_content"

#: The daemon verb ``store drop-unpublished-row`` sends, spelled here for the same reason.
STORE_DROP_UNPUBLISHED_ROW: Final = "runtime.store.drop_unpublished_row"

store_app = typer.Typer(
    name="store",
    help="JSONL store maintenance (compact, redact-content, drop-unpublished-row).",
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


@store_app.command(name="redact-content")
def redact_content_cmd(
    ctx: typer.Context,
    idempotency_key: Annotated[
        str,
        typer.Option(
            "--idempotency-key", help="Caller's name for this request; a retry replays its receipt."
        ),
    ],
    actor: Annotated[
        str, typer.Option("--actor", help="Principal key the request is attributed to.")
    ],
) -> None:
    """Withhold leaked lines from content rows that no commit holds yet.

    The daemon compares the artifact ledger with its bytes in HEAD, replaces
    every line the leak scan flags in the rows after them with the withheld
    marker, and leaves every committed row as it is.
    """
    from eawf.surfaces.cli.commands.domain_integration import _answer, _send

    subject = "artifact ledger"
    answer = _send(
        ctx,
        STORE_REDACT_CONTENT,
        {"actor": actor, "idempotency_key": idempotency_key},
        urn=subject,
        verb_text="store redact-content",
        key=idempotency_key,
    )
    if answer is not None:
        _answer(ctx, answer, operation=STORE_REDACT_CONTENT, urn=subject, revision=None)


@store_app.command(name="drop-unpublished-row")
def drop_unpublished_row_cmd(
    ctx: typer.Context,
    record_key: Annotated[
        str, typer.Argument(help="Record key of the artifact-ledger line to remove.")
    ],
    idempotency_key: Annotated[
        str,
        typer.Option(
            "--idempotency-key", help="Caller's name for this request; a retry replays its receipt."
        ),
    ],
    actor: Annotated[
        str, typer.Option("--actor", help="Principal key the request is attributed to.")
    ],
) -> None:
    """Remove one non-content row no commit holds and nothing else names.

    The way out for a leaking row redact-content lists but cannot rewrite:
    the daemon refuses a committed row, a content row, a compacted record
    and a row another record cites, and writes nothing then.
    """
    from eawf.surfaces.cli.commands.domain_integration import _answer, _send

    subject = "artifact ledger"
    answer = _send(
        ctx,
        STORE_DROP_UNPUBLISHED_ROW,
        {"actor": actor, "idempotency_key": idempotency_key, "record_key": record_key},
        urn=subject,
        verb_text="store drop-unpublished-row",
        key=idempotency_key,
    )
    if answer is not None:
        _answer(ctx, answer, operation=STORE_DROP_UNPUBLISHED_ROW, urn=subject, revision=None)
