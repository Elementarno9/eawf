"""Unit tests for :class:`HookEvent` and :class:`HookEventType`.

Phase 4 W04 freezes the v1 :class:`HookEventType` set and the
:class:`HookEvent` Pydantic shape (``extra="forbid"``). These tests
pin:

- the required-field set (``event_type``, ``occurred_at``);
- the frozen :class:`HookEventType` enumeration (15 entries — the v1
  list per ``docs/hook-events.md``);
- the ``runtime`` Literal (``claude``/``codex``/``opencode``/``generic``);
- the ``extra="forbid"`` rejection on unknown keys; and
- a JSON round-trip per event type so a future serializer change
  cannot regress one variant silently.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.runtime.hooks.event import HookEvent, HookEventType


def _base_payload(**overrides: object) -> dict[str, object]:
    """Helper mirroring the pattern from ``tests/unit/test_envelope.py:14``."""
    defaults: dict[str, object] = {
        "event_type": HookEventType.PRE_COMMIT,
        "scope_id": "P04-I01-W04",
        "command": "eawf wave close",
        "args": {"verdict": "shipped"},
        "runtime": "generic",
        "occurred_at": datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC),
        "payloads": {},
    }
    defaults.update(overrides)
    return defaults


def test_hook_event_v1_event_type_set_is_frozen() -> None:
    """The v1 :class:`HookEventType` enumeration carries exactly 15 entries."""
    expected: set[str] = {
        "pre_commit",
        "post_commit",
        "pre_push",
        "post_push",
        "pre_audit",
        "post_audit",
        "session_start",
        "session_end",
        "wave_open",
        "wave_close",
        "iter_open",
        "iter_close",
        "phase_open",
        "phase_close",
        "agent_end",
    }
    assert {member.value for member in HookEventType} == expected


@pytest.mark.parametrize("member", list(HookEventType))
def test_hook_event_round_trips_per_event_type(member: HookEventType) -> None:
    """Every :class:`HookEventType` round-trips through model_dump_json."""
    event = HookEvent(**_base_payload(event_type=member))  # type: ignore[arg-type]
    raw = event.model_dump_json()
    parsed = HookEvent.model_validate_json(raw)
    assert parsed == event
    assert parsed.event_type == member


def test_hook_event_rejects_unknown_event_type_value() -> None:
    """Arbitrary strings for ``event_type`` are rejected by the Literal."""
    with pytest.raises(ValidationError, match="event_type"):
        HookEvent(**_base_payload(event_type="not_a_real_event"))  # type: ignore[arg-type]


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode", "generic"])
def test_hook_event_accepts_known_runtime_literals(runtime: str) -> None:
    """``runtime`` accepts each declared adapter literal."""
    event = HookEvent(**_base_payload(runtime=runtime))  # type: ignore[arg-type]
    assert event.runtime == runtime


def test_hook_event_rejects_unknown_runtime_literal() -> None:
    """``runtime`` rejects unknown adapter literals."""
    with pytest.raises(ValidationError, match="runtime"):
        HookEvent(**_base_payload(runtime="zsh"))  # type: ignore[arg-type]


def test_hook_event_rejects_extra_field() -> None:
    """``extra='forbid'`` blocks unknown keys."""
    data = _base_payload()
    data["unexpected"] = "oops"
    with pytest.raises(ValidationError, match="Extra inputs"):
        HookEvent.model_validate(data)


def test_hook_event_rejects_naive_datetime() -> None:
    """``occurred_at`` must be timezone-aware."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        HookEvent(
            **_base_payload(occurred_at=datetime(2026, 5, 9, 12, 0, 0)),  # type: ignore[arg-type]
        )


def test_hook_event_payloads_default_empty_dict() -> None:
    """``payloads`` defaults to an empty mapping."""
    data = dict(_base_payload())
    del data["payloads"]
    event = HookEvent.model_validate(data)
    assert event.payloads == {}


def test_hook_event_args_default_empty_dict() -> None:
    """``args`` defaults to an empty mapping when omitted."""
    data = dict(_base_payload())
    del data["args"]
    event = HookEvent.model_validate(data)
    assert event.args == {}


def test_hook_event_scope_and_command_default_empty_string() -> None:
    """``scope_id`` and ``command`` default to ``""`` (session-level events)."""
    data = dict(_base_payload())
    del data["scope_id"]
    del data["command"]
    event = HookEvent.model_validate(data)
    assert event.scope_id == ""
    assert event.command == ""


def test_hook_event_payloads_round_trip_nested_extension() -> None:
    """Nested payload extensions survive a JSON round-trip verbatim."""
    nested: dict[str, dict[str, Any]] = {
        "pre_commit": {"files_changed": ["a.py", "b.py"], "branch": "feature/x"}
    }
    event = HookEvent(**_base_payload(payloads=nested))  # type: ignore[arg-type]
    parsed = HookEvent.model_validate_json(event.model_dump_json())
    assert parsed.payloads == nested
