"""Tests: live token accrual folds dispatch_cost into Wave.tokens_consumed.

Exercises :func:`eawf.runtime.daemon.dispatch_runner.accrue_tokens_consumed` (and
the :func:`eawf.runtime.daemon.dispatch_runner.run_dispatch` wiring that drives it)
against a real ``state.json`` on a tmp filesystem.

The load-bearing assertion is the wave's success criterion: a
``dispatch_cost`` event increments the target wave's ``tokens_consumed``
live, and a STATE_REVISION is triggered so live burn gauges advance during
execution. The accrual routes through the daemon canonical state writer
(:func:`eawf.kernel.state.writer.atomic_write_json_locked` under the ``state.json``
portalock), so the persisted counter is indistinguishable from one written
by the daemon's ``state.mutate`` path; the daemon-push STATE_REVISION feed
is driven by a ``state_mutated`` envelope published on the subscription bus.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.dispatch_runner import (
    DispatchTokens,
    accrue_tokens_consumed,
    run_dispatch,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_ID = "P27-I04-W01"
_SESSION_ID = "SES-executor"


def _state_payload(*, wave_status: str = "in_progress", tokens_consumed: int = 0) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-05-23T00:00:00Z",
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "Eawf",
            "description": "",
            "domains": ["workflow"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {
            "project_code": "EAWF",
            "track_id": None,
            "phase_id": "P27",
            "iter_id": "P27-I04",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [_SESSION_ID],
        },
        "workspace": None,
        "phases": {
            "P27": {
                "id": "P27",
                "scope_id": "EAWF",
                "title": "Observability",
                "status": "active",
                "iter_ids": ["P27-I04"],
                "outcome_ids": [],
                "opened_at": "2026-05-23T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P27-I04": {
                "id": "P27-I04",
                "phase_id": "P27",
                "title": "TUI richer views",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-23T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P27-I04",
                "title": "Live token accrual into tokens_consumed",
                "status": wave_status,
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/daemon/dispatch_runner.py"],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "M",
                "claim_session_id": _SESSION_ID,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": tokens_consumed,
                "outcome": None,
                "opened_at": "2026-05-23T00:00:00Z",
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {
            _SESSION_ID: {
                "id": _SESSION_ID,
                "role": "executor",
                "runtime": "claude",
                "scope_id": _WAVE_ID,
                "status": "active",
                "claimed_wave_ids": [_WAVE_ID],
                "worktree_ids": [],
                "artifact_ids": [],
                "started_at": "2026-05-23T00:00:00Z",
                "ended_at": None,
                "summary": None,
            }
        },
        "plugins": {},
        "indexes": {},
    }


def _write_state(
    tmp_path: Path,
    *,
    wave_status: str = "in_progress",
    tokens_consumed: int = 0,
) -> Path:
    """Serialise a valid :class:`State` whose wave carries the given fields."""
    from eawf.kernel.state.models import State

    state = State.model_validate(
        _state_payload(wave_status=wave_status, tokens_consumed=tokens_consumed)
    )
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path | None, *, bus: EventBus | None = None) -> MethodContext:
    """Daemon context wired to *state_path* (+ sibling event log) or stateless."""
    event_path = state_path.parent / "store" / "event.jsonl" if state_path is not None else None
    return MethodContext(
        started_at="2026-05-23T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.3.0",
        bus=bus,
        event_path=event_path,
        state_path=state_path,
    )


def _tokens(
    *,
    input_tokens: int = 1200,
    output_tokens: int = 340,
    cache_creation_input_tokens: int = 8000,
    cache_read_input_tokens: int = 64000,
) -> DispatchTokens:
    return DispatchTokens(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )


def _persisted_consumed(state_path: Path) -> int:
    return load_state(state_path).waves[_WAVE_ID].tokens_consumed


# ---- success criterion: live increment + STATE_REVISION --------------------


def test_tokens_consumed_increments_live(tmp_path: Path) -> None:
    """A dispatch_cost accrual increments tokens_consumed and revises state.

    The wave's success criterion: a ``dispatch_cost`` event increments the
    target wave's ``tokens_consumed`` (the sum of every billed token field)
    and a STATE_REVISION is triggered — here observed as the daemon-push
    ``state_mutated`` envelope landing on a live subscriber's queue (the
    daemon-push feed) plus the rewritten ``state.json`` (the mtime-poll
    feed).
    """
    bus = EventBus()
    sub = bus.register(connection_id="burn-gauge")
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path, bus=bus)
    assert _persisted_consumed(state_path) == 0

    tokens = _tokens()
    accrued = accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=tokens)

    # The accrual returns the token-cap interlock outcome (truthy), not a bool.
    assert accrued is not None
    assert accrued.terminated is False
    # tokens_consumed == sum of every billed token field (the burn-gauge unit).
    assert _persisted_consumed(state_path) == tokens.total
    assert tokens.total == 1200 + 340 + 8000 + 64000
    # STATE_REVISION (daemon-push feed): one state_mutated envelope queued.
    assert len(sub.queue) == 1
    revision = sub.queue[0]
    assert revision.kind is StoreKind.EVENT
    assert revision.scope_id == _WAVE_ID
    assert revision.payload["event_kind"] == "state_mutated"
    assert revision.payload["before_state_version"] != revision.payload["after_state_version"]


def test_accrue_tokens_consumed_run_dispatch_accrues_on_dispatch_cost(tmp_path: Path) -> None:
    """``run_dispatch`` folds the dispatch token tally into tokens_consumed.

    End-to-end: the runner emits the ``dispatch_cost`` event and accrues
    the same tally, so the wave's persisted ``tokens_consumed`` reflects
    the dispatch the moment ``run_dispatch`` returns.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path, bus=EventBus())
    tokens = _tokens()

    run_dispatch(
        ctx,
        wave_id=_WAVE_ID,
        primary_runtime="claude",
        fallback_runtime="codex",
        model="claude-opus-4-7",
        pricing_version="2026.05.17",
        primary_error=None,
        tokens=tokens,
        cost_usd=Decimal("0.05"),
    )

    assert _persisted_consumed(state_path) == tokens.total


# ---- boundary: two consecutive accruals accumulate -------------------------


def test_accrue_tokens_consumed_two_events_accumulate(tmp_path: Path) -> None:
    """Two consecutive dispatch_cost accruals accumulate on tokens_consumed."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path, bus=EventBus())

    first = _tokens(
        input_tokens=100, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )
    second = _tokens(
        input_tokens=0, output_tokens=250, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )

    accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=first)
    assert _persisted_consumed(state_path) == 100
    accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=second)
    assert _persisted_consumed(state_path) == 350


def test_accrue_tokens_consumed_adds_to_existing_count(tmp_path: Path) -> None:
    """The accrual adds onto a pre-existing tokens_consumed value."""
    state_path = _write_state(tmp_path, tokens_consumed=500)
    ctx = _ctx(state_path, bus=EventBus())

    accrue_tokens_consumed(
        ctx,
        wave_id=_WAVE_ID,
        tokens=_tokens(
            input_tokens=10,
            output_tokens=20,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )

    assert _persisted_consumed(state_path) == 530


# ---- boundary: zero delta still rewrites state -----------------------------


def test_accrue_tokens_consumed_zero_delta_leaves_counter(tmp_path: Path) -> None:
    """A zero-token dispatch leaves the counter but still revises state.

    A dispatch that billed nothing is a degenerate boundary; the counter
    must not move, but the accrual still rewrites ``state.json`` (the
    mtime-poll STATE_REVISION feed) + publishes a daemon-push revision so
    the gauge re-reads the unchanged burn rather than going stale.
    """
    bus = EventBus()
    sub = bus.register(connection_id="burn-gauge")
    state_path = _write_state(tmp_path, tokens_consumed=42)
    ctx = _ctx(state_path, bus=bus)

    zero = _tokens(
        input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )
    assert zero.total == 0

    accrued = accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=zero)

    assert accrued is not None
    assert accrued.terminated is False
    assert _persisted_consumed(state_path) == 42
    assert len(sub.queue) == 1


# ---- skip path: stateless context ------------------------------------------


def test_accrue_tokens_consumed_skips_without_state_path() -> None:
    """The accrual is a no-op (returns ``None``) when no state is wired."""
    ctx = _ctx(None, bus=EventBus())
    assert accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=_tokens()) is None


def test_accrue_tokens_consumed_busless_context_still_persists(tmp_path: Path) -> None:
    """A bus-less context still persists the increment (mtime-poll feed only)."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path, bus=None)

    accrued = accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=_tokens())

    assert accrued is not None
    assert accrued.terminated is False
    assert _persisted_consumed(state_path) == _tokens().total


# ---- error path: unknown wave + negative delta -----------------------------


def test_accrue_tokens_consumed_unknown_wave_raises(tmp_path: Path) -> None:
    """An accrual for a wave absent from state raises ``KeyError``."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path, bus=EventBus())

    with pytest.raises(KeyError, match="unknown wave"):
        accrue_tokens_consumed(ctx, wave_id="P27-I04-W99", tokens=_tokens())


def test_accrue_tokens_consumed_negative_delta_rejected(tmp_path: Path) -> None:
    """A negative token tally is rejected by the canonical consume path."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path, bus=EventBus())
    negative = _tokens(
        input_tokens=-1, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )
    assert negative.total == -1

    with pytest.raises(ValueError, match="non-negative"):
        accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=negative)
    # state.json untouched on the rejected accrual.
    assert _persisted_consumed(state_path) == 0
