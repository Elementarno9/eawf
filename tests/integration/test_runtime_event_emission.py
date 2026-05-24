"""Integration: runtime adapters emit through canonical Event model (D14 / XB07).

These tests assert that the three dispatch-side event kinds
(``runtime_switched``, ``session_continued``, ``session_failover``)
flow through :class:`~eawf.kernel.store.kinds.event.Event` — the single
source of truth from W06 — never through an adapter-private envelope.

The :func:`emit_runtime_event` helper centralises construction; the
integration assertion is that the resulting :class:`Event` validates,
round-trips through JSON, and its ``payload.event_kind`` matches the
closed :data:`~eawf.runtimes.adapter.DispatchEventKind` Literal.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from eawf.kernel.store import Event as EventReExport
from eawf.kernel.store.kinds.event import Event, EventKind, EventPayload
from eawf.runtimes.adapter import (
    DispatchEventKind,
    emit_runtime_event,
)


def _occurred() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


@pytest.mark.integration
def test_runtime_switched_event_emits_canonical_shape() -> None:
    """V5 cross-runtime switch path lands ``runtime_switched`` via canonical Event."""
    ev = emit_runtime_event(
        event_id="e-2026-05-19-0010-runtime_switched",
        scope_id="P25-I01-W10",
        occurred_at=_occurred(),
        event_kind="runtime_switched",
        actor="claude-code",
        command="agent.dispatch",
        args_hash="h1",
        status="ok",
        message="ladder fall-through codex -> claude-code",
        error_class="RUNTIME_RATE_LIMIT",
        extras={"from_runtime": "codex", "to_runtime": "claude-code", "exit_status": 2},
        idempotency_key="11111111-1111-1111-1111-111111111111",
    )

    assert isinstance(ev, Event)
    assert ev.payload.event_kind == "runtime_switched"
    assert ev.payload.error_class == "RUNTIME_RATE_LIMIT"
    assert ev.idempotency_key == "11111111-1111-1111-1111-111111111111"


@pytest.mark.integration
def test_session_continued_event_emits_canonical_shape() -> None:
    """V8 ``--continue`` path lands ``session_continued`` via canonical Event."""
    ev = emit_runtime_event(
        event_id="e-2026-05-19-0011-session_continued",
        scope_id="P25-I01-W10",
        occurred_at=_occurred(),
        event_kind="session_continued",
        actor="claude-code",
        command="agent.dispatch",
        args_hash="h2",
        status="ok",
        message="continued session abc-123",
        extras={"session_id": "abc-123", "attempt": 4},
    )

    assert ev.payload.event_kind == "session_continued"
    # Re-validating through the canonical Event preserves the payload.
    reloaded = Event.model_validate_json(ev.model_dump_json())
    assert reloaded.payload.event_kind == "session_continued"
    assert reloaded.payload.extras["session_id"] == "abc-123"


@pytest.mark.integration
def test_session_failover_event_emits_canonical_shape() -> None:
    """V8 continue-fall-back lands ``session_failover`` via canonical Event."""
    ev = emit_runtime_event(
        event_id="e-2026-05-19-0012-session_failover",
        scope_id="P25-I01-W10",
        occurred_at=_occurred(),
        event_kind="session_failover",
        actor="claude-code",
        command="agent.dispatch",
        args_hash="h3",
        status="ok",
        message="continue failed; fell back to fresh dispatch",
        extras={"prior_session_id": "old-456"},
    )

    assert ev.payload.event_kind == "session_failover"
    assert ev.payload.extras["prior_session_id"] == "old-456"


@pytest.mark.integration
def test_canonical_event_re_export_identity() -> None:
    """D14 / XB07: ``eawf.kernel.store.Event`` re-exports the canonical model.

    Adapters import :func:`emit_runtime_event` which calls into
    :class:`eawf.kernel.store.kinds.event.Event`; the package-root re-export
    MUST be the same object so callers reading either path get a
    single source of truth.
    """
    assert Event is EventReExport


@pytest.mark.integration
def test_dispatch_event_kind_subset_of_event_kind() -> None:
    """The 3 adapter-emitted kinds MUST be a subset of canonical EventKind."""
    from typing import get_args

    dispatch_kinds = set(get_args(DispatchEventKind))
    canonical_kinds = set(get_args(EventKind))
    assert dispatch_kinds <= canonical_kinds


@pytest.mark.integration
def test_emit_runtime_event_payload_is_event_payload() -> None:
    """The constructor returns ``Event`` carrying ``EventPayload`` (XB07)."""
    ev = emit_runtime_event(
        event_id="e-2026-05-19-0013-session_continued",
        scope_id="P25-I01-W10",
        occurred_at=_occurred(),
        event_kind="session_continued",
        actor="claude-code",
        command="agent.dispatch",
        args_hash="h4",
        status="ok",
        message="ok",
    )
    assert isinstance(ev.payload, EventPayload)


@pytest.mark.integration
def test_event_serialises_to_jsonl_compatible_row() -> None:
    """Canonical Event round-trips through JSON for JSONL store ingestion."""
    ev = emit_runtime_event(
        event_id="e-2026-05-19-0014-runtime_switched",
        scope_id="P25-I01-W10",
        occurred_at=_occurred(),
        event_kind="runtime_switched",
        actor="codex",
        command="agent.dispatch",
        args_hash="h5",
        status="ok",
        message="ladder switch",
        error_class="RUNTIME_SERVER_ERROR",
    )
    row = json.loads(ev.model_dump_json())
    assert row["scope_id"] == "P25-I01-W10"
    assert row["payload"]["event_kind"] == "runtime_switched"
    assert row["payload"]["error_class"] == "RUNTIME_SERVER_ERROR"
