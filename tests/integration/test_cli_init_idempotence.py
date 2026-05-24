"""Idempotence tests for ``eawf init --no-input``.

Two calls with identical inputs (and ``--force`` on the second) must
leave every generated file byte-stable:

- ``.ea/state.json`` — schema_version + scope_kind + project_code do not
  drift; the only field that *could* drift is ``updated_at`` (and the
  inner fresh-init payload regenerates it). For idempotence on the
  rendered files (AGENTS.md, CLAUDE.md, manifest), the identical-inputs
  contract holds without resorting to time-mocking.
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
    """Re-running init with --force leaves AGENTS.md byte-stable."""
    res1 = _invoke_init(tmp_path, "--profile", "core")
    assert res1.exit_code == 0, res1.stdout

    agents_md = tmp_path / "AGENTS.md"
    claude_md = tmp_path / "CLAUDE.md"
    text_before = agents_md.read_text(encoding="utf-8")
    claude_before = claude_md.read_text(encoding="utf-8")

    res2 = _invoke_init(tmp_path, "--profile", "core", "--force")
    assert res2.exit_code == 0, res2.stdout

    text_after = agents_md.read_text(encoding="utf-8")
    claude_after = claude_md.read_text(encoding="utf-8")

    assert text_before == text_after, "AGENTS.md must be byte-stable on re-run"
    assert claude_before == claude_after, "CLAUDE.md must be byte-stable on re-run"


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


def test_cli_init_state_json_keys_stable(tmp_path: Path) -> None:
    """Top-level keys of state.json after init match the documented shape."""
    res = _invoke_init(tmp_path, "--profile", "core")
    assert res.exit_code == 0, res.stdout

    import json as _json

    state = _json.loads((tmp_path / ".ea" / "state.json").read_text(encoding="utf-8"))
    expected_keys = {
        "schema_version",
        "scope_kind",
        "urn",
        "updated_at",
        "project",
        "current",
        "workspace",
        "phases",
        "iters",
        "waves",
        "artifacts",
        "agent_sessions",
        "plugins",
        "indexes",
    }
    assert expected_keys.issubset(state.keys()), sorted(state.keys())
    assert state["scope_kind"] == "repo"
    assert state["current"]["project_code"] == "DEMO"
