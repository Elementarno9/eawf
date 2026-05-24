"""End-to-end golden scenarios for the eawf init wizard + lifecycle CLI.

Three scenarios exercise the public surface from a clean slate and
assert byte-stable outputs against the committed golden fixtures in
this directory:

1. :func:`test_run_wizard_no_input_fresh_repo` — wizard on empty dir.
2. :func:`test_run_wizard_no_input_enrich_existing` — wizard does not
   touch pre-existing files outside ``.ea/`` / ``AGENTS.md`` /
   ``CLAUDE.md``.
3. :func:`test_lifecycle_flow_full` — ``project init -> phase open ->
   iter open -> wave plan/claim/close -> phase close`` walk via the
   Typer CliRunner produces the committed end-state projection.

Goldens are JSON projections (see :func:`conftest.project_state`) plus
one byte-stable AGENTS.md snapshot from :func:`fresh_repo`. The
projection is the canonical evidence — state.json itself embeds
timestamps and per-run urns that are not byte-stable across runs.

See ``conftest.py`` for the regen workflow (``EAWF_GOLDEN_SCENARIOS_REGEN=1``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.platform.install.wizard import WizardAnswers, run_wizard_no_input
from eawf.surfaces.cli.app import app

from .conftest import (
    assert_or_regen_json,
    project_agents_md,
    project_state,
)

pytestmark = pytest.mark.golden_scenarios

_PROJECT_CODE = "GOLDENTEST"
_PROJECT_TITLE = "golden-test"


def _wizard_answers() -> WizardAnswers:
    """Canonical wizard answers shared across scenarios that exercise the wizard.

    Locked-in choices match the spec for B009:

    - ``project_code = GOLDENTEST`` — passes the canonical regex.
    - ``profiles = ("core",)`` — single-profile keeps the rendered
      AGENTS.md region set small and stable.
    - ``runtime = claude-code`` — the v0.1 default runtime.
    - ``lifecycle_depth = phase`` — minimum depth that still permits
      :func:`run_wizard_no_input` to produce a complete tree.
    - All acceptance gates enabled (the v0.1 init default).
    """
    return WizardAnswers(
        state_path=".ea/state.json",
        project_code=_PROJECT_CODE,
        project_title=_PROJECT_TITLE,
        lifecycle_depth="phase",
        profiles=("core",),
        runtime="claude-code",
        plugins=(),
        mcp=(),
        acceptance_tests=True,
        acceptance_lint=True,
        acceptance_typecheck=True,
    )


# ---- fresh_repo scenario ---------------------------------------------------


def test_run_wizard_no_input_fresh_repo(
    fresh_target: Path,
    scenarios_dir: Path,
) -> None:
    """Wizard on an empty target produces the canonical state + agents.md.

    Asserts:

    - ``.ea/state.json`` projection matches ``fresh_repo/state.golden.json``.
    - ``AGENTS.md`` region projection (ordered region ids + per-region
      body byte-length) matches ``fresh_repo/agents.golden.json``. The
      raw bytes are intentionally NOT committed here: their content is
      already pinned by ``tests/golden/agents_md/core_only.md``, and
      committing a second copy would re-leak the literal pattern
      examples that the user-scope PII guard rejects.
    - ``.ea/config.yaml`` and ``CLAUDE.md`` exist (their byte-stability
      is already covered by sibling integration tests; we only assert
      presence here).
    """
    result = run_wizard_no_input(_wizard_answers(), fresh_target)
    state_path = fresh_target / ".ea" / "state.json"
    config_path = fresh_target / ".ea" / "config.yaml"
    agents_md_path = fresh_target / "AGENTS.md"
    claude_md_path = fresh_target / "CLAUDE.md"

    assert state_path.exists()
    assert config_path.exists()
    assert agents_md_path.exists()
    assert claude_md_path.exists()
    assert result.project_code == _PROJECT_CODE

    live_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert_or_regen_json(
        scenarios_dir / "fresh_repo" / "state.golden.json",
        project_state(live_state),
    )
    assert_or_regen_json(
        scenarios_dir / "fresh_repo" / "agents.golden.json",
        project_agents_md(agents_md_path.read_text(encoding="utf-8")),
    )


def test_run_wizard_no_input_fresh_repo_byte_stable_agents_md(
    tmp_path: Path,
) -> None:
    """Two consecutive wizard runs on identical inputs emit identical AGENTS.md.

    Sister assertion to the golden check above: the golden fixture
    only catches regressions; this test catches *new* non-determinism
    that happens to land between two runs of the same Python process.
    """
    target_a = tmp_path / "a"
    target_b = tmp_path / "b"
    target_a.mkdir()
    target_b.mkdir()
    run_wizard_no_input(_wizard_answers(), target_a)
    run_wizard_no_input(_wizard_answers(), target_b)
    bytes_a = (target_a / "AGENTS.md").read_bytes()
    bytes_b = (target_b / "AGENTS.md").read_bytes()
    assert bytes_a == bytes_b, (
        "AGENTS.md drifted between two identical wizard runs — "
        "non-determinism leaked into eawf.surfaces.render.agents_md"
    )


# ---- enrich_existing scenario ----------------------------------------------


def test_run_wizard_no_input_enrich_existing(
    enriched_target: Path,
    scenarios_dir: Path,
) -> None:
    """Wizard does not clobber arbitrary repo files outside ``.ea/``.

    The :func:`enriched_target` fixture pre-populates the directory
    with a ``README.md``, ``user_notes.txt`` and a ``.git/HEAD``. The
    wizard MUST:

    - leave those files byte-identical;
    - create ``.ea/state.json`` / ``.ea/config.yaml`` and a fresh
      ``AGENTS.md`` / ``CLAUDE.md`` (no pre-existing AGENTS.md → the
      wizard wholly authors the new file);
    - emit a state projection matching the committed golden.
    """
    readme_before = (enriched_target / "README.md").read_bytes()
    user_notes_before = (enriched_target / "user_notes.txt").read_bytes()
    git_head_before = (enriched_target / ".git" / "HEAD").read_bytes()

    run_wizard_no_input(_wizard_answers(), enriched_target)

    assert (enriched_target / "README.md").read_bytes() == readme_before
    assert (enriched_target / "user_notes.txt").read_bytes() == user_notes_before
    assert (enriched_target / ".git" / "HEAD").read_bytes() == git_head_before

    state_path = enriched_target / ".ea" / "state.json"
    assert state_path.exists()
    live_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert_or_regen_json(
        scenarios_dir / "enrich_existing" / "state.golden.json",
        project_state(live_state),
    )


# ---- flow_full scenario ----------------------------------------------------


def _invoke(runner: CliRunner, *args: str) -> object:
    """Invoke the root eawf Typer app with explicit args + return the result.

    A thin wrapper so the scenario test reads as a step-list. Any
    non-zero exit causes the caller's assertion to fail with the
    captured stdout in the message.
    """
    return runner.invoke(app, list(args))


def test_lifecycle_flow_full(
    flow_target: Path,
    scenarios_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end lifecycle walk via the Typer CliRunner.

    Path traced:

    1. ``project init GOLDENTEST --title golden-test --domains demo`` —
       creates the state file with a non-null ``project`` (the
       lifecycle invariants reject ``scope_kind=repo`` with a null
       project, so the wizard path is not viable for this scenario;
       see ``flow_full/README`` for the rationale).
    2. ``phase open P01 --title Bootstrap``.
    3. ``iter open P01-I01 --title Iter1``.
    4. ``wave plan P01-I01 --id P01-I01-W01 --title Implement
       --files src/``.
    5. ``wave claim P01-I01-W01 --session SES-1``.
    6. ``wave close P01-I01-W01 --outcome done`` (W04 dropped the
       ``Wave.commit`` field; the SHA is now derived on demand from
       ``git log --grep``).
    7. ``iter close P01-I01 --audit AUD-1``.
    8. ``decision add D001 --scope-id P01 ...`` records the explicit
       single-wave scope-collapse rationale required by phase close.
    9. ``phase close P01 --audit AUD-1``.

    Asserts the final state projection matches the committed
    ``flow_full/state.golden.json``. The projection encodes:
    ``phases.P01.status == "closed"``, ``iters.P01-I01`` present,
    ``waves.P01-I01-W01`` present, and the ``current.*`` pointer
    keyset (which stays the same shape across the walk).
    """
    state_path = flow_target / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    runner = CliRunner()

    steps: list[tuple[str, ...]] = [
        (
            "project",
            "init",
            _PROJECT_CODE,
            "--title",
            _PROJECT_TITLE,
            "--domains",
            "demo",
        ),
        ("phase", "open", "P01", "--title", "Bootstrap"),
        ("iter", "open", "P01-I01", "--title", "Iter1"),
        (
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "Implement",
            "--files",
            "src/",
        ),
        ("wave", "claim", "P01-I01-W01", "--session", "SES-1"),
        (
            "wave",
            "close",
            "P01-I01-W01",
            "--outcome",
            "done",
        ),
        ("iter", "close", "P01-I01", "--audit", "AUD-1"),
        (
            "decision",
            "add",
            "D001",
            "--scope-id",
            "P01",
            "--summary",
            "P01 scope collapse: finish as single-wave phase",
            "--rationale",
            "scope collapse accepted for minimal lifecycle scenario",
            "--alternative",
            "plan a second wave",
        ),
        ("phase", "close", "P01", "--audit", "AUD-1"),
    ]
    for step_args in steps:
        result = _invoke(runner, *step_args)
        # CliRunner returns Click's Result; .exit_code is the canonical attr.
        exit_code = getattr(result, "exit_code", None)
        stdout = getattr(result, "stdout", "")
        assert exit_code == 0, f"step {step_args} failed: exit={exit_code} stdout={stdout!r}"

    assert state_path.exists()
    live_state = json.loads(state_path.read_text(encoding="utf-8"))

    # Sanity invariant: phase + iter + wave all reach the closed terminal.
    assert (live_state.get("phases") or {}).get("P01", {}).get("status") == "closed"
    assert (live_state.get("iters") or {}).get("P01-I01", {}).get("status") == "closed"
    assert (live_state.get("waves") or {}).get("P01-I01-W01", {}).get("status") == "closed"

    assert_or_regen_json(
        scenarios_dir / "flow_full" / "state.golden.json",
        project_state(live_state),
    )
