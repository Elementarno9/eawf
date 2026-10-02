"""The operator surface of the native per-entity lifecycle verbs.

The daemon registers one JSON-RPC verb per lifecycle move a Track, a
Milestone, a Batch, a Task or a Run can make. Until this module there was no way
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
- **A create is dispatch too, and nothing more.** ``eawf milestone
  create`` sends ``domain.milestone.create`` with the whole create
  document from ``--from-spec`` (a path, or ``-`` for stdin); the daemon
  still decides the cursor, the free key and the live parent under its own
  locks. The document is parsed through the same strict per-kind model the
  daemon's transaction uses, and refused with the same envelope, so an
  unparseable document costs no round trip and earns one answer.

Every verb takes the same four addressing flags -- the subject URN, the
revision the caller read it at (``--expected-revision``), the retry key and
the actor -- and reads the rest of the request from a ``--from-spec`` JSON
document, so the payload of a move is a reviewable artifact rather than a
shell line. A refused envelope still prints in full and then exits with the
typed status :func:`~eawf.surfaces.cli.verb_contract.envelope_exit_code`
gives it, so a script can branch on the exit status without losing the code.

The verbs whose daemon answers outside the :class:`DomainEnvelope` shape --
the candidate, integration, proof, approval and evidence verbs -- live in
:mod:`eawf.surfaces.cli.commands.domain_integration`, reach the daemon
through :func:`_native_answer`, and wrap its answer into the same envelope
before printing it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import typer
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from eawf.runtime.session.host_session import with_host_session
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import (
    DEFAULT_CALL_TIMEOUT_SECONDS,
    DaemonClient,
    DaemonRpcError,
)
from eawf.surfaces.cli.commands.domain_consequence import DryRun, Yes, preview
from eawf.surfaces.cli.commands.lifecycle import (
    batch_app,
    milestone_app,
    repository_app,
    task_app,
    track_app,
)
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.verb_contract import emit_envelope, read_spec_document

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
BATCH_MERGE: Final = "domain.batch.merge"
BATCH_OBSERVE_MERGE: Final = "domain.batch.observe_merge"
BATCH_COMPLETE: Final = "domain.batch.complete"
TASK_PROMOTE: Final = "domain.task.promote"
TASK_DEMOTE: Final = "domain.task.demote"
TASK_CLAIM: Final = "domain.task.claim"
TASK_RELEASE: Final = "domain.task.release"
TASK_START: Final = "domain.task.start"
TASK_READY: Final = "domain.task.ready"
TASK_COMPLETE: Final = "domain.task.complete"
RUN_START: Final = "domain.run.start"
RUN_FINISH: Final = "domain.run.finish"
RUN_FAIL: Final = "domain.run.fail"

#: Every lifecycle-move verb the CLI exposes, in registration order. The
#: Task, Run and Batch delivery commands live in the sibling
#: ``domain_delivery`` module; their names are listed here so one table
#: is asserted against the daemon's.
DOMAIN_CLI_METHODS: Final[tuple[str, ...]] = (
    TRACK_RETIRE,
    MILESTONE_ACTIVATE,
    MILESTONE_OPEN_REVIEW,
    MILESTONE_ACCEPT,
    MILESTONE_CANCEL,
    BATCH_ACTIVATE,
    BATCH_READY,
    BATCH_MERGE,
    BATCH_OBSERVE_MERGE,
    BATCH_COMPLETE,
    TASK_PROMOTE,
    TASK_DEMOTE,
    TASK_CLAIM,
    TASK_RELEASE,
    TASK_START,
    TASK_READY,
    TASK_COMPLETE,
    RUN_START,
    RUN_FINISH,
    RUN_FAIL,
)

#: The dotted JSON-RPC name each create command forwards to.
TRACK_CREATE: Final = "domain.track.create"
MILESTONE_CREATE: Final = "domain.milestone.create"
BATCH_CREATE: Final = "domain.batch.create"
TASK_CREATE: Final = "domain.task.create"
RUN_CREATE: Final = "domain.run.create"
#: A repository row is not a lifecycle record, but a plan cannot be
#: submitted until one records the head it binds, so it is created here too.
REPOSITORY_CREATE: Final = "domain.repository.create"

#: Every create verb this module exposes, in registration order.
DOMAIN_CREATE_CLI_METHODS: Final[tuple[str, ...]] = (
    TRACK_CREATE,
    MILESTONE_CREATE,
    BATCH_CREATE,
    TASK_CREATE,
    RUN_CREATE,
    REPOSITORY_CREATE,
)

#: The two verbs answered outside the :class:`DomainEnvelope` shape --
#: see the module docstring for why ``milestone seal-approval`` and
#: ``task submit`` differ from every other command here.
DELIVERY_SEAL_APPROVAL: Final = "runtime.delivery.seal_acceptance_approval"
CANDIDATE_SUBMIT: Final = "runtime.candidate.submit"

#: How long a retry key may be. Mirrors the bound the request model
#: enforces daemon-side; the contract test pins the two together. The
#: check runs here as well so a key that cannot possibly be accepted
#: costs no round trip.
IDEMPOTENCY_KEY_MAX: Final = 128

#: The exit status of a refused mutation. One code for every refusal but the
#: one only an operator can clear (see
#: :func:`~eawf.surfaces.cli.verb_contract.envelope_exit_code`): the code a
#: caller routes on is the one inside the envelope, and a finer mapping from
#: that vocabulary onto exit statuses would be a second contract to keep stable.
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
_BUNDLE_HELP: Final = (
    "JSON file carrying the acceptance bundle the approval was sealed against, as "
    "milestone open-approval printed it."
)
_CREATE_URN_HELP: Final = "URN of the record to admit."
_TREE_REVISION_HELP: Final = (
    "The tree's committed canonical sequence the caller read; 0 for a tree "
    "nothing has committed to yet."
)
_CREATE_SPEC_HELP: Final = "JSON file carrying the entity's whole create document."
_CORRELATION_HELP: Final = "Caller's thread of related requests."
_DATE_HELP: Final = "The day the Milestone is aimed at, as YYYY-MM-DD."


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
        observations: Facts about the outside world the caller presents,
            each the name of an observed fact such as ``run_report_bound``.
        reason_code: The stable reason the move happened.
        binding_refs: The exact proofs the move was taken against.
        correlation_id: The caller's thread of related requests.
    """

    model_config = ConfigDict(extra="forbid")

    updates: dict[str, Any] = Field(default_factory=dict)
    observations: tuple[str, ...] = ()
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
        extra_params: Wire fields one verb declares beyond the shared
            shape, such as the commit and assessment a Task completion
            carries; empty everywhere else.
    """

    method: str
    urn: str
    expected_revision: int
    idempotency_key: str
    actor: str
    spec: DomainVerbSpec = field(default_factory=DomainVerbSpec)
    approval_receipt_ref: str | None = None
    extra_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DomainCreateRequest:
    """One native create request, as the CLI resolved it.

    Attributes:
        method: The dotted JSON-RPC name to send.
        urn: The record to admit.
        expected_revision: The tree's committed canonical sequence the
            caller read, ``0`` for a tree nothing has committed to yet.
        idempotency_key: The caller's name for this request.
        actor: The principal the create is attributed to.
        document: The entity's whole create document, forwarded as read.
        correlation_id: The caller's thread of related requests.
    """

    method: str
    urn: str
    expected_revision: int
    idempotency_key: str
    actor: str
    document: dict[str, Any]
    correlation_id: str | None = None


def _load_spec(path: Path | None) -> DomainVerbSpec:
    """Return the payload the caller named, or an empty one.

    Args:
        path: The ``--from-spec`` file, ``-`` for stdin, or ``None``.

    Returns:
        The parsed payload.

    Raises:
        UserError: The file is missing, is not a JSON object, or names a
            field the request has no room for. Parsing fails at the boundary so an
            unusable payload never reaches the wire.
    """
    if path is None:
        return DomainVerbSpec()
    raw = read_spec_document(path)
    try:
        return DomainVerbSpec.model_validate(raw)
    except PydanticValidationError as exc:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in exc.errors()})
        raise cli_errors.UserError(
            f"--from-spec {path} is not a valid request payload; check {', '.join(fields)}",
            kind="InvalidInput",
        ) from exc


def _check_idempotency_key(idempotency_key: str) -> None:
    """Raise when the retry key falls outside the bound the request model accepts.

    Args:
        idempotency_key: The caller's name for the request.

    Raises:
        UserError: The key is empty or longer than :data:`IDEMPOTENCY_KEY_MAX`.
            The daemon refuses these too; refusing here keeps a request that
            cannot succeed off the wire.
    """
    if not 1 <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX:
        raise cli_errors.UserError(
            f"--idempotency-key must be 1 to {IDEMPOTENCY_KEY_MAX} characters, "
            f"got {len(idempotency_key)}",
            kind="InvalidInput",
        )


def _build_request(
    *,
    method: str,
    urn: str,
    expected_revision: int,
    idempotency_key: str,
    actor: str,
    from_spec: Path | None,
    approval_receipt_ref: str | None = None,
    extra_params: dict[str, Any] | None = None,
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
        extra_params: The verb's own wire fields, or ``None``.

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
    _check_idempotency_key(idempotency_key)
    spec = _load_spec(from_spec)
    if method == RUN_START:
        # The host session this command runs inside is the one that counts
        # the Run's work, so a root Run is started bound to it.
        updates = with_host_session(spec.updates, os.environ)
        spec = spec.model_copy(update={"updates": updates})
    return DomainVerbRequest(
        method=method,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        spec=spec,
        approval_receipt_ref=approval_receipt_ref,
        extra_params=dict(extra_params or {}),
    )


def _build_create_request(
    *,
    method: str,
    urn: str,
    expected_revision: int,
    idempotency_key: str,
    actor: str,
    from_spec: Path,
    correlation_id: str | None,
) -> DomainCreateRequest:
    """Return the typed create request one command's flags stand for.

    Args:
        method: The dotted JSON-RPC name the command forwards to.
        urn: The record to admit.
        expected_revision: The tree's committed canonical sequence cursor.
        idempotency_key: The caller's name for this request.
        actor: The principal the create is attributed to.
        from_spec: The create-document file.
        correlation_id: The caller's thread of related requests.

    Returns:
        The request to dispatch.

    Raises:
        UserError: A bound the request model could not accept was broken,
            or the create document could not be read.
    """
    if expected_revision < 0:
        raise cli_errors.UserError(
            f"--expected-revision must not be negative, got {expected_revision}",
            kind="InvalidInput",
        )
    _check_idempotency_key(idempotency_key)
    return DomainCreateRequest(
        method=method,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        document=read_spec_document(from_spec),
        correlation_id=correlation_id,
    )


def _create_document_refusal(request: DomainCreateRequest) -> DomainEnvelope | None:
    """Return the refusal a create document earns from its kind's strict model.

    The document is parsed through the same per-kind model the daemon's
    create transaction parses it through, and a failure is answered with
    the envelope that transaction would answer: the same code, the same
    message naming the offending fields, and no revision because nothing
    was read. A document that cannot become a record therefore never
    reaches the wire, and the caller sees one answer whichever side caught it.

    Args:
        request: The resolved create request.

    Returns:
        The ``schema_validation_failed`` envelope, or ``None`` when the
        document parses.
    """
    from eawf.kernel.state.epoch2.repository import RepositoryCreateSpec
    from eawf.kernel.state.epoch2.transitions import LifecycleEntity
    from eawf.runtime.daemon.epoch2_create import CREATE_SPECS
    from eawf.runtime.daemon.methods.domain_envelope import (
        ENVELOPE_SCHEMA_VERSION,
        DomainEnvelope,
        DomainError,
        DomainErrorCode,
        DomainStatus,
    )

    kind = request.method.split(".")[1]
    model = (
        RepositoryCreateSpec
        if request.method == REPOSITORY_CREATE
        else CREATE_SPECS[LifecycleEntity(kind)]
    )
    try:
        model.model_validate(request.document)
    except PydanticValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        return DomainEnvelope(
            schema_version=ENVELOPE_SCHEMA_VERSION,
            status=DomainStatus.ERROR,
            operation=request.method,
            errors=(
                DomainError(
                    code=DomainErrorCode.SCHEMA_VALIDATION_FAILED,
                    message=(
                        f"the {kind} create document does not validate; check {', '.join(fields)}"
                    ),
                    entity_ref=request.urn,
                    remediation="Correct the named fields of the create document and retry.",
                ),
            ),
        )
    return None


def _rpc_params(request: DomainVerbRequest) -> dict[str, Any]:
    """Return the wire parameters of one request, less ``repo_root``.

    The approval reference is omitted rather than sent as null when the
    verb does not carry one: the request models forbid unknown fields, so
    a key no verb declares would be refused as a schema failure rather
    than as the thing that is actually wrong.

    Args:
        request: The resolved request.

    Returns:
        The JSON-RPC parameters.
    """
    params: dict[str, Any] = {
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
    params.update(request.extra_params)
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


def _rpc_refusal(error: DaemonRpcError, *, method: str, urn: str) -> DomainEnvelope:
    """Return the envelope one JSON-RPC error stands for.

    Args:
        error: What the daemon answered.
        method: The dotted JSON-RPC name that earned it.
        urn: The subject the request addressed.

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
    logger.info(f"_rpc_refusal method={method} code={code.value}")
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=method,
        errors=(
            DomainError(
                code=code,
                message=error.message.removeprefix("validation_failed: "),
                entity_ref=urn,
                remediation=_FENCE_REMEDIATION,
            ),
        ),
    )


def _call_envelope(
    method: str, params: dict[str, Any], *, urn: str, flags: GlobalFlags
) -> DomainEnvelope:
    """Send one envelope-answered request to the daemon and return its answer.

    Args:
        method: The dotted JSON-RPC name to send.
        params: The wire parameters, less ``repo_root``.
        urn: The subject the request addresses, named in a fence refusal.
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
        _dispatch.escalate_mutation(_operator_verb(method), flags=flags)
        with DaemonClient() as client:
            answer = client.call(method, {"repo_root": repo_root, **params})
    except DaemonRpcError as exc:
        return _rpc_refusal(exc, method=method, urn=urn)
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise cli_errors.DaemonUnreachable(f"daemon unavailable for {method}: {exc}") from exc
    try:
        return DomainEnvelope.model_validate(answer)
    except PydanticValidationError as exc:
        raise cli_errors.InternalError(
            f"{method} answered something that is not a domain envelope: {exc}"
        ) from exc


def _call_domain_verb(request: DomainVerbRequest, *, flags: GlobalFlags) -> DomainEnvelope:
    """Send one lifecycle request to the daemon and return the answer it stands for.

    Args:
        request: The resolved request.
        flags: Resolved global flags.

    Returns:
        The machine envelope, whether the mutation committed or was
        refused.

    Raises:
        CliError: As :func:`_call_envelope` raises it.
    """
    return _call_envelope(request.method, _rpc_params(request), urn=request.urn, flags=flags)


def _call_domain_create(request: DomainCreateRequest, *, flags: GlobalFlags) -> DomainEnvelope:
    """Send one create request to the daemon and return the answer it stands for.

    A create has no existing record revision to address, and carries a
    whole document rather than a move's update/observation fields; it is
    fenced and answered exactly as a lifecycle move is.

    Args:
        request: The resolved create request.
        flags: Resolved global flags.

    Returns:
        The machine envelope, whether the create committed or was
        refused.

    Raises:
        CliError: As :func:`_call_envelope` raises it.
    """
    params: dict[str, Any] = {
        "urn": request.urn,
        "expected_revision": request.expected_revision,
        "idempotency_key": request.idempotency_key,
        "actor": request.actor,
        "spec": dict(request.document),
        "correlation_id": request.correlation_id,
    }
    return _call_envelope(request.method, params, urn=request.urn, flags=flags)


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
    extra_params: dict[str, Any] | None = None,
    dry_run: bool = False,
    yes: bool = False,
) -> None:
    """Dispatch one native lifecycle verb and render its answer.

    The single body every command in this module delegates to: build the
    typed request, print its consequence, send it, print the envelope.

    Args:
        ctx: Typer context carrying the resolved global flags.
        method: The dotted JSON-RPC name the command forwards to.
        urn: The record to move.
        expected_revision: The compare-and-swap token.
        idempotency_key: The caller's name for this request.
        actor: The principal the move is attributed to.
        from_spec: The payload file, or ``None``.
        approval_receipt_ref: The sealed approval reference, or ``None``.
        extra_params: The verb's own wire fields, or ``None``.
        dry_run: Print the consequence only and send nothing.
        yes: Send after printing the consequence, without asking.
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
            extra_params=extra_params,
        )
        if not preview(method, urn, expected_revision, flags=flags, dry_run=dry_run, yes=yes):
            return
        envelope = _call_domain_verb(request, flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    emit_envelope(envelope, urn=request.urn, flags=flags)


def _run_create_verb(
    ctx: typer.Context,
    *,
    method: str,
    urn: str,
    expected_revision: int,
    idempotency_key: str,
    actor: str,
    from_spec: Path,
    correlation_id: str | None,
    dry_run: bool = False,
    yes: bool = False,
    target_date: str | None = None,
) -> None:
    """Dispatch one native create verb and render its answer.

    The single body every create command in this module delegates to:
    build the typed request, parse its document through the kind's strict
    model, send it, print the envelope. Mirrors
    :func:`_run_verb`'s shape; what differs is only that a create has no
    existing revision to address and reads its whole document rather than
    a move's update fields.

    Args:
        ctx: Typer context carrying the resolved global flags.
        method: The dotted JSON-RPC name the command forwards to.
        urn: The record to admit.
        expected_revision: The tree's committed canonical sequence cursor.
        idempotency_key: The caller's name for this request.
        actor: The principal the create is attributed to.
        from_spec: The create-document file.
        correlation_id: The caller's thread of related requests.
        dry_run: Print the consequence only and send nothing.
        yes: Send after printing the consequence, without asking.
        target_date: The ``--date`` a Milestone create carries, or ``None``.
    """
    flags: GlobalFlags = ctx.obj
    try:
        request = _build_create_request(
            method=method,
            urn=urn,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            actor=actor,
            from_spec=from_spec,
            correlation_id=correlation_id,
        )
        if target_date is not None:
            from eawf.surfaces.cli.commands.domain_target import parse_target_date

            day = parse_target_date(target_date).isoformat()
            stated = request.document.get("target_date")
            if stated is not None and stated != day:
                raise cli_errors.UserError(
                    f"--date {day} disagrees with the create document's target_date {stated!r}",
                    kind="InvalidInput",
                )
            request = replace(request, document={**request.document, "target_date": day})
        refusal = _create_document_refusal(request)
        if refusal is None and not preview(
            method, urn, expected_revision, flags=flags, dry_run=dry_run, yes=yes
        ):
            return
        envelope = refusal or _call_domain_create(request, flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    emit_envelope(envelope, urn=request.urn, flags=flags)


def _native_answer(
    method: str,
    params: dict[str, Any],
    *,
    flags: GlobalFlags,
    verb_text: str,
    gated: bool = False,
    read: bool = False,
) -> dict[str, Any]:
    """Send one non-envelope native RPC and return its raw answer.

    The candidate, integration, proof, approval and evidence verbs answer
    with their own typed shape rather than a :class:`DomainEnvelope`, and
    the daemon raises their refusal as a JSON-RPC error rather than
    returning it as an ok-shaped result -- that is the daemon's own answer
    contract for these verbs, not a choice made here.

    Args:
        method: The dotted JSON-RPC name to send.
        params: The wire parameters, less ``repo_root``.
        flags: Resolved global flags.
        verb_text: The command spelling an operator typed, used only for
            the ``--daemonless`` rejection message.
        gated: Whether the verb runs gates inside the request, so the wire
            waits as long as the daemon lets a gated mutation run.
        read: Whether the verb only reads, so it neither escalates as a
            mutation nor starts a daemon that is not running.

    Returns:
        The daemon's answer, as a JSON-mode mapping.

    Raises:
        UserError: ``--daemonless`` was asked for a mutation.
        DaemonRpcError: The daemon answered an error, left for the caller
            to render.
        DaemonUnreachable: The daemon could not be reached.
    """
    from eawf.surfaces.cli import _dispatch

    repo_root = str((flags.workspace or Path.cwd()).resolve())
    wire_params = {"repo_root": repo_root, **params}
    timeout = DEFAULT_CALL_TIMEOUT_SECONDS
    if gated:
        from eawf.runtime.daemon.limits import (
            cli_mutation_timeout_for,
            configured_juror_wall_clock,
        )

        timeout = cli_mutation_timeout_for(configured_juror_wall_clock(Path(repo_root)))
    try:
        if not read:
            _dispatch.escalate_mutation(verb_text, flags=flags)
        with DaemonClient(call_timeout_seconds=timeout, spawn=not read) as client:
            return client.call(method, wire_params)
    except DaemonRpcError:
        raise
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise cli_errors.DaemonUnreachable(f"daemon unavailable for {method}: {exc}") from exc


# ---- Track ------------------------------------------------------------------


@track_app.command("retire")
def track_retire_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-revision", "--expected-track-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Retire an ACTIVE Track once no Milestone under it is open."""
    _run_verb(
        ctx,
        dry_run=dry_run,
        yes=yes,
        method=TRACK_RETIRE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


@track_app.command("create")
def track_create_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_CREATE_URN_HELP)],
    expected_revision: Annotated[
        int,
        typer.Option("--expected-revision", "--expected-tree-revision", help=_TREE_REVISION_HELP),
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path, typer.Option("--from-spec", help=_CREATE_SPEC_HELP)],
    correlation_id: Annotated[
        str | None, typer.Option("--correlation-id", help=_CORRELATION_HELP)
    ] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Admit a new Track's create document into the addressed tree."""
    _run_create_verb(
        ctx,
        method=TRACK_CREATE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
        correlation_id=correlation_id,
        dry_run=dry_run,
        yes=yes,
    )


@repository_app.command("create")
def repository_create_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_CREATE_URN_HELP)],
    expected_revision: Annotated[
        int,
        typer.Option("--expected-revision", "--expected-tree-revision", help=_TREE_REVISION_HELP),
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path, typer.Option("--from-spec", help=_CREATE_SPEC_HELP)],
    correlation_id: Annotated[
        str | None, typer.Option("--correlation-id", help=_CORRELATION_HELP)
    ] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Admit a repository row at the head its git history holds now."""
    _run_create_verb(
        ctx,
        method=REPOSITORY_CREATE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
        correlation_id=correlation_id,
        dry_run=dry_run,
        yes=yes,
    )


# ---- Milestone --------------------------------------------------------------


@milestone_app.command("activate")
def milestone_activate_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int,
        typer.Option("--expected-revision", "--expected-milestone-revision", help=_REVISION_HELP),
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Move a PLANNED Milestone to ACTIVE under an active Track."""
    _run_verb(
        ctx,
        dry_run=dry_run,
        yes=yes,
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
        int,
        typer.Option("--expected-revision", "--expected-milestone-revision", help=_REVISION_HELP),
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Open acceptance review on an ACTIVE Milestone."""
    _run_verb(
        ctx,
        dry_run=dry_run,
        yes=yes,
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
        int,
        typer.Option("--expected-revision", "--expected-milestone-revision", help=_REVISION_HELP),
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    approval_receipt_ref: Annotated[
        str | None, typer.Option("--approval-receipt-ref", help=_APPROVAL_HELP)
    ] = None,
    acceptance_bundle: Annotated[
        Path | None, typer.Option("--acceptance-bundle", help=_BUNDLE_HELP)
    ] = None,
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Accept a Milestone in review against a sealed approval receipt.

    The reference and the bundle are forwarded as given. A request that
    carries no reference, one addressing anything other than a
    PendingAction, or no bundle the approval was sealed against is refused
    by the daemon with ``protected_approval_required`` and nothing moves.
    """
    flags: GlobalFlags = ctx.obj
    extra: dict[str, Any] = {}
    if acceptance_bundle is not None:
        try:
            extra["acceptance_bundle"] = read_spec_document(acceptance_bundle)
        except cli_errors.CliError as exc:
            cli_errors.emit_error(exc, flags=flags)
            return
    _run_verb(
        ctx,
        dry_run=dry_run,
        yes=yes,
        method=MILESTONE_ACCEPT,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
        approval_receipt_ref=approval_receipt_ref,
        extra_params=extra,
    )


@milestone_app.command("cancel")
def milestone_cancel_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int,
        typer.Option("--expected-revision", "--expected-milestone-revision", help=_REVISION_HELP),
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Cancel a Milestone that has not completed."""
    _run_verb(
        ctx,
        dry_run=dry_run,
        yes=yes,
        method=MILESTONE_CANCEL,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


@milestone_app.command("create")
def milestone_create_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_CREATE_URN_HELP)],
    expected_revision: Annotated[
        int,
        typer.Option("--expected-revision", "--expected-tree-revision", help=_TREE_REVISION_HELP),
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path, typer.Option("--from-spec", help=_CREATE_SPEC_HELP)],
    correlation_id: Annotated[
        str | None, typer.Option("--correlation-id", help=_CORRELATION_HELP)
    ] = None,
    target_date: Annotated[str | None, typer.Option("--date", help=_DATE_HELP)] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Admit a new Milestone's create document into the addressed tree."""
    _run_create_verb(
        ctx,
        method=MILESTONE_CREATE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
        correlation_id=correlation_id,
        dry_run=dry_run,
        yes=yes,
        target_date=target_date,
    )


# ---- Batch ------------------------------------------------------------------


@batch_app.command("activate")
def batch_activate_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-revision", "--expected-batch-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Move a PLANNED delivery Batch to ACTIVE."""
    _run_verb(
        ctx,
        dry_run=dry_run,
        yes=yes,
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
        int, typer.Option("--expected-revision", "--expected-batch-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Declare an ACTIVE Batch ready to merge."""
    _run_verb(
        ctx,
        dry_run=dry_run,
        yes=yes,
        method=BATCH_READY,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


@batch_app.command("create")
def batch_create_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_CREATE_URN_HELP)],
    expected_revision: Annotated[
        int,
        typer.Option("--expected-revision", "--expected-tree-revision", help=_TREE_REVISION_HELP),
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path, typer.Option("--from-spec", help=_CREATE_SPEC_HELP)],
    correlation_id: Annotated[
        str | None, typer.Option("--correlation-id", help=_CORRELATION_HELP)
    ] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Admit a new delivery Batch's create document into the addressed tree."""
    _run_create_verb(
        ctx,
        method=BATCH_CREATE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
        correlation_id=correlation_id,
        dry_run=dry_run,
        yes=yes,
    )


# ---- Task -------------------------------------------------------------------


@task_app.command("promote")
def task_promote_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-revision", "--expected-task-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Promote a DRAFT Task to PLANNED once its contract is complete."""
    _run_verb(
        ctx,
        dry_run=dry_run,
        yes=yes,
        method=TASK_PROMOTE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


@task_app.command("demote")
def task_demote_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-revision", "--expected-task-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Hand a PLANNED Task that was never claimed back to the backlog as a DRAFT."""
    _run_verb(
        ctx,
        dry_run=dry_run,
        yes=yes,
        method=TASK_DEMOTE,
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
        int, typer.Option("--expected-revision", "--expected-task-revision", help=_REVISION_HELP)
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Start a CLAIMED Task under the Run the payload binds it to."""
    _run_verb(
        ctx,
        dry_run=dry_run,
        yes=yes,
        method=TASK_START,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


@task_app.command("create")
def task_create_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_CREATE_URN_HELP)],
    expected_revision: Annotated[
        int,
        typer.Option("--expected-revision", "--expected-tree-revision", help=_TREE_REVISION_HELP),
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    from_spec: Annotated[Path, typer.Option("--from-spec", help=_CREATE_SPEC_HELP)],
    correlation_id: Annotated[
        str | None, typer.Option("--correlation-id", help=_CORRELATION_HELP)
    ] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Admit a new Task's create document into the addressed tree."""
    _run_create_verb(
        ctx,
        method=TASK_CREATE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
        correlation_id=correlation_id,
        dry_run=dry_run,
        yes=yes,
    )


# ---- Command registration ---------------------------------------------------
# The sibling modules import verb names and runners from this module, so they
# load here, after every one is defined, rather than from ``lifecycle``: from
# there they would run against a partial module whenever this module is the
# first of the two a process imports.
from eawf.surfaces.cli.commands import domain_delivery as _domain_delivery  # noqa: E402, F401
from eawf.surfaces.cli.commands import domain_legacy as _domain_legacy  # noqa: E402, F401
from eawf.surfaces.cli.commands import domain_target as _domain_target  # noqa: E402, F401

__all__ = [
    "CANDIDATE_SUBMIT",
    "DELIVERY_SEAL_APPROVAL",
    "DOMAIN_CLI_METHODS",
    "DOMAIN_CREATE_CLI_METHODS",
    "DOMAIN_REFUSAL_EXIT",
    "IDEMPOTENCY_KEY_MAX",
    "DomainCreateRequest",
    "DomainVerbRequest",
    "DomainVerbSpec",
    "batch_activate_cmd",
    "batch_create_cmd",
    "batch_ready_cmd",
    "milestone_accept_cmd",
    "milestone_activate_cmd",
    "milestone_cancel_cmd",
    "milestone_create_cmd",
    "milestone_open_review_cmd",
    "repository_create_cmd",
    "task_create_cmd",
    "task_promote_cmd",
    "task_start_cmd",
    "track_create_cmd",
    "track_retire_cmd",
]
