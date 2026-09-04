"""Unit: the claude adapter spawn argv carries the headless permission mode.

A headless ``claude -p`` spawn has no TTY to answer a permission prompt, so a
default-mode spawn denies Edit / Write / Bash and the jailed executor reports
"blocked on write permissions". The adapter must launch with
``--permission-mode bypassPermissions`` so the tool calls auto-approve; the OS
filesystem jail + the per-wave ``--disallowedTools`` deny-list stay the enforced
boundary. This pins the flag into the built argv without running a real child.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter


class _StopBeforeSpawnError(Exception):
    """Raised by the stubbed ``create_subprocess_exec`` after argv capture."""


def _capture_argv(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Stub ``create_subprocess_exec`` to record argv, then abort the spawn."""
    captured: dict[str, list[str]] = {}

    async def _fake_exec(*argv: str, **_kwargs: Any) -> Any:
        captured["argv"] = list(argv)
        raise _StopBeforeSpawnError

    monkeypatch.setattr(claude_adapter.asyncio, "create_subprocess_exec", _fake_exec)
    return captured


def test_spawn_argv_carries_bypass_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The built argv launches claude with ``--permission-mode bypassPermissions``."""
    captured = _capture_argv(monkeypatch)

    with pytest.raises(_StopBeforeSpawnError):
        asyncio.run(ClaudeAdapter().spawn_session("do the work", model="opus", cwd=str(tmp_path)))

    argv = captured["argv"]
    assert "--permission-mode" in argv, argv
    idx = argv.index("--permission-mode")
    assert argv[idx + 1] == "bypassPermissions", argv
    # The permission flag is a peer of the streaming flags on the same claude
    # argv (it survives any OS-jail wrapping, which only prefixes the list).
    assert "-p" in argv and "stream-json" in argv


def test_spawn_argv_permission_mode_precedes_deny_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The permission mode is set even when a per-wave deny-list is present.

    ``bypassPermissions`` auto-approves tool calls, but Claude still honors the
    explicit ``--disallowedTools`` deny, so the two compose: the wave's denied
    tools stay blocked while everything else runs unprompted.
    """
    captured = _capture_argv(monkeypatch)

    with pytest.raises(_StopBeforeSpawnError):
        asyncio.run(
            ClaudeAdapter().spawn_session(
                "do the work",
                model="opus",
                cwd=str(tmp_path),
                denied_tools=["WebFetch"],
            )
        )

    argv = captured["argv"]
    assert "bypassPermissions" in argv
    assert "--disallowedTools" in argv
    assert "WebFetch" in " ".join(argv)
