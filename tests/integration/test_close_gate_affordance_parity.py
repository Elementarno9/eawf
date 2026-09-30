"""Tests: a required affordance_parity gate blocks wave close.

The kind drove the retired epoch-1 TUI, so it reports ``blocked`` and a wave
that still requires it cannot close on it.
"""

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

from eawf.kernel.spec.common import CriterionSpec, GateSpec, QualityDimension
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.methods.state import _enforce_wave_close_gate
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
                "title": "tui close affordance parity",
                "status": "claimed",
                "claim_session_id": "session-abc",
                "file_scopes": ["src/eawf/surfaces/tui/console/keybar.py"],
                "success_criteria": [_affordance_criterion()],
                "gates": [_affordance_gate()],
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


def _criterion_rows(rows: list[EvidenceRecord]) -> list[EvidenceRecord]:
    """Return the criterion-level evidence rows among *rows*.

    The per-gate receipts share the store and the ``deterministic`` shape, so
    the criterion-level row is the one WITHOUT the receipt marker.
    """
    return [row for row in rows if (row.metrics or {}).get("receipt") is None]


def _setup(tmp_path: Path) -> Path:
    """Build the enforcing fixture repo and return its state path."""
    _write_enforcing_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    return state_path


def test_close_gate_blocks_on_the_retired_affordance_gate(tmp_path: Path) -> None:
    state_path = _setup(tmp_path)
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
        assert "status=blocked" in message
        assert "epoch-1 TUI app this gate drove is retired" in message
        assert _criterion_rows(_read_evidence_rows(state_path)) == []

    _run(body)
