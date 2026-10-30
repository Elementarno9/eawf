"""Strict validation of skill output envelopes.

:func:`validate_envelope` and :class:`EnvelopeValidationReport` enforce the two
strict-mode contracts from `docs/architecture/envelope.md`:

- ``status=needs_user`` MUST set ``body.user_question``.
- ``status in {blocked, failed}`` MUST set non-empty ``footer.repair_commands``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from pydantic_core import ErrorDetails

from eawf.surfaces.render.envelope import OutputEnvelope

logger = logging.getLogger(__name__)


def _format_pydantic_error(err: ErrorDetails) -> str:
    """Render a single Pydantic v2 error as ``"<loc>: <msg>"``."""
    loc = ".".join(str(part) for part in err.get("loc", ()))
    msg = err.get("msg", "validation error")
    return f"{loc}: {msg}" if loc else f"{msg}"


@dataclass(frozen=True)
class EnvelopeValidationReport:
    """Outcome of validating a skill output envelope.

    ``envelope`` is ``None`` when schema validation failed; in that case
    ``contract_errors`` is always empty because contract checks require a
    typed :class:`~eawf.surfaces.render.envelope.OutputEnvelope`.

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
      :mod:`eawf.workflow.skills.bodies` (each carries a ``user_question: UserQuestion |
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
        logger.debug(f"validate_envelope schema_errors={len(schema_errors)}")
        return EnvelopeValidationReport(
            envelope=None,
            schema_errors=schema_errors,
            contract_errors=[],
        )
    contract_errors = _envelope_contract_errors(envelope)
    logger.debug(f"validate_envelope contract_errors={len(contract_errors)}")
    return EnvelopeValidationReport(
        envelope=envelope,
        schema_errors=[],
        contract_errors=contract_errors,
    )
