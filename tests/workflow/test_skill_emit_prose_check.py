"""Tests for the advisory prose chokepoint bound at the skill emit path.

Pins SKILL-5 (P30-I03-W05): :func:`eawf.workflow.skills.engine._prose_warnings`
runs ``validate_prose`` over the emit body and folds findings into a
``prose_clarity`` footer warning. The contract is ADVISORY end-to-end:

- A clean markdown body yields no ``prose_clarity`` warning and ``status=ok``.
- A body tripping a deterministic leg (EAWF014 manual-wrap) adds a
  ``footer.warnings`` entry with ``code=prose_clarity`` while ``status`` stays
  ``ok`` -- the check never flips the envelope to blocked.
- A dict body is normalized to markdown (via
  :func:`~eawf.surfaces.render.envelope.body_to_markdown`) BEFORE the check, so
  a dict that renders to wrapped prose still carries the warning.
- A prose-checker crash is caught (fail-open): emit still succeeds.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.render.envelope import EnvelopeBody, EnvelopeWarning
from eawf.workflow.skills.engine import (
    PROSE_CLARITY_CODE,
    ProbeOutcome,
    Skill,
    SkillContext,
    SkillResult,
    run_skill,
)

# A body that wraps a single sentence across two physical plain-prose lines.
# The first line does not end with sentence punctuation, so the EAWF014
# no-manual-wrap leg reports a finding -- the deterministic trip the advisory
# check folds into the prose_clarity warning.
_WRAPPED_BODY = (
    "This sentence is deliberately wrapped across two physical\n"
    "lines so the no-manual-wrap lint reports a finding here."
)


class _StubSkill(Skill):
    """Test-only skill returning a prefab probe-ok + action result."""

    def __init__(self, name: str, body: EnvelopeBody) -> None:
        self.name = name
        self._body = body

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(ok=True, instrument_probe={"git": "ok"})

    def action(self, ctx: SkillContext) -> SkillResult:
        return SkillResult(status="ok", body=self._body)


def _ctx() -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
        instrument_probe={"git": "ok"},
    )


def _prose_codes(env_warnings: list[EnvelopeWarning]) -> list[str]:
    """Return the codes of the prose_clarity footer warnings (helper)."""
    return [w.code for w in env_warnings if w.code == PROSE_CLARITY_CODE]


def test_clean_body_yields_no_prose_clarity_warning() -> None:
    """A clean markdown body emits status ok and no prose_clarity warning."""
    skill = _StubSkill("/research", body="All done.")
    env = run_skill(skill, _ctx())

    assert env.header.status == "ok"
    assert _prose_codes(env.footer.warnings) == []


def test_wrapped_body_adds_prose_clarity_warning_status_stays_ok() -> None:
    """A manually-wrapped body adds a prose_clarity warning, status stays ok."""
    skill = _StubSkill("/research", body=_WRAPPED_BODY)
    env = run_skill(skill, _ctx())

    # Advisory: the warning rides the footer but the status is untouched.
    assert env.header.status == "ok"
    warnings = [w for w in env.footer.warnings if w.code == PROSE_CLARITY_CODE]
    assert len(warnings) == 1
    assert "advisory" in warnings[0].detail


def test_advisory_check_never_flips_status_to_blocked() -> None:
    """Even a finding-heavy body keeps status ok -- never blocked."""
    skill = _StubSkill("/research", body=_WRAPPED_BODY)
    env = run_skill(skill, _ctx())

    assert env.header.status != "blocked"
    assert env.header.status == "ok"


def test_dict_body_is_normalized_to_markdown_before_check() -> None:
    """A dict body that renders to wrapped prose carries the prose_clarity warning.

    The skill name is unregistered in ``SKILL_BODY_MODELS`` so the dict body
    bypasses the per-skill body validator and reaches the prose pass; the
    dict is normalized through ``body_to_markdown`` (sorted-key YAML) whose
    long-scalar fold trips EAWF014, proving the check ran on the rendered
    markdown surface rather than on the raw dict.
    """
    dict_body: dict[str, object] = {
        "note": (
            "This is a very long sentence that the YAML dumper folds across "
            "multiple physical lines because it exceeds the eighty column "
            "default width by a wide margin indeed"
        )
    }
    skill = _StubSkill("/mockup", body=dict_body)
    env = run_skill(skill, _ctx())

    assert env.header.status == "ok"
    assert env.body == dict_body
    warnings = [w for w in env.footer.warnings if w.code == PROSE_CLARITY_CODE]
    assert len(warnings) == 1


def test_prose_checker_exception_does_not_break_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash in the prose chokepoint is caught (fail-open): emit succeeds."""

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("prose-lint boom")

    monkeypatch.setattr(
        "eawf.platform.lint.validate_prose.validate_prose",
        _boom,
    )
    skill = _StubSkill("/research", body=_WRAPPED_BODY)
    env = run_skill(skill, _ctx())

    # Emit still succeeds; the crash never escapes and adds no warning.
    assert env.header.status == "ok"
    assert _prose_codes(env.footer.warnings) == []


def test_prose_warning_combines_after_probe_and_action_warnings() -> None:
    """The prose_clarity warning appends after probe + action warnings."""

    class _WarnSkill(_StubSkill):
        def probe(self, ctx: SkillContext) -> ProbeOutcome:
            return ProbeOutcome(
                ok=True,
                instrument_probe={"git": "ok"},
                warnings=[EnvelopeWarning(code="probe_warn", detail="probe note")],
            )

        def action(self, ctx: SkillContext) -> SkillResult:
            return SkillResult(
                status="ok",
                body=_WRAPPED_BODY,
                warnings=[EnvelopeWarning(code="action_warn", detail="action note")],
            )

    env = run_skill(_WarnSkill("/research", body=_WRAPPED_BODY), _ctx())
    codes = [w.code for w in env.footer.warnings]
    assert codes == ["probe_warn", "action_warn", PROSE_CLARITY_CODE]
