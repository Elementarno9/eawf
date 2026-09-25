"""A native Run's real launcher gets its MCP entry and streams its usage.

Native Runs carried no MCP configuration and published no usage stream: the
per-Run semantic MCP server was never attached to the launched child, and
the in-flight token meter had nothing to read while the turn ran. These
cases drive the REAL :class:`~eawf.runtime.runtimes.claude.adapter.ClaudeNativeLauncher`
and :class:`~eawf.runtime.runtimes.codex.adapter.CodexNativeLauncher` -- not
a stand-in -- against a fake provider CLI that records its own argv and
prints a scripted, synthesized stream-json / JSONL transcript. No model is
ever reached: the fake is a Python script the launcher's own ``spawn_session``
forks exactly the way it would fork the real ``claude`` / ``codex`` binary.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from typing import Final

import pytest

from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.runtime.daemon.native_dispatch import CapsuleRequest, seal_capsule
from eawf.runtime.runtimes.adapter import NativeLaunchRequest
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter, ClaudeNativeLauncher
from eawf.runtime.runtimes.codex import adapter as codex_adapter
from eawf.runtime.runtimes.codex.adapter import CodexAdapter, CodexNativeLauncher
from eawf.runtime.runtimes.metering import UsageSample
from eawf.workflow.runtime.compile import compile_run_spec
from tests import _provider_helpers as fx
from tests.integration.runtime.daemon.test_native_dispatch import capsule_request

pytestmark = pytest.mark.integration


#: A no-op jail stub, at parity with the pattern
#: ``tests/unit/runtime/runtimes/test_claude_child_env.py`` uses: the OS jail
#: has its own coverage, so these cases observe the MCP + usage seam alone.
def _bypass_jail(
    argv: list[str], *, runtime: str, cwd: str | None, session: str = "", sink: object = None
) -> list[str]:
    return argv


_CLAUDE_LINES: Final[tuple[str, ...]] = (
    json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "model": "fixture-model",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        }
    ),
    "not-json-at-all",
    json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "m2",
                "model": "fixture-model",
                "usage": {"input_tokens": 100, "output_tokens": 40},
            },
        }
    ),
    json.dumps({"type": "assistant", "message": {"id": "m3"}}),
    json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "m4",
                "model": "fixture-model",
                "usage": {"input_tokens": 100, "output_tokens": 90},
            },
        }
    ),
    json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "sess-fake-claude",
            "result": "ok",
            "total_cost_usd": 0.0,
            "usage": {"input_tokens": 100, "output_tokens": 90},
        }
    ),
)

_CODEX_LINES: Final[tuple[str, ...]] = (
    json.dumps({"type": "thread.started", "thread_id": "th-fake"}),
    json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50},
        }
    ),
    "not-json-at-all",
    json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 200, "cached_input_tokens": 0, "output_tokens": 150},
        }
    ),
    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
    json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 300, "cached_input_tokens": 50, "output_tokens": 250},
        }
    ),
)


def _write_fake_cli(directory: Path, name: str, lines: tuple[str, ...]) -> Path:
    """Write a fake provider CLI that records its argv and prints *lines*.

    A Python script rather than a shell one, at parity with the existing
    fake-``claude`` fixture, because a shell exports variables of its own.
    The lines are synthesized, neutral stream content -- no real prompt or
    session id -- so nothing here resembles a live transcript.
    """
    script = directory / name
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "with open('argv.json', 'w', encoding='utf-8') as handle:\n"
        "    json.dump(sys.argv, handle)\n"
        f"for line in {list(lines)!r}:\n"
        "    print(line)\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


class _Recorder:
    """A hand-built usage sink that records every reading it was handed.

    Attributes:
        calls: The ``(sample, pgid)`` pairs, in the order the launcher
            relayed them.
    """

    def __init__(self, *, terminate_after: int | None = None) -> None:
        """Bind the recorder; *terminate_after* answers ``True`` from then on."""
        self.calls: list[tuple[UsageSample, int | None]] = []
        self._terminate_after = terminate_after

    async def sink(self, sample: UsageSample, pgid: int | None) -> bool:
        """Record *sample* and answer whether the Run is now terminated."""
        self.calls.append((sample, pgid))
        return self._terminate_after is not None and len(self.calls) >= self._terminate_after


def _claude_spec() -> CompiledRunSpec:
    """Return a compiled spec whose winning profile is the claude lane."""
    profile = fx.profile_document(
        profile_id="fixture_claude",
        driver_ref=fx.OTHER_DRIVER_REF,
        auth_profile_ref=fx.OTHER_AUTH_REF,
        provider_options={"provider_kind": "claude"},
    )
    configuration = fx.configuration(
        profiles=[profile],
        routes=[fx.task_route_document(allowed_profiles=["fixture_claude"])],
        global_routes=[],
    )
    return compile_run_spec(
        fx.task_request(),
        configuration=configuration,
        bindings=[fx.binding(driver_ref=fx.OTHER_DRIVER_REF)],
        compiled_at=fx.COMPILED_AT,
    )


def _codex_spec() -> CompiledRunSpec:
    """Return a compiled spec whose winning profile is the codex lane."""
    return compile_run_spec(
        fx.task_request(),
        configuration=fx.configuration(),
        bindings=[fx.binding()],
        compiled_at=fx.COMPILED_AT,
    )


def _request(*, spec: CompiledRunSpec, workspace: Path, usage_sink: object) -> NativeLaunchRequest:
    """Return the launch request one fake lane is started with."""
    capsule = seal_capsule(
        spec=spec, request=CapsuleRequest.model_validate(capsule_request(token_budget=100_000))
    )
    return NativeLaunchRequest(
        spec=spec,
        capsule=capsule,
        workspace_handle=f"wsh-{'a' * 32}",
        workspace=workspace,
        prompt="implement the fixture task",
        hello_sequence=1,
        usage_sink=usage_sink,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="the fake child is a shebang script")
def test_claude_native_launch_wires_mcp_config_and_relays_usage_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launched child sees the MCP entry, and readings arrive in order."""
    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", _bypass_jail)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_fake_cli(workspace, "claude", _CLAUDE_LINES)
    recorder = _Recorder()
    request = _request(spec=_claude_spec(), workspace=workspace, usage_sink=recorder.sink)
    adapter = ClaudeAdapter()
    adapter.cli_binary = str(workspace / "claude")

    outcome = asyncio.run(ClaudeNativeLauncher(adapter=adapter).launch(request))

    assert outcome.provider_session_ref == "sess-fake-claude"
    # Claude's message.usage bills PER CALL, not a running total (unlike
    # codex below) -- the launcher's own accumulator folds each distinct
    # message id into the running sum the meter expects, so readings
    # climb: m1, then m1+m2, then m1+m2+m4.
    assert [(s.input_tokens, s.output_tokens) for s, _pgid in recorder.calls] == [
        (100, 10),
        (200, 50),
        (300, 140),
    ]
    assert all(pgid is not None for _sample, pgid in recorder.calls)

    argv = json.loads((workspace / "argv.json").read_text(encoding="utf-8"))
    assert "--mcp-config" in argv
    config_path = Path(argv[argv.index("--mcp-config") + 1])
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert "eawf-semantic" in document["mcpServers"]
    allow_index = argv.index("--allowedTools")
    assert argv[allow_index + 1].split() == [
        "mcp__eawf-semantic__budget_status",
        "mcp__eawf-semantic__submit_candidate",
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="the fake child is a shebang script")
def test_claude_native_launch_stops_relaying_once_the_sink_terminates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sink that answers terminated is not handed the readings that follow."""
    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", _bypass_jail)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_fake_cli(workspace, "claude", _CLAUDE_LINES)
    recorder = _Recorder(terminate_after=2)
    request = _request(spec=_claude_spec(), workspace=workspace, usage_sink=recorder.sink)
    adapter = ClaudeAdapter()
    adapter.cli_binary = str(workspace / "claude")

    asyncio.run(ClaudeNativeLauncher(adapter=adapter).launch(request))

    assert [(s.input_tokens, s.output_tokens) for s, _pgid in recorder.calls] == [
        (100, 10),
        (200, 50),
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="the fake child is a shebang script")
def test_codex_native_launch_wires_mcp_config_and_relays_usage_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launched child sees the MCP entry, and readings arrive in order."""
    monkeypatch.setattr(codex_adapter, "_maybe_jail_argv", _bypass_jail)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_fake_cli(workspace, "codex", _CODEX_LINES)
    recorder = _Recorder()
    request = _request(spec=_codex_spec(), workspace=workspace, usage_sink=recorder.sink)
    adapter = CodexAdapter()
    adapter.cli_binary = str(workspace / "codex")

    outcome = asyncio.run(CodexNativeLauncher(adapter=adapter).launch(request))

    assert outcome.provider_session_ref == "th-fake"
    assert [
        (s.input_tokens, s.output_tokens, s.cache_read_input_tokens) for s, _pgid in recorder.calls
    ] == [
        (100, 50, 0),
        (200, 150, 0),
        (250, 250, 50),
    ]
    assert all(pgid is not None for _sample, pgid in recorder.calls)

    argv = json.loads((workspace / "argv.json").read_text(encoding="utf-8"))
    joined = " ".join(argv)
    assert "mcp_servers.eawf_semantic.enabled=true" in joined
    assert "mcp_servers.eawf_semantic.command=" in joined
