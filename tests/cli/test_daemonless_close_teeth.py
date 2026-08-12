"""End-to-end teeth for the daemonless / in-process wave close.

Before this wave the ``EAWF_DAEMONLESS`` (and daemon-down fallback) close path
computed readiness but only LOGGED ``close_advisory`` warnings and applied the
close unconditionally, so every enforcing daemonless close slipped its
falsifiers (the strategy-campaign 38/40 bypass). This drives the REAL
``eawf wave close`` CLI under ``EAWF_DAEMONLESS=1`` and pins the three teeth:

* **CR-01 (deterministic block).** A close of a wave with a FAILING
  ``command_exit_zero`` gate under an enforcing profile aborts non-zero with NO
  state write and NO waiver -- the W06 pre-flight runs the deterministic gate
  and BLOCKS on the grounded gate failure (not the blunt gate-bearing door).
* **CR-02 (waiver door preserved).** The same close succeeds with the
  ``--no-runtime`` operator waiver, flips the wave CLOSED, and stamps the
  ``close_mechanism = daemonless-waiver`` bypass event + close-event extra.
* **CR-03 (verdict READ gate).** A verdict-always wave with no fresh persisted
  auditor verdict is refused -- the synchronous verdict read gate runs
  in-process (the daemonless path cannot spawn the auditor) and the refusal
  message says to close via the daemon or waive.

The companion unit coverage of the bypass-door function + close-mechanism stamp
lives in :mod:`tests.unit.cli.test_daemonless_close_waiver`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.common import CriterionSpec, GateSpec, QualityDimension
from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus, StoreKind
from eawf.kernel.state.models import AgentSession, State
from eawf.kernel.store.paths import store_path
from eawf.runtime.lock import portalock
from eawf.surfaces.cli._mutation import DAEMONLESS_WAIVER_EVENT_TYPE
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import lifecycle_wave
from eawf.workflow.verify.models import CloseReadiness
from tests._session_helpers import seed_active_session_on_disk
from tests.conftest import make_claim_criterion

pytestmark = pytest.mark.unit

runner = CliRunner()

_WAVE_ID = "P01-I01-W01"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Temp workspace whose state mutations run daemonless (in-process fallback).

    ``EAWF_EVIDENCE_DIRECT_WRITE`` lets the ``--no-runtime`` waiver evidence row
    land via the direct-append fallback rather than the (absent) daemon RPC. The
    workspace is a real git repo so the deterministic-floor gate runner has a
    tree to execute ``git`` argvs in.
    """
    _git_init(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setenv("EAWF_EVIDENCE_DIRECT_WRITE", "1")
    yield tmp_path


def _git_init(root: Path) -> None:
    """Initialise *root* as a minimal git repo with one commit."""
    import subprocess

    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "seed"], check=True)


def _state_path(workspace: Path) -> Path:
    return workspace / ".ea" / "state.json"


def _event_path(workspace: Path) -> Path:
    return store_path(_state_path(workspace), StoreKind.EVENT)


def _write_enforce_profile(workspace: Path) -> None:
    """Write a workspace profile with ``verify.enforce: true`` (empty floor pack).

    An empty floor pack keeps a no-gate wave's readiness vacuously ready, so the
    verdict-gate test (CR-03) isolates the verdict read gate from the floor
    pack. The ``argv_allowlist`` carries ``git`` for the fixture command gates.
    """
    profile_dir = workspace / ".ea" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (workspace / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled:\n    - enforcing\n", encoding="utf-8"
    )
    profile_dir.joinpath("enforcing.yaml").write_text(
        "name: enforcing\nverify:\n  enforce: true\n  argv_allowlist:\n    - git\n",
        encoding="utf-8",
    )


def _bootstrap_claimed_wave(workspace: Path, *, effort_bucket: str = "M") -> None:
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
                effort_bucket,
            ],
        ).exit_code
        == 0
    )
    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    state.waves[_WAVE_ID].success_criteria = [make_claim_criterion()]
    _state_path(workspace).write_bytes(orjson.dumps(state.model_dump(mode="json")))
    seed_active_session_on_disk(_state_path(workspace), session_id="S-1")
    assert runner.invoke(app, ["wave", "claim", _WAVE_ID, "--session", "S-1"]).exit_code == 0
    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    state.waves[_WAVE_ID].success_criteria = []
    _state_path(workspace).write_bytes(orjson.dumps(state.model_dump(mode="json")))


def _attach_failing_command_gate(workspace: Path) -> None:
    """Attach a deterministic criterion + FAILING ``command_exit_zero`` gate.

    The CLI ``wave plan`` surface takes no gate flags, so the pair is injected
    directly onto the test-fixture state.json. ``git show <no-such-ref>`` exits
    non-zero in any git tree, so the deterministic floor scores the gate FAIL.
    The wave is thereby both gate-bearing (arms the daemonless-waiver door) and
    not-ready under the enforcing profile.
    """
    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    wave = state.waves[_WAVE_ID]
    criterion = CriterionSpec(
        id="CR-01",
        text="the wave test suite exits zero",
        kind="behavior",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["GATE-01"],
        required=True,
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="command_exit_zero gate argv exits zero under the deterministic floor",
    )
    gate = GateSpec(
        id="GATE-01",
        criterion_id="CR-01",
        kind="command_exit_zero",
        args={"argv": ["git", "show", "no-such-ref-w20-teeth"]},
        policy="block",
        cadence="every-wave",
    )
    wave.success_criteria = [criterion]
    wave.gates = [gate]
    _state_path(workspace).write_text(state.model_dump_json(), encoding="utf-8")


def _attach_operator_session(workspace: Path) -> None:
    """Seed an ACTIVE operator session so the ``--no-runtime`` waiver can land."""
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
    """Decode the ``wave close`` mutation event rows (excludes the waiver row)."""
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


def _wave_status(workspace: Path) -> str:
    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    return state.waves[_WAVE_ID].status.value


# --- CR-01: failing deterministic gate BLOCKS the daemonless close ----------


def test_daemonless_failing_gate_close_blocks_with_no_state_and_no_waiver(
    workspace: Path,
) -> None:
    """CR-01: an ``EAWF_DAEMONLESS`` close of a wave with a failing
    ``command_exit_zero`` gate exits non-zero, leaves the wave un-closed, and
    stamps NO waiver -- the pre-flight deterministic gate refuses the close.
    """
    _bootstrap_claimed_wave(workspace)
    _write_enforce_profile(workspace)
    _attach_failing_command_gate(workspace)

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done"])

    assert res.exit_code != 0, res.stdout
    # No state write: the wave never flipped to CLOSED.
    assert _wave_status(workspace) == "claimed"
    # No waiver: neither a daemonless-waiver bypass event nor a waiver row.
    assert _waiver_events(workspace) == []
    # The refusal is the grounded deterministic pre-flight (names the criterion
    # gate that failed), not the blunt gate-bearing bypass-door message.
    assert "CR-01" in res.stdout
    assert "readiness enforcement failed" in res.stdout


# --- CR-02: the same close succeeds under an explicit waiver -----------------


def test_daemonless_failing_gate_close_succeeds_with_waiver(workspace: Path) -> None:
    """CR-02: the same failing-gate close succeeds with ``--no-runtime``, flips
    the wave CLOSED, and stamps ``close_mechanism = daemonless-waiver`` on both
    the bypass event and the close event.
    """
    _bootstrap_claimed_wave(workspace)
    _write_enforce_profile(workspace)
    _attach_failing_command_gate(workspace)
    _attach_operator_session(workspace)

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done", "--no-runtime"])

    assert res.exit_code == 0, res.stdout
    assert _wave_status(workspace) == "closed"
    # The waiver door fired: one bypass event naming the wave + reason.
    waivers = _waiver_events(workspace)
    assert len(waivers) == 1
    extras = waivers[0]["payload"]["extras"]
    assert extras["wave"] == _WAVE_ID
    assert extras["close_mechanism"] == "daemonless-waiver"
    assert extras["reason"]  # non-empty operator reason recorded
    # The close event carries the daemonless-waiver mechanism stamp.
    closes = _close_events(workspace)
    assert len(closes) == 1
    assert closes[0]["payload"]["extras"]["close_mechanism"] == "daemonless-waiver"


# --- CR-03: a verdict-always wave is refused without a fresh verdict ---------


def test_daemonless_verdict_always_close_refused_without_verdict(workspace: Path) -> None:
    """CR-03: a verdict-always (XL) wave with no fresh auditor verdict is
    refused daemonless -- the synchronous verdict read gate runs in-process and
    the refusal says to close via the daemon or waive.
    """
    _bootstrap_claimed_wave(workspace, effort_bucket="XL")
    _write_enforce_profile(workspace)

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done"])

    assert res.exit_code != 0, res.stdout
    assert _wave_status(workspace) == "claimed"
    assert "fresh auditor verdict" in res.stdout
    # The refusal points at the two ways forward: the daemon, or a waiver.
    assert "daemon" in res.stdout
    assert "waive" in res.stdout


def test_daemonless_verdict_always_close_succeeds_with_waiver(workspace: Path) -> None:
    """CR-03 (waiver override): the verdict-always close proceeds under
    ``--no-runtime`` -- the operator explicitly force-closes the un-audited wave.
    """
    _bootstrap_claimed_wave(workspace, effort_bucket="XL")
    _write_enforce_profile(workspace)
    _attach_operator_session(workspace)

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done", "--no-runtime"])

    assert res.exit_code == 0, res.stdout
    assert _wave_status(workspace) == "closed"


def test_daemonless_mechanical_wave_not_verdict_gated(workspace: Path) -> None:
    """A mechanical (non-``always``) wave with no gate closes daemonless without
    a verdict -- the verdict gate hard-blocks only the ``"always"`` subset, so a
    small executor wave is not refused for lacking a verdict (daemon parity).
    """
    _bootstrap_claimed_wave(workspace, effort_bucket="S")
    _write_enforce_profile(workspace)

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done"])

    assert res.exit_code == 0, res.stdout
    assert _wave_status(workspace) == "closed"
    assert _waiver_events(workspace) == []


def test_daemonless_close_preflight_does_not_hold_state_lock(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback verifier can acquire the state lock while it runs."""
    _bootstrap_claimed_wave(workspace, effort_bucket="XL")
    _write_enforce_profile(workspace)
    calls = 0

    def _probe(
        state: State,
        *,
        wave_id: str,
        state_path: Path,
        repo_root: Path,
        config_root: Path,
        waived: bool,
    ) -> CloseReadiness | None:
        del state, wave_id, repo_root, config_root, waived
        nonlocal calls
        calls += 1
        with portalock.acquire(state_path, timeout=0.1):
            pass
        return None

    monkeypatch.setattr(lifecycle_wave, "_run_daemonless_close_preflight", _probe)

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done"])

    assert res.exit_code == 0, res.stdout
    assert calls == 1
    assert _wave_status(workspace) == "closed"


def test_daemonless_close_preflight_rejects_target_wave_drift(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target-wave change after preflight is refused instead of applied."""
    _bootstrap_claimed_wave(workspace, effort_bucket="XL")
    _write_enforce_profile(workspace)

    def _drift_target(
        state: State,
        *,
        wave_id: str,
        state_path: Path,
        repo_root: Path,
        config_root: Path,
        waived: bool,
    ) -> CloseReadiness | None:
        del state, repo_root, config_root, waived
        live = State.model_validate_json(state_path.read_bytes())
        live.waves[wave_id].title = "changed during preflight"
        live.updated_at = datetime.now(UTC)
        state_path.write_text(live.model_dump_json(), encoding="utf-8")
        return None

    monkeypatch.setattr(
        lifecycle_wave,
        "_run_daemonless_close_preflight",
        _drift_target,
    )

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done"])

    assert res.exit_code != 0
    assert "close_preflight_stale" in res.stdout
    final = State.model_validate_json(_state_path(workspace).read_bytes())
    assert final.waves[_WAVE_ID].status.value == "claimed"
    assert final.waves[_WAVE_ID].title == "changed during preflight"
