"""Unit tests for the live ``codex exec`` spawn + result parse (P29-I04-W05).

Pins the codex juror lane (G1): :meth:`CodexAdapter.spawn_session` forks
``codex exec --json`` (headless, newline-delimited JSON events) and
:func:`_parse_codex_result` parses that event stream into a typed
:class:`~eawf.runtime.runtimes.adapter.SpawnResult`.

The subprocess is ALWAYS mocked -- these tests never spawn a real
``codex`` process (no network / auth / cost). The well-formed parse is
exercised against a fixed event-stream string captured from a live
``codex exec --json`` probe; the spawn-seam tests patch
:func:`asyncio.create_subprocess_exec` with a fake process so the argv
construction + timeout + pid callback are observable without a live CLI.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.runtime.runtimes.adapter import RuntimeSpawnError, SpawnResult
from eawf.runtime.runtimes.codex import adapter as codex_adapter
from eawf.runtime.runtimes.codex.adapter import (
    _REASONING_EFFORT_CONFIG_KEY,
    CodexAdapter,
    _parse_codex_result,
)

# ---------------------------------------------------------------------------
# Fixtures: a well-formed ``codex exec --json`` event stream
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC)

#: A representative successful event stream from a live ``codex exec --json``
#: probe (codex 0.134.0): thread.started -> turn.started -> item.completed
#: (agent_message) -> turn.completed (usage). Codex emits these as
#: newline-delimited JSON objects, one event per line.
_WELL_FORMED_EVENTS: list[dict[str, object]] = [
    {"type": "thread.started", "thread_id": "019e85f5-786a-75f0-a0e2-dbc546cdfb3b"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "agent_message", "text": "the answer text"},
    },
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 27243,
            "cached_input_tokens": 4480,
            "output_tokens": 17,
            "reasoning_output_tokens": 10,
        },
    },
]


def _events_bytes(events: list[dict[str, object]] | None = None) -> bytes:
    """Serialise an event list to a newline-delimited JSON byte stream."""
    rows = _WELL_FORMED_EVENTS if events is None else events
    return ("\n".join(json.dumps(row) for row in rows)).encode("utf-8")


# ---------------------------------------------------------------------------
# Fake subprocess (NEVER a real ``codex`` process)
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

    monkeypatch.setattr(codex_adapter.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(
        codex_adapter,
        "_maybe_jail_argv",
        lambda argv, *, runtime, cwd, session="", sink=None: argv,
    )
    return calls


# ---------------------------------------------------------------------------
# _parse_codex_result -- well-formed event stream
# ---------------------------------------------------------------------------


def test_parse_codex_result_well_formed_stream_parses() -> None:
    """A well-formed event stream parses every captured field into the result."""
    result = _parse_codex_result(
        runtime="codex",
        model="gpt-5.5",
        stdout=_events_bytes(),
        stderr=b"",
        exit_status=0,
        subprocess_pid=4321,
        started_at=_T0,
        ended_at=_T1,
    )
    assert isinstance(result, SpawnResult)
    assert result.session_id == "019e85f5-786a-75f0-a0e2-dbc546cdfb3b"
    assert result.runtime == "codex"
    assert result.model == "gpt-5.5"
    # Codex does not disclose a billed-model alias.
    assert result.resolved_model is None
    assert result.subprocess_pid == 4321
    assert result.exit_status == 0
    assert result.text == "the answer text"
    # Codex input_tokens is GROSS (27243) and includes the 4480 cached
    # tokens; the result splits them so they never double-count.
    assert result.input_tokens == 27243 - 4480
    assert result.cache_read_input_tokens == 4480
    assert result.output_tokens == 17
    # Codex reports no cache-creation tokens and no self-reported cost.
    assert result.cache_creation_input_tokens == 0
    assert result.cache_creation_5m_input_tokens == 0
    assert result.cache_creation_1h_input_tokens == 0
    assert result.cost_usd_reported is None
    assert result.started_at == _T0
    assert result.ended_at == _T1


def test_parse_codex_result_last_agent_message_wins() -> None:
    """A multi-message turn resolves to the FINAL agent_message text."""
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
    ]
    result = _parse_codex_result(
        runtime="codex",
        model="gpt-5.5",
        stdout=_events_bytes(events),
        stderr=b"",
        exit_status=0,
        subprocess_pid=7,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.text == "final"


def test_parse_codex_result_no_thread_id_falls_back_to_synthetic_session() -> None:
    """No thread.started id falls back to a synthetic runtime-pid session id."""
    events: list[dict[str, object]] = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
        {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 1}},
    ]
    result = _parse_codex_result(
        runtime="codex",
        model="gpt-5.5",
        stdout=_events_bytes(events),
        stderr=b"",
        exit_status=0,
        subprocess_pid=99,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.session_id == "codex-99"


def test_parse_codex_result_missing_usage_defaults_to_zero() -> None:
    """A stream with no turn.completed usage zeroes the token counts."""
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
    ]
    result = _parse_codex_result(
        runtime="codex",
        model="gpt-5.5",
        stdout=_events_bytes(events),
        stderr=b"",
        exit_status=0,
        subprocess_pid=1,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cache_read_input_tokens == 0


def test_parse_codex_result_empty_text_item_becomes_empty_string() -> None:
    """An agent_message with null text yields empty text, not the string 'None'."""
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": None}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    result = _parse_codex_result(
        runtime="codex",
        model="gpt-5.5",
        stdout=_events_bytes(events),
        stderr=b"",
        exit_status=0,
        subprocess_pid=1,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.text == ""


def test_parse_codex_result_cached_exceeds_total_clamps_input_at_zero() -> None:
    """A malformed usage where cached > total clamps non-cached input at 0."""
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "x"}},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 99, "output_tokens": 1},
        },
    ]
    result = _parse_codex_result(
        runtime="codex",
        model="gpt-5.5",
        stdout=_events_bytes(events),
        stderr=b"",
        exit_status=0,
        subprocess_pid=1,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.input_tokens == 0
    assert result.cache_read_input_tokens == 99


# ---------------------------------------------------------------------------
# _parse_codex_result -- error / malformed paths (fail-fast, no swallowing)
# ---------------------------------------------------------------------------


def test_parse_codex_result_nonzero_exit_raises_with_stderr_snippet() -> None:
    """A non-zero exit raises and surfaces a stderr snippet."""
    with pytest.raises(RuntimeSpawnError, match="exited nonzero"):
        _parse_codex_result(
            runtime="codex",
            model="gpt-5.5",
            stdout=b"",
            stderr=b"boom: auth failed",
            exit_status=1,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_codex_result_empty_stdout_raises() -> None:
    """Empty stdout on a clean exit is a spawn error, not an empty result."""
    with pytest.raises(RuntimeSpawnError, match="empty stdout"):
        _parse_codex_result(
            runtime="codex",
            model="gpt-5.5",
            stdout=b"   \n",
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_codex_result_no_json_event_raises() -> None:
    """Stdout with no parseable JSON event line raises a typed error."""
    with pytest.raises(RuntimeSpawnError, match="no parseable json event"):
        _parse_codex_result(
            runtime="codex",
            model="gpt-5.5",
            stdout=b"not json at all\nstill not json",
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_codex_result_no_agent_message_raises() -> None:
    """A stream that never carries an agent_message item raises."""
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 0}},
    ]
    with pytest.raises(RuntimeSpawnError, match="no agent_message item"):
        _parse_codex_result(
            runtime="codex",
            model="gpt-5.5",
            stdout=_events_bytes(events),
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_codex_result_error_event_raises() -> None:
    """An ``error`` event raises even on a zero exit code."""
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "error", "message": "400 invalid_request_error"},
    ]
    with pytest.raises(RuntimeSpawnError, match="reported an error event"):
        _parse_codex_result(
            runtime="codex",
            model="gpt-5.5",
            stdout=_events_bytes(events),
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


def test_parse_codex_result_turn_failed_event_raises() -> None:
    """A ``turn.failed`` event raises even on a zero exit code."""
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.failed", "error": {"message": "model not supported"}},
    ]
    with pytest.raises(RuntimeSpawnError, match="turn failed"):
        _parse_codex_result(
            runtime="codex",
            model="gpt-5.5",
            stdout=_events_bytes(events),
            stderr=b"",
            exit_status=0,
            subprocess_pid=1,
            started_at=_T0,
            ended_at=_T1,
        )


# ---------------------------------------------------------------------------
# spawn_session -- forks ``codex exec`` (mocked subprocess)
# ---------------------------------------------------------------------------


def test_spawn_session_forks_codex_exec_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """spawn_session forks ``codex exec --json ...`` and parses the stream."""
    proc = _FakeProcess(stdout=_events_bytes(), stderr=b"", returncode=0, pid=5555)
    calls = _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = CodexAdapter()
    result = asyncio.run(adapter.spawn_session("solve it", model="gpt-5.5"))

    # Exactly one subprocess was forked with the expected ``codex exec`` argv.
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert "--json" in argv
    assert "-m" in argv
    assert argv[argv.index("-m") + 1] == "gpt-5.5"
    # The prompt is the trailing positional (codex exec [OPTIONS] [PROMPT]).
    assert argv[-1] == "solve it"

    assert result.session_id == "019e85f5-786a-75f0-a0e2-dbc546cdfb3b"
    assert result.subprocess_pid == 5555
    assert result.text == "the answer text"


def test_spawn_session_appends_extra_args_before_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """extra_args (codex options) are appended before the trailing prompt positional."""
    proc = _FakeProcess(stdout=_events_bytes(), stderr=b"", returncode=0)
    calls = _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = CodexAdapter()
    asyncio.run(
        adapter.spawn_session(
            "do it",
            model="gpt-5.5",
            extra_args=("--ephemeral",),
        )
    )
    argv = calls[0]
    # The option precedes the prompt; the prompt stays the final positional.
    assert "--ephemeral" in argv
    assert argv.index("--ephemeral") < argv.index("do it")
    assert argv[-1] == "do it"


def test_spawn_session_reasoning_effort_passes_via_config_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reasoning-effort -c override flows through extra_args into the argv."""
    proc = _FakeProcess(stdout=_events_bytes(), stderr=b"", returncode=0)
    calls = _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = CodexAdapter()
    override = f'{_REASONING_EFFORT_CONFIG_KEY}="low"'
    asyncio.run(
        adapter.spawn_session(
            "do it",
            model="gpt-5.5",
            extra_args=("-c", override),
        )
    )
    argv = calls[0]
    assert "-c" in argv
    assert argv[argv.index("-c") + 1] == override
    # The confirmed dotted key is the codex reasoning-effort config path.
    assert _REASONING_EFFORT_CONFIG_KEY == "model_reasoning_effort"


def _codex_tool_grants(argv: list[str]) -> dict[str, bool]:
    """Extract the ``-c tools.<name>=<bool>`` grant map from a codex argv.

    Walks the argv for ``-c`` flags whose value targets the ``tools.``
    config namespace and decodes the TOML-ish boolean tail into a
    ``{tool_name: granted}`` map. The tool name is the lowercased segment
    after ``tools.`` (codex's per-tool config convention). A test asserts
    membership / grant value against this map so the inverted allowlist is
    checked structurally rather than by argv string matching.

    Args:
        argv: The captured codex spawn argv.

    Returns:
        The ``{tool: True|False}`` grant map parsed from the ``tools.*``
        overrides.
    """
    grants: dict[str, bool] = {}
    for index, token in enumerate(argv):
        if token != "-c" or index + 1 >= len(argv):
            continue
        key_value = argv[index + 1]
        if not key_value.startswith("tools."):
            continue
        key, _, value = key_value.partition("=")
        grants[key.removeprefix("tools.")] = value.strip().lower() == "true"
    return grants


def test_spawn_session_denied_tools_emits_inverted_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty deny-list emits the inverted allowlist on the codex argv.

    Wave criterion 1: the allow flag (codex ``-c tools.<name>=true``) is
    present AND the denied tool is absent from the allow set while the other
    universe tools are granted.
    """
    proc = _FakeProcess(stdout=_events_bytes(), stderr=b"", returncode=0)
    calls = _patch_factory(monkeypatch, proc)

    import asyncio

    from eawf.runtime.sandbox.policy import invert_deny_to_allow

    adapter = CodexAdapter()
    denied = ["Edit", "Bash"]
    asyncio.run(adapter.spawn_session("x", model="gpt-5.5", denied_tools=denied))
    argv = calls[0]

    # The codex per-tool grant surface (``-c tools.<name>=...``) is present.
    assert "-c" in argv
    grants = _codex_tool_grants(argv)
    assert grants, "expected -c tools.* grant overrides on the argv"

    # The inverted allowlist (universe minus denied) is granted ``true``.
    expected_allow = {tool.lower() for tool in invert_deny_to_allow(denied)}
    granted_true = {tool for tool, granted in grants.items() if granted}
    assert granted_true == expected_allow

    # The denied tools are NOT in the allow (``true``) set, and other universe
    # tools (e.g. Read) ARE granted.
    assert "edit" not in granted_true
    assert "bash" not in granted_true
    assert "read" in granted_true


def test_spawn_session_denied_tool_absent_from_effective_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied tool is absent from the codex child's effective tool grant.

    Wave criterion 2: a deny must not silently pass through. The denied tool
    is both absent from the ``true`` grant set AND pinned ``false`` so it can
    never reach the child's effective grant even under a default-allow codex.
    """
    proc = _FakeProcess(stdout=_events_bytes(), stderr=b"", returncode=0)
    calls = _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = CodexAdapter()
    asyncio.run(adapter.spawn_session("x", model="gpt-5.5", denied_tools=["Edit"]))
    grants = _codex_tool_grants(calls[0])

    # The denied tool appears in the grant map pinned ``false`` -- explicitly
    # disabled, never granted -- so the deny cannot pass through.
    assert grants.get("edit") is False
    # No tool is both granted and denied (the deny is unambiguous).
    assert grants["edit"] is False
    # The legacy claude/no-op deny surfaces never appear on the codex argv.
    assert "--disallowedTools" not in calls[0]


def test_spawn_session_denied_tools_empty_is_byte_equivalent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty deny-list is byte-identical to the default (no deny handling)."""
    proc = _FakeProcess(stdout=_events_bytes(), stderr=b"", returncode=0)
    calls = _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = CodexAdapter()
    asyncio.run(adapter.spawn_session("x", model="gpt-5.5", denied_tools=()))
    asyncio.run(adapter.spawn_session("x", model="gpt-5.5"))

    assert calls[0] == calls[1]


def test_spawn_session_scrubs_child_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawn passes a SCRUBBED child env, not the full parent env."""
    proc = _FakeProcess(stdout=_events_bytes(), stderr=b"", returncode=0)
    seen_env: list[dict[str, str] | None] = []

    async def _fake_exec(*_argv: str, **kwargs: object) -> _FakeProcess:
        env_kwarg = kwargs.get("env")
        seen_env.append(env_kwarg if env_kwarg is None else dict(env_kwarg))  # type: ignore[arg-type]
        return proc

    monkeypatch.setattr(codex_adapter.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(
        codex_adapter,
        "_maybe_jail_argv",
        lambda argv, *, runtime, cwd, session="", sink=None: argv,
    )
    # Plant a sensitive var in the parent env; the scrubbed child must not carry it.
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-leak")

    import asyncio

    adapter = CodexAdapter()
    asyncio.run(adapter.spawn_session("x", model="gpt-5.5"))

    assert len(seen_env) == 1
    child_env = seen_env[0]
    assert child_env is not None
    assert "AWS_SECRET_ACCESS_KEY" not in child_env


def test_spawn_session_jail_wrap_applied_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The argv handed to the subprocess is the jail-wrapped form when jailed."""
    proc = _FakeProcess(stdout=_events_bytes(), stderr=b"", returncode=0)
    calls: list[list[str]] = []

    async def _fake_exec(*argv: str, **_kwargs: object) -> _FakeProcess:
        calls.append(list(argv))
        return proc

    seen_by_jail: list[list[str]] = []

    def _recording_jail(
        argv: list[str],
        *,
        runtime: str,
        cwd: str | None,
        session: str = "",
        sink: object | None = None,
    ) -> list[str]:
        seen_by_jail.append(list(argv))
        return ["JAIL", *argv]

    monkeypatch.setattr(codex_adapter.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(codex_adapter, "_maybe_jail_argv", _recording_jail)

    import asyncio

    adapter = CodexAdapter()
    asyncio.run(adapter.spawn_session("x", model="gpt-5.5"))

    assert len(seen_by_jail) == 1
    # The codex argv (binary first) is what the jail seam wraps.
    assert seen_by_jail[0][0] == "codex"
    # The spawned argv is the jail-wrapped form of that same argv.
    assert calls[0][0] == "JAIL"
    assert calls[0][1:] == seen_by_jail[0]


def test_spawn_session_invokes_on_spawn_with_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_spawn fires with the child pid before output is collected."""
    proc = _FakeProcess(stdout=_events_bytes(), stderr=b"", returncode=0, pid=7777)
    _patch_factory(monkeypatch, proc)
    seen: list[int] = []

    import asyncio

    adapter = CodexAdapter()
    asyncio.run(adapter.spawn_session("x", model="gpt-5.5", on_spawn=seen.append))
    assert seen == [7777]


def test_spawn_session_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero subprocess exit surfaces as RuntimeSpawnError."""
    proc = _FakeProcess(stdout=b"", stderr=b"500 internal_server_error", returncode=1)
    _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = CodexAdapter()
    with pytest.raises(RuntimeSpawnError, match="exited nonzero"):
        asyncio.run(adapter.spawn_session("x", model="gpt-5.5"))


def test_spawn_session_empty_output_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty stdout on a clean exit raises rather than yielding a blank result."""
    proc = _FakeProcess(stdout=b"", stderr=b"", returncode=0)
    _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = CodexAdapter()
    with pytest.raises(RuntimeSpawnError, match="empty stdout"):
        asyncio.run(adapter.spawn_session("x", model="gpt-5.5"))


def test_spawn_session_timeout_kills_child_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """On timeout the child is killed and a typed timeout error is raised."""
    proc = _FakeProcess(stdout=_events_bytes(), stderr=b"", returncode=0, hang=True)
    _patch_factory(monkeypatch, proc)

    import asyncio

    adapter = CodexAdapter()
    with pytest.raises(RuntimeSpawnError, match="timed out"):
        asyncio.run(adapter.spawn_session("x", model="gpt-5.5", timeout=0.01))
    assert proc.killed is True
    assert proc.waited is True


# ---------------------------------------------------------------------------
# SpawnResult model -- schema-mismatch guard (codex-side smoke)
# ---------------------------------------------------------------------------


def test_spawn_result_rejects_negative_token_count() -> None:
    """Token counts are ge=0 -- the codex parser must never emit a negative."""
    with pytest.raises(ValidationError):
        SpawnResult(
            session_id="s1",
            runtime="codex",
            model="gpt-5.5",
            subprocess_pid=1,
            exit_status=0,
            text="",
            input_tokens=-1,
            started_at=_T0,
            ended_at=_T1,
        )
