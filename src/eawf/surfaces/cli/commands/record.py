"""The ``record`` verbs that file ledger records in an epoch-2 tree.

``record append`` sends ``domain.record.append`` with an audit, decision or
artifact read from ``--from-spec``; ``record evidence`` is added to the same
group by :mod:`eawf.surfaces.cli.commands.domain_integration`.

As in :mod:`eawf.surfaces.cli.commands.domain`, the command is dispatch and
rendering only: the record is validated and filed daemon-side.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import typer
from pydantic import ValidationError as PydanticValidationError

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError
from eawf.surfaces.cli.commands.domain import _rpc_refusal
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.verb_contract import emit_envelope, read_spec_document

if TYPE_CHECKING:
    from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope

#: The dotted JSON-RPC name ``record append`` forwards to, spelled here so the
#: Typer tree builds without the daemon method registry on the path.
RECORD_APPEND: Final = "domain.record.append"

record_app = typer.Typer(
    name="record",
    help="Append audit, decision and artifact records to an epoch-2 tree.",
    no_args_is_help=True,
)

_KIND_HELP: Final = "Record kind: audit, decision or artifact."
_RECORD_SPEC_HELP: Final = "JSON file carrying the whole record, validated daemon-side."


def _send(params: dict[str, Any], *, subject: str, flags: GlobalFlags) -> DomainEnvelope:
    """Send one ``domain.record.append`` request and return the envelope it answered.

    Raises:
        UserError: ``--daemonless`` was asked for; the verb is a mutation.
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
        _dispatch.escalate_mutation("record append", flags=flags)
        with DaemonClient(call_timeout_seconds=timeout) as client:
            answer = client.call(RECORD_APPEND, {"repo_root": str(repo_root), **params})
    except DaemonRpcError as exc:
        return _rpc_refusal(exc, method=RECORD_APPEND, urn=subject)
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise cli_errors.DaemonUnreachable(
            f"daemon unavailable for {RECORD_APPEND}: {exc}"
        ) from exc
    try:
        return DomainEnvelope.model_validate(answer)
    except PydanticValidationError as exc:
        raise cli_errors.InternalError(
            f"{RECORD_APPEND} answered something that is not a domain envelope: {exc}"
        ) from exc


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
        subject = f"{kind}/{record.get('id', '?')}"
        envelope = _send({"kind": kind, "record": record}, subject=subject, flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    emit_envelope(envelope, urn=subject, flags=flags)


# ---- Command registration ---------------------------------------------------
# ``domain_integration`` adds ``record evidence`` to :data:`record_app`, so it
# loads here, once the app exists, whichever module a process imports first.
from eawf.surfaces.cli.commands import (  # noqa: E402
    domain_integration as _domain_integration,  # noqa: F401
)

__all__ = [
    "RECORD_APPEND",
    "record_app",
    "record_append_cmd",
]
