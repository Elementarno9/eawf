"""SURF-168: each host configuration key eawf writes has an observed host effect.

These are the probes :data:`eawf.runtime.harness.host_keys.HOST_KEYS` cites.
Each runs the installed host binary against a configuration carrying the
key and watches the host behave differently than without it -- a host's
own parse check is not evidence, since hosts accept invented keys. Model
traffic goes to a local stub API with a dummy credential, and every host
home is a temporary directory, so no probe reaches the network or the
operator's configuration. A probe skips when its host is not installed.
"""

from __future__ import annotations

import json
import os
import pty
import select
import shutil
import socketserver
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

needs_codex = pytest.mark.skipif(shutil.which("codex") is None, reason="codex is not installed")
needs_claude = pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not installed")

_HOST_TIMEOUT = 90

#: A credential the stub API accepts; it authenticates nowhere else.
_STUB_API_KEY = "sk-ant-probe"  # pragma: allowlist secret


# --- stub model APIs -------------------------------------------------------------------------


@dataclass
class _Exchange:
    """What the stub saw of one model request."""

    body: dict[str, Any]
    role: str
    tools: list[str]


@dataclass
class _Stub:
    """A local model API that answers from *route* and records every request."""

    route: Callable[[dict[str, Any]], tuple[str, bytes]]
    exchanges: list[_Exchange] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    in_flight: int = 0
    peak: int = 0


class _Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind resolves the host's FQDN, which can stall
        # for tens of seconds; the probes only need the loopback address.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


@contextmanager
def _serve(stub: _Stub) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            self._send("application/json", b'{"object":"list","data":[]}')

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            content_type, payload = stub.route(body)
            self._send(content_type, payload)

        def _send(self, content_type: str, payload: bytes) -> None:
            self.send_response(200)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = _Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


# Anthropic Messages API --------------------------------------------------------------------


def _anthropic_reply(blocks: list[dict[str, Any]]) -> tuple[str, bytes]:
    message: dict[str, Any] = {
        "id": "msg_probe",
        "type": "message",
        "role": "assistant",
        "model": "claude-probe",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    events: list[dict[str, Any]] = [{"type": "message_start", "message": message}]
    stop = "end_turn"
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            events.append(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                }
            )
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            stop = "tool_use"
            events.append(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {**block, "input": {}},
                }
            )
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        events.append({"type": "content_block_delta", "index": index, "delta": delta})
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 1},
        }
    )
    events.append({"type": "message_stop"})
    return "text/event-stream", _sse(events)


def _text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": value}]


def _tool(index: int, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{index}", "name": name, "input": tool_input}


def _agent(index: int, prompt: str) -> dict[str, Any]:
    return _tool(
        index,
        "Agent",
        {"description": f"probe {index}", "prompt": prompt, "subagent_type": "general-purpose"},
    )


def _has_tool_result(messages: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(message.get("content"), list)
        and any(
            isinstance(part, dict) and part.get("type") == "tool_result"
            for part in message["content"]
        )
        for message in messages
    )


def _claude_stub(root_blocks: list[dict[str, Any]], *, child_spawns: bool = False) -> _Stub:
    """Answer the root with *root_blocks*, then close every turn with text.

    A child (its prompt carries ``CHILD``) holds its reply briefly so
    concurrent children overlap, and with *child_spawns* asks for one
    grandchild when the host offers it the Agent tool.
    """
    stub: _Stub

    def route(body: dict[str, Any]) -> tuple[str, bytes]:
        messages = body.get("messages", [])
        dump = json.dumps(messages)
        tools = [str(tool.get("name")) for tool in body.get("tools", [])]
        done = _has_tool_result(messages)
        if not tools:
            role, blocks = "side", _text("ok")
        elif "GRANDCHILD" in dump:
            role, blocks = "grandchild", _text("done")
        elif "CHILD" in dump and "PROBE" not in dump:
            role = "child"
            with stub.lock:
                stub.in_flight += 1
                stub.peak = max(stub.peak, stub.in_flight)
            time.sleep(1.0)
            with stub.lock:
                stub.in_flight -= 1
            spawn = child_spawns and "Agent" in tools and not done
            blocks = [_agent(9, "GRANDCHILD say done")] if spawn else _text("done")
        else:
            role = "root"
            blocks = _text("done") if done or "PROBE" not in dump else root_blocks
        with stub.lock:
            stub.exchanges.append(_Exchange(body=body, role=role, tools=tools))
        return _anthropic_reply(blocks)

    stub = _Stub(route=route)
    return stub


@dataclass(frozen=True)
class _ClaudeHost:
    """An isolated Claude Code home with one project directory."""

    root: Path
    project: Path

    def env(self, base_url: str) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CLAUDE", "ANTHROPIC"))
        }
        env.update(
            HOME=str(self.root / "home"),
            CLAUDE_CONFIG_DIR=str(self.root / "config"),
            ANTHROPIC_API_KEY=_STUB_API_KEY,
            ANTHROPIC_BASE_URL=base_url,
            DISABLE_TELEMETRY="1",
            DISABLE_AUTOUPDATER="1",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        )
        return env

    def settings(self, document: dict[str, Any]) -> None:
        (self.project / ".claude" / "settings.json").write_text(json.dumps(document), "utf-8")

    def run(self, prompt: str, stub: _Stub) -> subprocess.CompletedProcess[str]:
        with _serve(stub) as base_url:
            return subprocess.run(
                ["claude", "-p", prompt, "--max-turns", "6", "--permission-mode", "default"],
                cwd=self.project,
                env=self.env(base_url),
                capture_output=True,
                text=True,
                timeout=_HOST_TIMEOUT,
                stdin=subprocess.DEVNULL,
                check=False,
            )


@pytest.fixture
def claude_host(tmp_path: Path) -> _ClaudeHost:
    root = tmp_path.resolve()
    project = root / "project"
    for directory in (project / ".claude", root / "home", root / "config"):
        directory.mkdir(parents=True)
    return _ClaudeHost(root=root, project=project)


def _recording_hooks(events: list[str], sink: Path) -> dict[str, Any]:
    command = f"cat >> '{sink}'; echo >> '{sink}'"
    entry = [{"matcher": "", "hooks": [{"type": "command", "command": command}]}]
    return dict.fromkeys(events, entry)


def _fired(sink: Path) -> list[str]:
    if not sink.exists():
        return []
    lines = sink.read_text("utf-8").splitlines()
    return [json.loads(line)["hook_event_name"] for line in lines if line.strip()]


# OpenAI Responses API ----------------------------------------------------------------------


def _codex_reply(items: list[dict[str, Any]]) -> tuple[str, bytes]:
    response = {
        "id": "resp_probe",
        "object": "response",
        "status": "completed",
        "output": items,
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        }
    ]
    events += [
        {"type": "response.output_item.done", "output_index": index, "item": item}
        for index, item in enumerate(items)
    ]
    events.append({"type": "response.completed", "response": response})
    return "text/event-stream", _sse(events)


def _codex_message(value: str) -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_probe",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": value, "annotations": []}],
    }


def _codex_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": f"fc_{name}",
        "call_id": f"call_{name}",
        "name": name,
        "namespace": "multi_agent_v1",
        "arguments": json.dumps(arguments),
    }


def _codex_spawn_stub() -> _Stub:
    """Root spawns a child and waits for it; the child tries to spawn a grandchild."""
    stub: _Stub

    def route(body: dict[str, Any]) -> tuple[str, bytes]:
        items = [item for item in body.get("input", []) if isinstance(item, dict)]
        dump = json.dumps(items)
        outputs = [
            str(item.get("output")) for item in items if item.get("type") == "function_call_output"
        ]
        calls = [item.get("name") for item in items if item.get("type") == "function_call"]
        if "PROBE" in dump:
            role = "root"
            if not outputs:
                reply = [_codex_call("spawn_agent", {"message": "CHILD say done"})]
            elif "wait_agent" not in calls:
                agent_id = json.loads(outputs[-1])["agent_id"]
                reply = [_codex_call("wait_agent", {"targets": [agent_id], "timeout_ms": 30000})]
            else:
                reply = [_codex_message("done")]
        elif "CHILD" in dump:
            role = "child"
            spawn = _codex_call("spawn_agent", {"message": "GRANDKID say done"})
            reply = [_codex_message("done")] if outputs else [spawn]
        elif "GRANDKID" in dump:
            role, reply = "grandchild", [_codex_message("done")]
        else:
            role, reply = "side", [_codex_message("ok")]
        with stub.lock:
            stub.exchanges.append(_Exchange(body=body, role=role, tools=[]))
        return _codex_reply(reply)

    stub = _Stub(route=route)
    return stub


def _plain_codex_stub() -> _Stub:
    def route(body: dict[str, Any]) -> tuple[str, bytes]:
        return _codex_reply([_codex_message("ok")])

    return _Stub(route=route)


@dataclass(frozen=True)
class _CodexHost:
    """An isolated Codex home and working directory."""

    root: Path

    @property
    def home(self) -> Path:
        return self.root / "codex-home"

    def env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("OPENAI")}
        env.update(HOME=str(self.root / "home"), CODEX_HOME=str(self.home), PROBE_KEY="probe")
        return env

    def config(self, body: str, base_url: str = "http://127.0.0.1:9") -> None:
        provider = (
            "\n[model_providers.probe]\n"
            'name = "probe"\n'
            f'base_url = "{base_url}/v1"\n'
            'wire_api = "responses"\n'
            'env_key = "PROBE_KEY"\n'
        )
        (self.home / "config.toml").write_text(body + provider, "utf-8")

    def exec(self, body: str, stub: _Stub, *, model: str = "gpt-5.5") -> None:
        with _serve(stub) as base_url:
            self.config(body, base_url)
            subprocess.run(
                [
                    "codex",
                    "exec",
                    "-c",
                    'model_provider="probe"',
                    "-m",
                    model,
                    "--skip-git-repo-check",
                    "PROBE",
                ],
                cwd=self.root / "work",
                env=self.env(),
                capture_output=True,
                text=True,
                timeout=_HOST_TIMEOUT,
                stdin=subprocess.DEVNULL,
                check=True,
            )

    def prompt_input(self, body: str) -> str:
        self.config(body)
        return subprocess.run(
            ["codex", "debug", "prompt-input", "hi"],
            cwd=self.root / "work",
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=_HOST_TIMEOUT,
            check=True,
        ).stdout


@pytest.fixture
def codex_host(tmp_path: Path) -> _CodexHost:
    root = tmp_path.resolve()
    for directory in (root / "codex-home", root / "home", root / "work"):
        directory.mkdir(parents=True)
    return _CodexHost(root=root)


# --- Codex ------------------------------------------------------------------------------------


@needs_codex
def test_surf_168_codex_thread_count_reaches_the_session(codex_host: _CodexHost) -> None:
    for threads in (2, 5):
        prompt = codex_host.prompt_input(
            f"[agents]\nmax_concurrent_threads_per_session = {threads}\n"
        )
        assert f"There are {threads + 1} available concurrency slots" in prompt


@needs_codex
def test_surf_168_codex_max_depth_refuses_a_grandchild(codex_host: _CodexHost) -> None:
    roles: dict[int, list[str]] = {}
    for depth in (1, 2):
        stub = _codex_spawn_stub()
        codex_host.exec(f"[agents]\nmax_depth = {depth}\n", stub, model="gpt-5")
        roles[depth] = [exchange.role for exchange in stub.exchanges]
    assert "child" in roles[1]
    assert "grandchild" not in roles[1]
    assert "grandchild" in roles[2]


@needs_codex
def test_surf_168_codex_subagent_effort_reaches_the_child(codex_host: _CodexHost) -> None:
    efforts: dict[bool, set[str | None]] = {}
    for written in (False, True):
        stub = _codex_spawn_stub()
        key = 'default_subagent_reasoning_effort = "low"\n' if written else ""
        codex_host.exec(f'model_reasoning_effort = "high"\n[agents]\nmax_depth = 1\n{key}', stub)
        efforts[written] = {
            (exchange.body.get("reasoning") or {}).get("effort")
            for exchange in stub.exchanges
            if exchange.role == "child"
        }
    assert efforts == {False: {"high"}, True: {"low"}}


@needs_codex
def test_surf_168_codex_mcp_table_spawns_its_server(codex_host: _CodexHost) -> None:
    sink = codex_host.root / "server-env"
    codex_host.exec(
        "[mcp_servers.probe]\n"
        'command = "sh"\n'
        f'args = ["-c", "env > \'{sink}\'"]\n'
        'env = { PROBE_GRANT = "granted" }\n'
        '__eawf_owner = "eawf"\n'
        '__eawf_managed_at = "1970-01-01T00:00:00+00:00"\n',
        _plain_codex_stub(),
    )
    assert "PROBE_GRANT=granted" in sink.read_text("utf-8")


@needs_codex
def test_surf_168_codex_bare_plugin_key_loads_nothing(codex_host: _CodexHost) -> None:
    skill = "---\nname: probe-skill\ndescription: PROBE_SKILL_MARKER\n---\nbody\n"
    plugin = codex_host.home / "plugins" / "eawf"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(
            {"name": "eawf", "version": "0.0.0", "description": "probe", "skills": "./skills/"}
        ),
        "utf-8",
    )
    (plugin / "skills" / "probe-skill").mkdir(parents=True)
    (plugin / "skills" / "probe-skill" / "SKILL.md").write_text(skill, "utf-8")
    assert "PROBE_SKILL_MARKER" not in codex_host.prompt_input("[plugins.eawf]\nenabled = true\n")
    # The same skill under the host's own skill root is visible, so the probe
    # would have seen the plugin's skill had the key loaded it.
    (codex_host.home / "skills" / "probe-skill").mkdir(parents=True)
    (codex_host.home / "skills" / "probe-skill" / "SKILL.md").write_text(skill, "utf-8")
    assert "PROBE_SKILL_MARKER" in codex_host.prompt_input("")


# --- Claude Code ------------------------------------------------------------------------------


@needs_claude
def test_surf_168_claude_session_hooks_fire(claude_host: _ClaudeHost) -> None:
    sink = claude_host.root / "events"
    claude_host.settings(
        {
            "__eawf_managed": {"version": "probe"},
            "hooks": _recording_hooks(["SessionStart", "Stop"], sink),
        }
    )
    claude_host.run("hello", _claude_stub([]))
    assert set(_fired(sink)) == {"SessionStart", "Stop"}


@needs_claude
def test_surf_168_claude_subagent_hooks_fire(claude_host: _ClaudeHost) -> None:
    sink = claude_host.root / "events"
    claude_host.settings({"hooks": _recording_hooks(["SubagentStart", "SubagentStop"], sink)})
    claude_host.run("PROBE", _claude_stub([_agent(1, "CHILD say done")]))
    assert _fired(sink) == ["SubagentStart", "SubagentStop"]


@needs_claude
def test_surf_168_claude_permission_request_hook_fires(claude_host: _ClaudeHost) -> None:
    sink = claude_host.root / "events"
    claude_host.settings({"hooks": _recording_hooks(["PermissionRequest"], sink)})
    touch = _tool(1, "Bash", {"command": "touch probe-file", "description": "probe"})
    claude_host.run("PROBE", _claude_stub([touch]))
    assert _fired(sink) == ["PermissionRequest"]


@needs_claude
def test_surf_168_claude_concurrency_cap_limits_children(claude_host: _ClaudeHost) -> None:
    fan_out = [_agent(index, f"CHILD {index} say done") for index in range(3)]
    children: dict[str | None, int] = {}
    for cap in (None, "1"):
        claude_host.settings(
            {"env": {} if cap is None else {"CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": cap}}
        )
        stub = _claude_stub(fan_out)
        claude_host.run("PROBE", stub)
        children[cap] = sum(exchange.role == "child" for exchange in stub.exchanges)
    assert children == {None: 3, "1": 1}


@needs_claude
def test_surf_168_claude_spawn_depth_strips_the_child_agent_tool(claude_host: _ClaudeHost) -> None:
    offered: dict[str | None, bool] = {}
    for depth in (None, "1"):
        claude_host.settings(
            {"env": {} if depth is None else {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": depth}}
        )
        stub = _claude_stub([_agent(1, "CHILD say done")])
        claude_host.run("PROBE", stub)
        offered[depth] = any(
            "Agent" in exchange.tools for exchange in stub.exchanges if exchange.role == "child"
        )
    assert offered == {None: True, "1": False}


@needs_claude
def test_surf_168_claude_mcp_entry_spawns_its_server(claude_host: _ClaudeHost) -> None:
    sink = claude_host.root / "server-env"
    claude_host.settings({"enableAllProjectMcpServers": True})
    entry = {
        "command": "sh",
        "args": ["-c", f"env > '{sink}'"],
        "env": {"PROBE_GRANT": "granted"},
        # Claude reads the transport from ``type``; an invalid ``transport``
        # still yields a stdio server, so the key has no effect.
        "transport": "http",
        "__eawf_owner": "eawf",
        "__eawf_managed_at": "1970-01-01T00:00:00+00:00",
    }
    (claude_host.project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"probe": entry}}), "utf-8"
    )
    claude_host.run("hello", _claude_stub([]))
    assert "PROBE_GRANT=granted" in sink.read_text("utf-8")


@needs_claude
def test_surf_168_claude_status_line_runs(claude_host: _ClaudeHost) -> None:
    marker = claude_host.root / "status-line-ran"
    claude_host.settings({"statusLine": {"type": "command", "command": f"touch '{marker}'"}})
    (claude_host.root / "config" / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "customApiKeyResponses": {"approved": [_STUB_API_KEY], "rejected": []},
                "projects": {str(claude_host.project): {"hasTrustDialogAccepted": True}},
            }
        ),
        "utf-8",
    )
    # The status line only draws in an interactive session, so the probe
    # drives one on a pseudo-terminal until the command runs.
    controller, terminal = pty.openpty()
    with _serve(_claude_stub([])) as base_url:
        session = subprocess.Popen(
            ["claude"],
            cwd=claude_host.project,
            env={**claude_host.env(base_url), "TERM": "xterm-256color"},
            stdin=terminal,
            stdout=terminal,
            stderr=terminal,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline and not marker.exists():
                ready, _, _ = select.select([controller], [], [], 0.2)
                if ready:
                    os.read(controller, 65536)
        finally:
            session.kill()
            session.wait()
            os.close(terminal)
            os.close(controller)
    assert marker.exists()
