"""The env-scrub floor on win32: a scrubbed child must still be able to start.

The POSIX floor (``HOME``/``TERM``/``USER``/``LOGNAME`` and a ``/usr/bin``
PATH) names nothing a Windows process needs. Without ``SYSTEMROOT`` Winsock
fails to initialise, so every scrubbed Python gate child died with
``WinError 10106`` before running a line.
"""

from __future__ import annotations

import subprocess  # noqa: EAWF024 spawns the real win32 child the backend acts on
import sys

import pytest

from eawf.runtime.sandbox import env_scrub
from eawf.runtime.sandbox.env_scrub import GATE_RUNTIME_LANE, build_child_env, pinned_path

_WIN_BASE_ENV: dict[str, str] = {
    "SystemRoot": "C:\\Windows",
    "COMSPEC": "C:\\Windows\\system32\\cmd.exe",
    "PATHEXT": ".COM;.EXE;.BAT;.CMD",
    "USERPROFILE": "C:\\Users\\someone",
    "APPDATA": "C:\\Users\\someone\\AppData\\Roaming",
    "TEMP": "C:\\Users\\someone\\AppData\\Local\\Temp",
    "PATH": "C:\\hostile;C:\\Windows\\System32",
    "OPENAI_API_KEY": "sk-not-for-the-claude-lane",  # pragma: allowlist secret
    "ANTHROPIC_API_KEY": "sk-ant-claude-lane",  # pragma: allowlist secret
    "GITHUB_TOKEN": "ghp-never-forwarded",
}


@pytest.fixture
def as_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env_scrub.sys, "platform", "win32")


def test_pinned_path_is_unchanged_off_win32() -> None:
    assert pinned_path("linux", _WIN_BASE_ENV) == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert pinned_path("darwin", {}) == "/usr/bin:/bin:/usr/sbin:/sbin"


def test_pinned_path_on_win32_is_the_system_dirs_joined_by_semicolons() -> None:
    path = pinned_path("win32", _WIN_BASE_ENV)
    assert path.split(";")[:2] == ["C:\\Windows\\System32", "C:\\Windows"]
    assert ":" not in path.replace("C:\\", "")


def test_pinned_path_on_win32_defaults_systemroot() -> None:
    assert pinned_path("win32", {}).startswith("C:\\Windows\\System32;")


@pytest.mark.usefixtures("as_win32")
def test_win32_floor_carries_the_os_vars_a_child_needs() -> None:
    env = build_child_env("claude-code", base_env=_WIN_BASE_ENV)
    assert env["SystemRoot"] == "C:\\Windows"
    assert env["COMSPEC"].endswith("cmd.exe")
    assert env["PATHEXT"].startswith(".COM")
    assert env["USERPROFILE"] == "C:\\Users\\someone"
    assert env["APPDATA"].endswith("Roaming")
    assert env["TEMP"].endswith("Temp")
    assert env["PATH"].startswith("C:\\Windows\\System32;")


@pytest.mark.usefixtures("as_win32")
def test_win32_floor_still_drops_the_parent_path_and_foreign_credentials() -> None:
    env = build_child_env("claude-code", base_env=_WIN_BASE_ENV)
    assert "C:\\hostile" not in env["PATH"]
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-claude-lane"  # pragma: allowlist secret


@pytest.mark.usefixtures("as_win32")
def test_win32_extra_path_dir_is_prepended_with_os_pathsep() -> None:
    env = build_child_env("codex", base_env=_WIN_BASE_ENV, extra_path_dir="C:\\npm")
    assert env["PATH"].startswith("C:\\npm" + env_scrub.os.pathsep)


@pytest.mark.skipif(sys.platform != "win32", reason="starts a real win32 child")
def test_a_scrubbed_python_child_starts_on_win32() -> None:
    env = build_child_env(GATE_RUNTIME_LANE)
    result = subprocess.run(
        [sys.executable, "-c", "import asyncio, socket; socket.socket().close(); print('ok')"],
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout.decode().strip() == "ok"
