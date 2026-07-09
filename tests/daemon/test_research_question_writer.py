"""Tests for the OpenQuestion + Claim daemon writers (P30-I18-W02).

Covers the two halves of the campaign ledger the research board was never
populating:

* :func:`~eawf.runtime.daemon.methods.research.add_question` writes an
  :class:`~eawf.kernel.state.models.OpenQuestion` row through the canonical
  state writer (the same path the TUI ``o`` key drives end to end), with the
  state write + event append landing.
* :func:`~eawf.runtime.daemon.methods.research.reconcile_round_claims` folds a
  round's parsed findings into ``OPEN`` :class:`~eawf.kernel.state.models.Claim`
  rows carrying the researcher body's evidence refs.

The handler is driven through the module-level coroutine against an on-disk
state fixture, matching the in-process harness in
:mod:`tests.daemon.test_track_methods`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

from eawf import __version__
from eawf.kernel.spec.campaign_driver import RoundFindings
from eawf.kernel.state.enums import ClaimStatus, OpenQuestionStatus, StoreKind
from eawf.kernel.store.kinds.agent_report import ResearcherReportBody
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.research import (
    ResolveQuestionParams,
    _apply_resolve_question,
    add_question,
    reconcile_round_claims,
    resolve_question,
)
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _build_state_payload() -> dict[str, object]:
    """Minimal valid State payload with a project + no questions yet."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _build_ctx(tmp_path: Path) -> tuple[MethodContext, Path]:
    state_path = tmp_path / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(
        orjson.dumps(_build_state_payload(), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    )
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    ctx = MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )
    return ctx, state_path


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


# --------------------------------------------------------------------------
# add_question -- writes an OpenQuestion through the canonical writer
# --------------------------------------------------------------------------


def test_add_question_writes_open_row_to_state(tmp_path: Path) -> None:
    """A title-only add (the TUI o-key shape) lands one OPEN question row."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        result: dict[str, Any] = await add_question(
            ctx, {"title": "which curve model fits the short tenor"}
        )
        assert result["status"] == "open"
        assert result["scope_id"] == "ABC"
        assert result["question_id"].startswith("OQ-")
        state = load_state(state_path)
        assert state.open_questions is not None
        rows = list(state.open_questions.values())
        assert len(rows) == 1
        assert rows[0].title == "which curve model fits the short tenor"
        assert rows[0].status is OpenQuestionStatus.OPEN
        assert rows[0].scope_id == "ABC"
        assert rows[0].blocking is False

    _run(body)


def test_add_question_blocking_lands_blocked_status(tmp_path: Path) -> None:
    """A blocking add lands a BLOCKED question (the interrupt status)."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        result = await add_question(
            ctx, {"title": "is the venue feed authoritative", "blocking": True, "urgency": "urgent"}
        )
        assert result["status"] == "blocked"
        state = load_state(state_path)
        assert state.open_questions is not None
        row = next(iter(state.open_questions.values()))
        assert row.status is OpenQuestionStatus.BLOCKED
        assert row.blocking is True

    _run(body)


def test_add_question_uses_caller_id(tmp_path: Path) -> None:
    """A caller-supplied question id is used verbatim."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        await add_question(ctx, {"title": "pin the id", "question_id": "OQ-pinned"})
        state = load_state(state_path)
        assert state.open_questions is not None
        assert "OQ-pinned" in state.open_questions

    _run(body)


def test_add_question_emits_event(tmp_path: Path) -> None:
    """The write appends a research.add_question event row."""
    ctx, state_path = _build_ctx(tmp_path)
    event_path = store_path(state_path, StoreKind.EVENT)

    async def body() -> None:
        await add_question(ctx, {"title": "logs an event"})
        lines = [
            line for line in event_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert any("research.add_question" in line for line in lines)

    _run(body)


def test_add_question_rejects_extra_param(tmp_path: Path) -> None:
    """An unknown param is rejected by extra='forbid'."""
    ctx, _state_path = _build_ctx(tmp_path)

    async def body() -> None:
        with pytest.raises(ValidationError):
            await add_question(ctx, {"title": "ok", "rogue": True})

    _run(body)


def test_add_question_rejects_over_cap_title(tmp_path: Path) -> None:
    """An over-72-char title is rejected fail-fast at the params boundary."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        with pytest.raises(ValidationError):
            await add_question(ctx, {"title": "x" * 80})

    _run(body)
    # The rejected params never reached the writer, so no question row landed.
    state = load_state(state_path)
    assert not (state.open_questions or {})


# --------------------------------------------------------------------------
# resolve_question -- flip a BLOCKED question terminal + clear blocking
# --------------------------------------------------------------------------


def test_resolve_question_marks_answered_and_clears_blocking(tmp_path: Path) -> None:
    """Resolving a BLOCKED blocking question lands ANSWERED with blocking cleared.

    Clearing ``blocking`` is the load-bearing bit: the RISKS band +
    ``BLOCKED_AWAIT_USER`` run phase count the ``blocking`` bool, not the status,
    so a resolve that left it set would never resume the halted campaign.
    """
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        added = await add_question(
            ctx, {"title": "is the venue feed authoritative", "blocking": True}
        )
        qid = added["question_id"]
        result = await resolve_question(ctx, {"question_id": qid})
        assert result["status"] == "answered"
        assert result["question_id"] == qid
        assert result["scope_id"] == "ABC"
        state = load_state(state_path)
        assert state.open_questions is not None
        row = state.open_questions[qid]
        assert row.status is OpenQuestionStatus.ANSWERED
        assert row.blocking is False
        assert row.resolved_at is not None
        # An operator resolve links no answering claim.
        assert row.answered_by_claim_id is None

    _run(body)


def test_resolve_question_drop_marks_dropped_and_clears_blocking(tmp_path: Path) -> None:
    """Resolving with ``drop`` lands DROPPED and still clears the blocking bit."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        added = await add_question(ctx, {"title": "is this in scope", "blocking": True})
        qid = added["question_id"]
        result = await resolve_question(ctx, {"question_id": qid, "drop": True})
        assert result["status"] == "dropped"
        state = load_state(state_path)
        assert state.open_questions is not None
        row = state.open_questions[qid]
        assert row.status is OpenQuestionStatus.DROPPED
        assert row.blocking is False

    _run(body)


def test_resolve_question_unknown_id_raises(tmp_path: Path) -> None:
    """A resolve against an absent question id fails fast with a clear ValueError."""
    _ctx, state_path = _build_ctx(tmp_path)
    state = load_state(state_path)
    with pytest.raises(ValueError, match="unknown question"):
        _apply_resolve_question(state, ResolveQuestionParams(question_id="OQ-missing"))


def test_resolve_question_rejects_extra_param(tmp_path: Path) -> None:
    """An unknown param is rejected by extra='forbid' at the params boundary."""
    ctx, _state_path = _build_ctx(tmp_path)

    async def body() -> None:
        with pytest.raises(ValidationError):
            await resolve_question(ctx, {"question_id": "OQ-x", "rogue": True})

    _run(body)


# --------------------------------------------------------------------------
# reconcile_round_claims -- fold findings into OPEN Claim rows
# --------------------------------------------------------------------------


def _researcher_body(domain: str, *, findings: list[str]) -> ResearcherReportBody:
    return ResearcherReportBody.model_validate(
        {
            "role": "researcher",
            "verdict": "pass",
            "confidence": "medium",
            "summary": f"surveyed {domain}",
            "question": f"what does {domain} reveal",
            "findings": findings,
            "recommendation": f"pursue {domain}",
            "evidence_refs": [{"kind": "store_record", "ref": f"src/{domain}.py:1"}],
        }
    )


def test_reconcile_round_claims_writes_open_claims(tmp_path: Path) -> None:
    """Each finding line becomes one OPEN claim carrying the body's evidence."""
    _ctx, state_path = _build_ctx(tmp_path)
    state = load_state(state_path)
    findings = RoundFindings(
        round_number=1,
        bodies=(
            _researcher_body("market-structure", findings=["ms claim a", "ms claim b"]),
            _researcher_body("pricing-models", findings=["pm claim"]),
        ),
        domains=("market-structure", "pricing-models"),
    )
    written = reconcile_round_claims(state, findings, scope_id=None, now=_now())
    assert len(written) == 3
    assert state.claims is not None
    assert len(state.claims) == 3
    a = state.claims["CLM-r1-market-structure-0"]
    assert a.title == "ms claim a"
    assert a.status is ClaimStatus.OPEN
    assert a.scope_id == "ABC"
    assert a.evidence_refs == ["src/market-structure.py:1"]


def test_reconcile_round_claims_truncates_over_cap_title(tmp_path: Path) -> None:
    """A finding longer than the 72-char title cap truncates into the title."""
    _ctx, state_path = _build_ctx(tmp_path)
    state = load_state(state_path)
    long_line = "y" * 120
    findings = RoundFindings(
        round_number=2,
        bodies=(_researcher_body("d", findings=[long_line]),),
        domains=("d",),
    )
    written = reconcile_round_claims(state, findings, scope_id="CUSTOM", now=_now())
    claim = state.claims["CLM-r2-d-0"]  # type: ignore[index]
    assert len(claim.title) == 72
    assert claim.title.endswith("...")
    assert claim.description == long_line[:500]
    assert claim.scope_id == "CUSTOM"
    assert written == ["CLM-r2-d-0"]
