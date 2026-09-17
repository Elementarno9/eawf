"""``runtime.capture`` persists only the digest of a vendor session id.

A Claude Code session id names the session's local transcript file, so the
capture RPC hashes it before anything reaches ``runtime_baseline``,
``runtime_latest``, the interactive session attempt row or the event store.
The digest is still an exact session key: a repeat capture with the same raw
id is the same session to :func:`rebase_for_session`, so it folds nothing into
``runtime_carry``, and a baseline that still carries a raw id matches through
the idempotent helper.

The suite drives the real ``runtime_capture`` coroutine against a state file
under ``tmp_path``; no live daemon, no subprocess. Raw ids are derived at
runtime so the file carries no literal session id.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import orjson
import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import RuntimeLatest, State
from eawf.kernel.store.paths import store_path
from eawf.observability.logging.state_leak import state_leak_refusal
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.state import runtime_capture
from eawf.runtime.daemon.methods.state_runtime import rebase_for_session
from eawf.runtime.session.vendor_id import hash_vendor_session_id, same_vendor_session

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
WAVE_A = "P40-I01-W01"
WAVE_B = "P40-I01-W02"
RAW_SESSION = str(uuid.uuid5(uuid.NAMESPACE_URL, "vendor-session-a"))
OTHER_RAW_SESSION = str(uuid.uuid5(uuid.NAMESPACE_URL, "vendor-session-b"))


def _baseline(session_id: str | None) -> dict[str, Any]:
    return {
        "api_duration_ms": 0,
        "total_duration_ms": 0,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "session_id": session_id,
        "captured_at": _T0.isoformat(),
    }


def _wave_row(wave_id: str, *, baseline_session_id: str | None) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": "P40-I01",
        "title": f"wave {wave_id}",
        "status": "claimed",
        "claim_session_id": "SES-claim-1",
        "opened_at": _T0.isoformat(),
        "claimed_at": _T0.isoformat(),
        "runtime_baseline": _baseline(baseline_session_id),
        "sessions": {},
    }


def state_payload(
    *,
    active_wave_ids: list[str],
    baseline_session_id: str | None = None,
) -> dict[str, Any]:
    """A minimal valid State whose active waves carry a zero-origin baseline."""
    waves = {
        wave_id: _wave_row(wave_id, baseline_session_id=baseline_session_id)
        for wave_id in active_wave_ids
    }
    return {
        "schema_version": "1.9",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _T0.isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {
            "project_code": "ABC",
            "phase_id": "P40",
            "iter_id": "P40-I01",
            "active_wave_ids": active_wave_ids,
        },
        "workspace": None,
        "phases": {
            "P40": {
                "id": "P40",
                "scope_id": "ABC",
                "track_id": None,
                "title": "P40",
                "status": "active",
                "iter_ids": ["P40-I01"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P40-I01": {
                "id": "P40-I01",
                "phase_id": "P40",
                "title": "I01",
                "status": "active",
                "wave_ids": list(waves),
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": waves,
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def method_ctx(tmp_path: Path, payload: dict[str, Any]) -> tuple[MethodContext, Path]:
    """Write *payload* under ``tmp_path`` and return a daemon context bound to it."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    ctx = MethodContext(
        started_at=_T0.isoformat(),
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=store_path(state_path, StoreKind.EVENT),
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )
    return ctx, state_path


def capture_params(**overrides: Any) -> dict[str, Any]:
    """A priced cumulative capture from the raw vendor session."""
    payload: dict[str, Any] = {
        "api_duration_ms": 17_000,
        "total_duration_ms": 21_000,
        "cost_usd": "0.42",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 7,
        "session_id": RAW_SESSION,
        "captured_at": (_T0 + timedelta(minutes=5)).isoformat(),
    }
    payload.update(overrides)
    return payload


def rpc(call: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    """Drive one registered daemon method to completion."""
    return asyncio.run(cast(Coroutine[Any, Any, dict[str, Any]], call))


def _load(state_path: Path) -> State:
    return State.model_validate(orjson.loads(state_path.read_bytes()))


def _capture(ctx: MethodContext, **overrides: Any) -> dict[str, Any]:
    return rpc(runtime_capture(ctx, capture_params(**overrides)))


def test_hash_vendor_session_id_returns_prefixed_digest() -> None:
    hashed = hash_vendor_session_id(RAW_SESSION)

    assert hashed.startswith("vsid-")
    assert len(hashed) == len("vsid-") + 32
    assert RAW_SESSION not in hashed
    assert hashed == hash_vendor_session_id(RAW_SESSION)
    assert hashed != hash_vendor_session_id(OTHER_RAW_SESSION)


def test_hash_vendor_session_id_is_idempotent() -> None:
    hashed = hash_vendor_session_id(RAW_SESSION)

    assert hash_vendor_session_id(hashed) == hashed


def test_hash_vendor_session_id_single_char_ids_stay_distinct() -> None:
    assert hash_vendor_session_id("a") != hash_vendor_session_id("b")


def test_hash_vendor_session_id_hashes_near_miss_digest_shape() -> None:
    hashed = hash_vendor_session_id(RAW_SESSION)
    one_short = hashed[:-1]
    uppercase = hashed.upper()

    assert hash_vendor_session_id(one_short) != one_short
    assert hash_vendor_session_id(uppercase) != uppercase


def test_hash_vendor_session_id_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        hash_vendor_session_id("")


def test_same_vendor_session_matches_legacy_raw_against_hashed() -> None:
    hashed = hash_vendor_session_id(RAW_SESSION)

    assert same_vendor_session(RAW_SESSION, hashed)
    assert same_vendor_session(hashed, RAW_SESSION)
    assert same_vendor_session(hashed, hashed)
    assert not same_vendor_session(RAW_SESSION, OTHER_RAW_SESSION)


@pytest.mark.parametrize(("stored", "incoming"), [(None, None), ("", ""), (None, RAW_SESSION)])
def test_same_vendor_session_missing_id_matches_nothing(
    stored: str | None, incoming: str | None
) -> None:
    assert not same_vendor_session(stored, incoming)


def test_runtime_capture_persists_only_hashed_session_id(tmp_path: Path) -> None:
    ctx, state_path = method_ctx(tmp_path, state_payload(active_wave_ids=[WAVE_A]))
    before = orjson.loads(state_path.read_bytes())
    hashed = hash_vendor_session_id(RAW_SESSION)

    result = _capture(ctx)

    wave = _load(state_path).waves[WAVE_A]
    assert result["active_wave_ids"] == [WAVE_A]
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.session_id == hashed
    assert wave.runtime_latest is not None
    assert wave.runtime_latest.session_id == hashed
    assert set(wave.sessions) == {1}
    attempt = wave.sessions[1]
    assert attempt.session_id == hashed
    assert attempt.session_log_handle == f"urn:eawf:v1:session-log:claude-code:{hashed}"
    assert attempt.subprocess_pid is None
    assert RAW_SESSION not in state_path.read_text(encoding="utf-8")
    event_text = store_path(state_path, StoreKind.EVENT).read_text(encoding="utf-8")
    assert hashed in event_text
    assert RAW_SESSION not in event_text
    # The digest carries no leak shape, so a canonical writer would accept it.
    assert state_leak_refusal(before, orjson.loads(state_path.read_bytes())) is None


def test_runtime_capture_same_raw_id_twice_folds_nothing(tmp_path: Path) -> None:
    ctx, state_path = method_ctx(tmp_path, state_payload(active_wave_ids=[WAVE_A]))

    _capture(ctx, cost_usd="0.10")
    first_baseline = _load(state_path).waves[WAVE_A].runtime_baseline
    _capture(
        ctx,
        cost_usd="0.30",
        api_duration_ms=19_000,
        captured_at=(_T0 + timedelta(minutes=9)).isoformat(),
    )

    wave = _load(state_path).waves[WAVE_A]
    assert wave.runtime_carry is None
    assert wave.runtime_baseline == first_baseline
    assert set(wave.sessions) == {1}
    assert wave.sessions[1].cost_usd == pytest.approx(0.30)
    assert wave.runtime_latest is not None
    assert wave.runtime_latest.api_duration_ms == 19_000


def test_runtime_capture_legacy_raw_baseline_matches_hashed_capture(tmp_path: Path) -> None:
    payload = state_payload(active_wave_ids=[WAVE_A], baseline_session_id=RAW_SESSION)
    legacy_handle = f"urn:eawf:v1:session-log:claude-code:{RAW_SESSION}"
    payload["waves"][WAVE_A]["sessions"] = {
        "1": {
            "attempt": 1,
            "runtime": "claude-code",
            "session_id": RAW_SESSION,
            "session_log_handle": legacy_handle,
            "started_at": _T0.isoformat(),
            "ended_at": (_T0 + timedelta(minutes=1)).isoformat(),
            "exit_status": 0,
            "cost_usd": 0.05,
        }
    }
    ctx, state_path = method_ctx(tmp_path, payload)
    hashed = hash_vendor_session_id(RAW_SESSION)

    _capture(ctx)

    wave = _load(state_path).waves[WAVE_A]
    assert wave.runtime_carry is None
    assert wave.runtime_baseline is not None
    # The legacy baseline is kept as written; only the rows this capture writes
    # carry the digest.
    assert wave.runtime_baseline.session_id == RAW_SESSION
    assert wave.runtime_latest is not None
    assert wave.runtime_latest.session_id == hashed
    assert set(wave.sessions) == {1}
    assert wave.sessions[1].session_id == hashed
    assert wave.sessions[1].cost_usd == pytest.approx(0.42)


def test_rebase_for_session_legacy_raw_baseline_keeps_snapshots() -> None:
    state = State.model_validate(
        state_payload(active_wave_ids=[WAVE_A], baseline_session_id=RAW_SESSION)
    )
    wave = state.waves[WAVE_A]
    baseline = wave.runtime_baseline
    incoming = RuntimeLatest(
        api_duration_ms=5,
        cost_usd=0.01,
        session_id=hash_vendor_session_id(RAW_SESSION),
        captured_at=_T0,
    )

    rebase_for_session(wave, incoming, hash_vendor_session_id(RAW_SESSION))

    assert wave.runtime_baseline == baseline
    assert wave.runtime_carry is None


def test_runtime_capture_other_session_folds_once_and_rebases_hashed(tmp_path: Path) -> None:
    ctx, state_path = method_ctx(tmp_path, state_payload(active_wave_ids=[WAVE_A]))

    _capture(ctx)
    _capture(
        ctx,
        session_id=OTHER_RAW_SESSION,
        api_duration_ms=3_000,
        captured_at=(_T0 + timedelta(minutes=9)).isoformat(),
    )

    wave = _load(state_path).waves[WAVE_A]
    assert wave.runtime_carry is not None
    assert wave.runtime_carry.sessions_folded == 1
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.session_id == hash_vendor_session_id(OTHER_RAW_SESSION)
    text = state_path.read_text(encoding="utf-8")
    assert RAW_SESSION not in text
    assert OTHER_RAW_SESSION not in text


def test_runtime_capture_empty_session_id_is_treated_as_absent(tmp_path: Path) -> None:
    ctx, state_path = method_ctx(tmp_path, state_payload(active_wave_ids=[WAVE_A]))

    _capture(ctx, session_id="")

    wave = _load(state_path).waves[WAVE_A]
    assert wave.runtime_latest is not None
    assert wave.runtime_latest.session_id is None
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.session_id is None
    assert wave.sessions[1].session_id == f"interactive:{WAVE_A}"
