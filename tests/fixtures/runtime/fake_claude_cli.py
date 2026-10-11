"""A stand-in ``claude`` CLI that speaks the Agent SDK's stream-json protocol.

Tests install it as an executable (see ``install_fake_claude``) and point the
adapter's ``cli_binary`` at it, so the SDK drives this script and never the
real ``claude`` binary or the network. It answers ``-v``, answers every
control request (``initialize``, ``interrupt``) with success, and replies to
the prompt with a scripted turn read from ``fake-claude-script.json`` in its
working directory. It records what it was started with in that directory:
``argv.json``, ``env.json``, ``fake.pgid`` and ``tools.json``.

It resolves its tools the way the real CLI does: ``--tools`` picks the base
set of built-ins (all of :data:`BUILTIN_TOOLS` when absent or ``default``,
none when empty), each ``--disallowedTools`` value removes names from it,
and the MCP tools named in ``--allowedTools`` are added when an
``--mcp-config`` registers their server. ``tools.json`` records the flags
and the resolved set, and the init message reports the set as ``tools``.

Script keys, all optional:

- ``session_id``: the session the init message announces.
- ``init``: whether the init message is sent before the scripted lines.
- ``init_fields``: fields laid over the init message's defaults, which
  carry the version and model the real CLI reports there.
- ``lines``: what the turn prints, each an object (assistant and result
  objects are completed with the fields the SDK parser requires) or a raw
  string printed verbatim.
- ``stderr``: text written to stderr once the lines are out.
- ``exit_code``: exit right after the lines with this status.
- ``hang``: once stdin closes, sleep instead of exiting, so only a signal
  ends the process.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Final

#: The file a test writes the turn's script to, in the fake's working directory.
SCRIPT_FILENAME: Final = "fake-claude-script.json"

#: The version the fake reports, above the SDK's supported minimum.
VERSION: Final = "2.1.300 (Claude Code)"

#: The model the fake's init message says the session runs.
MODEL: Final = "claude-fixture-4-5"

#: The built-in tools Claude Code 2.1.291 reported in its init message with
#: no tool flag, which is the set a deny-list has to keep up with.
BUILTIN_TOOLS: Final[tuple[str, ...]] = (
    "Task",
    "Bash",
    "CronCreate",
    "CronDelete",
    "CronList",
    "DesignSync",
    "Edit",
    "EnterWorktree",
    "ExitWorktree",
    "ListAgents",
    "Monitor",
    "NotebookEdit",
    "PushNotification",
    "Read",
    "RemoteTrigger",
    "ReportFindings",
    "ScheduleWakeup",
    "SendMessage",
    "Skill",
    "TaskStop",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
)

#: The file the resolved tool set and the flags behind it are recorded in.
TOOLS_FILENAME: Final = "tools.json"


def install_fake_claude(directory: Path, script: dict[str, Any] | None = None) -> Path:
    """Write this fake as an executable ``claude`` in *directory*.

    Args:
        directory: Where the executable goes; also the turn's working
            directory, so the script is written beside it.
        script: The turn's script; the default turn when ``None``.

    Returns:
        The executable to set as the adapter's ``cli_binary``.
    """
    executable = directory / "claude"
    body = Path(__file__).read_text(encoding="utf-8")
    executable.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    if script is not None:
        (directory / SCRIPT_FILENAME).write_text(json.dumps(script), encoding="utf-8")
    return executable


def _emit(payload: dict[str, Any] | str) -> None:
    """Print one stdout line and flush it."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _complete(line: dict[str, Any] | str, session_id: str) -> dict[str, Any] | str:
    """Fill in the fields the SDK parser requires of an assistant or result line."""
    if not isinstance(line, dict):
        return line
    completed = dict(line)
    if completed.get("type") == "assistant":
        message = dict(completed.get("message") or {})
        message.setdefault("model", "fixture-model")
        message.setdefault("content", [])
        completed["message"] = message
    elif completed.get("type") == "result":
        for key, value in (
            ("subtype", "success"),
            ("duration_ms", 1),
            ("duration_api_ms", 1),
            ("is_error", False),
            ("num_turns", 1),
            ("session_id", session_id),
        ):
            completed.setdefault(key, value)
    return completed


def _flag_values(argv: list[str], flag: str) -> list[str]:
    """Return every value *flag* carries in *argv*, in order."""
    return [argv[index + 1] for index, token in enumerate(argv[:-1]) if token == flag]


def _names(value: str) -> list[str]:
    """Split one tool-flag value on the commas and spaces the CLI splits on."""
    return [name for name in value.replace(",", " ").split() if name]


def _resolve_tools(argv: list[str]) -> dict[str, Any]:
    """Return the tool flags in *argv* and the tool set they leave the session."""
    base = _flag_values(argv, "--tools")
    if not base or base[-1] == "default":
        available = list(BUILTIN_TOOLS)
    else:
        available = [name for name in _names(base[-1]) if name in BUILTIN_TOOLS]
    disallowed = [
        name for value in _flag_values(argv, "--disallowedTools") for name in _names(value)
    ]
    available = [name for name in available if name not in disallowed]
    servers: set[str] = set()
    for document in _flag_values(argv, "--mcp-config"):
        servers.update(json.loads(document)["mcpServers"])
    allowed = [name for value in _flag_values(argv, "--allowedTools") for name in _names(value)]
    for name in allowed:
        parts = name.split("__")
        if len(parts) == 3 and parts[0] == "mcp" and parts[1] in servers and name not in disallowed:
            available.append(name)
    return {
        "tools_flag": base,
        "allowed": allowed,
        "disallowed": disallowed,
        "available": available,
    }


def _default_script() -> dict[str, Any]:
    """Return a turn that says one thing and completes."""
    return {
        "lines": [
            {
                "type": "assistant",
                "message": {
                    "id": "m-default",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                },
            },
            {"type": "result", "result": "done", "usage": {"input_tokens": 3, "output_tokens": 1}},
        ]
    }


def _play(script: dict[str, Any], session_id: str, tools: list[str]) -> None:
    """Print the scripted turn, then exit if the script says so."""
    if script.get("init", True):
        init = {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "cwd": os.getcwd(),
            "tools": tools,
            "model": MODEL,
            "permissionMode": "bypassPermissions",
            "claude_code_version": VERSION.split()[0],
        }
        _emit({**init, **script.get("init_fields", {})})
    for line in script.get("lines", []):
        _emit(_complete(line, session_id))
    if "stderr" in script:
        sys.stderr.write(str(script["stderr"]) + "\n")
        sys.stderr.flush()
    if "exit_code" in script:
        sys.exit(int(script["exit_code"]))


def main() -> None:
    """Answer the SDK until stdin closes."""
    if sys.argv[1:] == ["-v"]:
        print(VERSION)
        return
    cwd = Path.cwd()
    (cwd / "argv.json").write_text(json.dumps(sys.argv), encoding="utf-8")
    (cwd / "env.json").write_text(json.dumps(dict(os.environ)), encoding="utf-8")
    (cwd / "fake.pgid").write_text(str(os.getpgid(0)), encoding="utf-8")
    tools = _resolve_tools(sys.argv)
    (cwd / TOOLS_FILENAME).write_text(json.dumps(tools), encoding="utf-8")
    script_path = cwd / SCRIPT_FILENAME
    session_id = "sess-fake-claude"
    script = _default_script()
    if script_path.exists():
        script = json.loads(script_path.read_text(encoding="utf-8"))
        session_id = str(script.get("session_id", session_id))
    for raw in sys.stdin:
        if not raw.strip():
            continue
        message = json.loads(raw)
        if message.get("type") == "control_request":
            _emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": message["request_id"],
                        "response": {},
                    },
                }
            )
        elif message.get("type") == "user":
            _play(script, session_id, tools["available"])
    while script.get("hang"):
        time.sleep(1)


if __name__ == "__main__":
    main()
