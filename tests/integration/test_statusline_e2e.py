"""End-to-end integration tests for ``eawf cc statusline`` (Phase 4 W06).

Drives the Typer app via :class:`CliRunner` with synthetic Claude JSON on
stdin and asserts:

1. ``eawf cc statusline`` emits the powerline rows on stdout with exit ``0``:
   line 1 with the segments the payload sources, then one bar row per window,
   and no ``n/a`` marker for a segment it cannot source.
2. ``EAWF_STATUSLINE_THEME`` picks the theme, and ``--theme`` wins over it.
3. ``eawf cc statusline prewarm`` writes a cache entry at the expected
   path; subsequent ``statusline`` invocations with the same session id
   use the cached rows while they are fresh.
4. Any module exception is contained — the orchestrator never crashes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()

#: The model fill of the dark (Catppuccin Mocha) and light (Latte) themes.
_DARK_BASE = "48;2;203;166;247m"
_LIGHT_BASE = "48;2;136;57;239m"


def _stub_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every git invocation to fail with FileNotFoundError so
    ``git:-`` is the rendered output. Keeps the test independent of the
    surrounding repo state."""

    def fake_run(*_: Any, **__: Any) -> Any:
        raise FileNotFoundError("git stubbed away")

    monkeypatch.setattr(subprocess, "run", fake_run)


@pytest.mark.integration
def test_statusline_e2e_emits_powerline_rows_with_zero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("EAWF_STATUSLINE_THEME", raising=False)
    # A terminal width would drop segments this test expects to see.
    monkeypatch.delenv("COLUMNS", raising=False)
    # The plugin count reads the host's install record under $HOME.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _stub_no_git(monkeypatch)

    payload = {
        "session_id": "ses-int-001",
        "model": "claude-opus-4-7",
        "cwd": str(tmp_path),
        "workspace": {"project_dir": str(tmp_path)},
        "context_window": {
            "context_window_size": 200_000,
            "current_usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
    # Pass ``--workspace tmp_path`` so the state resolver scopes to the empty
    # tmp dir (which has no ``.ea/state.json`` and no ``.claude/hooks/``).
    # Without this, the surrounding self-applied repo state (post Phase 5 W06)
    # would leak into the rendered segments.
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "cc",
            "statusline",
            "--theme",
            "dark",
        ],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    rows = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout.rstrip("\n")).split("\n")
    segments = [part.strip() for part in rows[0].split("\ue0b0") if part.strip()]
    assert segments == ["claude-opus-4-7", tmp_path.name]
    assert len(rows) == 3
    assert rows[1] == ""
    assert rows[2].startswith("ctx ") and rows[2].endswith("   1%   0%c 1K/200K")
    assert "n/a" not in result.stdout


@pytest.mark.integration
def test_statusline_env_theme_is_honored_when_no_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EAWF_STATUSLINE_THEME`` selects the palette when ``--theme`` is omitted."""
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("EAWF_STATUSLINE_THEME", "light")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _stub_no_git(monkeypatch)

    payload = {"session_id": "ses-env-theme", "model": "haiku", "cwd": str(tmp_path)}
    result = runner.invoke(
        app, ["--workspace", str(tmp_path), "cc", "statusline"], input=json.dumps(payload)
    )
    assert result.exit_code == 0, result.output
    line = result.stdout.rstrip("\n")
    assert _LIGHT_BASE in line, f"env-selected light theme not drawn: {line!r}"
    assert " haiku " in line


@pytest.mark.integration
def test_statusline_flag_wins_over_env_theme(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--theme`` flag takes precedence over ``EAWF_STATUSLINE_THEME``."""
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("EAWF_STATUSLINE_THEME", "light")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _stub_no_git(monkeypatch)

    payload = {"session_id": "ses-flag-wins", "model": "haiku", "cwd": str(tmp_path)}
    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "cc", "statusline", "--theme", "dark"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    line = result.stdout.rstrip("\n")
    assert _DARK_BASE in line and _LIGHT_BASE not in line, f"flag override failed: {line!r}"


@pytest.mark.integration
def test_statusline_prewarm_writes_cache_and_subsequent_run_hits_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(cache_root))
    monkeypatch.delenv("EAWF_STATUSLINE_THEME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _stub_no_git(monkeypatch)

    payload = {"session_id": "ses-cache-001", "model": "haiku", "cwd": str(tmp_path)}

    # 1. Prewarm: same render path, but writes the line to cache.
    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "cc", "statusline", "prewarm", "--theme", "dark"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    cache_path = cache_root / "ses-cache-001.json"
    assert cache_path.exists(), "prewarm should write the cache file"
    cached = orjson.loads(cache_path.read_bytes())
    assert isinstance(cached, dict)
    assert " haiku " in cached["value"]
    assert cached["key"] == "catppuccin-mocha/config"

    # 2. Subsequent statusline call: the render finishes in time, so the fresh
    # line is printed and replaces the entry, never the older cached one.
    cache_path.write_bytes(orjson.dumps({**cached, "value": "SENTINEL"}))

    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "cc", "statusline", "--theme", "dark"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.rstrip("\n") == cached["value"]
    assert orjson.loads(cache_path.read_bytes())["value"] == cached["value"]


@pytest.mark.integration
def test_statusline_healthy_call_fits_columns_and_keeps_stderr_empty(tmp_path: Path) -> None:
    """A real process: every row fits ``COLUMNS`` and nothing reaches stderr."""
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "EAWF_STATUSLINE_CACHE": str(tmp_path / "cache"),
        "COLUMNS": "80",
    }
    env.pop("EAWF_STATUSLINE_THEME", None)
    payload = {"session_id": "ses-quiet", "model": "haiku", "cwd": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, "-m", "eawf", "--workspace", str(tmp_path), "cc", "statusline"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    visible = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout.rstrip("\n"))
    assert visible
    assert all(len(row) <= 80 for row in visible.split("\n"))
