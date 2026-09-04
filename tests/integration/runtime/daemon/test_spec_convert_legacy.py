"""Daemon-side tests for the ``spec.convert_legacy`` JSON-RPC handler.

The live legacy-to-typed criteria converter: every
``kind == legacy`` criterion row under the scope is pushed through
:func:`eawf.kernel.spec.common.convert_legacy_criterion` inside the
daemon's canonical state-write transaction, with the EAWF021
measurability lint applied per converted row. Coverage:

* happy path: a closed wave's legacy rows become ``kind == converted``
  with falsifying blocking gates attached and oracle tiers resolved,
  asserted against a freshly-loaded :class:`State`;
* ``--dry-run``: the would-convert report is returned and the state file
  bytes are untouched;
* honest refusal: an unmeasurable row (sub-floor text) stays ``legacy``
  and carries a named EAWF021 reason; a wave with no ``file_scopes``
  refuses every row with a named reason;
* boundary: an unknown wave scope raises; a scope with no legacy rows
  reports zero rows and writes nothing.

The handler is driven directly through the module-level coroutine so the
tests need no live UDS transport.
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

from eawf import __version__
from eawf.kernel.spec.common import CONVERTED_KIND, GRANDFATHERED_KIND
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.spec_convert import convert_legacy

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
_WAVE_ID = "P30-I20-W01"

#: A legacy criterion text long enough to clear the 20-char signal floor
#: and carrying a recognisable EAWF021 verb + locus.
_MEASURABLE_TEXT = "renders the humanized token total in the cost tab under pytest exit zero"

#: A sub-floor legacy text: shorter than 20 chars, so the converted row's
#: measurable_signal falls back to the grandfathered sentinel and EAWF021
#: refuses it.
_UNMEASURABLE_TEXT = "works correctly"


def _legacy_criterion(index: int, text: str) -> dict[str, Any]:
    """A grandfathered legacy criterion row as persisted at migration."""
    signal = text[:300] if len(text) >= 20 else "grandfathered legacy criterion (pre-typed)"
    return {
        "id": f"CR-{index:02d}",
        "text": text,
        "kind": GRANDFATHERED_KIND,
        "acceptance_style": "binary",
        "evidence_kind": "attested",
        "quality_dimension": "functional_suitability",
        "measurable_signal": signal,
    }


def _state_payload(
    *,
    criteria: list[dict[str, Any]],
    file_scopes: list[str] | None = None,
) -> dict[str, Any]:
    """A minimal valid State with one CLOSED wave carrying *criteria*."""
    scopes = file_scopes if file_scopes is not None else ["src/eawf/surfaces/render/units.py"]
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": _T0.isoformat(),
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "EAWF",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {"project_code": "EAWF"},
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "EAWF",
                "track_id": None,
                "title": "P30",
                "status": "active",
                "iter_ids": ["P30-I20"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I20": {
                "id": "P30-I20",
                "phase_id": "P30",
                "title": "I20",
                "status": "closed",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": _T0.isoformat(),
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P30-I20",
                "title": "historical closed wave with legacy criteria",
                "status": "closed",
                "file_scopes": scopes,
                "success_criteria": criteria,
                "gates": [],
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "closed_at": _T0.isoformat(),
                "outcome": "ok",
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(state_path: Path, payload: dict[str, Any]) -> State:
    state = State.model_validate(payload)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state


def _build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at="2026-07-02T00:00:00+00:00",
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


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def _load_wave(state_path: Path) -> Any:
    payload = orjson.loads(state_path.read_bytes())
    return State.model_validate(payload).waves[_WAVE_ID]


def test_convert_legacy_converts_rows_through_daemon_transaction(tmp_path: Path) -> None:
    """CR-01: legacy rows become kind=converted with gates + tiers, persisted."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(criteria=[_legacy_criterion(1, _MEASURABLE_TEXT)]),
    )
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await convert_legacy(
            ctx,
            {"scope_id": _WAVE_ID, "repo_root": str(repo_root)},
        )
        assert result["converted_count"] == 1
        assert result["refused_count"] == 0
        assert result["rows"][0]["disposition"] == "converted"
        assert result["rows"][0]["gate_kind"]
        assert result["envelope"] is not None
        assert result["before_version"] != result["after_version"]

    _run(body)
    wave = _load_wave(state_path)
    criterion = wave.success_criteria[0]
    assert criterion.kind == CONVERTED_KIND
    assert criterion.evidence_kind == "deterministic"
    assert criterion.gate_ids
    assert criterion.oracle_tier is not None
    assert len(wave.gates) == 1
    assert wave.gates[0].criterion_id == criterion.id
    assert wave.gates[0].policy == "block"


def test_convert_legacy_dry_run_reports_without_writing(tmp_path: Path) -> None:
    """CR-02: --dry-run prints the would-convert set with no state write."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(criteria=[_legacy_criterion(1, _MEASURABLE_TEXT)]),
    )
    before_bytes = state_path.read_bytes()
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await convert_legacy(
            ctx,
            {"scope_id": _WAVE_ID, "dry_run": True, "repo_root": str(repo_root)},
        )
        assert result["dry_run"] is True
        assert result["converted_count"] == 1
        assert result["envelope"] is None
        assert result["before_version"] is None

    _run(body)
    assert state_path.read_bytes() == before_bytes
    wave = _load_wave(state_path)
    assert wave.success_criteria[0].kind == GRANDFATHERED_KIND


def test_convert_legacy_refuses_unmeasurable_row_with_named_reason(tmp_path: Path) -> None:
    """CR-03: an unmeasurable row stays legacy with a named EAWF021 reason."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(
            criteria=[
                _legacy_criterion(1, _MEASURABLE_TEXT),
                _legacy_criterion(2, _UNMEASURABLE_TEXT),
            ]
        ),
    )
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await convert_legacy(
            ctx,
            {"scope_id": _WAVE_ID, "repo_root": str(repo_root)},
        )
        assert result["converted_count"] == 1
        assert result["refused_count"] == 1
        refused = [row for row in result["rows"] if row["disposition"] == "refused"]
        assert refused[0]["criterion_id"] == "CR-02"
        assert "EAWF021" in refused[0]["reason"]

    _run(body)
    wave = _load_wave(state_path)
    assert wave.success_criteria[0].kind == CONVERTED_KIND
    assert wave.success_criteria[1].kind == GRANDFATHERED_KIND


def test_convert_legacy_refuses_wave_without_file_scopes(tmp_path: Path) -> None:
    """A scope-less wave refuses every row: the file-grep gate cannot anchor.

    Post-W25 a refusal is not silent: the row STAYS legacy but the named
    reason is persisted onto ``waiver_reason`` so committed state explains
    why the row was never retyped.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(criteria=[_legacy_criterion(1, _MEASURABLE_TEXT)], file_scopes=[]),
    )
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await convert_legacy(
            ctx,
            {"scope_id": _WAVE_ID, "repo_root": str(repo_root)},
        )
        assert result["converted_count"] == 0
        assert result["refused_count"] == 1
        assert "file_scopes" in result["rows"][0]["reason"]

    _run(body)
    payload = orjson.loads(state_path.read_bytes())
    row = payload["waves"][_WAVE_ID]["success_criteria"][0]
    assert row["kind"] == "legacy"
    assert row["waiver_reason"].startswith("non-convertible:")
    assert "file_scopes" in row["waiver_reason"]


def test_convert_legacy_disabled_rejects_generated_raw_waiver_before_write(
    tmp_path: Path,
) -> None:
    """Disabled mode rejects conversion refusal reasons atomically."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(criteria=[_legacy_criterion(1, _MEASURABLE_TEXT)], file_scopes=[]),
    )
    (repo_root / ".ea" / "config.yaml").write_text(
        "verify:\n  waiver_mode: disabled\n",
        encoding="utf-8",
    )
    before_bytes = state_path.read_bytes()
    ctx = _build_ctx(tmp_path, state_path)
    assert ctx.event_path is not None
    assert ctx.wal_dir is not None

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="waiver_mode_disabled"):
            await convert_legacy(
                ctx,
                {"scope_id": _WAVE_ID, "repo_root": str(repo_root)},
            )

    _run(body)

    assert state_path.read_bytes() == before_bytes
    assert not ctx.event_path.exists()
    assert list(ctx.wal_dir.iterdir()) == []


def test_convert_legacy_unknown_wave_scope_raises(tmp_path: Path) -> None:
    """An unknown wave scope raises ValueError (maps to -32602)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state_payload(criteria=[]))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown wave"):
            await convert_legacy(
                ctx,
                {"scope_id": "P30-I20-W99", "repo_root": str(repo_root)},
            )

    _run(body)


def test_convert_legacy_scope_without_legacy_rows_writes_nothing(tmp_path: Path) -> None:
    """A scope with zero legacy rows reports zero rows and leaves state alone."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state_payload(criteria=[]))
    before_bytes = state_path.read_bytes()
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await convert_legacy(
            ctx,
            {"scope_id": "P30-I20", "repo_root": str(repo_root)},
        )
        assert result["converted_count"] == 0
        assert result["refused_count"] == 0
        assert result["rows"] == []
        assert result["envelope"] is None

    _run(body)
    assert state_path.read_bytes() == before_bytes
