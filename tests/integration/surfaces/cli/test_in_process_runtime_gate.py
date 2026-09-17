"""The in-process wave close measures its runtime and runs the zero-runtime gate.

The daemon close derives the wave's runtime delta from ``runtime_baseline`` and
``runtime_latest`` and refuses a silent zero through
:func:`eawf.runtime.daemon.methods.state_close.enforce_nonzero_runtime_close`.
The in-process close did neither: it called ``close_wave`` with no measured
actual, so every daemonless or transport-fallback close recorded
``elapsed_eu=0.0`` and no enforcing profile could refuse it.

:func:`eawf.surfaces.cli.commands.lifecycle_wave._close_and_pin` now measures
the delta the same way and gates on it. These tests drive the real daemonless
``eawf wave close`` and, for the transport-fallback close (which no CLI
invocation in this suite can provoke without a daemon), call ``_close_and_pin``
directly with ``transport_fallback=True``. Every tree lives under ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus
from eawf.kernel.state.models import AgentSession, RuntimeBaseline, RuntimeLatest, State
from eawf.observability.telemetry.join import DEFAULT_EU_MINUTES
from eawf.runtime.daemon.methods import state_close
from eawf.surfaces.cli._mutation import CloseMechanism
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.lifecycle_wave import _close_and_pin
from eawf.workflow.lifecycle.transitions import LifecycleError
from tests._session_helpers import seed_active_session_on_disk
from tests.conftest import make_claim_criterion

runner = CliRunner()

_WAVE_ID = "P01-I01-W01"
_CLAIMED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
#: Fifteen minutes of measured agent runtime: half of one default effort unit.
_MEASURED_MS = 15 * 60 * 1000
_NO_RUNTIME_MESSAGE = "has no captured runtime"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Return a git-backed temp workspace with one CLAIMED gate-free S wave."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setenv("EAWF_EVIDENCE_DIRECT_WRITE", "1")
    for args in (
        ["project", "init", "QR", "--title", "Quant", "--domains", "quant"],
        ["phase", "open", "--auto", "--title", "P1"],
        ["iter", "open", "--phase", "P01", "--title", "I1"],
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            _WAVE_ID,
            "--title",
            "runtime gate fixture",
            "--files",
            "src/",
            "--effort-bucket",
            "S",
        ],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stdout
    state = _load(tmp_path)
    state.waves[_WAVE_ID].success_criteria = [make_claim_criterion()]
    _save(tmp_path, state)
    seed_active_session_on_disk(state_path, session_id="S-1")
    result = runner.invoke(app, ["wave", "claim", _WAVE_ID, "--session", "S-1"])
    assert result.exit_code == 0, result.stdout
    state = _load(tmp_path)
    state.waves[_WAVE_ID].success_criteria = []
    operator = AgentSession(
        id="OP-1",
        role=AgentSessionRole.OPERATOR,
        runtime="cli",
        scope_id="QR",
        status=AgentSessionStatus.ACTIVE,
        started_at=_CLAIMED_AT,
    )
    state.agent_sessions[operator.id] = operator
    state.current.active_session_ids.insert(0, operator.id)
    _save(tmp_path, state)
    yield tmp_path


def _state_path(workspace: Path) -> Path:
    return workspace / ".ea" / "state.json"


def _load(workspace: Path) -> State:
    return State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))


def _save(workspace: Path, state: State) -> None:
    _state_path(workspace).write_text(state.model_dump_json(), encoding="utf-8")


def _enable_profile(workspace: Path, *, enforce: bool) -> None:
    """Enable one workspace profile whose verify block sets *enforce*."""
    profile_dir = workspace / ".ea" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (workspace / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled:\n    - metered\n", encoding="utf-8"
    )
    (profile_dir / "metered.yaml").write_text(
        f"name: metered\nverify:\n  enforce: {str(enforce).lower()}\n", encoding="utf-8"
    )


def _seed_captured_runtime(workspace: Path) -> None:
    """Record a claim baseline and a later capture that measured real runtime."""
    state = _load(workspace)
    wave = state.waves[_WAVE_ID]
    wave.runtime_baseline = RuntimeBaseline(
        api_duration_ms=0,
        total_duration_ms=0,
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        session_id="SESSION-A",
        captured_at=_CLAIMED_AT,
    )
    wave.runtime_latest = RuntimeLatest(
        api_duration_ms=_MEASURED_MS,
        total_duration_ms=_MEASURED_MS * 2,
        cost_usd=1.25,
        input_tokens=100,
        output_tokens=400,
        cache_creation_input_tokens=500,
        cache_read_input_tokens=9_000,
        session_id="SESSION-A",
        captured_at=_CLAIMED_AT + timedelta(minutes=40),
    )
    _save(workspace, state)


def _close(workspace: Path, *extra: str) -> tuple[int, str]:
    result = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done", *extra])
    return result.exit_code, result.output


def _pin_in_process(
    workspace: Path, *, transport_fallback: bool, wave_id: str = _WAVE_ID
) -> tuple[State, list[CloseMechanism]]:
    """Run ``_close_and_pin`` over the on-disk state; return the state and stamp."""
    state = _load(workspace)
    holder: list[CloseMechanism] = []
    _close_and_pin(
        state,
        wave_id=wave_id,
        outcome="done",
        tokens_consumed=None,
        no_runtime=False,
        commit_sha=None,
        commit_identity_digest=None,
        state_path=_state_path(workspace),
        repo_root=workspace,
        transport_fallback=transport_fallback,
        mechanism_holder=holder,
    )
    return state, holder


# --- the daemonless CLI close --------------------------------------------------


def test_close_and_pin_refuses_zero_runtime_under_enforcing_profile(workspace: Path) -> None:
    """A silent zero-runtime close is refused, and nothing is written."""
    _enable_profile(workspace, enforce=True)

    exit_code, output = _close(workspace)

    assert exit_code != 0, output
    assert _NO_RUNTIME_MESSAGE in output
    loaded = _load(workspace)
    assert loaded.waves[_WAVE_ID].status.value == "claimed"
    assert _WAVE_ID not in (loaded.actuals or {})


def test_close_and_pin_records_measured_elapsed_eu(workspace: Path) -> None:
    """A captured delta lands the close with the delta's EU, cost and tokens."""
    _enable_profile(workspace, enforce=True)
    _seed_captured_runtime(workspace)

    exit_code, output = _close(workspace)

    assert exit_code == 0, output
    loaded = _load(workspace)
    assert loaded.waves[_WAVE_ID].status.value == "closed"
    actual = (loaded.actuals or {})[_WAVE_ID]
    assert actual.elapsed_eu > 0.0
    assert actual.elapsed_eu == pytest.approx(_MEASURED_MS / 60_000 / DEFAULT_EU_MINUTES)
    assert actual.agent_runtime_eu == pytest.approx(actual.elapsed_eu)
    assert actual.actual_cost_usd == pytest.approx(1.25)
    # Cache reads are billed but are not work.
    assert actual.actual_tokens == 100 + 400 + 500


def test_close_and_pin_honours_runtime_waiver(workspace: Path) -> None:
    """Boundary: ``--no-runtime`` still waives the missing capture."""
    _enable_profile(workspace, enforce=True)

    exit_code, output = _close(workspace, "--no-runtime")

    assert exit_code == 0, output
    loaded = _load(workspace)
    assert loaded.waves[_WAVE_ID].status.value == "closed"
    assert (loaded.actuals or {})[_WAVE_ID].elapsed_eu == pytest.approx(0.0)


def test_close_and_pin_warns_on_zero_runtime_under_advisory_profile(workspace: Path) -> None:
    """Boundary: a profile that enforces nothing lets the zero close, with an advisory."""
    _enable_profile(workspace, enforce=False)

    exit_code, output = _close(workspace)

    assert exit_code == 0, output
    assert "elapsed_eu=0.0" in output
    assert _load(workspace).waves[_WAVE_ID].status.value == "closed"


def test_close_and_pin_pre_fix_ungated_close_lands_zero(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeding the old ungated close back in lands the silent zero the gate refuses."""
    _enable_profile(workspace, enforce=True)
    monkeypatch.setattr(state_close, "enforce_nonzero_runtime_close", lambda *_a, **_k: None)

    exit_code, output = _close(workspace)

    assert exit_code == 0, output
    assert _load(workspace).waves[_WAVE_ID].status.value == "closed"


# --- the transport-fallback close ----------------------------------------------


def test_close_and_pin_transport_fallback_refuses_zero_runtime(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport fallback skips only the bypass door, not the runtime gate."""
    monkeypatch.delenv("EAWF_DAEMONLESS")
    _enable_profile(workspace, enforce=True)

    with pytest.raises(LifecycleError, match=_NO_RUNTIME_MESSAGE):
        _pin_in_process(workspace, transport_fallback=True)


def test_close_and_pin_transport_fallback_records_measured_elapsed_eu(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport fallback with a captured delta closes on the measured EU."""
    monkeypatch.delenv("EAWF_DAEMONLESS")
    _enable_profile(workspace, enforce=True)
    _seed_captured_runtime(workspace)

    state, holder = _pin_in_process(workspace, transport_fallback=True)

    assert holder == ["daemon-fallback"]
    assert state.waves[_WAVE_ID].status.value == "closed"
    assert (state.actuals or {})[_WAVE_ID].elapsed_eu == pytest.approx(
        _MEASURED_MS / 60_000 / DEFAULT_EU_MINUTES
    )


def test_close_and_pin_rejects_unknown_wave(workspace: Path) -> None:
    """Error path: an unknown wave is named as such, not as a missing runtime."""
    _enable_profile(workspace, enforce=True)

    with pytest.raises(LifecycleError, match="unknown wave 'P99-I99-W99'"):
        _pin_in_process(workspace, transport_fallback=False, wave_id="P99-I99-W99")
