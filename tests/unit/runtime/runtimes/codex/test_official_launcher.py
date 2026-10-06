"""The native Codex launcher drives its Run through the official Codex SDK.

Every launch case starts the real :class:`CodexNativeLauncher` against
``tests/fixtures/runtime/fake_codex_server.py`` installed as the ``codex``
binary, so the SDK's JSON-RPC client, the per-Run wrapper, the scrubbed
environment and the kill ladder all run for real while no real ``codex``
binary and no network is reached.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

import pytest
from openai_codex.generated.v2_all import AgentMessageThreadItem

from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.kernel.runtime.events import MessageSummaryPayload, TranscriptItem
from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.runtime.budget.service import DEFAULT_GRACE_SECONDS
from eawf.runtime.daemon.native_dispatch import CapsuleRequest, seal_capsule
from eawf.runtime.mcp.native_launch import MCP_ARTIFACT_DIRNAME
from eawf.runtime.runtimes import adapter as runtimes_adapter
from eawf.runtime.runtimes.adapter import (
    ConcurrentSpawnCapError,
    NativeLaunchOutcome,
    NativeLaunchRequest,
    RuntimeSpawnError,
    TurnOutcome,
    spawn_inflight,
)
from eawf.runtime.runtimes.codex import adapter as codex_adapter
from eawf.runtime.runtimes.codex.adapter import CodexAdapter, CodexNativeLauncher
from eawf.runtime.runtimes.codex.app_server import (
    APP_SERVER_ARGS,
    PGID_FILENAME,
    WRAPPER_CONFIG_FILENAME,
    WRAPPER_FILENAME,
    _message_payload,
    _user_agent_version,
    thread_config,
)
from eawf.runtime.runtimes.codex.mcp_config import SERVER_CONFIG_KEY
from eawf.runtime.runtimes.host_transcript import SUMMARY_LIMIT, WITHHELD_TEXT, SpokenMessage
from eawf.runtime.runtimes.metering import UsageSample
from eawf.runtime.sandbox.env_scrub import build_child_env, resolve_binary_dir
from eawf.workflow.runtime.compile import compile_run_spec
from tests import _provider_helpers as fx
from tests.fixtures.runtime.fake_codex_server import (
    THREAD_ID,
    TURN_ID,
    VERSION,
    agent_message,
    error_notice,
    install_fake_codex,
    token_usage,
    turn_completed,
)
from tests.integration.runtime.daemon.test_native_dispatch import capsule_request

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake server is a shebang script"
)

#: The token cap the budget case crosses.
_CAP_TOKENS: Final = 100

#: CoreFoundation seeds this into every darwin process itself, so the child
#: reports it whatever environment it was started with.
_PLATFORM_INJECTED: Final = frozenset({"__CF_USER_TEXT_ENCODING"})

#: The variables a leak would carry into the child if the spawn did not scrub.
_LEAKS: Final = {
    "AWS_SECRET_ACCESS_KEY": "leak",  # pragma: allowlist secret
    "EAWF_FIXTURE_LEAK": "leak",
}

#: The Run's server, as the codex lane names it in its config table.
_SERVER: Final = SERVER_CONFIG_KEY.split(".", 1)[1]

UsageSink = Callable[[UsageSample, int | None], Awaitable[bool]]


def _bypass_jail(
    argv: list[str], *, runtime: str, cwd: str | None, session: str = "", sink: object = None
) -> list[str]:
    """Leave the child unjailed so the fake runs on any host."""
    return argv


def _implement_spec() -> CompiledRunSpec:
    """Return a compiled codex spec for a mutating Task Run."""
    return compile_run_spec(
        fx.task_request(),
        configuration=fx.configuration(),
        bindings=[fx.binding()],
        compiled_at=fx.COMPILED_AT,
    )


def _review_spec() -> CompiledRunSpec:
    """Return a compiled codex spec for a read-only review Run."""
    return compile_run_spec(
        fx.review_request(),
        configuration=fx.configuration(),
        bindings=[fx.binding()],
        compiled_at=fx.COMPILED_AT,
    )


def _request(
    workspace: Path,
    *,
    spec: CompiledRunSpec | None = None,
    usage_sink: UsageSink | None = None,
    said: list[MessageSummaryPayload] | None = None,
) -> NativeLaunchRequest:
    """Return the launch request of one fake Run in *workspace*."""
    compiled = spec if spec is not None else _implement_spec()
    capsule = seal_capsule(
        spec=compiled,
        request=CapsuleRequest.model_validate(
            capsule_request(token_budget=_CAP_TOKENS, tool_grants=["budget_status"])
        ),
    )

    async def record(item: TranscriptItem) -> None:
        assert said is not None
        if isinstance(item, MessageSummaryPayload):
            said.append(item)

    return NativeLaunchRequest(
        spec=compiled,
        capsule=capsule,
        workspace_handle=f"wsh-{'a' * 32}",
        workspace=workspace,
        prompt="implement the fixture task",
        hello_sequence=1,
        usage_sink=usage_sink,
        message_sink=record if said is not None else None,
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return an empty workspace, with HOME pointed away from the operator's."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(codex_adapter, "_maybe_jail_argv", _bypass_jail)
    root = tmp_path / "ws"
    root.mkdir()
    return root


def _launcher(workspace: Path, script: dict[str, Any] | None = None) -> CodexNativeLauncher:
    """Return a launcher whose ``codex`` is the fake, installed in *workspace*."""
    adapter = CodexAdapter()
    adapter.cli_binary = str(install_fake_codex(workspace, script))
    return CodexNativeLauncher(adapter=adapter)


async def _launch_and_turn(
    launcher: CodexNativeLauncher, request: NativeLaunchRequest
) -> tuple[NativeLaunchOutcome, TurnOutcome]:
    outcome = await launcher.launch(request)
    assert outcome.turn is not None
    return outcome, await outcome.turn


def _run(
    workspace: Path, script: dict[str, Any] | None = None, **kwargs: Any
) -> tuple[NativeLaunchOutcome, TurnOutcome]:
    return asyncio.run(
        _launch_and_turn(_launcher(workspace, script), _request(workspace, **kwargs))
    )


def _received(workspace: Path) -> list[dict[str, Any]]:
    lines = (workspace / "received.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _thread_start(workspace: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((workspace / "thread-start.json").read_text("utf-8"))
    return loaded


def _observed(spec: CompiledRunSpec) -> RunRuntimeTuple:
    """Return the runtime the fake reports for a thread opened on *spec*."""
    return RunRuntimeTuple(
        harness="codex", harness_version=VERSION, provider="openai", model=spec.model_id
    )


def _fake_pgid(workspace: Path) -> int:
    return int((workspace / "fake.pgid").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# CR-1: one thread, the hello at its id, the turn resolving to TurnOutcome
# ---------------------------------------------------------------------------


def test_launch_opens_one_thread_and_the_turn_resolves_completed(workspace: Path) -> None:
    said: list[MessageSummaryPayload] = []
    spec = _implement_spec()

    outcome, turn = _run(workspace, spec=spec, said=said)

    assert outcome.provider_session_ref == THREAD_ID
    assert outcome.hello.provider_session_ref == THREAD_ID
    assert turn == TurnOutcome(
        exit_kind="completed", answer_text="done", runtime_tuple=_observed(spec)
    )
    assert [message.get("method") for message in _received(workspace)] == [
        "initialize",
        "initialized",
        "thread/start",
        "turn/start",
    ]
    params = _thread_start(workspace)
    assert params["cwd"] == str(workspace)
    assert params["model"] == spec.model_id
    assert params["approvalPolicy"] == "never"
    assert list(params["config"]["mcp_servers"]) == [_SERVER]
    assert "turn" not in outcome.model_dump()


def test_the_run_server_is_the_only_mcp_server(workspace: Path) -> None:
    _run(workspace)

    servers = _thread_start(workspace)["config"]["mcp_servers"]
    assert list(servers) == [_SERVER]
    assert servers[_SERVER]["enabled"] is True
    assert servers[_SERVER]["command"] == "eawf"
    assert servers[_SERVER]["args"][:3] == ["mcp", "serve", "--binding"]


def test_the_prompt_starts_the_turn_on_the_opened_thread(workspace: Path) -> None:
    _run(workspace)

    (start,) = [m for m in _received(workspace) if m.get("method") == "turn/start"]
    assert start["params"]["threadId"] == THREAD_ID
    assert start["params"]["input"] == [{"type": "text", "text": "implement the fixture task"}]


def test_no_exec_argv_is_built_for_a_native_run(workspace: Path) -> None:
    _run(workspace)

    argv = json.loads((workspace / "argv.json").read_text(encoding="utf-8"))
    assert argv[1:] == list(APP_SERVER_ARGS)
    assert "exec" not in argv


def test_the_sdk_starts_the_wrapper_which_execs_the_certified_codex(workspace: Path) -> None:
    launcher = _launcher(workspace)

    asyncio.run(_launch_and_turn(launcher, _request(workspace)))

    run_dir = workspace / MCP_ARTIFACT_DIRNAME
    config = json.loads((run_dir / WRAPPER_CONFIG_FILENAME).read_text(encoding="utf-8"))
    argv = json.loads((workspace / "argv.json").read_text(encoding="utf-8"))
    assert (run_dir / WRAPPER_FILENAME).stat().st_mode & 0o777 == 0o700
    assert config["argv"] == [launcher._adapter.cli_binary, *APP_SERVER_ARGS]
    assert argv[0] == launcher._adapter.cli_binary
    assert "codex_cli_bin" not in json.dumps(config)


def test_launch_returns_before_the_turn_ends(workspace: Path) -> None:
    async def scenario() -> bool:
        outcome = await _launcher(workspace, {"lines": []}).launch(_request(workspace))
        assert outcome.turn is not None
        running = not outcome.turn.done()
        outcome.turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outcome.turn
        return running

    assert asyncio.run(scenario())


def test_an_interrupted_turn_resolves_interrupted(workspace: Path) -> None:
    _, turn = _run(workspace, {"lines": [turn_completed("interrupted")]})

    assert turn.exit_kind == "interrupted"


def test_a_server_request_is_refused_and_the_turn_goes_on(workspace: Path) -> None:
    ask = {"id": 99, "method": "item/commandExecution/requestApproval", "params": {}}

    _, turn = _run(workspace, {"lines": [ask, turn_completed()]})

    (answer,) = [m for m in _received(workspace) if m.get("id") == 99]
    assert turn.exit_kind == "completed"
    assert answer["result"] == {"decision": "decline"}


# ---------------------------------------------------------------------------
# CR-2: jail, env scrub, pgid and sandbox mode
# ---------------------------------------------------------------------------


def test_the_child_env_is_the_scrub_allowlist(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in _LEAKS.items():
        monkeypatch.setenv(key, value)
    launcher = _launcher(workspace)
    cli = launcher._adapter.cli_binary
    allowlist = set(build_child_env("codex", extra_path_dir=resolve_binary_dir(cli)))

    asyncio.run(_launch_and_turn(launcher, _request(workspace)))

    env = json.loads((workspace / "env.json").read_text(encoding="utf-8"))
    assert set(env) - _PLATFORM_INJECTED <= allowlist
    assert not set(_LEAKS) & set(env)


def test_the_wrapper_config_names_keys_and_never_values(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fixture-value")  # pragma: allowlist secret
    launcher = _launcher(workspace)

    asyncio.run(_launch_and_turn(launcher, _request(workspace)))

    text = (workspace / MCP_ARTIFACT_DIRNAME / WRAPPER_CONFIG_FILENAME).read_text("utf-8")
    env = json.loads((workspace / "env.json").read_text(encoding="utf-8"))
    assert "OPENAI_API_KEY" in json.loads(text)["env_keys"]
    assert "sk-fixture-value" not in text
    assert env["OPENAI_API_KEY"] == "sk-fixture-value"  # pragma: allowlist secret


def test_the_worker_group_is_recorded(workspace: Path) -> None:
    outcome, _ = _run(workspace)

    recorded = (workspace / MCP_ARTIFACT_DIRNAME / PGID_FILENAME).read_text(encoding="utf-8")
    assert int(recorded) == _fake_pgid(workspace)
    assert outcome.subprocess_pid == _fake_pgid(workspace)
    assert _fake_pgid(workspace) != os.getpgid(0)


def test_win32_is_refused_before_anything_is_written(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _launcher(workspace)
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(RuntimeSpawnError, match="win32"):
        asyncio.run(launcher.launch(_request(workspace)))

    assert not (workspace / MCP_ARTIFACT_DIRNAME).exists()


@pytest.mark.parametrize(
    ("spec_factory", "mode", "sandbox"),
    [
        (_implement_spec, "workspace_write", "workspace-write"),
        (_review_spec, "read_only", "read-only"),
    ],
)
def test_the_thread_sandbox_follows_the_compiled_spec(
    workspace: Path, spec_factory: Callable[[], CompiledRunSpec], mode: str, sandbox: str
) -> None:
    spec = spec_factory()

    _run(workspace, spec=spec)

    assert spec.sandbox.mode == mode
    assert _thread_start(workspace)["sandbox"] == sandbox


def test_the_jail_wraps_the_app_server_argv(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jail = workspace / "fake-jail"
    jail.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(workspace / 'jail.json')!r}, 'w', encoding='utf-8') as handle:\n"
        "    json.dump(sys.argv, handle)\n"
        "os.execv(sys.argv[1], sys.argv[1:])\n",
        encoding="utf-8",
    )
    jail.chmod(0o700)
    calls: list[dict[str, Any]] = []

    def fake_jail(
        argv: list[str], *, runtime: str, cwd: str | None, session: str = "", sink: object = None
    ) -> list[str]:
        calls.append({"argv": list(argv), "runtime": runtime, "cwd": cwd})
        return [str(jail), *argv]

    monkeypatch.setattr(codex_adapter, "_maybe_jail_argv", fake_jail)
    launcher = _launcher(workspace)

    _, turn = asyncio.run(_launch_and_turn(launcher, _request(workspace)))

    cli = launcher._adapter.cli_binary
    assert turn.exit_kind == "completed"
    assert calls == [{"argv": [cli, *APP_SERVER_ARGS], "runtime": "codex", "cwd": str(workspace)}]
    assert json.loads((workspace / "jail.json").read_text(encoding="utf-8"))[1:] == [
        cli,
        *APP_SERVER_ARGS,
    ]


def test_under_the_outer_seatbelt_codex_sandboxing_is_deferred(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seatbelt(
        argv: list[str], *, runtime: str, cwd: str | None, session: str = "", sink: object = None
    ) -> list[str]:
        return codex_adapter._defer_to_outer_seatbelt(argv)

    monkeypatch.setattr(codex_adapter, "_maybe_jail_argv", seatbelt)

    _run(workspace)

    argv = json.loads((workspace / "argv.json").read_text(encoding="utf-8"))
    assert argv[1:4] == ["-c", codex_adapter.OUTER_SEATBELT_OVERRIDE, "app-server"]
    assert _thread_start(workspace)["sandbox"] == "danger-full-access"


def test_seatbelt_deferral_keeps_the_exec_form_for_exec() -> None:
    argv = ["codex", "exec", "--json", "prompt"]

    assert codex_adapter._defer_to_outer_seatbelt(argv) == [
        "codex",
        "exec",
        "--sandbox",
        "danger-full-access",
        "--json",
        "prompt",
    ]


def test_the_turn_holds_a_spawn_slot_until_it_ends(workspace: Path) -> None:
    before = spawn_inflight()

    async def scenario() -> tuple[int, int]:
        outcome = await _launcher(workspace).launch(_request(workspace))
        assert outcome.turn is not None
        during = spawn_inflight()
        await outcome.turn
        return during, spawn_inflight()

    assert asyncio.run(scenario()) == (before + 1, before)


def test_a_saturated_spawn_cap_refuses_before_the_fork(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtimes_adapter, "CONCURRENT_SPAWN_CAP", 0)

    with pytest.raises(ConcurrentSpawnCapError):
        asyncio.run(_launcher(workspace).launch(_request(workspace)))

    assert not (workspace / "argv.json").exists()


# ---------------------------------------------------------------------------
# CR-3: usage reaches usage_sink, agent messages reach message_sink
# ---------------------------------------------------------------------------


def test_usage_and_agent_messages_are_relayed(workspace: Path) -> None:
    script = {
        "lines": [
            token_usage(100, 50),
            token_usage(100, 50),
            agent_message("first answer"),
            token_usage(200, 150, cached=50),
            turn_completed(),
        ]
    }
    said: list[MessageSummaryPayload] = []
    seen: list[tuple[UsageSample, int | None]] = []

    async def sink(sample: UsageSample, pgid: int | None) -> bool:
        seen.append((sample, pgid))
        return False

    _, turn = _run(workspace, script, usage_sink=sink, said=said)

    pgid = _fake_pgid(workspace)
    assert turn.exit_kind == "completed"
    assert seen == [
        (UsageSample(input_tokens=100, output_tokens=50, cache_read_input_tokens=0), pgid),
        (UsageSample(input_tokens=150, output_tokens=150, cache_read_input_tokens=50), pgid),
    ]
    assert said == [
        SpokenMessage(message_role="assistant", summary="first answer", text="first answer")
    ]


def test_a_long_message_is_cut_and_a_path_is_withheld(workspace: Path) -> None:
    script = {
        "lines": [
            agent_message("x" * (SUMMARY_LIMIT + 1), item_id="a"),
            agent_message("wrote " + "/".join(("", "Users", "fixture", "notes.txt")), item_id="b"),
            turn_completed(),
        ]
    }
    said: list[MessageSummaryPayload] = []

    _run(workspace, script, said=said)

    assert [(m.summary, m.truncated) for m in said] == [
        ("x" * SUMMARY_LIMIT, True),
        (WITHHELD_TEXT, True),
    ]


@pytest.mark.parametrize(("text", "expected"), [("", None), ("   ", None), ("a", "a")])
def test_message_payload_boundaries(text: str, expected: str | None) -> None:
    payload = _message_payload(AgentMessageThreadItem(id="m", text=text, type="agentMessage"))

    assert (payload.summary if payload is not None else None) == expected


def test_a_message_of_exactly_the_limit_is_not_truncated() -> None:
    item = AgentMessageThreadItem(id="m", text="y" * SUMMARY_LIMIT, type="agentMessage")

    payload = _message_payload(item)

    assert payload is not None
    assert not payload.truncated


# ---------------------------------------------------------------------------
# CR-4: a budget crossing interrupts, then the kill ladder reaps the group
# ---------------------------------------------------------------------------


def test_budget_crossing_interrupts_reaps_and_resolves_budget_terminated(
    workspace: Path,
) -> None:
    seen: list[tuple[int, int | None]] = []

    async def sink(sample: UsageSample, pgid: int | None) -> bool:
        total = sample.input_tokens + sample.output_tokens
        seen.append((total, pgid))
        return total >= _CAP_TOKENS

    started = time.monotonic()
    outcome, turn = _run(workspace, {"lines": [token_usage(80, 40)]}, usage_sink=sink)
    elapsed = time.monotonic() - started

    pgid = _fake_pgid(workspace)
    (interrupt,) = [m for m in _received(workspace) if m.get("method") == "turn/interrupt"]
    assert turn == TurnOutcome(
        exit_kind="budget_terminated", runtime_tuple=_observed(_implement_spec())
    )
    assert seen == [(120, pgid)]
    assert interrupt["params"] == {"threadId": THREAD_ID, "turnId": TURN_ID}
    assert outcome.subprocess_pid == pgid
    assert elapsed < DEFAULT_GRACE_SECONDS
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_a_crossing_after_the_server_exited_does_not_wait_on_the_interrupt(
    workspace: Path,
) -> None:
    async def sink(sample: UsageSample, pgid: int | None) -> bool:
        return True

    started = time.monotonic()
    _, turn = _run(workspace, {"lines": [token_usage(80, 40)], "exit_code": 0}, usage_sink=sink)

    assert turn.exit_kind == "budget_terminated"
    assert time.monotonic() - started < DEFAULT_GRACE_SECONDS


def test_a_terminated_sink_is_not_handed_later_readings(workspace: Path) -> None:
    script = {"lines": [token_usage(80, 40), token_usage(90, 50)]}
    seen: list[int] = []

    async def sink(sample: UsageSample, pgid: int | None) -> bool:
        seen.append(sample.input_tokens + sample.output_tokens)
        return True

    _, turn = _run(workspace, script, usage_sink=sink)

    assert turn.exit_kind == "budget_terminated"
    assert seen == [120]


# ---------------------------------------------------------------------------
# Failures map onto the spawn error taxonomy, before and after the hello
# ---------------------------------------------------------------------------


def test_a_missing_binary_raises_127_and_frees_the_slot(workspace: Path) -> None:
    launcher = _launcher(workspace)
    launcher._adapter.cli_binary = str(workspace / "missing-codex")
    before = spawn_inflight()

    with pytest.raises(RuntimeSpawnError) as raised:
        asyncio.run(launcher.launch(_request(workspace)))

    assert raised.value.exit_status == 127
    assert spawn_inflight() == before


@pytest.mark.parametrize("method", ["initialize", "thread/start", "turn/start"])
def test_a_refused_request_before_the_hello_raises_a_classified_error(
    workspace: Path, method: str
) -> None:
    launcher = _launcher(workspace, {"refuse": {"method": method, "message": "401 unauthorized"}})
    before = spawn_inflight()

    with pytest.raises(RuntimeSpawnError, match=method) as raised:
        asyncio.run(launcher.launch(_request(workspace)))

    error = raised.value
    assert launcher._adapter.parse_error(error.exit_status or 1, error.stderr) == (
        "RUNTIME_AUTH_ERROR"
    )
    assert spawn_inflight() == before


_AFTER_HELLO: Final[dict[str, tuple[dict[str, Any], str, bool]]] = {
    "failed-turn": (
        {"lines": [error_notice("429 rate_limit"), turn_completed("failed", "429 rate_limit")]},
        "RUNTIME_RATE_LIMIT",
        True,
    ),
    "failed-turn-without-error": (
        {"lines": [error_notice("401 unauthorized"), turn_completed("failed")]},
        "RUNTIME_AUTH_ERROR",
        True,
    ),
    "process-exit": (
        {"lines": [], "stderr": "HTTP 401 unauthorized", "exit_code": 1},
        "RUNTIME_AUTH_ERROR",
        False,
    ),
    "not-json": ({"lines": ["{not json"]}, "RUNTIME_API_ERROR", False),
}


@pytest.mark.parametrize("case", sorted(_AFTER_HELLO))
def test_failures_after_the_hello_resolve_the_turn_failed(workspace: Path, case: str) -> None:
    script, error_class, result_is_error = _AFTER_HELLO[case]

    outcome, turn = _run(workspace, script)

    assert outcome.provider_session_ref == THREAD_ID
    assert turn.exit_kind == "failed"
    assert turn.error_class == error_class
    assert turn.result_is_error is result_is_error
    assert turn.error_text


def test_a_malformed_completion_fails_the_turn(workspace: Path) -> None:
    malformed = {
        "method": "turn/completed",
        "params": {"threadId": THREAD_ID, "turnId": TURN_ID, "turn": {"id": TURN_ID}},
    }

    _, turn = _run(workspace, {"lines": [malformed]})

    assert turn.exit_kind == "failed"
    assert turn.error_text is not None
    assert "out of protocol" in turn.error_text


def test_a_retried_error_does_not_fail_the_turn(workspace: Path) -> None:
    script = {"lines": [error_notice("stream reset", will_retry=True), turn_completed()]}

    _, turn = _run(workspace, script)

    assert turn == TurnOutcome(exit_kind="completed", runtime_tuple=_observed(_implement_spec()))


# ---------------------------------------------------------------------------
# The lane flags translate to a thread config, at their boundaries
# ---------------------------------------------------------------------------


def test_thread_config_of_no_flags_is_empty() -> None:
    assert thread_config(()) == {}


def test_thread_config_of_one_override_is_one_key() -> None:
    assert thread_config(("-c", "tools.websearch=false")) == {"tools.websearch": False}


def test_thread_config_folds_the_server_keys_into_one_table() -> None:
    flags = (
        "-c",
        f'{SERVER_CONFIG_KEY}.command="eawf"',
        "-c",
        f'{SERVER_CONFIG_KEY}.args=["mcp", "serve"]',
        "-c",
        f"{SERVER_CONFIG_KEY}.enabled=true",
        "-c",
        "tools.websearch=false",
    )

    assert thread_config(flags) == {
        "mcp_servers": {_SERVER: {"command": "eawf", "args": ["mcp", "serve"], "enabled": True}},
        "tools.websearch": False,
    }


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (("-c",), "are not -c pairs"),
        (("--model", "x=1"), "is not a -c override"),
        (("-c", "novalue"), "is not a -c override"),
        (("-c", "key=not toml ["), "is not TOML"),
    ],
)
def test_thread_config_refuses_what_it_cannot_express(flags: tuple[str, ...], message: str) -> None:
    with pytest.raises(RuntimeSpawnError, match=message):
        thread_config(flags)


# ---------------------------------------------------------------------------
# The turn carries the final answer and the runtime the server reported
# ---------------------------------------------------------------------------


def test_a_completed_turn_carries_the_whole_unscrubbed_final_answer(workspace: Path) -> None:
    path = "/".join(("", "Users", "fixture", "report.md"))
    answer = f"wrote {path}\n" + "x" * (SUMMARY_LIMIT + 1)
    script = {
        "lines": [
            agent_message("looking", item_id="a", phase="commentary"),
            agent_message(answer, item_id="b", phase="final_answer"),
            agent_message("one more note", item_id="c", phase="commentary"),
            turn_completed(),
        ]
    }
    said: list[MessageSummaryPayload] = []

    _, turn = _run(workspace, script, said=said)

    assert turn.exit_kind == "completed"
    assert turn.answer_text == answer
    assert [m.summary for m in said] == ["looking", WITHHELD_TEXT, "one more note"]


def test_the_last_unphased_message_is_the_answer(workspace: Path) -> None:
    script = {
        "lines": [agent_message("first", item_id="a"), agent_message("", "b"), turn_completed()]
    }

    _, turn = _run(workspace, script)

    assert turn.answer_text == ""


def test_a_turn_of_only_commentary_carries_no_answer(workspace: Path) -> None:
    script = {"lines": [agent_message("musing", phase="commentary"), turn_completed()]}

    _, turn = _run(workspace, script)

    assert turn.exit_kind == "completed"
    assert turn.answer_text is None


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_a_turn_that_did_not_complete_carries_no_answer(workspace: Path, status: str) -> None:
    script = {"lines": [agent_message("partial"), turn_completed(status, error="boom")]}

    _, turn = _run(workspace, script)

    assert turn.exit_kind == status
    assert turn.answer_text is None


def test_runtime_tuple_is_read_from_the_initialize_user_agent_and_named_by_the_hello(
    workspace: Path,
) -> None:
    spec = _implement_spec()

    outcome, turn = _run(workspace, spec=spec)

    assert turn.runtime_tuple == _observed(spec)
    assert outcome.hello.sdk_version == VERSION


def test_a_failed_turn_still_carries_the_runtime_tuple(workspace: Path) -> None:
    _, turn = _run(workspace, {"lines": [turn_completed("failed", error="boom")]})

    assert turn.exit_kind == "failed"
    assert turn.runtime_tuple == _observed(_implement_spec())


def test_runtime_tuple_without_a_version_falls_back_to_the_driver_in_the_hello(
    workspace: Path,
) -> None:
    launcher = _launcher(workspace, {"user_agent": "codex", "lines": [turn_completed()]})

    outcome, turn = asyncio.run(_launch_and_turn(launcher, _request(workspace)))

    assert turn.runtime_tuple is not None
    assert turn.runtime_tuple.harness_version is None
    assert outcome.hello.sdk_version == launcher.driver_version


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        (None, None),
        ("", None),
        ("codex", None),
        ("codex_cli_rs/", None),
        ("codex_cli_rs/0.159.2", "0.159.2"),
        ("  codex_cli_rs/0.159.2 (Mac OS 15.5.0; arm64) unknown (eawf; 1.0.0)", "0.159.2"),
    ],
)
def test_user_agent_version_reads_the_token_after_the_first_slash(
    user_agent: str | None, expected: str | None
) -> None:
    assert _user_agent_version(user_agent) == expected
