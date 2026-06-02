"""Unit tests for the live ``opencode run`` spawn + result parse (P29-I04-W06).

Pins the live-spawn engine: :meth:`OpenCodeAdapter.spawn_session` forks
``opencode run --format json`` (headless) and
:func:`_parse_opencode_result` parses the newline-delimited JSON event
stream into a typed
:class:`~eawf.runtime.runtimes.adapter.SpawnResult`.

The subprocess is ALWAYS mocked -- these tests never spawn a real
``opencode`` process (no network / auth / cost). The well-formed parse is
exercised against a fixed event stream captured from a real
``opencode run --format json`` invocation; the spawn-seam tests patch
:func:`asyncio.create_subprocess_exec` with a fake process so the argv
construction + timeout + pid callback are observable without a live CLI.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from eawf.runtime.runtimes.adapter import RuntimeSpawnError, SpawnResult
from eawf.runtime.runtimes.opencode import adapter as opencode_adapter
from eawf.runtime.runtimes.opencode.adapter import (
    OpenCodeAdapter,
    _line_json_objects,
    _parse_opencode_result,
)

# ---------------------------------------------------------------------------
# Fixtures: a well-formed ``opencode run --format json`` event stream
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC)

_SESSION_ID = "ses_17a0a0d47ffehjigY9e5iwv7uf"

#: A representative event stream captured live from
#: ``opencode run --format json -m <provider/model> "reply with the single
#: word ok"`` -- step_start, two text fragments (proving concatenation), and
#: the terminal step_finish carrying the token map + cost. Newline-delimited
#: JSON, one object per line (the real shape; see capabilities.yaml).
_STREAM_EVENTS: list[dict[str, object]] = [
    {
        "type": "step_start",
        "timestamp": 1780363966284,
        "sessionID": _SESSION_ID,
        "part": {"type": "step-start"},
    },
    {
        "type": "text",
        "timestamp": 1780363970084,
        "sessionID": _SESSION_ID,
        "part": {"type": "text", "text": "\n\n"},
    },
    {
        "type": "text",
        "timestamp": 1780363970085,
        "sessionID": _SESSION_ID,
        "part": {"type": "text", "text": "ok"},
    },
    {
        "type": "step_finish",
        "timestamp": 1780363970086,
        "sessionID": _SESSION_ID,
        "part": {
            "type": "step-finish",
            "reason": "stop",
            "tokens": {
                "total": 30580,
                "input": 30489,
                "output": 7,
                "reasoning": 84,
                "cache": {"write": 12, "read": 34},
            },
            "cost": 0.0021,
        },
    },
]


def _stream_bytes(events: list[dict[str, object]] | None = None) -> bytes:
    """Serialise an NDJSON event stream to bytes (one JSON object per line)."""
    rows = _STREAM_EVENTS if events is None else events
    return ("\n".join(json.dumps(row) for row in rows) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Fake subprocess (NEVER a real ``opencode`` process)
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
    jail wrapper). The env-scrub seam is neutralised to a constant dict so
    the spawn does not read the real process environment.
    """
    calls: list[list[str]] = []

    async def _fake_exec(*argv: str, **_kwargs: object) -> _FakeProcess:
        calls.append(list(argv))
        return proc

    monkeypatch.setattr(opencode_adapter.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(opencode_adapter, "_maybe_jail_argv", lambda argv, *, runtime, cwd: argv)
    monkeypatch.setattr(opencode_adapter, "build_child_env", lambda *_a, **_k: {"PATH": "/usr/bin"})
    return calls


# ---------------------------------------------------------------------------
# _line_json_objects -- NDJSON parse tolerates terminal escape noise
# ---------------------------------------------------------------------------


def test_line_json_objects_parses_newline_delimited_stream() -> None:
    """Each NDJSON line decodes into a dict in stream order."""
    objects = _line_json_objects(_stream_bytes().decode("utf-8"))
    assert [obj["type"] for obj in objects] == [
        "step_start",
        "text",
        "text",
        "step_finish",
    ]


def test_line_json_objects_skips_ansi_title_noise() -> None:
    """A line prefixed with an OSC title-set escape is trimmed to its JSON.

    opencode interleaves terminal title-set escapes onto the JSON stream;
    the parser must find the first ``{`` and skip a line carrying no JSON.
    """
    noisy = "\x1b]0;tmp: working\x07" + json.dumps(_STREAM_EVENTS[1])
    blank = "\x1b]0;tmp: done\x07"
    raw = "\n".join([noisy, blank])
    objects = _line_json_objects(raw)
    assert len(objects) == 1
    assert objects[0]["type"] == "text"


def test_line_json_objects_drops_non_object_json() -> None:
    """A bare JSON array line is dropped (only objects are kept)."""
    raw = "[1, 2, 3]\n" + json.dumps(_STREAM_EVENTS[3])
    objects = _line_json_objects(raw)
    assert len(objects) == 1
    assert objects[0]["type"] == "step_finish"


# ---------------------------------------------------------------------------
# _parse_opencode_result -- well-formed stream
# ---------------------------------------------------------------------------


def test_parse_opencode_result_well_formed_stream_parses() -> None:
    """A well-formed event stream parses every captured field into the result."""
    result = _parse_opencode_result(
        runtime="opencode",
        model="anthropic/claude-x",
        stdout=_stream_bytes(),
        stderr=b"",
        exit_status=0,
        subprocess_pid=4321,
        started_at=_T0,
        ended_at=_T1,
    )
    assert isinstance(result, SpawnResult)
    assert result.session_id == _SESSION_ID
    assert result.runtime == "opencode"
    assert result.model == "anthropic/claude-x"
    # opencode does not disclose a separate billed-model id.
    assert result.resolved_model is None
    assert result.subprocess_pid == 4321
    assert result.exit_status == 0
    # The two text fragments concatenate into the final answer.
    assert result.text == "\n\nok"
    assert result.input_tokens == 30489
    assert result.output_tokens == 7
    assert result.cache_read_input_tokens == 34
    assert result.cache_creation_input_tokens == 12
    assert result.cache_creation_5m_input_tokens == 12
    assert result.cache_creation_1h_input_tokens == 0
    assert result.cost_usd_reported == Decimal("0.0021")
    assert result.started_at == _T0
    assert result.ended_at == _T1


def test_parse_opencode_result_missing_tokens_default_to_zero() -> None:
    """A stream with no token map defaults every count to zero (priced honestly)."""
    events: list[dict[str, object]] = [
        {"type": "text", "sessionID": "s1", "part": {"type": "text", "text": "hi"}},
        {"type": "step_finish", "sessionID": "s1", "part": {"type": "step-finish"}},
    ]
    result = _parse_opencode_result(
        runtime="opencode",
        model="anthropic/claude-x",
        stdout=_stream_bytes(events),
        stderr=b"",
        exit_status=0,
        subprocess_pid=99,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.text == "hi"
    assert result.session_id == "s1"
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cache_read_input_tokens == 0
    assert result.cache_creation_input_tokens == 0
    assert result.cost_usd_reported is None


def test_parse_opencode_result_no_session_id_falls_back_to_runtime_pid() -> None:
    """A stream with no sessionID synthesises ``<runtime>-<pid>``."""
    events: list[dict[str, object]] = [
        {"type": "text", "part": {"type": "text", "text": "hi"}},
    ]
    result = _parse_opencode_result(
        runtime="opencode",
        model="anthropic/claude-x",
        stdout=_stream_bytes(events),
        stderr=b"",
        exit_status=0,
        subprocess_pid=7,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.session_id == "opencode-7"


def test_parse_opencode_result_no_text_events_yields_empty_string() -> None:
    """A stream with only a step_finish (no text) yields empty answer text."""
    events: list[dict[str, object]] = [
        {
            "type": "step_finish",
            "sessionID": "s1",
            "part": {"type": "step-finish", "tokens": {"input": 5, "output": 0}},
        },
    ]
    result = _parse_opencode_result(
        runtime="opencode",
        model="anthropic/claude-x",
        stdout=_stream_bytes(events),
        stderr=b"",
        exit_status=0,
        subprocess_pid=1,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.text == ""
    assert result.input_tokens == 5


# ---------------------------------------------------------------------------
# _parse_opencode_result -- error / malformed paths (fail-fast, no swallowing)
# ---------------------------------------------------------------------------


def test_parse_opencode_result_nonzero_exit_raises_with_stderr_snippet() -> None:
    """A non-zero exit raises and surfaces a stderr snippet."""
    with pytest.raises(RuntimeSpawnError, match="exited nonzero"):
        _parse_opencode_result(
            runtime="opencode",
            model="anthropic/claude-x",
            stdout=b"",
            stderr=b"boom: auth failed",
            exit_status=2,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_opencode_result_empty_stdout_raises() -> None:
    """Empty stdout on a clean exit is a spawn error, not an empty result."""
    with pytest.raises(RuntimeSpawnError, match="empty stdout"):
        _parse_opencode_result(
            runtime="opencode",
            model="anthropic/claude-x",
            stdout=b"   \n",
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_opencode_result_no_decodable_events_raises() -> None:
    """Stdout with no decodable JSON object lines raises a typed error."""
    with pytest.raises(RuntimeSpawnError, match="no decodable json events"):
        _parse_opencode_result(
            runtime="opencode",
            model="anthropic/claude-x",
            stdout=b"{not json\nalso not json\n",
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_opencode_result_error_event_raises() -> None:
    """An ``error`` event in the stream raises even on a zero exit code.

    Mirrors the live ``Model not found`` error captured from a real spawn:
    ``{"type":"error","error":{"name":"UnknownError","data":{"message":...}}}``.
    """
    events: list[dict[str, object]] = [
        {
            "type": "error",
            "sessionID": "s1",
            "error": {
                "name": "UnknownError",
                "data": {"message": "Model not found: anthropic/claude-x."},
            },
        },
    ]
    with pytest.raises(RuntimeSpawnError, match="reported an error event"):
        _parse_opencode_result(
            runtime="opencode",
            model="anthropic/claude-x",
            stdout=_stream_bytes(events),
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


# ---------------------------------------------------------------------------
# spawn_session -- forks ``opencode run`` (mocked subprocess)
# ---------------------------------------------------------------------------


def test_spawn_session_forks_opencode_run_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """spawn_session forks ``opencode run --format json -m ...`` and parses."""
    proc = _FakeProcess(stdout=_stream_bytes(), stderr=b"", returncode=0, pid=5555)
    calls = _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = OpenCodeAdapter()
    result = asyncio.run(adapter.spawn_session("solve it", model="anthropic/claude-x"))

    # Exactly one subprocess was forked with the expected ``opencode run`` argv.
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "opencode"
    assert argv[1] == "run"
    assert "--format" in argv
    assert argv[argv.index("--format") + 1] == "json"
    assert "-m" in argv
    assert argv[argv.index("-m") + 1] == "anthropic/claude-x"
    # The prompt is the trailing positional (opencode treats message as a positional).
    assert argv[-1] == "solve it"

    assert result.session_id == _SESSION_ID
    assert result.subprocess_pid == 5555
    assert result.text == "\n\nok"


def test_spawn_session_scrubs_child_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawn passes a scrubbed env (no parent credential leak)."""
    proc = _FakeProcess(stdout=_stream_bytes(), stderr=b"", returncode=0)
    seen_env: list[dict[str, str]] = []

    async def _fake_exec(*_argv: str, **kwargs: object) -> _FakeProcess:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        seen_env.append(env)
        return proc

    monkeypatch.setattr(opencode_adapter.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(opencode_adapter, "_maybe_jail_argv", lambda argv, *, runtime, cwd: argv)
    monkeypatch.setattr(
        opencode_adapter,
        "build_child_env",
        lambda *_a, **_k: {"PATH": "/usr/bin", "HOME": "/sandbox/agent"},
    )

    import asyncio

    adapter = OpenCodeAdapter()
    asyncio.run(adapter.spawn_session("x", model="anthropic/claude-x"))
    assert seen_env == [{"PATH": "/usr/bin", "HOME": "/sandbox/agent"}]


def test_spawn_session_appends_extra_args_before_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """extra_args are appended verbatim, with the prompt staying the tail positional."""
    proc = _FakeProcess(stdout=_stream_bytes(), stderr=b"", returncode=0)
    calls = _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = OpenCodeAdapter()
    asyncio.run(
        adapter.spawn_session(
            "do it",
            model="anthropic/claude-x",
            extra_args=("--print-logs", "--pure"),
        )
    )
    argv = calls[0]
    # The prompt remains the trailing positional after the verbatim extra args.
    assert argv[-1] == "do it"
    assert argv[-3:-1] == ["--print-logs", "--pure"]


def test_spawn_session_denied_tools_adds_no_argv_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-empty deny-list adds NO argv flag (opencode has no per-call deny flag)."""
    proc = _FakeProcess(stdout=_stream_bytes(), stderr=b"", returncode=0)
    calls_deny = _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = OpenCodeAdapter()
    asyncio.run(
        adapter.spawn_session("x", model="anthropic/claude-x", denied_tools=["Edit", "Bash"])
    )
    asyncio.run(adapter.spawn_session("x", model="anthropic/claude-x"))

    # The deny-list does NOT change the argv (no fake flag); the two spawns
    # are byte-identical and neither carries a deny-style flag.
    assert calls_deny[0] == calls_deny[1]
    assert "--disallowedTools" not in calls_deny[0]
    assert not any(tok in calls_deny[0] for tok in ("Edit", "Bash"))


def test_spawn_session_jail_wraps_inner_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """The jail seam receives the full inner argv built before the wrap.

    Records the argv passed into ``_maybe_jail_argv`` (rather than
    neutralising it) and asserts the spawned argv is the jail-wrapped form
    of that exact argv -- proving the jail confines the full child argv.
    """
    proc = _FakeProcess(stdout=_stream_bytes(), stderr=b"", returncode=0)
    calls: list[list[str]] = []

    async def _fake_exec(*argv: str, **_kwargs: object) -> _FakeProcess:
        calls.append(list(argv))
        return proc

    seen_by_jail: list[list[str]] = []

    def _recording_jail(argv: list[str], *, runtime: str, cwd: str | None) -> list[str]:
        seen_by_jail.append(list(argv))
        return ["JAIL", *argv]

    monkeypatch.setattr(opencode_adapter.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(opencode_adapter, "_maybe_jail_argv", _recording_jail)
    monkeypatch.setattr(opencode_adapter, "build_child_env", lambda *_a, **_k: {"PATH": "/usr/bin"})

    import asyncio

    adapter = OpenCodeAdapter()
    asyncio.run(adapter.spawn_session("x", model="anthropic/claude-x"))

    assert len(seen_by_jail) == 1
    assert seen_by_jail[0][0] == "opencode"
    assert seen_by_jail[0][1] == "run"
    # The spawned argv is the jail-wrapped form of that same inner argv.
    assert calls[0][0] == "JAIL"
    assert calls[0][1:] == seen_by_jail[0]


def test_spawn_session_invokes_on_spawn_with_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_spawn fires with the child pid before output is collected."""
    proc = _FakeProcess(stdout=_stream_bytes(), stderr=b"", returncode=0, pid=7777)
    _patch_factory(monkeypatch, proc)
    seen: list[int] = []

    import asyncio

    adapter = OpenCodeAdapter()
    asyncio.run(adapter.spawn_session("x", model="anthropic/claude-x", on_spawn=seen.append))
    assert seen == [7777]


def test_spawn_session_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero subprocess exit surfaces as RuntimeSpawnError."""
    proc = _FakeProcess(stdout=b"", stderr=b"500 internal_server_error", returncode=1)
    _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = OpenCodeAdapter()
    with pytest.raises(RuntimeSpawnError, match="exited nonzero"):
        asyncio.run(adapter.spawn_session("x", model="anthropic/claude-x"))


def test_spawn_session_empty_output_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty stdout on a clean exit raises rather than yielding a blank result."""
    proc = _FakeProcess(stdout=b"", stderr=b"", returncode=0)
    _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = OpenCodeAdapter()
    with pytest.raises(RuntimeSpawnError, match="empty stdout"):
        asyncio.run(adapter.spawn_session("x", model="anthropic/claude-x"))


def test_spawn_session_timeout_kills_child_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """On timeout the child is killed and a typed timeout error is raised."""
    proc = _FakeProcess(stdout=_stream_bytes(), stderr=b"", returncode=0, hang=True)
    _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = OpenCodeAdapter()
    with pytest.raises(RuntimeSpawnError, match="timed out"):
        asyncio.run(adapter.spawn_session("x", model="anthropic/claude-x", timeout=0.01))
    assert proc.killed is True
    assert proc.waited is True
