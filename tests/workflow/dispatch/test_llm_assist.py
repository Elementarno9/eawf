"""Unit tests for the schema-forced LLMAssistResult + re-ask loop (P29-I01-W24).

Pins the structured-output contract for spawned agents:
:func:`~eawf.workflow.dispatch.llm_assist.assist_with_schema` validates a
spawn's answer ``text`` against the forced ``agent_end`` report schema and
either wraps the validated body in a typed
:class:`~eawf.workflow.dispatch.llm_assist.LLMAssistResult` or, once the
bounded retry ceiling is exhausted, raises a typed
:class:`~eawf.workflow.dispatch.llm_assist.LLMAssistError`.

The spawn is ALWAYS a recording stub — these tests never fork a real
``claude`` subprocess (no network, no auth, no cost). The stub replays a
queue of canned answer strings and records each prompt it was handed so the
re-ask path (a correction-annotated re-prompt) is observable.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.dispatch.llm_assist import (
    DEFAULT_MAX_ATTEMPTS,
    LLMAssistError,
    LLMAssistResult,
    SchemaAttemptFailure,
    assist_with_schema,
)

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures: a valid agent_end body + the spawn-result envelope around it
# ---------------------------------------------------------------------------


def _valid_body_json() -> str:
    """Serialise a minimal schema-valid executor ``agent_end`` body to JSON."""
    return json.dumps(
        {
            "role": "executor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "did the thing",
            "wave_id": "P29-I01-W24",
            "files_changed": ["src/x.py"],
            "tests_run": ["uv run pytest -q"],
            "commit_sha": "abcdef1",
            "outcome": "shipped",
        }
    )


def _spawn_result(text: str, *, pid: int = 4321) -> SpawnResult:
    """Wrap *text* in an otherwise-valid :class:`SpawnResult` envelope."""
    return SpawnResult(
        session_id="sess-abc123",
        runtime="claude-code",
        model="opus",
        subprocess_pid=pid,
        exit_status=0,
        text=text,
        started_at=_T0,
        ended_at=_T1,
    )


class _RecordingSpawn:
    """Recording stand-in for a live ``spawn_session`` (NEVER a real process).

    Replays a queue of canned answer strings — one per call — and records the
    prompt each call was handed so a test can assert the re-ask prompt carried
    the correction notice. Raises if called more times than answers queued so
    an unbounded loop surfaces as a test failure rather than an ``IndexError``
    deep in the loop.
    """

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []
        self.calls = 0

    async def __call__(self, prompt: str) -> SpawnResult:
        self.prompts.append(prompt)
        if self.calls >= len(self._answers):
            raise AssertionError(
                f"spawn called {self.calls + 1} times but only "
                f"{len(self._answers)} answer(s) queued (unbounded loop?)"
            )
        text = self._answers[self.calls]
        self.calls += 1
        return _spawn_result(text)


# ---------------------------------------------------------------------------
# Success criterion 1: valid structured output validates into the result store
# ---------------------------------------------------------------------------


def test_assist_with_schema_valid_output_validates_into_result() -> None:
    """A first-spawn schema-valid body wraps into LLMAssistResult, no re-ask."""
    spawn = _RecordingSpawn([_valid_body_json()])
    result = asyncio.run(assist_with_schema("solve it", spawn=spawn))

    assert isinstance(result, LLMAssistResult)
    assert result.attempts_used == 1
    assert result.prior_failures == []
    assert result.session_id == "sess-abc123"
    assert result.runtime == "claude-code"
    assert result.model == "opus"
    # The body is the validated, typed agent_end body — not the raw string.
    assert result.body.role == "executor"
    assert result.body.verdict.value == "pass"
    # Exactly one spawn, and the prompt was the verbatim dispatched prompt.
    assert spawn.calls == 1
    assert spawn.prompts == ["solve it"]


# ---------------------------------------------------------------------------
# Success criterion 2a: schema-mismatch triggers a bounded re-ask
# ---------------------------------------------------------------------------


def test_assist_with_schema_invalid_then_valid_re_asks_and_succeeds() -> None:
    """A first schema-mismatch re-asks; the second valid answer is accepted."""
    bad = json.dumps({"role": "executor", "verdict": "pass"})  # missing required fields
    spawn = _RecordingSpawn([bad, _valid_body_json()])
    result = asyncio.run(assist_with_schema("solve it", spawn=spawn))

    assert result.attempts_used == 2
    assert spawn.calls == 2
    # The re-ask prompt carried the correction notice naming the rejection.
    assert spawn.prompts[0] == "solve it"
    assert "## Output correction required" in spawn.prompts[1]
    assert "schema_mismatch" in spawn.prompts[1]
    # The single prior failure is retained on the accepted result.
    assert len(result.prior_failures) == 1
    assert result.prior_failures[0].reason == "schema_mismatch"
    assert result.prior_failures[0].attempt == 1


def test_assist_with_schema_invalid_json_classified_and_re_asked() -> None:
    """Unparseable answer text is a re-ask trigger classified ``invalid_json``."""
    spawn = _RecordingSpawn(["not json at all {{{", _valid_body_json()])
    result = asyncio.run(assist_with_schema("go", spawn=spawn))

    assert result.attempts_used == 2
    assert result.prior_failures[0].reason == "invalid_json"
    assert "not valid json" in result.prior_failures[0].detail
    assert "invalid_json" in spawn.prompts[1]


# ---------------------------------------------------------------------------
# Success criterion 2b: the loop is bounded — it stops after N spawns
# ---------------------------------------------------------------------------


def test_assist_with_schema_is_bounded_stops_after_max_attempts() -> None:
    """The loop spawns at most max_attempts times, never an infinite loop."""
    bad = json.dumps({"role": "executor"})  # always schema-invalid
    # Queue one MORE answer than the ceiling; the loop must not consume it.
    spawn = _RecordingSpawn([bad, bad, bad, _valid_body_json()])
    with pytest.raises(LLMAssistError):
        asyncio.run(assist_with_schema("go", spawn=spawn, max_attempts=3))
    # Exactly the ceiling — the 4th (valid) answer was never reached.
    assert spawn.calls == 3


def test_assist_with_schema_default_ceiling_is_three() -> None:
    """The default retry ceiling is DEFAULT_MAX_ATTEMPTS (3) spawns."""
    bad = json.dumps({"role": "executor"})
    spawn = _RecordingSpawn([bad] * DEFAULT_MAX_ATTEMPTS)
    with pytest.raises(LLMAssistError):
        asyncio.run(assist_with_schema("go", spawn=spawn))
    assert spawn.calls == DEFAULT_MAX_ATTEMPTS == 3


def test_assist_with_schema_single_attempt_no_re_ask() -> None:
    """max_attempts=1 spawns once and fails typed — no re-ask on a bad answer."""
    spawn = _RecordingSpawn([json.dumps({"role": "executor"})])
    with pytest.raises(LLMAssistError):
        asyncio.run(assist_with_schema("go", spawn=spawn, max_attempts=1))
    assert spawn.calls == 1


# ---------------------------------------------------------------------------
# Success criterion 2c: exhausted retries surface a TYPED failure
# ---------------------------------------------------------------------------


def test_assist_with_schema_exhaustion_raises_typed_error_with_trail() -> None:
    """Exhausting retries raises LLMAssistError carrying every rejection."""
    bad = json.dumps({"role": "executor"})
    spawn = _RecordingSpawn([bad, bad, bad])
    with pytest.raises(LLMAssistError) as excinfo:
        asyncio.run(assist_with_schema("go", spawn=spawn, max_attempts=3))

    err = excinfo.value
    assert err.attempts == 3
    assert len(err.failures) == 3
    assert all(isinstance(f, SchemaAttemptFailure) for f in err.failures)
    assert [f.attempt for f in err.failures] == [1, 2, 3]
    assert "exhausted after 3 attempt(s)" in str(err)


def test_assist_with_schema_exhaustion_is_not_a_silent_pass() -> None:
    """An exhausted loop never returns an unvalidated body (no silent pass)."""
    bad = "still not valid json"
    spawn = _RecordingSpawn([bad, bad])
    # The contract: exhaustion RAISES; it does not return a falsy/partial result.
    with pytest.raises(LLMAssistError):
        asyncio.run(assist_with_schema("go", spawn=spawn, max_attempts=2))


# ---------------------------------------------------------------------------
# Error path: invalid max_attempts argument
# ---------------------------------------------------------------------------


def test_assist_with_schema_rejects_zero_max_attempts() -> None:
    """max_attempts < 1 is a ValueError before any spawn is attempted."""
    spawn = _RecordingSpawn([_valid_body_json()])
    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        asyncio.run(assist_with_schema("go", spawn=spawn, max_attempts=0))
    # No spawn was attempted — the guard fires first.
    assert spawn.calls == 0


# ---------------------------------------------------------------------------
# LLMAssistResult / SchemaAttemptFailure model — closed-schema invariants
# ---------------------------------------------------------------------------


def test_llm_assist_result_is_frozen_and_forbids_extra() -> None:
    """The result store is frozen and rejects unexpected keys (schema mismatch)."""
    spawn = _RecordingSpawn([_valid_body_json()])
    result = asyncio.run(assist_with_schema("go", spawn=spawn))
    # frozen: mutation is rejected.
    with pytest.raises(ValidationError):
        result.attempts_used = 9  # type: ignore[misc]
    # extra='forbid': an unexpected construction key is rejected.
    with pytest.raises(ValidationError):
        LLMAssistResult(
            body=result.body,
            session_id="s1",
            runtime="claude-code",
            model="opus",
            attempts_used=1,
            unexpected="x",  # type: ignore[call-arg]
        )


def test_schema_attempt_failure_requires_positive_attempt() -> None:
    """SchemaAttemptFailure.attempt is ge=1 — a zero attempt is out of range."""
    with pytest.raises(ValidationError):
        SchemaAttemptFailure(attempt=0, reason="schema_mismatch", detail="x")


def test_assist_with_schema_honours_custom_validator() -> None:
    """A caller-supplied validator forces a narrower schema than the default."""
    sentinel_calls: list[object] = []

    def _reject_everything(decoded: object) -> object:
        sentinel_calls.append(decoded)
        raise ValidationError.from_exception_data("custom", [])

    # Even valid agent_end JSON fails the custom validator, so the loop
    # exhausts — proving the injected validator (not the default) gates.
    spawn = _RecordingSpawn([_valid_body_json(), _valid_body_json()])
    with pytest.raises(LLMAssistError):
        asyncio.run(
            assist_with_schema(
                "go",
                spawn=spawn,
                validator=_reject_everything,  # type: ignore[arg-type]
                max_attempts=2,
            )
        )
    assert len(sentinel_calls) == 2
