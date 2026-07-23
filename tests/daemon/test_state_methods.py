"""Tests for the ``state.*`` JSON-RPC handlers (P24-W09).

Covers the W09 mutator path end-to-end:

* :func:`state.read` returns the full state + a stable digest version.
* :func:`state.digest` returns the SHA256 of the on-disk bytes.
* :func:`state.mutate` applies one :class:`MutationKind.WAVE_CLOSE`
  payload, writes the WAL ``.fsynced.json`` record, atomic-writes
  ``state.json``, appends the canonical event envelope to
  ``event.jsonl``, and publishes the envelope on the subscription
  bus.
* :func:`state.mutate` idempotency: a repeat call with the same
  ``idempotency_key`` returns the cached envelope verbatim with
  ``idempotent_replay=True``.
* :func:`state.mutate` rejects an invalid mutation with
  ``ValueError("validation_failed: ...")`` (mapped to ``-32002``).
* :func:`state.mutate` WAL replay path: simulate a crash between
  ``.applied`` and ``.fsynced`` → restart →
  :func:`eawf.runtime.daemon.recovery.replay_wal` runs → ``event.jsonl``
  carries the envelope (or stays consistent if it was already
  written) without re-executing the mutator.

The handlers are driven through the module-level coroutines — JSON-RPC
framing is exercised in :mod:`tests.daemon.test_scaffolding`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import orjson
import pytest

from eawf import __version__
from eawf.kernel.spec.common import grandfather_criterion
from eawf.kernel.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    DecisionStatus,
    StoreKind,
)
from eawf.kernel.state.models import CriteriaFloorWaiver
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.kinds.event import EventKind
from eawf.kernel.store.paths import store_path
from eawf.observability.telemetry.join import DEFAULT_TOKENS_PER_EU
from eawf.runtime.daemon import PROTOCOL_VERSION, recovery, wal
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state import digest, mutate, read
from eawf.runtime.daemon.wal import WalStatus

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _build_state_payload(
    *,
    wave_id: str = "P24-I01-W09",
    wave_status: str = "claimed",
) -> dict[str, object]:
    """Construct a minimal valid State payload with one claimed wave."""
    iter_id = "P24-I01"
    phase_id = "P24"
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
        "phases": {
            phase_id: {
                "id": phase_id,
                "scope_id": "ABC",
                "track_id": None,
                "title": "Plan import",
                "status": "active",
                "iter_ids": [iter_id],
                "outcome_ids": [],
                "opened_at": _now().isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            iter_id: {
                "id": iter_id,
                "phase_id": phase_id,
                "title": "First iter",
                "status": "active",
                "wave_ids": [wave_id],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _now().isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            wave_id: {
                "id": wave_id,
                "iter_id": iter_id,
                "title": "test wave",
                "status": wave_status,
                "success_criteria": [
                    {
                        "id": "CR-01",
                        "text": "the daemon mutation produces the expected state transition",
                        "kind": "deterministic",
                        "acceptance_style": "binary",
                        "evidence_kind": "deterministic",
                        "gate_ids": [],
                        "required": True,
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": (
                            "the focused daemon test observes the expected state row"
                        ),
                    }
                ],
                "claim_session_id": "session-abc" if wave_status == "claimed" else None,
                "opened_at": _now().isoformat(),
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _build_planned_phase_payload(
    *,
    phase_id: str = "P50",
    iter_status: str = "planned",
    waves: dict[str, object] | None = None,
) -> dict[str, object]:
    """Construct a minimal valid State with one PLANNED phase + iter.

    The roadmap mutation kinds (``ROADMAP_REVISE`` / ``ROADMAP_APPLY`` /
    ``ROADMAP_DROP``) operate on the PLANNED queue, so they need a
    PLANNED phase rather than the ACTIVE one :func:`_build_state_payload`
    builds. ``waves`` lets a caller seed the phase's iter with waves
    (e.g. so ``ROADMAP_APPLY``'s ≥1-wave gate passes).
    """
    iter_id = f"{phase_id}-I01"
    wave_map = waves or {}
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
        "phases": {
            phase_id: {
                "id": phase_id,
                "scope_id": "ABC",
                "track_id": None,
                "title": phase_id,
                "status": "planned",
                "iter_ids": [iter_id],
                "outcome_ids": [],
                "opened_at": _now().isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            iter_id: {
                "id": iter_id,
                "phase_id": phase_id,
                "title": "First iter",
                "status": iter_status,
                "wave_ids": list(wave_map),
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _now().isoformat(),
                "closed_at": None,
            }
        },
        "waves": dict(wave_map),
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _pending_wave(
    wave_id: str, iter_id: str, *, deps: list[str] | None = None
) -> dict[str, object]:
    """Build one minimal PENDING wave dict for a seeded planned phase."""
    return {
        "id": wave_id,
        "iter_id": iter_id,
        "title": "seed wave",
        "status": "pending",
        "deps": list(deps or []),
        "claim_session_id": None,
        "opened_at": _now().isoformat(),
        "sessions": {},
    }


def _decision_payload(
    decision_id: str,
    *,
    status: str = "active",
    superseded_by: str | None = None,
    obsoleted_at: str | None = None,
) -> dict[str, object]:
    """Build one minimal decision row for daemon mutation tests."""
    return {
        "id": decision_id,
        "scope_id": "ABC",
        "title": f"decision {decision_id}",
        "rationale": "because",
        "alternatives": [],
        "consequences": [],
        "status": status,
        "created_at": _now().isoformat(),
        "superseded_by": superseded_by,
        "obsoleted_at": obsoleted_at,
    }


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _write_verify_profile(root: Path, *, enforce: bool) -> None:
    """Select a local profile with a deterministic failing floor check."""
    profile_dir = root / ".ea" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (root / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled:\n    - enforcing\n",
        encoding="utf-8",
    )
    profile_dir.joinpath("enforcing.yaml").write_text(
        "\n".join(
            [
                "name: enforcing",
                "verify:",
                f"  enforce: {'true' if enforce else 'false'}",
                "  argv_allowlist:",
                "    - git",
                "  floor_checks:",
                "    - name: fail-floor",
                '      cmd: ["git", "show", "no-such-ref-w26-daemon"]',
                "      scope: all",
                "      cadence: every-wave",
                # W13: only policy: block floor rows gate the close.
                "      policy: block",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_disabled_waiver_config(root: Path) -> None:
    """Set the strict repo waiver policy used by H04 daemon tests."""
    config_dir = root / ".ea"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.joinpath("config.yaml").write_text(
        "verify:\n  waiver_mode: disabled\n",
        encoding="utf-8",
    )


def _write_eu_basis_config(root: Path, *, eu_basis: str) -> None:
    """Write a repo config overriding the close-time EU basis."""
    config_dir = root / ".ea"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.joinpath("config.yaml").write_text(
        f"estimation:\n  eu_basis: {eu_basis}\n",
        encoding="utf-8",
    )


def _add_runtime_delta(payload: dict[str, object]) -> None:
    """Add non-zero runtime baseline/latest counters to the default wave."""
    wave = payload["waves"]["P24-I01-W09"]  # type: ignore[index]
    assert isinstance(wave, dict)
    wave["runtime_baseline"] = {
        "api_duration_ms": 5000,
        "total_duration_ms": 7000,
        "captured_at": _now().isoformat(),
    }
    wave["runtime_latest"] = {
        "api_duration_ms": 17000,
        "total_duration_ms": 23000,
        "captured_at": (_now() + timedelta(minutes=5)).isoformat(),
    }


def _build_ctx(
    *,
    tmp_path: Path,
    state_payload: dict[str, object] | None = None,
) -> tuple[MethodContext, Path, Path, Path]:
    """Build a wired :class:`MethodContext` for the W09 mutator tests.

    Returns the context plus the state / event / wal paths so tests
    can poke at the on-disk artefacts directly.
    """
    state_path = tmp_path / "state.json"
    if state_payload is None:
        state_payload = _build_state_payload()
        _add_runtime_delta(state_payload)
    _write_state(state_path, state_payload)
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    ctx = MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
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
    return ctx, state_path, event_path, wal_dir


def _run(body: Callable[[], Awaitable[None]]) -> None:
    """Run an async test body without ``pytest-asyncio``."""
    asyncio.run(body())


def _iter_close_payload(*, verdict: AuditVerdict = AuditVerdict.PASS) -> dict[str, object]:
    """Return a closable iter with one real evaluation audit."""
    payload = _build_state_payload()
    payload["waves"] = {}
    iter_row = payload["iters"]["P24-I01"]  # type: ignore[index]
    iter_row["wave_ids"] = []  # type: ignore[index]
    payload["audits"] = {
        "AUD-ITER": {
            "id": "AUD-ITER",
            "scope_id": "P24-I01",
            "kind": AuditKind.EVALUATION.value,
            "status": AuditStatus.COMPLETE.value,
            "created_at": _now().isoformat(),
            "verdict": verdict.value,
            "check_results": [
                {
                    "name": "acceptance",
                    "passed": True,
                    "details": "targeted verification passed",
                }
            ],
        }
    }
    return payload


def _path_digest(path: Path) -> str | None:
    """Return the content digest for *path*, or ``None`` when absent."""
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


# ---- state.read -------------------------------------------------------------


def test_state_read_returns_state_and_version(tmp_path: Path) -> None:
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        result: dict[str, Any] = await read(ctx, {})
        assert "state" in result and "version" in result
        assert result["state"]["project"]["code"] == "ABC"
        assert isinstance(result["version"], str)
        assert len(result["version"]) == 16

    _run(body)


def test_state_read_rejects_unknown_field(tmp_path: Path) -> None:
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        with pytest.raises(Exception, match=r"(unexpected|extra)"):
            await read(ctx, {"unexpected": "field"})

    _run(body)


# ---- state.digest -----------------------------------------------------------


def test_state_digest_returns_sha256(tmp_path: Path) -> None:
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        first: dict[str, Any] = await digest(ctx, {})
        second: dict[str, Any] = await digest(ctx, {})
        assert first["version"] == second["version"]
        assert len(first["version"]) == 16

    _run(body)


def test_state_digest_publishes_wave_elapsed_update_once_per_minute(tmp_path: Path) -> None:
    payload = _build_state_payload()
    wave = payload["waves"]["P24-I01-W09"]  # type: ignore[index]
    wave["effort_bucket"] = "M"  # type: ignore[index]
    # The elapsed publisher anchors on claimed_at (work-start), not
    # opened_at (plan/creation), so a wave planned long before it is
    # claimed never inflates its elapsed clock.
    wave["claimed_at"] = (datetime.now(UTC) - timedelta(minutes=45, seconds=5)).isoformat()  # type: ignore[index]
    ctx, _state_path, event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    bus = ctx.bus
    assert isinstance(bus, EventBus)
    sub = bus.register(connection_id="elapsed-sub")

    async def body() -> None:
        await digest(ctx, {})
        await digest(ctx, {})
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1
        event = orjson.loads(rows[0])
        payload = event["payload"]
        assert payload["event_type"] == "wave_elapsed_update"
        assert payload["event_kind"] == "wave_elapsed_update"
        assert payload["status"] == "error"
        assert payload["extras"]["wave_id"] == "P24-I01-W09"
        assert payload["extras"]["elapsed_minute"] >= 45
        assert payload["extras"]["elapsed_band"] == "err"
        assert len(sub.queue) == 1

    _run(body)


def test_state_digest_publishes_no_wave_elapsed_update_when_claimed_at_unset(
    tmp_path: Path,
) -> None:
    """A wave with a stale opened_at but no claimed_at emits no elapsed update.

    The publisher anchors on claimed_at (work-start), so a wave that was
    planned long ago but has not been claimed must not inflate an elapsed
    clock from its opened_at.
    """
    payload = _build_state_payload()
    wave = payload["waves"]["P24-I01-W09"]  # type: ignore[index]
    wave["effort_bucket"] = "M"  # type: ignore[index]
    wave["opened_at"] = (datetime.now(UTC) - timedelta(minutes=45, seconds=5)).isoformat()  # type: ignore[index]
    wave.pop("claimed_at", None)  # type: ignore[union-attr]
    ctx, _state_path, event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)

    async def body() -> None:
        await digest(ctx, {})
        await digest(ctx, {})
        assert not event_path.exists() or event_path.read_text().strip() == ""

    _run(body)


# ---- state.mutate (happy path) ----------------------------------------------


def test_mutate_wave_close_persists_state_event_wal(tmp_path: Path) -> None:
    """End-to-end wave_close: state updated + event appended + WAL fsynced."""
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "test-close"},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert result["idempotent_replay"] is False
        assert len(result["before_version"]) == 16
        assert len(result["after_version"]) == 16
        assert result["before_version"] != result["after_version"]
        envelope = result["event"]
        assert envelope["kind"] == StoreKind.EVENT.value
        assert envelope["scope_id"] == "P24-I01-W09"

        # state.json reflects the close.
        new_state = orjson.loads(state_path.read_bytes())
        assert new_state["waves"]["P24-I01-W09"]["status"] == "closed"
        assert new_state["waves"]["P24-I01-W09"]["outcome"] == "test-close"

        # event.jsonl carries exactly one envelope with the same id.
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1
        on_disk = orjson.loads(rows[0])
        assert on_disk["id"] == envelope["id"]
        assert on_disk["kind"] == StoreKind.EVENT.value

        # WAL record fully fsynced; no pending / applied lingering.
        assert list(wal_dir.glob("*.pending.json")) == []
        assert list(wal_dir.glob("*.applied.json")) == []
        fsynced = list(wal_dir.glob("*.fsynced.json"))
        assert len(fsynced) == 1

    _run(body)


def test_mutate_track_add_switch_persists_state_event_wal(tmp_path: Path) -> None:
    """End-to-end track add/switch: daemon applies Track mutations."""
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)
    add_mutation = Mutation(
        kind=MutationKind.TRACK_ADD,
        scope_id="COLLAR",
        mutation_id=uuid.uuid4().hex,
        params={
            "code": "COLLAR",
            "kind": "strategy",
            "title": "Collar",
            "domains": ["quant"],
        },
    )
    switch_mutation = Mutation(
        kind=MutationKind.TRACK_SWITCH,
        scope_id="COLLAR",
        mutation_id=uuid.uuid4().hex,
        params={"code": "COLLAR"},
    )

    async def body() -> None:
        add_result: dict[str, Any] = await mutate(
            ctx, {"mutation": add_mutation.model_dump(mode="json")}
        )
        switch_result: dict[str, Any] = await mutate(
            ctx, {"mutation": switch_mutation.model_dump(mode="json")}
        )
        assert add_result["idempotent_replay"] is False
        assert switch_result["idempotent_replay"] is False

        written = orjson.loads(state_path.read_bytes())
        assert written["tracks"]["COLLAR"]["kind"] == "strategy"
        assert written["tracks"]["COLLAR"]["domains"] == ["quant"]
        assert written["project"]["track_ids"] == ["COLLAR"]
        assert written["current"]["track_id"] == "COLLAR"

        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 2
        assert [orjson.loads(row)["payload"]["command"] for row in rows] == [
            "state.mutate.track_add",
            "state.mutate.track_switch",
        ]
        assert list(wal_dir.glob("*.pending.json")) == []
        assert list(wal_dir.glob("*.applied.json")) == []
        assert len(list(wal_dir.glob("*.fsynced.json"))) == 2

    _run(body)


def test_mutate_publishes_to_bus(tmp_path: Path) -> None:
    """The mutator publishes the post-apply envelope on the subscription bus."""
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)
    bus = ctx.bus
    assert isinstance(bus, EventBus)
    sub = bus.register(connection_id="test-sub")

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "test"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        # Subscriber should have received exactly one envelope.
        assert len(sub.queue) == 1

    _run(body)


def test_mutate_wave_close_publishes_wave_closed_event_kind(tmp_path: Path) -> None:
    """WAVE_CLOSE envelope carries ``event_kind='wave_closed'`` + token/cost extras.

    P28-I02-W03: the daemon's wave-close mutator maps MutationKind.WAVE_CLOSE
    to the closed ``EventKind`` literal ``wave_closed`` and surfaces the
    upserted ActualSummary's ``actual_tokens`` + ``actual_cost_usd`` on the
    envelope ``extras`` so subscribers see the cost view without re-reading
    state.json.
    """
    # Pre-seed one tally, then override it through mutation params so
    # the close path proves the final tally is accepted at close time.
    _write_verify_profile(tmp_path, enforce=False)
    payload = _build_state_payload()
    payload["waves"]["P24-I01-W09"]["tokens_consumed"] = 111  # type: ignore[index]
    ctx, _state_path, event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok", "tokens_consumed": 7777},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        envelope_payload = result["event"]["payload"]
        assert envelope_payload["event_kind"] == "wave_closed"
        assert envelope_payload["extras"]["actual_written_auto"] is True
        assert envelope_payload["extras"]["actual_tokens"] == 7777
        assert envelope_payload["extras"]["actual_cost_usd"] == 0.0
        # The on-disk JSONL row carries the same typed discriminator + extras.
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1
        on_disk = orjson.loads(rows[0])
        assert on_disk["payload"]["event_kind"] == "wave_closed"
        assert on_disk["payload"]["extras"]["actual_written_auto"] is True
        assert on_disk["payload"]["extras"]["actual_tokens"] == 7777

    _run(body)


def test_mutate_wave_close_derives_attention_eu_from_telemetry_duration(
    tmp_path: Path,
) -> None:
    """Wave close auto-actual reads projected telemetry session duration."""
    from decimal import Decimal

    from eawf.observability.telemetry.models import TelemetrySession
    from eawf.observability.telemetry.store import SqliteMetricsStore, metrics_db_path

    payload = _build_state_payload()
    wave = payload["waves"]["P24-I01-W09"]  # type: ignore[index]
    assert isinstance(wave, dict)
    wave["sessions"] = {
        "1": {
            "attempt": 1,
            "runtime": "codex",
            "session_id": "sess-close-1",
            "session_log_handle": "urn:eawf:v1:session-log:codex:sess-close-1",
            "started_at": _now().isoformat(),
            "ended_at": (_now() + timedelta(minutes=30)).isoformat(),
            "exit_status": 0,
        }
    }
    ctx, state_path, event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    store = SqliteMetricsStore(metrics_db_path(state_path))
    store.init_schema()
    store.upsert(
        "telemetry_sessions",
        TelemetrySession(
            session_id="sess-close-1",
            project_id="repo/eawf",
            runtime="codex",
            wave_id="P24-I01-W09",
            attempt_id="1",
            session_log_path="opaque://sess-close-1",
            started_at=_now(),
            ended_at=_now() + timedelta(minutes=30),
            duration_ms=1_800_000,
            model_primary=None,
            total_cost_usd=Decimal("0"),
            end_marker="clean_stop",
        ),
    )
    store.commit()
    store.close()
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        extras = result["event"]["payload"]["extras"]
        assert extras["actual_written_auto"] is True
        assert extras["actual_attention_eu"] == pytest.approx(1.0)
        written = orjson.loads(state_path.read_bytes())
        actual = written["actuals"]["P24-I01-W09"]
        assert actual["attention_eu"] == pytest.approx(1.0)
        # ``agent_runtime_eu`` is None until WaveSessionRollup grows its own
        # runtime-EU column; the close path never substitutes attention for
        # runtime (the two metrics measure different things).
        assert actual["agent_runtime_eu"] is None
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1
        on_disk = orjson.loads(rows[0])
        assert on_disk["payload"]["extras"]["actual_attention_eu"] == pytest.approx(1.0)

    _run(body)


def test_mutate_wave_close_does_not_conflate_attention_and_runtime_eu(
    tmp_path: Path,
) -> None:
    """Wave close separates ``attention_eu`` and ``agent_runtime_eu`` rollups.

    A42 surfaced the daemon close path reading
    ``wave_session_rollup.attention_eu`` for *both* the attention and
    runtime EU fields on the persisted :class:`ActualSummary`. Until the
    rollup model gains its own runtime-EU column the runtime field stays
    ``None`` rather than echoing attention — the two metrics measure
    different things and a substituted value would mis-rollup the
    downstream variance / velocity numbers. This pins the symptom fix so a
    future regression cannot quietly conflate them again.
    """
    from decimal import Decimal

    from eawf.observability.telemetry.models import TelemetrySession
    from eawf.observability.telemetry.store import SqliteMetricsStore, metrics_db_path

    payload = _build_state_payload()
    wave = payload["waves"]["P24-I01-W09"]  # type: ignore[index]
    assert isinstance(wave, dict)
    wave["sessions"] = {
        "1": {
            "attempt": 1,
            "runtime": "codex",
            "session_id": "sess-no-conflate-1",
            "session_log_handle": "urn:eawf:v1:session-log:codex:sess-no-conflate-1",
            "started_at": _now().isoformat(),
            "ended_at": (_now() + timedelta(minutes=45)).isoformat(),
            "exit_status": 0,
        }
    }
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    store = SqliteMetricsStore(metrics_db_path(state_path))
    store.init_schema()
    store.upsert(
        "telemetry_sessions",
        TelemetrySession(
            session_id="sess-no-conflate-1",
            project_id="repo/eawf",
            runtime="codex",
            wave_id="P24-I01-W09",
            attempt_id="1",
            session_log_path="opaque://sess-no-conflate-1",
            started_at=_now(),
            ended_at=_now() + timedelta(minutes=45),
            duration_ms=2_700_000,  # 45 min → 1.5 EU at 30 min/EU.
            model_primary=None,
            total_cost_usd=Decimal("0"),
            end_marker="clean_stop",
        ),
    )
    store.commit()
    store.close()
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        actual = written["actuals"]["P24-I01-W09"]
        # Attention EU lands as the telemetry-derived value (1.5 EU).
        assert actual["attention_eu"] == pytest.approx(1.5)
        # Runtime EU is *not* substituted from attention; the deferred
        # WaveSessionRollup runtime-EU column carries the real signal once
        # that wave lands. Until then the field stays ``None``.
        assert actual["agent_runtime_eu"] is None
        assert actual["attention_eu"] != actual["agent_runtime_eu"]

    _run(body)


def test_mutate_wave_close_derives_elapsed_eu_from_telemetry_duration(
    tmp_path: Path,
) -> None:
    """Wave close auto-actual records elapsed_eu from session runtime."""
    from decimal import Decimal

    from eawf.observability.telemetry.models import TelemetrySession
    from eawf.observability.telemetry.store import SqliteMetricsStore, metrics_db_path

    payload = _build_state_payload()
    wave = payload["waves"]["P24-I01-W09"]  # type: ignore[index]
    assert isinstance(wave, dict)
    wave["sessions"] = {
        "1": {
            "attempt": 1,
            "runtime": "codex",
            "session_id": "sess-elapsed-1",
            "session_log_handle": "urn:eawf:v1:session-log:codex:sess-elapsed-1",
            "started_at": _now().isoformat(),
            "ended_at": (_now() + timedelta(minutes=30)).isoformat(),
            "exit_status": 0,
        }
    }
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    store = SqliteMetricsStore(metrics_db_path(state_path))
    store.init_schema()
    store.upsert(
        "telemetry_sessions",
        TelemetrySession(
            session_id="sess-elapsed-1",
            project_id="repo/eawf",
            runtime="codex",
            wave_id="P24-I01-W09",
            attempt_id="1",
            session_log_path="opaque://sess-elapsed-1",
            started_at=_now(),
            ended_at=_now() + timedelta(minutes=30),
            duration_ms=1_800_000,  # 30 min -> 1.0 EU at 30 min/EU.
            model_primary=None,
            total_cost_usd=Decimal("0"),
            end_marker="clean_stop",
        ),
    )
    store.commit()
    store.close()
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        actual = written["actuals"]["P24-I01-W09"]
        # The measured session runtime (30 min) derives 1.0 elapsed EU.
        assert actual["elapsed_eu"] == pytest.approx(1.0)

    _run(body)


def test_mutate_wave_close_uses_runtime_delta_when_captured(tmp_path: Path) -> None:
    """Baseline/latest counters drive close-time runtime actuals."""
    payload = _build_state_payload()
    wave = payload["waves"]["P24-I01-W09"]  # type: ignore[index]
    assert isinstance(wave, dict)
    wave["runtime_baseline"] = {
        "api_duration_ms": 5000,
        "total_duration_ms": 7000,
        "cost_usd": 0.10,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "captured_at": _now().isoformat(),
    }
    wave["runtime_latest"] = {
        "api_duration_ms": 17000,
        "total_duration_ms": 23000,
        "cost_usd": 0.52,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 7,
        "captured_at": (_now() + timedelta(minutes=5)).isoformat(),
    }
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        actual = written["actuals"]["P24-I01-W09"]
        expected_eu = 12000 / (30 * 60_000)
        assert actual["elapsed_eu"] == pytest.approx(expected_eu)
        assert actual["agent_runtime_eu"] == pytest.approx(expected_eu)
        # Work tokens exclude cache reads (7): 100 input + 50 output + 5 write.
        assert actual["actual_tokens"] == 155
        assert actual["actual_cost_usd"] == pytest.approx(0.42)

    _run(body)


def test_mutate_wave_close_uses_configured_token_basis(tmp_path: Path) -> None:
    """``estimation.eu_basis=tokens`` derives elapsed EU from token delta."""
    _write_eu_basis_config(tmp_path, eu_basis="tokens")
    payload = _build_state_payload()
    wave = payload["waves"]["P24-I01-W09"]  # type: ignore[index]
    assert isinstance(wave, dict)
    wave["runtime_baseline"] = {
        "api_duration_ms": 5000,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 15,
        "captured_at": _now().isoformat(),
    }
    wave["runtime_latest"] = {
        "api_duration_ms": 17000,
        "input_tokens": 600,
        "output_tokens": 220,
        "cache_creation_input_tokens": 55,
        "cache_read_input_tokens": 65,
        "captured_at": (_now() + timedelta(minutes=5)).isoformat(),
    }
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        actual = written["actuals"]["P24-I01-W09"]
        # 500 input + 200 output + 50 cache-write; the 50-token cache-read delta
        # is billed but is not work, so the TOKENS basis does not count it.
        assert actual["elapsed_eu"] == pytest.approx(750 / DEFAULT_TOKENS_PER_EU)
        assert actual["agent_runtime_eu"] == pytest.approx(750 / DEFAULT_TOKENS_PER_EU)
        assert actual["actual_tokens"] == 750

    _run(body)


def test_close_refuses_zero_runtime(tmp_path: Path) -> None:
    """Default zero-runtime close is refused and leaves the wave unclosed."""
    from eawf.runtime.daemon.methods import DaemonValidationError

    payload = _build_state_payload()
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="no captured runtime"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        assert written["waves"]["P24-I01-W09"]["status"] == "claimed"
        assert written.get("actuals") in ({}, None)

    _run(body)


def test_close_no_runtime_without_flag_still_refuses(tmp_path: Path) -> None:
    """The no-runtime waiver is opt-in; absence still rejects zero runtime."""
    from eawf.runtime.daemon.methods import DaemonValidationError

    payload = _build_state_payload()
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="no captured runtime"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        assert written["waves"]["P24-I01-W09"]["status"] == "claimed"

    _run(body)


def test_close_with_no_runtime_waiver_succeeds(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A close-scoped no-runtime waiver bypasses the enforcing zero-runtime gate."""
    payload = _build_state_payload()
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={
            "wave_id": "P24-I01-W09",
            "outcome": "ok",
            "no_runtime_waiver": True,
        },
    )

    async def body() -> None:
        with caplog.at_level("WARNING", logger="eawf.runtime.daemon.methods.state"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        assert written["waves"]["P24-I01-W09"]["status"] == "closed"
        assert written["actuals"]["P24-I01-W09"]["elapsed_eu"] == pytest.approx(0.0)
        assert "wave_close_runtime_zero" in caplog.text
        assert "mode='waived'" in caplog.text

    _run(body)


def test_close_allows_nonzero_runtime(tmp_path: Path) -> None:
    """A non-zero runtime delta closes cleanly and persists elapsed EU."""
    payload = _build_state_payload()
    _add_runtime_delta(payload)
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        assert written["waves"]["P24-I01-W09"]["status"] == "closed"
        assert written["actuals"]["P24-I01-W09"]["elapsed_eu"] == pytest.approx(
            12000 / (30 * 60_000)
        )

    _run(body)


def test_close_advisory_phase_warns_not_refuses(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A zero-runtime close warns but lands when active verify profile is advisory."""
    _write_verify_profile(tmp_path, enforce=False)
    payload = _build_state_payload()
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        with caplog.at_level("WARNING", logger="eawf.runtime.daemon.methods.state"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        assert written["waves"]["P24-I01-W09"]["status"] == "closed"
        assert written["actuals"]["P24-I01-W09"]["elapsed_eu"] == pytest.approx(0.0)
        assert "wave_close_runtime_zero" in caplog.text

    _run(body)


def test_wave_close_elapsed_eu_helper_boundaries() -> None:
    """``_wave_close_elapsed_eu`` converts rollup duration; None when absent."""
    from eawf.observability.telemetry.join import WaveSessionRollup
    from eawf.runtime.daemon.methods.state import _wave_close_elapsed_eu

    # No rollup -> no derived EU.
    assert _wave_close_elapsed_eu(None, eu_minutes=30.0) is None
    # Rollup with no captured duration -> no derived EU.
    no_duration = WaveSessionRollup(wave_id="P24-I01-W09", duration_ms=None)
    assert _wave_close_elapsed_eu(no_duration, eu_minutes=30.0) is None
    # 45 min of measured runtime -> 1.5 EU at 30 min/EU.
    measured = WaveSessionRollup(wave_id="P24-I01-W09", duration_ms=2_700_000)
    assert _wave_close_elapsed_eu(measured, eu_minutes=30.0) == pytest.approx(1.5)


def test_mutate_wave_close_enforced_readiness_rejects_before_write(tmp_path: Path) -> None:
    """Daemon path enforces real profile readiness before persisting close."""
    from eawf.runtime.daemon.methods import DaemonValidationError

    _write_verify_profile(tmp_path, enforce=True)
    ctx, state_path, event_path, _wal_dir = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="readiness enforcement failed"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"]["P24-I01-W09"]["status"] == "claimed"
        assert not event_path.exists() or not event_path.read_text(encoding="utf-8").strip()

    _run(body)


def test_mutate_wave_close_failing_readiness_without_enforce_is_advisory(
    tmp_path: Path,
) -> None:
    """Daemon path still closes when the active profile leaves enforce false."""
    _write_verify_profile(tmp_path, enforce=False)
    ctx, state_path, event_path, _wal_dir = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"]["P24-I01-W09"]["status"] == "closed"
        assert event_path.read_text(encoding="utf-8").strip()

    _run(body)


def test_mutate_phase_close_requires_close_audit_before_write(tmp_path: Path) -> None:
    """Daemon PHASE_CLOSE goes through phase-close readiness before persistence."""
    from eawf.runtime.daemon.methods import DaemonValidationError

    payload = _build_state_payload(wave_status="closed")
    payload["iters"]["P24-I01"]["status"] = "closed"  # type: ignore[index]
    payload["iters"]["P24-I01"]["closed_at"] = _now().isoformat()  # type: ignore[index]
    payload["iters"]["P24-I01"]["audit_id"] = "AUD-ITER"  # type: ignore[index]
    payload["waves"]["P24-I01-W09"]["claim_session_id"] = None  # type: ignore[index]
    payload["waves"]["P24-I01-W09"]["closed_at"] = _now().isoformat()  # type: ignore[index]
    payload["waves"]["P24-I01-W09"]["outcome"] = "ok"  # type: ignore[index]
    payload["iters"]["P24-I01"]["wave_ids"].append("P24-I01-W10")  # type: ignore[index]
    payload["waves"]["P24-I01-W10"] = {  # type: ignore[index]
        "id": "P24-I01-W10",
        "iter_id": "P24-I01",
        "title": "second wave",
        "status": "closed",
        "claim_session_id": None,
        "outcome": "ok",
        "opened_at": _now().isoformat(),
        "closed_at": _now().isoformat(),
        "sessions": {},
    }
    ctx, state_path, event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.PHASE_CLOSE,
        scope_id="P24",
        mutation_id=uuid.uuid4().hex,
        params={"phase_id": "P24", "audit_id": "AUD-PH-1"},
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="close audit 'AUD-PH-1' not found"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        assert written["phases"]["P24"]["status"] == "active"
        assert not event_path.exists() or not event_path.read_text(encoding="utf-8").strip()

    _run(body)


def test_mutate_wave_claim_publishes_wave_claimed_event_kind(tmp_path: Path) -> None:
    """WAVE_CLAIM event binds the validated session without fake auth."""
    payload = _build_state_payload(wave_status="pending")
    payload["waves"]["P24-I01-W09"]["effort_bucket"] = "M"  # type: ignore[index]
    payload["waves"]["P24-I01-W09"]["file_scopes"] = ["src/"]  # type: ignore[index]
    payload["agent_sessions"]["SES-2"] = {  # type: ignore[index]
        "id": "SES-2",
        "role": "executor",
        "runtime": "test",
        "scope_id": "P24-I01-W09",
        "status": "active",
        "started_at": _now().isoformat(),
    }
    payload["current"]["active_session_ids"] = ["SES-2"]  # type: ignore[index]
    ctx, _state_path, event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    bus = ctx.bus
    assert isinstance(bus, EventBus)
    sub = bus.register(connection_id="claim-sub")
    mutation = Mutation(
        kind=MutationKind.WAVE_CLAIM,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={
            "wave_id": "P24-I01-W09",
            "session_id": "SES-2",
            "out_of_order": False,
        },
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        envelope_payload = result["event"]["payload"]
        assert envelope_payload["event_kind"] == "wave_claimed"
        assert envelope_payload["actor"] == "daemon"
        assert envelope_payload["extras"] == {"claim_session_id": "SES-2"}
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1
        on_disk = orjson.loads(rows[0])
        assert on_disk["payload"]["event_kind"] == "wave_claimed"
        assert on_disk["payload"]["actor"] == "daemon"
        assert on_disk["payload"]["extras"] == {"claim_session_id": "SES-2"}
        assert len(sub.queue) == 1
        assert sub.queue[0].payload["event_kind"] == "wave_claimed"
        assert sub.queue[0].payload["actor"] == "daemon"
        assert sub.queue[0].payload["extras"] == {"claim_session_id": "SES-2"}

    _run(body)


def test_mutate_wave_claim_missing_session_preserves_state_and_event_store(
    tmp_path: Path,
) -> None:
    """Advanced daemon claim exposes the stable code and writes nothing."""
    payload = _build_state_payload(wave_status="pending")
    payload["waves"]["P24-I01-W09"]["effort_bucket"] = "M"  # type: ignore[index]
    payload["waves"]["P24-I01-W09"]["file_scopes"] = ["src/"]  # type: ignore[index]
    ctx, state_path, event_path, _wal_dir = _build_ctx(
        tmp_path=tmp_path,
        state_payload=payload,
    )
    before = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.WAVE_CLAIM,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={
            "wave_id": "P24-I01-W09",
            "session_id": "SES-missing",
            "out_of_order": False,
        },
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="claim_session_not_found"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert state_path.read_bytes() == before
        assert not event_path.exists() or not event_path.read_bytes()

    _run(body)


def test_mutate_wave_claim_resolves_configured_cap_under_lock(tmp_path: Path) -> None:
    """Daemon claim counts live statuses and ignores stale pointers/caller cap hints."""
    payload = _build_state_payload(wave_status="pending")
    candidate = payload["waves"]["P24-I01-W09"]  # type: ignore[index]
    candidate["effort_bucket"] = "M"  # type: ignore[index]
    candidate["file_scopes"] = ["src/"]  # type: ignore[index]
    occupied = dict(candidate)  # type: ignore[arg-type]
    occupied.update(
        {
            "id": "P24-I01-W08",
            "title": "occupied lane",
            "status": "in_progress",
            "claim_session_id": "SES-active",
            "claimed_at": _now().isoformat(),
        }
    )
    payload["waves"]["P24-I01-W08"] = occupied  # type: ignore[index]
    payload["iters"]["P24-I01"]["wave_ids"] = [  # type: ignore[index]
        "P24-I01-W08",
        "P24-I01-W09",
    ]
    payload["current"]["active_wave_ids"] = []  # type: ignore[index]
    payload["agent_sessions"].update(  # type: ignore[union-attr]
        {
            "SES-active": {
                "id": "SES-active",
                "role": "executor",
                "runtime": "test",
                "scope_id": "P24-I01-W08",
                "status": "active",
                "claimed_wave_ids": ["P24-I01-W08"],
                "started_at": _now().isoformat(),
            },
            "SES-2": {
                "id": "SES-2",
                "role": "executor",
                "runtime": "test",
                "scope_id": "P24-I01-W09",
                "status": "active",
                "started_at": _now().isoformat(),
            },
        }
    )
    payload["current"]["active_session_ids"] = ["SES-active", "SES-2"]  # type: ignore[index]
    ctx, state_path, event_path, _wal_dir = _build_ctx(
        tmp_path=tmp_path,
        state_payload=payload,
    )
    (tmp_path / ".ea").mkdir(exist_ok=True)
    (tmp_path / ".ea" / "config.yaml").write_text(
        "planning:\n  max_parallel_waves: 1\n",
        encoding="utf-8",
    )
    before = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.WAVE_CLAIM,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={
            "wave_id": "P24-I01-W09",
            "session_id": "SES-2",
            "out_of_order": True,
            "max_parallel_waves": 99,
        },
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="claim_parallel_limit_reached"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert state_path.read_bytes() == before
        assert not event_path.exists() or not event_path.read_bytes()

    _run(body)


def test_mutate_wave_claim_disabled_rejects_historical_floor_waiver(
    tmp_path: Path,
) -> None:
    """Daemon claim derives disabled config and leaves every store untouched."""
    payload = _build_state_payload(wave_status="pending")
    wave = payload["waves"]["P24-I01-W09"]  # type: ignore[index]
    wave["effort_bucket"] = "M"  # type: ignore[index]
    wave["file_scopes"] = ["src/"]  # type: ignore[index]
    wave["success_criteria"] = [  # type: ignore[index]
        grandfather_criterion("historical criterion", index=1).model_dump(mode="json")
    ]
    wave["criteria_floor_waiver"] = CriteriaFloorWaiver(  # type: ignore[index]
        reason="historical typed-criteria repair waiver",
        waived_at=_now(),
    ).model_dump(mode="json")
    payload["agent_sessions"]["SES-2"] = {  # type: ignore[index]
        "id": "SES-2",
        "role": "executor",
        "runtime": "test",
        "scope_id": "P24-I01-W09",
        "status": "active",
        "started_at": _now().isoformat(),
    }
    payload["current"]["active_session_ids"] = ["SES-2"]  # type: ignore[index]
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    _write_disabled_waiver_config(tmp_path)
    before = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.WAVE_CLAIM,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={
            "wave_id": "P24-I01-W09",
            "session_id": "SES-2",
            "out_of_order": False,
            "waiver_mode": "A",
        },
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="waiver_mode_disabled"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert state_path.read_bytes() == before
        assert not event_path.exists() or not event_path.read_bytes()
        assert not list(wal_dir.glob("*.json"))

    _run(body)


def test_daemon_wave_event_kind_table_has_no_w19_orphans() -> None:
    """W19 mapped wave lifecycle kinds are canonical EventKind literals."""
    from eawf.runtime.daemon.methods.state import _MUTATION_EVENT_KIND

    mapped = set(_MUTATION_EVENT_KIND.values())
    assert {"wave_claimed", "wave_closed"} <= mapped
    assert mapped <= set(get_args(EventKind))


def test_mutate_unmapped_event_append_omits_event_kind(tmp_path: Path) -> None:
    """Mutation kinds not in _MUTATION_EVENT_KIND emit ``event_kind=None``.

    P28-I02-W19 wires WAVE_CLAIM + WAVE_CLOSE; other kinds land with the
    discriminator left ``None`` during the v0.3-v0.5 migration window so
    on-disk rows pre-dating the typed event-kind era stay valid.
    """
    ctx, _state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.EVENT_APPEND,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"event_type": "note.recorded", "message": "hi"},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        envelope_payload = result["event"]["payload"]
        assert envelope_payload["event_kind"] is None

    _run(body)


def test_mutate_last_event_id_updated(tmp_path: Path) -> None:
    """``ctx.last_event_id`` reflects the most recent envelope id."""
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)
    assert ctx.last_event_id == ""
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert ctx.last_event_id == result["event"]["id"]

    _run(body)


# ---- state.mutate (idempotency) ---------------------------------------------


def test_mutate_idempotency_returns_cached_replay(tmp_path: Path) -> None:
    """Repeat call with the same idempotency_key returns the cached result."""
    ctx, _state_path, event_path, _wal_dir = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        idempotency_key="key-1",
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        first: dict[str, Any] = await mutate(
            ctx, {"mutation": mutation.model_dump(mode="json"), "idempotency_key": "key-1"}
        )
        # Build a SECOND mutation with the same key + new mutation_id.
        mutation_two = Mutation(
            kind=MutationKind.WAVE_CLOSE,
            scope_id="P24-I01-W09",
            mutation_id=uuid.uuid4().hex,
            idempotency_key="key-1",
            params={"wave_id": "P24-I01-W09", "outcome": "ok"},
        )
        second: dict[str, Any] = await mutate(
            ctx, {"mutation": mutation_two.model_dump(mode="json"), "idempotency_key": "key-1"}
        )
        assert first["idempotent_replay"] is False
        assert second["idempotent_replay"] is True
        assert first["event"]["id"] == second["event"]["id"]

        # event.jsonl still carries exactly one row (replay did not re-append).
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1

    _run(body)


# ---- state.mutate (validation rejection) -----------------------------------


def test_mutate_unknown_wave_id_rejected(tmp_path: Path) -> None:
    """An unknown wave id raises ``validation_failed``; state.json unchanged."""
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)
    before_bytes = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W99",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W99", "outcome": "nope"},
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        # state.json untouched.
        assert state_path.read_bytes() == before_bytes
        # No event row appended.
        assert not event_path.exists() or event_path.read_text() == ""
        # The WAL pending record SHOULD also be absent — the rejection
        # short-circuited BEFORE write_pending, so no file lingers.
        assert list(wal_dir.glob("*.pending.json")) == []

    _run(body)


def test_mutate_missing_required_param_rejected(tmp_path: Path) -> None:
    """An apply that needs ``outcome`` rejects when the key is missing."""
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09"},  # outcome missing
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})

    _run(body)


# ---- state.mutate (JSON-RPC wire contract) ---------------------------------
# The handler-level tests above catch the raw ``DaemonValidationError``;
# these drive :func:`eawf.runtime.daemon.server._process_frame` so the on-the-wire
# error CODE is exercised. A lifecycle/post-invariant rejection MUST emit
# ``-32002`` (so the CLI client maps it to ValidationFailed / exit 2), not
# the generic ``-32602 invalid_params`` a bare ValueError would yield.


def _mutate_frame(mutation: Mutation) -> bytes:
    """Serialise a ``state.mutate`` JSON-RPC request frame for *mutation*."""
    request = {
        "jsonrpc": "2.0",
        "id": "wire-1",
        "method": "state.mutate",
        "params": {"mutation": mutation.model_dump(mode="json")},
    }
    return orjson.dumps(request)


def test_process_frame_validation_rejection_emits_minus_32002(tmp_path: Path) -> None:
    """A rejected mutation surfaces as JSON-RPC ``-32002`` on the wire.

    Wire-contract regression: before W31 the lifecycle-guard rejection
    left the handler as a bare ``ValueError`` that ``_process_frame``
    mapped to ``-32602 invalid_params``, so the CLI client's
    ``-32002`` ValidationFailed branch was dead and the daemon path
    returned a different exit code than the in-process fallback for the
    SAME rejection.
    """
    from eawf.runtime.daemon.methods import VALIDATION_FAILED as WIRE_VALIDATION_FAILED
    from eawf.runtime.daemon.server import _process_frame

    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W99",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W99", "outcome": "nope"},  # unknown wave
    )

    async def body() -> None:
        response = await _process_frame(_mutate_frame(mutation), ctx)
        assert response["id"] == "wire-1"
        assert "result" not in response
        assert response["error"]["code"] == WIRE_VALIDATION_FAILED
        assert response["error"]["code"] == -32002
        assert "validation_failed" in response["error"]["message"]

    _run(body)


def test_process_frame_nonclosure_lifecycle_rejection_emits_minus_32602(tmp_path: Path) -> None:
    """A NON-closure lifecycle-guard rejection surfaces as ``-32602``.

    ROADMAP_APPLY on an ACTIVE (not PLANNED) phase is a lifecycle-guard
    rejection, but ROADMAP_APPLY is not a closure (``*_CLOSE``) kind, so the
    daemon emits ``-32602 invalid_params`` (the client maps it to
    InvalidInput / exit 1) rather than ``-32002``. This keeps the daemon-up
    path and the in-process fallback (which maps a non-closure
    ``LifecycleError`` to InvalidInput) agreeing on the exit code for the
    same rejection. Only the ``*_CLOSE`` kinds surface ``-32002``
    (see ``test_process_frame_validation_rejection_emits_minus_32002``).
    """
    from eawf.runtime.daemon.server import INVALID_PARAMS, _process_frame

    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.ROADMAP_APPLY,
        scope_id="P24",
        mutation_id=uuid.uuid4().hex,
        params={"phase_id": "P24"},  # P24 is ACTIVE, not PLANNED → rejected
    )

    async def body() -> None:
        response = await _process_frame(_mutate_frame(mutation), ctx)
        assert response["error"]["code"] == INVALID_PARAMS
        assert response["error"]["code"] == -32602

    _run(body)


def test_process_frame_corrupt_state_stays_minus_32602(tmp_path: Path) -> None:
    """On-disk corruption stays ``-32602`` — NOT the mutation-rejection code.

    Reading a schema-invalid ``state.json`` raises a *bare* ``ValueError``
    (on-disk corruption, not a rejected mutation), so the wire code must
    remain ``-32602 invalid_params`` — distinct from the ``-32002`` a
    typed :class:`DaemonValidationError` yields. Guards against the
    refactor over-broadening the typed exception.
    """
    from eawf.runtime.daemon.server import INVALID_PARAMS, _process_frame

    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path)
    state_path.write_text('{"schema_version": "1.0", "not": "valid"}')
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        response = await _process_frame(_mutate_frame(mutation), ctx)
        assert response["error"]["code"] == INVALID_PARAMS
        assert response["error"]["code"] == -32602

    _run(body)


def test_apply_registry_has_no_stub_kinds() -> None:
    """Every MutationKind resolves to a real apply fn (no not-yet-wired stub)."""
    from eawf.runtime.daemon.methods import state as state_methods

    # The not-yet-wired stub was removed once every kind was wired; assert
    # it is gone so a regression that re-introduces it fails loudly.
    assert not hasattr(state_methods, "_apply_not_yet_wired")
    registry = state_methods._APPLY_REGISTRY
    # All 13 enum members are registered, and each resolves to a callable.
    assert set(registry) == set(MutationKind)
    for kind in MutationKind:
        func = state_methods._resolve_apply(kind)
        assert callable(func)


# ---- state.mutate (newly-wired kinds: DECISION_OBSOLETE) -------------------


def test_mutate_decision_obsolete_marks_active_decision(tmp_path: Path) -> None:
    """DECISION_OBSOLETE flips an active decision and stamps ``obsoleted_at``."""
    payload = _build_state_payload()
    obsolete_at = datetime(2026, 5, 27, 12, 30, 0, tzinfo=UTC)
    payload["decisions"] = {"D010": _decision_payload("D010")}  # type: ignore[index]
    ctx, state_path, event_path, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.DECISION_OBSOLETE,
        scope_id="D010",
        mutation_id=uuid.uuid4().hex,
        params={"id": "D010", "obsoleted_at": obsolete_at.isoformat()},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert result["before_version"] != result["after_version"]
        new_state = orjson.loads(state_path.read_bytes())
        decision = new_state["decisions"]["D010"]
        assert decision["status"] == DecisionStatus.OBSOLETE.value
        assert datetime.fromisoformat(decision["obsoleted_at"]) == obsolete_at
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1

    _run(body)


def test_mutate_decision_obsolete_rejects_superseded_decision(tmp_path: Path) -> None:
    """DECISION_OBSOLETE preserves supersede-link semantics."""
    payload = _build_state_payload()
    payload["decisions"] = {
        "D010": _decision_payload("D010", status="superseded", superseded_by="D011"),
        "D011": _decision_payload("D011"),
    }  # type: ignore[index]
    ctx, state_path, event_path, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    before_bytes = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.DECISION_OBSOLETE,
        scope_id="D010",
        mutation_id=uuid.uuid4().hex,
        params={"id": "D010", "obsoleted_at": _now().isoformat()},
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="cannot obsolete decision in status"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert state_path.read_bytes() == before_bytes
        assert not event_path.exists() or event_path.read_text() == ""

    _run(body)


# ---- state.mutate (newly-wired kinds: WAVE_RELEASE) ------------------------


def test_mutate_wave_release_returns_wave_to_pending(tmp_path: Path) -> None:
    """WAVE_RELEASE un-claims a claimed wave back to pending + clears the claim."""
    ctx, state_path, event_path, _ = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_RELEASE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "reason": "runtime swap"},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert result["before_version"] != result["after_version"]
        new_state = orjson.loads(state_path.read_bytes())
        wave = new_state["waves"]["P24-I01-W09"]
        assert wave["status"] == "pending"
        assert wave["claim_session_id"] is None
        # Event row appended for the release.
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1

    _run(body)


def test_mutate_wave_release_unknown_wave_rejected(tmp_path: Path) -> None:
    """WAVE_RELEASE on an unknown wave id raises validation_failed; state intact."""
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path)
    before_bytes = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.WAVE_RELEASE,
        scope_id="P24-I01-W99",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W99"},
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown wave"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert state_path.read_bytes() == before_bytes

    _run(body)


# ---- state.mutate (newly-wired kinds: EVENT_APPEND) ------------------------


def test_mutate_event_append_writes_event_no_state_change(tmp_path: Path) -> None:
    """EVENT_APPEND appends an event row without a structural state change."""
    ctx, state_path, event_path, _ = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.EVENT_APPEND,
        scope_id="P24",
        mutation_id=uuid.uuid4().hex,
        params={"event_type": "note.recorded", "message": "manual audit row"},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        # Exactly one event row appended.
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1
        envelope = result["event"]
        assert envelope["scope_id"] == "P24"
        # No structural change: the wave keeps its pre-append status +
        # claim binding and the phase keeps its status (the apply is a
        # deliberate no-op on State beyond the updated_at bump). Comparing
        # the on-disk dict to the raw seed is unsafe because the State
        # round-trip canonicalises default fields, so assert the
        # load-bearing fields instead.
        after = orjson.loads(state_path.read_bytes())
        wave = after["waves"]["P24-I01-W09"]
        assert wave["status"] == "claimed"
        assert wave["claim_session_id"] == "session-abc"
        assert after["phases"]["P24"]["status"] == "active"
        assert list(after["waves"]) == ["P24-I01-W09"]

    _run(body)


def test_mutate_event_append_missing_event_type_rejected(tmp_path: Path) -> None:
    """EVENT_APPEND without a non-empty event_type raises validation_failed."""
    ctx, state_path, event_path, _ = _build_ctx(tmp_path=tmp_path)
    before_bytes = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.EVENT_APPEND,
        scope_id="P24",
        mutation_id=uuid.uuid4().hex,
        params={"event_type": "   "},  # blank-only
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="event_type"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        # No state change + no event row from the rejected append.
        assert state_path.read_bytes() == before_bytes
        assert not event_path.exists() or event_path.read_text() == ""

    _run(body)


# ---- state.mutate (newly-wired kinds: ROADMAP_REVISE) ----------------------


def test_mutate_roadmap_revise_add_wave_inserts_pending_wave(tmp_path: Path) -> None:
    """ROADMAP_REVISE op=add_wave plans a new PENDING wave under the iter."""
    payload = _build_planned_phase_payload()
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={
            "op": "add_wave",
            "wave_id": "P50-I01-W01",
            "iter_id": "P50-I01",
            "title": "New wave",
            "file_scopes": ["src/eawf/x.py"],
            "success_criteria": ["does x"],
            "criteria_floor_waiver_reason": "test fixture: legacy success strings under test",
            "effort_bucket": "S",
            "intent": {
                "problem": "x is unhandled",
                "desired_outcome": "x is handled",
                "priority_rationale": "handle x before the dependents land",
            },
        },
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        new_state = orjson.loads(state_path.read_bytes())
        assert "P50-I01-W01" in new_state["waves"]
        assert new_state["waves"]["P50-I01-W01"]["status"] == "pending"
        assert "P50-I01-W01" in new_state["iters"]["P50-I01"]["wave_ids"]
        # The typed-criteria floor waiver persists visibly on the wave row.
        waiver = new_state["waves"]["P50-I01-W01"]["criteria_floor_waiver"]
        assert waiver is not None and "test fixture" in waiver["reason"]

    _run(body)


def test_mutate_roadmap_revise_disabled_overrides_caller_mode_before_write(
    tmp_path: Path,
) -> None:
    """Daemon derives disabled policy; mutation params cannot weaken it."""
    payload = _build_planned_phase_payload()
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    _write_disabled_waiver_config(tmp_path)
    before = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={
            "op": "add_wave",
            "wave_id": "P50-I01-W01",
            "iter_id": "P50-I01",
            "title": "New wave",
            "file_scopes": ["src/eawf/x.py"],
            "success_criteria": ["does x"],
            "criteria_floor_waiver_reason": "caller tries to weaken strict policy",
            "waiver_mode": "A",
            "effort_bucket": "S",
            "intent": {
                "problem": "x is unhandled",
                "desired_outcome": "x is handled",
                "priority_rationale": "handle x before dependents land",
            },
        },
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="waiver_mode_disabled"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert state_path.read_bytes() == before
        assert not event_path.exists() or not event_path.read_bytes()
        assert not list(wal_dir.glob("*.json"))

    _run(body)


def test_mutate_roadmap_revise_remove_wave_drops_pending_wave(tmp_path: Path) -> None:
    """ROADMAP_REVISE op=remove_wave deletes a PENDING wave from the plan."""
    waves = {"P50-I01-W01": _pending_wave("P50-I01-W01", "P50-I01")}
    payload = _build_planned_phase_payload(waves=waves)
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={"op": "remove_wave", "wave_id": "P50-I01-W01"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        new_state = orjson.loads(state_path.read_bytes())
        assert "P50-I01-W01" not in new_state["waves"]

    _run(body)


def test_mutate_roadmap_revise_set_deps_rewrites_deps(tmp_path: Path) -> None:
    """ROADMAP_REVISE op=set_deps replaces a PENDING wave's dep set."""
    waves = {
        "P50-I01-W01": _pending_wave("P50-I01-W01", "P50-I01"),
        "P50-I01-W02": _pending_wave("P50-I01-W02", "P50-I01"),
    }
    payload = _build_planned_phase_payload(waves=waves)
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={"op": "set_deps", "wave_id": "P50-I01-W02", "deps": ["P50-I01-W01"]},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        new_state = orjson.loads(state_path.read_bytes())
        assert new_state["waves"]["P50-I01-W02"]["deps"] == ["P50-I01-W01"]
        assert "P50-I01-W02" in new_state["waves"]["P50-I01-W01"]["blocks"]

    _run(body)


def test_mutate_roadmap_revise_retitle_rewrites_title(tmp_path: Path) -> None:
    """ROADMAP_REVISE op=retitle rewrites a PENDING wave's title."""
    waves = {"P50-I01-W01": _pending_wave("P50-I01-W01", "P50-I01")}
    payload = _build_planned_phase_payload(waves=waves)
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={"op": "retitle", "wave_id": "P50-I01-W01", "title": "Retitled wave"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        new_state = orjson.loads(state_path.read_bytes())
        assert new_state["waves"]["P50-I01-W01"]["title"] == "Retitled wave"

    _run(body)


def test_mutate_roadmap_revise_retitle_iter_routes_to_iter(tmp_path: Path) -> None:
    """ROADMAP_REVISE op=retitle with an iter_id rewrites the iter title."""
    payload = _build_planned_phase_payload()
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={"op": "retitle", "iter_id": "P50-I01", "title": "TUI richer views"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        new_state = orjson.loads(state_path.read_bytes())
        assert new_state["iters"]["P50-I01"]["title"] == "TUI richer views"

    _run(body)


def test_mutate_roadmap_revise_unknown_op_rejected(tmp_path: Path) -> None:
    """ROADMAP_REVISE with an unknown op raises validation_failed; state intact."""
    payload = _build_planned_phase_payload()
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    before_bytes = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={"op": "frobnicate", "wave_id": "P50-I01-W01"},
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown roadmap revise op"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert state_path.read_bytes() == before_bytes

    _run(body)


# ---- state.mutate (newly-wired kinds: ROADMAP_APPLY) -----------------------


def test_mutate_roadmap_apply_passes_for_planned_phase_with_waves(tmp_path: Path) -> None:
    """ROADMAP_APPLY succeeds for a PLANNED phase with ≥1 wave; state unchanged."""
    waves = {"P50-I01-W01": _pending_wave("P50-I01-W01", "P50-I01")}
    payload = _build_planned_phase_payload(waves=waves)
    ctx, state_path, event_path, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.ROADMAP_APPLY,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={"phase_id": "P50"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        # apply is informational: phase stays PLANNED, the wave stays
        # PENDING (no structural edit beyond the updated_at bump).
        after = orjson.loads(state_path.read_bytes())
        assert after["phases"]["P50"]["status"] == "planned"
        assert list(after["waves"]) == ["P50-I01-W01"]
        assert after["waves"]["P50-I01-W01"]["status"] == "pending"
        # An audit event row still lands.
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1

    _run(body)


def test_mutate_roadmap_apply_no_waves_rejected(tmp_path: Path) -> None:
    """ROADMAP_APPLY on a PLANNED phase with zero waves raises validation_failed."""
    payload = _build_planned_phase_payload()  # no waves seeded
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    before_bytes = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.ROADMAP_APPLY,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={"phase_id": "P50"},
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="has no waves"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert state_path.read_bytes() == before_bytes

    _run(body)


# ---- state.mutate (newly-wired kinds: ROADMAP_DROP) ------------------------


def test_mutate_roadmap_drop_archives_planned_phase(tmp_path: Path) -> None:
    """ROADMAP_DROP archives a PLANNED phase + abandons its child waves."""
    waves = {"P50-I01-W01": _pending_wave("P50-I01-W01", "P50-I01")}
    payload = _build_planned_phase_payload(waves=waves)
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.ROADMAP_DROP,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={"phase_id": "P50"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        new_state = orjson.loads(state_path.read_bytes())
        assert new_state["phases"]["P50"]["status"] == "archived"
        assert new_state["iters"]["P50-I01"]["status"] == "abandoned"
        assert new_state["waves"]["P50-I01-W01"]["status"] == "abandoned"

    _run(body)


def test_mutate_roadmap_drop_unknown_phase_rejected(tmp_path: Path) -> None:
    """ROADMAP_DROP on an unknown phase id raises validation_failed; state intact."""
    payload = _build_planned_phase_payload()
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    before_bytes = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.ROADMAP_DROP,
        scope_id="P99",
        mutation_id=uuid.uuid4().hex,
        params={"phase_id": "P99"},
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown phase"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        assert state_path.read_bytes() == before_bytes

    _run(body)


# ---- state.mutate (WAL replay) ---------------------------------------------


def test_replay_wal_applied_record_is_promoted_to_fsynced(tmp_path: Path) -> None:
    """An ``.applied.json`` record left behind on restart is replayed to fsynced.

    Simulates the crash window between :func:`wal.mark_applied` and
    :func:`wal.mark_fsynced`: the event row is already on disk, but the
    WAL still says ``.applied``. Recovery should rename to
    ``.fsynced`` without re-appending the envelope.
    """
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)

    # Run a successful mutate so we have a fsynced WAL record + event row.
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id="record-1",
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        # Roll the WAL state back to ``.applied`` to simulate a crash
        # between mark_applied + mark_fsynced.
        fsynced = list(wal_dir.glob("*.fsynced.json"))
        assert len(fsynced) == 1
        os.replace(fsynced[0], wal_dir / fsynced[0].name.replace(".fsynced.", ".applied."))

        # Now run recovery — should detect the existing event row and
        # promote applied → fsynced without re-appending.
        rows_before = event_path.read_text().strip().splitlines()
        report = recovery.replay_wal(wal_dir, state_path, event_path)
        rows_after = event_path.read_text().strip().splitlines()

        assert report.applied_count == 1
        assert report.replayed_event_count == 0  # envelope already present
        assert rows_before == rows_after
        assert list(wal_dir.glob("*.applied.json")) == []
        assert len(list(wal_dir.glob("*.fsynced.json"))) == 1

    _run(body)


def test_replay_wal_applied_record_replays_missing_event(tmp_path: Path) -> None:
    """An ``.applied.json`` whose envelope is NOT in event.jsonl is replayed.

    Simulates the crash window between :func:`atomic_write_json_locked`
    (state.json fsynced) and :func:`append_envelope` (event.jsonl).
    Recovery appends the envelope from the WAL record to keep state +
    event consistent.
    """
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id="record-2",
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        # Manually:
        # 1. Truncate event.jsonl.
        # 2. Roll the WAL back to ``.applied``.
        # This simulates the crash between state-write and event-append.
        event_path.write_text("")
        fsynced = list(wal_dir.glob("*.fsynced.json"))
        assert len(fsynced) == 1
        os.replace(fsynced[0], wal_dir / fsynced[0].name.replace(".fsynced.", ".applied."))

        report = recovery.replay_wal(wal_dir, state_path, event_path)
        assert report.applied_count == 1
        assert report.replayed_event_count == 1
        # The envelope from the WAL record landed in event.jsonl.
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1
        env = orjson.loads(rows[0])
        assert env["kind"] == StoreKind.EVENT.value
        assert env["scope_id"] == "P24-I01-W09"
        # Record promoted to fsynced.
        assert len(list(wal_dir.glob("*.fsynced.json"))) == 1

    _run(body)


def test_replay_wal_pending_is_poisoned(tmp_path: Path) -> None:
    """A leftover ``.pending.json`` is moved to ``poisoned/`` (never re-run).

    Crash before state.json was written; the mutator outcome was never
    observed, so replay must NOT execute the mutator (the outcome-WAL
    by design captures POST-apply envelopes, so a pending record means
    "apply was attempted but state was not written").
    """
    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)

    # Write a synthetic pending record (we don't have a fully-built
    # WalRecord on hand without running mutate; use the wal helper).
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id="record-3",
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        # Run mutate then roll the wal back to .pending.
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        fsynced = list(wal_dir.glob("*.fsynced.json"))
        os.replace(fsynced[0], wal_dir / fsynced[0].name.replace(".fsynced.", ".pending."))
        report = recovery.replay_wal(wal_dir, state_path, event_path)
        assert report.pending_count == 1
        assert (wal_dir / "poisoned").exists()
        poisoned = list((wal_dir / "poisoned").glob("*.poisoned.json"))
        assert len(poisoned) == 1

    _run(body)


# ---- state.mutate (WAL mark_applied ordering — S7) --------------------------
# A crash AFTER the state write but BEFORE the event append must leave an
# APPLIED record (not a PENDING one) so replay_wal re-issues the event row
# instead of poisoning it. mark_applied is reordered to fire immediately
# after the durable state write, before append_envelope.


def test_mutate_crash_after_state_write_leaves_applied_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the state write and the event append leaves ``.applied``.

    Regression for the S7 ordering bug: with ``mark_applied`` AFTER the
    event append, this crash window left a ``.pending`` record that
    ``replay_wal`` POISONS — silently losing the event row and diverging
    state from the event log. With ``mark_applied`` moved before the
    append, the crash leaves an ``.applied`` record that replay re-issues.
    """
    from eawf.runtime.daemon.methods import state as state_methods

    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)
    before_bytes = state_path.read_bytes()

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash between state-write and event-append")

    monkeypatch.setattr(state_methods, "append_envelope", _boom)

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id="crash-1",
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        # The simulated crash propagates out of mutate (OSError is not a
        # validation rejection — it surfaces as an internal error on the
        # wire). The point is the on-disk WAL/state state it leaves.
        with pytest.raises(OSError, match="simulated crash"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})

        # state.json WAS written (the crash is after the durable write).
        assert state_path.read_bytes() != before_bytes
        # The WAL record is APPLIED — not PENDING — because mark_applied
        # ran before the (failing) event append.
        assert list(wal_dir.glob("*.pending.json")) == []
        applied = list(wal_dir.glob("*.applied.json"))
        assert len(applied) == 1
        # The event row never landed (the append blew up).
        assert not event_path.exists() or event_path.read_text().strip() == ""

    _run(body)


def test_replay_after_crash_reissues_event_for_applied_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end S7: crash → restart → replay re-issues the missing event.

    Drives the full recovery: a mutation crashes after the state write
    (event append fails), leaving an ``.applied`` record. On the next
    startup ``replay_wal`` re-issues the captured envelope so the event
    log matches the already-committed state.
    """
    from eawf.runtime.daemon.methods import state as state_methods

    ctx, state_path, event_path, wal_dir = _build_ctx(tmp_path=tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash between state-write and event-append")

    monkeypatch.setattr(state_methods, "append_envelope", _boom)

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id="crash-2",
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        with pytest.raises(OSError, match="simulated crash"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})

        # Restore the real append for the replay path.
        monkeypatch.undo()

        # Replay (uses the real append_envelope again) re-issues the event.
        report = recovery.replay_wal(wal_dir, state_path, event_path)
        assert report.applied_count == 1
        assert report.replayed_event_count == 1
        assert report.poisoned_count == 0
        rows = event_path.read_text().strip().splitlines()
        assert len(rows) == 1
        env = orjson.loads(rows[0])
        assert env["kind"] == StoreKind.EVENT.value
        assert env["scope_id"] == "P24-I01-W09"
        # The record is now fsynced (replay completed it).
        assert len(list(wal_dir.glob("*.fsynced.json"))) == 1

    _run(body)


# ---- state.mutate (param shape errors) -------------------------------------


def test_mutate_rejects_missing_state_path(tmp_path: Path) -> None:
    """Daemon misconfiguration (no state_path) raises RuntimeError."""
    ctx = MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
    )
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        # P26-W03: ``_resolve_mutator_paths`` raises with the
        # ``state_path not configured`` phrasing when neither the
        # per-request ``repo_root`` param nor ``ctx.state_path`` is
        # available — same fail-fast contract, sharper message.
        with pytest.raises(RuntimeError, match="state_path not configured"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})

    _run(body)


def test_mutate_rejects_unknown_param_field(tmp_path: Path) -> None:
    """An unknown top-level params field is rejected by the schema."""
    ctx, _, _, _ = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await mutate(
                ctx,
                {
                    "mutation": mutation.model_dump(mode="json"),
                    "unexpected": "field",
                },
            )

    _run(body)


# ---- WAL record schema sanity ----------------------------------------------


def test_mutate_wal_record_carries_post_apply_envelope(tmp_path: Path) -> None:
    """The WAL pending → applied → fsynced lifecycle captures the canonical envelope."""
    ctx, _, _, wal_dir = _build_ctx(tmp_path=tmp_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id="record-x",
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        fsynced = list(wal.list_records(wal_dir, status=WalStatus.FSYNCED))
        assert len(fsynced) == 1
        record = wal.read_record(fsynced[0])
        assert record.record_id == "record-x"
        assert record.envelope.id == result["event"]["id"]
        assert record.before_state_version == result["before_version"]
        assert record.after_state_version == result["after_version"]

    _run(body)


# ---- state.mutate (description round-trip P28-W02) -------------------------
#
# Description is an existing model field (≤500 char bound) that was not
# previously surfaced through the daemon's apply functions. These tests
# pin the wire from CLI -> Mutation.params['description'] -> daemon ->
# state.json -> read-back.


def test_mutate_iter_open_carries_description(tmp_path: Path) -> None:
    """ITER_OPEN routes the description param onto Iter.description."""
    payload = _build_state_payload()
    # Drop the wave so close_iter can later run; for this test we only
    # need iter_open to land a fresh iter. To make a fresh iter id we
    # reuse a planned phase fixture instead.
    payload = _build_planned_phase_payload()
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.ITER_OPEN,
        scope_id="P50-I02",
        mutation_id=uuid.uuid4().hex,
        params={
            "iter_id": "P50-I02",
            "phase_id": "P50",
            "title": "i02 active",
            "description": "second iter narrative for the planning phase",
        },
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        new_state = orjson.loads(state_path.read_bytes())
        iter_row = new_state["iters"]["P50-I02"]
        assert iter_row["description"] == "second iter narrative for the planning phase"

    _run(body)


def test_mutate_iter_open_description_optional(tmp_path: Path) -> None:
    """Omitting description leaves Iter.description == None."""
    payload = _build_planned_phase_payload()
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.ITER_OPEN,
        scope_id="P50-I02",
        mutation_id=uuid.uuid4().hex,
        params={
            "iter_id": "P50-I02",
            "phase_id": "P50",
            "title": "no desc iter",
        },
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        new_state = orjson.loads(state_path.read_bytes())
        assert new_state["iters"]["P50-I02"]["description"] is None

    _run(body)


def test_mutate_phase_open_carries_description(tmp_path: Path) -> None:
    """PHASE_OPEN routes the description param onto Phase.description."""
    payload = _build_planned_phase_payload()
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.PHASE_OPEN,
        scope_id="P51",
        mutation_id=uuid.uuid4().hex,
        params={
            "phase_id": "P51",
            "title": "Plan import",
            "description": "fresh phase narrative for the operator surface",
        },
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        new_state = orjson.loads(state_path.read_bytes())
        phase_row = new_state["phases"]["P51"]
        assert phase_row["description"] == "fresh phase narrative for the operator surface"

    _run(body)


def test_mutate_roadmap_revise_add_wave_carries_description(tmp_path: Path) -> None:
    """ROADMAP_REVISE op=add_wave routes description onto Wave.description."""
    payload = _build_planned_phase_payload()
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={
            "op": "add_wave",
            "wave_id": "P50-I01-W01",
            "iter_id": "P50-I01",
            "title": "New wave",
            "file_scopes": ["src/eawf/x.py"],
            "description": "long-form rationale for what this wave does",
            "effort_bucket": "S",
            "intent": {
                "problem": "x is unhandled",
                "desired_outcome": "x is handled",
                "priority_rationale": "handle x before the dependents land",
            },
        },
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        new_state = orjson.loads(state_path.read_bytes())
        wave_row = new_state["waves"]["P50-I01-W01"]
        assert wave_row["description"] == "long-form rationale for what this wave does"

    _run(body)


def test_mutate_roadmap_revise_retitle_wave_carries_description(tmp_path: Path) -> None:
    """ROADMAP_REVISE op=retitle on a wave routes description onto Wave.description."""
    waves = {"P50-I01-W01": _pending_wave("P50-I01-W01", "P50-I01")}
    payload = _build_planned_phase_payload(waves=waves)
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={
            "op": "retitle",
            "wave_id": "P50-I01-W01",
            "title": "Retitled wave",
            "description": "post-edit description landing on the wave",
        },
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        new_state = orjson.loads(state_path.read_bytes())
        wave_row = new_state["waves"]["P50-I01-W01"]
        assert wave_row["title"] == "Retitled wave"
        assert wave_row["description"] == "post-edit description landing on the wave"

    _run(body)


def test_mutate_roadmap_revise_retitle_iter_carries_description(tmp_path: Path) -> None:
    """ROADMAP_REVISE op=retitle with iter_id routes description onto Iter.description."""
    payload = _build_planned_phase_payload()
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={
            "op": "retitle",
            "iter_id": "P50-I01",
            "title": "renamed iter",
            "description": "iter narrative attached post-plan",
        },
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        new_state = orjson.loads(state_path.read_bytes())
        iter_row = new_state["iters"]["P50-I01"]
        assert iter_row["title"] == "renamed iter"
        assert iter_row["description"] == "iter narrative attached post-plan"

    _run(body)


def test_mutate_roadmap_revise_description_over_cap_rejected(tmp_path: Path) -> None:
    """An over-cap description (>500 chars) is rejected by the model on the daemon path."""
    payload = _build_planned_phase_payload()
    ctx, state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    before_bytes = state_path.read_bytes()
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P50",
        mutation_id=uuid.uuid4().hex,
        params={
            "op": "add_wave",
            "wave_id": "P50-I01-W01",
            "iter_id": "P50-I01",
            "title": "Over-cap desc wave",
            "file_scopes": ["src/x.py"],
            "description": "z" * 501,
            "effort_bucket": "S",
            "intent": {
                "problem": "x is unhandled",
                "desired_outcome": "x is handled",
                "priority_rationale": "handle x before the dependents land",
            },
        },
    )

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        # state.json untouched on rejection.
        assert state_path.read_bytes() == before_bytes

    _run(body)


def test_mutate_phase_open_then_state_read_roundtrips_description(tmp_path: Path) -> None:
    """Full round-trip: PHASE_OPEN with description -> state.read returns it."""
    payload = _build_planned_phase_payload()
    ctx, _state_path, _, _ = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.PHASE_OPEN,
        scope_id="P52",
        mutation_id=uuid.uuid4().hex,
        params={
            "phase_id": "P52",
            "title": "Plan import",
            "description": "phase open round-trip description",
        },
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        result: dict[str, Any] = await read(ctx, {})
        phase_row = result["state"]["phases"]["P52"]
        assert phase_row["description"] == "phase open round-trip description"

    _run(body)


# --- P30-I25-W40/W41: a counter reset must not strand the wave --------------


def test_close_succeeds_for_a_wave_whose_counters_reset(tmp_path: Path) -> None:
    """A wave re-originated by a counter reset CLOSES -- no operator waiver needed.

    This drives the real `mutate` close, not the delta helper. The prior test only
    asserted `compute_runtime_delta` returned 0.0 -- which is exactly the value the
    close gate then REFUSED, so the wave still could not close and the test was
    green over the failure.

    The live case: P30-I25-W34 changed what the duration measures while waves were
    claimed against baselines under the old measure. Their counters "went
    backwards", the capture path re-originated them, and the resulting zero-EU
    close was refused -- stranding the wave, since the baseline is on disk and
    every retry hits the same zero.
    """
    payload = _build_state_payload()
    wave = payload["waves"]["P24-I01-W09"]
    now = _now().isoformat()
    # The reset re-originated the wave: baseline == latest, so the delta is 0, and
    # nothing has captured since (the reset is the last thing that happened).
    wave["runtime_baseline"] = {"api_duration_ms": 500, "captured_at": now}
    wave["runtime_latest"] = {"api_duration_ms": 500, "captured_at": now}
    # ... and the wave RECORDS why its runtime is missing.
    wave["runtime_carry"] = {"counter_resets": 1}
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        # It CLOSES. That is the whole point: a recorded reset is an honest reason
        # for missing runtime, not a silent capture failure.
        assert written["waves"]["P24-I01-W09"]["status"] == "closed"
        assert written["actuals"]["P24-I01-W09"]["elapsed_eu"] == 0.0

    _run(body)


def test_close_still_refuses_an_unexplained_zero(tmp_path: Path) -> None:
    """Without a recorded reset, a zero-EU close is still refused.

    The gate's teeth stay in: the reset exemption is not a licence for a silent
    capture failure, which is the defect the gate exists to catch.
    """
    from eawf.runtime.daemon.methods import DaemonValidationError

    payload = _build_state_payload()
    wave = payload["waves"]["P24-I01-W09"]
    now = _now().isoformat()
    wave["runtime_baseline"] = {"api_duration_ms": 500, "captured_at": now}
    wave["runtime_latest"] = {"api_duration_ms": 500, "captured_at": now}
    wave["runtime_carry"] = {"counter_resets": 0}
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="no captured runtime"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        assert written["waves"]["P24-I01-W09"]["status"] == "claimed"

    _run(body)


def test_a_stale_reset_does_not_pardon_a_later_silent_zero(tmp_path: Path) -> None:
    """A reset excuses a zero only while it is the LAST thing that happened.

    Once a capture reports after the reset, the capture path has proven itself
    alive -- so a zero from that point on is unexplained again. Without this, one
    reset in a wave's first minute would pardon every zero it ever recorded,
    including those of a capture path that silently died forty turns later. That is
    exactly the failure the gate exists to catch, laundered through the mechanism
    meant to stop an honest reset from stranding a wave (P30-I25-W45).
    """
    from eawf.runtime.daemon.methods import DaemonValidationError

    payload = _build_state_payload()
    wave = payload["waves"]["P24-I01-W09"]
    reset_at = _now()
    later = (reset_at + timedelta(hours=2)).isoformat()
    # A reset happened...
    wave["runtime_carry"] = {"counter_resets": 1}
    wave["runtime_baseline"] = {"api_duration_ms": 500, "captured_at": reset_at.isoformat()}
    # ... but captures have reported SINCE, and they measured nothing.
    wave["runtime_latest"] = {"api_duration_ms": 500, "captured_at": later}
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="no captured runtime"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        assert written["waves"]["P24-I01-W09"]["status"] == "claimed"

    _run(body)


def test_the_zero_runtime_gate_bites_outside_the_uiux_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W52: the runtime gate reads the FLEET opt-in, not the band-narrowed block.

    The UI/UX band scopes CRITERIA enforcement -- a spec jury judging a screen has
    nothing to say about a wave that touches no screen. Runtime capture is not a
    property of the band: every wave burns agent runtime, and every wave's actual
    feeds the same corpus. Narrowing this gate by band made it advisory for every
    wave outside the band, which in P30-I25 meant every wave in the iter: it could
    not refuse anything, and reported a pass while doing it.

    Restore `resolve_wave_verify_block` here and the close below succeeds with a
    warning -- which is what shipped, and what let a wave close on a figure the fifth
    audit proved was broken.
    """
    from eawf.platform.profiles.models import VerifyBlock
    from eawf.runtime.daemon.methods import DaemonValidationError

    # An enforcing profile whose band is the TUI. The wave under test is not a TUI
    # wave, so the band-narrowed resolver used to hand back enforce=False.
    monkeypatch.setattr(
        "eawf.workflow.verify.readiness.load_active_verify_block",
        lambda *a, **k: VerifyBlock(enforce=True, uiux_bands=["tui"]),
    )
    payload = _build_state_payload()
    ctx, state_path, _event_path, _wal_dir = _build_ctx(tmp_path=tmp_path, state_payload=payload)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "ok"},
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="no captured runtime"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        written = orjson.loads(state_path.read_bytes())
        assert written["waves"]["P24-I01-W09"]["status"] == "claimed"

    _run(body)


def test_mutate_iter_close_config_strictness_cannot_be_weakened_by_rpc(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Resolved strict config wins over a caller-supplied false param."""
    payload = _iter_close_payload()
    payload["audits"] = {}
    ctx, state_path, event_path, _wal_dir = _build_ctx(
        tmp_path=tmp_path,
        state_payload=payload,
    )
    config_dir = tmp_path / ".ea"
    config_dir.mkdir()
    config_dir.joinpath("config.yaml").write_text(
        "verify:\n  require_iter_audit_accepted: true\n",
        encoding="utf-8",
    )
    audit_path = store_path(state_path, StoreKind.AUDIT)
    memory_path = store_path(state_path, StoreKind.MEMORY)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text("audit sentinel\n", encoding="utf-8")
    memory_path.write_text("memory sentinel\n", encoding="utf-8")
    before = {
        path: _path_digest(path) for path in (state_path, event_path, audit_path, memory_path)
    }
    mutation = Mutation(
        kind=MutationKind.ITER_CLOSE,
        scope_id="P24-I01",
        mutation_id=uuid.uuid4().hex,
        params={
            "iter_id": "P24-I01",
            "audit_id": "AUD-PHANTOM",
            "require_audit_accepted": False,
        },
    )

    async def body() -> None:
        with (
            caplog.at_level(logging.WARNING),
            pytest.raises(DaemonValidationError, match="audit_not_found"),
        ):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})

    _run(body)
    after = {path: _path_digest(path) for path in (state_path, event_path, audit_path, memory_path)}
    assert after == before
    assert "mutation_kind=iter_close" in caplog.text
    assert "scope_id='P24-I01'" in caplog.text
    assert "guard_code=audit_not_found" in caplog.text


def test_mutate_iter_close_minor_returns_stable_warning(tmp_path: Path) -> None:
    """Daemon result surfaces the strict MINOR backlog-triage advisory."""
    payload = _iter_close_payload(verdict=AuditVerdict.MINOR)
    ctx, state_path, _event_path, _wal_dir = _build_ctx(
        tmp_path=tmp_path,
        state_payload=payload,
    )
    mutation = Mutation(
        kind=MutationKind.ITER_CLOSE,
        scope_id="P24-I01",
        mutation_id=uuid.uuid4().hex,
        params={
            "iter_id": "P24-I01",
            "audit_id": "AUD-ITER",
            "require_audit_accepted": True,
        },
    )

    result: dict[str, Any] = {}

    async def body() -> None:
        result.update(await mutate(ctx, {"mutation": mutation.model_dump(mode="json")}))

    _run(body)
    assert result["event"]["payload"]["extras"]["warning"] == ("audit_minor_backlog_triage")
    written = orjson.loads(state_path.read_bytes())
    assert written["iters"]["P24-I01"]["status"] == "closed"
    assert written["iters"]["P24-I01"]["audit_id"] == "AUD-ITER"
