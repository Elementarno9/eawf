"""Tests for daemon ``needs_user.*`` wrappers."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.needs_user import (
    park_needs_user,
    raise_needs_user,
    resolve_needs_user,
)
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import list_open_pauses

pytestmark = pytest.mark.unit

_SCOPE = "urn:eawf:v1:state:QR"
_SESSION = "urn:eawf:v1:session:cli/SES-test"
_QUESTION = UserQuestion(
    question="Apply roadmap?",
    options=[
        UserQuestionOption(label="apply"),
        UserQuestionOption(label="revise"),
    ],
)


def _state_path(tmp_path: Path) -> Path:
    ea = tmp_path / ".ea"
    ea.mkdir()
    path = ea / "state.json"
    path.touch()
    return path


def _ctx(state_path: Path, bus: EventBus | None = None) -> MethodContext:
    return MethodContext(
        started_at="2026-05-27T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=bus,
        state_path=state_path,
        event_path=store_path(state_path, StoreKind.EVENT),
    )


def test_needs_user_raise_records_pause_and_publishes(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    bus = EventBus()
    sub = bus.register(connection_id="c-1")
    ctx = _ctx(state_path, bus)

    async def body() -> dict[str, object]:
        return await raise_needs_user(
            ctx,
            {
                "scope_id": _SCOPE,
                "session": _SESSION,
                "question": _QUESTION.model_dump(mode="json"),
            },
        )

    result = asyncio.run(body())
    assert isinstance(result["pause_urn"], str)
    pauses = list_open_pauses(state_path, scope_id=_SCOPE)
    assert [pause.pause_urn for pause in pauses] == [result["pause_urn"]]
    assert len(sub.queue) == 1
    published = sub.queue[0]
    assert published.scope_id == _SCOPE
    assert published.payload["event_type"] == "needs_user_pause"
    assert ctx.last_event_id == published.id


def test_needs_user_resolve_closes_pause_and_publishes(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    ctx = _ctx(state_path)
    raised = asyncio.run(
        raise_needs_user(
            ctx,
            {
                "scope_id": _SCOPE,
                "session": _SESSION,
                "question": _QUESTION.model_dump(mode="json"),
            },
        )
    )
    bus = EventBus()
    sub = bus.register(connection_id="c-1")
    ctx.bus = bus

    async def body() -> dict[str, object]:
        return await resolve_needs_user(
            ctx,
            {"pause_urn": raised["pause_urn"], "choice": "revise"},
        )

    result = asyncio.run(body())
    assert result["scope_id"] == _SCOPE
    assert list_open_pauses(state_path, scope_id=_SCOPE) == []
    assert sub.queue[-1].payload["event_type"] == "needs_user_resume"


def test_needs_user_park_lists_legacy_pause_shape(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    now = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    pause_urn = "urn:eawf:v1:event:QR/needs-user-legacy"
    payload = EventPayload(
        timestamp=now,
        event_type="needs_user_pause",
        actor="skill",
        command="skill pause",
        args_hash="",
        status="needs_user",
        message=_QUESTION.question,
        extras={
            "pause_urn": pause_urn,
            "scope": _SCOPE,
            "session": _SESSION,
            "user_question": _QUESTION.model_dump_json(),
        },
    )
    append_envelope(
        store_path(state_path, StoreKind.EVENT),
        Envelope(
            id="EV-legacy",
            kind=StoreKind.EVENT,
            scope_id=None,
            created_at=now,
            updated_at=None,
            summary="legacy needs_user pause",
            payload=payload.model_dump(mode="json"),
        ),
    )
    ctx = _ctx(state_path)

    async def body() -> dict[str, object]:
        return await park_needs_user(ctx, {"scope_id": _SCOPE})

    result = asyncio.run(body())
    pauses = result["pauses"]
    assert isinstance(pauses, list)
    assert pauses[0]["pause_urn"] == pause_urn
    assert pauses[0]["scope_id"] == _SCOPE
