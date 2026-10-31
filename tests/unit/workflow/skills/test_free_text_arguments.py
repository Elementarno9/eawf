"""Skill argument text that does not split as shell words still parses.

An apostrophe in a research topic leaves a quote open; the text is then read
word by word on whitespace, its flags still become fields, and the schema
check still refuses an unknown flag or a value outside the declared set.
"""

from __future__ import annotations

import pytest

from eawf.workflow.skills.arguments import (
    InvocationRefusedError,
    check_invocation,
    parse_host_arguments,
)
from eawf.workflow.skills.catalog import SKILL_CATALOG, SkillCatalogEntry


def _entry(skill_id: str) -> SkillCatalogEntry:
    entry = SKILL_CATALOG.entry(skill_id)
    assert entry is not None
    return entry


def test_an_apostrophe_topic_parses_with_its_flags() -> None:
    args = parse_host_arguments(_entry("research"), "what's the cost of X --depth deep --rounds 2")
    assert args == {
        "topic": ["what's", "the", "cost", "of", "X"],
        "depth": "deep",
        "rounds": "2",
    }
    check_invocation(_entry("research"), args, lane="operator")


def test_an_equals_flag_parses_beside_an_apostrophe_topic() -> None:
    args = parse_host_arguments(_entry("research"), "--depth=shallow don't cache")
    assert args == {"topic": ["don't", "cache"], "depth": "shallow"}


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("what's next --bogus x", "unknown_argument"),
        ("what's next --depth bottomless", "invalid_value"),
        ("what's next --rounds 9", "invalid_value"),
    ],
)
def test_the_tolerant_read_still_refuses_an_invalid_flag(text: str, code: str) -> None:
    args = parse_host_arguments(_entry("research"), text)
    with pytest.raises(InvocationRefusedError, match=code):
        check_invocation(_entry("research"), args, lane="operator")


def test_an_unterminated_quote_is_a_subject_word() -> None:
    assert parse_host_arguments(_entry("research"), '"unterminated') == {"topic": ['"unterminated']}


def test_a_trailing_backslash_is_a_subject_word() -> None:
    assert parse_host_arguments(_entry("research"), "caching \\") == {"topic": ["caching", "\\"]}


def test_only_flags_after_an_apostrophe_value_leave_no_subject() -> None:
    assert parse_host_arguments(_entry("research"), "--question what's --rounds 1") == {
        "question": "what's",
        "rounds": "1",
    }


def test_empty_text_is_no_arguments() -> None:
    assert parse_host_arguments(_entry("research"), "") == {}


def test_a_tolerant_action_word_is_still_the_action() -> None:
    args = parse_host_arguments(_entry("accept"), "show MLS-0001 --reason it's")
    assert args == {"action": "show", "subject_ref": ["MLS-0001"], "reason": "it's"}
