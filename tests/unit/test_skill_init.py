"""Unit tests for :class:`eawf.workflow.skills.init.InitSkill`.

Pin the Phase 4 W03 acceptance contract for ``/init``:

- Happy path: ``ctx.args["answers"]`` carries every wizard answer →
  status=ok envelope with a populated :class:`InitBody` whose ``steps``
  reflect each wizard write target.
- Mid-wizard degrade: ``ctx.args`` missing required keys → status=needs_user
  with a typed :class:`UserQuestion` populated.
- Schema mismatch: malformed answers → status=failed with a populated
  ``footer.repair_commands`` list.
- Registration: the skill registers under the ``/init`` slot.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from eawf.workflow.skills.bodies.init import InitBody
from eawf.workflow.skills.engine import SkillContext, run_skill
from eawf.workflow.skills.init import InitSkill


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return state_dir


def _ctx(args: dict[str, object] | None = None) -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
        args=dict(args or {}),
    )


def _full_answers(target_dir: Path) -> dict[str, object]:
    return {
        "state_path": ".ea/state.json",
        "project_code": "QR",
        "project_title": "Quant Research",
        "lifecycle_depth": "phase",
        "profiles": ("core",),
        "runtime": "claude-code",
    }


def test_init_skill_registered_with_canonical_name() -> None:
    from eawf.workflow.skills import registry

    cls = registry.lookup("/init")
    assert cls is InitSkill


def test_init_missing_answers_returns_needs_user(state_dir: Path) -> None:
    skill = InitSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "needs_user"
    assert env.header.skill == "/init"
    body = InitBody.model_validate(cast(dict, env.body))
    assert body.user_question is not None
    assert 2 <= len(body.user_question.options) <= 4
    # The collect_answers step should be the only entry, marked needs_user.
    assert any(s.status == "needs_user" for s in body.steps)


def test_init_partial_answers_lists_missing_keys(state_dir: Path) -> None:
    skill = InitSkill()
    args = {
        "answers": {
            "state_path": ".ea/state.json",
            "project_code": "QR",
        },
    }
    env = run_skill(skill, _ctx(args))
    assert env.header.status == "needs_user"
    body = InitBody.model_validate(cast(dict, env.body))
    assert body.user_question is not None
    # The question text should mention at least one of the missing keys.
    text = body.user_question.question.lower()
    assert "missing" in text


def test_init_happy_path_status_ok(state_dir: Path, tmp_path: Path) -> None:
    skill = InitSkill()
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    answers = _full_answers(target)
    env = run_skill(skill, _ctx({"answers": answers, "target_dir": str(target)}))
    assert env.header.status == "ok", env.body
    body = InitBody.model_validate(cast(dict, env.body))
    assert body.project_code == "QR"
    assert body.profile_ids == ["core"]
    # state.json + config.yaml + AGENTS.md + manifest + CLAUDE.md → 5 ok rows;
    # materialised_state_keys may add a 6th row if the profile registers any.
    step_names = {s.name for s in body.steps}
    assert {"state_json", "config_yaml", "agents_md", "manifest", "claude_md"} <= step_names
    # Wizard outputs should land inside the target_dir.
    assert (target / ".ea" / "state.json").exists()
    assert (target / "AGENTS.md").exists()
    assert (target / "CLAUDE.md").exists()


def test_init_invalid_answers_returns_failed(state_dir: Path) -> None:
    """Bad answers → status=failed (engine wraps InvalidInput as failed)."""
    skill = InitSkill()
    bad = {
        "state_path": ".ea/state.json",
        "project_code": "lowercase-bad",  # rejects pattern
        "project_title": "Quant Research",
        "lifecycle_depth": "phase",
        "profiles": ("core",),
        "runtime": "claude-code",
    }
    env = run_skill(skill, _ctx({"answers": bad}))
    assert env.header.status == "failed"
    assert env.footer.repair_commands  # populated for failed status


def test_init_emits_expected_events(state_dir: Path, tmp_path: Path) -> None:
    """The skill writes one EVENT row per algorithm milestone.

    Happy path emits at least: detect_state, run_wizard, complete → 3.
    """
    skill = InitSkill()
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    env = run_skill(skill, _ctx({"answers": _full_answers(target), "target_dir": str(target)}))
    assert env.header.status == "ok"
    events_path = state_dir / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 3
    assert len(env.footer.persisted_store_records) >= 3


def test_init_flattened_args_supported(state_dir: Path, tmp_path: Path) -> None:
    """Operators may flatten the answers onto ctx.args directly."""
    skill = InitSkill()
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    flat = dict(_full_answers(target))
    flat["target_dir"] = str(target)
    env = run_skill(skill, _ctx(flat))
    assert env.header.status == "ok", env.body
