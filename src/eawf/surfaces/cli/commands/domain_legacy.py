"""The operator verbs that finish imported epoch-1 work after the cutover.

Once a repository is cut over to epoch 2, the epoch-1 ``wave``, ``iter``
and ``phase`` verbs refuse, and the native per-entity verbs refuse every
record the cutover imported. These commands are the operator's spelling of
the two daemon verbs that take their place for that imported work:

- ``task advance-legacy``, ``batch close-legacy``, ``milestone
  close-legacy`` and ``milestone cancel-legacy`` send
  ``domain.legacy.advance``, addressing the record by its epoch-1 id;
- ``record append`` sends ``domain.record.append`` with an audit, decision
  or artifact read from ``--from-spec``.

As in :mod:`eawf.surfaces.cli.commands.domain`, the commands are dispatch
and rendering only: the edge table, the child checks, the evidence
resolution and the decisive gates a Task completion runs are all decided
daemon-side. A Task completion runs its gates inside that request, so the
wire timeout is the mutation ceiling a gated close gets rather than the
default one.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import typer
from pydantic import ValidationError as PydanticValidationError

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError
from eawf.surfaces.cli.commands.domain import (
    DOMAIN_REFUSAL_EXIT,
    _rpc_refusal,
)
from eawf.surfaces.cli.commands.lifecycle import batch_app, milestone_app, task_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.verb_contract import read_spec_document

if TYPE_CHECKING:
    from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope

#: The dotted JSON-RPC names these commands forward to, spelled here so the
#: Typer tree builds without the daemon method registry on the path.
LEGACY_ADVANCE: Final = "domain.legacy.advance"
RECORD_APPEND: Final = "domain.record.append"

record_app = typer.Typer(
    name="record",
    help="Append audit, decision and artifact records to an epoch-2 tree.",
    no_args_is_help=True,
)

_KEY_HELP: Final = "Epoch-1 id of the imported record (wave, iter, phase or backlog id)."
_ACTOR_HELP: Final = "Principal key the move is attributed to."
_REASON_HELP: Final = "Why the record moves, recorded on its continuation event."
_EVIDENCE_HELP: Final = "Id of a recorded audit, decision or artifact the move cites (repeatable)."
_TO_HELP: Final = "Target status: CLAIMED, RUNNING, COMPLETED or DROPPED."
_KIND_HELP: Final = "Record kind: audit, decision or artifact."
_RECORD_SPEC_HELP: Final = "JSON file carrying the whole record, validated daemon-side."


def _send(
    method: str, params: dict[str, Any], *, subject: str, verb_text: str, flags: GlobalFlags
) -> DomainEnvelope:
    """Send one request to the daemon and return the envelope it answered.

    Raises:
        UserError: ``--daemonless`` was asked for; both verbs are mutations.
        DaemonUnreachable: The daemon could not be reached.
        InternalError: The daemon answered something that is not an envelope.
        CliError: The daemon answered a transport-level failure.
    """
    from eawf.runtime.daemon.limits import cli_mutation_timeout_for, configured_juror_wall_clock
    from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope
    from eawf.surfaces.cli import _dispatch

    repo_root = (flags.workspace or Path.cwd()).resolve()
    timeout = cli_mutation_timeout_for(configured_juror_wall_clock(repo_root))
    try:
        _dispatch.escalate_mutation(verb_text, flags=flags)
        with DaemonClient(call_timeout_seconds=timeout) as client:
            answer = client.call(method, {"repo_root": str(repo_root), **params})
    except DaemonRpcError as exc:
        return _rpc_refusal(exc, method=method, urn=subject)
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise cli_errors.DaemonUnreachable(f"daemon unavailable for {method}: {exc}") from exc
    try:
        return DomainEnvelope.model_validate(answer)
    except PydanticValidationError as exc:
        raise cli_errors.InternalError(
            f"{method} answered something that is not a domain envelope: {exc}"
        ) from exc


def _envelope_text(envelope: DomainEnvelope, *, subject: str) -> str:
    """Return the human-readable rendering of one continuation answer."""
    from eawf.runtime.daemon.methods.domain_envelope import DomainStatus

    if envelope.status is DomainStatus.OK:
        result = envelope.result or {}
        if "to_status" in result:
            lines = [
                f"{envelope.operation} ok {subject} "
                f"{result['from_status']} -> {result['to_status']}"
            ]
            lines.extend(
                f"  gate {item['gate_id']} {item['status']} receipt {item['receipt_id']}"
                for item in result.get("gate_receipts", ())
            )
        else:
            lines = [f"{envelope.operation} ok {subject} in {result.get('collection')} ledger"]
    else:
        lines = []
        for row in envelope.errors:
            lines.append(f"{envelope.operation} error {row.code.value} {row.entity_ref}")
            lines.append(f"  {row.message}")
            if row.guard is not None:
                lines.append(f"  guard: {row.guard}")
            lines.append(f"  remediation: {row.remediation}")
    lines.extend(f"  warning: {warning}" for warning in envelope.warnings)
    return "\n".join(lines)


def _run(
    ctx: typer.Context, method: str, params: dict[str, Any], *, subject: str, verb_text: str
) -> None:
    """Dispatch one verb, print its answer, and exit non-zero on a refusal."""
    from eawf.runtime.daemon.methods.domain_envelope import DomainStatus

    flags: GlobalFlags = ctx.obj
    try:
        envelope = _send(method, params, subject=subject, verb_text=verb_text, flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    emit_json_or_text(
        envelope.model_dump(mode="json"), _envelope_text(envelope, subject=subject), flags=flags
    )
    if envelope.status is not DomainStatus.OK:
        raise typer.Exit(DOMAIN_REFUSAL_EXIT)


def _advance(
    ctx: typer.Context,
    *,
    key: str,
    collection: str,
    to: str,
    actor: str,
    reason: str,
    evidence_ref: list[str] | None,
    verb_text: str,
) -> None:
    """Send one ``domain.legacy.advance`` request."""
    params = {
        "key": key,
        "collection": collection,
        "to": to,
        "reason": reason,
        "evidence_refs": list(evidence_ref or ()),
        "actor": actor,
    }
    _run(ctx, LEGACY_ADVANCE, params, subject=f"legacy:{collection}/{key}", verb_text=verb_text)


@task_app.command("advance-legacy")
def task_advance_legacy_cmd(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help=_KEY_HELP)],
    to: Annotated[str, typer.Option("--to", help=_TO_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    reason: Annotated[str, typer.Option("--reason", help=_REASON_HELP)],
    evidence_ref: Annotated[
        list[str] | None, typer.Option("--evidence-ref", help=_EVIDENCE_HELP)
    ] = None,
) -> None:
    """Move an imported Task (wave or backlog row) along the legacy edge table.

    Completing one runs its decisive gates daemon-side and refuses, naming
    the gate, when one does not pass.
    """
    _advance(
        ctx,
        key=key,
        collection="task",
        to=to,
        actor=actor,
        reason=reason,
        evidence_ref=evidence_ref,
        verb_text="task advance-legacy",
    )


@batch_app.command("close-legacy")
def batch_close_legacy_cmd(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    reason: Annotated[str, typer.Option("--reason", help=_REASON_HELP)],
    evidence_ref: Annotated[
        list[str] | None, typer.Option("--evidence-ref", help=_EVIDENCE_HELP)
    ] = None,
) -> None:
    """Complete an imported ACTIVE Batch (iter) once every Task in it is terminal."""
    _advance(
        ctx,
        key=key,
        collection="batch",
        to="COMPLETED",
        actor=actor,
        reason=reason,
        evidence_ref=evidence_ref,
        verb_text="batch close-legacy",
    )


@milestone_app.command("close-legacy")
def milestone_close_legacy_cmd(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    reason: Annotated[str, typer.Option("--reason", help=_REASON_HELP)],
    evidence_ref: Annotated[list[str], typer.Option("--evidence-ref", help=_EVIDENCE_HELP)],
) -> None:
    """Complete an imported ACTIVE Milestone (phase) against a recorded audit."""
    _advance(
        ctx,
        key=key,
        collection="milestone",
        to="COMPLETED",
        actor=actor,
        reason=reason,
        evidence_ref=evidence_ref,
        verb_text="milestone close-legacy",
    )


@milestone_app.command("cancel-legacy")
def milestone_cancel_legacy_cmd(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    reason: Annotated[str, typer.Option("--reason", help=_REASON_HELP)],
    evidence_ref: Annotated[list[str], typer.Option("--evidence-ref", help=_EVIDENCE_HELP)],
) -> None:
    """Cancel an imported PLANNED Milestone against a recorded decision or artifact."""
    _advance(
        ctx,
        key=key,
        collection="milestone",
        to="CANCELLED",
        actor=actor,
        reason=reason,
        evidence_ref=evidence_ref,
        verb_text="milestone cancel-legacy",
    )


@record_app.command("append")
def record_append_cmd(
    ctx: typer.Context,
    kind: Annotated[str, typer.Option("--kind", help=_KIND_HELP)],
    from_spec: Annotated[Path, typer.Option("--from-spec", help=_RECORD_SPEC_HELP)],
) -> None:
    """Append one audit, decision or artifact to the generation's ledger."""
    flags: GlobalFlags = ctx.obj
    try:
        record = read_spec_document(from_spec)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    subject = f"{kind}/{record.get('id', '?')}"
    _run(
        ctx,
        RECORD_APPEND,
        {"kind": kind, "record": record},
        subject=subject,
        verb_text="record append",
    )


__all__ = [
    "LEGACY_ADVANCE",
    "RECORD_APPEND",
    "batch_close_legacy_cmd",
    "milestone_cancel_legacy_cmd",
    "milestone_close_legacy_cmd",
    "record_app",
    "record_append_cmd",
    "task_advance_legacy_cmd",
]
