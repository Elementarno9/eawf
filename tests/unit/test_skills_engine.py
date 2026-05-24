"""Unit tests for :mod:`eawf.workflow.skills.engine`.

Pin the engine's three contracts:

- Happy path: probe ok + action ok → ``status=ok`` envelope with the
  body and footer fields populated from the action result.
- Probe-fail path: probe returns ``ok=False`` → ``status=blocked``
  envelope with ``footer.repair_commands`` populated; ``skill.action``
  is never called.
- Action-raises path: probe ok + action raises → ``status=failed``
  envelope with the traceback in the body and
  ``footer.repair_commands`` populated. The exception does NOT escape
  the engine.

Plus a few boundary checks: probe also raising, ``failure_repair_commands``
override, and the SkillName/timestamp invariants on the returned envelope.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eawf.surfaces.render.envelope import EnvelopeWarning
from eawf.workflow.skills.engine import (
    ProbeOutcome,
    Skill,
    SkillContext,
    SkillResult,
    run_skill,
)


class _StubResearchSkill(Skill):
    """Test-only skill that returns prefab probe + action outcomes."""

    name = "/research"

    def __init__(
        self,
        probe_outcome: ProbeOutcome,
        action_result: SkillResult | None = None,
        action_exception: Exception | None = None,
        probe_exception: Exception | None = None,
    ) -> None:
        self._probe_outcome = probe_outcome
        self._action_result = action_result
        self._action_exception = action_exception
        self._probe_exception = probe_exception
        self.action_call_count = 0
        self.probe_call_count = 0

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        self.probe_call_count += 1
        if self._probe_exception is not None:
            raise self._probe_exception
        return self._probe_outcome

    def action(self, ctx: SkillContext) -> SkillResult:
        self.action_call_count += 1
        if self._action_exception is not None:
            raise self._action_exception
        assert self._action_result is not None, "test must supply action_result"
        return self._action_result


def _ctx() -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
        instrument_probe={"git": "ok"},
    )


def test_run_skill_happy_path_returns_ok_envelope() -> None:
    skill = _StubResearchSkill(
        probe_outcome=ProbeOutcome(ok=True, instrument_probe={"git": "ok"}),
        action_result=SkillResult(
            status="ok",
            body={"brief_id": "BR-001"},
            persisted_artifacts=["urn:eawf:v1:artifact:QR/A1"],
            persisted_store_records=["urn:eawf:v1:store:QR/research/RB-1"],
            state_mutations=["phases.P00.status=active"],
            evidence_refs=["urn:eawf:v1:commit:QR/abc"],
            next_valid_actions=["eawf prep"],
        ),
    )
    env = run_skill(skill, _ctx())

    assert env.header.skill == "/research"
    assert env.header.status == "ok"
    assert env.header.instrument_probe == {"git": "ok"}
    assert env.body == {"brief_id": "BR-001"}
    assert env.footer.persisted_artifacts == ["urn:eawf:v1:artifact:QR/A1"]
    assert env.footer.persisted_store_records == ["urn:eawf:v1:store:QR/research/RB-1"]
    assert env.footer.state_mutations == ["phases.P00.status=active"]
    assert env.footer.evidence_refs == ["urn:eawf:v1:commit:QR/abc"]
    assert env.footer.next_valid_actions == ["eawf prep"]
    assert env.footer.repair_commands is None
    assert skill.probe_call_count == 1
    assert skill.action_call_count == 1


def test_run_skill_probe_fail_returns_blocked_envelope() -> None:
    """probe returning ok=False short-circuits to a blocked envelope."""
    skill = _StubResearchSkill(
        probe_outcome=ProbeOutcome(
            ok=False,
            instrument_probe={"git": "missing"},
            repair_commands=["brew install git"],
            warnings=[EnvelopeWarning(code="instrument_missing", detail="git absent")],
        ),
    )
    env = run_skill(skill, _ctx())

    assert env.header.status == "blocked"
    assert env.header.instrument_probe == {"git": "missing"}
    assert env.footer.repair_commands == ["brew install git"]
    assert any(w.code == "instrument_missing" for w in env.footer.warnings)
    assert skill.probe_call_count == 1
    # Crucial: action MUST NOT be called when the probe fails.
    assert skill.action_call_count == 0


def test_run_skill_action_raises_returns_failed_envelope() -> None:
    """An exception in action flips status to failed; engine does NOT raise."""
    skill = _StubResearchSkill(
        probe_outcome=ProbeOutcome(ok=True, instrument_probe={"git": "ok"}),
        action_exception=RuntimeError("boom"),
    )
    env = run_skill(skill, _ctx())

    assert env.header.status == "failed"
    assert env.footer.repair_commands == ["see body for traceback"]
    assert isinstance(env.body, str)
    # Traceback contains the exception's class and message.
    assert "RuntimeError" in env.body
    assert "boom" in env.body
    # The probe ran exactly once; action ran but raised.
    assert skill.probe_call_count == 1
    assert skill.action_call_count == 1


def test_run_skill_probe_raises_returns_failed_envelope() -> None:
    """probe raising is treated identically to action raising."""
    skill = _StubResearchSkill(
        probe_outcome=ProbeOutcome(ok=True),  # ignored
        probe_exception=RuntimeError("probe-blew-up"),
    )
    env = run_skill(skill, _ctx())

    assert env.header.status == "failed"
    assert isinstance(env.body, str)
    assert "probe-blew-up" in env.body
    # action MUST NOT be called when probe raises.
    assert skill.action_call_count == 0


def test_run_skill_action_raises_uses_failure_repair_commands_override() -> None:
    """``ctx.failure_repair_commands`` overrides the engine's default."""
    skill = _StubResearchSkill(
        probe_outcome=ProbeOutcome(ok=True),
        action_exception=RuntimeError("boom"),
    )
    ctx = _ctx()
    ctx.failure_repair_commands = ["eawf doctor", "git status"]
    env = run_skill(skill, ctx)

    assert env.header.status == "failed"
    assert env.footer.repair_commands == ["eawf doctor", "git status"]


def test_run_skill_envelope_timestamps_monotonic() -> None:
    """``finished_at >= started_at`` on every envelope the engine emits."""
    skill = _StubResearchSkill(
        probe_outcome=ProbeOutcome(ok=True, instrument_probe={"git": "ok"}),
        action_result=SkillResult(status="ok", body=""),
    )
    env = run_skill(skill, _ctx())
    assert isinstance(env.header.started_at, datetime)
    assert isinstance(env.header.finished_at, datetime)
    assert env.header.started_at.tzinfo == UTC
    assert env.header.finished_at >= env.header.started_at


def test_run_skill_status_blocked_without_repair_uses_default() -> None:
    """If a skill returns blocked without repair_commands, engine fills a default."""
    skill = _StubResearchSkill(
        probe_outcome=ProbeOutcome(ok=True),
        action_result=SkillResult(status="blocked", body="halted"),
    )
    env = run_skill(skill, _ctx())
    assert env.header.status == "blocked"
    assert env.footer.repair_commands == ["see body for details"]


def test_run_skill_status_partial_does_not_set_repair_commands() -> None:
    """``status=partial`` keeps ``repair_commands=None`` (no contract requirement)."""
    skill = _StubResearchSkill(
        probe_outcome=ProbeOutcome(ok=True),
        action_result=SkillResult(status="partial", body="halfway"),
    )
    env = run_skill(skill, _ctx())
    assert env.header.status == "partial"
    assert env.footer.repair_commands is None


def test_run_skill_combines_probe_and_action_warnings() -> None:
    """Footer warnings = probe warnings + action warnings, in that order."""
    skill = _StubResearchSkill(
        probe_outcome=ProbeOutcome(
            ok=True,
            instrument_probe={"git": "ok"},
            warnings=[EnvelopeWarning(code="probe_warn", detail="probe note")],
        ),
        action_result=SkillResult(
            status="ok",
            body="",
            warnings=[EnvelopeWarning(code="action_warn", detail="action note")],
        ),
    )
    env = run_skill(skill, _ctx())
    assert [w.code for w in env.footer.warnings] == ["probe_warn", "action_warn"]


def test_run_skill_needs_user_status_passes_through_user_question_body() -> None:
    """``status=needs_user`` with a user_question body survives the engine."""
    user_question_body: dict[str, object] = {
        "user_question": {
            "question": "Pick an option",
            "options": [
                {"label": "A"},
                {"label": "B"},
            ],
        }
    }
    skill = _StubResearchSkill(
        probe_outcome=ProbeOutcome(ok=True),
        action_result=SkillResult(status="needs_user", body=user_question_body),
    )
    env = run_skill(skill, _ctx())
    assert env.header.status == "needs_user"
    assert env.body == user_question_body
