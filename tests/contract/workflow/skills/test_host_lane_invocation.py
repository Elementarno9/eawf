"""A host-lane skill invocation is judged before its Run starts.

SURF-112: the text an operator submits after ``/<skill>``, or the arguments a
model hands the host's skill tool, is read against the skill's one strict
argument schema, and an unknown or action-incompatible argument is refused by
the host hook before the model ever sees the skill.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.host_lane import HOST_SKILL_HOOK, judge_host_skill_invocation
from eawf.surfaces.cli.app import app
from eawf.workflow.skills.arguments import parse_host_arguments
from eawf.workflow.skills.catalog import SKILL_CATALOG

runner = CliRunner()


def _entry(skill_id: str) -> Any:
    entry = SKILL_CATALOG.entry(skill_id)
    assert entry is not None
    return entry


def _prompt_hook(tmp_path: Path, prompt: str) -> Any:
    (tmp_path / ".ea").mkdir(exist_ok=True)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "host-1", "prompt": prompt}
    return runner.invoke(
        app,
        ["-w", str(tmp_path), "hook", "run", "user_prompt_submit", "--runtime", "claude"],
        input=json.dumps(payload),
    )


def _skill_tool_event(skill: str, args: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.PRE_TOOL_USE,
        runtime="claude",
        occurred_at=datetime(2026, 9, 30, tzinfo=UTC),
        payloads={
            "claude_code": {
                "session_id": "host-1",
                "tool_name": "Skill",
                "tool_input": {"skill": skill, "args": args},
            }
        },
    )


# ---- SURF-112: the host text reads through the one schema --------------------


def test_surf_112_host_text_becomes_the_argument_mapping_the_schema_judges() -> None:
    args = parse_host_arguments(_entry("accept"), 'show MLS-0001 --reason "two words"')
    assert args == {"action": "show", "subject_ref": ["MLS-0001"], "reason": "two words"}


def test_surf_112_an_optional_action_leaves_a_bare_subject_as_the_subject() -> None:
    assert parse_host_arguments(_entry("attend"), "ITEM-1 --kind question") == {
        "subject_ref": ["ITEM-1"],
        "kind": "question",
    }


def test_surf_112_empty_text_is_no_arguments() -> None:
    assert parse_host_arguments(_entry("research"), "") == {}


def test_surf_112_text_that_does_not_split_is_read_word_by_word() -> None:
    assert parse_host_arguments(_entry("research"), '"unterminated --depth deep') == {
        "topic": ['"unterminated'],
        "depth": "deep",
    }


@pytest.mark.parametrize(
    ("prompt", "code"),
    [
        ("/research caching --bogus x", "unknown_argument"),
        ("/eawf:accept bogus MLS-0001", "action_undeclared"),
        ('/accept show MLS-0001 --reason "why"', "action_incompatible"),
    ],
)
def test_surf_112_the_prompt_hook_blocks_a_refused_invocation_before_a_run(
    tmp_path: Path, prompt: str, code: str
) -> None:
    result = _prompt_hook(tmp_path, prompt)
    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["decision"] == "block"
    assert code in document["reason"]


@pytest.mark.parametrize("prompt", ["/research caching layer --sources repo", "/clear", "hello"])
def test_surf_112_an_admitted_or_foreign_prompt_passes_untouched(
    tmp_path: Path, prompt: str
) -> None:
    result = _prompt_hook(tmp_path, prompt)
    assert result.exit_code == 0
    assert result.stdout == ""


def test_surf_112_a_model_skill_call_is_judged_on_the_agent_lane() -> None:
    refused = judge_host_skill_invocation(_skill_tool_event("eawf:research", "x --bogus y"))
    assert refused.name == HOST_SKILL_HOOK
    assert refused.block
    assert "unknown_argument" in refused.output
    admitted = judge_host_skill_invocation(_skill_tool_event("research", "caching"))
    assert not admitted.block
    assert admitted.output == f"{HOST_SKILL_HOOK} admitted /research lane=agent"


def test_surf_112_a_foreign_skill_call_is_left_to_the_host() -> None:
    result = judge_host_skill_invocation(_skill_tool_event("someone-elses-skill", "--any"))
    assert not result.block
    assert "skipped" in result.output
