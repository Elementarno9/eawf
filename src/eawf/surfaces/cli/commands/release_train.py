"""``eawf release receipts`` and ``eawf release advance``: walking the train.

Split out of :mod:`eawf.surfaces.cli.commands.release`, whose
:data:`~eawf.surfaces.cli.commands.release.release_app` and daemon
dispatch these verbs reuse. They are the two steps that move the release
train past a finished checkpoint. ``receipts`` proves each required gate
on the checkpoint's pinned source and stores one receipt per gate that
passed. ``advance`` walks the train on from the stored record and
receipts. Neither takes a record file: the daemon reads the stores, so a
copy saved one step early has no way in.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.commands.release import (
    RELEASE_RPC_METHODS,
    _dispatch,
    _read_json_document,
    release_app,
)
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)


def _proof_wait_seconds(version: str) -> float:
    """Return how long to wait for the daemon to prove *version*'s gates.

    Args:
        version: Checkpoint version whose profile's proof commands run.

    Returns:
        The profile's summed proof budgets plus the producer's margin.

    Raises:
        cli_errors.UserError: When the train declares no such rung or no
            binding table is authored for its profile.
    """
    from eawf.workflow.release.checkpoint_receipts import proof_budget_seconds
    from eawf.workflow.release.train import V07_TRAIN, gate_bindings_for

    try:
        rung = V07_TRAIN.checkpoint_for_version(version)
        return float(proof_budget_seconds(gate_bindings_for(rung.gate_profile)))
    except (KeyError, ValueError) as exc:
        raise cli_errors.UserError(
            f"cannot prove the gates of {version!r}: {exc}", kind="NotFound"
        ) from exc


def _receipts_text(result: dict[str, Any]) -> str:
    """Return the operator-facing summary of a ``release.produce_receipts`` reply."""
    lines = [f"{result.get('release_key')} at {result.get('source_sha')}"]
    lines.extend(
        f"  pass {row.get('gate'):<26} {row.get('receipt_ref')} expires {row.get('expires_at')}"
        for row in result.get("receipts") or ()
        if isinstance(row, dict)
    )
    lines.extend(
        f"  FAIL {row.get('gate'):<26} {row.get('detail')}"
        for row in result.get("refused") or ()
        if isinstance(row, dict)
    )
    return "\n".join(lines)


@release_app.command("receipts")
def release_receipts(
    ctx: typer.Context,
    version: Annotated[
        str, typer.Argument(help="Checkpoint version whose gates are proven, e.g. 0.7.0.dev2.")
    ],
    ttl_seconds: Annotated[
        int | None,
        typer.Option("--ttl-seconds", help="How long each receipt stays fresh (default: a day)."),
    ] = None,
    waiver_count: Annotated[
        int,
        typer.Option("--waiver-count", help="Gate waivers recorded against the checkpoint."),
    ] = 0,
    waivers_file: Annotated[
        Path | None,
        typer.Option("--waivers", help="JSON object with a 'waivers' list explaining the count."),
    ] = None,
    acknowledgements_file: Annotated[
        Path | None,
        typer.Option(
            "--acknowledgements",
            help="JSON object with an 'acknowledgements' list accepting the waivers.",
        ),
    ] = None,
) -> None:
    """Prove every required gate of one checkpoint on its pinned source.

    Signal gates read a fresh readiness sweep of the commit the stored
    record pins. Proof commands run in a git worktree checked out at that
    commit, never in this checkout. The waiver-block gate reads the
    waiver rows passed here. Each gate that passes stores one receipt
    bound to the record's source and manifest digest. A gate that fails
    stores none, and the verb exits non-zero naming every such gate
    after storing the others.

    The call waits as long as the profile's proof commands may run,
    which is about an hour when a gate runs the whole suite.
    """
    flags: GlobalFlags = ctx.obj
    try:
        params: dict[str, Any] = {"version": version, "waiver_count": waiver_count}
        if ttl_seconds is not None:
            params["ttl_seconds"] = ttl_seconds
        if waivers_file is not None:
            params["waivers"] = _read_json_document(waivers_file, label="waiver rows")["waivers"]
        if acknowledgements_file is not None:
            params["acknowledgements"] = _read_json_document(
                acknowledgements_file, label="acknowledgement rows"
            )["acknowledgements"]
        result = _dispatch(
            RELEASE_RPC_METHODS["receipts"],
            params,
            call_timeout_seconds=_proof_wait_seconds(version),
        )
    except KeyError as exc:
        cli_errors.emit_error(
            cli_errors.ValidationError(f"waiver document is missing the {exc} key"), flags=flags
        )
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    emit_json_or_text(result, _receipts_text(result), flags=flags)
    if result.get("refused"):
        raise typer.Exit(exit_codes.STATE_CONFLICT)


@release_app.command("advance")
def release_advance(
    ctx: typer.Context,
    release_key: Annotated[
        str, typer.Argument(help="Release key the train walks past, e.g. REL-0.7.0.dev2.")
    ],
) -> None:
    """Walk the train past one finished checkpoint, or refuse and change nothing.

    The daemon reads the checkpoint's record and its newest receipts from
    the stores. The train moves only when the checkpoint is the rung it
    has open, stands at baked or released, and every required gate
    carries a fresh receipt bound to its exact source and manifest. A
    missing or stale receipt refuses ``prerequisite_receipt_stale``, and
    ``eawf release receipts`` earns fresh ones.

    The advance opens no record. Open the next checkpoint with
    ``eawf release create``, which runs its measured admission.
    """
    flags: GlobalFlags = ctx.obj
    try:
        result = _dispatch(RELEASE_RPC_METHODS["advance"], {"release_key": release_key})
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    closed = result.get("closed") or {}
    train = result.get("train") or {}
    opened = next(
        (
            row.get("version")
            for row in train.get("checkpoints") or ()
            if isinstance(row, dict) and row.get("status") == "open"
        ),
        None,
    )
    text = (
        f"advanced past {closed.get('key')} ({closed.get('status')}) -> "
        f"{train.get('current_checkpoint')} open\n"
        f"  index: {train.get('current_checkpoint_index')}\n"
        f"  receipts: {', '.join(result.get('receipt_refs') or ()) or '(none)'}\n"
        f"  next: eawf release create {opened}"
    )
    emit_json_or_text(result, text, flags=flags)


__all__ = ["release_advance", "release_receipts"]
