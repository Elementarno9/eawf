"""Integration tests for the ``spec.sync`` JSON-RPC handler (P29-I12-W05).

``eawf spec sync <wave-id>`` is the authoring keystone: it reads a per-wave
spec markdown body, parses its ``eawf-wave-body`` fenced block into typed
criteria + gates, runs the EAWF021 measurability + EAWF022 coverage lints,
and — only when both pass — replaces the target PENDING wave's
``success_criteria`` + ``gates`` through the daemon's canonical state-write
transaction (AGENTS rule 4). Coverage:

* happy path: a well-formed body syncs → the wave row gains the typed
  criteria + gates, asserted against a freshly-loaded :class:`State`;
* error path: a sub-floor / vague ``measurable_signal`` trips EAWF021 →
  non-zero (DaemonValidationError), NO state write;
* error path: an uncovered planned-step span trips EAWF022 → rejected,
  NO state write;
* error path: a CLOSED (non-PENDING) target is rejected;
* boundary: a missing spec file is rejected before any read of state.

The handler is driven directly through the module-level coroutine so the
tests do not need a live UDS / named-pipe transport.
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

_T0 = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)
_WAVE_ID = "P29-I12-W05"

# A well-formed wave body: one deterministic criterion + its schema gate.
# The ``measurable_signal`` clears the 20-char floor and carries no banned
# vague token, and the criterion's text names an observation verb + locus
# so EAWF021 passes both legs.
_GOOD_YAML = textwrap.dedent(
    """\
    criteria:
      - id: CR-01
        text: returns the materialised rows; pytest tests/integration/test_spec_sync.py
        kind: behavioral
        acceptance_style: binary
        evidence_kind: deterministic
        quality_dimension: functional_suitability
        measurable_signal: the spec-sync integration test asserts the typed rows land
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

# A vague-signal body: ``is performant`` is a banned EAWF021 token in the
# ``measurable_signal`` even though it clears the 20-char floor.
_VAGUE_YAML = textwrap.dedent(
    """\
    criteria:
      - id: CR-01
        text: returns the materialised rows; pytest tests/x.py::test_ok
        kind: behavioral
        acceptance_style: binary
        evidence_kind: deterministic
        quality_dimension: functional_suitability
        measurable_signal: the endpoint is performant under sustained load
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


def _wrap_body(yaml_block: str) -> str:
    """Wrap a YAML block in the canonical ``eawf-wave-body`` fence."""
    return (
        "# Wave deliverable\n\n"
        "Authored prose before the structured block.\n\n"
        "```eawf-wave-body\n"
        f"{yaml_block}"
        "```\n\n"
        "Trailing prose.\n"
    )


def _state_payload(
    *, status: str, planned_steps: list[str], source_brief_ids: list[str] | None = None
) -> dict[str, Any]:
    """A minimal valid State with one wave under P29-I12 in *status*.

    The wave carries an :class:`IntentBrief` whose ``planned_steps`` are the
    per-wave brief spans the EAWF022 coverage lint scores the criteria
    against. Pass ``planned_steps=[]`` to make the planned-step coverage leg a
    no-op. Pass ``source_brief_ids`` to make the wave required-intent so the
    source-brief coverage leg reads the referenced document(s) even when
    ``planned_steps`` is empty.
    """
    intent = {
        "problem": "materialise parsed criteria + gates onto the wave row",
        "desired_outcome": "the wave row carries typed criteria + gates from its spec body",
        "planned_steps": list(planned_steps),
        "source_brief_ids": list(source_brief_ids) if source_brief_ids is not None else [],
    }
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
                "subproject_id": None,
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
                "title": "add eawf spec sync",
                "status": status,
                "success_criteria": [],
                "gates": [],
                "effort_bucket": "M",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "closed_at": _T0.isoformat() if status == "closed" else None,
                "outcome": "ok" if status == "closed" else None,
                "sessions": {},
                "intent": intent,
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


def _write_spec_file(repo_root: Path, body: str) -> Path:
    spec_path = repo_root / ".ea" / "specs" / "P29" / "P29-I12" / f"{_WAVE_ID}.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(body, encoding="utf-8")
    return spec_path


def _build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at="2026-06-06T00:00:00+00:00",
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


# --------------------------------------------------------------------------- #
# Happy path — a well-formed body materialises typed criteria + gates.
# --------------------------------------------------------------------------- #
def test_sync_materialises_criteria_and_gates(tmp_path: Path) -> None:
    """A well-formed spec body syncs → the wave row gains the typed rows."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state_payload(status="pending", planned_steps=[]))
    _write_spec_file(repo_root, _wrap_body(_GOOD_YAML))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})
        assert result["operation"] == "sync"
        assert result["criteria_count"] == 1
        assert result["gates_count"] == 1

    _run(body)

    wave = _load_wave(state_path)
    assert len(wave.success_criteria) == 1
    assert wave.success_criteria[0].id == "CR-01"
    assert wave.success_criteria[0].evidence_kind == "deterministic"
    assert len(wave.gates) == 1
    assert wave.gates[0].id == "G-01"
    assert wave.gates[0].criterion_id == "CR-01"


def test_sync_publishes_event_envelope(tmp_path: Path) -> None:
    """The canonical state-event envelope publishes on the bus."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state_payload(status="pending", planned_steps=[]))
    _write_spec_file(repo_root, _wrap_body(_GOOD_YAML))
    ctx = _build_ctx(tmp_path, state_path)
    published: list[Any] = []
    ctx.bus.publish = lambda env: published.append(env)  # type: ignore[method-assign]

    async def body() -> None:
        await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})

    _run(body)
    assert len(published) == 1
    assert published[0].kind is StoreKind.EVENT
    assert published[0].payload["event_type"] == "state.mutate.spec_sync"
    assert published[0].scope_id == _WAVE_ID


# --------------------------------------------------------------------------- #
# EAWF022 coverage — a planned step covered by a criterion passes.
# --------------------------------------------------------------------------- #
def test_sync_passes_when_planned_step_is_covered(tmp_path: Path) -> None:
    """A planned step the criterion shares a significant token with is covered.

    The planned step ``materialise the parsed rows onto state`` shares the
    significant token ``rows`` with the ``_GOOD_YAML`` criterion's text /
    measurable_signal, so EAWF022 sees the span as covered.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(status="pending", planned_steps=["materialise the parsed rows onto state"]),
    )
    _write_spec_file(repo_root, _wrap_body(_GOOD_YAML))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})
        assert result["criteria_count"] == 1

    _run(body)
    wave = _load_wave(state_path)
    assert len(wave.success_criteria) == 1


# --------------------------------------------------------------------------- #
# EAWF021 — a vague measurable_signal is rejected with no state write.
# --------------------------------------------------------------------------- #
def test_sync_rejects_vague_measurable_signal(tmp_path: Path) -> None:
    """A banned-vague ``measurable_signal`` trips EAWF021; no state write."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state_payload(status="pending", planned_steps=[]))
    _write_spec_file(repo_root, _wrap_body(_VAGUE_YAML))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError) as exc:
            await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})
        assert "EAWF021" in str(exc.value)

    _run(body)
    # No state write: the wave row still carries empty criteria + gates.
    wave = _load_wave(state_path)
    assert wave.success_criteria == []
    assert wave.gates == []


# --------------------------------------------------------------------------- #
# EAWF022 — an uncovered planned-step span is rejected with no state write.
# --------------------------------------------------------------------------- #
def test_sync_rejects_uncovered_span(tmp_path: Path) -> None:
    """A planned-step span no criterion covers trips EAWF022; no state write.

    The planned step ``recalibrate the telemetry sampler histogram buckets``
    shares no significant token with the ``_GOOD_YAML`` criterion (which is
    about materialising rows), so EAWF022 surfaces it as an uncovered span.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(
            status="pending",
            planned_steps=["recalibrate the telemetry sampler histogram buckets"],
        ),
    )
    _write_spec_file(repo_root, _wrap_body(_GOOD_YAML))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError) as exc:
            await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})
        assert "EAWF022" in str(exc.value)

    _run(body)
    wave = _load_wave(state_path)
    assert wave.success_criteria == []
    assert wave.gates == []


# --------------------------------------------------------------------------- #
# EAWF022 source-brief — a required-intent wave with empty planned_steps still
# diffs the source-brief document; an uncovered deliverable is rejected.
# --------------------------------------------------------------------------- #
def test_sync_rejects_uncovered_source_brief_unit_with_empty_planned_steps(
    tmp_path: Path,
) -> None:
    """An uncovered source-brief deliverable trips EAWF022 even with no steps.

    The wave is required-intent (its ``source_brief_ids`` names an on-disk
    brief) but carries no ``planned_steps``, so the legacy planned-step no-op
    would have short-circuited the coverage check. The source-brief leg reads
    the brief document, whose ``recalibrate the telemetry sampler histogram
    buckets`` deliverable shares no significant token with the ``_GOOD_YAML``
    criterion, so EAWF022 surfaces it; no state write lands.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    brief = repo_root / ".ea" / "local" / "research" / "brief.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("Recalibrate the telemetry sampler histogram buckets.\n", encoding="utf-8")
    state_path = repo_root / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(
            status="pending",
            planned_steps=[],
            source_brief_ids=[".ea/local/research/brief.md"],
        ),
    )
    _write_spec_file(repo_root, _wrap_body(_GOOD_YAML))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError) as exc:
            await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})
        assert "EAWF022" in str(exc.value)

    _run(body)
    wave = _load_wave(state_path)
    assert wave.success_criteria == []
    assert wave.gates == []


# --------------------------------------------------------------------------- #
# PENDING-only — a CLOSED target is rejected.
# --------------------------------------------------------------------------- #
def test_sync_rejects_closed_wave(tmp_path: Path) -> None:
    """A non-PENDING (CLOSED) wave target is rejected; no state write."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state_payload(status="closed", planned_steps=[]))
    _write_spec_file(repo_root, _wrap_body(_GOOD_YAML))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError) as exc:
            await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})
        assert "not pending" in str(exc.value)

    _run(body)
    wave = _load_wave(state_path)
    assert wave.success_criteria == []


# --------------------------------------------------------------------------- #
# Boundary — a missing spec file is rejected.
# --------------------------------------------------------------------------- #
def test_sync_rejects_missing_spec_file(tmp_path: Path) -> None:
    """A wave with no on-disk spec body is rejected before any state write."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state_payload(status="pending", planned_steps=[]))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="spec file missing"):
            await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})

    _run(body)


# --------------------------------------------------------------------------- #
# Boundary — a non-wave scope is rejected.
# --------------------------------------------------------------------------- #
def test_sync_rejects_non_wave_scope(tmp_path: Path) -> None:
    """A phase / iter scope id is rejected (sync targets a wave)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state_payload(status="pending", planned_steps=[]))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="wave scope"):
            await sync(ctx, {"wave_id": "P29-I12", "repo_root": str(repo_root)})

    _run(body)


# --------------------------------------------------------------------------- #
# Idempotency — a repeat call with the same key replays the cached result.
# --------------------------------------------------------------------------- #
def test_sync_idempotent_replay(tmp_path: Path) -> None:
    """A repeat sync with the same idempotency_key replays the cached result."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state_payload(status="pending", planned_steps=[]))
    _write_spec_file(repo_root, _wrap_body(_GOOD_YAML))
    ctx = _build_ctx(tmp_path, state_path)
    key = uuid.uuid4().hex

    async def body() -> None:
        first = await sync(
            ctx,
            {"wave_id": _WAVE_ID, "repo_root": str(repo_root), "idempotency_key": key},
        )
        second = await sync(
            ctx,
            {"wave_id": _WAVE_ID, "repo_root": str(repo_root), "idempotency_key": key},
        )
        assert first["idempotent_replay"] is False
        assert second["idempotent_replay"] is True
        assert second["after_version"] == first["after_version"]

    _run(body)
