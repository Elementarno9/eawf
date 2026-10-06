"""End-to-end integration tests for ``eawf cc statusline`` (Phase 4 W06).

Drives the Typer app via :class:`CliRunner` with synthetic Claude JSON on
stdin and asserts:

1. ``eawf cc statusline`` emits a single line on stdout with exit ``0``.
2. The line contains expected segment markers (``scope:``, ``git:``,
   ``model:``, ``ses:``, ``cwd:``, ``ctx:``, ``cost:``, ``mcp:``, ``hooks:``,
   ``mem:``, ``save:``), each unavailable one naming why.
3. ``--theme ascii-fallback`` honors the env var precedence (flag wins).
4. ``eawf cc statusline prewarm`` writes a cache entry at the expected
   path; subsequent ``statusline`` invocations with the same session id
   use the cached line while it is fresh.
5. Any module exception is contained — the orchestrator never crashes.
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


def _stub_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every git invocation to fail with FileNotFoundError so
    ``git:-`` is the rendered output. Keeps the test independent of the
    surrounding repo state."""

    def fake_run(*_: Any, **__: Any) -> Any:
        raise FileNotFoundError("git stubbed away")

    monkeypatch.setattr(subprocess, "run", fake_run)


@pytest.mark.integration
def test_statusline_e2e_emits_single_line_with_zero_exit(
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
            "ascii-fallback",
        ],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    line = result.stdout.rstrip("\n")
    # One line — no embedded newlines.
    assert "\n" not in line
    # Each module's prefix is present (ascii-fallback drops colour, keeps
    # the segment text verbatim).
    for marker in (
        "scope:n/a(epoch1-undeclared)",
        "budget:n/a(epoch1-undeclared)",
        "git:n/a(not-a-repository)",
        "model:claude-opus-4-7",
        "ses:ses-int-",
        "cwd:",
        "ctx:1.0k/200.0k",
        "cost:n/a(no-cost-reported)",
        "mcp:n/a(epoch1-undeclared)",
        "hooks:0 plugins:n/a(no-plugin-record)",
        "mem:n/a(epoch1-undeclared)",
        "save:0%",
    ):
        assert marker in line, f"missing marker {marker!r} in line: {line!r}"


@pytest.mark.integration
def test_statusline_env_theme_is_honored_when_no_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EAWF_STATUSLINE_THEME`` selects the theme when ``--theme`` is omitted."""
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("EAWF_STATUSLINE_THEME", "ascii-fallback")
    _stub_no_git(monkeypatch)

    payload = {"session_id": "ses-env-theme", "model": "haiku", "cwd": str(tmp_path)}
    result = runner.invoke(
        app, ["--workspace", str(tmp_path), "cc", "statusline"], input=json.dumps(payload)
    )
    assert result.exit_code == 0, result.output
    line = result.stdout.rstrip("\n")
    # ascii-fallback theme: no ANSI escape codes in the rendered line.
    assert "\x1b[" not in line, f"env-selected ascii-fallback theme leaked colour: {line!r}"
    assert "model:haiku" in line


@pytest.mark.integration
def test_statusline_flag_wins_over_env_theme(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--theme`` flag takes precedence over ``EAWF_STATUSLINE_THEME``."""
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("EAWF_STATUSLINE_THEME", "default")
    _stub_no_git(monkeypatch)

    payload = {"session_id": "ses-flag-wins", "model": "haiku", "cwd": str(tmp_path)}
    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "cc", "statusline", "--theme", "ascii-fallback"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    line = result.stdout.rstrip("\n")
    assert "\x1b[" not in line, f"flag override failed; theme leaked colour: {line!r}"


@pytest.mark.integration
def test_statusline_prewarm_writes_cache_and_subsequent_run_hits_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(cache_root))
    monkeypatch.delenv("EAWF_STATUSLINE_THEME", raising=False)
    _stub_no_git(monkeypatch)

    payload = {"session_id": "ses-cache-001", "model": "haiku", "cwd": str(tmp_path)}

    # 1. Prewarm: same render path, but writes the line to cache.
    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "cc", "statusline", "prewarm", "--theme", "ascii-fallback"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    cache_path = cache_root / "ses-cache-001.json"
    assert cache_path.exists(), "prewarm should write the cache file"
    cached = orjson.loads(cache_path.read_bytes())
    assert isinstance(cached, dict)
    assert "model:haiku" in cached["value"]

    # 2. Subsequent statusline call: cache hit. Tamper the cache with a
    # sentinel string (keeping the fresh write stamp) so we can verify the
    # cached value is what's printed, not a fresh render.
    cache_path.write_bytes(orjson.dumps({**cached, "value": "SENTINEL"}))

    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "cc", "statusline", "--theme", "ascii-fallback"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.rstrip("\n") == "SENTINEL"


@pytest.mark.integration
def test_statusline_healthy_call_fits_columns_and_keeps_stderr_empty(tmp_path: Path) -> None:
    """A real process: the line fits ``COLUMNS`` and nothing reaches stderr."""
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
    assert 0 < len(visible) <= 80
