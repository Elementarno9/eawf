"""Tests for the round reconcile that folds findings into Claim rows.

:func:`~eawf.runtime.daemon.methods.research.reconcile_round_claims` folds a
round's parsed findings into ``OPEN`` :class:`~eawf.kernel.state.models.Claim`
rows carrying the researcher body's evidence refs. Each test runs it against an
on-disk state fixture.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf import __version__
from eawf.kernel.spec.campaign_driver import RoundFindings
from eawf.kernel.state.enums import (
    ClaimStatus,
    StoreKind,
)
from eawf.kernel.store.kinds.agent_report import ResearcherReportBody
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.research import reconcile_round_claims
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
