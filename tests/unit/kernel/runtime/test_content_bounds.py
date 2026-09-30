"""UI-072: transcript content is bounded and scrubbed before anything stores it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.content import (
    CONTENT_CHAR_LIMIT,
    CONTENT_LINE_CHARS,
    CONTENT_LINE_LIMIT,
    WITHHELD_LINE,
    ResolvedContent,
    StoredContent,
    bound_content,
)
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.host_calls import observe_host_tool

RUN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"


def test_empty_text_keeps_no_line() -> None:
    assert bound_content("") == bound_content("  \n")
    assert bound_content("").lines == ()
    assert bound_content("").total_lines == 0


def test_a_single_line_is_kept_whole() -> None:
    bounded = bound_content("built 3 targets\n")
    assert bounded.lines == ("built 3 targets",)
    assert (bounded.total_lines, bounded.withheld_lines) == (1, 0)


def test_lines_past_the_limit_are_counted_not_kept() -> None:
    text = "\n".join(f"line {index}" for index in range(CONTENT_LINE_LIMIT + 1))
    bounded = bound_content(text)
    assert len(bounded.lines) == CONTENT_LINE_LIMIT
    assert bounded.total_lines == CONTENT_LINE_LIMIT + 1


def test_a_long_line_is_cut_and_the_character_budget_stops_the_rest() -> None:
    bounded = bound_content("\n".join("x" * (CONTENT_LINE_CHARS + 7) for _ in range(40)))
    assert {len(line) for line in bounded.lines} == {CONTENT_LINE_CHARS}
    assert sum(map(len, bounded.lines)) <= CONTENT_CHAR_LIMIT
    assert bounded.total_lines == 40


def test_a_line_carrying_a_home_path_is_withheld_whole() -> None:
    bounded = bound_content("ok\nwrote /Users/jdoe/project/keys\ndone")  # pragma: allowlist secret
    assert bounded.lines == ("ok", WITHHELD_LINE, "done")
    assert bounded.withheld_lines == 1
    assert "jdoe" not in "".join(bounded.lines)


def test_the_same_lines_name_the_same_digest() -> None:
    assert bound_content("a\nb").digest == bound_content("a\nb\n").digest
    assert bound_content("a\nb").digest != bound_content("a\nc").digest


def _stored(**overrides: Any) -> StoredContent:
    fields: dict[str, Any] = {
        "artifact_ref": "artifact://content/run-00000010/abc",
        "run_ref": RUN,
        "lines": ("one",),
        "total_lines": 1,
        "withheld_lines": 0,
        "recorded_at": datetime(2026, 9, 30, tzinfo=UTC),
    }
    return StoredContent.model_validate({**fields, **overrides})


def test_stored_content_refuses_counts_below_its_kept_lines() -> None:
    assert _stored().total_lines == 1
    with pytest.raises(ValidationError, match="counts do not cover"):
        _stored(total_lines=0)
    with pytest.raises(ValidationError, match="counts do not cover"):
        _stored(withheld_lines=2)


def test_stored_content_refuses_a_host_path_reference_and_extra_keys() -> None:
    with pytest.raises(ValidationError):
        _stored(artifact_ref="/tmp/output.txt")
    with pytest.raises(ValidationError):
        _stored(raw="leaked")


def test_resolved_content_states_what_it_did_not_keep() -> None:
    held = ResolvedContent(
        ref="call-0000000000000001", lines=("a",), total_lines=3, withheld_lines=0
    )
    assert held.unkept == 2


def _event(event_type: HookEventType, runtime: str, payload: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        scope_id="",
        command="",
        args={},
        runtime=runtime,
        occurred_at=datetime(2026, 9, 30, tzinfo=UTC),
        payloads={event_type.value: payload},
    )


def test_a_runtime_with_no_host_tools_is_skipped_without_a_daemon() -> None:
    result = observe_host_tool(_event(HookEventType.PRE_TOOL_USE, "opencode", {}))
    assert "skipped" in result.output and not result.block


def test_a_payload_naming_no_tool_is_skipped_without_a_daemon() -> None:
    result = observe_host_tool(
        _event(HookEventType.POST_TOOL_USE, "claude", {"session_id": "s-1"}),
        daemon_client_factory=lambda: pytest.fail("no daemon is asked"),
    )
    assert "missing session_id or tool_name" in result.output and not result.block


def test_a_daemon_error_never_blocks_the_host() -> None:
    def refuse() -> Any:
        raise ConnectionError("no daemon")

    result = observe_host_tool(
        _event(
            HookEventType.POST_TOOL_USE_FAILURE,
            "claude",
            {"session_id": "s-1", "tool_name": "Bash", "tool_use_id": "t-1", "error": "boom"},
        ),
        daemon_client_factory=refuse,
    )
    assert "no daemon" in result.output and not result.block
