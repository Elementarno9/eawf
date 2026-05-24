from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.config import layered
from eawf.surfaces.cli.app import app

runner = CliRunner()


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    fake_global = tmp_path / "global.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.chdir(repo)
    yield repo


def test_coauthor_resolve_json_runtime(repo_root: Path) -> None:
    result = runner.invoke(app, ["--json", "coauthor", "resolve", "--runtime", "codex"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["trailer"] == "Co-Authored-By: Codex <noreply@openai.com>"


def test_coauthor_resolve_disabled_rejects_message_trailer(repo_root: Path) -> None:
    (repo_root / ".ea" / "config.yaml").write_text(
        "vcs:\n  coauthor:\n    mode: disabled\n",
        encoding="utf-8",
    )
    message = repo_root / "COMMIT_EDITMSG"
    message.write_text(
        "subject\n\nCo-Authored-By: Codex <noreply@openai.com>\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["coauthor", "resolve", "--message-file", str(message)],
    )
    # Post C05 § 5.3: VALIDATION_FAILED bucket = VALIDATION_ERROR (2).
    assert result.exit_code == 2
    assert "disabled" in result.output
