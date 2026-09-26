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

The catalog cases run with no arguments, so the lifecycle skills ``/dispatch``,
``/integrate`` and ``/verify`` land on their empty-request refusal. Each of them
also has a happy-path case (``golden/happy/<skill_id>.json``): a complete
invocation answered by a canned transport, so the envelope a real pass returns
is pinned too, deterministically and without a daemon.

The shape test (``test_skill_envelope_matches_golden``) pins the envelope
contract exactly. The score test (``test_skill_envelope_score_meets_threshold``)
checks the weighted :func:`eawf.observability.eval.score_envelope` total against
the fixture's ``eval_score_threshold`` (default ``0.85``).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

import eawf.workflow.skills._bootstrap  # noqa: F401  registers every engine skill
from eawf.observability.eval import score_envelope
from eawf.surfaces.render.envelope import OutputEnvelope
from eawf.workflow.skills import dispatch as dispatch_skill
from eawf.workflow.skills import integrate as integrate_skill
from eawf.workflow.skills import verify as verify_skill
from eawf.workflow.skills.catalog import SKILL_CATALOG
from eawf.workflow.skills.engine import Skill, SkillContext, run_skill
from eawf.workflow.skills.registry import lookup

_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
_HAPPY_GOLDEN_DIR = _GOLDEN_DIR / "happy"


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


@dataclass(frozen=True)
class _HappyCase:
    """One complete lifecycle invocation and the transport answers it gets.

    Attributes:
        skill_id: The catalog id; also the golden's file stem.
        args: The invocation arguments, complete enough to reach the verb.
        answers: Per-method canned results; any other method answers with
            an empty projection.
    """

    skill_id: str
    args: dict[str, Any]
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)

    def caller(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Answer one JSON-RPC call from the canned table."""
        return dict(self.answers.get(method, {"header": {"source_cursor": 1}, "rows": []}))


_HAPPY_CASES: tuple[_HappyCase, ...] = (
    _HappyCase(
        skill_id="dispatch",
        args={
            "batch_ref": "batch-1",
            "task": ["task-a"],
            "run": "run-1",
            "run_request": {"prompt": "compiled"},
        },
        answers={
            dispatch_skill.TASK_READ_METHOD: {
                "header": {"source_cursor": 4},
                "rows": [{"key": "task-a", "status": {"state": "known", "value": "PLANNED"}}],
            },
        },
    ),
    _HappyCase(
        skill_id="integrate",
        args={
            "action": "apply",
            "subject_ref": "batch-1",
            "base": {"head_sha": "a" * 40},
            "exit": {"repair_task": "task-9", "rebase_task": "task-9"},
            "diagnostic": "evidence-1",
        },
        answers={
            integrate_skill.DELIVERY_ASSEMBLE_METHOD: {
                "urn": "batch-1",
                "actor": "SKILL-INTEGRATE",
                "idempotency_key": "integrate-eval",
                "branch": "canary/one",
                "subjects": {"cand-1": "Deliver the value"},
            },
            integrate_skill.DELIVERY_INTEGRATE_METHOD: {
                "candidates": ["cand-1"],
                "generation_ids": ["ING-000002"],
                "delivered": True,
                "reason": "batch-1 is delivered in 1 generation(s)",
            },
        },
    ),
    _HappyCase(
        skill_id="verify",
        args={"subject_ref": "batch-1", "mode": "audit"},
        answers={
            verify_skill.DELIVERY_VERIFY_BATCH_METHOD: {
                "head_generation": 3,
                "stage": "CLEARED",
                "blocking_criterion_ids": [],
                "settled_criterion_ids": ["CR-01"],
                "merge_ready": True,
                "reason": "every required criterion cleared on generation 3",
            },
        },
    ),
)
_HAPPY_IDS = [case.skill_id for case in _HAPPY_CASES]


def _run_happy(case: _HappyCase, ctx: SkillContext) -> OutputEnvelope:
    skill_cls = lookup(f"/{case.skill_id}")
    assert skill_cls is not None, f"/{case.skill_id} has no engine class"
    skill = skill_cls(caller=case.caller)  # type: ignore[call-arg]
    return run_skill(skill, dataclasses.replace(ctx, args=dict(case.args)))


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


@pytest.mark.eval
def test_skill_eval_happy_cases_cover_happy_goldens() -> None:
    """Happy goldens exist for exactly the happy cases, each a catalog engine skill."""
    golden_slugs = sorted(p.stem for p in _HAPPY_GOLDEN_DIR.glob("*.json"))
    assert golden_slugs == sorted(_HAPPY_IDS)
    assert set(_HAPPY_IDS) <= set(_CASE_IDS)


@pytest.mark.eval
@pytest.mark.parametrize("case", _HAPPY_CASES, ids=_HAPPY_IDS)
def test_skill_happy_envelope_matches_golden(
    case: _HappyCase, eval_state_dir: Path, eval_ctx: SkillContext
) -> None:
    """A complete invocation reaches its verb and matches the happy golden."""
    golden = cast(
        dict[str, object],
        json.loads((_HAPPY_GOLDEN_DIR / f"{case.skill_id}.json").read_text("utf-8")),
    )
    env = _run_happy(case, eval_ctx)

    assert env.header.status == golden["status"]
    assert env.header.status == "ok", f"{case.skill_id} happy path did not end ok"
    assert isinstance(env.body, dict), f"{case.skill_id} happy body is not typed"
    assert sorted(env.body.keys()) == golden["body_keys"]
    assert env.body["outcome"] == golden["outcome"]
    warnings_count = len(env.footer.warnings) if env.footer.warnings else 0
    repair_count = len(env.footer.repair_commands) if env.footer.repair_commands else 0
    assert warnings_count == golden["warnings_count"]
    assert repair_count == golden["repair_commands_count"]
    score = score_envelope(env, golden)
    assert score.total >= float(golden.get("eval_score_threshold", 0.85))  # type: ignore[arg-type]
