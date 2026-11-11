"""``eawf reflect run`` outside an eawf-managed repository, and ``--format json --out``.

- In a plain ``git init`` directory the run reports scope ``user``, keeps its
  history index under ``$XDG_CACHE_HOME/eawf/reflect``, writes its pages into a
  new private temporary directory, prints ``pages: <path>`` and exits 0, writing
  nothing into the working directory and calling nothing off the machine.
- ``--format json --out F`` writes the report JSON to F and no text, inside or
  outside a managed repository.
- The reflect skill is supported outside a managed repository.
"""

from __future__ import annotations

import json
import stat
import subprocess  # noqa: EAWF024 the criterion is a run in a real `git init` directory
import tempfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the exit status is the contract

from eawf.observability.reflect.analysis import ANALYSIS_PREFIX, HISTORY_INDEX_FILENAME
from eawf.observability.reflect.report import (
    FillManifest,
    ReflectReport,
    new_output_dir,
    store_report,
    user_reflect_root,
)
from eawf.observability.reflect.scopes import OutputArea
from eawf.surfaces.cli.app import app
from eawf.workflow.skills.catalog import render_skill_classes_tsv, resolve_skill, standalone_class
from tests.contract.surfaces.cli._reflect_tree import native_tree, refuse_egress, run_row

__all__ = ["refuse_egress"]

runner = CliRunner()


def _transcript(path: Path, cwd: Path) -> None:
    rows: list[dict[str, Any]] = [
        {
            "type": "user",
            "sessionId": "elsewhere-session",
            "cwd": str(cwd),
            "timestamp": "2026-11-01T10:00:00Z",
            "message": {"content": "synthetic request"},
        },
        *(
            {
                "type": "assistant",
                "sessionId": "elsewhere-session",
                "timestamp": f"2026-11-01T10:00:0{index + 1}Z",
                "message": {
                    "id": f"elsewhere-m{index}",
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
            for index in range(3)
        ),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows), encoding="utf-8")


def _files(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point home, the caches, the histories and the temporary directory into *tmp_path*."""
    monkeypatch.delenv("EA_STATE", raising=False)
    for name in ("home", "cache", "tmp"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "harness" / "projects"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    yield tmp_path


@pytest.fixture
def scratch(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a fresh ``git init`` directory, the working directory, with no ``.ea``."""
    repo = sandbox / "scratch"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _transcript(sandbox / "harness" / "projects" / "-elsewhere" / "s.jsonl", sandbox / "elsewhere")
    monkeypatch.chdir(repo)
    return repo


def test_outside_repo_pages(scratch: Path, refuse_egress: list[str]) -> None:
    before = _files(scratch)

    result = runner.invoke(app, ["reflect", "run", "--local-only"])

    assert result.exit_code == 0, result.output
    assert refuse_egress == []
    assert "reporting scope user" in result.stderr
    pages = [line for line in result.stdout.splitlines() if line.startswith("pages: ")]
    assert len(pages) == 1
    overview = Path(pages[0].removeprefix("pages: "))
    assert overview.is_file()
    output_dir = overview.parent
    assert output_dir.parent == scratch.parent / "tmp"
    assert output_dir.name.startswith("eawf-reflect-")
    assert stat.S_IMODE(output_dir.stat().st_mode) == 0o700
    analysis_path = next(output_dir.glob(f"{ANALYSIS_PREFIX}*.json"))
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    # Scope user covers a session another project ran.
    assert len(analysis["sessions"]) == 1
    assert (scratch.parent / "cache" / "eawf" / "reflect" / HISTORY_INDEX_FILENAME).is_file()
    assert not (output_dir / HISTORY_INDEX_FILENAME).exists()
    assert _files(scratch) == before
    assert not (scratch / ".ea").exists()
    assert _files(scratch.parent / "home") == set()


def test_outside_repo_runs_again_into_a_new_directory(
    scratch: Path, refuse_egress: list[str]
) -> None:
    first = runner.invoke(app, ["--json", "reflect", "run", "--local-only"])
    second = runner.invoke(app, ["--json", "reflect", "run", "--local-only"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    pages = [Path(json.loads(run.stdout)["result"]["pages"]).parent for run in (first, second)]
    assert pages[0] != pages[1]
    assert refuse_egress == []


@pytest.fixture
def stray(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a plain working directory below an ancestor holding an empty ``.git``.

    The temporary directory lies below that ancestor too, as on a machine whose
    shared temporary directory holds a stray ``.git``; no repository exists.
    """
    ancestor = sandbox / "stray"
    (ancestor / ".git").mkdir(parents=True)
    (ancestor / "tmp").mkdir()
    work = ancestor / "work"
    work.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(ancestor / "tmp"))
    monkeypatch.chdir(work)
    return work


def test_stray_git_entry_spawns_no_ignore_check(stray: Path, refuse_egress: list[str]) -> None:
    result = runner.invoke(app, ["reflect", "run", "--local-only"])

    assert result.exit_code == 0, result.output
    assert refuse_egress == []
    pages = next(line for line in result.stdout.splitlines() if line.startswith("pages: "))
    assert Path(pages.removeprefix("pages: ")).parent.parent == stray.parent / "tmp"


def test_stray_git_entry_with_a_named_out(stray: Path, refuse_egress: list[str]) -> None:
    out = stray.parent / "out" / "r.json"
    out.parent.mkdir()

    result = runner.invoke(
        app, ["reflect", "run", "--local-only", "--format", "json", "--out", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert refuse_egress == []
    assert json.loads(out.read_text(encoding="utf-8"))["scope"]["kind"] == "user"


def test_output_area_passes_an_invalid_git_entry(stray: Path) -> None:
    area = OutputArea.of(stray.parent / "out", tree_root=stray / ".ea")
    assert area.committed is False
    created = OutputArea.created(stray / "fresh")
    assert created.committed is False
    assert created.directory == (stray / "fresh").resolve()


def test_json_out(scratch: Path, refuse_egress: list[str]) -> None:
    result = runner.invoke(
        app, ["reflect", "run", "--local-only", "--format", "json", "--out", "r.json"]
    )

    assert result.exit_code == 0, result.output
    assert refuse_egress == []
    with (scratch / "r.json").open(encoding="utf-8") as handle:
        written = json.load(handle)
    assert written["schema_version"] == 2
    assert written["scope"]["kind"] == "user"
    assert not (scratch / "r.report.json").exists()
    assert not list(scratch.glob(f"{ANALYSIS_PREFIX}*"))
    assert json.loads(result.stdout) == written


def test_json_out_inside_a_managed_repository(sandbox: Path) -> None:
    repo = sandbox / "repo"
    repo.mkdir()
    tree = native_tree(repo, [run_row("RUN-00000001")], [])
    out = sandbox / "report.json"

    arguments = ["-w", str(tree.parent), "reflect", "run", "--local-only", "--format", "json"]
    result = runner.invoke(app, [*arguments, "--out", str(out)])

    assert result.exit_code == 0, result.output
    written = json.loads(out.read_text(encoding="utf-8"))
    assert [line["run_key"] for line in written["sessions"]] == ["RUN-00000001"]
    assert not out.with_suffix(".report.json").exists()


def test_reflect_skill_supported() -> None:
    entry = resolve_skill("reflect")
    assert entry.standalone == "supported"
    assert standalone_class("/reflect") == "supported"
    assert "scope user" in entry.standalone_note
    assert "temporary directory" in entry.standalone_note
    assert 0 < len(entry.standalone_note) <= 160
    assert "reflect\tsupported" in render_skill_classes_tsv().splitlines()


def test_user_reflect_root_honours_an_absolute_xdg_cache_home(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert user_reflect_root() == sandbox / "cache" / "eawf" / "reflect"
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")
    assert user_reflect_root() == sandbox / "home" / ".cache" / "eawf" / "reflect"
    monkeypatch.setenv("XDG_CACHE_HOME", "")
    assert user_reflect_root() == sandbox / "home" / ".cache" / "eawf" / "reflect"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert user_reflect_root() == sandbox / "home" / ".cache" / "eawf" / "reflect"
    assert not user_reflect_root().exists()


def test_new_output_dir_is_private_and_fresh(sandbox: Path) -> None:
    first, second = new_output_dir(), new_output_dir()
    assert first != second
    assert first.parent == (sandbox / "tmp").resolve()
    assert stat.S_IMODE(first.stat().st_mode) == 0o700
    assert list(first.iterdir()) == []


def _empty_report() -> ReflectReport:
    return ReflectReport.model_validate(
        {
            "schema_version": 2,
            "projects": [],
            "projection_revision": 0,
            "title_fill": "disabled",
            "scope": {"kind": "user", "key": None},
            "sessions": [],
        }
    )


def _manifest() -> FillManifest:
    return FillManifest(
        provider=None,
        local_only=True,
        sessions_listed=0,
        digests_sent=0,
        cache_hits=0,
        unknown_uncached=0,
        titles_by_source={},
    )


def test_store_report_document_only(tmp_path: Path) -> None:
    out = tmp_path / "named.json"
    stored = store_report(
        tmp_path / "c",
        _empty_report(),
        _manifest(),
        on=date(2026, 11, 10),
        out=out,
        document_only=True,
    )
    assert stored.text is None
    assert stored.document == out
    assert json.loads(out.read_text(encoding="utf-8"))["schema_version"] == 2
    assert stored.manifest.parent == tmp_path
    # With no named output the collection keeps its text and its machine form.
    kept = store_report(
        tmp_path / "c",
        _empty_report(),
        _manifest(),
        on=date(2026, 11, 10),
        out=None,
        document_only=True,
    )
    assert kept.text is not None and kept.text.is_file()
    assert kept.document.suffix == ".json"
