"""A tree ``eawf init`` bears holds its project row, and what reads that row finds it.

Every project-scoped write files its record under the tree's one project,
the doctor's branch check compares against the project's default branch,
and the console's first run registers the tree under the project's code.
A born tree writes no v1 ``state.json``, so each of them reads the row the
born generation holds.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.models import Project
from eawf.kernel.store.commit_policy import PROMOTED_ARTIFACTS
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.doctor.checks import check_branch_currency
from eawf.runtime.daemon.epoch2_root import attach_root_context
from eawf.runtime.daemon.methods.memory import MemoryAdd, add_note, project_subject
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.tui.console.onboarding import project_code

pytestmark = pytest.mark.integration

runner = CliRunner()

_CODE = "RVT"

#: The branch the old v1 init recorded and a born project still delivers into.
_BRANCH = "main"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _init(repo: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> dict[str, Any]:
    """Run ``eawf --json --no-input init`` in *repo* and return its payload."""
    monkeypatch.delenv("EA_STATE", raising=False)
    result = runner.invoke(
        app,
        [
            "--json",
            "--no-input",
            "init",
            "--project-code",
            _CODE,
            "--profile",
            "core",
            "--target",
            str(repo),
            *extra,
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def _document(repo: Path) -> tuple[Path, dict[str, Any]]:
    authority = resolve_authority(repo / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    return path, read_document(path)


def test_init_writes_the_project_row_with_its_code_and_default_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path, monkeypatch, "--project-title", "Revet")
    _, document = _document(tmp_path)
    rows = document[Epoch2Collection.PROJECT.value]
    assert list(rows) == [_CODE]
    project = Project.model_validate(rows[_CODE])
    assert (project.code, project.title, project.default_branch) == (
        _CODE,
        "Revet",
        _BRANCH,
    )


def test_init_without_a_title_titles_the_project_by_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path, monkeypatch)
    _, document = _document(tmp_path)
    assert document[Epoch2Collection.PROJECT.value][_CODE]["title"] == _CODE


def test_init_reports_the_generation_document_as_its_state_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _init(tmp_path, monkeypatch)
    path, _ = _document(tmp_path)
    assert payload["state_path"] == str(path)
    assert path.parent.name == payload["generation_id"]
    assert Path(payload["state_path"]).is_file()
    assert not (tmp_path / ".ea" / "state.json").exists()


def test_a_project_scoped_write_succeeds_right_after_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path, monkeypatch)
    context = attach_root_context({}, tree_root=tmp_path / ".ea", daemon_wal_dir=tmp_path / "wal")
    assert project_subject(context).endswith(f"/project/{_CODE}")
    note = add_note(context, MemoryAdd(scope_id=_CODE, title="t", body="b"), now=datetime.now(UTC))
    assert note.scope_id == _CODE


def test_a_fresh_init_gitignore_carries_no_eawf_release_evidence_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _init(tmp_path, monkeypatch, "--quick")
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    for row in PROMOTED_ARTIFACTS:
        assert f"!{row.pattern}" not in text, row.pattern
        assert f"!{row.pattern}" not in payload["gitignore_patterns"], row.pattern
    assert ".ea/artifacts/**" in text.splitlines()


def test_branch_currency_evaluates_on_a_freshly_initialised_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(tmp_path, "init", "--quiet", "--initial-branch", _BRANCH)
    _git(tmp_path, "commit", "--quiet", "--allow-empty", "-m", "seed")
    _git(tmp_path, "update-ref", f"refs/remotes/origin/{_BRANCH}", "HEAD")
    _init(tmp_path, monkeypatch)
    result = check_branch_currency(workspace=tmp_path)
    assert result.detail == (f"local {_BRANCH} is current with origin/{_BRANCH}"), result.detail


def test_onboarding_finds_the_project_code_of_a_freshly_initialised_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path, monkeypatch)
    assert project_code(tmp_path) == _CODE


def test_init_renders_the_project_brief_from_the_born_project_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository authoring ``.ea/rules.yaml`` renders its brief once the row exists."""
    source = tmp_path / ".ea" / "rules.yaml"
    source.parent.mkdir()
    source.write_text(
        yaml.safe_dump({"schema_version": 1, "modules": ["eawf.craft.python"], "rules": []}),
        encoding="utf-8",
    )
    _init(tmp_path, monkeypatch)
    brief = (tmp_path / "AGENTS.md").read_text(encoding="utf-8").splitlines()
    assert f"- Project code `{_CODE}`; domains: general." in brief
    assert f"- Default branch: `{_BRANCH}`." in brief
