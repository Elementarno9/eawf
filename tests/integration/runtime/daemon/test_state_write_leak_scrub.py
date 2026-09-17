"""State writers refuse new free text that carries a state leak shape.

The daemon's single-lock commit, its WAVE_CLOSE commit and the CLI
``state_transaction`` fallback each diff the payload on disk against the one
about to be written. A new or changed string that matches the state leak
patterns refuses the write with ``validation_failed`` naming its field path,
and ``state.json`` stays byte-identical. A clean write is unaffected: the
bytes written are exactly the payload handed to the scrub. A workspace index
may still record a repo checkout under a home directory, since that field is
a local path by design.

The suite drives the real ``mutate`` coroutine and the real
``state_transaction`` context manager; no live daemon, no subprocess. The home
path is assembled at runtime so this file carries no literal the leak lints
would flag.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.state.enums import BacklogPriority, ProjectStatus, StoreKind
from eawf.kernel.state.models import State, WorkspaceIndex, WorkspaceRepoRef
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods import state as daemon_state
from eawf.runtime.daemon.methods.state import mutate
from eawf.surfaces.cli import _mutation
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.workspace import _empty_workspace_state
from eawf.workflow.evidence.backlog import add_backlog
from eawf.workflow.lifecycle.wave import fail_wave
from eawf.workflow.verify.preflight import ClosePreflight

pytestmark = pytest.mark.integration

MACOS_HOME = "/" + "Users" + "/" + "alice"
LEAKY_TEXT = f"finished in {MACOS_HOME}/work/repo"

_T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
_WAVE = "P40-I01-W01"
_SIBLING = "P40-I01-W02"


def _wave_row(wave_id: str, *, status: str, outcome: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": wave_id,
        "iter_id": "P40-I01",
        "title": f"wave {wave_id}",
        "status": status,
        "file_scopes": ["src/x.py"],
        "success_criteria": [],
        "gates": [],
        "effort_bucket": "S",
        "agent_role": "executor",
        "opened_at": _T0.isoformat(),
        "sessions": {},
    }
    if outcome is not None:
        row["outcome"] = outcome
    return row


def _state_payload(*, sibling_outcome: str | None = None) -> dict[str, Any]:
    """A minimal valid State with a CLAIMED wave and a CLAIMED sibling."""
    return {
        "schema_version": "1.0",
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
        "current": {"project_code": "ABC"},
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
                "wave_ids": [_WAVE, _SIBLING],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE: _wave_row(_WAVE, status="claimed"),
            _SIBLING: _wave_row(_SIBLING, status="claimed", outcome=sibling_outcome),
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(state_path: Path, *, sibling_outcome: str | None = None) -> bytes:
    state = State.model_validate(_state_payload(sibling_outcome=sibling_outcome))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path.read_bytes()


def _build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at="2026-09-01T00:00:00+00:00",
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


def _fail_params(wave_id: str, reason: str) -> dict[str, Any]:
    return {
        "mutation": Mutation(
            kind=MutationKind.WAVE_FAIL,
            scope_id=wave_id,
            mutation_id=uuid.uuid4().hex,
            params={"wave_id": wave_id, "reason": reason},
        ).model_dump(mode="json")
    }


def _close_params(outcome: str) -> dict[str, Any]:
    return {
        "mutation": Mutation(
            kind=MutationKind.WAVE_CLOSE,
            scope_id=_WAVE,
            mutation_id=uuid.uuid4().hex,
            params={"wave_id": _WAVE, "outcome": outcome, "no_runtime_waiver": True},
        ).model_dump(mode="json")
    }


def _run_mutate(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    async def _body() -> dict[str, Any]:
        return await mutate(ctx, params)

    return asyncio.run(_body())


def _stub_close_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _preflight(*args: Any, **kwargs: Any) -> ClosePreflight:
        return ClosePreflight(evidence=[], readiness=None)

    monkeypatch.setattr(daemon_state, "run_close_preflight", _preflight)


def _record_scrubbed_bytes(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Wrap ``module.state_leak_refusal`` to capture the payload it was handed.

    Each entry is the payload serialised the way the state writer serialises
    it, taken before the real scrub runs, so a caller can prove the bytes on
    disk are exactly what the scrub saw.
    """
    seen: list[bytes] = []
    real_refusal: Callable[[object, object], str | None] = module.state_leak_refusal

    def _spy(old: object, new: dict[str, Any]) -> str | None:
        seen.append(orjson.dumps(new, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n")
        refusal = real_refusal(old, new)
        assert refusal is None
        return refusal

    monkeypatch.setattr(module, "state_leak_refusal", _spy)
    return seen


def _assert_nothing_written(state_path: Path, before: bytes, wal_dir: Path) -> None:
    assert state_path.read_bytes() == before
    assert list(wal_dir.iterdir()) == []
    assert not store_path(state_path, StoreKind.EVENT).exists()


def test_mutate_single_lock_refuses_leaking_wave_outcome(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    before = _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)

    with pytest.raises(DaemonValidationError) as excinfo:
        _run_mutate(ctx, _fail_params(_WAVE, LEAKY_TEXT))

    message = str(excinfo.value)
    assert message.startswith("validation_failed: state_leak_refused: ")
    assert f"waves.{_WAVE}.outcome (home_path)" in message
    assert MACOS_HOME not in message
    _assert_nothing_written(state_path, before, tmp_path / "wal")


def test_mutate_wave_close_refuses_leaking_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    before = _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)
    _stub_close_preflight(monkeypatch)

    with pytest.raises(DaemonValidationError) as excinfo:
        _run_mutate(ctx, _close_params(LEAKY_TEXT))

    message = str(excinfo.value)
    assert message.startswith("validation_failed: state_leak_refused: ")
    assert f"waves.{_WAVE}.outcome (home_path)" in message
    _assert_nothing_written(state_path, before, tmp_path / "wal")


def test_state_transaction_refuses_leaking_backlog_title(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    before = _write_state(state_path)
    _mutation.set_daemonless_flag(False)

    with (
        pytest.raises(cli_errors.ValidationError) as excinfo,
        _mutation.state_transaction(state_path) as state,
    ):
        add_backlog(
            state,
            item_id="B900",
            title=f"triage {MACOS_HOME}/notes",
            priority=BacklogPriority.P2,
            scope_id="ABC",
        )

    message = str(excinfo.value)
    assert message.startswith("validation_failed: state_leak_refused: ")
    assert "backlog.B900.title (home_path)" in message
    assert state_path.read_bytes() == before


def test_state_transaction_refuses_leaking_wave_outcome(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    before = _write_state(state_path)
    _mutation.set_daemonless_flag(False)

    with (
        pytest.raises(cli_errors.ValidationError, match=rf"waves\.{_WAVE}\.outcome \(home_path\)"),
        _mutation.state_transaction(state_path) as state,
    ):
        fail_wave(state, wave_id=_WAVE, reason=LEAKY_TEXT)

    assert state_path.read_bytes() == before


def test_mutate_single_lock_ignores_preexisting_leak(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, sibling_outcome=LEAKY_TEXT)
    ctx = _build_ctx(tmp_path, state_path)

    _run_mutate(ctx, _fail_params(_WAVE, "clean failure reason"))

    payload = orjson.loads(state_path.read_bytes())
    assert payload["waves"][_WAVE]["status"] == "failed"
    assert payload["waves"][_SIBLING]["outcome"] == LEAKY_TEXT


def test_mutate_single_lock_clean_write_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)
    seen = _record_scrubbed_bytes(daemon_state, monkeypatch)

    _run_mutate(ctx, _fail_params(_WAVE, "clean failure reason"))

    assert len(seen) == 1
    assert state_path.read_bytes() == seen[0]
    assert orjson.loads(seen[0])["waves"][_WAVE]["outcome"] == "clean failure reason"


def test_mutate_wave_close_clean_write_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)
    _stub_close_preflight(monkeypatch)
    seen = _record_scrubbed_bytes(daemon_state, monkeypatch)

    _run_mutate(ctx, _close_params("shipped cleanly"))

    assert len(seen) == 1
    assert state_path.read_bytes() == seen[0]
    closed = orjson.loads(seen[0])["waves"][_WAVE]
    assert (closed["status"], closed["outcome"]) == ("closed", "shipped cleanly")


def test_state_transaction_clean_write_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    _mutation.set_daemonless_flag(False)
    seen = _record_scrubbed_bytes(_mutation, monkeypatch)

    with _mutation.state_transaction(state_path) as state:
        add_backlog(
            state,
            item_id="B901",
            title="triage the flaky heartbeat test",
            priority=BacklogPriority.P2,
            scope_id="ABC",
        )

    assert len(seen) == 1
    assert state_path.read_bytes() == seen[0]
    assert orjson.loads(seen[0])["backlog"]["B901"]["title"] == "triage the flaky heartbeat test"


def test_state_transaction_accepts_workspace_repo_checkout_under_home(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(orjson.dumps(_empty_workspace_state(code="WSP", title="workspace")))
    checkout = f"{MACOS_HOME}/work/abc"
    _mutation.set_daemonless_flag(False)

    with _mutation.state_transaction(state_path) as state:
        assert state.workspace is not None
        ref = WorkspaceRepoRef(
            code="ABC",
            path=checkout,
            state_urn="urn:eawf:v1:state:ABC",
            project_code="ABC",
            title="abc",
            status=ProjectStatus.ACTIVE,
        )
        state.workspace = WorkspaceIndex(
            code=state.workspace.code,
            title=state.workspace.title,
            repos={"ABC": ref},
        )

    assert orjson.loads(state_path.read_bytes())["workspace"]["repos"]["ABC"]["path"] == checkout


def test_state_transaction_read_only_skips_scrub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    before = _write_state(state_path)
    seen = _record_scrubbed_bytes(_mutation, monkeypatch)

    with _mutation.state_transaction(state_path, read_only=True) as state:
        assert state.waves[_WAVE].status.value == "claimed"

    assert seen == []
    assert state_path.read_bytes() == before
