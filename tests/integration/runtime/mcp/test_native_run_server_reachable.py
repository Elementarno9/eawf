"""The per-Run MCP server a native Claude Run is configured with starts and answers.

The launcher is driven for real against the fake ``claude``, which records
the command line the Agent SDK built and the environment the child was
started with. The server entry is then started exactly as Claude Code
starts a stdio MCP server: the entry's command and arguments, the child's
environment, and the child's working directory. A stand-in daemon listens
on the runtime directory the launcher pinned, so the server connects the
way it does in a real Run, and no ``claude`` binary or model is reached.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from eawf import __version__
from eawf.runtime.mcp.native_launch import MCP_ARTIFACT_DIRNAME
from eawf.runtime.mcp.semantic_stdio import SERVER_NAME
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter, ClaudeNativeLauncher
from tests.fixtures.runtime.fake_claude_cli import install_fake_claude
from tests.unit.runtime.runtimes.claude.test_sdk_launcher import _bypass_jail, _request

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="the fake child is a shebang script"),
]

#: How long the server is given to answer its frames.
_SERVER_SECONDS: Final = 60

#: The frames Claude Code opens a stdio MCP session with.
_HANDSHAKE: Final = (
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "0"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
)


class _StandInDaemon:
    """A daemon socket that answers ``daemon.ping`` and records each method.

    Attributes:
        methods: The methods called, in arrival order.
    """

    def __init__(self, runtime_dir: Path) -> None:
        """Listen on *runtime_dir*'s daemon socket."""
        self.methods: list[str] = []
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(runtime_dir / "eawfd.sock"))
        self._listener.listen()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._answer, args=(connection,), daemon=True).start()

    def _answer(self, connection: socket.socket) -> None:
        with connection, connection.makefile("rb") as reader:
            for line in reader:
                request = json.loads(line)
                self.methods.append(request["method"])
                result = {"pid": os.getpid(), "version": __version__}
                answer = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                connection.sendall(json.dumps(answer).encode() + b"\n")

    def close(self) -> None:
        """Stop listening."""
        self._listener.close()


@pytest.fixture
def runtime_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Return a daemon runtime directory short enough for a socket path."""
    path = Path(tempfile.mkdtemp(prefix="eawf-mcp-", dir="/tmp"))
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(path))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_dir: Path
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    """Launch one Run on the fake and return its workspace, server entry and env."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", _bypass_jail)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # A checkout that holds an ``eawf`` package must not shadow the server.
    (workspace / "eawf").mkdir()
    (workspace / "eawf" / "__init__.py").write_text(
        "raise SystemExit('the workspace shadowed eawf')\n", encoding="utf-8"
    )
    adapter = ClaudeAdapter()
    adapter.cli_binary = str(install_fake_claude(workspace))

    async def launch_and_turn() -> None:
        outcome = await ClaudeNativeLauncher(adapter=adapter).launch(_request(workspace))
        assert outcome.turn is not None
        await outcome.turn

    asyncio.run(launch_and_turn())
    argv = json.loads((workspace / "argv.json").read_text(encoding="utf-8"))
    document = json.loads(argv[argv.index("--mcp-config") + 1])
    env = json.loads((workspace / "env.json").read_text(encoding="utf-8"))
    return workspace, document["mcpServers"][SERVER_NAME], env


def _serve(
    workspace: Path, entry: dict[str, Any], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Start the server entry as Claude Code would and play the handshake."""
    return subprocess.run(
        [entry["command"], *entry["args"]],
        input="".join(json.dumps(frame) + "\n" for frame in _HANDSHAKE),
        capture_output=True,
        text=True,
        env={**env, **entry["env"]},
        cwd=workspace,
        timeout=_SERVER_SECONDS,
        check=False,
    )


def test_the_configured_server_starts_in_the_child_env_and_lists_the_grant(
    launched: tuple[Path, dict[str, Any], dict[str, str]], runtime_dir: Path
) -> None:
    workspace, entry, env = launched
    daemon = _StandInDaemon(runtime_dir)
    try:
        served = _serve(workspace, entry, env)
    finally:
        daemon.close()

    frames = [json.loads(line) for line in served.stdout.splitlines()]
    assert served.returncode == 0, served.stderr
    assert [frame["id"] for frame in frames] == [1, 2]
    assert frames[0]["result"]["serverInfo"]["name"] == SERVER_NAME
    assert [tool["name"] for tool in frames[1]["result"]["tools"]] == [
        "budget_status",
        "submit_candidate",
    ]
    assert "daemon.ping" in daemon.methods


def test_the_server_runs_on_the_daemon_interpreter_and_its_runtime_dir(
    launched: tuple[Path, dict[str, Any], dict[str, str]], runtime_dir: Path
) -> None:
    workspace, entry, env = launched

    binding = json.loads(
        (workspace / MCP_ARTIFACT_DIRNAME / "binding.json").read_text(encoding="utf-8")
    )

    assert entry["command"] == sys.executable
    assert entry["args"][:3] == ["-I", "-m", "eawf"]
    assert binding["runtime_dir"] == str(runtime_dir)
    # The child's PATH alone would not find this interpreter's ``eawf``.
    assert shutil.which("eawf", path=env["PATH"]) != str(Path(sys.executable).parent / "eawf")


def test_with_no_daemon_the_server_exits_without_spawning_one(
    launched: tuple[Path, dict[str, Any], dict[str, str]], runtime_dir: Path
) -> None:
    workspace, entry, env = launched
    before = sorted(path.name for path in runtime_dir.iterdir())

    served = _serve(workspace, entry, env)

    assert served.returncode != 0
    assert served.stdout == ""
    assert sorted(path.name for path in runtime_dir.iterdir()) == before
