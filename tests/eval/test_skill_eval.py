"""Skill-dispatch eval harness — golden envelope regression suite.

Each case loads a golden envelope JSON from ``tests/eval/golden/<skill_id>.json``
and asserts the live dispatch produces a structurally-equivalent envelope:
same skill name, same status, same set of body keys, same warning /
repair-command counts. The harness is opt-in via the ``eval`` pytest
marker — default ``uv run pytest`` skips the cluster; the regression run is
``uv run pytest -m eval``, which the ``skill-eval`` CI job runs.

The cases are derived from the closed skill catalog: every catalog skill with
an engine implementation is a case, so a skill that joins the catalog without
a golden fails here, and a golden left behind by a retired skill fails the
coverage test. Scoring is a pure structural comparison — no model is called.

The shape test (``test_skill_envelope_matches_golden``) pins the envelope
contract exactly. The score test (``test_skill_envelope_score_meets_threshold``)
checks the weighted :func:`eawf.observability.eval.score_envelope` total against
the fixture's ``eval_score_threshold`` (default ``0.85``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

import eawf.workflow.skills._bootstrap  # noqa: F401  registers every engine skill
from eawf.observability.eval import score_envelope
from eawf.workflow.skills.catalog import SKILL_CATALOG
from eawf.workflow.skills.engine import Skill, SkillContext, run_skill
from eawf.workflow.skills.registry import lookup

_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def _catalog_cases() -> tuple[tuple[str, type[Skill]], ...]:
    # Catalog skills without an engine class are rendered pages only; they
    # emit no envelope, so there is nothing to compare.
    cases: list[tuple[str, type[Skill]]] = []
    for entry in SKILL_CATALOG.entries:
        cls = lookup(entry.invocation_name)
        if cls is not None:
            cases.append((entry.skill_id, cls))
    return tuple(cases)


_SKILL_CASES = _catalog_cases()
_CASE_IDS = [s for s, _ in _SKILL_CASES]


def _load_golden(slug: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads((_GOLDEN_DIR / f"{slug}.json").read_text("utf-8")))


@pytest.mark.eval
def test_skill_eval_cases_cover_catalog_goldens() -> None:
    """Goldens exist for exactly the catalog skills with an engine class."""
    golden_slugs = sorted(p.stem for p in _GOLDEN_DIR.glob("*.json"))
    assert golden_slugs == sorted(_CASE_IDS)
    retired = {row.skill_id for row in SKILL_CATALOG.retired}
    assert not retired & set(golden_slugs)


@pytest.mark.eval
@pytest.mark.parametrize("slug,skill_cls", _SKILL_CASES, ids=_CASE_IDS)
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


@pytest.mark.eval
@pytest.mark.parametrize("slug,skill_cls", _SKILL_CASES, ids=_CASE_IDS)
def test_skill_envelope_score_meets_threshold(
    slug: str,
    skill_cls: type[Skill],
    eval_state_dir: Path,
    eval_ctx: SkillContext,
) -> None:
    """Live envelope scores at or above the golden's ``eval_score_threshold``.

    The score combines six normalised dimensions (status, body_keys,
    warnings ±1, repair_commands ±1, evidence_refs presence,
    state_mutation kinds) into a 0..1 total.
    """
    golden = _load_golden(slug)
    env = run_skill(skill_cls(), eval_ctx)
    score = score_envelope(env, golden)
    threshold = float(golden.get("eval_score_threshold", 0.85))  # type: ignore[arg-type]
    assert score.total >= threshold, (
        f"score {score.total:.3f} < threshold {threshold:.3f} for {slug}: per_dim={score.per_dim}"
    )


@pytest.mark.eval
@pytest.mark.parametrize("slug,skill_cls", _SKILL_CASES, ids=_CASE_IDS)
def test_skill_envelope_score_drifted_golden_below_threshold(
    slug: str,
    skill_cls: type[Skill],
    eval_state_dir: Path,
    eval_ctx: SkillContext,
) -> None:
    """A golden whose body keys drift by one key scores below its threshold."""
    golden = _load_golden(slug)
    drifted = {**golden, "body_keys": [*cast(list[str], golden["body_keys"]), "drifted_key"]}
    env = run_skill(skill_cls(), eval_ctx)
    score = score_envelope(env, drifted)
    assert score.per_dim["body_keys"] == pytest.approx(0.0)
    assert score.total < float(golden.get("eval_score_threshold", 0.85))  # type: ignore[arg-type]
