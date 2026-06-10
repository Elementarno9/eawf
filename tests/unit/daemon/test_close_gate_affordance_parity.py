"""Tests: required UI affordance gates fire at wave close (P30-I04-W06)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
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
_WAVE = "P30-I04-W06"
_CRITERION = "CR-AFFORDANCE"
_GATE = "affordance_parity"


def _now() -> datetime:
    return _T0


def _affordance_criterion() -> dict[str, Any]:
    spec = CriterionSpec(
        id=_CRITERION,
        text="affordance_parity validates home mode advertised footer keys",
        kind="ui_affordance",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=[_GATE],
        required=True,
        quality_dimension=QualityDimension.INTERACTION_CAPABILITY,
        measurable_signal="affordance_parity HOME mode probe finds no dead advertised keys",
    )
    return spec.model_dump(mode="json")


def _affordance_gate() -> dict[str, Any]:
    spec = GateSpec(
        id=_GATE,
        criterion_id=_CRITERION,
        kind="affordance_parity",
        args={"mode": "home", "state_path": ".ea/state.json", "size": [100, 30]},
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
                "subproject_id": None,
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
                "title": "tui close affordance parity",
                "status": "claimed",
                "claim_session_id": "session-abc",
                "file_scopes": ["src/eawf/surfaces/tui/app.py"],
                "success_criteria": [_affordance_criterion()],
                "gates": [_affordance_gate()],
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _now().isoformat(),
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
    offending_keys: list[str],
) -> tuple[Path, MethodContext]:
    _write_enforcing_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)

    def _fake_collect_offending_keys(
        *,
        mode: str,
        state_path: Path | None,
        size: tuple[int, int],
    ) -> list[str]:
        assert mode == "home"
        assert state_path is not None
        assert size == (100, 30)
        return list(offending_keys)

    monkeypatch.setattr(
        "eawf.workflow.audit_dsl.kinds.affordance_parity._collect_offending_keys",
        _fake_collect_offending_keys,
    )
    return state_path, ctx


def test_close_gate_passes_when_parity_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path, ctx = _setup(tmp_path, monkeypatch, offending_keys=[])

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
        assert row.metrics["oracle_tier"] == 2
        assert row.metrics["gate_id"] == _GATE
        assert row.refs == [_GATE, _CRITERION]
        assert "tier T2" in row.summary

    _run(body)


def test_close_gate_blocks_on_dead_affordance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path, _ctx = _setup(tmp_path, monkeypatch, offending_keys=["z"])
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
        assert "tier=2" in message
        assert "status=fail" in message
        assert "unresolved advertised keys: z" in message
        assert _read_evidence_rows(state_path) == []

    _run(body)
