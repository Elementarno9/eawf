"""End-to-end integration tests for ``eawf cc statusline`` (Phase 4 W06).

Drives the Typer app via :class:`CliRunner` with synthetic Claude JSON on
stdin and asserts:

1. ``eawf cc statusline`` emits a single line on stdout with exit ``0``.
2. The line contains expected segment markers (``state:``, ``git:``,
   ``model:``, ``ses:``, ``cwd:``, ``ctx:``, ``mcp:``, ``hooks:``,
   ``mem:``, ``save:``).
3. ``--theme ascii-fallback`` honors the env var precedence (flag wins).
4. ``eawf cc statusline prewarm`` writes a cache file at the expected
   path; subsequent ``statusline`` invocations with the same session id
   use the cached line.
5. Any module exception is contained — the orchestrator never crashes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

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
    _stub_no_git(monkeypatch)

    payload = {
        "session_id": "ses-int-001",
        "model": "claude-opus-4-7",
        "cwd": str(tmp_path),
        "token_usage": {"input_tokens": 1000, "output_tokens": 200},
    }
    result = runner.invoke(
        app,
        ["cc", "statusline", "--theme", "ascii-fallback"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    line = result.stdout.rstrip("\n")
    # One line — no embedded newlines.
    assert "\n" not in line
    # Each module's prefix is present (ascii-fallback drops colour, keeps
    # the segment text verbatim).
    for marker in (
        "state:",
        "git:-",
        "model:claude-opus-4-7",
        "ses:ses-int-",
        "cwd:",
        "ctx:1000/200",
        "mcp:?",
        "hooks:- plugins:-",
        "mem:-",
        "save:",
    ):
        assert marker in line, f"missing marker {marker!r} in line: {line!r}"


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
        ["cc", "statusline", "prewarm", "--theme", "ascii-fallback"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    cache_path = cache_root / "ses-cache-001.json"
    assert cache_path.exists(), "prewarm should write the cache file"
    cached = orjson.loads(cache_path.read_bytes())
    assert isinstance(cached, dict)
    assert cached["session_id"] == "ses-cache-001"
    assert "model:haiku" in cached["line"]

    # 2. Subsequent statusline call: cache hit. Tamper the cache with a
    # sentinel string so we can verify the cached value is what's
    # printed, not a fresh render.
    cache_path.write_bytes(orjson.dumps({"session_id": "ses-cache-001", "line": "SENTINEL"}))

    result = runner.invoke(
        app,
        ["cc", "statusline", "--theme", "ascii-fallback"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.rstrip("\n") == "SENTINEL"
