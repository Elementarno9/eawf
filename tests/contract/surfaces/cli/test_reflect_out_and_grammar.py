"""The reflect run output path and the /reflect skill page.

- ``reflect run --out`` keeps the report, its machine form and the fill manifest
  together beside the named path, and writes nothing into the local collection.
- The /reflect grammar names every verb the group ships, and its page promises
  no analysis the report does not carry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.observability.reflect import report as rp
from eawf.surfaces.cli.app import app
from eawf.surfaces.render.skills.render import render_skill_md_from_spec
from eawf.workflow.skills.catalog import resolve_skill, shipped_skill_specs
from tests.contract.surfaces.cli._reflect_tree import native_tree, run_row

runner = CliRunner()


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a canary holding one finished Run."""
    monkeypatch.delenv("EA_STATE", raising=False)
    return native_tree(tmp_path / "repo", [run_row("RUN-00000001")], [])


def test_out_keeps_every_file_beside_it(tree: Path, tmp_path: Path) -> None:
    """r.txt, r.json and the manifest land in the --out directory; the collection stays absent."""
    out_dir = tmp_path / "out"
    result = runner.invoke(
        app,
        ["-w", str(tree.parent), "reflect", "run", "--local-only", "--out", str(out_dir / "r.txt")],
    )
    assert result.exit_code == 0, result.output
    names = {path.name for path in out_dir.iterdir()}
    manifests = {name for name in names if name.endswith(f"{rp.MANIFEST_STEM_SUFFIX}.json")}
    assert len(manifests) == 1
    assert names == {"r.txt", "r.json", *manifests}
    assert not rp.reflect_root(tree).exists()


def test_run_without_out_keeps_every_file_in_the_collection(tree: Path) -> None:
    """Boundary: with no --out the report, its machine form and the manifest share one home."""
    result = runner.invoke(app, ["-w", str(tree.parent), "reflect", "run", "--local-only"])
    assert result.exit_code == 0, result.output
    names = {path.name for path in rp.reflect_root(tree).iterdir()}
    assert any(name.endswith(f"{rp.REPORT_STEM_SUFFIX}.txt") for name in names)
    assert any(name.endswith(f"{rp.REPORT_STEM_SUFFIX}.json") for name in names)
    assert any(name.endswith(f"{rp.MANIFEST_STEM_SUFFIX}.json") for name in names)


def test_reflect_grammar_lists_serve_and_promises_only_what_ships() -> None:
    """The actions are the group's five verbs, and the page drops the variance promise."""
    entry = resolve_skill("reflect")
    assert entry.grammar.actions == ("run", "show", "export", "serve", "prune")
    spec = next(spec for spec in shipped_skill_specs() if spec.skill_name == "reflect")
    page = render_skill_md_from_spec(spec)
    for phrase in ("estimate-versus-actual", "variance", "cohort"):
        assert phrase not in page, phrase
    assert "eawf reflect serve" in page
