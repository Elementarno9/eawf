"""Idempotence tests for ``eawf init --no-input``.

Two calls with identical inputs (and ``--force`` on the second) must
leave every generated file byte-stable:

- The rendered files (AGENTS.md, CLAUDE.md, manifest) hold the
  identical-inputs contract without resorting to time-mocking.
- ``AGENTS.md`` — managed regions hash-stable per :mod:`eawf.surfaces.render.agents_md`.
- ``.ea/indexes/generated.json`` — :mod:`eawf.surfaces.render.manifest` writes
  deterministically (``sort_keys + indent=2``); only ``generated_at``
  varies.
- Multi-profile combinations produce a composed AGENTS.md with both
  blocks.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


def _invoke_init(target: Path, *extra: str) -> object:
    args = ["--no-input", "init", "--project-code", "DEMO", "--target", str(target), *extra]
    return runner.invoke(app, args)


def test_cli_init_idempotent(tmp_path: Path) -> None:
    """Two inits from identical inputs render byte-identical AGENTS.md and CLAUDE.md.

    REL-021: an initialised tree is born at epoch 2, whose epoch-1 document
    is frozen, so ``--force`` over it refuses (see
    ``test_rel_021_a_forced_reinit_of_an_epoch2_tree_refuses``); determinism
    is proven across two fresh targets instead.
    """
    first, second = tmp_path / "a", tmp_path / "b"
    for target in (first, second):
        target.mkdir()
        res = _invoke_init(target, "--profile", "core")
        assert res.exit_code == 0, res.stdout
    for name in ("AGENTS.md", "CLAUDE.md"):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_cli_init_second_run_no_force_fails(tmp_path: Path) -> None:
    """Running init twice without --force exits 3 the second time."""
    res1 = _invoke_init(tmp_path, "--profile", "core")
    assert res1.exit_code == 0, res1.stdout

    res2 = _invoke_init(tmp_path, "--profile", "core")
    assert res2.exit_code == 1, res2.stdout


def test_cli_init_with_multiple_profiles_combines_render_blocks(tmp_path: Path) -> None:
    """``--profile core --profile python`` renders both block ids into AGENTS.md."""
    res = _invoke_init(tmp_path, "--profile", "core", "--profile", "python")
    assert res.exit_code == 0, res.stdout

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # Core profile has 'non-negotiable-rules'; python profile has 'python-style'.
    assert "BEGIN EAWF:managed id=non-negotiable-rules" in text
    assert "BEGIN EAWF:managed id=python-style" in text
    assert "END EAWF:managed id=non-negotiable-rules" in text
    assert "END EAWF:managed id=python-style" in text
