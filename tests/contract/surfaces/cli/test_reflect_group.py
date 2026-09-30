"""SURF-165 and SURF-170: the ``reflect`` group, read-only against canonical state.

- SURF-165: the group carries ``run``, ``show``, ``serve``, ``export`` and ``prune``; each
  writes only to the local reflection collection, its title cache or a named output path;
  an output path inside the canonical store is refused before anything is read; and no
  verb other than ``run`` calls out.
- SURF-170: the group is mounted under the closed verb contract, mutates no canonical
  state, and its viewer binds loopback behind a nonce-bearing URL.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import pytest
import typer
from typer.testing import CliRunner

from eawf.observability.reflect import report as rp
from eawf.observability.reflect import serve as sv
from eawf.observability.reflect import titles as tt
from eawf.surfaces.cli import exit_codes, verb_contract
from eawf.surfaces.cli.app import app
from eawf.workflow.skills.catalog import resolve_skill
from tests.contract.surfaces.cli._reflect_tree import (
    command,
    native_tree,
    refuse_egress,
    run_row,
    summarized,
    tree_digest,
)

__all__ = ["refuse_egress"]

runner = CliRunner()


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a canary holding one finished Run with a few events."""
    monkeypatch.delenv("EA_STATE", raising=False)
    return native_tree(
        tmp_path,
        [run_row("RUN-00000001")],
        [summarized("RUN-00000001", 1, "read the loader"), command("RUN-00000001", 2)],
    )


def _invoke(tree: Path, *args: str) -> click.testing.Result:
    return runner.invoke(app, ["-w", str(tree.parent), "reflect", *args])


def _canonical(tree: Path) -> dict[str, str]:
    """Digest every file of the store outside its local tree."""
    return tree_digest(tree, skip=tree / "local")


def _reflect_group() -> click.Group:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    group = root.commands["reflect"]
    assert isinstance(group, click.Group)
    return group


def test_surf_165_the_group_carries_exactly_its_five_verbs() -> None:
    """run, show, serve, export and prune, and nothing else."""
    assert set(_reflect_group().commands) == {"run", "show", "serve", "export", "prune"}


def test_surf_170_the_group_is_mounted_under_the_verb_contract() -> None:
    """reflect is a declared cross-cutting group and is mounted at the root."""
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    assert "reflect" in verb_contract.CROSS_CUTTING_GROUPS
    assert "reflect" in root.commands


def test_surf_165_run_writes_only_the_local_collection(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run leaves every canonical byte alone and writes report, manifest and cache."""
    monkeypatch.setattr(
        tt.ClaudeTitleProvider, "discover", classmethod(lambda cls: _Stub("Tidy the loader"))
    )
    before = _canonical(tree)
    result = _invoke(tree, "run")
    assert result.exit_code == 0, result.output
    assert _canonical(tree) == before
    names = {path.name for path in rp.reflect_root(tree).iterdir()}
    assert rp.TITLE_CACHE_FILENAME in names
    assert any(name.endswith("-reflect-report.txt") for name in names)
    assert any(name.endswith("-reflect-manifest.json") for name in names)


@pytest.mark.parametrize("verb", ["show", "export", "prune", "serve"])
def test_surf_165_a_non_run_verb_neither_calls_out_nor_writes_canonically(
    tree: Path, verb: str, refuse_egress: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a network-refusing fixture each non-run verb succeeds with zero egress."""
    assert _invoke(tree, "run", "--local-only").exit_code == 0
    monkeypatch.setattr(sv.ReportServer, "serve_until_interrupted", lambda self: None)
    before = _canonical(tree)
    result = _invoke(tree, verb)
    assert result.exit_code == 0, result.output
    assert refuse_egress == []
    assert _canonical(tree) == before


def test_surf_165_show_prints_the_newest_report(tree: Path) -> None:
    """show reads the collection back as the report run rendered."""
    assert _invoke(tree, "run", "--local-only").exit_code == 0
    result = _invoke(tree, "show")
    assert result.exit_code == 0
    assert "session RUN-00000001 COMPLETED" in result.output


def test_surf_165_export_writes_a_page_and_its_script_chunk(tree: Path) -> None:
    """export lands a page and the data chunk beside it in the collection."""
    assert _invoke(tree, "run", "--local-only").exit_code == 0
    result = _invoke(tree, "export")
    assert result.exit_code == 0
    page = Path(result.output.split("page: ", 1)[1].strip())
    assert page.is_relative_to(rp.reflect_root(tree))
    assert (page.parent / rp.EXPORT_DATA_FILENAME).is_file()


def test_surf_170_serve_announces_a_loopback_url_with_a_nonce(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """serve binds loopback and its URL carries the invocation's nonce."""
    assert _invoke(tree, "run", "--local-only").exit_code == 0
    monkeypatch.setattr(sv.ReportServer, "serve_until_interrupted", lambda self: None)
    result = runner.invoke(app, ["--json", "-w", str(tree.parent), "reflect", "serve"])
    assert result.exit_code == 0
    url = json.loads(result.output)["result"]["url"]
    assert url.startswith("http://127.0.0.1:")
    assert len(url.rstrip("/").rsplit("/", 1)[1]) >= 16


def test_surf_165_prune_removes_only_expired_local_entries(tree: Path) -> None:
    """prune drops dated entries and titles past retention and keeps the rest."""
    root = rp.reflect_root(tree)
    root.mkdir(parents=True)
    now = datetime(2026, 9, 29, tzinfo=UTC)
    old = (now - rp.REPORT_RETENTION - timedelta(days=1)).date().isoformat()
    fresh = now.date().isoformat()
    (root / f"{old}-reflect-report.txt").write_text("old\n")
    (root / f"{fresh}-reflect-report.txt").write_text("new\n")
    (root / "notes.txt").write_text("undated\n")
    cache = tt.TitleCache(
        entries={
            "a" * 64: tt.CachedTitle(title="Keep", stored_at=now),
            "b" * 64: tt.CachedTitle(
                title="Drop", stored_at=now - rp.TITLE_CACHE_RETENTION - timedelta(days=1)
            ),
        }
    )
    result = rp.prune_collection(root, cache, now=now)
    assert result.removed == (f"{old}-reflect-report.txt",)
    assert result.titles_removed == 1
    assert set(result.cache.entries) == {"a" * 64}
    assert {path.name for path in root.iterdir()} == {f"{fresh}-reflect-report.txt", "notes.txt"}


def test_surf_165_prune_of_an_empty_collection_removes_nothing(tmp_path: Path) -> None:
    """Boundary: no collection yet is nothing to prune."""
    result = rp.prune_collection(tmp_path / "absent", tt.TitleCache(), now=datetime.now(UTC))
    assert result.removed == ()
    assert result.titles_removed == 0


@pytest.mark.parametrize("verb", ["run", "export"])
@pytest.mark.parametrize("target", ["state.json", "generations/out.txt", "store/x.txt"])
def test_surf_165_an_output_inside_the_canonical_store_is_refused(
    tree: Path, verb: str, target: str
) -> None:
    """Argument resolution refuses a canonical output before any read or write."""
    before = tree_digest(tree, skip=tree / "local")
    result = _invoke(tree, verb, "--out", str(tree / target))
    assert result.exit_code == exit_codes.USER_ERROR
    assert "canonical store" in result.output
    assert tree_digest(tree, skip=tree / "local") == before
    assert not rp.reflect_root(tree).exists()


def test_surf_165_an_output_under_the_local_tree_is_accepted(tmp_path: Path) -> None:
    """Boundary: the local tree and anywhere outside the store resolve as named."""
    store = tmp_path / ".ea"
    assert (
        rp.resolve_output_path(store, store / "local" / "r.txt")
        == (store / "local" / "r.txt").resolve()
    )
    assert rp.resolve_output_path(store, tmp_path / "r.txt") == (tmp_path / "r.txt").resolve()
    with pytest.raises(rp.CanonicalWriteRefusedError):
        rp.resolve_output_path(store, store / "local" / ".." / "state.json")


def test_surf_170_the_skill_catalog_declares_no_mutating_rpc_for_reflect() -> None:
    """The /reflect entry the group serves declares no canonical mutation."""
    entry = resolve_skill("reflect")
    assert entry.effects.canonical_mutates is False
    assert set(entry.effects.rpcs) <= {"read_entity", "query_measurement", "query_telemetry"}


class _Stub:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    @property
    def name(self) -> str:
        return "stub"

    def title_for(self, digest_text: str) -> str | None:
        return self.answer


def test_surf_165_the_egress_fixture_catches_a_spawn_and_a_connect(
    refuse_egress: list[str],
) -> None:
    """The no-egress cases above would red: a spawn and an outbound connect are caught."""
    import socket
    import subprocess

    with pytest.raises(AssertionError):
        subprocess.run(["true"], check=False)
    with pytest.raises(AssertionError), socket.socket() as sock:
        sock.connect(("192.0.2.1", 80))
    assert refuse_egress == ["spawn ['true']", "connect 192.0.2.1"]
