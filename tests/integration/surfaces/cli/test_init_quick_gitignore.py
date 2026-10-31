"""``eawf init --quick`` in an empty git repository.

The quick path must leave a managed ignore block that keeps ``.ea/local``
out of git without carrying eawf's own promoted release evidence, and must
report the state file it wrote: the born generation's document, since a
native tree never writes ``.ea/state.json``.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.platform.install.repository_row import RepositoryRowOutcome
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()

_BEGIN = "# BEGIN EAWF:gitignore"
_END = "# END EAWF:gitignore"


def _git_repo(root: Path) -> Path:
    """Return *root* as an empty git repository."""
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _quick_init(target: Path) -> Result:
    """Run ``eawf --json init --quick --target <target>``."""
    return runner.invoke(app, ["--json", "init", "--quick", "--target", str(target)])


def _managed_block(target: Path) -> list[str]:
    """Return the lines between the managed markers of *target*'s ignore file."""
    lines = (target / ".gitignore").read_text(encoding="utf-8").splitlines()
    return lines[lines.index(_BEGIN) + 1 : lines.index(_END)]


def _check_ignore(target: Path, path: str) -> int:
    """Return the exit status of ``git check-ignore`` for *path* (0 = ignored)."""
    return subprocess.run(
        ["git", "-C", str(target), "check-ignore", "-q", path], check=False
    ).returncode


def test_quick_init_block_ignores_ea_local(tmp_path: Path) -> None:
    target = _git_repo(tmp_path / "repo")
    result = _quick_init(target)
    assert result.exit_code == exit_codes.OK, result.output
    scratch = target / ".ea" / "local" / "scratch.md"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text("x\n", encoding="utf-8")
    assert ".ea/local/" in _managed_block(target)
    assert _check_ignore(target, ".ea/local/scratch.md") == 0


def test_quick_init_block_carries_no_eawf_release_evidence(tmp_path: Path) -> None:
    target = _git_repo(tmp_path / "repo")
    assert _quick_init(target).exit_code == exit_codes.OK
    block = _managed_block(target)
    assert not [line for line in block if line.startswith("!.ea/artifacts/evidence")]
    assert not [line for line in block if re.search(r"20\d\d-\d\d-\d\d", line)]


def test_quick_init_reports_the_generation_document_it_wrote(tmp_path: Path) -> None:
    target = _git_repo(tmp_path / "repo")
    result = _quick_init(target)
    assert result.exit_code == exit_codes.OK, result.output
    payload = json.loads(result.output)
    reported = Path(payload["state_path"])
    expected = target / ".ea" / GENERATIONS_DIRNAME / payload["generation_id"] / GENERATION_DOCUMENT
    assert reported.resolve() == expected.resolve()
    assert reported.is_file()
    assert not (target / ".ea" / "state.json").exists()


def test_quick_init_rerun_refuses_and_keeps_one_block(tmp_path: Path) -> None:
    target = _git_repo(tmp_path / "repo")
    assert _quick_init(target).exit_code == exit_codes.OK
    before = (target / ".gitignore").read_bytes()
    rerun = _quick_init(target)
    assert rerun.exit_code == exit_codes.USER_ERROR, rerun.output
    assert "already initialised" in json.loads(rerun.output)["message"]
    assert (target / ".gitignore").read_bytes() == before
    assert before.decode("utf-8").count(_BEGIN) == 1


def test_quick_init_refresh_keeps_one_identical_block(tmp_path: Path) -> None:
    target = _git_repo(tmp_path / "repo")
    assert _quick_init(target).exit_code == exit_codes.OK
    before = (target / ".gitignore").read_text(encoding="utf-8")
    refresh = runner.invoke(app, ["init", "--refresh-gitignore", "--target", str(target)])
    assert refresh.exit_code == exit_codes.OK, refresh.output
    after = (target / ".gitignore").read_text(encoding="utf-8")
    assert after == before
    assert after.count(_BEGIN) == 1


def test_quick_init_outside_git_writes_the_block_and_admits_no_row(tmp_path: Path) -> None:
    target = tmp_path / "plain"
    target.mkdir()
    result = _quick_init(target)
    assert result.exit_code == exit_codes.OK, result.output
    payload = json.loads(result.output)
    assert payload["repository_row"]["outcome"] == RepositoryRowOutcome.NOT_A_GIT_REPOSITORY.value
    assert ".ea/local/" in _managed_block(target)
