"""A stand-in ``codex`` that speaks the app-server JSON-RPC protocol over stdio.

Tests install it as an executable (see :func:`install_fake_codex`) and point
the adapter's ``cli_binary`` at it, so the Codex SDK client, started on the
launcher's per-Run wrapper, talks to this script and never to the real
``codex`` binary, the SDK's bundled copy or the network. It answers ``initialize``,
``thread/start``, ``turn/start`` and ``turn/interrupt`` with results shaped
as the 0.159.2 schema requires, and after ``turn/start`` plays a scripted
turn read from ``fake-codex-script.json`` in its working directory. It
records what it was started with in that directory: ``argv.json``,
``env.json`` and ``fake.pgid``, every message it was sent in
``received.jsonl``, and the ``thread/start`` parameters in
``thread-start.json``.

Script keys, all optional:

- ``thread_id``: the thread ``thread/start`` opens.
- ``user_agent``: the ``initialize`` user agent, which names the version.
- ``refuse``: a method answered with a JSON-RPC error carrying this text,
  as ``{"method": ..., "message": ...}``.
- ``lines``: what the turn prints after ``turn/start``, each an object or
  a raw string printed verbatim.
- ``stderr``: text written to stderr once the lines are out.
- ``exit_code``: exit right after the lines with this status.
- ``on_interrupt``: lines printed after answering ``turn/interrupt``.
- ``sync_managed_preferences``: when true, ``thread/start`` first reads
  and synchronizes the ``com.openai.codex`` preference domain, as codex
  does on macOS before it loads any configuration, and is refused with
  codex's own message when the synchronize fails.

Without ``exit_code`` the fake keeps reading stdin, so it answers an
interrupt while the turn hangs and exits when its stdin closes.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Final

#: The file a test writes the turn's script to, in the fake's working directory.
SCRIPT_FILENAME: Final = "fake-codex-script.json"

#: The thread the fake opens unless the script names another.
THREAD_ID: Final = "thread-fake-codex"

#: The one turn the fake starts.
TURN_ID: Final = "turn-fake-codex"

#: The version the fake reports, in its user agent and on its threads.
VERSION: Final = "0.159.2"

#: The user agent ``initialize`` answers with, spelled as codex spells it.
USER_AGENT: Final = f"codex_cli_rs/{VERSION} (Mac OS 15.5.0; arm64) unknown (eawf; 1.0.0)"


def install_fake_codex(directory: Path, script: dict[str, Any] | None = None) -> Path:
    """Write this fake as an executable ``codex`` in *directory*.

    Args:
        directory: Where the executable goes; also the server's working
            directory, so the script is written beside it.
        script: The turn's script; the default turn when ``None``.

    Returns:
        The executable to set as the adapter's ``cli_binary``.
    """
    executable = directory / "codex"
    body = Path(__file__).read_text(encoding="utf-8")
    executable.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    if script is not None:
        (directory / SCRIPT_FILENAME).write_text(json.dumps(script), encoding="utf-8")
    return executable


def agent_message(text: str, item_id: str = "msg-1", phase: str | None = None) -> dict[str, Any]:
    """Return an ``item/completed`` notification for one agent message."""
    return {
        "method": "item/completed",
        "params": {
            "completedAtMs": 0,
            "item": {"id": item_id, "phase": phase, "text": text, "type": "agentMessage"},
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
        },
    }


def token_usage(input_tokens: int, output_tokens: int, cached: int = 0) -> dict[str, Any]:
    """Return a ``thread/tokenUsage/updated`` notification with this running total."""
    breakdown = {
        "cachedInputTokens": cached,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "reasoningOutputTokens": 0,
        "totalTokens": input_tokens + output_tokens,
    }
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "tokenUsage": {"last": breakdown, "total": breakdown},
        },
    }


def turn_completed(status: str = "completed", error: str | None = None) -> dict[str, Any]:
    """Return a ``turn/completed`` notification ending the turn with *status*."""
    return {
        "method": "turn/completed",
        "params": {
            "threadId": THREAD_ID,
            "turn": {
                "id": TURN_ID,
                "items": [],
                "status": status,
                "error": None if error is None else {"message": error},
            },
        },
    }


def error_notice(message: str, *, will_retry: bool = False) -> dict[str, Any]:
    """Return an ``error`` notification the server sends before a failed turn ends."""
    return {
        "method": "error",
        "params": {
            "error": {"message": message},
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "willRetry": will_retry,
        },
    }


#: The preference domain codex reads its forced (MDM) policy from.
MANAGED_PREFERENCES_DOMAIN: Final = "com.openai.codex"

#: The refusal codex answers ``thread/start`` with when that domain cannot sync.
MANAGED_PREFERENCES_REFUSAL: Final = (
    "failed to load configuration: Failed to synchronize managed preferences"
)


#: The forced key codex reads before it synchronizes the domain.
_MANAGED_PREFERENCES_KEY: Final = "requirements_toml_base64"


def _managed_preferences_synchronize() -> bool:
    """Return whether codex's preference domain synchronizes after a read, as codex does it.

    The read comes first because it is what fails the preferences daemon
    connection under a profile that cannot reach it; a synchronize alone
    still reports success there.
    """
    import ctypes

    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    core.CFStringCreateWithCString.restype = ctypes.c_void_p
    core.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    core.CFPreferencesCopyAppValue.restype = ctypes.c_void_p
    core.CFPreferencesCopyAppValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    core.CFPreferencesAppSynchronize.restype = ctypes.c_ubyte
    core.CFPreferencesAppSynchronize.argtypes = [ctypes.c_void_p]
    utf8 = 0x08000100
    domain = core.CFStringCreateWithCString(None, MANAGED_PREFERENCES_DOMAIN.encode(), utf8)
    key = core.CFStringCreateWithCString(None, _MANAGED_PREFERENCES_KEY.encode(), utf8)
    core.CFPreferencesCopyAppValue(key, domain)
    return bool(core.CFPreferencesAppSynchronize(domain))


def _default_script() -> dict[str, Any]:
    """Return a turn that says one thing, reports its usage and completes."""
    return {"lines": [agent_message("done"), token_usage(3, 1), turn_completed()]}


def _emit(payload: dict[str, Any] | str) -> None:
    """Print one stdout line and flush it."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _thread_result(params: dict[str, Any], thread_id: str) -> dict[str, Any]:
    """Return a ``thread/start`` result for *params*, as the schema requires it."""
    cwd = str(params.get("cwd") or os.getcwd())
    sandbox = {"read-only": "readOnly", "workspace-write": "workspaceWrite"}.get(
        str(params.get("sandbox")), "dangerFullAccess"
    )
    return {
        "approvalPolicy": params.get("approvalPolicy") or "on-request",
        "approvalsReviewer": "user",
        "cwd": cwd,
        "model": params.get("model") or "fixture-model",
        "modelProvider": "openai",
        "sandbox": {"type": sandbox},
        "thread": {
            "cliVersion": VERSION,
            "createdAt": 0,
            "cwd": cwd,
            "ephemeral": False,
            "id": thread_id,
            "modelProvider": "openai",
            "preview": "",
            "projectId": None,
            "sessionId": thread_id,
            "source": "appServer",
            "status": {"type": "idle"},
            "turns": [],
            "updatedAt": 0,
        },
    }


def _play(lines: list[Any], script: dict[str, Any]) -> None:
    """Print *lines*, then the stderr and exit the script asks for."""
    for line in lines:
        _emit(line)
    if "stderr" in script:
        sys.stderr.write(str(script["stderr"]) + "\n")
        sys.stderr.flush()
    if "exit_code" in script:
        sys.exit(int(script["exit_code"]))


def _answer(message: dict[str, Any], script: dict[str, Any], cwd: Path) -> None:
    """Answer one request, then play whatever the script attaches to it."""
    method = message["method"]
    params = message.get("params") or {}
    refusal = script.get("refuse") or {}
    if refusal.get("method") == method:
        _emit({"id": message["id"], "error": {"code": -32600, "message": refusal["message"]}})
        return
    if (
        method == "thread/start"
        and script.get("sync_managed_preferences")
        and not _managed_preferences_synchronize()
    ):
        _emit(
            {"id": message["id"], "error": {"code": -32600, "message": MANAGED_PREFERENCES_REFUSAL}}
        )
        return
    thread_id = str(script.get("thread_id", THREAD_ID))
    if method == "initialize":
        result: dict[str, Any] = {
            "codexHome": str(cwd),
            "platformFamily": "unix",
            "platformOs": "macos",
            "userAgent": str(script.get("user_agent", USER_AGENT)),
        }
    elif method == "thread/start":
        (cwd / "thread-start.json").write_text(json.dumps(params), encoding="utf-8")
        result = _thread_result(params, thread_id)
    elif method == "turn/start":
        result = {"turn": {"id": TURN_ID, "items": [], "status": "inProgress"}}
    else:
        result = {}
    _emit({"id": message["id"], "result": result})
    if method == "turn/start":
        _play(script.get("lines", []), script)
    elif method == "turn/interrupt":
        _play(script.get("on_interrupt", []), {})


def main() -> None:
    """Answer the client until stdin closes."""
    cwd = Path.cwd()
    (cwd / "argv.json").write_text(json.dumps(sys.argv), encoding="utf-8")
    (cwd / "env.json").write_text(json.dumps(dict(os.environ)), encoding="utf-8")
    (cwd / "fake.pgid").write_text(str(os.getpgid(0)), encoding="utf-8")
    script_path = cwd / SCRIPT_FILENAME
    script = _default_script()
    if script_path.exists():
        script = json.loads(script_path.read_text(encoding="utf-8"))
    for raw in sys.stdin:
        if not raw.strip():
            continue
        message = json.loads(raw)
        with (cwd / "received.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message) + "\n")
        if "method" in message and "id" in message:
            _answer(message, script, cwd)


if __name__ == "__main__":
    main()
