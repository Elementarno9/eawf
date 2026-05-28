"""Real-process E2E: ``agent.dispatch`` drives the dispatch runner.

This is the live-log proof for the dispatch-runner production caller: a
*real* ``eawfd`` daemon, spoken to over its actual AF_UNIX socket with a
raw JSON-RPC ``agent.dispatch`` frame, must persist the C09
``runtime_switched`` (on a V5 fallback) + ``dispatch_cost`` events to the
real on-disk ``event.jsonl`` through the daemon canonical writer.

``agent.dispatch`` has no CLI verb (it is RPC-only), so the harness here
seeds a wave-bearing ``state.json`` into the isolated temp repo, spawns a
real detached daemon against it, sends the frame over the socket, and
reads the resulting ``.ea/store/event.jsonl`` rows back. Isolation
(``EAWF_RUNTIME_DIR`` + ``EA_STATE`` + temp cwd) is inherited from the
``e2e_env`` fixture, so the test never touches the developer's live
``~/.eawfd`` nor this repo's real ``.ea/``.
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import E2EEnv

# AF_UNIX + fork-based detach are POSIX-only; the Windows pipe transport
# has its own surface. Gate the whole module off there.
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="E2E tier drives the POSIX UDS daemon; Windows pipe is out of scope",
    ),
]

#: A committed state fixture carrying an active wave (``P01-I01-W01``), so
#: the daemon's ``agent.dispatch`` resolver finds a real wave to dispatch.
_WAVE_STATE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "states"
    / "valid"
    / "03-phase-iter-wave-active.json"
)

_WAVE_ID = "P01-I01-W01"

#: Wall-clock budget for the daemon to expose its socket after spawn.
_DAEMON_READY_TIMEOUT_S: float = 8.0
_POLL_INTERVAL_S: float = 0.05


def _wait_for_socket(sandbox: E2EEnv, deadline: float) -> bool:
    """Poll until the daemon socket accepts a connection or *deadline* passes."""
    while time.monotonic() < deadline:
        if sandbox.sock_file.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    probe.connect(str(sandbox.sock_file))
                    return True
            except OSError:
                pass
        time.sleep(_POLL_INTERVAL_S)
    return False


def _rpc(sock_path: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Send one newline-delimited JSON-RPC frame and return the parsed reply.

    Mirrors the daemon wire format (one JSON object per ``\\n``-terminated
    line). Used because ``agent.dispatch`` has no CLI verb to drive it.

    Args:
        sock_path: Path to the live daemon's AF_UNIX socket.
        method: JSON-RPC method name.
        params: Request params object.

    Returns:
        The parsed JSON-RPC response object.

    Raises:
        AssertionError: When the daemon closes the connection without a
            reply.
    """
    req = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5.0)
        client.connect(str(sock_path))
        client.sendall(json.dumps(req).encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
    assert buf, "daemon closed without replying"
    line = buf.split(b"\n", 1)[0]
    parsed = json.loads(line)
    assert isinstance(parsed, dict)
    return parsed


def _read_event_payloads(
    event_path: Path,
    *,
    event_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return selected ``event.jsonl`` payload dicts in append order.

    Args:
        event_path: Path to the live event store.
        event_ids: Optional envelope ids to keep. When supplied, setup
            events already present in the log stay out of dispatch-specific
            assertions.

    Returns:
        Matching payload dicts in on-disk append order.
    """
    payloads: list[dict[str, Any]] = []
    with event_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                row = json.loads(raw)
                if event_ids is None or row["id"] in event_ids:
                    payloads.append(row["payload"])
    return payloads


def _spawn_wave_daemon(e2e_env: E2EEnv) -> Path:
    """Seed a wave-bearing state, spawn a real daemon, return its event path.

    Args:
        e2e_env: The isolated sandbox (state not yet wave-bearing).

    Returns:
        Path to the real ``event.jsonl`` the daemon writes through.

    Raises:
        AssertionError: When the daemon never exposes its socket within
            :data:`_DAEMON_READY_TIMEOUT_S`.
    """
    shutil.copy(_WAVE_STATE, e2e_env.state_path)
    e2e_env.spawn_daemon()
    deadline = time.monotonic() + _DAEMON_READY_TIMEOUT_S
    if not _wait_for_socket(e2e_env, deadline) or not e2e_env.pid_file.exists():
        log_tail = (
            e2e_env.log_file.read_text(encoding="utf-8", errors="replace")
            if e2e_env.log_file.exists()
            else "(no log file written)"
        )
        raise AssertionError(
            f"daemon did not become ready within {_DAEMON_READY_TIMEOUT_S}s\n"
            f"--- eawfd.log ---\n{log_tail}"
        )
    return e2e_env.state_path.parent / "store" / "event.jsonl"


def test_real_dispatch_emits_runtime_switched_and_cost_to_live_log(e2e_env: E2EEnv) -> None:
    """A real ``agent.dispatch`` V5 fallback persists both C09 events.

    Drives the *actual* daemon over its socket with an outcome carrying a
    ``primary_error`` + ``fallback_runtime``, then reads the real
    ``event.jsonl`` and asserts the ``runtime_switched`` and
    ``dispatch_cost`` rows both landed in append order.
    """
    event_path = _spawn_wave_daemon(e2e_env)

    reply = _rpc(
        e2e_env.sock_file,
        "agent.dispatch",
        {
            "wave_id": _WAVE_ID,
            "runtime": "codex",
            "session_policy": "fresh",
            "outcome": {
                "model": "claude-opus-4-7",
                "input_tokens": 1200,
                "output_tokens": 340,
                "cache_creation_input_tokens": 8000,
                "cache_read_input_tokens": 64000,
                "cost_usd": "0.123456",
                "primary_error": "RUNTIME_RATE_LIMIT",
                "fallback_runtime": "claude-code",
            },
        },
    )

    assert "error" not in reply, reply
    plan = reply["result"]
    assert len(plan["event_ids"]) == 2

    assert event_path.exists(), "daemon never wrote an event.jsonl"
    payloads = _read_event_payloads(event_path, event_ids=set(plan["event_ids"]))
    assert [p["event_type"] for p in payloads] == ["runtime_switched", "dispatch_cost"]
    switched, cost = payloads
    assert switched["wave_id"] == _WAVE_ID
    assert switched["runtime_from"] == "codex"
    assert switched["runtime_to"] == "claude"
    assert switched["cause"] == "RUNTIME_RATE_LIMIT"
    assert cost["wave_id"] == _WAVE_ID
    assert cost["runtime"] == "claude"
    assert cost["cost_usd"] == "0.123456"
    # The switch's to-attempt is the attempt the cost is billed against.
    assert switched["attempt_id_to"] == cost["attempt_id"]


def test_real_dispatch_no_error_emits_only_dispatch_cost(e2e_env: E2EEnv) -> None:
    """A real dispatch with no primary_error persists only ``dispatch_cost``."""
    event_path = _spawn_wave_daemon(e2e_env)

    reply = _rpc(
        e2e_env.sock_file,
        "agent.dispatch",
        {
            "wave_id": _WAVE_ID,
            "runtime": "claude-code",
            "session_policy": "fresh",
            "outcome": {
                "model": "claude-opus-4-7",
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cost_usd": "0.05",
            },
        },
    )

    assert "error" not in reply, reply
    plan = reply["result"]
    assert len(plan["event_ids"]) == 1
    payloads = _read_event_payloads(event_path, event_ids=set(plan["event_ids"]))
    assert [p["event_type"] for p in payloads] == ["dispatch_cost"]
    assert payloads[0]["runtime"] == "claude"
