"""``eawf plan`` -- the plan-revision verbs.

``eawf plan submit`` / ``approve`` / ``apply`` are mutating dispatch
verbs: each builds the wire parameters for one
``planning.plan_revision.*`` JSON-RPC call, sends it to the daemon, and
renders whatever
:class:`~eawf.runtime.daemon.methods.domain_envelope.DomainEnvelope`
comes back.

Exit codes (the canonical 0..5 surface; see
:mod:`eawf.surfaces.cli.exit_codes`):

- ``0`` on success.
- ``1`` (``USER_ERROR``) for an operator-fixable input problem: a
  missing or malformed ``--from-spec`` document or a non-positive
  ``--expected-revision``.
- ``3`` (``STATE_CONFLICT``) when ``submit`` / ``approve`` / ``apply``
  is refused by a domain guard (a stale revision, the epoch-2 fence,
  etc.) — :data:`~eawf.surfaces.cli.commands.domain.DOMAIN_REFUSAL_EXIT`.
- ``4`` (``DAEMON_UNREACHABLE``) when a plan-revision verb cannot reach
  the daemon.
- ``5`` (``INTERNAL_ERROR``) when the daemon answers something that is
  not a valid envelope, or on any other uncaught error.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import typer

from eawf.surfaces.cli import errors, exit_codes
from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.verb_contract import envelope_exit_code, envelope_text, read_spec_document

if TYPE_CHECKING:
    from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope, DomainErrorCode

logger = logging.getLogger(__name__)


#: The dotted JSON-RPC name each plan-revision command forwards to.
#: Spelled here rather than imported so the Typer tree builds without the
#: daemon method registry on the path; the contract test asserts this
#: table against the daemon's own, so a renamed verb reds rather than
#: drifts.
PLAN_SUBMIT_METHOD: Final = "planning.plan_revision.submit"
PLAN_APPROVE_METHOD: Final = "planning.plan_revision.approve"
PLAN_APPLY_METHOD: Final = "planning.plan_revision.apply"

#: Every plan-revision verb this module exposes, in registration order.
PLAN_REVISION_CLI_METHODS: Final[tuple[str, ...]] = (
    PLAN_SUBMIT_METHOD,
    PLAN_APPROVE_METHOD,
    PLAN_APPLY_METHOD,
)

#: What an operator does about a request the epoch-2 fence turned away.
#: The code and the message on that path are the daemon's; only this
#: sentence is written here, because the fence sends no remediation.
_FENCE_REMEDIATION: Final = (
    "Activate the tree as an epoch-2 canary before calling a native planning verb."
)

_PROPOSAL_SPEC_HELP: Final = (
    "JSON file carrying the plan-revision proposal (key, author, body, parent_key)."
)
_PLAN_KEY_HELP: Final = "The plan-revision key (PRV-####)."
_PLAN_REVISION_HELP: Final = "Revision the plan revision was read at (compare-and-swap token)."
_IDEMPOTENCY_KEY_HELP: Final = "Caller's name for this request; a retry replays its receipt."
_ACTOR_HELP: Final = "Principal key the request is attributed to."
_ACTION_REF_HELP: Final = "URN of the sealed PendingAction the approval is recorded against."
_APPROVED_BY_HELP: Final = "Operator principal id approving; must be a human, not an agent."


plan_app = typer.Typer(
    name="plan",
    help="List, show, submit, approve and apply plan revisions.",
    no_args_is_help=True,
    add_completion=False,
)


# ---- Plan revision (submit / approve / apply) --------------------------------
#
# These three verbs are dispatch only, exactly as the native lifecycle and
# create verbs in :mod:`eawf.surfaces.cli.commands.domain` are: a handler
# builds the wire parameters, sends the one RPC its name promises, and
# renders whatever :class:`DomainEnvelope` comes back unchanged. The
# fence-refusal reconstruction and the refusal exit are shared with that
# module rather than re-invented, because a plan-revision verb is fenced
# and answered exactly as any other native verb is.


def _declared_code(message: str) -> DomainErrorCode | None:
    """Return the domain code a daemon validation message leads with.

    The epoch-2 fence refuses a native call before any handler runs, so
    its answer arrives as a JSON-RPC error rather than as an envelope.
    The stable code still leads the message, and lifting it back out is
    what lets that refusal be rendered in the same shape as every other.

    Args:
        message: The JSON-RPC error message.

    Returns:
        The declared code, or ``None`` when the message leads with
        something outside the domain vocabulary.
    """
    from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode

    parts = message.split(": ", 2)
    if len(parts) < 2:
        return None
    try:
        return DomainErrorCode(parts[1] if parts[0] == "validation_failed" else parts[0])
    except ValueError:
        return None


def _fence_refusal(error: DaemonRpcError, *, operation: str, subject: str) -> DomainEnvelope:
    """Return the envelope one pre-transaction JSON-RPC error stands for.

    Args:
        error: What the daemon answered.
        operation: The dotted JSON-RPC name that earned it.
        subject: The plan revision the request addressed.

    Returns:
        An ``error`` envelope carrying the daemon's own code and message.
        Neither revision is filled, because the refusal happened before
        any record was read.

    Raises:
        CliError: The error is not a domain refusal -- a transport
            failure, a missing method, a lock timeout -- and belongs in
            the CLI's own error taxonomy rather than in an envelope.
    """
    from eawf.runtime.daemon.methods.domain_envelope import (
        ENVELOPE_SCHEMA_VERSION,
        DomainEnvelope,
        DomainError,
        DomainStatus,
    )

    code = _declared_code(error.message)
    if error.code != errors.RPC_VALIDATION_FAILED or code is None:
        raise errors.cli_error_for_rpc(error.code, error.message)
    logger.info(f"_fence_refusal method={operation} code={code.value}")
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=operation,
        errors=(
            DomainError(
                code=code,
                message=error.message.removeprefix("validation_failed: "),
                entity_ref=subject,
                remediation=_FENCE_REMEDIATION,
            ),
        ),
    )


def _send_plan_rpc(
    method: str, params: dict[str, Any], *, flags: GlobalFlags, subject: str
) -> DomainEnvelope:
    """Send one plan-revision RPC and return the envelope it stands for.

    Args:
        method: The dotted JSON-RPC name to send.
        params: The wire parameters, less ``repo_root``.
        flags: Resolved global flags (the workspace anchor and the
            ``--daemonless`` source).
        subject: The plan revision the request addresses, for a fence
            refusal taken before any record was read.

    Returns:
        The machine envelope, whether the request committed or was
        refused.

    Raises:
        UserError: ``--daemonless`` was asked for. Every verb here is a
            mutation, and the daemon is the only canonical mutator.
        DaemonUnreachable: The daemon could not be reached.
        InternalError: The daemon answered something that is not an
            envelope, which a client cannot branch on.
        CliError: The daemon answered a transport-level failure.
    """
    from pydantic import ValidationError as PydanticValidationError

    from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope
    from eawf.surfaces.cli import _dispatch

    repo_root = str((flags.workspace or Path.cwd()).resolve())
    wire_params = {"repo_root": repo_root, **params}
    try:
        _dispatch.escalate_mutation(method.removeprefix("planning.plan_revision."), flags=flags)
        with DaemonClient() as client:
            answer = client.call(method, wire_params)
    except DaemonRpcError as exc:
        return _fence_refusal(exc, operation=method, subject=subject)
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise errors.DaemonUnreachable(f"daemon unavailable for {method}: {exc}") from exc
    try:
        return DomainEnvelope.model_validate(answer)
    except PydanticValidationError as exc:
        raise errors.InternalError(
            f"{method} answered something that is not a domain envelope: {exc}"
        ) from exc


def _emit_plan_envelope(envelope: DomainEnvelope, *, flags: GlobalFlags) -> None:
    """Print one plan-revision envelope and exit non-zero when it refused.

    Args:
        envelope: The daemon's answer.
        flags: Resolved global flags.

    Raises:
        typer.Exit: With the envelope's typed exit status when the request
            was refused. The envelope prints first either
            way, so a caller reading stdout gets the code whichever branch
            it took.
    """
    subject = envelope.result.get("entity_ref", "?") if envelope.result else "?"
    emit_json_or_text(
        envelope.model_dump(mode="json"),
        envelope_text(envelope, urn=subject),
        flags=flags,
    )
    code = envelope_exit_code(envelope)
    if code != exit_codes.OK:
        raise typer.Exit(code)


def _run_plan_rpc(method: str, params: dict[str, Any], *, flags: GlobalFlags, subject: str) -> None:
    """Send one plan-revision RPC and render whatever it answers.

    The single body every command in this section delegates to once its
    own flags are resolved into wire parameters: send the request, print
    the envelope, or print a taxonomy'd CLI error when the daemon could
    not be reached or answered something unusable.

    Args:
        method: The dotted JSON-RPC name to send.
        params: The wire parameters, less ``repo_root``.
        flags: Resolved global flags.
        subject: The plan revision the request addresses.
    """
    try:
        envelope = _send_plan_rpc(method, params, flags=flags, subject=subject)
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    _emit_plan_envelope(envelope, flags=flags)


@plan_app.command("submit")
def plan_submit_cmd(
    ctx: typer.Context,
    from_spec: Annotated[Path, typer.Option("--from-spec", help=_PROPOSAL_SPEC_HELP)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_IDEMPOTENCY_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
) -> None:
    """Record one planner proposal as a VALIDATED plan revision."""
    from pydantic import ValidationError as PydanticValidationError

    from eawf.workflow.planning.apply import PlanRevisionProposal

    flags: GlobalFlags = ctx.obj
    try:
        raw = read_spec_document(from_spec)
        proposal = PlanRevisionProposal.model_validate(raw)
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    except PydanticValidationError as exc:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in exc.errors()})
        errors.emit_error(
            errors.UserError(
                f"--from-spec {from_spec} is not a valid proposal; check {', '.join(fields)}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    params: dict[str, Any] = {
        "proposal": proposal.model_dump(mode="json"),
        "actor": actor,
        "idempotency_key": idempotency_key,
    }
    _run_plan_rpc(PLAN_SUBMIT_METHOD, params, flags=flags, subject=proposal.key)


@plan_app.command("approve")
def plan_approve_cmd(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help=_PLAN_KEY_HELP)],
    expected_revision: Annotated[
        int,
        typer.Option("--expected-revision", "--expected-plan-revision", help=_PLAN_REVISION_HELP),
    ],
    action_ref: Annotated[str, typer.Option("--action-ref", help=_ACTION_REF_HELP)],
    approved_by: Annotated[str, typer.Option("--approved-by", help=_APPROVED_BY_HELP)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_IDEMPOTENCY_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
) -> None:
    """Seal a human principal's approval onto a VALIDATED plan revision.

    ``--approved-by`` is always recorded as an operator principal: the
    daemon requires a plan approval's human to be one, so naming any other
    kind here would only ever earn the daemon's own refusal.
    """
    flags: GlobalFlags = ctx.obj
    if expected_revision <= 0:
        errors.emit_error(
            errors.UserError(
                f"--expected-revision must be a positive revision, got {expected_revision}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    params: dict[str, Any] = {
        "key": key,
        "expected_revision": expected_revision,
        "action_ref": action_ref,
        "approved_by": {"principal_kind": "operator", "principal_id": approved_by},
        "actor": actor,
        "idempotency_key": idempotency_key,
    }
    _run_plan_rpc(PLAN_APPROVE_METHOD, params, flags=flags, subject=key)


@plan_app.command("apply")
def plan_apply_cmd(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help=_PLAN_KEY_HELP)],
    expected_revision: Annotated[
        int,
        typer.Option("--expected-revision", "--expected-plan-revision", help=_PLAN_REVISION_HELP),
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_IDEMPOTENCY_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
) -> None:
    """Materialise one APPROVED plan revision into its Milestone."""
    flags: GlobalFlags = ctx.obj
    if expected_revision <= 0:
        errors.emit_error(
            errors.UserError(
                f"--expected-revision must be a positive revision, got {expected_revision}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    params: dict[str, Any] = {
        "key": key,
        "expected_revision": expected_revision,
        "actor": actor,
        "idempotency_key": idempotency_key,
    }
    _run_plan_rpc(PLAN_APPLY_METHOD, params, flags=flags, subject=key)


__all__ = [
    "PLAN_APPLY_METHOD",
    "PLAN_APPROVE_METHOD",
    "PLAN_REVISION_CLI_METHODS",
    "PLAN_SUBMIT_METHOD",
    "plan_apply_cmd",
    "plan_approve_cmd",
    "plan_submit_cmd",
]
