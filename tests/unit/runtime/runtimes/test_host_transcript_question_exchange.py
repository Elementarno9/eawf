"""An AskUserQuestion answer, checked against the host transcript that recorded it.

Every transcript here is a small synthetic JSONL file under ``tmp_path`` shaped like a
Claude Code session: an assistant line whose content holds the ``tool_use`` block, and a
user line whose content holds the matching ``tool_result`` and whose ``toolUseResult``
carries the questions and the answers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.runtime.runtimes.host_transcript import question_exchange

TOOL_USE_ID: Final = "toolu_01QUESTION"

QUESTIONS: Final[list[dict[str, Any]]] = [
    {
        "question": "[eawf act=ACT-0001 rev=2]\nApprove the plan for MLS-0001?",
        "header": "Approve",
        "multiSelect": False,
        "options": [
            {"label": "Approve the plan", "description": "Apply it next."},
            {"label": "Decline it", "description": "Nothing is applied."},
        ],
    }
]

ANSWERS: Final[dict[str, str]] = {QUESTIONS[0]["question"]: "Approve the plan"}


def tool_use_line(
    *, tool_use_id: str = TOOL_USE_ID, name: str = "AskUserQuestion", **input_extra: Any
) -> dict[str, Any]:
    """Return the assistant line that asks the question."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Asking now."},
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": name,
                    "input": {"questions": QUESTIONS, **input_extra},
                },
            ],
        },
    }


def result_line(
    *, tool_use_id: str = TOOL_USE_ID, answers: dict[str, str] | None = None
) -> dict[str, Any]:
    """Return the user line that carries the answer."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}],
        },
        "toolUseResult": {
            "questions": QUESTIONS,
            "answers": ANSWERS if answers is None else answers,
            "annotations": {},
        },
    }


def transcript(tmp_path: Path, *lines: dict[str, Any] | str) -> Path:
    """Write *lines* as a JSONL transcript under *tmp_path* and return its path."""
    path = tmp_path / "session.jsonl"
    body = [line if isinstance(line, str) else json.dumps(line) for line in lines]
    path.write_text("".join(f"{item}\n" for item in body), encoding="utf-8")
    return path


def test_a_question_and_its_answer_on_file_match(tmp_path: Path) -> None:
    path = transcript(
        tmp_path,
        {"type": "user", "message": {"role": "user", "content": "Plan it."}},
        tool_use_line(),
        result_line(),
    )
    assert question_exchange(path, TOOL_USE_ID, QUESTIONS, ANSWERS) == "matched"


def test_the_questions_compare_as_values_not_as_containers(tmp_path: Path) -> None:
    """A caller's tuples equal the transcript's lists; the check reads values."""
    path = transcript(tmp_path, tool_use_line(), result_line())
    as_tuples = tuple({**item, "options": tuple(item["options"])} for item in QUESTIONS)
    assert question_exchange(path, TOOL_USE_ID, as_tuples, ANSWERS) == "matched"


def test_a_question_whose_answer_is_not_yet_flushed_is_tool_use_only(tmp_path: Path) -> None:
    path = transcript(tmp_path, tool_use_line())
    assert question_exchange(path, TOOL_USE_ID, QUESTIONS, ANSWERS) == "tool_use_only"


def test_a_missing_transcript_is_absent(tmp_path: Path) -> None:
    missing = tmp_path / "gone.jsonl"
    assert question_exchange(missing, TOOL_USE_ID, QUESTIONS, ANSWERS) == "absent"


def test_an_unreadable_transcript_is_absent(tmp_path: Path) -> None:
    assert question_exchange(tmp_path, TOOL_USE_ID, QUESTIONS, ANSWERS) == "absent"


def test_an_empty_transcript_is_absent(tmp_path: Path) -> None:
    assert question_exchange(transcript(tmp_path), TOOL_USE_ID, QUESTIONS, ANSWERS) == "absent"


def test_a_tool_use_id_the_transcript_never_saw_is_absent(tmp_path: Path) -> None:
    path = transcript(tmp_path, tool_use_line(), result_line())
    assert question_exchange(path, "toolu_01OTHER", QUESTIONS, ANSWERS) == "absent"


def test_an_answer_without_its_question_is_absent(tmp_path: Path) -> None:
    path = transcript(tmp_path, result_line())
    assert question_exchange(path, TOOL_USE_ID, QUESTIONS, ANSWERS) == "absent"


def test_an_empty_tool_use_id_is_absent(tmp_path: Path) -> None:
    path = transcript(tmp_path, tool_use_line(), result_line())
    assert question_exchange(path, "", QUESTIONS, ANSWERS) == "absent"


def test_an_id_only_named_in_prose_is_absent(tmp_path: Path) -> None:
    """The prefilter admits the line, but only a tool_use block with that id counts."""
    prose = {"type": "user", "message": {"role": "user", "content": f"see {TOOL_USE_ID}"}}
    path = transcript(tmp_path, prose)
    assert question_exchange(path, TOOL_USE_ID, QUESTIONS, ANSWERS) == "absent"


def test_malformed_lines_are_skipped(tmp_path: Path) -> None:
    path = transcript(
        tmp_path, f"{{not json {TOOL_USE_ID}", f'["{TOOL_USE_ID}"]', tool_use_line(), result_line()
    )
    assert question_exchange(path, TOOL_USE_ID, QUESTIONS, ANSWERS) == "matched"


def test_edited_questions_are_a_mismatch(tmp_path: Path) -> None:
    path = transcript(tmp_path, tool_use_line(), result_line())
    edited = [{**QUESTIONS[0], "options": QUESTIONS[0]["options"][:1]}]
    assert question_exchange(path, TOOL_USE_ID, edited, ANSWERS) == "mismatch"


def test_no_questions_against_a_recorded_one_are_a_mismatch(tmp_path: Path) -> None:
    path = transcript(tmp_path, tool_use_line(), result_line())
    assert question_exchange(path, TOOL_USE_ID, [], ANSWERS) == "mismatch"


def test_a_different_answer_is_a_mismatch(tmp_path: Path) -> None:
    path = transcript(tmp_path, tool_use_line(), result_line())
    declined = {QUESTIONS[0]["question"]: "Decline it"}
    assert question_exchange(path, TOOL_USE_ID, QUESTIONS, declined) == "mismatch"


def test_an_answer_on_file_that_differs_is_a_mismatch(tmp_path: Path) -> None:
    path = transcript(tmp_path, tool_use_line(), result_line(answers={}))
    assert question_exchange(path, TOOL_USE_ID, QUESTIONS, ANSWERS) == "mismatch"


def test_a_pre_answered_question_is_a_mismatch(tmp_path: Path) -> None:
    """Answers supplied in the tool input mean nobody was prompted."""
    path = transcript(tmp_path, tool_use_line(answers=ANSWERS), result_line())
    assert question_exchange(path, TOOL_USE_ID, QUESTIONS, ANSWERS) == "mismatch"


def test_a_pre_answered_question_with_no_result_is_still_a_mismatch(tmp_path: Path) -> None:
    path = transcript(tmp_path, tool_use_line(answers=ANSWERS))
    assert question_exchange(path, TOOL_USE_ID, QUESTIONS, ANSWERS) == "mismatch"


def test_another_tool_under_the_same_id_is_a_mismatch(tmp_path: Path) -> None:
    path = transcript(tmp_path, tool_use_line(name="Bash"), result_line())
    assert question_exchange(path, TOOL_USE_ID, QUESTIONS, ANSWERS) == "mismatch"


@pytest.mark.parametrize("payload", [None, "matched", ["answers"]], ids=["null", "str", "list"])
def test_a_result_line_whose_payload_is_not_a_mapping_is_not_an_answer(
    tmp_path: Path, payload: object
) -> None:
    line = {**result_line(), "toolUseResult": payload}
    path = transcript(tmp_path, tool_use_line(), line)
    assert question_exchange(path, TOOL_USE_ID, QUESTIONS, ANSWERS) == "tool_use_only"
