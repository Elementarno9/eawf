"""Tests: ``spec.sync`` runs the plan-time criteria floor over incoming gates.

``spec.sync`` materialises a wave's typed criteria AND its gates in one
:func:`~eawf.workflow.lifecycle.wave.edit_wave_plan` call. The gates have to
travel with the criteria: the floor resolves every ``gate_ids`` entry against
the gate set it is handed, and at sync time the row's own ``gates`` list is
still the pre-sync one (empty on a first sync), so a valid body would be
rejected as dangling if the incoming gates did not reach the floor.

Coverage:

* happy path: a well-formed body syncs and the wave row carries both the
  criteria and the gates;
* error path: a one-way binding -- a gate whose ``criterion_id`` names a
  criterion that omits that gate from ``gate_ids`` -- is rejected before any
  write. This is the shape that deadlocks a close: the gate RUNS, PASSES, and
  stores a receipt, and the close-time preflight then rejects the stored
  receipt because its gate id is absent from the scored criterion, which no
  operator waiver rescues because preflight reads receipts before waivers;
* error path: a ``gate_ids`` entry naming a gate the body does not define is
  rejected before any write (the spec layer's cross-reference check reaches
  this one first, so the assertion pins the boundary, not the floor leg).
"""

from __future__ import annotations

import asyncio
import os
import textwrap
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.spec import sync

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
_WAVE_ID = "P29-I12-W05"

_SYMMETRIC_YAML = textwrap.dedent(
    """\
    criteria:
      - id: CR-01
        text: returns the materialised rows; pytest tests/daemon/test_spec_sync_floor.py
        kind: behavioral
        acceptance_style: binary
        evidence_kind: deterministic
        quality_dimension: functional_suitability
        measurable_signal: the spec-sync floor test asserts the typed rows land
        gate_ids: [G-01]
    gates:
      - id: G-01
        criterion_id: CR-01
        kind: schema_validate
        args: {model: CloseReadiness}
        policy: block
        cadence: every-wave
    """
)

# G-02 names CR-01, but CR-01's gate_ids lists only G-01: the binding is
# one-way, so G-02 would run, pass, and strand the close on its own receipt.
_ONE_WAY_BINDING_YAML = _SYMMETRIC_YAML + (
    "  - id: G-02\n"
    "    criterion_id: CR-01\n"
    "    kind: schema_validate\n"
    "    args: {model: CloseReadiness}\n"
    "    policy: block\n"
    "    cadence: every-wave\n"
)

_UNKNOWN_GATE_REF_YAML = _SYMMETRIC_YAML.replace(
    "    gate_ids: [G-01]\n", "    gate_ids: [G-01, G-404]\n"
)


def _wrap_body(yaml_block: str) -> str:
    return (
        "# Wave deliverable\n\nAuthored prose.\n\n"
        f"```eawf-wave-body\n{yaml_block}```\n\nTrailing prose.\n"
    )


def _state_payload() -> dict[str, Any]:
    """A minimal valid State with one PENDING wave under P29-I12."""
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
            "P29": {
                "id": "P29",
                "scope_id": "EAWF",
                "track_id": None,
                "title": "P29",
                "status": "active",
                "iter_ids": ["P29-I12"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P29-I12": {
                "id": "P29-I12",
                "phase_id": "P29",
                "title": "I12",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P29-I12",
                "title": "sync typed criteria and gates together",
                "status": "pending",
                "file_scopes": [],
                "success_criteria": [],
                "gates": [],
                "effort_bucket": "M",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "outcome": None,
                "sessions": {},
                "intent": {
                    "problem": "materialise parsed criteria + gates onto the wave row",
                    "desired_outcome": "the wave row carries typed criteria + gates",
                    "planned_steps": [],
                    "source_brief_ids": [],
                },
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _prepare(tmp_path: Path, yaml_block: str) -> tuple[Path, Path, MethodContext]:
    repo_root = tmp_path / f"repo-{uuid.uuid4().hex[:8]}"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        State.model_validate(_state_payload()).model_dump_json(), encoding="utf-8"
    )
    spec_path = repo_root / ".ea" / "specs" / "P29" / "P29-I12" / f"{_WAVE_ID}.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(_wrap_body(yaml_block), encoding="utf-8")
    wal_dir = tmp_path / f"wal-{uuid.uuid4().hex[:8]}"
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
    return repo_root, state_path, ctx


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def test_sync_lands_criteria_and_gates_when_the_binding_is_symmetric(tmp_path: Path) -> None:
    """A valid body still syncs: the incoming gates reach the floor.

    Regression pin for the wiring -- the row's ``gates`` are empty when the
    floor runs, so CR-01's ``gate_ids: [G-01]`` resolves only because
    ``edit_wave_plan`` is handed the incoming gate set.
    """
    repo_root, state_path, ctx = _prepare(tmp_path, _SYMMETRIC_YAML)

    async def body() -> None:
        result = await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})
        assert result["criteria_count"] == 1
        assert result["gates_count"] == 1

    _run(body)

    wave = State.model_validate(orjson.loads(state_path.read_bytes())).waves[_WAVE_ID]
    assert [criterion.id for criterion in wave.success_criteria] == ["CR-01"]
    assert [gate.id for gate in wave.gates] == ["G-01"]
    assert wave.success_criteria[0].gate_ids == ["G-01"]


def test_sync_rejects_a_one_way_gate_binding_before_any_write(tmp_path: Path) -> None:
    """A gate the criterion does not name back is rejected, state untouched."""
    repo_root, state_path, ctx = _prepare(tmp_path, _ONE_WAY_BINDING_YAML)
    before = state_path.read_bytes()
    assert ctx.wal_dir is not None

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="does not list them in gate_ids"):
            await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})

    _run(body)

    assert state_path.read_bytes() == before
    assert not list(Path(ctx.wal_dir).glob("*.json"))


def test_sync_rejects_a_gate_ref_that_does_not_resolve_before_any_write(tmp_path: Path) -> None:
    """A gate_ids entry naming no defined gate is rejected, state untouched."""
    repo_root, state_path, ctx = _prepare(tmp_path, _UNKNOWN_GATE_REF_YAML)
    before = state_path.read_bytes()
    assert ctx.wal_dir is not None

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="G-404"):
            await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})

    _run(body)

    assert state_path.read_bytes() == before
    assert not list(Path(ctx.wal_dir).glob("*.json"))
