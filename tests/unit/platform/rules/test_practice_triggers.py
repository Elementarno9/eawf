"""Tests for the practice triggers the skill engine evaluates at decision points."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import IncidentSeverity, StoreKind
from eawf.kernel.store.paths import store_path
from eawf.platform.rules.conduct import conduct_obligation_ids, read_conduct_deviations
from eawf.platform.rules.triggers import (
    DECISION_POINTS,
    PRACTICE_MISS_CODE,
    PRACTICE_TRIGGER_EVENT_TYPE,
    PRACTICE_TRIGGERS,
    PracticeTrigger,
    evaluate_practice_triggers,
    fire_practice_triggers,
    resolve_trigger_runtime,
)
from eawf.surfaces.render.envelope import EnvelopeBody, EnvelopeStatus
from eawf.workflow.skills.engine import (
    ProbeOutcome,
    Skill,
    SkillContext,
    SkillResult,
    run_skill,
)

_SCOPE = "P01-I01-W01"
_SESSION = "urn:eawf:v1:store:QR/sessions/SES-1"


class _StubSkill(Skill):
    """A skill whose action returns a prefab terminal result."""

    name = "/stub-practice"

    def __init__(self, status: EnvelopeStatus, body: EnvelopeBody) -> None:
        self._status = status
        self._body = body

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(ok=True)

    def action(self, ctx: SkillContext) -> SkillResult:
        return SkillResult(status=self._status, body=self._body)


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".ea" / "state.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setenv("EA_STATE", str(path))
    monkeypatch.delenv("EAWF_COAUTHOR_RUNTIME", raising=False)
    monkeypatch.delenv("EA_COAUTHOR_RUNTIME", raising=False)
    return path


def _events(state_path: Path) -> list[dict[str, Any]]:
    path = store_path(state_path, StoreKind.EVENT)
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [row for row in rows if row["payload"]["event_type"] == PRACTICE_TRIGGER_EVENT_TYPE]


def _question(*descriptions: str | None) -> dict[str, Any]:
    return {
        "user_question": {
            "question": "Which layout?",
            "options": [
                {"label": f"option-{index}", "description": description}
                for index, description in enumerate(descriptions)
            ],
        }
    }


def _coordination(
    parallel: list[str], sequential: list[str], constraint: str = ""
) -> dict[str, Any]:
    return {
        "kind": "coordination_report",
        "batch_ref": "B-1",
        "plan": {"parallel": parallel, "sequential": sequential, "constraint": constraint},
        "outcome": "frontier_empty",
        "reason": "done",
    }


def _verification(outcome: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "verification_report",
        "subject_ref": "B-1",
        "mode": "gates",
        "rows": rows,
        "outcome": outcome,
        "reason": "judged",
    }


def _row(verdict: str = "passed", falsifier: str = "uv run pytest -q") -> dict[str, Any]:
    return {"criterion_id": "CR-01", "verdict": verdict, "falsifier": falsifier}


def _ctx() -> SkillContext:
    return SkillContext(scope=_SCOPE, session=_SESSION)


# Gate-fire proof: the engine hook is what makes a run reaching a decision
# point record the trigger; removing the hook from run_skill reds these.


def test_run_skill_choice_decision_point_emits_trigger_event_and_records_miss(
    state_path: Path,
) -> None:
    envelope = run_skill(_StubSkill("needs_user", _question("does A", None)), _ctx())

    events = _events(state_path)
    assert len(events) == 1
    extras = events[0]["payload"]["extras"]
    assert extras == {
        "decision_point": "choice",
        "obligation_id": "conduct.typed-choice",
        "met": False,
    }
    assert events[0]["id"] in envelope.footer.persisted_store_records
    deviations = read_conduct_deviations(state_path)
    assert [row.obligation_id for row in deviations] == ["conduct.typed-choice"]
    assert deviations[0].detection == "practice_trigger"
    assert deviations[0].evidence_ref == events[0]["id"]
    assert deviations[0].run_id == _SESSION
    assert deviations[0].scope_id == _SCOPE
    assert deviations[0].runtime == "claude"
    assert [w.code for w in envelope.footer.warnings if w.code == PRACTICE_MISS_CODE] == [
        PRACTICE_MISS_CODE
    ]


def test_run_skill_choice_met_emits_trigger_event_without_deviation(state_path: Path) -> None:
    envelope = run_skill(_StubSkill("needs_user", _question("does A", "does B")), _ctx())

    events = _events(state_path)
    assert len(events) == 1
    assert events[0]["payload"]["extras"]["met"] is True
    assert read_conduct_deviations(state_path) == ()
    assert not [w for w in envelope.footer.warnings if w.code == PRACTICE_MISS_CODE]


def test_run_skill_dispatch_decision_point_records_unreasoned_sequence(
    state_path: Path,
) -> None:
    run_skill(_StubSkill("ok", _coordination(["T-1"], ["T-2", "T-3"])), _ctx())

    events = _events(state_path)
    assert [e["payload"]["extras"]["decision_point"] for e in events] == ["dispatch"]
    assert [row.obligation_id for row in read_conduct_deviations(state_path)] == [
        "conduct.concurrent-independent-dispatch"
    ]


def test_run_skill_report_decision_point_records_unevidenced_pass(state_path: Path) -> None:
    run_skill(_StubSkill("ok", _verification("passed", [_row(falsifier="")])), _ctx())

    deviations = read_conduct_deviations(state_path)
    assert [row.obligation_id for row in deviations] == ["conduct.verdict-needs-evidence"]
    assert deviations[0].severity == IncidentSeverity.HIGH


def test_run_skill_no_decision_point_writes_nothing(state_path: Path) -> None:
    envelope = run_skill(_StubSkill("ok", "plain markdown"), _ctx())

    assert _events(state_path) == []
    assert read_conduct_deviations(state_path) == ()
    assert envelope.footer.persisted_store_records == []


def test_run_skill_trigger_failure_keeps_the_envelope(
    state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(**_: object) -> None:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr("eawf.platform.rules.triggers.fire_practice_triggers", _raise)
    envelope = run_skill(_StubSkill("needs_user", _question("a", None)), _ctx())

    assert envelope.header.status == "needs_user"
    assert not [w for w in envelope.footer.warnings if w.code == PRACTICE_MISS_CODE]


# Declaration invariants.


def test_practice_triggers_name_compiled_conduct_obligations() -> None:
    assert {trigger.obligation_id for trigger in PRACTICE_TRIGGERS} <= conduct_obligation_ids()


def test_practice_triggers_cover_every_decision_point() -> None:
    assert DECISION_POINTS == ("dispatch", "choice", "report")
    assert {trigger.decision_point for trigger in PRACTICE_TRIGGERS} == set(DECISION_POINTS)


# evaluate_practice_triggers: boundaries.


def test_evaluate_practice_triggers_empty_trigger_set_fires_nothing() -> None:
    assert evaluate_practice_triggers("needs_user", _question(None, None), triggers=()) == ()


def test_evaluate_practice_triggers_string_body_ok_fires_nothing() -> None:
    assert evaluate_practice_triggers("ok", "") == ()


def test_evaluate_practice_triggers_free_text_question_is_a_miss() -> None:
    (evaluation,) = evaluate_practice_triggers("needs_user", "Which layout do you want?")
    assert evaluation.trigger.obligation_id == "conduct.typed-choice"
    assert evaluation.met is False


def test_evaluate_practice_triggers_question_without_options_is_a_miss() -> None:
    (evaluation,) = evaluate_practice_triggers("needs_user", {"user_question": {"options": []}})
    assert evaluation.met is False


def test_evaluate_practice_triggers_blank_description_is_a_miss() -> None:
    (evaluation,) = evaluate_practice_triggers("needs_user", _question("does A", "   "))
    assert evaluation.met is False


def test_evaluate_practice_triggers_single_unit_dispatch_does_not_fire() -> None:
    assert evaluate_practice_triggers("ok", _coordination([], ["T-1"])) == ()


def test_evaluate_practice_triggers_two_units_dispatch_fires() -> None:
    (evaluation,) = evaluate_practice_triggers("ok", _coordination(["T-1", "T-2"], []))
    assert evaluation.trigger.decision_point == "dispatch"
    assert evaluation.met is True


def test_evaluate_practice_triggers_sequence_with_constraint_is_met() -> None:
    body = _coordination([], ["T-1", "T-2"], constraint="T-2 consumes T-1's schema")
    (evaluation,) = evaluate_practice_triggers("ok", body)
    assert evaluation.met is True


def test_evaluate_practice_triggers_non_pass_report_does_not_fire() -> None:
    assert evaluate_practice_triggers("ok", _verification("failed", [])) == ()


def test_evaluate_practice_triggers_pass_with_no_rows_is_a_miss() -> None:
    (evaluation,) = evaluate_practice_triggers("ok", _verification("passed", []))
    assert evaluation.met is False


def test_evaluate_practice_triggers_pass_with_unverified_row_is_a_miss() -> None:
    body = _verification("passed", [_row(), _row(verdict="unverified")])
    (evaluation,) = evaluate_practice_triggers("ok", body)
    assert evaluation.met is False


def test_evaluate_practice_triggers_pass_with_evidenced_rows_is_met() -> None:
    (evaluation,) = evaluate_practice_triggers("ok", _verification("passed", [_row()]))
    assert evaluation.met is True


# resolve_trigger_runtime.


def test_resolve_trigger_runtime_empty_env_defaults_to_claude() -> None:
    assert resolve_trigger_runtime({}) == "claude"


def test_resolve_trigger_runtime_alias_resolves_to_codex() -> None:
    assert resolve_trigger_runtime({"EAWF_COAUTHOR_RUNTIME": "codex-cli"}) == "codex"


def test_resolve_trigger_runtime_unknown_runtime_defaults_to_claude() -> None:
    assert resolve_trigger_runtime({"EAWF_COAUTHOR_RUNTIME": "unknown-agent"}) == "claude"


# fire_practice_triggers: error paths.


def test_fire_practice_triggers_no_fire_touches_no_store(tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "state.json"
    outcome = fire_practice_triggers(
        status="ok", body="", scope_id=_SCOPE, run_id=_SESSION, state_path=missing
    )
    assert outcome.evaluations == ()
    assert not missing.parent.exists()


def test_fire_practice_triggers_unknown_obligation_raises(
    state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bogus = PracticeTrigger(
        obligation_id="conduct.not-an-obligation",
        decision_point="choice",
        severity=IncidentSeverity.LOW,
        fires=lambda status, body: True,
        met=lambda body: False,
    )
    monkeypatch.setattr(
        "eawf.platform.rules.triggers.evaluate_practice_triggers.__kwdefaults__",
        {"triggers": (bogus,)},
    )
    with pytest.raises(ValidationError, match="not a compiled conduct obligation"):
        fire_practice_triggers(
            status="needs_user", body="", scope_id=_SCOPE, run_id=_SESSION, state_path=state_path
        )
