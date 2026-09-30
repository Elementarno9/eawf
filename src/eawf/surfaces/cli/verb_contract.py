"""The one contract the entity verbs answer under.

Verbs are grouped by the entity they operate on, plus a small closed set of
cross-cutting groups. The two tuples below are that closed set: a group is
added by amending the contract, never by mounting a Typer app under a new
name. A mutating verb names its compare-and-swap anchor ``--expected-revision``
and its retry key ``--idempotency-key``; a create verb reads its document from
``--from-spec <path>``, or from stdin when the path is ``-``.

Every native verb answers with one machine envelope, and this module owns the
two renderings of it and the exit status it maps to:

- :func:`envelope_text` is the human rendering. It carries every fact the
  machine rendering carries -- the schema version, the status, the operation,
  both revisions, each result field, each warning, each error row with its
  guard and remediation, and each link -- so neither mode is a superset of
  the other.
- :func:`envelope_exit_code` maps the envelope to a typed exit status: ``0``
  for a committed answer, the needs-operator code for a refusal only an
  operator can clear, and the refusal code for every other refusal.

The verbs whose daemon answers with its own typed shape rather than an
envelope -- the integration and release verbs -- are wrapped into one here:
:func:`answer_envelope` wraps an answer, and :func:`refusal_envelope` wraps a
refusal the daemon raised. A refusal code finer than the closed vocabulary
travels in the row's ``guard``, the same way a registry denial does.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import orjson
import typer
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.verb_closure import ROOT_ENTRY_EXCEPTIONS

if TYPE_CHECKING:
    from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope
    from eawf.surfaces.cli.flags import GlobalFlags

#: The entity groups, one per entity whose legal operations are its verbs.
ENTITY_GROUPS: Final[tuple[str, ...]] = (
    "track",
    "milestone",
    "batch",
    "task",
    "run",
    "release",
    "campaign",
    "question",
    "action",
    "decision",
)

#: The cross-cutting groups: surfaces that report or configure across entities
#: and so have no entity group to live in. Closed by amendment, each with its
#: reason in the root entry exceptions.
CROSS_CUTTING_GROUPS: Final[tuple[str, ...]] = tuple(
    row.name for row in ROOT_ENTRY_EXCEPTIONS if row.kind == "cross_cutting"
)

#: Every group whose verbs answer under this contract.
CONTRACT_GROUPS: Final[frozenset[str]] = frozenset((*ENTITY_GROUPS, *CROSS_CUTTING_GROUPS))

#: The ``--from-spec`` value that reads the document from stdin.
STDIN_SPEC: Final = "-"

#: The prefix the daemon leads every refusal message with.
_REFUSAL_PREFIX: Final = "validation_failed: "

#: The shape of a refusal code the daemon leads its detail with.
_CODE_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]*")

#: What an operator does about an answer the daemon gave but that did not
#: hold, or a refusal that carries no remediation of its own.
_ANSWER_REMEDIATION: Final = "Read the reason, repair what it names, and retry."


def read_spec_document(path: Path) -> dict[str, Any]:
    """Return the JSON object a ``--from-spec`` style option names.

    Args:
        path: The file to read, or ``-`` to read stdin.

    Returns:
        The parsed JSON object, as read.

    Raises:
        UserError: The file is missing, stdin is a terminal rather than a
            pipe, the bytes are not JSON, or the JSON is not an object.
            Nothing here could be a request document, so the request stops
            before it reaches the wire.
    """
    if str(path) == STDIN_SPEC:
        if sys.stdin.isatty():
            raise cli_errors.UserError(
                "--from-spec - reads the document from stdin; pipe it in", kind="InvalidInput"
            )
        data = sys.stdin.buffer.read()
    else:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise cli_errors.UserError(
                f"cannot read --from-spec {path}: {exc}", kind="NotFound"
            ) from exc
    try:
        raw = orjson.loads(data)
    except orjson.JSONDecodeError as exc:
        raise cli_errors.UserError(
            f"--from-spec {path} is not valid JSON: {exc}", kind="InvalidInput"
        ) from exc
    if not isinstance(raw, dict):
        raise cli_errors.UserError(f"--from-spec {path} must be a JSON object", kind="InvalidInput")
    return raw


def request_document[T: BaseModel](
    model: type[T], from_spec: Path | None, given: Mapping[str, Any]
) -> T:
    """Return a create verb's request, from ``--from-spec`` or from its flags.

    Both spellings parse through *model*, the closed model the receiving end
    validates the request with, so the flags are a shorthand for a document
    rather than a second contract beside it.

    Args:
        model: The closed request model.
        from_spec: The ``--from-spec`` path, ``-`` for stdin, or ``None`` to
            build the request from the flags.
        given: The flag values by field name; ``None`` for a flag not set.

    Returns:
        The validated request.

    Raises:
        UserError: A flag was set beside ``--from-spec``, which carries the
            whole request; the document cannot be read; or the request does
            not validate, naming the offending fields.
    """
    named = {key: value for key, value in given.items() if value is not None}
    if from_spec is not None:
        if named:
            raise cli_errors.UserError(
                f"--from-spec carries the whole request; drop {', '.join(sorted(named))}",
                kind="InvalidInput",
            )
        document = read_spec_document(from_spec)
    else:
        document = named
    try:
        return model.model_validate(document)
    except PydanticValidationError as exc:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in exc.errors()})
        raise cli_errors.UserError(
            f"the request does not validate; check {', '.join(fields) or model.__name__}",
            kind="InvalidInput",
        ) from exc


def answer_envelope(
    answer: dict[str, Any],
    *,
    operation: str,
    urn: str,
    revision_before: int | None,
    revision_after: int | None,
    failed_guard: str | None = None,
    links: Mapping[str, str] | None = None,
) -> DomainEnvelope:
    """Return the envelope one typed daemon answer stands for.

    Args:
        answer: The daemon's answer, which becomes the result whole.
        operation: The dotted JSON-RPC name that earned it.
        urn: The subject the request addressed.
        revision_before: The subject's revision the request was anchored at,
            or ``None`` for a verb that takes no anchor.
        revision_after: The subject's revision the answer reports, or
            ``None`` when it reports none.
        failed_guard: The check the answer did not pass -- a proof that
            did not pass, a seal whose checks failed -- named by its answer
            field where it has one, or ``None`` when the answer stands. The
            answer's own ``reason``, when it carries one, is the refusal text.
        links: Named commands or references a caller may follow next, or
            ``None`` for none.

    Returns:
        An ``ok`` envelope, or an ``error`` one whose row names the guard.
    """
    from eawf.runtime.daemon.methods.domain_envelope import (
        ENVELOPE_SCHEMA_VERSION,
        DomainEnvelope,
        DomainError,
        DomainErrorCode,
        DomainStatus,
    )

    errors: tuple[DomainError, ...] = ()
    if failed_guard is not None:
        errors = (
            DomainError(
                code=DomainErrorCode.TRANSITION_GUARD_FAILED,
                message=str(answer.get("reason") or f"{failed_guard} does not hold"),
                entity_ref=urn,
                guard=failed_guard,
                remediation=_ANSWER_REMEDIATION,
            ),
        )
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.OK if failed_guard is None else DomainStatus.ERROR,
        operation=operation,
        revision_before=revision_before,
        revision_after=revision_after,
        result=answer,
        errors=errors,
        links=dict(links or {}),
    )


def refusal_envelope(message: str, *, operation: str, urn: str) -> DomainEnvelope:
    """Return the envelope one refusal the daemon raised stands for.

    Args:
        message: The daemon's ``validation_failed`` message.
        operation: The dotted JSON-RPC name that earned it.
        urn: The subject the request addressed.

    Returns:
        An ``error`` envelope. A code in the closed vocabulary is the row's
        code; a finer one is the row's guard under
        ``transition_guard_failed``; a message leading with no code keeps
        the guard empty. The message itself is carried unchanged.
    """
    from eawf.runtime.daemon.methods.domain_envelope import (
        ENVELOPE_SCHEMA_VERSION,
        DomainEnvelope,
        DomainError,
        DomainErrorCode,
        DomainStatus,
    )

    detail = message.removeprefix(_REFUSAL_PREFIX)
    token = detail.split(": ", 1)[0]
    declared = {code.value: code for code in DomainErrorCode}
    code = declared.get(token, DomainErrorCode.TRANSITION_GUARD_FAILED)
    guard = token if token not in declared and _CODE_PATTERN.fullmatch(token) else None
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=operation,
        errors=(
            DomainError(
                code=code,
                message=detail,
                entity_ref=urn,
                guard=guard,
                remediation=_ANSWER_REMEDIATION,
            ),
        ),
    )


def _scalar(value: Any) -> str:
    """Return one leaf value as the text rendering spells it."""
    return value if isinstance(value, str) else orjson.dumps(value).decode()


def _field_lines(prefix: str, value: Any) -> list[str]:
    """Return one ``path: value`` line per leaf of *value*, depth first."""
    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, item in value.items():
            lines.extend(_field_lines(f"{prefix}.{key}", item))
        return lines
    return [f"  {prefix}: {_scalar(value)}"]


def envelope_text(envelope: DomainEnvelope, *, urn: str) -> str:
    """Return the human rendering of one envelope, carrying every fact it holds.

    Args:
        envelope: The daemon's answer.
        urn: The subject the request addressed, which names it for a
            refusal taken before any read could report one.

    Returns:
        The text body: a headline, then the revisions of a refusal, each
        error row with its guard and remediation, each warning, each result
        field, each link and the schema version.
    """
    from eawf.runtime.daemon.methods.domain_envelope import DomainStatus

    if envelope.status is DomainStatus.OK:
        lines = [
            f"{envelope.operation} ok {urn} "
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
        if envelope.revision_before is not None or envelope.revision_after is not None:
            lines.append(f"  revision {envelope.revision_before} -> {envelope.revision_after}")
    lines.extend(f"  warning: {warning}" for warning in envelope.warnings)
    lines.extend(_field_lines("result", envelope.result or {}))
    lines.extend(f"  link {name}: {target}" for name, target in envelope.links.items())
    lines.append(f"  schema_version: {envelope.schema_version}")
    return "\n".join(lines)


def envelope_exit_code(envelope: DomainEnvelope) -> int:
    """Return the typed exit status one envelope stands for.

    Args:
        envelope: The daemon's answer.

    Returns:
        :data:`~eawf.surfaces.cli.exit_codes.OK` for a committed answer;
        :data:`~eawf.surfaces.cli.exit_codes.NEEDS_OPERATOR` when a refusal
        waits on a protected approval, which only an operator can seal, so a
        harness knows a retry alone will not clear it; otherwise
        :data:`~eawf.surfaces.cli.exit_codes.STATE_CONFLICT`.
    """
    from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode, DomainStatus

    if envelope.status is DomainStatus.OK:
        return exit_codes.OK
    if any(row.code is DomainErrorCode.PROTECTED_APPROVAL_REQUIRED for row in envelope.errors):
        return exit_codes.NEEDS_OPERATOR
    return exit_codes.STATE_CONFLICT


def emit_envelope(envelope: DomainEnvelope, *, urn: str, flags: GlobalFlags) -> None:
    """Print one envelope and exit with its typed status when it refused.

    Args:
        envelope: The answer to print.
        urn: The subject the request addressed.
        flags: Resolved global flags, which pick the machine or human mode.

    Raises:
        typer.Exit: With the envelope's typed exit status when it is not
            ``ok``. The envelope prints first either way, so a caller
            reading stdout gets the code whichever branch it took.
    """
    emit_json_or_text(
        envelope.model_dump(mode="json"), envelope_text(envelope, urn=urn), flags=flags
    )
    code = envelope_exit_code(envelope)
    if code != exit_codes.OK:
        raise typer.Exit(code)


__all__ = [
    "CONTRACT_GROUPS",
    "CROSS_CUTTING_GROUPS",
    "ENTITY_GROUPS",
    "STDIN_SPEC",
    "answer_envelope",
    "emit_envelope",
    "envelope_exit_code",
    "envelope_text",
    "read_spec_document",
    "refusal_envelope",
    "request_document",
]
