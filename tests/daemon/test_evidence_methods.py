"""Tests for the ``evidence.append`` JSON-RPC handler (P28-I01-W04)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import orjson
import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord, mint_evidence_id
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.evidence import append

pytestmark = pytest.mark.unit


def _build_ctx(*, state_path: Path | None) -> MethodContext:
    return MethodContext(
        started_at="2026-05-26T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        state_path=state_path,
    )


def _record_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": mint_evidence_id(),
        "scope_id": "P28-I01-W04",
        "produced_by": "tool",
        "evidence_kind": "deterministic",
        "status": "pass",
        "summary": "pytest gate green",
        "refs": [],
        "created_at": "2026-05-26T12:00:00+00:00",
    }
    base.update(overrides)
    return base


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def test_evidence_append_writes_one_row(tmp_path: Path) -> None:
    """RPC validates the record, appends to ``evidence.jsonl``, returns id + ts."""
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    ctx = _build_ctx(state_path=state_path)
    record = _record_dict()

    async def body() -> None:
        result = await append(ctx, {"record": record})
        assert result["id"] == record["id"]
        assert result["appended_at"]  # ISO-8601 string

    _run(body)

    evidence_path = store_path(state_path, StoreKind.EVIDENCE)
    assert evidence_path.exists()
    lines = evidence_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    env = Envelope.model_validate(orjson.loads(lines[0]))
    assert env.kind is StoreKind.EVIDENCE
    assert env.id == record["id"]
    assert env.scope_id == record["scope_id"]
    loaded = EvidenceRecord.model_validate(env.payload)
    assert loaded.summary == "pytest gate green"
    assert loaded.evidence_kind == "deterministic"


def test_evidence_append_round_trip_re_validates(tmp_path: Path) -> None:
    """A row written by the RPC reloads losslessly through ``EvidenceRecord``."""
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    ctx = _build_ctx(state_path=state_path)
    record = _record_dict(
        evidence_kind="attested",
        produced_by="human",
        status="waived",
        summary="operator waiver",
        refs=["DEC-123"],
    )

    async def body() -> None:
        await append(ctx, {"record": record})

    _run(body)

    evidence_path = store_path(state_path, StoreKind.EVIDENCE)
    env = Envelope.model_validate(orjson.loads(evidence_path.read_text().splitlines()[0]))
    loaded = EvidenceRecord.model_validate(env.payload)
    assert loaded.evidence_kind == "attested"
    assert loaded.produced_by == "human"
    assert loaded.status == "waived"
    assert loaded.refs == ["DEC-123"]


def test_evidence_append_appends_multiple_rows(tmp_path: Path) -> None:
    """Two appends produce two distinct JSONL rows."""
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        await append(ctx, {"record": _record_dict()})
        await append(ctx, {"record": _record_dict(status="fail", summary="ruff failed")})

    _run(body)

    evidence_path = store_path(state_path, StoreKind.EVIDENCE)
    lines = evidence_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    statuses = [orjson.loads(line)["payload"]["status"] for line in lines]
    assert statuses == ["pass", "fail"]


def test_evidence_append_rejects_invalid_record(tmp_path: Path) -> None:
    """A semantically invalid record raises ``ValueError`` (mapped to -32602)."""
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    ctx = _build_ctx(state_path=state_path)
    record = _record_dict(status="maybe")  # not in the closed Literal

    async def body() -> None:
        with pytest.raises(ValueError):
            await append(ctx, {"record": record})

    _run(body)


def test_evidence_append_rejects_extra_field(tmp_path: Path) -> None:
    """``extra='forbid'`` on the AppendParams blocks stray keys."""
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValueError):
            await append(ctx, {"record": _record_dict(), "stowaway": True})

    _run(body)


def test_evidence_append_requires_state_path() -> None:
    """Without ``ctx.state_path`` the handler raises ``RuntimeError``."""
    ctx = _build_ctx(state_path=None)

    async def body() -> None:
        with pytest.raises(RuntimeError, match="state_path"):
            await append(ctx, {"record": _record_dict()})

    _run(body)


def test_evidence_append_id_in_envelope_matches_record(tmp_path: Path) -> None:
    """The envelope id mirrors the record id (single addressable row)."""
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    ctx = _build_ctx(state_path=state_path)
    ev_id = mint_evidence_id()
    record = _record_dict(id=ev_id)

    async def body() -> None:
        result = await append(ctx, {"record": record})
        assert result["id"] == ev_id

    _run(body)

    evidence_path = store_path(state_path, StoreKind.EVIDENCE)
    env = Envelope.model_validate(orjson.loads(evidence_path.read_text().splitlines()[0]))
    assert env.id == ev_id


def test_evidence_append_method_registered() -> None:
    """The ``evidence.append`` method is on the registry after import."""
    # Importing the daemon server pulls in the methods package; the
    # explicit import below ensures the side-effecting registration ran.
    import eawf.runtime.daemon.methods.evidence  # noqa: F401
    from eawf.runtime.daemon.methods import registered_methods

    assert "evidence.append" in registered_methods()
