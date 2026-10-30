"""The research skill offers swift and rounds modes and leaves a checked brief.

The page names both modes and the brief's sections; the host hook admits
``--rounds`` and ``--depth`` within the values the grammar declares and
refuses any other; the brief check passes a brief holding every section, in
order, with a sound References table, and names what is wrong with any other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.host_lane import judge_host_skill_invocation
from eawf.workflow.skills.arguments import (
    InvocationRefusedError,
    argument_schema,
    check_invocation,
)
from eawf.workflow.skills.bodies.chassis import check_skill_page, shipped_skill_page
from eawf.workflow.skills.catalog import SKILL_CATALOG, SkillCatalogEntry
from eawf.workflow.skills.research_brief import BRIEF_SECTIONS, check_research_brief

_BRIEF = """# Which store holds the tiers

## Question

Which module owns the store tiers [1]?

## Findings

The tiers module names every collection [1], as the guide says [2].

## Alternatives

A registry per collection; more files, no gain.

## Recommendation

Keep the tiers module.

## Open gaps

None.

## References

| # | Reference |
| --- | --- |
| 1 | `src/eawf/kernel/store/tiers.py` |
| 2 | https://example.org/guide |
"""


def _research() -> SkillCatalogEntry:
    entry = SKILL_CATALOG.entry("research")
    assert entry is not None
    return entry


def _hook(args: str) -> Any:
    event = HookEvent(
        event_type=HookEventType.PRE_TOOL_USE,
        runtime="claude",
        occurred_at=datetime(2026, 10, 30, tzinfo=UTC),
        payloads={
            "claude_code": {
                "session_id": "host-1",
                "tool_name": "Skill",
                "tool_input": {"skill": "research", "args": args},
            }
        },
    )
    return judge_host_skill_invocation(event)


# ---- the page --------------------------------------------------------------


def test_research_page_offers_swift_and_rounds_modes() -> None:
    page = shipped_skill_page(_research())
    check_skill_page(page, _research())
    assert "Swift mode: run one survey pass and ask the operator nothing." in page
    assert "Rounds mode:" in page
    assert "one preference question through the host's structured question tool" in page
    assert "--rounds <1..5>" in page
    assert "--depth <shallow|medium|deep|exhaustive>" in page


def test_research_page_names_the_brief_sections_in_order_and_the_check() -> None:
    page = shipped_skill_page(_research())
    assert ", ".join(BRIEF_SECTIONS) in page
    assert "`eawf research check <brief>`" in page
    assert "`eawf research check`" in page


# ---- the hook ----------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        "store tiers --rounds 3 --depth deep",
        "store tiers --rounds=1 --depth=shallow",
        "store tiers --rounds 5 --depth exhaustive",
        "store tiers",
    ],
)
def test_hook_admits_rounds_and_depth_within_the_grammar(args: str) -> None:
    result = _hook(args)
    assert not result.block, result.output
    assert "admitted /research" in result.output


@pytest.mark.parametrize(
    ("args", "named"),
    [
        ("store tiers --rounds 0", "--rounds '0' is not one of 1..5"),
        ("store tiers --rounds 6", "--rounds '6' is not one of 1..5"),
        ("store tiers --rounds many", "--rounds 'many' is not one of 1..5"),
        ("store tiers --depth abyss", "--depth 'abyss' is not one of shallow|medium|deep"),
        ("store tiers --depth ''", "--depth '' is not one of"),
    ],
)
def test_hook_refuses_a_rounds_or_depth_value_outside_the_grammar(args: str, named: str) -> None:
    result = _hook(args)
    assert result.block
    assert "invalid_value" in result.output
    assert named in result.output


@pytest.mark.parametrize("value", [1, 5, "5"])
def test_headless_lane_admits_a_rounds_number_in_range(value: object) -> None:
    check_invocation(_research(), {"topic": ["x"], "rounds": value}, lane="agent")


@pytest.mark.parametrize("value", [0, 6, True, 2.5, "-1"])
def test_headless_lane_refuses_a_rounds_value_out_of_range_or_type(value: object) -> None:
    with pytest.raises(InvocationRefusedError) as raised:
        check_invocation(_research(), {"topic": ["x"], "rounds": value}, lane="agent")
    assert raised.value.code == "invalid_value"


def test_a_repeated_choice_option_checks_every_value() -> None:
    entry = SKILL_CATALOG.entry("why")
    assert entry is not None
    check_invocation(entry, {"include": ["decisions", "events"]}, lane="operator")
    with pytest.raises(InvocationRefusedError, match="'nope' is not one of"):
        check_invocation(entry, {"include": ["decisions", "nope"]}, lane="operator")


def test_a_path_or_stdin_placeholder_is_not_a_closed_set() -> None:
    entry = SKILL_CATALOG.entry("milestone")
    assert entry is not None
    option = next(o for o in argument_schema(entry).options if o.flag == "--from-spec")
    assert option.choices == ()
    assert option.bounds is None
    assert option.admits("plans/next.yaml")


# ---- the brief check ---------------------------------------------------------


def test_check_brief_passes_the_complete_brief() -> None:
    verdict = check_research_brief(_BRIEF)
    assert verdict.ok
    assert verdict.errors == ()


@pytest.mark.parametrize("text", ["", "   \n\n"])
def test_check_brief_refuses_an_empty_file(text: str) -> None:
    assert check_research_brief(text).errors == ("brief is empty",)


def test_check_brief_refuses_headings_out_of_order() -> None:
    text = _BRIEF.replace("## Alternatives", "## Swap").replace(
        "## Recommendation", "## Alternatives"
    )
    text = text.replace("## Swap", "## Recommendation")
    errors = check_research_brief(text).errors
    assert len(errors) == 1
    assert "are not in the order" in errors[0]


def test_check_brief_refuses_an_absolute_path_in_references() -> None:
    text = _BRIEF.replace("`src/eawf/kernel/store/tiers.py`", "/opt/build/tiers.py")
    errors = check_research_brief(text).errors
    assert errors == (
        "References row 1 ref '/opt/build/tiers.py' is not repo-relative, a URL or an Eawf URN",
    )


def test_check_brief_refuses_a_local_host_url() -> None:
    text = _BRIEF.replace("https://example.org/guide", "http://localhost:8000/guide")
    assert "References row 2" in check_research_brief(text).errors[0]


def test_check_brief_refuses_a_missing_title_and_an_extra_section() -> None:
    text = _BRIEF.removeprefix("# Which store holds the tiers\n\n") + "\n## Notes\n\nmore\n"
    errors = check_research_brief(text).errors
    assert "brief does not start with an H1 title" in errors
    assert "unexpected section: Notes" in errors


def test_check_brief_refuses_a_repeated_section() -> None:
    text = _BRIEF.replace("## Open gaps\n\nNone.\n", "## Open gaps\n\nNone.\n\n## Findings\n\nx\n")
    assert any("a section repeats" in error for error in check_research_brief(text).errors)


def test_check_brief_refuses_an_empty_section() -> None:
    text = _BRIEF.replace("## Open gaps\n\nNone.\n", "## Open gaps\n\n")
    assert check_research_brief(text).errors == ("section Open gaps is empty",)


def test_check_brief_refuses_references_that_are_not_a_table() -> None:
    text = _BRIEF[: _BRIEF.index("| # |")] + "1. src/eawf/kernel/store/tiers.py\n"
    errors = check_research_brief(text).errors
    assert errors == ("References is not a table with a header, a separator and at least one row",)


def test_check_brief_refuses_a_header_without_the_number_column() -> None:
    text = _BRIEF.replace("| # | Reference |", "| N | Reference |")
    assert check_research_brief(text).errors == (
        "References table header must start with the columns # and Reference",
    )


def test_check_brief_refuses_a_bad_separator_row() -> None:
    text = _BRIEF.replace("| --- | --- |", "| --- |")
    assert check_research_brief(text).errors == (
        "References table separator row does not match its header",
    )


def test_check_brief_refuses_a_row_off_by_one_cell_and_a_non_number() -> None:
    text = _BRIEF.replace(
        "| 2 | https://example.org/guide |", "| 2 | https://example.org/guide | x |"
    )
    text = text.replace("| 1 | `src", "| one | `src")
    assert check_research_brief(text).errors == (
        "References row 1 number 'one' is not a number",
        "References row 2 has 3 cells, header has 2",
    )


def test_check_brief_refuses_an_uncited_row_and_a_gap_in_numbering() -> None:
    uncited = _BRIEF.replace(", as the guide says [2]", "")
    assert check_research_brief(uncited).errors == (
        "References: citation rows unused by prose: [2]",
    )
    gap = _BRIEF.replace("| 2 | https", "| 3 | https").replace("[2]", "[3]")
    assert "dense" in check_research_brief(gap).errors[0]
