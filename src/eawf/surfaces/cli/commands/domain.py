"""The operator surface of the native per-entity lifecycle verbs.

The daemon registers one JSON-RPC verb per lifecycle move a Track, a
Milestone, a Batch or a Task can make. Until this module there was no way
to drive one of them except by hand-writing a JSON-RPC frame, which makes
the canary milestone reachable only by a client nobody ships. Each verb
here is the operator's spelling of exactly one of those RPCs.

The commands are dispatch and nothing else. A handler parses its flags,
builds one :class:`DomainVerbRequest`, hands it to the daemon and renders
the answer; every predicate about whether the move is legal -- the edge,
the guards, the compare-and-swap token, the sealed approval an acceptance
needs -- is decided daemon-side, because the daemon is the only process
that holds the document under a lock while it decides.

Three things follow from that and are worth stating, because each is a
shortcut this module deliberately does not take:

- **The verb is fixed per command.** ``eawf milestone activate`` sends
  ``domain.milestone.activate`` whatever URN it is handed. A URN of
  another kind is forwarded unchanged and comes back refused with
  ``identity_kind_mismatch``. Routing on the URN instead would be a
  second dispatcher, and the daemon's own guard exists precisely because
  the transaction derives its machine from the URN rather than the verb.
- **A refusal is rendered, not re-worded.** The daemon answers a denial
  as a machine envelope carrying a stable code, its own message, the
  guard that failed and a remediation. All four are printed as they
  arrived. A CLI that re-spelled them would be a second error vocabulary
  able to drift from the one clients branch on.
- **There is no create verb here.** A record is admitted by the daemon's
  ``domain.<entity>.create`` verbs, whose payload is a whole create
  document rather than a move of a record that exists, so this module of
  moves offers none.

Every verb takes the same four addressing flags -- the subject URN, the
revision the caller read it at, the retry key and the actor -- and reads
the rest of the request from a ``--from-spec`` JSON file, so the payload
of a move is a reviewable artifact rather than a shell line. A refused
envelope still prints in full and then exits :data:`DOMAIN_REFUSAL_EXIT`,
so a script can branch on the exit status without losing the code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import orjson
import typer
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError
from eawf.surfaces.cli.commands.lifecycle import batch_app, milestone_app, task_app, track_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope, DomainErrorCode

logger = logging.getLogger(__name__)


#: The dotted JSON-RPC name each command forwards to. Spelled here rather
#: than imported so the Typer tree builds without the daemon method
#: registry on the path; the contract test asserts this table against the
#: daemon's own, so a renamed verb reds rather than drifts.
TRACK_RETIRE: Final = "domain.track.retire"
MILESTONE_ACTIVATE: Final = "domain.milestone.activate"
MILESTONE_OPEN_REVIEW: Final = "domain.milestone.open_review"
MILESTONE_ACCEPT: Final = "domain.milestone.accept"
MILESTONE_CANCEL: Final = "domain.milestone.cancel"
BATCH_ACTIVATE: Final = "domain.batch.activate"
BATCH_READY: Final = "domain.batch.ready"
TASK_PROMOTE: Final = "domain.task.promote"
TASK_START: Final = "domain.task.start"

#: Every verb this module exposes, in registration order.
DOMAIN_CLI_METHODS: Final[tuple[str, ...]] = (
    TRACK_RETIRE,
    MILESTONE_ACTIVATE,
    MILESTONE_OPEN_REVIEW,
    MILESTONE_ACCEPT,
    MILESTONE_CANCEL,
    BATCH_ACTIVATE,
    BATCH_READY,
    TASK_PROMOTE,
    TASK_START,
)

#: How long a retry key may be. Mirrors the bound the request model
#: enforces daemon-side; the contract test pins the two together. The
#: check runs here as well so a key that cannot possibly be accepted
#: costs no round trip.
IDEMPOTENCY_KEY_MAX: Final = 128

#: The exit status of a refused mutation. One code for every refusal: the
#: code a caller routes on is the one inside the envelope, and a second
#: mapping from that vocabulary onto exit statuses would be a second
#: contract to keep stable.
DOMAIN_REFUSAL_EXIT: Final = exit_codes.STATE_CONFLICT

#: What an operator does about a request the epoch-2 fence turned away.
#: The code and the message on that path are the daemon's; only this
#: sentence is written here, because the fence sends no remediation.
_FENCE_REMEDIATION: Final = (
    "Activate the tree as an epoch-2 canary before calling a native lifecycle verb."
)

_URN_HELP: Final = "URN of the record to move."
_REVISION_HELP: Final = "Revision the record was read at (compare-and-swap token)."
_KEY_HELP: Final = "Caller's name for this request; a retry replays its receipt."
_ACTOR_HELP: Final = "Principal key the move is attributed to."
_SPEC_HELP: Final = "JSON file carrying updates, observations, reason_code, binding_refs."
_APPROVAL_HELP: Final = "URN of the sealed PendingAction receipt the acceptance was approved by."


class DomainVerbSpec(BaseModel):
    """The payload half of one native lifecycle request.

    Addressing (which record, at which revision, under which retry key,
    by which actor) stays on the command line; everything the move
    carries into the document lives here, so the payload of a mutation is
    a file that can be reviewed and replayed.

    The field values are forwarded as given. The strict models for an
    observation row and for an update field are the daemon's, and
    re-spelling them here would be a second schema able to disagree with
    the one that actually decides the request.

    Attributes:
        updates: Field values the target status makes facts.
        observations: Facts about the outside world the caller presents.
        reason_code: The stable reason the move happened.
        binding_refs: The exact proofs the move was taken against.
        correlation_id: The caller's thread of related requests.
    """

    model_config = ConfigDict(extra="forbid")

    updates: dict[str, Any] = Field(default_factory=dict)
    observations: tuple[dict[str, Any], ...] = ()
    reason_code: str | None = None
    binding_refs: tuple[str, ...] = ()
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class DomainVerbRequest:
    """One native lifecycle request, as the CLI resolved it.

    Attributes:
        method: The dotted JSON-RPC name to send.
        urn: The record to move.
        expected_revision: The compare-and-swap token.
        idempotency_key: The caller's name for this request.
        actor: The principal the move is attributed to.
        spec: The payload half of the request.
        approval_receipt_ref: The sealed approval reference, for the one
            verb that requires one; ``None`` everywhere else.
    """

    method: str
    urn: str
    expected_revision: int
    idempotency_key: str
    actor: str
    spec: DomainVerbSpec = field(default_factory=DomainVerbSpec)
    approval_receipt_ref: str | None = None


def _load_spec(path: Path | None) -> DomainVerbSpec:
    """Return the payload the caller named, or an empty one.

    Args:
        path: The ``--from-spec`` file, or ``None``.

    Returns:
        The parsed payload.

    Raises:
        UserError: The file is missing, is not JSON, or names a field the
            request has no room for. Parsing fails at the boundary so an
            unusable payload never reaches the wire.
    """
    if path is None:
        return DomainVerbSpec()
    try:
        raw = orjson.loads(path.read_bytes())
    except OSError as exc:
        raise cli_errors.UserError(f"cannot read --from-spec {path}: {exc}", kind="NotFound") from (
            exc
        )
    except orjson.JSONDecodeError as exc:
        raise cli_errors.UserError(
            f"--from-spec {path} is not valid JSON: {exc}", kind="InvalidInput"
        ) from exc
    try:
        return DomainVerbSpec.model_validate(raw)
    except PydanticValidationError as exc:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in exc.errors()})
        raise cli_errors.UserError(
            f"--from-spec {path} is not a valid request payload; check {', '.join(fields)}",
            kind="InvalidInput",
        ) from exc


def _build_request(
    *,
    method: str,
    urn: str,
    expected_revision: int,
    idempotency_key: str,
    actor: str,
    from_spec: Path | None,
    approval_receipt_ref: str | None = None,
) -> DomainVerbRequest:
    """Return the typed request one command's flags stand for.

    Args:
        method: The dotted JSON-RPC name the command forwards to.
        urn: The record to move.
        expected_revision: The compare-and-swap token.
        idempotency_key: The caller's name for this request.
        actor: The principal the move is attributed to.
        from_spec: The payload file, or ``None``.
        approval_receipt_ref: The sealed approval reference, or ``None``.

    Returns:
        The request to dispatch.

    Raises:
        UserError: A bound the request model could not accept was broken.
            The daemon refuses these too; refusing here keeps a request
            that cannot succeed off the wire.
    """
    if expected_revision <= 0:
        raise cli_errors.UserError(
            f"--expected-revision must be a positive revision, got {expected_revision}",
            kind="InvalidInput",
        )
    if not 1 <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX:
        raise cli_errors.UserError(
            f"--idempotency-key must be 1 to {IDEMPOTENCY_KEY_MAX} characters, "
            f"got {len(idempotency_key)}",
            kind="InvalidInput",
        )
    return DomainVerbRequest(
        method=method,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        spec=_load_spec(from_spec),
        approval_receipt_ref=approval_receipt_ref,
    )


def _rpc_params(request: DomainVerbRequest, *, repo_root: str) -> dict[str, Any]:
    """Return the wire parameters of one request.

    The approval reference is omitted rather than sent as null when the
    verb does not carry one: the request models forbid unknown fields, so
    a key no verb declares would be refused as a schema failure rather
    than as the thing that is actually wrong.

    Args:
        request: The resolved request.
        repo_root: The tree the request addresses.

    Returns:
        The JSON-RPC parameters.
    """
    params: dict[str, Any] = {
        "repo_root": repo_root,
        "urn": request.urn,
        "expected_revision": request.expected_revision,
        "idempotency_key": request.idempotency_key,
        "actor": request.actor,
        "observations": list(request.spec.observations),
        "updates": dict(request.spec.updates),
        "reason_code": request.spec.reason_code,
        "binding_refs": list(request.spec.binding_refs),
        "correlation_id": request.spec.correlation_id,
    }
    if request.approval_receipt_ref is not None:
        params["approval_receipt_ref"] = request.approval_receipt_ref
    return params


def _operator_verb(method: str) -> str:
    """Return the command spelling of a dotted RPC name.

    The escalation gate names the verb it refused in the message an
    operator reads, and that operator typed ``milestone open-review``
    rather than ``domain.milestone.open_review``.

    Args:
        method: The dotted JSON-RPC name.

    Returns:
        The command spelling, as ``<noun> <verb>``.
    """
    return method.removeprefix("domain.").replace(".", " ").replace("_", "-")


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


def _rpc_refusal(error: DaemonRpcError, *, request: DomainVerbRequest) -> DomainEnvelope:
    """Return the envelope one JSON-RPC error stands for.

    Args:
        error: What the daemon answered.
        request: The request that earned it.

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
    if error.code != cli_errors.RPC_VALIDATION_FAILED or code is None:
        raise cli_errors.cli_error_for_rpc(error.code, error.message)
    logger.info(f"_rpc_refusal method={request.method} code={code.value}")
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=request.method,
        errors=(
            DomainError(
                code=code,
                message=error.message.removeprefix("validation_failed: "),
                entity_ref=request.urn,
                remediation=_FENCE_REMEDIATION,
            ),
        ),
    )


def _call_domain_verb(request: DomainVerbRequest, *, flags: GlobalFlags) -> DomainEnvelope:
    """Send one request to the daemon and return the answer it stands for.

    Args:
        request: The resolved request.
        flags: Resolved global flags (the workspace anchor and the
            ``--daemonless`` source).

    Returns:
        The machine envelope, whether the mutation committed or was
        refused.

    Raises:
        UserError: ``--daemonless`` was asked for. Every verb here is a
            mutation, and the daemon is the only canonical mutator.
        DaemonUnreachable: The daemon could not be reached.
        InternalError: The daemon answered something that is not an
            envelope, which a client cannot branch on.
        CliError: The daemon answered a transport-level failure.
    """
    from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope
    from eawf.surfaces.cli import _dispatch

    repo_root = str((flags.workspace or Path.cwd()).resolve())
    try:
        _dispatch.escalate_mutation(_operator_verb(request.method), flags=flags)
        with DaemonClient() as client:
            answer = client.call(request.method, _rpc_params(request, repo_root=repo_root))
    except DaemonRpcError as exc:
        return _rpc_refusal(exc, request=request)
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise cli_errors.DaemonUnreachable(
            f"daemon unavailable for {request.method}: {exc}"
        ) from exc
    try:
        return DomainEnvelope.model_validate(answer)
    except PydanticValidationError as exc:
        raise cli_errors.InternalError(
            f"{request.method} answered something that is not a domain envelope: {exc}"
        ) from exc


def _envelope_text(envelope: DomainEnvelope, *, request: DomainVerbRequest) -> str:
    """Return the human-readable rendering of one envelope.

    Args:
        envelope: The daemon's answer.
        request: The request that earned it, which names the subject a
            refusal taken before any read cannot.

    Returns:
        The text body: one headline plus a line per warning, and for a
        refusal the message, the guard that failed and the remediation.
    """
    from eawf.runtime.daemon.methods.domain_envelope import DomainStatus

    if envelope.status is DomainStatus.OK:
        lines = [
            f"{envelope.operation} ok {request.urn} "
            f"revision {envelope.revision_before} -> {envelope.revision_after}"
        ]
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


def _emit(envelope: DomainEnvelope, *, request: DomainVerbRequest, flags: GlobalFlags) -> None:
    """Print one envelope and exit non-zero when it refused.

    Args:
        envelope: The daemon's answer.
        request: The request that earned it.
        flags: Resolved global flags.

    Raises:
        typer.Exit: With :data:`DOMAIN_REFUSAL_EXIT` when the mutation was
            refused. The envelope prints first either way, so a caller
            reading stdout gets the code whichever branch it took.
    """
    from eawf.runtime.daemon.methods.domain_envelope import DomainStatus

    emit_json_or_text(
        envelope.model_dump(mode="json"),
        _envelope_text(envelope, request=request),
        flags=flags,
    )
    if envelope.status is not DomainStatus.OK:
        raise typer.Exit(DOMAIN_REFUSAL_EXIT)


def _run_verb(
    ctx: typer.Context,
    *,
    method: str,
    urn: str,
    expected_revision: int,
    idempotency_key: str,
    actor: str,
    from_spec: Path | None,
    approval_receipt_ref: str | None = None,
) -> None:
    """Dispatch one native lifecycle verb and render its answer.

    The single body every command in this module delegates to: build the
    typed request, send it, print the envelope.

    Args:
        ctx: Typer context carrying the resolved global flags.
        method: The dotted JSON-RPC name the command forwards to.
        urn: The record to move.
        expected_revision: The compare-and-swap token.
        idempotency_key: The caller's name for this request.
        actor: The principal the move is attributed to.
        from_spec: The payload file, or ``None``.
        approval_receipt_ref: The sealed approval reference, or ``None``.
    """
    flags: GlobalFlags = ctx.obj
    try:
        request = _build_request(
            method=method,
            urn=urn,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            actor=actor,
            from_spec=from_spec,
            approval_receipt_ref=approval_receipt_ref,
        )
        envelope = _call_domain_verb(request, flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    _emit(envelope, request=request, flags=flags)


# ---- Track ------------------------------------------------------------------


@track_app.command("retire")
def track_retire_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-track-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
) -> None:
    """Retire an ACTIVE Track once no Milestone under it is open."""
    _run_verb(
        ctx,
        method=TRACK_RETIRE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


# ---- Milestone --------------------------------------------------------------


@milestone_app.command("activate")
def milestone_activate_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-milestone-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
) -> None:
    """Move a PLANNED Milestone to ACTIVE under an active Track."""
    _run_verb(
        ctx,
        method=MILESTONE_ACTIVATE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


@milestone_app.command("open-review")
def milestone_open_review_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-milestone-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
) -> None:
    """Open acceptance review on an ACTIVE Milestone."""
    _run_verb(
        ctx,
        method=MILESTONE_OPEN_REVIEW,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


@milestone_app.command("accept")
def milestone_accept_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-milestone-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    approval_receipt_ref: Annotated[
        str | None, typer.Option("--approval-receipt-ref", help=_APPROVAL_HELP)
    ] = None,
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
) -> None:
    """Accept a Milestone in review against a sealed approval receipt.

    The reference is forwarded as given. A request that carries none, or
    one addressing anything other than a PendingAction, is refused by the
    daemon with ``protected_approval_required`` and nothing moves.
    """
    _run_verb(
        ctx,
        method=MILESTONE_ACCEPT,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
        approval_receipt_ref=approval_receipt_ref,
    )


@milestone_app.command("cancel")
def milestone_cancel_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-milestone-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
) -> None:
    """Cancel a Milestone that has not completed."""
    _run_verb(
        ctx,
        method=MILESTONE_CANCEL,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


# ---- Batch ------------------------------------------------------------------


@batch_app.command("activate")
def batch_activate_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-batch-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
) -> None:
    """Move a PLANNED delivery Batch to ACTIVE."""
    _run_verb(
        ctx,
        method=BATCH_ACTIVATE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


@batch_app.command("ready")
def batch_ready_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-batch-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
) -> None:
    """Declare an ACTIVE Batch ready to merge."""
    _run_verb(
        ctx,
        method=BATCH_READY,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


# ---- Task -------------------------------------------------------------------


@task_app.command("promote")
def task_promote_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-task-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
) -> None:
    """Promote a DRAFT Task to PLANNED once its contract is complete."""
    _run_verb(
        ctx,
        method=TASK_PROMOTE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


@task_app.command("start")
def task_start_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-task-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
) -> None:
    """Start a CLAIMED Task under the Run the payload binds it to."""
    _run_verb(
        ctx,
        method=TASK_START,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


__all__ = [
    "DOMAIN_CLI_METHODS",
    "DOMAIN_REFUSAL_EXIT",
    "IDEMPOTENCY_KEY_MAX",
    "DomainVerbRequest",
    "DomainVerbSpec",
    "batch_activate_cmd",
    "batch_ready_cmd",
    "milestone_accept_cmd",
    "milestone_activate_cmd",
    "milestone_cancel_cmd",
    "milestone_open_review_cmd",
    "task_promote_cmd",
    "task_start_cmd",
    "track_retire_cmd",
]
