"""Unit tests for :class:`eawf.workflow.skills.review.ReviewSkill`.

Pin the Phase 4 W02 acceptance contract for ``/review``:

- Happy path → ``status=ok`` with a populated :class:`ReviewBody`.
- Probe-blocked path → ``status=blocked`` + repair commands.
- ``recommendation`` arg accepts the four frozen literals; bad input
  falls back to ``"comment"``.
- ``post=true`` toggles ``body.posted``.
- Body schema (``pr_url``, ``base``, ``head``, ``recommendation``)
  populated.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from eawf.render.envelope import EnvelopeWarning
from eawf.workflow.skills.bodies.review import ReviewBody
from eawf.workflow.skills.engine import ProbeOutcome, SkillContext, run_skill
from eawf.workflow.skills.review import ReviewSkill


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return state_dir


def _ctx() -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
    )


def test_review_default_recommendation_is_comment(state_dir: Path) -> None:
    skill = ReviewSkill()
    env = run_skill(skill, _ctx())
    assert env.header.status == "ok"
    body = ReviewBody.model_validate(cast(dict, env.body))
    assert body.recommendation == "comment"
    assert body.posted is False


def test_review_recommendation_approve_honoured(state_dir: Path) -> None:
    skill = ReviewSkill()
    ctx = _ctx()
    ctx.args = {"recommendation": "approve"}
    env = run_skill(skill, ctx)
    body = ReviewBody.model_validate(cast(dict, env.body))
    assert body.recommendation == "approve"


def test_review_recommendation_request_changes_honoured(state_dir: Path) -> None:
    skill = ReviewSkill()
    ctx = _ctx()
    ctx.args = {"recommendation": "request_changes"}
    env = run_skill(skill, ctx)
    body = ReviewBody.model_validate(cast(dict, env.body))
    assert body.recommendation == "request_changes"


def test_review_invalid_recommendation_falls_back_to_comment(state_dir: Path) -> None:
    skill = ReviewSkill()
    ctx = _ctx()
    ctx.args = {"recommendation": "wat"}
    env = run_skill(skill, ctx)
    body = ReviewBody.model_validate(cast(dict, env.body))
    assert body.recommendation == "comment"


def test_review_post_flag_toggles_posted(state_dir: Path) -> None:
    skill = ReviewSkill()
    ctx = _ctx()
    ctx.args = {"post": True}
    env = run_skill(skill, ctx)
    body = ReviewBody.model_validate(cast(dict, env.body))
    assert body.posted is True


def test_review_explicit_pr_url_propagates(state_dir: Path) -> None:
    skill = ReviewSkill()
    ctx = _ctx()
    ctx.args = {"pr": "https://example.com/pr/1", "base": "develop", "head": "topic"}
    env = run_skill(skill, ctx)
    body = ReviewBody.model_validate(cast(dict, env.body))
    assert body.pr_url == "https://example.com/pr/1"
    assert body.base == "develop"
    assert body.head == "topic"


def test_review_emits_one_event_per_step(state_dir: Path) -> None:
    skill = ReviewSkill()
    env = run_skill(skill, _ctx())
    events_path = state_dir / "store" / "event.jsonl"
    assert events_path.exists()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    # Steps: resolve_pr, fetch_metadata, diff, dispatch, template_check,
    # findings → 6 (post is conditional).
    assert len(lines) == 6
    assert len(env.footer.persisted_store_records) == 6


def test_review_post_event_added_when_posting(state_dir: Path) -> None:
    skill = ReviewSkill()
    ctx = _ctx()
    ctx.args = {"post": True}
    env = run_skill(skill, ctx)
    assert env.header.status == "ok"
    events_path = state_dir / "store" / "event.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 7  # 6 + post
    assert any("review.post" in ln for ln in lines)


def test_review_probe_blocked_short_circuits(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.workflow.skills import review as review_module

    def _blocked(self: object, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(
            ok=False,
            instrument_probe={"git": "missing"},
            repair_commands=["install git"],
            warnings=[EnvelopeWarning(code="instrument_missing", detail="x")],
        )

    monkeypatch.setattr(review_module.ReviewSkill, "probe", _blocked)
    env = run_skill(review_module.ReviewSkill(), _ctx())
    assert env.header.status == "blocked"
    assert env.footer.repair_commands == ["install git"]


def test_review_skill_registered_with_canonical_name() -> None:
    from eawf.workflow.skills import registry

    cls = registry.lookup("/review")
    assert cls is ReviewSkill
