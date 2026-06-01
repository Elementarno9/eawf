"""Unit tests for the live ``claude -p`` spawn + result parse (P29-I01-W21).

Pins the live-spawn engine: :meth:`ClaudeAdapter.spawn_session` forks
``claude -p`` (headless) and :func:`_parse_claude_result` parses the
single-result JSON envelope into a typed
:class:`~eawf.runtime.runtimes.adapter.SpawnResult`.

The subprocess is ALWAYS mocked — these tests never spawn a real
``claude`` process (no network / auth / cost). The well-formed parse is
exercised against a fixed envelope string; the spawn-seam tests patch
:func:`asyncio.create_subprocess_exec` with a fake process so the argv
construction + timeout + pid callback are observable without a live CLI.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from eawf.runtime.runtimes.adapter import RuntimeSpawnError, SpawnResult
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter, _parse_claude_result

# ---------------------------------------------------------------------------
# Fixtures: a well-formed ``claude -p --output-format json`` envelope
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC)

#: A representative single-result envelope from ``claude -p --output-format
#: json`` carrying the answer text, session id, usage with the TTL cache
#: split, the billed-model map, and the self-reported cost.
_WELL_FORMED_ENVELOPE: dict[str, object] = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "sess-abc123",
    "result": "the answer text",
    "total_cost_usd": 0.0123,
    "usage": {
        "input_tokens": 100,
        "output_tokens": 42,
        "cache_creation_input_tokens": 80,
        "cache_read_input_tokens": 200,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 50,
            "ephemeral_1h_input_tokens": 30,
        },
    },
    "modelUsage": {"claude-opus-4-8": {"inputTokens": 100}},
}


def _envelope_bytes(overrides: dict[str, object] | None = None) -> bytes:
    """Serialise the well-formed envelope (with optional overrides) to bytes."""
    payload = dict(_WELL_FORMED_ENVELOPE)
    if overrides:
        payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# Fake subprocess (NEVER a real ``claude`` process)
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Minimal stand-in for :class:`asyncio.subprocess.Process`.

    Records nothing about argv (the patched factory captures that) and
    replays a fixed ``(stdout, stderr)`` from :meth:`communicate`.
    """

    def __init__(
        self,
        *,
        stdout: bytes,
        stderr: bytes,
        returncode: int,
        pid: int = 4321,
        hang: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode: int | None = returncode
        self.pid = pid
        self._hang = hang
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            # Never resolve on its own; the spawn's wait_for must time out.
            import asyncio

            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return self.returncode if self.returncode is not None else -1


def _patch_factory(monkeypatch: pytest.MonkeyPatch, proc: _FakeProcess) -> list[list[str]]:
    """Patch ``asyncio.create_subprocess_exec`` to return *proc*.

    Returns a list that captures the argv of each spawn call so a test can
    assert the constructed command line without a live subprocess.

    The OS-jail seam (``_maybe_jail_argv``) is neutralised to a passthrough
    so these inner-argv assertions stay deterministic across hosts (a box
    with ``bwrap`` / ``sandbox-exec`` on PATH would otherwise prepend the
    jail wrapper). The jail seam has its own coverage in
    ``tests/runtime/sandbox/test_jail.py``.
    """
    calls: list[list[str]] = []

    async def _fake_exec(*argv: str, **_kwargs: object) -> _FakeProcess:
        calls.append(list(argv))
        return proc

    monkeypatch.setattr(claude_adapter.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", lambda argv, *, runtime, cwd: argv)
    return calls


# ---------------------------------------------------------------------------
# _parse_claude_result — well-formed envelope
# ---------------------------------------------------------------------------


def test_parse_claude_result_well_formed_envelope_parses() -> None:
    """A well-formed envelope parses every captured field into the result."""
    result = _parse_claude_result(
        runtime="claude-code",
        model="opus",
        stdout=_envelope_bytes(),
        stderr=b"",
        exit_status=0,
        subprocess_pid=4321,
        started_at=_T0,
        ended_at=_T1,
    )
    assert isinstance(result, SpawnResult)
    assert result.session_id == "sess-abc123"
    assert result.runtime == "claude-code"
    assert result.model == "opus"
    assert result.resolved_model == "claude-opus-4-8"
    assert result.subprocess_pid == 4321
    assert result.exit_status == 0
    assert result.text == "the answer text"
    assert result.input_tokens == 100
    assert result.output_tokens == 42
    assert result.cache_creation_input_tokens == 80
    assert result.cache_creation_5m_input_tokens == 50
    assert result.cache_creation_1h_input_tokens == 30
    assert result.cache_read_input_tokens == 200
    assert result.cost_usd_reported == Decimal("0.0123")
    assert result.started_at == _T0
    assert result.ended_at == _T1


def test_parse_claude_result_no_ttl_split_attributes_write_to_5m() -> None:
    """A write total with no TTL split lands wholly on the 5-minute tier."""
    result = _parse_claude_result(
        runtime="claude-code",
        model="opus",
        stdout=_envelope_bytes({"usage": {"cache_creation_input_tokens": 64}}),
        stderr=b"",
        exit_status=0,
        subprocess_pid=1,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.cache_creation_input_tokens == 64
    assert result.cache_creation_5m_input_tokens == 64
    assert result.cache_creation_1h_input_tokens == 0


def test_parse_claude_result_missing_optionals_default_to_zero() -> None:
    """A minimal envelope (no usage, no cost, no session) still parses."""
    result = _parse_claude_result(
        runtime="claude-code",
        model="haiku",
        stdout=b'{"result": "hi"}',
        stderr=b"",
        exit_status=0,
        subprocess_pid=99,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.text == "hi"
    # No session_id in the envelope falls back to a synthetic runtime-pid id.
    assert result.session_id == "claude-code-99"
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cache_creation_input_tokens == 0
    assert result.cache_read_input_tokens == 0
    assert result.resolved_model is None
    assert result.cost_usd_reported is None


def test_parse_claude_result_null_result_text_becomes_empty_string() -> None:
    """A ``result: null`` envelope yields empty text, not the string 'None'."""
    result = _parse_claude_result(
        runtime="claude-code",
        model="haiku",
        stdout=b'{"result": null, "session_id": "s1"}',
        stderr=b"",
        exit_status=0,
        subprocess_pid=7,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.text == ""


# ---------------------------------------------------------------------------
# _parse_claude_result — error / malformed paths (fail-fast, no swallowing)
# ---------------------------------------------------------------------------


def test_parse_claude_result_nonzero_exit_raises_with_stderr_snippet() -> None:
    """A non-zero exit raises and surfaces a stderr snippet."""
    with pytest.raises(RuntimeSpawnError, match="exited nonzero"):
        _parse_claude_result(
            runtime="claude-code",
            model="opus",
            stdout=b"",
            stderr=b"boom: auth failed",
            exit_status=2,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_claude_result_empty_stdout_raises() -> None:
    """Empty stdout on a clean exit is a spawn error, not an empty result."""
    with pytest.raises(RuntimeSpawnError, match="empty stdout"):
        _parse_claude_result(
            runtime="claude-code",
            model="opus",
            stdout=b"   \n",
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_claude_result_malformed_json_raises() -> None:
    """Unparseable stdout raises a typed error wrapping the JSON failure."""
    with pytest.raises(RuntimeSpawnError, match="not valid json"):
        _parse_claude_result(
            runtime="claude-code",
            model="opus",
            stdout=b"{not json",
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_claude_result_non_object_envelope_raises() -> None:
    """A JSON array (not an object) is rejected."""
    with pytest.raises(RuntimeSpawnError, match="not a json object"):
        _parse_claude_result(
            runtime="claude-code",
            model="opus",
            stdout=b"[1, 2, 3]",
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_claude_result_is_error_envelope_raises() -> None:
    """An ``is_error`` envelope raises even on a zero exit code."""
    with pytest.raises(RuntimeSpawnError, match="reported an error result"):
        _parse_claude_result(
            runtime="claude-code",
            model="opus",
            stdout=_envelope_bytes({"is_error": True, "subtype": "error_max_turns"}),
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_claude_result_non_object_usage_raises() -> None:
    """A non-object ``usage`` block is rejected rather than silently zeroed."""
    with pytest.raises(RuntimeSpawnError, match="usage block is not a json object"):
        _parse_claude_result(
            runtime="claude-code",
            model="opus",
            stdout=b'{"result": "x", "usage": "nope"}',
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


# ---------------------------------------------------------------------------
# SpawnResult model — schema-mismatch + boundary validation
# ---------------------------------------------------------------------------


def _result_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "session_id": "s1",
        "runtime": "claude-code",
        "model": "opus",
        "subprocess_pid": 1,
        "exit_status": 0,
        "text": "",
        "started_at": _T0,
        "ended_at": _T1,
    }
    base.update(overrides)
    return base


def test_spawn_result_rejects_extra_field() -> None:
    """extra='forbid' rejects an unexpected key (schema mismatch)."""
    with pytest.raises(ValidationError):
        SpawnResult(**_result_kwargs(unexpected="x"))


def test_spawn_result_rejects_zero_pid() -> None:
    """subprocess_pid is ge=1 — a zero pid is out of range."""
    with pytest.raises(ValidationError):
        SpawnResult(**_result_kwargs(subprocess_pid=0))


def test_spawn_result_rejects_negative_token_count() -> None:
    """Token counts are ge=0 — a negative count is out of range."""
    with pytest.raises(ValidationError):
        SpawnResult(**_result_kwargs(input_tokens=-1))


def test_spawn_result_rejects_empty_session_id() -> None:
    """session_id is min_length=1 — empty is rejected."""
    with pytest.raises(ValidationError):
        SpawnResult(**_result_kwargs(session_id=""))


# ---------------------------------------------------------------------------
# spawn_session — forks ``claude -p`` (mocked subprocess)
# ---------------------------------------------------------------------------


def test_spawn_session_forks_claude_p_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """spawn_session forks ``claude -p ...`` and parses the result envelope."""
    proc = _FakeProcess(stdout=_envelope_bytes(), stderr=b"", returncode=0, pid=5555)
    calls = _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = ClaudeAdapter()
    result = asyncio.run(adapter.spawn_session("solve it", model="opus"))

    # Exactly one subprocess was forked with the expected ``claude -p`` argv.
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "claude"
    assert argv[1] == "-p"
    assert argv[2] == "solve it"
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "opus"

    assert result.session_id == "sess-abc123"
    assert result.subprocess_pid == 5555
    assert result.text == "the answer text"


def test_spawn_session_appends_extra_args_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """extra_args are appended verbatim after the base argv."""
    proc = _FakeProcess(stdout=_envelope_bytes(), stderr=b"", returncode=0)
    calls = _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = ClaudeAdapter()
    asyncio.run(
        adapter.spawn_session(
            "do it",
            model="haiku",
            extra_args=("--verbose", "--max-turns", "3"),
        )
    )
    argv = calls[0]
    assert argv[-3:] == ["--verbose", "--max-turns", "3"]


def test_spawn_session_invokes_on_spawn_with_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_spawn fires with the child pid before output is collected."""
    proc = _FakeProcess(stdout=_envelope_bytes(), stderr=b"", returncode=0, pid=7777)
    _patch_factory(monkeypatch, proc)
    seen: list[int] = []

    import asyncio

    adapter = ClaudeAdapter()
    asyncio.run(adapter.spawn_session("x", model="opus", on_spawn=seen.append))
    assert seen == [7777]


def test_spawn_session_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero subprocess exit surfaces as RuntimeSpawnError."""
    proc = _FakeProcess(stdout=b"", stderr=b"500 internal_server_error", returncode=1)
    _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = ClaudeAdapter()
    with pytest.raises(RuntimeSpawnError, match="exited nonzero"):
        asyncio.run(adapter.spawn_session("x", model="opus"))


def test_spawn_session_empty_output_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty stdout on a clean exit raises rather than yielding a blank result."""
    proc = _FakeProcess(stdout=b"", stderr=b"", returncode=0)
    _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = ClaudeAdapter()
    with pytest.raises(RuntimeSpawnError, match="empty stdout"):
        asyncio.run(adapter.spawn_session("x", model="opus"))


def test_spawn_session_timeout_kills_child_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """On timeout the child is killed and a typed timeout error is raised."""
    proc = _FakeProcess(stdout=_envelope_bytes(), stderr=b"", returncode=0, hang=True)
    _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = ClaudeAdapter()
    with pytest.raises(RuntimeSpawnError, match="timed out"):
        asyncio.run(adapter.spawn_session("x", model="opus", timeout=0.01))
    assert proc.killed is True
    assert proc.waited is True
