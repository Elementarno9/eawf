"""A headless close is the same close, not a degraded one.

A close issued with no interactive session used to have one route past a
gate-bearing wave's falsifiers: the daemonless bypass, which skips the gates
behind an operator waiver. ``close.host`` is that route's replacement -- the
daemon owns the close, runs every gate in the crash-isolated out-of-process
sandboxed runner, and reports through the same durable attempt row.

The suite pins the three properties that make the replacement real: a paired
run over one fixture wave produces the same gate set, the same receipt ids and
the same ``failure_kind`` whether the close was hosted or issued interactively;
a deliberately crashing hosted gate leaves the daemon answering and the close
resumable to a verdict; and with the daemon available the hosted close takes no
waiver at all, while the daemonless lane still records and counts one.

Every tree in this file is a fixture built under ``tmp_path``. The repository's
own ``.ea`` is never read or written: the state path comes from the fixture
repo, and ``EAWF_RUNTIME_DIR`` is pinned into ``tmp_path`` so the gate child's
sandbox snapshots the fixture rather than any live runtime directory.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    CloseAttemptStatus,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.models import AgentSession, State
from eawf.kernel.store.paths import store_dir, store_path
from eawf.runtime.daemon import gate_execution
from eawf.runtime.daemon.methods import close as close_module
from eawf.runtime.daemon.methods.close import status, submit
from eawf.runtime.daemon.methods.close_hosted import host
from eawf.runtime.daemon.methods.daemon import ping
from eawf.surfaces.cli import errors as cli_errors
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec
from eawf.workflow.verify.hosted_close import count_scope_waivers, resolve_hosted_close
from tests.integration.runtime.daemon.test_close_fault_matrix import (
    _configure_real_fault_matrix,
)
from tests.integration.runtime.daemon.test_close_lock_split import _WAVE
from tests.integration.runtime.daemon.test_durable_close import _repo_with_state

pytestmark = pytest.mark.integration

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="SIGKILL is POSIX-only; Windows crash isolation is a separate probe",
)

#: A module whose mere import SIGKILLs the interpreter that spawned pytest --
#: i.e. the gate runner child. The child therefore dies before it can write a
#: terminal response, which is the exact fault a hosted close must survive.
_CRASH_MODULE_NAME = "test_headless_close_crash_probe.py"
_CRASH_MODULE_SOURCE = "import os\nimport signal\n\nos.kill(os.getppid(), signal.SIGKILL)\n"
_CRASH_ARGV = ["pytest", "-p", "no:cacheprovider", "-q", _CRASH_MODULE_NAME]

_CLOSE_PARAMS: dict[str, Any] = {
    "wave_id": _WAVE,
    "outcome": "verified integrated revision",
    "no_runtime_waiver": True,
}


def _params(repo: Path) -> dict[str, Any]:
    """Return the close params for *repo* -- identical across both lanes."""
    return {**_CLOSE_PARAMS, "repo_root": str(repo)}


def _pin_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the gate sandbox's snapshot source at a fixture runtime dir."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))


async def _wait_terminal(ctx: Any, repo: Path, ref: str) -> dict[str, Any]:
    """Poll durable status until the attempt reports a terminal state."""
    for _ in range(600):
        result = await status(ctx, {"ref": ref, "repo_root": str(repo)})
        if result["attempt"]["status"] in {
            CloseAttemptStatus.CLOSED.value,
            CloseAttemptStatus.BLOCKED.value,
            CloseAttemptStatus.STALE.value,
            CloseAttemptStatus.FAILED.value,
            CloseAttemptStatus.CANCELLED.value,
        }:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError(f"close attempt {ref!r} never reached a terminal state")


def _receipt_ids(state_path: Path) -> list[str]:
    """Return every persisted gate-receipt id, in file order."""
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    if not path.is_file():
        return []
    return [
        str(orjson.loads(line)["id"]) for line in path.read_bytes().splitlines() if line.strip()
    ]


def _lane_fingerprint(state_path: Path, attempt: dict[str, Any]) -> dict[str, Any]:
    """Return the facts a hosted and an interactive close must agree on."""
    return {
        "required_gate_ids": sorted(attempt["required_gate_ids"]),
        "gate_receipt_ids": sorted(attempt["gate_receipt_ids"]),
        "persisted_receipt_ids": sorted(_receipt_ids(state_path)),
        "failure_kind": attempt["failure_kind"],
        "status": attempt["status"],
    }


def _rewind_close_residue(ctx: Any, state_path: Path, *, snapshot: bytes) -> None:
    """Restore the pre-close fixture so the second lane starts from scratch.

    Both lanes must EXECUTE their gates rather than resolve a receipt or a
    cached mutation the other lane already produced; otherwise the receipt-id
    match would be an artefact of reuse instead of proof that the two lanes
    derive the same freshness key. Dropping the idempotency cache alongside the
    on-disk residue is what a daemon restart between the two closes would do.
    """
    state_path.write_bytes(snapshot)
    local = state_path.parent / "local"
    for name in ("gate-claims", "gate-children", "gate-diagnostics", "close-logs"):
        shutil.rmtree(local / name, ignore_errors=True)
    for kind in (StoreKind.GATE_RECEIPT, StoreKind.EVIDENCE, StoreKind.AUDITOR_REPORT):
        candidate = store_path(state_path, kind)
        if candidate.is_file():
            candidate.unlink()
    ctx.idempotency_cache.clear()
    close_module._CLOSE_TASKS.clear()


def test_close_host_matches_close_submit_over_one_fixture_wave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hosted close and an interactive close agree on gates and receipts."""
    _pin_runtime_dir(tmp_path, monkeypatch)
    repo, state_path, ctx = _repo_with_state(tmp_path)
    gate_executions, _auditor = _configure_real_fault_matrix(
        repo=repo,
        state_path=state_path,
        monkeypatch=monkeypatch,
    )
    snapshot = state_path.read_bytes()

    async def _run(entry: Any) -> dict[str, Any]:
        started = await entry(ctx, _params(repo))
        terminal = await _wait_terminal(ctx, repo, str(started["attempt"]["id"]))
        return {"submitted": started, "terminal": terminal}

    hosted = asyncio.run(_run(host))
    hosted_fingerprint = _lane_fingerprint(state_path, hosted["terminal"]["attempt"])
    hosted_attempt_id = str(hosted["submitted"]["attempt"]["id"])

    _rewind_close_residue(ctx, state_path, snapshot=snapshot)

    interactive = asyncio.run(_run(submit))
    interactive_fingerprint = _lane_fingerprint(state_path, interactive["terminal"]["attempt"])

    assert hosted["submitted"]["hosted"] is True
    assert hosted_fingerprint["status"] == CloseAttemptStatus.CLOSED.value
    assert hosted_fingerprint["gate_receipt_ids"]
    assert hosted_attempt_id == str(interactive["submitted"]["attempt"]["id"])
    assert gate_executions == ["G-MATRIX", "G-MATRIX"]
    assert hosted_fingerprint == interactive_fingerprint


def test_close_host_matches_close_submit_failure_kind_on_a_refused_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both lanes refuse a failing gate with the same durable failure kind."""
    _pin_runtime_dir(tmp_path, monkeypatch)
    repo, state_path, ctx = _repo_with_state(tmp_path)
    _configure_real_fault_matrix(repo=repo, state_path=state_path, monkeypatch=monkeypatch)
    state = State.model_validate_json(state_path.read_bytes())
    gate = state.waves[_WAVE].gates[0]
    state.waves[_WAVE] = state.waves[_WAVE].model_copy(
        update={"gates": [gate.model_copy(update={"args": {"path": "absent-payload.txt"}})]}
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    snapshot = state_path.read_bytes()

    async def _run(entry: Any) -> dict[str, Any]:
        started = await entry(ctx, _params(repo))
        return await _wait_terminal(ctx, repo, str(started["attempt"]["id"]))

    hosted = _lane_fingerprint(state_path, asyncio.run(_run(host))["attempt"])
    _rewind_close_residue(ctx, state_path, snapshot=snapshot)
    interactive = _lane_fingerprint(state_path, asyncio.run(_run(submit))["attempt"])

    assert hosted["status"] == CloseAttemptStatus.BLOCKED.value
    assert hosted["failure_kind"] == "work_rejected"
    assert hosted == interactive


@_POSIX_ONLY
def test_close_host_crash_keeps_daemon_serving_and_resumes_to_a_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hosted gate that hard-kills its child leaves the daemon answering.

    The daemon surviving is this interpreter surviving: had the gate run in
    process, ``SIGKILL`` would have reached the test runner and there would be
    no ``daemon.ping`` left to answer.
    """
    _pin_runtime_dir(tmp_path, monkeypatch)
    repo, state_path, ctx = _repo_with_state(tmp_path)
    _configure_real_fault_matrix(repo=repo, state_path=state_path, monkeypatch=monkeypatch)

    real_runner = gate_execution.run_gate_out_of_process
    crashed: list[str] = []

    def _crash_once(
        spec: CheckSpec,
        *,
        cwd: Path,
        context: gate_execution.GateExecutionContext,
        criterion_id: str,
        gate_id: str,
    ) -> CheckResult:
        """Run the first hosted gate as a child that SIGKILLs itself."""
        if not crashed:
            crashed.append(gate_id)
            (cwd / _CRASH_MODULE_NAME).write_text(_CRASH_MODULE_SOURCE, encoding="utf-8")
            spec = CheckSpec(
                kind="command_exit_zero",
                name=spec.name,
                args={"argv": _CRASH_ARGV, "scope": "all"},
                freshness=spec.freshness,
            )
        return real_runner(
            spec,
            cwd=cwd,
            context=context,
            criterion_id=criterion_id,
            gate_id=gate_id,
        )

    monkeypatch.setattr(gate_execution, "run_gate_out_of_process", _crash_once)

    async def body() -> tuple[dict[str, Any], dict[str, Any]]:
        started = await host(ctx, _params(repo))
        terminal = await _wait_terminal(ctx, repo, str(started["attempt"]["id"]))
        return terminal, await ping(ctx, {})

    terminal, pong = asyncio.run(body())

    assert crashed == ["G-MATRIX"]
    assert pong["pid"] == os.getpid()
    assert pong["protocol_version"] == ctx.protocol_version
    assert terminal["attempt"]["status"] == CloseAttemptStatus.CLOSED.value
    assert terminal["attempt"]["gate_receipt_ids"]
    assert terminal["attempt"]["infrastructure_retry_budget_remaining"] == 0
    final = State.model_validate_json(state_path.read_bytes())
    assert final.waves[_WAVE].status is WaveStatus.CLOSED


def test_close_host_takes_no_waiver_when_the_daemon_is_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hosted close runs its gates instead of waiving them."""
    _pin_runtime_dir(tmp_path, monkeypatch)
    repo, state_path, ctx = _repo_with_state(tmp_path)
    _configure_real_fault_matrix(repo=repo, state_path=state_path, monkeypatch=monkeypatch)

    async def body() -> tuple[dict[str, Any], dict[str, Any]]:
        started = await host(ctx, _params(repo))
        return started, await _wait_terminal(ctx, repo, str(started["attempt"]["id"]))

    started, terminal = asyncio.run(body())

    assert started["hosted"] is True
    assert started["waiver_count"] == 0
    assert terminal["attempt"]["status"] == CloseAttemptStatus.CLOSED.value
    assert count_scope_waivers(store_dir(state_path), scope_id=_WAVE) == 0
    decision = resolve_hosted_close(mode="hosted", daemon_available=True, gate_bearing=True)
    assert decision.hosted is True
    assert decision.waiver_required is False


def test_daemonless_waiver_records_and_counts_when_the_daemon_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a daemon the bypass lane still records its waiver and counts it."""
    from eawf.surfaces.cli._mutation import (
        DAEMONLESS_WAIVER_EVENT_TYPE,
        enforce_daemonless_close_waiver,
    )
    from eawf.workflow.lifecycle.waivers import WaiverInput, apply_waiver

    _pin_runtime_dir(tmp_path, monkeypatch)
    repo, state_path, _ctx = _repo_with_state(tmp_path)
    _configure_real_fault_matrix(repo=repo, state_path=state_path, monkeypatch=monkeypatch)
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setenv("EAWF_EVIDENCE_DIRECT_WRITE", "1")
    state = State.model_validate_json(state_path.read_bytes())
    state.agent_sessions["AS-OPERATOR"] = AgentSession(
        id="AS-OPERATOR",
        role=AgentSessionRole.OPERATOR,
        runtime="cli",
        scope_id=_WAVE,
        status=AgentSessionStatus.ACTIVE,
        started_at=datetime.now(UTC),
    )
    wave = state.waves[_WAVE]

    decision = resolve_hosted_close(mode="hosted", daemon_available=False, gate_bearing=True)
    assert decision.hosted is False
    assert decision.waiver_required is True

    with pytest.raises(cli_errors.UserError, match="gate-bearing"):
        enforce_daemonless_close_waiver(wave, state_path=state_path, waived=False)

    mechanism = enforce_daemonless_close_waiver(
        wave,
        state_path=state_path,
        waived=True,
        reason="runtime capture unavailable; operator supplied --no-runtime",
    )
    apply_waiver(
        state,
        wave_id=_WAVE,
        waiver=WaiverInput(gate_id="runtime-zero", reason="runtime capture unavailable"),
        operator_identity="AS-OPERATOR",
        mode="B",
        state_path=state_path,
        repo_root=repo,
    )

    assert mechanism == "daemonless-waiver"
    events = [
        orjson.loads(line)
        for line in store_path(state_path, StoreKind.EVENT).read_bytes().splitlines()
        if line.strip()
    ]
    waiver_events = [
        row for row in events if row["payload"]["event_type"] == DAEMONLESS_WAIVER_EVENT_TYPE
    ]
    assert len(waiver_events) == 1
    assert waiver_events[0]["payload"]["extras"]["wave"] == _WAVE
    assert count_scope_waivers(store_dir(state_path), scope_id=_WAVE) == 1


def test_resolve_hosted_close_gateless_daemonless_needs_no_waiver() -> None:
    """A wave with no gates has nothing to falsify, so the bypass costs nothing."""
    decision = resolve_hosted_close(
        mode="interactive",
        daemon_available=False,
        gate_bearing=False,
    )

    assert decision.hosted is False
    assert decision.waiver_required is False
    assert "no gate to run" in decision.reason


def test_count_scope_waivers_missing_store_dir_is_zero(tmp_path: Path) -> None:
    """A store that was never appended to counts as zero, not as an error."""
    assert count_scope_waivers(tmp_path / "absent", scope_id=_WAVE) == 0


def test_count_scope_waivers_empty_scope_id_raises(tmp_path: Path) -> None:
    """An empty scope id cannot address a wave, so it is refused at the door."""
    with pytest.raises(ValueError, match="non-empty wave id"):
        count_scope_waivers(tmp_path, scope_id="")


def test_count_scope_waivers_non_directory_store_raises(tmp_path: Path) -> None:
    """A store root that is a file is a caller bug, not an empty ledger."""
    not_a_dir = tmp_path / "store"
    not_a_dir.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        count_scope_waivers(not_a_dir, scope_id=_WAVE)


def test_close_host_rejects_an_unknown_parameter(tmp_path: Path) -> None:
    """``close.host`` forbids extras, so a typo cannot silently change a close."""
    repo, _state_path, ctx = _repo_with_state(tmp_path)

    with pytest.raises(ValidationError):
        asyncio.run(host(ctx, {**_params(repo), "session_id": "AS-01"}))


def test_close_host_rejects_an_unknown_wave(tmp_path: Path) -> None:
    """A hosted close of an unknown wave fails exactly as a submitted one does."""
    repo, _state_path, ctx = _repo_with_state(tmp_path)

    with pytest.raises(ValueError, match="unknown wave"):
        asyncio.run(host(ctx, {**_params(repo), "wave_id": "P30-I23-W77"}))


def test_submit_hosted_close_names_the_bypass_when_the_daemon_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable daemon refuses the hosted close and quotes the lane."""
    from eawf.surfaces.cli.commands import close as close_cli
    from eawf.surfaces.cli.flags import GlobalFlags

    def _unreachable(*, method: str, params: dict[str, Any], flags: GlobalFlags) -> dict[str, Any]:
        del params, flags
        raise cli_errors.DaemonUnreachable(f"daemon unavailable for {method}")

    monkeypatch.setattr(close_cli, "call_close_rpc", _unreachable)

    with pytest.raises(cli_errors.DaemonUnreachable, match="daemonless bypass lane"):
        close_cli.submit_hosted_close(
            wave_id=_WAVE,
            params=dict(_CLOSE_PARAMS),
            flags=GlobalFlags(),
        )
