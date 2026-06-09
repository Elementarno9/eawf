"""Engine body-validation gate (P30-I03-W02).

Pin the bind: :func:`eawf.workflow.skills.engine.run_skill` validates a
registered ``dict`` body against its body model on the success/emit path,
BEFORE the :class:`~eawf.surfaces.render.envelope.OutputEnvelope` is built.

Boundary matrix:

- conforming dict   -> emits a normal envelope (no raise).
- drifted dict      -> raises :class:`pydantic.ValidationError` on the emit
                       path, naming the offending extra-forbid key.
- raw markdown str  -> bypasses the gate entirely (str branch is ungated).
- unregistered name -> dict body bypasses the gate (no registered model).

The gate runs on the already-serialized body only; it never constrains the
action's reasoning, only the wire shape the engine is about to emit.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.workflow.skills.engine import (
    ProbeOutcome,
    Skill,
    SkillContext,
    SkillResult,
    run_skill,
)


class _StubSkill(Skill):
    """Test-only skill returning a prefab probe-ok + action result.

    The ``name`` is caller-supplied so a single stub can stand in for a
    registered skill (``/coauthor``) or an unregistered one.
    """

    def __init__(self, name: str, action_result: SkillResult) -> None:
        self.name = name
        self._action_result = action_result

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(ok=True, instrument_probe={"git": "ok"})

    def action(self, ctx: SkillContext) -> SkillResult:
        return self._action_result


def _ctx() -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
        instrument_probe={"git": "ok"},
    )


def test_validate_body_conforming_dict_emits_normal_envelope() -> None:
    """A dict that conforms to the registered model emits unchanged."""
    body = {"kind": "coauthor_resolution", "mode": "project", "trailer": "Co-Authored-By: x"}
    skill = _StubSkill("/coauthor", SkillResult(status="ok", body=body))

    env = run_skill(skill, _ctx())

    assert env.header.skill == "/coauthor"
    assert env.header.status == "ok"
    assert env.body == body


def test_validate_body_drifted_dict_raises_on_emit_path() -> None:
    """A drifted dict (extra-forbid key) raises ValidationError before emit."""
    body = {"mode": "project", "bogus_extra": "drift"}
    skill = _StubSkill("/coauthor", SkillResult(status="ok", body=body))

    with pytest.raises(ValidationError) as excinfo:
        run_skill(skill, _ctx())

    # The offending key is named in the validation message (extra-forbid).
    assert "bogus_extra" in str(excinfo.value)


def test_validate_body_markdown_str_body_is_ungated() -> None:
    """A raw markdown string body bypasses validation (str branch ungated)."""
    markdown = "# Coauthor\n\nresolved trailer policy"
    skill = _StubSkill("/coauthor", SkillResult(status="ok", body=markdown))

    env = run_skill(skill, _ctx())

    assert env.header.status == "ok"
    assert env.body == markdown


def test_validate_body_unregistered_skill_name_dict_is_ungated() -> None:
    """A dict body under a skill name with no registered model bypasses the gate."""
    body = {"anything": "goes", "no_model": True}
    skill = _StubSkill("/not-a-registered-skill", SkillResult(status="ok", body=body))

    env = run_skill(skill, _ctx())

    assert env.header.status == "ok"
    assert env.body == body
