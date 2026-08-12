"""Schema-forced ``LLMAssistResult`` + the bounded re-ask loop.

A spawned agent (:meth:`~eawf.runtime.runtimes.claude.adapter.ClaudeAdapter.spawn_session`)
hands back a transient :class:`~eawf.runtime.runtimes.adapter.SpawnResult` whose
``text`` is the model's *answer* — free-form bytes the runtime never validates.
For an orchestrated dispatch we need the answer to satisfy a **required
schema** (the typed ``agent_end`` report body), not free prose. This module
closes that gap with two pieces:

* :class:`LLMAssistResult` — the **schema-forced result store**. It is the
  typed product of validating a spawn's ``text`` against the forced schema:
  the parsed-and-validated body plus the spawn provenance
  (session / runtime / model) and the number of attempts the loop burned.
  Like :class:`~eawf.runtime.runtimes.metering.MeteredCost` (the priced
  product of a ``SpawnResult``) it is a transient typed result, not a
  ``state.json`` row — a later daemon writer persists it. ``extra='forbid'``
  + ``frozen`` make it a closed, immutable fact about one validated spawn.
* :func:`assist_with_schema` — the **bounded re-ask loop**. It drives an
  injected spawn callable, validates each spawn's ``text`` against the forced
  schema, and on a schema mismatch sends a correction-only prompt naming the
  validation failure up to a fixed retry ceiling. Exhausting the
  ceiling raises :class:`LLMAssistError` carrying every rejected attempt — a
  *typed* failure, never an infinite loop and never a silent pass of an
  unvalidated body.

The loop takes the spawn as an injected ``SpawnFn`` rather than importing the
daemon or a concrete adapter, so the dispatch layer stays daemon-free (the
daemon already imports this layer) and a test drives the loop with a recording
stub — no real ``claude`` subprocess, no network, no cost.

Forced schema
-------------

The default forced schema is :data:`~eawf.kernel.store.kinds.agent_report.AgentReportBody`
— the discriminated union every role's ``agent_end`` body validates against,
the canonical typed output contract for a dispatched agent. A caller MAY pass
a different ``validator`` (any callable that parses raw JSON-decoded input into
a typed body or raises) when a wave forces a narrower schema; the loop's
re-ask machinery is agnostic to which schema is forced.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.store.kinds.agent_report import AgentReportBody
from eawf.runtime.runtimes.stream_json import unwrap_agent_json
from eawf.workflow.agent_report.store import parse_agent_report_body

if TYPE_CHECKING:
    from eawf.runtime.runtimes.adapter import SpawnResult

logger = logging.getLogger(__name__)

#: Default retry ceiling for :func:`assist_with_schema`. One initial spawn plus
#: up to ``DEFAULT_MAX_ATTEMPTS - 1`` re-asks. Bounded so a model that never
#: emits schema-valid output terminates in a typed failure rather than looping.
DEFAULT_MAX_ATTEMPTS: int = 3
_MAX_REJECTED_RESPONSE_CHARS: int = 6_000

#: A callable that performs one spawn for a given prompt and returns the
#: transient :class:`~eawf.runtime.runtimes.adapter.SpawnResult`. Injected into
#: :func:`assist_with_schema` so the loop drives a live adapter in production
#: and a recording stub under test — the loop itself never imports an adapter.
type SpawnFn = Callable[[str], Awaitable["SpawnResult"]]

#: A callable that parses raw JSON-decoded input into a typed body or raises
#: :class:`pydantic.ValidationError` on a schema mismatch. The default is
#: :func:`~eawf.workflow.agent_report.store.parse_agent_report_body` (validates
#: against :data:`AgentReportBody`).
type SchemaValidator = Callable[[object], AgentReportBody]


class SchemaAttemptFailure(BaseModel):
    """One rejected spawn attempt in the bounded re-ask loop.

    Captures *why* a single spawn's ``text`` failed the forced schema so the
    next re-ask can name the failure to the model and the terminal
    :class:`LLMAssistError` can surface the full rejection trail.

    Attributes:
        attempt: 1-based attempt number this failure occurred on.
        reason: Short classification of the failure
            (``"invalid_json"`` when ``text`` is not JSON, or
            ``"schema_mismatch"`` when it parses but fails the schema).
        detail: Human-readable failure detail (the JSON-decode message or a
            compact rendering of the validation errors), bounded so a long
            Pydantic error dump cannot blow the field.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: int = Field(ge=1)
    reason: str = Field(min_length=1)
    detail: str = Field(min_length=1, max_length=2000)


class LLMAssistResult(BaseModel):
    """Schema-forced result of one assisted, validated spawn.

    The typed product of :func:`assist_with_schema`: a spawn's ``text`` parsed
    and validated against the forced schema. Transient — NOT a ``state.json``
    row (a later daemon writer persists it), mirroring
    :class:`~eawf.runtime.runtimes.metering.MeteredCost`. Frozen +
    ``extra='forbid'`` so a validated result is a closed, immutable fact.

    Attributes:
        body: The validated typed body (a member of the
            :data:`~eawf.kernel.store.kinds.agent_report.AgentReportBody`
            discriminated union by default). This is the load-bearing payload
            — by construction it has already passed the forced schema.
        session_id: Runtime-disclosed session id of the spawn that produced the
            accepted body (provenance / trace correlation).
        runtime: Adapter id that produced the result (e.g. ``"claude-code"``).
        model: Model id the spawn was requested with.
        attempts_used: Number of spawns the loop burned to obtain the accepted
            body (``1`` when the first spawn validated; up to the loop's
            ``max_attempts`` ceiling).
        prior_failures: The rejected attempts that preceded the accepted body,
            in order (empty when the first spawn validated). Retained so a
            caller can observe how many re-asks a model needed without parsing
            logs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    body: AgentReportBody
    session_id: str = Field(min_length=1)
    runtime: str = Field(min_length=1)
    model: str = Field(min_length=1)
    attempts_used: int = Field(ge=1)
    prior_failures: list[SchemaAttemptFailure] = Field(default_factory=list)


class LLMAssistError(RuntimeError):
    """Raised when the bounded re-ask loop exhausts its retries.

    Surfaces a *typed* failure when every spawn in the loop (up to
    ``max_attempts``) produced ``text`` that failed the forced schema — the
    loop never falls through to an unvalidated body and never loops forever.

    Attributes:
        attempts: The retry ceiling that was exhausted.
        failures: Every rejected attempt, in order, so the caller can inspect
            the full rejection trail rather than only the last failure.
    """

    def __init__(self, *, attempts: int, failures: list[SchemaAttemptFailure]) -> None:
        self.attempts = attempts
        self.failures = failures
        last = failures[-1].detail if failures else "no attempts recorded"
        super().__init__(
            f"schema-forced re-ask exhausted after {attempts} attempt(s); last failure: {last}"
        )


def _failure_from_exc(
    *, attempt: int, exc: json.JSONDecodeError | ValidationError
) -> SchemaAttemptFailure:
    """Build a typed failure record from the exception that rejected an attempt.

    Internal to :func:`assist_with_schema`. The loop validates each spawn's
    ``text`` exactly once; when that single attempt raises, the raised
    exception is handed here to classify the rejection — so a stateful or
    side-effecting *validator* is never invoked twice for one spawn.

    Args:
        attempt: 1-based attempt number for the failure record.
        exc: The exception the single validate attempt raised — a
            :class:`json.JSONDecodeError` (``text`` was not JSON) or a
            :class:`pydantic.ValidationError` (``text`` parsed but failed the
            forced schema).

    Returns:
        A :class:`SchemaAttemptFailure` classifying the rejection.
    """
    if isinstance(exc, json.JSONDecodeError):
        return SchemaAttemptFailure(
            attempt=attempt,
            reason="invalid_json",
            detail=f"output is not valid json: {exc}"[:2000],
        )
    return SchemaAttemptFailure(
        attempt=attempt,
        reason="schema_mismatch",
        detail=f"output failed schema: {exc.error_count()} error(s): {exc}"[:2000],
    )


def _reask_prompt(*, failure: SchemaAttemptFailure, previous_response: str) -> str:
    """Build an envelope-only correction prompt from the rejected response.

    Retries may run in a fresh serving session. Carry bounded rejected output
    plus the validation error so the model can repair the envelope without
    repeating task analysis or using tools.

    Args:
        failure: The most recent rejection to surface to the model.

    Returns:
        A compact ``## Output correction required`` notice.
    """
    bounded = previous_response[-_MAX_REJECTED_RESPONSE_CHARS:]
    return (
        "## Output correction required\n\n"
        f"Your previous response (attempt {failure.attempt}) was rejected: "
        f"{failure.reason} — {failure.detail}\n\n"
        "Previous response:\n"
        f"{bounded}\n\n"
        "Repair only the response envelope. Do not re-analyze the task and do "
        "not use tools. Respond with exactly one corrected JSON object that "
        "validates against the required agent_end report schema. No prose or "
        "code fences."
    )


async def assist_with_schema(
    prompt: str,
    *,
    spawn: SpawnFn,
    validator: SchemaValidator = parse_agent_report_body,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> LLMAssistResult:
    """Drive a spawn through the bounded re-ask loop into a validated result.

    Calls *spawn* with *prompt*; validates the returned
    :class:`~eawf.runtime.runtimes.adapter.SpawnResult` ``text`` against the
    forced schema via *validator*. On a clean parse the validated body is
    wrapped in an :class:`LLMAssistResult` and returned. On a schema mismatch
    (unparseable JSON or a :class:`pydantic.ValidationError`) the loop records a
    typed :class:`SchemaAttemptFailure`, sends a correction-only notice naming
    the failure, and spawns again — up to *max_attempts* total spawns.
    Exhausting the ceiling raises :class:`LLMAssistError` carrying every
    rejected attempt.

    The loop is **bounded** (never more than *max_attempts* spawns) and
    **fail-loud** (exhaustion is a typed error, not an unvalidated body): the
    two halves of success criterion 2.

    Args:
        prompt: The rendered dispatch prompt for the first spawn.
        spawn: Injected async callable performing one spawn per prompt. The
            production caller binds a live adapter's
            :meth:`~eawf.runtime.runtimes.claude.adapter.ClaudeAdapter.spawn_session`;
            a test binds a recording stub (no real subprocess).
        validator: The forced-schema validator. Defaults to
            :func:`~eawf.workflow.agent_report.store.parse_agent_report_body`
            (the ``agent_end`` report-body union).
        max_attempts: Total spawn ceiling (one initial + re-asks). Must be
            at least 1.

    Returns:
        The :class:`LLMAssistResult` wrapping the first body that validated.

    Raises:
        ValueError: When *max_attempts* is less than 1.
        LLMAssistError: When every spawn (up to *max_attempts*) produced output
            that failed the forced schema.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1: {max_attempts!r}")

    failures: list[SchemaAttemptFailure] = []
    current_prompt = prompt
    for attempt in range(1, max_attempts + 1):
        result = await spawn(current_prompt)
        try:
            # Peel the runtime's stream-json result envelope + strip any prose /
            # code fence the model wrapped around the report so a well-formed
            # body binds on attempt 1 instead of being rejected as invalid_json.
            decoded = json.loads(unwrap_agent_json(result.text))
            body = validator(decoded)
        except (json.JSONDecodeError, ValidationError) as exc:
            failure = _failure_from_exc(attempt=attempt, exc=exc)
            failures.append(failure)
            logger.info(
                f"assist_with_schema bind_attempt={attempt} runtime={result.runtime!r} "
                f"session={result.session_id!r} status=rejected reason={failure.reason}"
            )
            current_prompt = _reask_prompt(
                failure=failure,
                previous_response=result.text,
            )
            continue
        logger.info(
            f"assist_with_schema bind_attempt={attempt} runtime={result.runtime!r} "
            f"session={result.session_id!r} status=accepted"
        )
        return LLMAssistResult(
            body=body,
            session_id=result.session_id,
            runtime=result.runtime,
            model=result.model,
            attempts_used=attempt,
            prior_failures=failures,
        )

    logger.warning(
        f"assist_with_schema status=exhausted attempts={max_attempts} failures={len(failures)}"
    )
    raise LLMAssistError(attempts=max_attempts, failures=failures)


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "LLMAssistError",
    "LLMAssistResult",
    "SchemaAttemptFailure",
    "SchemaValidator",
    "SpawnFn",
    "assist_with_schema",
]
