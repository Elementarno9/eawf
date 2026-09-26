"""``/memory`` asks the operator a typed question whenever it stops for one.

A named verb without a name ends ``needs_user``. The conduct trigger for a
choice point holds only when ``body.user_question`` is typed and every option
says what it does, so the envelope must carry that shape, and a live run must
record no practice miss. The lifecycle skills' happy-path goldens sit beside
their refusal goldens so the eval harness pins both envelopes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.platform.rules.triggers import PRACTICE_MISS_CODE, evaluate_practice_triggers
from eawf.workflow.skills.bodies.memory import MemoryBody
from eawf.workflow.skills.engine import SkillContext, run_skill
from eawf.workflow.skills.memory import MemorySkill, missing_name_question

_EVAL_GOLDEN_DIR = Path(__file__).resolve().parents[4] / "tests" / "eval" / "golden"


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the skill's state and instrument probe under *tmp_path*."""
    root = tmp_path / ".ea"
    root.mkdir()
    monkeypatch.setenv("EA_STATE", str(root / "state.json"))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(root / "instrument-probe.json"))
    return root


def _ctx(args: dict[str, Any]) -> SkillContext:
    return SkillContext(scope="urn:eawf:v1:state:EVL/P00", session="SES-memory", args=args)


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({}, id="empty-defaults-to-save"),
        pytest.param({"verb": "save"}, id="save"),
        pytest.param({"verb": "forget"}, id="forget"),
        pytest.param({"verb": "save", "name": ""}, id="empty-name"),
    ],
)
def test_memory_action_asks_a_typed_question_without_a_name(
    args: dict[str, Any], state_dir: Path
) -> None:
    """Every needs_user result carries a typed question the choice trigger accepts."""
    result = MemorySkill().action(_ctx(args))

    assert result.status == "needs_user"
    assert isinstance(result.body, dict)
    body = MemoryBody.model_validate(result.body)
    assert body.user_question is not None
    assert all(option.description for option in body.user_question.options)
    evaluations = evaluate_practice_triggers(result.status, result.body)
    assert [e.trigger.obligation_id for e in evaluations] == ["conduct.typed-choice"]
    assert all(e.met for e in evaluations)


def test_run_skill_memory_records_no_practice_miss(state_dir: Path) -> None:
    """The live envelope for a nameless save carries no typed-choice miss warning."""
    env = run_skill(MemorySkill(), _ctx({"verb": "save"}))

    assert env.header.status == "needs_user"
    codes = [warning.code for warning in env.footer.warnings or []]
    assert PRACTICE_MISS_CODE not in codes


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"verb": "list"}, id="list-takes-no-name"),
        pytest.param({"verb": "save", "name": "prefs"}, id="named-save"),
    ],
)
def test_memory_action_asks_nothing_when_it_can_proceed(
    args: dict[str, Any], state_dir: Path
) -> None:
    """A result that does not stop for the operator carries no question."""
    result = MemorySkill().action(_ctx(args))

    assert result.status == "ok"
    assert isinstance(result.body, dict)
    assert "user_question" not in result.body
    assert evaluate_practice_triggers(result.status, result.body) == ()


@pytest.mark.parametrize("verb", ["save", "forget"])
def test_missing_name_question_names_the_verb(verb: str) -> None:
    """The question is about the verb that stopped, with three described options."""
    question = missing_name_question(verb)

    assert f"/memory {verb}" in question.question
    assert [option.label for option in question.options] == [
        "name the entry",
        "list entries first",
        "cancel",
    ]


@pytest.mark.parametrize("verb", ["list", "", "drop"])
def test_missing_name_question_rejects_an_unnamed_verb(verb: str) -> None:
    """Error path: a verb that takes no name has no missing-name question."""
    with pytest.raises(ValueError, match="does not take a memory entry name"):
        missing_name_question(verb)


def test_memory_body_rejects_a_single_option_question() -> None:
    """Error path: a question below the two-option floor fails validation."""
    with pytest.raises(ValidationError, match="options must contain 2-4 entries"):
        MemoryBody.model_validate(
            {
                "verb": "save",
                "tier": "working",
                "user_question": {"question": "which?", "options": [{"label": "one"}]},
            }
        )


@pytest.mark.parametrize("slug", ["dispatch", "integrate", "verify"])
def test_lifecycle_eval_goldens_pin_a_happy_path_beside_the_refusal(slug: str) -> None:
    """Each lifecycle skill has a refusal golden and an ok happy-path golden."""
    refusal = json.loads((_EVAL_GOLDEN_DIR / f"{slug}.json").read_text("utf-8"))
    happy = json.loads((_EVAL_GOLDEN_DIR / "happy" / f"{slug}.json").read_text("utf-8"))

    assert refusal["status"] == "blocked"
    assert happy["status"] == "ok"
    assert happy["skill"] == refusal["skill"] == f"/{slug}"
