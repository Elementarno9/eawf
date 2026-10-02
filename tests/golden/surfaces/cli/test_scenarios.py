"""End-to-end golden scenarios for the eawf init wizard.

Two scenarios exercise the public surface from a clean slate and
assert byte-stable outputs against the committed golden fixtures in
this directory:

1. :func:`test_run_wizard_no_input_fresh_repo` — wizard on empty dir.
2. :func:`test_run_wizard_no_input_enrich_existing` — wizard does not
   touch pre-existing files outside ``.ea/`` / ``AGENTS.md`` /
   ``CLAUDE.md``.

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

from eawf.platform.install.wizard import WizardAnswers, run_wizard_no_input

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
    assert live_state["project"]["code"] == _PROJECT_CODE
    assert live_state["project"]["title"] == _PROJECT_TITLE
    assert live_state["project"]["domains"] == ["general"]
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
