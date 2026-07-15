"""Tests: ``mockup_golden_diff`` closes through the deterministic T5 gate."""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.spec.common import CriterionSpec, GateSpec, QualityDimension
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.state import _enforce_wave_close_gate, mutate
from eawf.workflow.lifecycle.transitions import LifecycleError

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
_PHASE = "P30"
_ITER = "P30-I04"
_WAVE = "P30-I04-W08"
_CRITERION = "CR-MOCKUP"
_GATE = "mockup_golden_diff"
_GOLDEN_REL = "tests/snapshots/tui/golden/mockup_P30-I04-W08.txt"
_GOLDEN_TEXT = "+----------------+\n| expected screen |\n+----------------+"


def _now() -> datetime:
    return _T0


def _mockup_criterion() -> dict[str, Any]:
    spec = CriterionSpec(
        id=_CRITERION,
        text="mockup_golden_diff validates the built TUI screen against the pick-time golden",
        kind="ui_mockup_fidelity",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=[_GATE],
        required=True,
        quality_dimension=QualityDimension.INTERACTION_CAPABILITY,
        measurable_signal="mockup_golden_diff compares the built screen to the approved golden",
    )
    return spec.model_dump(mode="json")


def _mockup_gate() -> dict[str, Any]:
    spec = GateSpec(
        id=_GATE,
        criterion_id=_CRITERION,
        kind="mockup_golden_diff",
        args={
            "golden_path": _GOLDEN_REL,
            "state_path": ".ea/state.json",
            "scope": "repo",
            "mode": "home",
            "size": [100, 30],
        },
        policy="block",
        cadence="every-wave",
        required=True,
    )
    return spec.model_dump(mode="json")


def _state_payload() -> dict[str, Any]:
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
            _PHASE: {
                "id": _PHASE,
                "scope_id": "ABC",
                "track_id": None,
                "title": "P30",
                "status": "active",
                "iter_ids": [_ITER],
                "outcome_ids": [],
                "opened_at": _now().isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            _ITER: {
                "id": _ITER,
                "phase_id": _PHASE,
                "title": "I04",
                "status": "active",
                "wave_ids": [_WAVE],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _now().isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE: {
                "id": _WAVE,
                "iter_id": _ITER,
                "title": "bind mockup golden diff close gate",
                "status": "claimed",
                "claim_session_id": "session-abc",
                "file_scopes": ["src/eawf/surfaces/tui/snapshot/pilot_harness.py"],
                "success_criteria": [_mockup_criterion()],
                "gates": [_mockup_gate()],
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _now().isoformat(),
                "runtime_baseline": {
                    "api_duration_ms": 5000,
                    "total_duration_ms": 7000,
                    "captured_at": _now().isoformat(),
                },
                "runtime_latest": {
                    "api_duration_ms": 17000,
                    "total_duration_ms": 23000,
                    "captured_at": (_now() + timedelta(minutes=5)).isoformat(),
                },
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _init_git_repo(root: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.t",
    }
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=root,
        check=True,
        env=env,
    )


def _write_enforcing_profile(root: Path) -> None:
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
                "  enforce: true",
                "  cross_vendor_jury: false",
                "  uiux_bands:",
                "    - tui",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_state(state_path: Path) -> State:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = State.model_validate(_state_payload())
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state


def _write_golden(root: Path) -> None:
    target = root / _GOLDEN_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_GOLDEN_TEXT + "\n", encoding="utf-8")


def _build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at="2026-06-10T00:00:00+00:00",
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


def _close_mutation() -> Mutation:
    return Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=_WAVE,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": _WAVE, "outcome": "ok"},
    )


def _run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(body())


def _read_evidence_rows(state_path: Path) -> list[EvidenceRecord]:
    path = store_path(state_path, StoreKind.EVIDENCE)
    if not path.exists():
        return []
    rows: list[EvidenceRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = orjson.loads(line)
        rows.append(EvidenceRecord.model_validate(envelope["payload"]))
    return rows


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured: str,
) -> tuple[Path, MethodContext]:
    _write_enforcing_profile(tmp_path)
    _init_git_repo(tmp_path)
    _write_golden(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)

    def _capture(**kwargs: object) -> str:
        assert kwargs["scope"] == "repo"
        assert kwargs["state_path"] == state_path.resolve()
        assert kwargs["mode"] == "home"
        assert kwargs["size"] == (100, 30)
        return captured

    monkeypatch.setattr(
        "eawf.surfaces.tui.snapshot.pilot_harness.capture_mockup_golden_screen_text_sync",
        _capture,
    )
    return state_path, ctx


def test_close_gate_passes_and_mints_t5_mockup_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path, ctx = _setup(tmp_path, monkeypatch, captured=_GOLDEN_TEXT)

    async def body() -> None:
        await mutate(ctx, {"mutation": _close_mutation().model_dump(mode="json")})

        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_WAVE]["status"] == "closed"

        rows = _read_evidence_rows(state_path)
        deterministic_pass = [
            row for row in rows if row.evidence_kind == "deterministic" and row.status == "pass"
        ]
        assert len(deterministic_pass) == 1
        row = deterministic_pass[0]
        assert row.scope_id == _WAVE
        assert row.produced_by == "tool"
        assert row.metrics is not None
        assert row.metrics["oracle_tier"] == 5
        assert row.metrics["gate_id"] == _GATE
        assert row.refs == [_GATE, _CRITERION]
        assert "tier T5" in row.summary

    _run(body)


def test_close_gate_blocks_mockup_golden_divergence_with_region_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path, _ctx = _setup(
        tmp_path,
        monkeypatch,
        captured="+----------------+\n| actual screen |\n+----------------+",
    )
    state = State.model_validate_json(state_path.read_text(encoding="utf-8"))

    async def body() -> None:
        with pytest.raises(LifecycleError) as excinfo:
            await _enforce_wave_close_gate(
                state,
                _close_mutation(),
                state_path=state_path,
                repo_root=tmp_path,
            )

        message = str(excinfo.value)
        assert "oracle blocked close" in message
        assert f"criterion='{_CRITERION}'" in message
        assert "tier=5" in message
        assert "status=fail" in message
        assert "mockup golden mismatch" in message
        assert "region=@@" in message
        assert "-| expected screen |" in message
        assert "+| actual screen |" in message
        assert _read_evidence_rows(state_path) == []

    _run(body)
