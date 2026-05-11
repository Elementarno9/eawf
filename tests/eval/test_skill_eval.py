"""Skill-dispatch eval harness — golden envelope regression suite.

Each case loads a golden envelope JSON from ``tests/eval/golden/<slug>.json``
and asserts the live dispatch produces a structurally-equivalent envelope:
same skill name, same status, same set of body keys, same warning /
repair-command counts. The harness is opt-in via the ``eval`` pytest
marker — default ``uv run pytest`` skips the cluster; the regression run is
``uv run pytest -m eval``.

The bar is intentionally low for v0.2: we want a guard-rail that protects
the envelope contract across model/version changes, not a deep semantic
check of skill behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from eawf.skills.audit import AuditSkill
from eawf.skills.engine import Skill, SkillContext, run_skill
from eawf.skills.polish import PolishSkill
from eawf.skills.prep import PrepSkill
from eawf.skills.research import ResearchSkill
from eawf.skills.review import ReviewSkill
from eawf.skills.ship import ShipSkill

_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


_SKILL_CASES: tuple[tuple[str, type[Skill]], ...] = (
    ("research", ResearchSkill),
    ("prep", PrepSkill),
    ("audit", AuditSkill),
    ("ship", ShipSkill),
    ("review", ReviewSkill),
    ("polish", PolishSkill),
)


def _load_golden(slug: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads((_GOLDEN_DIR / f"{slug}.json").read_text("utf-8")))


@pytest.mark.eval
@pytest.mark.parametrize("slug,skill_cls", _SKILL_CASES, ids=[s for s, _ in _SKILL_CASES])
def test_skill_envelope_matches_golden(
    slug: str,
    skill_cls: type[Skill],
    eval_state_dir: Path,
    eval_ctx: SkillContext,
) -> None:
    """Envelope status + body-key set + footer counts match the golden."""
    golden = _load_golden(slug)
    env = run_skill(skill_cls(), eval_ctx)

    assert env.header.skill == golden["skill"], (
        f"skill name drifted for {slug}: expected={golden['skill']!r} got={env.header.skill!r}"
    )
    assert env.header.status == golden["status"], (
        f"status drifted for {slug}: expected={golden['status']!r} got={env.header.status!r}"
    )

    body_keys = sorted(env.body.keys()) if isinstance(env.body, dict) else []
    assert body_keys == golden["body_keys"], (
        f"body keys drifted for {slug}: expected={golden['body_keys']} got={body_keys}"
    )

    warnings_count = len(env.footer.warnings) if env.footer.warnings else 0
    repair_count = len(env.footer.repair_commands) if env.footer.repair_commands else 0
    assert warnings_count == golden["warnings_count"], (
        f"warnings count drifted for {slug}: "
        f"expected={golden['warnings_count']} got={warnings_count}"
    )
    assert repair_count == golden["repair_commands_count"], (
        f"repair-commands count drifted for {slug}: "
        f"expected={golden['repair_commands_count']} got={repair_count}"
    )
