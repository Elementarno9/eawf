"""Integration test mirroring the manual coauthor verification procedure.

The companion doc :doc:`docs/architecture/coauthor.md` describes how an
operator confirms that a real ``git commit`` carries the
``Co-Authored-By`` trailer. This test exercises the deterministic core
of that procedure: run ``eawf coauthor resolve`` in a fresh tmp
workspace and assert the returned trailer line is the canonical
runtime identity. A failure here means the resolver itself is broken,
so any missing-trailer symptom in a real commit is a hook-wiring issue
rather than a policy-layer one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.config import layered

runner = CliRunner()

_CLAUDE_TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
_CODEX_TRAILER = "Co-Authored-By: Codex <noreply@openai.com>"


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a tmp repo with no overlays so defaults drive resolution."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    fake_global = tmp_path / "global.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.chdir(repo)
    yield repo


def test_coauthor_resolve_default_runtime_returns_claude_trailer(
    repo_root: Path,
) -> None:
    """Default config + no overrides resolves to the canonical Claude trailer."""
    result = runner.invoke(app, ["--json", "coauthor", "resolve"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["mode"] == "runtime"
    assert body["runtime"] == "claude"
    assert body["trailer"] == _CLAUDE_TRAILER
    assert body["required"] is True


def test_coauthor_resolve_text_mode_emits_trailer_only(repo_root: Path) -> None:
    """Without ``--json`` the resolver prints the bare trailer line.

    This is the exact byte sequence the runtime hook appends to a commit
    message, so the manual verification step in
    ``docs/architecture/coauthor.md`` can compare ``git log -1`` against
    this output verbatim.
    """
    result = runner.invoke(app, ["coauthor", "resolve"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == _CLAUDE_TRAILER


def test_coauthor_resolve_runtime_override_returns_codex_trailer(
    repo_root: Path,
) -> None:
    """``--runtime codex`` overrides ``default_runtime`` cleanly."""
    result = runner.invoke(
        app,
        ["--json", "coauthor", "resolve", "--runtime", "codex"],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["runtime"] == "codex"
    assert body["trailer"] == _CODEX_TRAILER
