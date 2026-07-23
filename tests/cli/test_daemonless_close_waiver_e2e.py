"""End-to-end wiring tests for the daemonless-waiver door + close-mechanism stamp.

P30-I16-W25 wires two W18/W19 mechanisms that prior waves built but left
disconnected into the LIVE wave-close path:

* **Daemonless-waiver door (W18 -> live close).** Under ``EAWF_DAEMONLESS=1`` the
  daemon close gate never runs, so a GATE-BEARING wave would slip its
  falsifiers. The live in-process close path now rejects such a close unless the
  operator passed ``--no-runtime`` (the per-invocation daemonless waiver), and on
  the allowed bypass appends an auditable waiver EVENT naming the wave + reason.
  A NON-gate-bearing daemonless close keeps the env hatch untouched.
* **close_mechanism stamp (W18 -> live close).** Every wave-close EVENT emitted by
  the in-process fallback now carries the ``close_mechanism`` field on its
  ``extras`` map so an audit can tell a daemon-mediated close from a
  daemonless-with-waiver bypass without re-deriving it. (The daemon path's stamp
  lives in :mod:`tests.daemon` against ``_compute_wave_close_extras``.)

These drive the REAL ``eawf wave close`` CLI under ``EAWF_DAEMONLESS=1`` so the
in-process WAL-backed mutation path runs -- not just the W18 unit functions.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.common import convert_legacy_criterion
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli._mutation import DAEMONLESS_WAIVER_EVENT_TYPE
from eawf.surfaces.cli.app import app
from tests._session_helpers import seed_active_session_on_disk
from tests.conftest import make_claim_criterion

pytestmark = pytest.mark.unit

runner = CliRunner()

_WAVE_ID = "P01-I01-W01"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Temp workspace whose state mutations run daemonless (in-process fallback).

    ``EAWF_EVIDENCE_DIRECT_WRITE`` lets the ``--no-runtime`` waiver evidence row
    land via the direct-append fallback rather than the (absent) daemon RPC.
    """
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setenv("EAWF_EVIDENCE_DIRECT_WRITE", "1")
    yield tmp_path


def _state_path(workspace: Path) -> Path:
    return workspace / ".ea" / "state.json"


def _event_path(workspace: Path) -> Path:
    return store_path(_state_path(workspace), StoreKind.EVENT)


def _bootstrap_claimed_wave(workspace: Path) -> None:
    """Bring state up to one CLAIMED wave under an ACTIVE P01-I01."""
    assert (
        runner.invoke(
            app, ["project", "init", "QR", "--title", "Quant", "--domains", "quant"]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "wave",
                "plan",
                "P01-I01",
                "--id",
                _WAVE_ID,
                "--title",
                "w",
                "--files",
                "src/",
                "--effort-bucket",
                "M",
            ],
        ).exit_code
        == 0
    )
    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    state.waves[_WAVE_ID].success_criteria = [make_claim_criterion()]
    _state_path(workspace).write_bytes(orjson.dumps(state.model_dump(mode="json")))
    seed_active_session_on_disk(_state_path(workspace), session_id="S-1")
    assert runner.invoke(app, ["wave", "claim", _WAVE_ID, "--session", "S-1"]).exit_code == 0


def _attach_gate(workspace: Path) -> None:
    """Attach a properly-paired criterion + gate onto the bootstrapped wave.

    The CLI ``wave plan`` surface takes no gate flags, so the gate is injected
    directly onto the test-fixture state.json (a test fixture, not the project's
    own ``.ea/state.json``). The pair is built via the production
    :func:`convert_legacy_criterion` helper so referential integrity holds.
    """
    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    wave = state.waves[_WAVE_ID]
    criterion, gate = convert_legacy_criterion(
        "the close path stamps the mechanism", index=1, file_scopes=["src/"]
    )
    wave.success_criteria = [criterion]
    wave.gates = [gate]
    _state_path(workspace).write_text(state.model_dump_json(), encoding="utf-8")


def _attach_operator_session(workspace: Path) -> None:
    """Seed an ACTIVE operator session so the ``--no-runtime`` waiver can land.

    The ``--no-runtime`` waiver is operator-only (a waiver names the operator who
    authored it). The CLI ``wave plan`` / ``claim`` bootstrap leaves no operator
    session, so one is injected onto the test-fixture state with
    ``current.active_session_ids`` pointing at it.
    """
    from datetime import UTC, datetime

    from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus
    from eawf.kernel.state.models import AgentSession

    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    session = AgentSession(
        id="OP-1",
        role=AgentSessionRole.OPERATOR,
        runtime="cli",
        scope_id="QR",
        status=AgentSessionStatus.ACTIVE,
        started_at=datetime(2026, 6, 11, tzinfo=UTC),
    )
    state.agent_sessions[session.id] = session
    if session.id not in state.current.active_session_ids:
        state.current.active_session_ids.insert(0, session.id)
    _state_path(workspace).write_text(state.model_dump_json(), encoding="utf-8")


def _close_events(workspace: Path) -> list[dict[str, Any]]:
    """Decode the wave-close mutation event rows from the event store.

    Filters on ``event_type == "wave close"`` (the close mutation event), which
    excludes the daemonless-waiver bypass row -- that carries ``command ==
    "wave close"`` too but a distinct ``event_type`` of
    :data:`DAEMONLESS_WAIVER_EVENT_TYPE`.
    """
    path = _event_path(workspace)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = orjson.loads(line)
        if row["payload"]["event_type"] == "wave close":
            rows.append(row)
    return rows


def _waiver_events(workspace: Path) -> list[dict[str, Any]]:
    """Decode every daemonless-waiver bypass event row from the event store."""
    path = _event_path(workspace)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = orjson.loads(line)
        if row["payload"]["event_type"] == DAEMONLESS_WAIVER_EVENT_TYPE:
            rows.append(row)
    return rows


# --- Criterion 1: daemonless gate-bearing close door (REJECT) --------------


def test_daemonless_gate_bearing_close_rejected_without_waiver(workspace: Path) -> None:
    """Criterion 1 (reject, end-to-end): a gate-bearing daemonless ``wave close``
    WITHOUT ``--no-runtime`` is REJECTED through the LIVE lifecycle close, the
    wave stays un-closed, and no waiver event is written.
    """
    _bootstrap_claimed_wave(workspace)
    _attach_gate(workspace)

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done"])

    assert res.exit_code != 0, res.stdout
    # The wave never flipped to CLOSED -- the close aborted before the mutation.
    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    assert state.waves[_WAVE_ID].status.value == "claimed"
    # No bypass event on the reject path.
    assert _waiver_events(workspace) == []


# --- Criterion 1: daemonless gate-bearing close door (ACCEPT + waiver event)


def test_daemonless_gate_bearing_close_accepted_with_waiver(workspace: Path) -> None:
    """Criterion 1 (accept, end-to-end): a gate-bearing daemonless ``wave close``
    WITH ``--no-runtime`` succeeds, flips the wave to CLOSED, and appends a
    daemonless-waiver EVENT naming the wave + reason.
    """
    _bootstrap_claimed_wave(workspace)
    _attach_gate(workspace)
    _attach_operator_session(workspace)

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done", "--no-runtime"])

    assert res.exit_code == 0, res.stdout
    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    assert state.waves[_WAVE_ID].status.value == "closed"
    waivers = _waiver_events(workspace)
    assert len(waivers) == 1
    extras = waivers[0]["payload"]["extras"]
    assert extras["wave"] == _WAVE_ID
    assert extras["close_mechanism"] == "daemonless-waiver"
    assert extras["reason"]  # non-empty operator reason recorded


# --- Criterion 2: close_mechanism stamped on the in-process close event -----


def test_close_event_carries_mechanism_non_gate_bearing(workspace: Path) -> None:
    """Criterion 2 (in-process path): a NON-gate-bearing daemonless close needs no
    waiver and its close EVENT carries ``close_mechanism = 'daemonless'``.
    """
    _bootstrap_claimed_wave(workspace)  # no gate attached -> not gate-bearing

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done"])

    assert res.exit_code == 0, res.stdout
    closes = _close_events(workspace)
    assert len(closes) == 1
    assert closes[0]["payload"]["extras"]["close_mechanism"] == "daemonless"
    # No bypass event for a non-gate-bearing close.
    assert _waiver_events(workspace) == []


def test_gate_bearing_waived_close_event_carries_waiver_mechanism(workspace: Path) -> None:
    """Criterion 2 (in-process path): the waived gate-bearing close EVENT carries
    ``close_mechanism = 'daemonless-waiver'`` on its extras map.
    """
    _bootstrap_claimed_wave(workspace)
    _attach_gate(workspace)
    _attach_operator_session(workspace)

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done", "--no-runtime"])

    assert res.exit_code == 0, res.stdout
    closes = _close_events(workspace)
    assert len(closes) == 1
    assert closes[0]["payload"]["extras"]["close_mechanism"] == "daemonless-waiver"
