"""The native Claude launcher drives its Run through the Agent SDK.

Every case starts the real :class:`ClaudeNativeLauncher` against
``tests/fixtures/runtime/fake_claude_cli.py`` installed as the ``claude``
binary, so the SDK, the per-Run wrapper and the kill ladder all run for real
while no real ``claude`` binary and no network is reached.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from pydantic import ValidationError

from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.runtime.budget.service import DEFAULT_GRACE_SECONDS
from eawf.runtime.daemon.native_dispatch import CapsuleRequest, seal_capsule
from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.runtime.mcp.native_launch import MCP_ARTIFACT_DIRNAME, RUN_TMP_DIRNAME, RUN_TMP_ENV_KEYS
from eawf.runtime.mcp.semantic_stdio import SERVER_NAME, ambient_tool_denial
from eawf.runtime.runtimes import adapter as runtimes_adapter
from eawf.runtime.runtimes.adapter import (
    ConcurrentSpawnCapError,
    NativeLaunchOutcome,
    NativeLaunchRequest,
    RuntimeSpawnError,
    TurnOutcome,
    ambient_denied_tools,
    hello_sdk_version,
    observed_runtime_tuple,
    spawn_inflight,
)
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude import sdk_launcher
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter, ClaudeNativeLauncher
from eawf.runtime.runtimes.claude.managed_run import (
    CONFIG_DIR_ENV,
    INSTRUCTION_SUPPRESSION_ENV,
    MANAGED_CONFIG_DIRNAME,
    SECURE_STORAGE_ENV,
)
from eawf.runtime.runtimes.claude.sdk_launcher import (
    CLAUDE_TMPDIR_ENV,
    PGID_FILENAME,
    WRAPPER_FILENAME,
    _lane_options,
    _UsageFold,
)
from eawf.runtime.runtimes.host_transcript import SUMMARY_LIMIT, WITHHELD_TEXT
from eawf.runtime.runtimes.metering import UsageSample
from eawf.runtime.sandbox.env_scrub import build_child_env, resolve_binary_dir
from eawf.workflow.runtime.compile import compile_run_spec
from tests import _provider_helpers as fx
from tests.fixtures.runtime import fake_claude_cli
from tests.fixtures.runtime.fake_claude_cli import (
    BUILTIN_TOOLS,
    MODEL,
    TOOLS_FILENAME,
    VERSION,
    install_fake_claude,
)
from tests.integration.runtime.daemon.test_native_dispatch import capsule_request

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the session wrapper and the fake are POSIX scripts"
)

#: The token cap the budget case crosses.
_CAP_TOKENS: Final = 100

#: CoreFoundation seeds this into every darwin process itself, so the child
#: reports it whatever environment it was started with.
_PLATFORM_INJECTED: Final = frozenset({"__CF_USER_TEXT_ENCODING"})

#: The variables a leak would carry into the child if the wrapper did not scrub.
_LEAKS: Final = {
    "AWS_SECRET_ACCESS_KEY": "leak",  # pragma: allowlist secret
    "EAWF_FIXTURE_LEAK": "leak",
}

#: The runtime the fake's init message reports.
_OBSERVED: Final = RunRuntimeTuple(
    harness="claude-code", harness_version=VERSION.split()[0], model=MODEL
)

UsageSink = Callable[[UsageSample, int | None], Awaitable[bool]]


def _bypass_jail(
    argv: list[str],
    *,
    runtime: str,
    cwd: str | None,
    session: str = "",
    sink: object = None,
    private_home: Path | None = None,
    storage_dirs: tuple[Path, ...] = (),
) -> list[str]:
    """Leave the child unjailed so the fake runs on any host."""
    return argv


def _spec() -> CompiledRunSpec:
    """Return a compiled spec whose winning profile is the claude lane."""
    profile = fx.profile_document(
        profile_id="fixture_claude",
        driver_ref=fx.OTHER_DRIVER_REF,
        auth_profile_ref=fx.OTHER_AUTH_REF,
        provider_options={"provider_kind": "claude"},
    )
    return compile_run_spec(
        fx.task_request(),
        configuration=fx.configuration(
            profiles=[profile],
            routes=[fx.task_route_document(allowed_profiles=["fixture_claude"])],
            global_routes=[],
        ),
        bindings=[fx.binding(driver_ref=fx.OTHER_DRIVER_REF)],
        compiled_at=fx.COMPILED_AT,
    )


def _request(
    workspace: Path,
    *,
    spec: CompiledRunSpec | None = None,
    usage_sink: UsageSink | None = None,
    said: list[str] | None = None,
) -> NativeLaunchRequest:
    """Return the launch request of one fake Run in *workspace*."""
    compiled = spec if spec is not None else _spec()
    capsule = seal_capsule(
        spec=compiled,
        request=CapsuleRequest.model_validate(capsule_request(token_budget=_CAP_TOKENS)),
    )

    async def record(message: Any) -> None:
        assert said is not None
        said.append(message.summary)

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
    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", _bypass_jail)
    root = tmp_path / "ws"
    root.mkdir()
    return root


def _launcher(workspace: Path, script: dict[str, Any] | None = None) -> ClaudeNativeLauncher:
    """Return a launcher whose ``claude`` is the fake, installed in *workspace*."""
    adapter = ClaudeAdapter()
    adapter.cli_binary = str(install_fake_claude(workspace, script))
    return ClaudeNativeLauncher(adapter=adapter)


async def _launch_and_turn(
    launcher: ClaudeNativeLauncher, request: NativeLaunchRequest
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


def _argv(workspace: Path) -> list[str]:
    return list(json.loads((workspace / "argv.json").read_text(encoding="utf-8")))


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv) if token == flag]


# ---------------------------------------------------------------------------
# CR-1: the launch returns at the session announcement and hands back the turn
# ---------------------------------------------------------------------------


def test_launch_announces_the_session_and_the_turn_resolves_completed(workspace: Path) -> None:
    said: list[str] = []

    outcome, turn = _run(workspace, said=said)

    pgid = int((workspace / MCP_ARTIFACT_DIRNAME / PGID_FILENAME).read_text(encoding="utf-8"))
    assert outcome.provider_session_ref == "sess-fake-claude"
    assert outcome.hello.provider_session_ref == "sess-fake-claude"
    assert outcome.subprocess_pid == pgid
    assert turn == TurnOutcome(exit_kind="completed", answer_text="done", runtime_tuple=_OBSERVED)
    assert said == ["done"]
    assert "turn" not in outcome.model_dump()


def test_launch_returns_before_the_turn_ends(workspace: Path) -> None:
    script = {"lines": [], "hang": True}

    async def scenario() -> bool:
        outcome = await _launcher(workspace, script).launch(_request(workspace))
        assert outcome.turn is not None
        running = not outcome.turn.done()
        outcome.turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outcome.turn
        return running

    assert asyncio.run(scenario())


def test_an_aborted_result_resolves_the_turn_interrupted(workspace: Path) -> None:
    script = {"lines": [{"type": "result", "terminal_reason": "aborted_streaming"}]}

    _, turn = _run(workspace, script)

    assert turn.exit_kind == "interrupted"


# ---------------------------------------------------------------------------
# CR-2: containment is enforced by the per-Run wrapper
# ---------------------------------------------------------------------------


def test_wrapper_scrubs_the_environment_and_records_the_group(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in _LEAKS.items():
        monkeypatch.setenv(key, value)
    launcher = _launcher(workspace)
    cli = launcher._adapter.cli_binary
    allowlist = {
        *build_child_env("claude-code", extra_path_dir=resolve_binary_dir(cli)),
        CONFIG_DIR_ENV,
        SECURE_STORAGE_ENV,
        *INSTRUCTION_SUPPRESSION_ENV,
        *RUN_TMP_ENV_KEYS,
        CLAUDE_TMPDIR_ENV,
    }

    asyncio.run(_launch_and_turn(launcher, _request(workspace)))

    run_dir = workspace / MCP_ARTIFACT_DIRNAME
    env = json.loads((workspace / "env.json").read_text(encoding="utf-8"))
    assert set(env) - _PLATFORM_INJECTED <= allowlist
    assert not set(_LEAKS) & set(env)
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert Path(env[CONFIG_DIR_ENV]).resolve().is_relative_to(run_dir.resolve())
    # Temp files go to the Run's own directory, which the launch created.
    run_tmp = run_dir / RUN_TMP_DIRNAME
    assert {env[key] for key in (*RUN_TMP_ENV_KEYS, CLAUDE_TMPDIR_ENV)} == {str(run_tmp)}
    assert run_tmp.is_dir()
    assert "--setting-sources=" in _argv(workspace)
    recorded = (run_dir / PGID_FILENAME).read_text(encoding="utf-8")
    assert recorded == (workspace / "fake.pgid").read_text(encoding="utf-8")
    assert (run_dir / WRAPPER_FILENAME).stat().st_mode & stat.S_IXUSR
    assert "leak" not in (run_dir / "claude-wrapper.json").read_text(encoding="utf-8")


def test_wrapper_applies_the_jail_argv(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jail = workspace / "fake-jail"
    jail.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "if sys.argv[2:] != ['-v']:\n"
        f"    with open({str(workspace / 'jail.json')!r}, 'w', encoding='utf-8') as handle:\n"
        "        json.dump(sys.argv, handle)\n"
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

    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", fake_jail)
    launcher = _launcher(workspace)

    _, turn = asyncio.run(_launch_and_turn(launcher, _request(workspace)))

    jailed = json.loads((workspace / "jail.json").read_text(encoding="utf-8"))
    assert turn.exit_kind == "completed"
    # The jail opens the Run's own configuration home, never the operator's.
    assert calls == [
        {
            "argv": [launcher._adapter.cli_binary],
            "runtime": "claude-code",
            "cwd": str(workspace),
            "private_home": workspace / MCP_ARTIFACT_DIRNAME / MANAGED_CONFIG_DIRNAME,
            # The daemon's state and write-ahead log are closed to the Run.
            "storage_dirs": (workspace.parent / ".ea", runtime_dir() / "wal"),
        }
    ]
    assert jailed[1] == launcher._adapter.cli_binary
    assert "--output-format" in jailed


def test_the_turn_holds_a_spawn_slot_until_it_ends(workspace: Path) -> None:
    before = spawn_inflight()

    async def scenario() -> tuple[int, int]:
        outcome = await _launcher(workspace).launch(_request(workspace))
        assert outcome.turn is not None
        during = spawn_inflight()
        await outcome.turn
        return during, spawn_inflight()

    during, after = asyncio.run(scenario())

    assert (during, after) == (before + 1, before)


def test_a_saturated_spawn_cap_refuses_before_the_fork(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtimes_adapter, "CONCURRENT_SPAWN_CAP", 0)

    with pytest.raises(ConcurrentSpawnCapError):
        asyncio.run(_launcher(workspace).launch(_request(workspace)))

    assert not (workspace / "argv.json").exists()


def test_win32_refuses_the_launch(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdk_launcher, "sys", SimpleNamespace(platform="win32"))

    with pytest.raises(RuntimeSpawnError, match="POSIX session wrapper"):
        asyncio.run(_launcher(workspace).launch(_request(workspace)))


# ---------------------------------------------------------------------------
# CR-3: the Run's server is the only MCP server, and the denials are the spec's
# ---------------------------------------------------------------------------


def test_options_carry_one_strict_server_and_the_spec_denials(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec()
    asked: list[CompiledRunSpec] = []

    def spec_denials(compiled: CompiledRunSpec) -> list[str]:
        # A compiled spec cannot seal an ambient denial into its capsule, so
        # the rendering of one is stood in for to see where it lands.
        asked.append(compiled)
        return ["Bash", "WebFetch"]

    monkeypatch.setattr(sdk_launcher, "ambient_denied_tools", spec_denials)

    _run(workspace, spec=spec)

    argv = _argv(workspace)
    (document,) = _flag_values(argv, "--mcp-config")
    assert list(json.loads(document)["mcpServers"]) == [SERVER_NAME]
    assert "--strict-mcp-config" in argv
    denied = _flag_values(argv, "--disallowedTools")
    assert asked == [spec]
    assert denied[0] == "Bash,WebFetch"
    assert denied[1] == " ".join(ambient_tool_denial())
    assert _flag_values(argv, "--permission-mode") == ["bypassPermissions"]
    assert _flag_values(argv, "--model") == [spec.model_id]


def test_no_spec_denials_leave_only_the_lane_denial(workspace: Path) -> None:
    _run(workspace)

    assert ambient_denied_tools(_spec()) == []
    assert _flag_values(_argv(workspace), "--disallowedTools") == [" ".join(ambient_tool_denial())]


#: The tools the fixture capsule grants, as the provider addresses them.
_GRANTED: Final = (
    f"mcp__{SERVER_NAME}__budget_status",
    f"mcp__{SERVER_NAME}__submit_candidate",
)

#: Built-ins that each reach a shell, the filesystem or the network.
_DANGEROUS: Final = ("Monitor", "Bash", "Write", "Edit", "WebFetch", "WebSearch", "ToolSearch")


def _tools(workspace: Path) -> dict[str, Any]:
    return dict(json.loads((workspace / TOOLS_FILENAME).read_text(encoding="utf-8")))


def test_the_child_holds_exactly_its_granted_tools(workspace: Path) -> None:
    _, turn = _run(workspace)

    tools = _tools(workspace)
    assert turn.exit_kind == "completed"
    assert tools["tools_flag"] == [""]
    assert tools["available"] == list(_GRANTED)
    assert not set(_DANGEROUS) & set(tools["available"])
    assert not set(BUILTIN_TOOLS) & set(tools["available"])


def test_the_deny_list_alone_would_leave_monitor_to_the_child() -> None:
    # The flags the launcher passed before the allow-list: the lane denial
    # with no base set. The fake resolves them as the real CLI did.
    denial = " ".join(ambient_tool_denial())

    resolved = fake_claude_cli._resolve_tools(["claude", "--disallowedTools", denial])

    assert "Monitor" in resolved["available"]
    assert "ToolSearch" in resolved["available"]


@pytest.mark.parametrize(
    "reported",
    [
        pytest.param(["Monitor"], id="one-ungranted-built-in"),
        pytest.param([*_GRANTED, "Bash"], id="grant-plus-one"),
        pytest.param([*_GRANTED, f"mcp__{SERVER_NAME}__ungranted"], id="ungranted-mcp-tool"),
    ],
)
def test_a_session_holding_an_ungranted_tool_is_refused_and_reaped(
    workspace: Path, reported: list[str]
) -> None:
    # A CLI that ignored the restriction is stood in for by overriding what
    # the fake reports; the launch must not hand that session a turn.
    script = {"init_fields": {"tools": reported}, "lines": [], "hang": True}

    with pytest.raises(RuntimeSpawnError, match="tool_allowlist_unenforced"):
        asyncio.run(_launcher(workspace, script).launch(_request(workspace)))

    pgid = int((workspace / "fake.pgid").read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


@pytest.mark.parametrize(
    "reported",
    [
        pytest.param(None, id="absent"),
        pytest.param("Monitor", id="not-a-list"),
        pytest.param([None], id="not-names"),
    ],
)
def test_a_session_without_a_readable_tool_list_is_refused(
    workspace: Path, reported: object
) -> None:
    script = {"init_fields": {"tools": reported}, "lines": [], "hang": True}

    with pytest.raises(RuntimeSpawnError, match="tool_allowlist_unverified"):
        asyncio.run(_launcher(workspace, script).launch(_request(workspace)))


def test_an_empty_tool_list_is_within_any_grant(workspace: Path) -> None:
    script = {"init_fields": {"tools": []}, "lines": [{"type": "result", "result": "done"}]}

    _, turn = _run(workspace, script)

    assert turn.exit_kind == "completed"


# ---------------------------------------------------------------------------
# CR-4: a budget crossing interrupts, then the kill ladder reaps the group
# ---------------------------------------------------------------------------


def test_budget_crossing_reaps_the_group_and_resolves_budget_terminated(workspace: Path) -> None:
    usage = {"input_tokens": 80, "output_tokens": 40}
    script = {
        "lines": [{"type": "assistant", "message": {"id": "m1", "usage": usage}}],
        "hang": True,
    }
    seen: list[tuple[int, int | None]] = []

    async def sink(sample: UsageSample, pgid: int | None) -> bool:
        total = sample.input_tokens + sample.output_tokens
        seen.append((total, pgid))
        return total >= _CAP_TOKENS

    started = time.monotonic()
    outcome, turn = _run(workspace, script, usage_sink=sink)
    elapsed = time.monotonic() - started

    pgid = int((workspace / "fake.pgid").read_text(encoding="utf-8"))
    assert turn == TurnOutcome(exit_kind="budget_terminated", runtime_tuple=_OBSERVED)
    assert seen == [(120, pgid)]
    assert outcome.subprocess_pid == pgid
    assert elapsed < DEFAULT_GRACE_SECONDS
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_usage_is_folded_cumulatively_across_calls(workspace: Path) -> None:
    usage = {"input_tokens": 10, "output_tokens": 5}
    script = {
        "lines": [
            {"type": "assistant", "message": {"id": "a", "usage": usage}},
            {"type": "assistant", "message": {"id": "a", "usage": usage}},
            {"type": "assistant", "message": {"id": "b", "usage": usage}},
            {"type": "result", "usage": {"input_tokens": 20, "output_tokens": 10}},
        ]
    }
    totals: list[int] = []

    async def sink(sample: UsageSample, pgid: int | None) -> bool:
        totals.append(sample.input_tokens + sample.output_tokens)
        return False

    _, turn = _run(workspace, script, usage_sink=sink)

    assert turn.exit_kind == "completed"
    assert totals == [15, 30]


# ---------------------------------------------------------------------------
# CR-5: SDK failures map onto the spawn error taxonomy
# ---------------------------------------------------------------------------

_ERROR_RESULT: Final = {
    "type": "result",
    "is_error": True,
    "api_error_status": 401,
    "result": "API Error: 401 unauthorized",
}

_BEFORE_HELLO: Final[dict[str, dict[str, Any] | None]] = {
    "cli-not-found": None,
    "process-error": {"init": False, "stderr": "HTTP 401 unauthorized", "exit_code": 1},
    "json-decode": {"init": False, "lines": ["{not json"], "exit_code": 0},
    "error-result": {"init": False, "lines": [_ERROR_RESULT], "exit_code": 1},
}


@pytest.mark.parametrize("case", sorted(_BEFORE_HELLO))
def test_failures_before_the_hello_raise_spawn_errors(workspace: Path, case: str) -> None:
    launcher = _launcher(workspace, _BEFORE_HELLO[case])
    if case == "cli-not-found":
        launcher._adapter.cli_binary = str(workspace / "missing-claude")

    with pytest.raises(RuntimeSpawnError) as raised:
        asyncio.run(launcher.launch(_request(workspace)))

    error = raised.value
    if case == "cli-not-found":
        assert error.exit_status == 127
    elif case == "json-decode":
        assert "not valid json" in str(error)
    else:
        assert launcher._adapter.parse_error(error.exit_status or 1, error.stderr) == (
            "RUNTIME_AUTH_ERROR"
        )


_AFTER_HELLO: Final[dict[str, tuple[dict[str, Any], str, bool]]] = {
    "process-error": (
        {"stderr": "rate_limit_error 429", "exit_code": 1},
        "RUNTIME_RATE_LIMIT",
        False,
    ),
    "json-decode": ({"lines": ["{not json"], "exit_code": 0}, "RUNTIME_API_ERROR", False),
    "error-result": ({"lines": [_ERROR_RESULT], "exit_code": 1}, "RUNTIME_AUTH_ERROR", True),
}


@pytest.mark.parametrize("case", sorted(_AFTER_HELLO))
def test_failures_after_the_hello_resolve_the_turn_failed(workspace: Path, case: str) -> None:
    script, error_class, result_is_error = _AFTER_HELLO[case]

    outcome, turn = _run(workspace, script)

    assert outcome.provider_session_ref == "sess-fake-claude"
    assert turn.exit_kind == "failed"
    assert turn.error_class == error_class
    assert turn.result_is_error is result_is_error
    assert turn.error_text


def test_a_turn_without_a_result_fails(workspace: Path) -> None:
    _, turn = _run(workspace, {"lines": [], "exit_code": 0})

    assert turn.exit_kind == "failed"
    assert turn.error_text == "claude ended the turn without a result"


# ---------------------------------------------------------------------------
# The usage fold and the lane-flag translation, at their boundaries
# ---------------------------------------------------------------------------


def _assistant(usage: Any, message_id: str | None = "m") -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text="x")], model="m", usage=usage, message_id=message_id
    )


def _result(usage: Any) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        usage=usage,
    )


def test_fold_skips_a_message_with_no_usage() -> None:
    assert _UsageFold().observe(_assistant(None)) is None


def test_fold_skips_a_malformed_usage_block() -> None:
    fold = _UsageFold()

    assert fold.observe(_assistant({"input_tokens": -1})) is None
    assert fold.observe(_assistant({"input_tokens": "many"}, message_id="n")) is None


def test_fold_sums_unidentified_calls_unconditionally() -> None:
    fold = _UsageFold()
    fold.observe(_assistant({"input_tokens": 1}, message_id=None))

    second = fold.observe(_assistant({"input_tokens": 1}, message_id=None))

    assert second == UsageSample(input_tokens=2, output_tokens=0, cache_read_input_tokens=0)


def test_fold_adopts_a_larger_turn_total_and_ignores_a_smaller_one() -> None:
    fold = _UsageFold()
    fold.observe(_assistant({"input_tokens": 5}))

    assert fold.observe(_result({"input_tokens": 4})) is None
    assert fold.observe(_result({"input_tokens": 9, "cache_read_input_tokens": 2})) == UsageSample(
        input_tokens=9, output_tokens=0, cache_read_input_tokens=2
    )


def test_lane_options_translate_the_isolation_and_mcp_flags() -> None:
    files = {"/run/mcp.json": json.dumps({"mcpServers": {SERVER_NAME: {"command": "eawf"}}})}

    lane = _lane_options(
        (
            "--mcp-config",
            "/run/mcp.json",
            "--strict-mcp-config",
            "--allowedTools",
            "a b",
            "--disallowedTools",
            "Bash",
            "--setting-sources",
            "",
        ),
        files,
    )

    assert list(lane.mcp_servers) == [SERVER_NAME]
    assert lane.strict_mcp_config
    assert lane.allowed_tools == ["a", "b"]
    assert lane.lane_denied_tools == "Bash"
    assert lane.setting_sources == []


def test_lane_options_of_no_flags_are_empty() -> None:
    lane = _lane_options((), {})

    assert lane.mcp_servers == {}
    assert not lane.strict_mcp_config
    assert lane.setting_sources is None


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (("--unknown", "x"), "has no Agent SDK option"),
        (("--allowedTools",), "carries no value"),
        (("--setting-sources", "user"), "are not empty"),
        (("--mcp-config", "/elsewhere.json"), "was not rendered"),
    ],
)
def test_lane_options_refuse_what_they_cannot_express(flags: tuple[str, ...], message: str) -> None:
    with pytest.raises(RuntimeSpawnError, match=message):
        _lane_options(flags, {})


# ---------------------------------------------------------------------------
# The turn carries the final answer and the runtime the provider reported
# ---------------------------------------------------------------------------


def test_a_completed_turn_carries_the_whole_unscrubbed_answer(workspace: Path) -> None:
    path = "/".join(("", "Users", "fixture", "report.md"))
    answer = f"wrote {path}\n" + "x" * (SUMMARY_LIMIT + 1)
    content = [{"type": "text", "text": answer}]
    script = {
        "lines": [
            {"type": "assistant", "message": {"id": "m1", "content": content}},
            {"type": "result", "result": answer},
        ]
    }
    said: list[str] = []

    _, turn = _run(workspace, script, said=said)

    assert turn.exit_kind == "completed"
    assert turn.answer_text == answer
    assert said == [WITHHELD_TEXT]


@pytest.mark.parametrize("answer", ["", "y"])
def test_a_completed_answer_of_boundary_length_is_kept_as_said(
    workspace: Path, answer: str
) -> None:
    _, turn = _run(workspace, {"lines": [{"type": "result", "result": answer}]})

    assert turn.answer_text == answer


def test_a_result_without_text_carries_no_answer(workspace: Path) -> None:
    _, turn = _run(workspace, {"lines": [{"type": "result"}]})

    assert turn.exit_kind == "completed"
    assert turn.answer_text is None


@pytest.mark.parametrize(
    ("script", "exit_kind"),
    [
        (
            {"lines": [{"type": "result", "result": "half", "terminal_reason": "aborted_tools"}]},
            "interrupted",
        ),
        ({"lines": [_ERROR_RESULT], "exit_code": 1}, "failed"),
    ],
)
def test_an_aborted_or_failed_turn_carries_no_answer(
    workspace: Path, script: dict[str, Any], exit_kind: str
) -> None:
    _, turn = _run(workspace, script)

    assert turn.exit_kind == exit_kind
    assert turn.answer_text is None


def test_runtime_tuple_is_read_from_the_init_message_and_named_by_the_hello(
    workspace: Path,
) -> None:
    outcome, turn = _run(workspace)

    assert turn.runtime_tuple == _OBSERVED
    assert outcome.hello.sdk_version == "2.1.300"


def test_a_failed_turn_still_carries_the_runtime_tuple(workspace: Path) -> None:
    _, turn = _run(workspace, {"lines": [_ERROR_RESULT], "exit_code": 1})

    assert turn.exit_kind == "failed"
    assert turn.runtime_tuple == _OBSERVED


def test_runtime_tuple_drops_what_the_init_message_omits_or_misspells(workspace: Path) -> None:
    script = {
        "init_fields": {"claude_code_version": None, "model": "not a model"},
        "lines": [{"type": "result", "result": "done"}],
    }
    launcher = _launcher(workspace, script)

    outcome, turn = asyncio.run(_launch_and_turn(launcher, _request(workspace)))

    assert turn.runtime_tuple == RunRuntimeTuple(harness="claude-code")
    assert outcome.hello.sdk_version == launcher.driver_version


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1", "1"),
        ("1" * 64, "1" * 64),
        ("1" * 65, None),
        ("", None),
        (2, None),
        ("2.1.300 (Claude Code)", None),
    ],
)
def test_observed_runtime_tuple_keeps_only_a_version_in_grammar(
    version: object, expected: str | None
) -> None:
    runtime = observed_runtime_tuple("claude-code", harness_version=version)

    assert runtime.harness_version == expected


def test_observed_runtime_tuple_of_nothing_names_only_the_harness() -> None:
    assert observed_runtime_tuple("codex") == RunRuntimeTuple(harness="codex")


def test_observed_runtime_tuple_refuses_a_harness_outside_its_grammar() -> None:
    with pytest.raises(ValidationError):
        observed_runtime_tuple("Not A Slug")


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2.1.300", "2.1.300"),
        ("2.1.300-beta.1", "2.1.300-beta.1"),
        ("2.1", "9.9.9"),
        (None, "9.9.9"),
    ],
)
def test_hello_sdk_version_falls_back_to_the_driver_outside_semver(
    version: str | None, expected: str
) -> None:
    runtime = RunRuntimeTuple(harness="claude-code", harness_version=version)

    assert hello_sdk_version(runtime, driver_version="9.9.9") == expected
