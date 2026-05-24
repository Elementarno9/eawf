"""Property test for hook-event idempotence (Phase 4 W04 acceptance §4).

Contract per the design spec: re-dispatching the same :class:`HookEvent`
through the runner + idempotent appender produces a deterministic
output and ``events.jsonl`` appends one row per ``(event_type,
scope_id, occurred_at)``.

The strategy generates :class:`HookEvent` instances that vary across the
14 :class:`HookEventType` values and a bounded set of scope IDs and
timestamps; the property writes the event twice through
:func:`append_event_idempotent` and asserts the file ends up with a
single row.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.runner import (
    HookRunner,
    _event_idempotence_key,
    append_event_idempotent,
)

_SCOPE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"  # pragma: allowlist secret
_scopes = st.text(alphabet=_SCOPE_ALPHABET, min_size=0, max_size=12)
_command_alphabet = "abcdefghijklmnopqrstuvwxyz "  # pragma: allowlist secret
_commands = st.text(alphabet=_command_alphabet, min_size=0, max_size=20)
_runtimes = st.sampled_from(["claude", "opencode", "generic"])
_event_types = st.sampled_from(list(HookEventType))
_occurred_ats = st.integers(min_value=0, max_value=86_400).map(
    lambda s: datetime(2026, 5, 9, tzinfo=UTC) + timedelta(seconds=s)
)


@st.composite
def hook_events(draw: st.DrawFn) -> HookEvent:
    """Build a typed :class:`HookEvent` with bounded fields."""
    return HookEvent(
        event_type=draw(_event_types),
        scope_id=draw(_scopes),
        command=draw(_commands),
        args={},
        runtime=draw(_runtimes),
        occurred_at=draw(_occurred_ats),
        payloads={},
    )


@given(event=hook_events())
@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_re_dispatching_event_appends_one_row(event: HookEvent) -> None:
    """``append_event_idempotent`` writes once per ``(event_type, scope_id, occurred_at)``."""
    # Each Hypothesis example gets a brand-new temp directory so the
    # property is never observing residue from a previous draw.
    with tempfile.TemporaryDirectory() as raw_dir:
        path = Path(raw_dir) / "events.jsonl"

        first = append_event_idempotent(path, event)
        second = append_event_idempotent(path, event)
        third = append_event_idempotent(path, event)

        assert first is True
        assert second is False
        assert third is False

        rows = [line for line in path.read_text("utf-8").splitlines() if line.strip()]
        assert len(rows) == 1, rows


@given(event=hook_events())
@settings(max_examples=80, deadline=None)
def test_re_dispatching_via_runner_is_deterministic(event: HookEvent) -> None:
    """Two consecutive runner.run_event calls produce the same result list."""
    runner = HookRunner()
    fired: list[str] = []

    def stable_hook(_: HookEvent) -> tuple[bool, str]:
        fired.append("x")
        return False, "ok"

    runner.register(event.event_type, stable_hook, name="stable")
    first = runner.run_event(event)
    second = runner.run_event(event)
    assert len(first) == 1
    assert len(second) == 1
    # Block / output / name / raised are stable across dispatches.
    assert first[0].block == second[0].block
    assert first[0].output == second[0].output
    assert first[0].name == second[0].name
    assert first[0].raised == second[0].raised
    # The hook fires once per dispatch.
    assert fired == ["x", "x"]


def test_idempotence_key_uses_documented_triple() -> None:
    """Sanity check on the v1 idempotence key shape (acceptance §4)."""
    event = HookEvent(
        event_type=HookEventType.PRE_COMMIT,
        scope_id="P04-I01-W04",
        command="eawf wave close",
        args={},
        runtime="generic",
        occurred_at=datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC),
        payloads={},
    )
    assert _event_idempotence_key(event) == (
        "pre_commit",
        "P04-I01-W04",
        "2026-05-09T12:00:00+00:00",
    )


def test_idempotent_appender_keeps_distinct_triples_separate(tmp_path: Path) -> None:
    """Two events with different triples each land their own row."""
    base = datetime(2026, 5, 9, tzinfo=UTC)
    e1 = HookEvent(
        event_type=HookEventType.PRE_COMMIT,
        scope_id="A",
        command="",
        args={},
        runtime="generic",
        occurred_at=base,
        payloads={},
    )
    e2 = HookEvent(
        event_type=HookEventType.PRE_COMMIT,
        scope_id="B",
        command="",
        args={},
        runtime="generic",
        occurred_at=base,
        payloads={},
    )
    path = tmp_path / "events.jsonl"
    assert append_event_idempotent(path, e1) is True
    assert append_event_idempotent(path, e2) is True
    assert append_event_idempotent(path, e1) is False
    rows = [line for line in path.read_text("utf-8").splitlines() if line.strip()]
    assert len(rows) == 2
