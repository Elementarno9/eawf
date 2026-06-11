"""Unit tests for memory.promotion: promote + supersede + promote_to_artifact."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import Confidence, DecisionStatus, MemoryStatus, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.platform.memory.promotion import (
    PromotionError,
    promote_record,
    promote_to_artifact,
    supersede,
)
from eawf.platform.memory.store import add_memory


def _make_state() -> State:
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def _seed_research(path: Path, rec_id: str, summary: str, body: str) -> None:
    env = Envelope(
        id=rec_id,
        kind=StoreKind.RESEARCH,
        scope_id="QR",
        created_at=datetime(2026, 5, 8, tzinfo=UTC),
        summary=summary,
        payload={"topic": summary, "findings": [body], "sources": []},
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(env.model_dump_json())
        fh.write("\n")


def test_promote_record_creates_memory_from_research(tmp_path: Path) -> None:
    state = _make_state()
    research = tmp_path / "research.jsonl"
    memory = tmp_path / "memory.jsonl"
    _seed_research(research, "RES-001", "Initial review", "Findings text body")
    result = promote_record(
        state=state,
        source_store_path=research,
        source_id="RES-001",
        memory_path=memory,
        confidence=Confidence.HIGH,
    )
    assert result.source_store_record_id == "RES-001"
    assert result.record.summary.scope_id == "QR"
    assert result.record.summary.confidence is Confidence.HIGH
    assert result.record.summary.id in (state.memory_index or {})
    body = json.loads(memory.read_text(encoding="utf-8").splitlines()[0])["payload"]["body"]
    assert "Findings text body" in body


def test_promote_record_overrides_scope(tmp_path: Path) -> None:
    state = _make_state()
    research = tmp_path / "research.jsonl"
    memory = tmp_path / "memory.jsonl"
    _seed_research(research, "RES-001", "Brief", "body")
    result = promote_record(
        state=state,
        source_store_path=research,
        source_id="RES-001",
        memory_path=memory,
        scope_id="P01-I01",
    )
    assert result.record.summary.scope_id == "P01-I01"


def test_promote_record_missing_source_raises_promotion_error(tmp_path: Path) -> None:
    state = _make_state()
    research = tmp_path / "research.jsonl"
    research.write_text("", encoding="utf-8")
    memory = tmp_path / "memory.jsonl"
    with pytest.raises(PromotionError, match="not found"):
        promote_record(
            state=state,
            source_store_path=research,
            source_id="MISSING",
            memory_path=memory,
        )


def test_promote_record_missing_file_raises(tmp_path: Path) -> None:
    state = _make_state()
    memory = tmp_path / "memory.jsonl"
    with pytest.raises(PromotionError, match="does not exist"):
        promote_record(
            state=state,
            source_store_path=tmp_path / "absent.jsonl",
            source_id="X",
            memory_path=memory,
        )


def test_supersede_marks_old_status(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    old = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="old", body="old")
    new = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="new", body="new")
    supersede(
        state=state,
        memory_path=memory_path,
        old_id=old.summary.id,
        new_id=new.summary.id,
    )
    assert state.memory_index is not None
    assert state.memory_index[old.summary.id].status == MemoryStatus.SUPERSEDED
    assert state.memory_index[new.summary.id].status == MemoryStatus.ACTIVE


def test_supersede_unknown_old_raises(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    new = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
    with pytest.raises(PromotionError, match=r"not in state\.memory_index"):
        supersede(
            state=state,
            memory_path=memory_path,
            old_id="MEM-MISSING",
            new_id=new.summary.id,
        )


def test_supersede_unknown_new_raises(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    old = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
    with pytest.raises(PromotionError, match=r"not in state\.memory_index"):
        supersede(
            state=state,
            memory_path=memory_path,
            old_id=old.summary.id,
            new_id="MEM-MISSING",
        )


def test_promote_uses_explicit_now(tmp_path: Path) -> None:
    state = _make_state()
    research = tmp_path / "research.jsonl"
    memory = tmp_path / "memory.jsonl"
    _seed_research(research, "RES-X", "summary", "body")
    moment = datetime(2026, 6, 1, 10, tzinfo=UTC)
    result = promote_record(
        state=state,
        source_store_path=research,
        source_id="RES-X",
        memory_path=memory,
        now=moment,
    )
    assert result.record.envelope.created_at == moment


# --- promote_to_artifact: memory -> Decision ---------------------------------


def _seed_active_memory(state: State, memory: Path, *, body: str = "body text") -> str:
    """Add an ACTIVE memory entry and return its ID."""
    rec = add_memory(
        state=state,
        memory_path=memory,
        scope_id="QR",
        title="title",
        body=body,
        confidence=Confidence.HIGH,
    )
    return rec.summary.id


def test_promote_to_artifact_creates_decision_record(tmp_path: Path) -> None:
    state = _make_state()
    memory = tmp_path / "memory.jsonl"
    decisions = tmp_path / "decision.jsonl"
    mid = _seed_active_memory(state, memory)
    moment = datetime(2026, 5, 9, 12, tzinfo=UTC)
    result = promote_to_artifact(
        state=state,
        memory_path=memory,
        decisions_path=decisions,
        source_id=mid,
        now=moment,
    )
    assert result.artifact_id.startswith("DEC-20260509-")
    assert result.decision.status == DecisionStatus.ACTIVE
    assert result.decision.scope_id == "QR"
    assert state.decisions[result.artifact_id] == result.decision  # type: ignore[index]
    # Decision JSONL has the new envelope.
    lines = decisions.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    decoded = json.loads(lines[0])
    assert decoded["id"] == result.artifact_id
    assert decoded["kind"] == "decision"


def test_promote_to_artifact_marks_source_memory_superseded(tmp_path: Path) -> None:
    state = _make_state()
    memory = tmp_path / "memory.jsonl"
    decisions = tmp_path / "decision.jsonl"
    mid = _seed_active_memory(state, memory)
    result = promote_to_artifact(
        state=state,
        memory_path=memory,
        decisions_path=decisions,
        source_id=mid,
    )
    assert state.memory_index is not None
    assert state.memory_index[mid].status == MemoryStatus.SUPERSEDED
    assert state.memory_index[mid].promoted_to_artifact_id == result.artifact_id


def test_promote_to_artifact_writes_promoted_to_artifact_id_in_jsonl(tmp_path: Path) -> None:
    state = _make_state()
    memory = tmp_path / "memory.jsonl"
    decisions = tmp_path / "decision.jsonl"
    mid = _seed_active_memory(state, memory)
    result = promote_to_artifact(
        state=state,
        memory_path=memory,
        decisions_path=decisions,
        source_id=mid,
    )
    # Latest memory envelope carries the link.
    lines = memory.read_text(encoding="utf-8").splitlines()
    last_for_mid = [json.loads(line) for line in lines if json.loads(line)["id"] == mid][-1]
    assert last_for_mid["payload"]["promoted_to_artifact_id"] == result.artifact_id


def test_promote_to_artifact_rejects_unknown_memory_id(tmp_path: Path) -> None:
    state = _make_state()
    memory = tmp_path / "memory.jsonl"
    decisions = tmp_path / "decision.jsonl"
    with pytest.raises(PromotionError, match=r"not in state\.memory_index"):
        promote_to_artifact(
            state=state,
            memory_path=memory,
            decisions_path=decisions,
            source_id="MEM-NOPE",
        )


def test_promote_to_artifact_rejects_unsupported_artifact_kind(tmp_path: Path) -> None:
    state = _make_state()
    memory = tmp_path / "memory.jsonl"
    decisions = tmp_path / "decision.jsonl"
    mid = _seed_active_memory(state, memory)
    with pytest.raises(PromotionError, match="not supported"):
        promote_to_artifact(
            state=state,
            memory_path=memory,
            decisions_path=decisions,
            source_id=mid,
            artifact_kind="hypothesis",
        )


def test_promote_to_artifact_allocates_unique_decision_id(tmp_path: Path) -> None:
    state = _make_state()
    memory = tmp_path / "memory.jsonl"
    decisions = tmp_path / "decision.jsonl"
    mid_a = _seed_active_memory(state, memory)
    mid_b = _seed_active_memory(state, memory)
    moment = datetime(2026, 5, 9, 12, tzinfo=UTC)
    a = promote_to_artifact(
        state=state,
        memory_path=memory,
        decisions_path=decisions,
        source_id=mid_a,
        now=moment,
    )
    b = promote_to_artifact(
        state=state,
        memory_path=memory,
        decisions_path=decisions,
        source_id=mid_b,
        now=moment,
    )
    assert a.artifact_id != b.artifact_id
    assert a.artifact_id.endswith("-01")
    assert b.artifact_id.endswith("-02")


def test_promote_to_artifact_refuses_pruned_entry(tmp_path: Path) -> None:
    state = _make_state()
    memory = tmp_path / "memory.jsonl"
    decisions = tmp_path / "decision.jsonl"
    mid = _seed_active_memory(state, memory)
    assert state.memory_index is not None
    state.memory_index[mid] = state.memory_index[mid].model_copy(
        update={"status": MemoryStatus.PRUNED}
    )
    with pytest.raises(PromotionError, match="PRUNED"):
        promote_to_artifact(
            state=state,
            memory_path=memory,
            decisions_path=decisions,
            source_id=mid,
        )
