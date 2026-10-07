"""A research round's answer is parsed into its report, or refused with a cause that names fields.

The answers here are the shapes a model writes for the round prompt when it reads the
asked keys loosely: sources as a list, findings as objects or long lines, an outcome
past its cap, extra keys, a report past its cap. The one lossless case is taken; every
other is refused with each field path and why, never the worker's text.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.runtime.daemon.campaign_scheduler import (
    StepAssignment,
    UnreportedRoundError,
    failure_detail,
)
from eawf.runtime.daemon.methods.campaign import MAX_TEXT_BYTES
from eawf.runtime.daemon.methods.campaign_run import parse_round_report, round_prompt

WORKER_TEXT = "worker-private-wording"


def _answer(**body: Any) -> str:
    base: dict[str, Any] = {"report": "# R", "outcome": "held", "sources": 2, "findings": []}
    base.update(body)
    return f"Notes.\n\n```json\n{json.dumps(base)}\n```\n"


# ---- what is taken -----------------------------------------------------------------


def test_sources_given_as_a_list_is_taken_as_its_count() -> None:
    text = _answer(sources=["src/a.py:1", "src/b.py:2", "https://example.org"])

    assert parse_round_report(text, tokens=5).sources == 3


def test_an_empty_sources_list_counts_none() -> None:
    assert parse_round_report(_answer(sources=[]), tokens=None).sources == 0


def test_a_single_finding_at_its_cap_is_kept() -> None:
    line = "x" * 200

    assert parse_round_report(_answer(findings=[line]), tokens=1).findings == (line,)


def test_an_outcome_at_its_cap_is_kept() -> None:
    assert len(parse_round_report(_answer(outcome="o" * 500), tokens=1).outcome) == 500


def test_a_report_at_its_cap_is_kept() -> None:
    assert len(parse_round_report(_answer(report="r" * MAX_TEXT_BYTES), tokens=1).report) == (
        MAX_TEXT_BYTES
    )


# ---- what is refused, and how it says so -------------------------------------------


@pytest.mark.parametrize(
    ("body", "field", "cause"),
    [
        ({"findings": [{"statement": WORKER_TEXT, "evidence": "a.py:1"}]}, "findings.0", "string"),
        ({"findings": [WORKER_TEXT + "y" * 200]}, "findings.0", "at most 200 characters"),
        ({"findings": [f"{WORKER_TEXT}\nsecond line"]}, "findings.0", "pattern"),
        ({"outcome": WORKER_TEXT + "o" * 500}, "outcome", "at most 500 characters"),
        ({"confidence": WORKER_TEXT}, "confidence", "Extra inputs are not permitted"),
        ({"report": WORKER_TEXT + "r" * MAX_TEXT_BYTES}, "report", f"at most {MAX_TEXT_BYTES}"),
        ({"sources": "3"}, "sources", "integer"),
        ({"sources": -1}, "sources", "greater than or equal to 0"),
        ({"report": ""}, "report", "at least 1 character"),
    ],
    ids=[
        "finding-object",
        "finding-past-cap",
        "finding-two-lines",
        "outcome-past-cap",
        "extra-key",
        "report-past-cap",
        "sources-string",
        "sources-negative",
        "report-empty",
    ],
)
def test_a_mismatched_answer_is_refused_naming_the_field_never_the_text(
    body: dict[str, Any], field: str, cause: str
) -> None:
    with pytest.raises(ValueError, match="does not hold the asked shape") as caught:
        parse_round_report(_answer(**body), tokens=1)

    message = str(caught.value)
    assert f"{field}: " in message
    assert cause in message
    assert WORKER_TEXT not in message


def test_a_missing_key_is_named() -> None:
    text = '```json\n{"outcome": "held"}\n```'

    with pytest.raises(ValueError, match="report: Field required"):
        parse_round_report(text, tokens=None)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "no ```json report block"),
        ("```json\n[1, 2]\n```", "no ```json report block"),
        ('```json\n{"report": "# R",}\n```', "not valid JSON"),
    ],
    ids=["empty", "array", "malformed"],
)
def test_an_answer_with_no_usable_block_is_refused(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_round_report(text, tokens=None)


# ---- the prompt states the constraints the parser holds ----------------------------


def test_the_prompt_states_each_cap_and_type_the_parser_holds() -> None:
    assignment = StepAssignment.model_validate(
        {
            "campaign_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/campaign/CAM-0001",
            "campaign_title": "Survey",
            "ordinal": 1,
            "title": "Survey the sources",
            "method": "survey",
            "question": "What does src hold?",
            "run_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000001",
            "round_number": 1,
        }
    )

    prompt = round_prompt(assignment)

    assert "exactly these keys and no other" in prompt
    assert "at most 500 characters" in prompt
    assert "at most 200 characters" in prompt
    assert "a whole number" in prompt
    assert "a list of strings" in prompt


# ---- failure_detail ----------------------------------------------------------------


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int


def test_a_validation_failure_is_detailed_by_path_and_message_without_its_input() -> None:
    with pytest.raises(ValidationError) as caught:
        _Closed.model_validate({"count": WORKER_TEXT, "other": WORKER_TEXT})

    detail = failure_detail(caught.value)

    assert detail.startswith("ValidationError: count: ")
    assert "other: Extra inputs are not permitted" in detail
    assert WORKER_TEXT not in detail


def test_a_root_level_validation_failure_is_named_by_the_report() -> None:
    with pytest.raises(ValidationError) as caught:
        _Closed.model_validate(WORKER_TEXT)

    assert failure_detail(caught.value).startswith("ValidationError: report: ")


def test_any_other_failure_keeps_its_message_on_one_line() -> None:
    assert failure_detail(RuntimeError("provider\n  lost")) == "RuntimeError: provider lost"


def test_a_failure_with_no_message_names_its_class() -> None:
    assert failure_detail(KeyError()) == "KeyError: "


def test_a_long_failure_is_cut_to_five_hundred_characters() -> None:
    assert len(failure_detail(ValueError("v" * 2000))) == 500
    assert len(failure_detail(ValueError("v" * 10))) == len("ValueError: ") + 10


def test_an_unreported_round_carries_its_tokens() -> None:
    error = UnreportedRoundError("no block", tokens=0)

    assert (str(error), error.tokens) == ("no block", 0)
    assert isinstance(error, ValueError)
    assert UnreportedRoundError("x", tokens=None).tokens is None
