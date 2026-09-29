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
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import orjson

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes

if TYPE_CHECKING:
    from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope

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
#: and so have no entity group to live in. Closed by amendment.
CROSS_CUTTING_GROUPS: Final[tuple[str, ...]] = (
    "workspace",
    "config",
    "daemon",
    "memory",
    "ui",
    "migrate",
    "reflect",
)

#: The ``--from-spec`` value that reads the document from stdin.
STDIN_SPEC: Final = "-"


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


__all__ = [
    "CROSS_CUTTING_GROUPS",
    "ENTITY_GROUPS",
    "STDIN_SPEC",
    "envelope_exit_code",
    "envelope_text",
    "read_spec_document",
]
