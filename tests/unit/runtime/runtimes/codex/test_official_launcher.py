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
import shutil
import sys
import threading
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

import pytest
from openai_codex.client import CodexClient
from openai_codex.generated.v2_all import AgentMessageThreadItem

from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.kernel.runtime.events import MessageSummaryPayload, TranscriptItem
from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.runtime.budget.service import TerminationResult
from eawf.runtime.daemon.native_dispatch import CapsuleRequest, seal_capsule
from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.runtime.mcp.native_launch import MCP_ARTIFACT_DIRNAME, RUN_TMP_DIRNAME, RUN_TMP_ENV_KEYS
from eawf.runtime.runtimes import adapter as runtimes_adapter
from eawf.runtime.runtimes import cancel
from eawf.runtime.runtimes.adapter import (
    ConcurrentSpawnCapError,
    NativeLaunchOutcome,
    NativeLaunchRequest,
    RuntimeSpawnError,
    TurnOutcome,
    spawn_inflight,
)
from eawf.runtime.runtimes.codex import adapter as codex_adapter
from eawf.runtime.runtimes.codex import app_server
from eawf.runtime.runtimes.codex.adapter import CodexAdapter, CodexNativeLauncher
from eawf.runtime.runtimes.codex.app_server import (
    APP_SERVER_ARGS,
    CODEX_AUTH_FILENAME,
    CODEX_CONFIG_FILENAME,
    CODEX_HOME_DIRNAME,
    PGID_FILENAME,
    WRAPPER_CONFIG_FILENAME,
    WRAPPER_FILENAME,
    _message_payload,
    _toml_document,
    _user_agent_version,
    codex_home_config,
)
from eawf.runtime.runtimes.codex.mcp_config import SERVER_CONFIG_KEY
from eawf.runtime.runtimes.host_transcript import SUMMARY_LIMIT, WITHHELD_TEXT, SpokenMessage
from eawf.runtime.runtimes.metering import UsageSample
from eawf.runtime.sandbox.env_scrub import build_child_env, resolve_binary_dir
from eawf.runtime.sandbox.policy import TOOL_UNIVERSE
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


#: The jail as production builds it, kept before the fixture bypasses it.
_REAL_JAIL: Final = codex_adapter._maybe_jail_argv

#: The default turn, for a script that only adds a key to it.
_DEFAULT_LINES: Final = {"lines": [agent_message("done"), token_usage(3, 1), turn_completed()]}


def _bypass_jail(
    argv: list[str],
    *,
    runtime: str,
    cwd: str | None,
    session: str = "",
    sink: object = None,
    private_home: object = None,
    storage_dirs: object = (),
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
        repo_root=workspace.parent,
        prompt="implement the fixture task",
        hello_sequence=1,
        usage_sink=usage_sink,
        message_sink=record if said is not None else None,
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return an empty workspace, with HOME pointed away from the operator's."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
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


def _home(workspace: Path) -> Path:
    """Return the Run's private ``CODEX_HOME``."""
    return workspace / MCP_ARTIFACT_DIRNAME / CODEX_HOME_DIRNAME


def _home_config(workspace: Path) -> dict[str, Any]:
    """Return the configuration the private home hands codex, parsed."""
    return tomllib.loads((_home(workspace) / CODEX_CONFIG_FILENAME).read_text("utf-8"))


def _operator_codex(home: Path) -> Path:
    """Write an operator ``~/.codex`` carrying plugins, a marketplace and an MCP server."""
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / CODEX_AUTH_FILENAME).write_text('{"auth_mode": "chatgpt"}', encoding="utf-8")
    (codex / CODEX_CONFIG_FILENAME).write_text(
        '[plugins."github@openai-curated"]\nenabled = true\n\n'
        '[marketplaces.operator]\nsource = "git"\n\n'
        '[mcp_servers.operator_server]\ncommand = "operator-mcp"\n',
        encoding="utf-8",
    )
    (codex / "history.jsonl").write_text("{}\n", encoding="utf-8")
    (codex / "AGENTS.md").write_text("operator instructions\n", encoding="utf-8")
    return codex


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
    assert outcome.transcript_path == f"{workspace}/rollout-{THREAD_ID}.jsonl"
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
    assert params.get("config") is None
    assert list(_home_config(workspace)["mcp_servers"]) == [_SERVER]
    assert "turn" not in outcome.model_dump()


def test_the_run_server_is_the_only_mcp_server(workspace: Path) -> None:
    _operator_codex(workspace.parent / "home")

    _run(workspace)

    servers = _home_config(workspace)["mcp_servers"]
    assert list(servers) == [_SERVER]
    assert servers[_SERVER]["enabled"] is True
    assert servers[_SERVER]["command"] == sys.executable
    assert servers[_SERVER]["args"][:6] == ["-I", "-m", "eawf", "mcp", "serve", "--binding"]


def test_codex_semantic_tools_repro_approval_rejected(workspace: Path) -> None:
    # Under approval_policy "never" codex refuses an MCP call it would prompt
    # for, so the run server's tools must be approved up front.
    _run(workspace)

    servers = _home_config(workspace)["mcp_servers"]
    assert servers[_SERVER]["default_tools_approval_mode"] == "approve"


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
        # Let the turn start: a task cancelled before its first step never runs
        # the finally that frees its spawn slot, which would leak to later tests.
        await asyncio.sleep(0)
        outcome.turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outcome.turn
        return running

    before = spawn_inflight()
    assert asyncio.run(scenario())
    assert spawn_inflight() == before


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
    # The private home and the Run's own temp directory are what the launcher
    # adds to what the scrub admits.
    allowlist = {
        "CODEX_HOME",
        *RUN_TMP_ENV_KEYS,
        *build_child_env("codex", extra_path_dir=resolve_binary_dir(cli)),
    }

    asyncio.run(_launch_and_turn(launcher, _request(workspace)))

    env = json.loads((workspace / "env.json").read_text(encoding="utf-8"))
    assert set(env) - _PLATFORM_INJECTED <= allowlist
    assert not set(_LEAKS) & set(env)
    run_tmp = workspace / MCP_ARTIFACT_DIRNAME / RUN_TMP_DIRNAME
    assert {env[key] for key in RUN_TMP_ENV_KEYS} == {str(run_tmp)}


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
    assert _home_config(workspace)["sandbox_mode"] == sandbox


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
        argv: list[str],
        *,
        runtime: str,
        cwd: str | None,
        session: str = "",
        sink: object = None,
        private_home: Path | None = None,
        storage_dirs: tuple[Path, ...] = (),
    ) -> list[str]:
        calls.append(
            {
                "argv": list(argv),
                "runtime": runtime,
                "cwd": cwd,
                "private_home": private_home,
                "storage_dirs": storage_dirs,
            }
        )
        return [str(jail), *argv]

    monkeypatch.setattr(codex_adapter, "_maybe_jail_argv", fake_jail)
    launcher = _launcher(workspace)

    _, turn = asyncio.run(_launch_and_turn(launcher, _request(workspace)))

    cli = launcher._adapter.cli_binary
    assert turn.exit_kind == "completed"
    assert calls == [
        {
            "argv": [cli, *APP_SERVER_ARGS],
            "runtime": "codex",
            "cwd": str(workspace),
            "private_home": _home(workspace),
            # The daemon's state and write-ahead log are closed to the Run.
            "storage_dirs": (workspace.parent / ".ea", runtime_dir() / "wal"),
        }
    ]
    assert json.loads((workspace / "jail.json").read_text(encoding="utf-8"))[1:] == [
        cli,
        *APP_SERVER_ARGS,
    ]


def test_under_the_outer_seatbelt_codex_sandboxing_is_deferred(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seatbelt(
        argv: list[str],
        *,
        runtime: str,
        cwd: str | None,
        session: str = "",
        sink: object = None,
        private_home: object = None,
        storage_dirs: object = (),
    ) -> list[str]:
        return codex_adapter._defer_to_outer_seatbelt(argv)

    monkeypatch.setattr(codex_adapter, "_maybe_jail_argv", seatbelt)

    _run(workspace)

    argv = json.loads((workspace / "argv.json").read_text(encoding="utf-8"))
    assert argv[1:4] == ["-c", codex_adapter.OUTER_SEATBELT_OVERRIDE, "app-server"]
    assert _thread_start(workspace)["sandbox"] == "danger-full-access"
    assert _home_config(workspace)["sandbox_mode"] == "danger-full-access"


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="the managed-preference sync is a macOS seatbelt behaviour",
)
def test_the_seatbelt_jailed_server_can_sync_codex_managed_preferences(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real jail confines the child to its repository root; the workspace stands in.
    monkeypatch.setattr(codex_adapter, "_repo_root_for", lambda path: workspace)
    monkeypatch.setattr(codex_adapter, "_maybe_jail_argv", _REAL_JAIL)

    _, turn = _run(workspace, {**_DEFAULT_LINES, "sync_managed_preferences": True})

    run_dir = workspace / MCP_ARTIFACT_DIRNAME
    argv = json.loads((run_dir / WRAPPER_CONFIG_FILENAME).read_text(encoding="utf-8"))["argv"]
    # Proves the server really ran under the seatbelt, not an unjailed fallback.
    assert Path(argv[0]).name == "sandbox-exec"
    assert turn.exit_kind == "completed"
    assert _thread_start(workspace)["sandbox"] == "danger-full-access"


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


def test_turn_notifications_are_read_off_the_default_executor(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers: list[str] = []
    read = CodexClient.next_turn_notification

    def recording(self: CodexClient, turn_id: str) -> Any:
        readers.append(threading.current_thread().name)
        return read(self, turn_id)

    monkeypatch.setattr(CodexClient, "next_turn_notification", recording)

    _, turn = _run(workspace)

    assert turn.exit_kind == "completed"
    assert readers
    assert all(name.startswith(app_server.TURN_READER_PREFIX) for name in readers)


def test_every_spawn_slot_has_a_notification_reader() -> None:
    assert app_server._TURN_READERS._max_workers == runtimes_adapter.CONCURRENT_SPAWN_CAP


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


def _ladder_results(monkeypatch: pytest.MonkeyPatch) -> list[TerminationResult]:
    """Record what every kill ladder run ended with, and still run it."""
    results: list[TerminationResult] = []
    ladder = cancel.cancel_with_grace

    def recording(pgid: int, *args: Any, **kwargs: Any) -> TerminationResult:
        result = ladder(pgid, *args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(cancel, "cancel_with_grace", recording)
    return results


def test_budget_crossing_interrupts_reaps_and_resolves_budget_terminated(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[int, int | None]] = []

    async def sink(sample: UsageSample, pgid: int | None) -> bool:
        total = sample.input_tokens + sample.output_tokens
        seen.append((total, pgid))
        return total >= _CAP_TOKENS

    ladders = _ladder_results(monkeypatch)
    outcome, turn = _run(workspace, {"lines": [token_usage(80, 40)]}, usage_sink=sink)

    pgid = _fake_pgid(workspace)
    (interrupt,) = [m for m in _received(workspace) if m.get("method") == "turn/interrupt"]
    assert turn == TurnOutcome(
        exit_kind="budget_terminated", runtime_tuple=_observed(_implement_spec())
    )
    assert seen == [(120, pgid)]
    assert interrupt["params"] == {"threadId": THREAD_ID, "turnId": TURN_ID}
    assert outcome.subprocess_pid == pgid
    # the group left on the soft signal, so the ladder never waited out its grace
    assert [(r.sigkill_sent, r.exited_on_term) for r in ladders] == [(False, True)]
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_a_crossing_after_the_server_exited_does_not_wait_on_the_interrupt(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def sink(sample: UsageSample, pgid: int | None) -> bool:
        # Hold the crossing until the server has exited, without reaping it,
        # so the interrupt is always written to a closed pipe.
        assert pgid is not None
        while os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            await asyncio.sleep(0.01)
        return True

    interrupts: list[str] = []
    interrupt = CodexClient.turn_interrupt

    def recording(self: CodexClient, *args: Any, **kwargs: Any) -> Any:
        try:
            answer = interrupt(self, *args, **kwargs)
        except BaseException:
            interrupts.append("raised")
            raise
        interrupts.append("answered")
        return answer

    monkeypatch.setattr(CodexClient, "turn_interrupt", recording)
    ladders = _ladder_results(monkeypatch)
    _, turn = _run(workspace, {"lines": [token_usage(80, 40)], "exit_code": 0}, usage_sink=sink)

    assert turn.exit_kind == "budget_terminated"
    # the closed pipe failed the interrupt outright rather than leaving it to time out
    assert interrupts == ["raised"]
    assert [r.sigkill_sent for r in ladders] == [False]


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


def _signalled(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every process group the kill ladder is run on, and still run it."""
    groups: list[int] = []
    ladder = cancel.cancel_with_grace

    def recording(pgid: int, *args: Any, **kwargs: Any) -> Any:
        groups.append(pgid)
        return ladder(pgid, *args, **kwargs)

    monkeypatch.setattr(cancel, "cancel_with_grace", recording)
    return groups


@pytest.mark.parametrize("method", ["initialize", "thread/start", "turn/start"])
def test_a_refused_launch_signals_the_worker_group(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    groups = _signalled(monkeypatch)
    launcher = _launcher(workspace, {"refuse": {"method": method, "message": "401 unauthorized"}})

    with pytest.raises(RuntimeSpawnError):
        asyncio.run(launcher.launch(_request(workspace)))

    assert groups == [_fake_pgid(workspace)]


def test_a_cancelled_launch_signals_the_group_and_frees_the_slot(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    groups = _signalled(monkeypatch)
    opened, gate = threading.Event(), threading.Event()
    start_thread = CodexClient.thread_start

    def stalled(self: CodexClient, params: Any) -> Any:
        opened.set()
        gate.wait(10)
        return start_thread(self, params)

    monkeypatch.setattr(CodexClient, "thread_start", stalled)
    before = spawn_inflight()

    async def scenario() -> bool:
        launch = asyncio.create_task(_launcher(workspace).launch(_request(workspace)))
        await asyncio.to_thread(opened.wait, 10)
        launch.cancel()
        gate.set()
        try:
            await launch
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(scenario())
    assert spawn_inflight() == before
    assert groups == [_fake_pgid(workspace)]


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
# Each Run reads a private CODEX_HOME, never the operator's
# ---------------------------------------------------------------------------


def test_the_wrapper_starts_codex_on_the_private_home(workspace: Path) -> None:
    _run(workspace)

    env = json.loads((workspace / "env.json").read_text(encoding="utf-8"))
    config = json.loads(
        (workspace / MCP_ARTIFACT_DIRNAME / WRAPPER_CONFIG_FILENAME).read_text("utf-8")
    )
    assert env["CODEX_HOME"] == str(_home(workspace))
    assert "CODEX_HOME" in config["env_keys"]


def test_an_operator_codex_home_in_the_environment_is_replaced(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = _operator_codex(workspace.parent / "elsewhere")
    monkeypatch.setenv("CODEX_HOME", str(operator))

    _run(workspace)

    env = json.loads((workspace / "env.json").read_text(encoding="utf-8"))
    assert env["CODEX_HOME"] == str(_home(workspace))
    link = _home(workspace) / CODEX_AUTH_FILENAME
    assert link.resolve() == (operator / CODEX_AUTH_FILENAME).resolve()


def test_the_generated_config_holds_one_server_and_no_plugin_or_marketplace(
    workspace: Path,
) -> None:
    _operator_codex(workspace.parent / "home")
    spec = _review_spec()

    _run(workspace, spec=spec)

    config = _home_config(workspace)
    assert list(config["mcp_servers"]) == [_SERVER]
    assert "plugins" not in config
    assert "marketplaces" not in config
    assert "projects" not in config
    assert config["model"] == spec.model_id
    assert config["approval_policy"] == "never"
    assert config["web_search"] == "disabled"
    assert config["features"]["shell_tool"] is False
    assert config["features"]["plugins"] is False
    assert config["features"]["apps"] is False
    assert config["skills"]["bundled"]["enabled"] is False


def test_the_auth_file_is_the_only_operator_file_in_the_private_home(workspace: Path) -> None:
    operator = _operator_codex(workspace.parent / "home")
    before = {path.name: path.read_bytes() for path in operator.iterdir()}

    _run(workspace)

    home = _home(workspace)
    link = home / CODEX_AUTH_FILENAME
    assert sorted(path.name for path in home.iterdir()) == [
        CODEX_AUTH_FILENAME,
        CODEX_CONFIG_FILENAME,
    ]
    assert link.is_symlink()
    assert link.resolve() == (operator / CODEX_AUTH_FILENAME).resolve()
    assert not (home / CODEX_CONFIG_FILENAME).is_symlink()
    assert {path.name: path.read_bytes() for path in operator.iterdir()} == before


def test_an_operator_without_an_auth_file_gets_no_link(workspace: Path) -> None:
    _run(workspace)

    assert [path.name for path in _home(workspace).iterdir()] == [CODEX_CONFIG_FILENAME]


def test_a_relaunch_replaces_the_previous_link(workspace: Path) -> None:
    operator = _operator_codex(workspace.parent / "home")
    _run(workspace)
    (operator / CODEX_AUTH_FILENAME).unlink()

    _run(workspace)

    assert not (_home(workspace) / CODEX_AUTH_FILENAME).is_symlink()


# ---------------------------------------------------------------------------
# The lane flags translate to the home's config table, at their boundaries
# ---------------------------------------------------------------------------


def test_codex_home_config_of_no_flags_is_empty() -> None:
    assert codex_home_config(()) == {}


def test_codex_home_config_of_one_override_nests_its_key() -> None:
    assert codex_home_config(("-c", "features.shell_tool=false")) == {
        "features": {"shell_tool": False}
    }


def test_codex_home_config_folds_the_server_keys_into_one_table() -> None:
    flags = (
        "-c",
        f'{SERVER_CONFIG_KEY}.command="eawf"',
        "-c",
        f'{SERVER_CONFIG_KEY}.args=["mcp", "serve"]',
        "-c",
        f"{SERVER_CONFIG_KEY}.enabled=true",
        "-c",
        'web_search="disabled"',
    )

    assert codex_home_config(flags) == {
        "mcp_servers": {_SERVER: {"command": "eawf", "args": ["mcp", "serve"], "enabled": True}},
        "web_search": "disabled",
    }


def test_codex_home_config_lets_a_later_flag_win() -> None:
    assert codex_home_config(("-c", 'model="a"', "-c", 'model="b"')) == {"model": "b"}


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (("-c",), "are not -c pairs"),
        (("--model", "x=1"), "is not a -c override"),
        (("-c", "novalue"), "is not a -c override"),
        (("-c", "key=not toml ["), "is not TOML"),
        (("-c", 'model="a"', "-c", 'model.name="b"'), "nests under a value"),
    ],
)
def test_codex_home_config_refuses_what_it_cannot_express(
    flags: tuple[str, ...], message: str
) -> None:
    with pytest.raises(RuntimeSpawnError, match=message):
        codex_home_config(flags)


@pytest.mark.parametrize(
    "table",
    [
        {},
        {"model": "gpt"},
        {"features": {}},
        {"a": {"b": {"c": 1}}, "z": 2.5},
        {"mcp_servers": {"eawf_semantic": {"args": [], "command": 'say "hi" \\ ok'}}},
        {"projects": {"/tmp/a b": {"trust_level": "trusted"}}, "name": "café\n"},
    ],
    ids=["empty", "single", "empty-table", "deep", "escapes", "quoted-key"],
)
def test_the_toml_document_reads_back_as_the_table(table: dict[str, Any]) -> None:
    assert tomllib.loads(_toml_document(table)) == table


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


# ---------------------------------------------------------------------------
# Ambient tool denials map onto real codex settings only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        ((), []),
        (("Bash",), ["-c", "features.shell_tool=false"]),
        (("Write",), []),
        (("NotATool",), []),
        (("WebFetch", "WebSearch"), ["-c", 'web_search="disabled"']),
        (("Bash", "Bash"), ["-c", "features.shell_tool=false"]),
    ],
    ids=["empty", "single", "no-setting", "unknown", "shared-setting", "repeated"],
)
def test_codex_tool_denials_boundaries(tools: tuple[str, ...], expected: list[str]) -> None:
    assert codex_adapter.codex_tool_denials(tools) == expected


def test_every_codex_tool_denial_is_a_key_the_home_config_accepts() -> None:
    config = codex_home_config(codex_adapter.codex_tool_denials(sorted(TOOL_UNIVERSE)))

    assert "tools" not in config
    assert config == {
        "features": {
            "multi_agent": False,
            "shell_tool": False,
            "skip_host_skill_discovery": True,
            "view_image": False,
        },
        "skills": {"bundled": {"enabled": False}, "include_instructions": False},
        "web_search": "disabled",
    }
