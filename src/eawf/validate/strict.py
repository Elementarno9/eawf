"""Two-layer state validation: schema (Pydantic) + cross-entity invariants.

The result is a :class:`ValidationReport` with separate buckets for schema and
invariant errors so that the CLI can fail fast on schema problems without
attempting to evaluate invariants over a half-typed payload.

``strict_optional`` controls whether absent optional top-level keys are flagged
as violations.  Schema errors and invariant violations are always reported
regardless of this flag.

Phase 4 W01 adds :func:`validate_envelope` and :class:`EnvelopeValidationReport`:
the strict validator for skill output envelopes. It enforces the two
strict-mode contracts from `ea-proposal.md` §15.1:

- ``status=needs_user`` MUST set ``body.user_question``.
- ``status in {blocked, failed}`` MUST set non-empty ``footer.repair_commands``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson
from pydantic import ValidationError
from pydantic_core import ErrorDetails

from eawf.render.envelope import OutputEnvelope
from eawf.state.models import State
from eawf.validate.invariants import ALL_INVARIANTS, Violation

logger = logging.getLogger(__name__)

# Optional top-level state keys flagged under --strict.
# project/workspace are excluded: they are alternation-required and handled
# by schema-layer business logic elsewhere.
_STRICT_OPTIONAL_KEYS: tuple[str, ...] = (
    "health",
    "subprojects",
    "goals",
    "outcomes",
    "estimates",
    "actuals",
    "hypotheses",
    "audits",
    "incidents",
    "backlog",
    "worktrees",
    "mcp_servers",
    "memory_index",
)


def _format_pydantic_error(err: ErrorDetails) -> str:
    """Render a single Pydantic v2 error as ``"<loc>: <msg>"``."""
    loc = ".".join(str(part) for part in err.get("loc", ()))
    msg = err.get("msg", "validation error")
    return f"{loc}: {msg}" if loc else f"{msg}"


def _strict_optional_violations(state: State) -> list[Violation]:
    """Return violations for any None-valued optional key when ``--strict`` is on."""
    out: list[Violation] = []
    for key in _STRICT_OPTIONAL_KEYS:
        if getattr(state, key) is None:
            out.append(
                Violation(
                    code="STRICT.OPTIONAL_MISSING",
                    path=f"/{key}",
                    message=f"optional key {key!r} is missing under --strict",
                )
            )
    return out


@dataclass(frozen=True)
class ValidationReport:
    """Outcome of validating a state document.

    ``state`` is ``None`` when schema validation failed; in that case
    ``violations`` is always empty because invariant checks require a typed
    :class:`~eawf.state.models.State`.
    """

    state: State | None
    schema_errors: list[str]
    violations: list[Violation]

    @property
    def ok(self) -> bool:
        """``True`` when no schema errors and no invariant violations."""
        return not self.schema_errors and not self.violations


def validate_state(payload: dict[str, Any], *, strict_optional: bool = False) -> ValidationReport:
    """Validate a decoded JSON payload.

    Schema errors short-circuit; invariants are only run when the payload
    survives :meth:`State.model_validate`.

    Args:
        payload: Decoded JSON dictionary to validate.
        strict_optional: When ``True``, absent optional top-level keys are
            added as ``STRICT.OPTIONAL_MISSING`` violations.
    """
    try:
        state = State.model_validate(payload)
    except ValidationError as exc:
        schema_errors = [_format_pydantic_error(e) for e in exc.errors()]
        logger.debug(f"schema validation failed with {len(schema_errors)} error(s)")
        return ValidationReport(
            state=None,
            schema_errors=schema_errors,
            violations=[],
        )
    violations: list[Violation] = []
    for invariant in ALL_INVARIANTS:
        violations.extend(invariant(state))
    if strict_optional:
        violations.extend(_strict_optional_violations(state))
    logger.debug(f"invariant pass: {len(violations)} violation(s)")
    return ValidationReport(state=state, schema_errors=[], violations=violations)


def validate_path(path: Path, *, strict_optional: bool = False) -> ValidationReport:
    """Read a state file from disk and validate it.

    Args:
        path: Path to the ``.ea/state.json`` file.
        strict_optional: Forwarded to :func:`validate_state`.
    """
    payload = orjson.loads(Path(path).read_bytes())
    return validate_state(payload, strict_optional=strict_optional)


@dataclass(frozen=True)
class EnvelopeValidationReport:
    """Outcome of validating a skill output envelope.

    ``envelope`` is ``None`` when schema validation failed; in that case
    ``contract_errors`` is always empty because contract checks require a
    typed :class:`~eawf.render.envelope.OutputEnvelope`.

    Attributes:
        envelope: Validated envelope or ``None`` on schema failure.
        schema_errors: One string per Pydantic v2 error, in
            ``"<loc>: <msg>"`` form.
        contract_errors: Strict-mode contract violations from §15.1
            (e.g. ``status=needs_user`` without ``body.user_question``).
    """

    envelope: OutputEnvelope | None
    schema_errors: list[str]
    contract_errors: list[str]

    @property
    def ok(self) -> bool:
        """``True`` when no schema errors and no contract errors."""
        return not self.schema_errors and not self.contract_errors


def _envelope_contract_errors(envelope: OutputEnvelope) -> list[str]:
    """Run the §15.1 strict-mode contract checks on a typed envelope.

    The two checks are:

    - ``status=needs_user`` requires ``body.user_question``. We accept
      any payload that has a non-null ``user_question`` field; the
      caller is free to use any of the typed body models from
      :mod:`eawf.skills.bodies` (each carries a ``user_question: UserQuestion |
      None`` slot) or a raw dict body with the same key.
    - ``status in {blocked, failed}`` requires
      ``footer.repair_commands`` to be a non-empty list of strings.
    """
    errors: list[str] = []
    status = envelope.header.status
    if status == "needs_user":
        body = envelope.body
        if isinstance(body, str):
            errors.append("status=needs_user requires body.user_question; got string body")
        else:
            user_question = body.get("user_question")
            if user_question is None:
                errors.append("status=needs_user requires non-null body.user_question")
    if status in {"blocked", "failed"}:
        repair = envelope.footer.repair_commands
        if not repair:
            errors.append(f"status={status} requires non-empty footer.repair_commands")
    return errors


def validate_envelope(payload: dict[str, Any]) -> EnvelopeValidationReport:
    """Validate a decoded skill output envelope payload.

    Schema errors short-circuit; the strict-mode contract checks only
    run when the payload survives :meth:`OutputEnvelope.model_validate`.

    Args:
        payload: Decoded JSON dictionary.
    """
    try:
        envelope = OutputEnvelope.model_validate(payload)
    except ValidationError as exc:
        schema_errors = [_format_pydantic_error(e) for e in exc.errors()]
        logger.debug(f"envelope schema validation failed with {len(schema_errors)} error(s)")
        return EnvelopeValidationReport(
            envelope=None,
            schema_errors=schema_errors,
            contract_errors=[],
        )
    contract_errors = _envelope_contract_errors(envelope)
    logger.debug(f"envelope contract pass: {len(contract_errors)} error(s)")
    return EnvelopeValidationReport(
        envelope=envelope,
        schema_errors=[],
        contract_errors=contract_errors,
    )


def validate_envelope_path(path: Path) -> EnvelopeValidationReport:
    """Read an envelope JSON file from disk and validate it.

    Args:
        path: Path to the envelope JSON file.
    """
    payload = orjson.loads(Path(path).read_bytes())
    return validate_envelope(payload)
