"""Close gates execute against a sandbox, never the live runtime tree.

A gate child used to inherit the daemon's runtime directory and state path, so
a gate suite that exercises eawf's own RPCs drove them against the live ledger
and the live dispatch loop. The suite below pins the two properties that close
that hole: the child is bound to a runtime directory that is a SNAPSHOT COPY,
and a gate that flips ``dispatch_paused`` and appends to the event store leaves
the live tree byte identical.

Two nested processes carry that boundary and both are observed here. The gate
RUNNER child is launched by ``run_gate_out_of_process`` with the sandbox bound
into ``EAWF_RUNTIME_DIR`` / ``EA_STATE``; a delegating spy on the spawn records
what the real child receives, because the sandbox is deleted the moment the
gate returns. The gate COMMAND itself is then launched from inside that runner
under the no-auth env-scrub floor, and reports what it resolved by writing a
JSON receipt the parent asserts on.

Each probe rides a planted pytest module rather than a bare interpreter: the
gate runner spawns only argv heads the L0 argv policy allowlists, and a
path-qualified interpreter is not one of them.

Every "live" tree in this file is a fixture directory built under ``tmp_path``.
The repository's own ``.ea`` is never read or written: the gate runner reaches
the live tree only through ``GateExecutionContext.state_path`` and the
``EAWF_RUNTIME_DIR`` env var, both of which each test points at its fixture.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.runtime.daemon import gate_execution
from eawf.workflow.audit_dsl.models import CheckSpec

pytestmark = pytest.mark.integration

_T0 = "2026-09-08T00:00:00+00:00"

#: Probe gate: records what the runtime-dir and state resolvers answer INSIDE
#: the gate command, so the parent can assert no live seam reached it.
_PROBE_MODULE = "test_gate_isolation_probe.py"
_PROBE_BODY = '''
import json
import os
from pathlib import Path

from eawf.kernel.state.resolve import resolve_with_reason
from eawf.runtime.daemon.runtime_dir import runtime_dir, socket_path


def test_report_resolved_seams() -> None:
    """Write the gate's own view of the runtime dir and the ledger."""
    resolved = runtime_dir()
    state_path, reason = resolve_with_reason(None)
    Path(OBSERVED).write_text(
        json.dumps(
            {
                "runtime_dir": str(resolved),
                "socket_path": str(socket_path()),
                "state_path": str(state_path),
                "reason": str(reason),
                "runtime_dir_env": os.environ.get("EAWF_RUNTIME_DIR", ""),
                "spec_cache_env": os.environ.get("EAWF_SPEC_CACHE_DIR", ""),
            }
        ),
        encoding="utf-8",
    )
'''

#: Mutating gate: resolves state the way every RPC does, flips the dispatch
#: pause flag, and appends an event row -- the shape of a suite that drives the
#: pause RPC and the dispatch path behind it.
_MUTATOR_MODULE = "test_gate_isolation_mutator.py"
_MUTATOR_BODY = '''
import json
from pathlib import Path

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.runtime_dir import socket_path


def test_pause_dispatch_on_resolved_state() -> None:
    """Flip ``dispatch_paused`` and append an event on whatever state resolves."""
    state_path, reason = resolve_with_reason(None)
    before = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    before["dispatch_paused"] = True
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(before), encoding="utf-8")

    events = store_path(state_path, StoreKind.EVENT)
    events.parent.mkdir(parents=True, exist_ok=True)
    with events.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": "EV-GATE", "kind": "dispatch_pause"}) + "\\n")

    after = json.loads(state_path.read_text(encoding="utf-8"))
    Path(OBSERVED).write_text(
        json.dumps(
            {
                "state_path": str(state_path),
                "reason": str(reason),
                "socket_path": str(socket_path()),
                "paused_after": after["dispatch_paused"],
                "events_after": events.read_text(encoding="utf-8").splitlines(),
            }
        ),
        encoding="utf-8",
    )
'''


def _state_payload() -> dict[str, Any]:
    """Return a minimal valid state with dispatch running."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _T0,
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
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
        "dispatch_paused": False,
    }


def _write_live_state(root: Path) -> Path:
    """Build a fixture "live" workspace under *root* and return its ledger."""
    state_path = root / "live" / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = State.model_validate(_state_payload())
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    store = state_path.parent / "store"
    store.mkdir(parents=True, exist_ok=True)
    (store / "event.jsonl").write_text('{"id": "EV-LIVE"}\n', encoding="utf-8")
    (state_path.parent / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return state_path


def _write_live_runtime_dir(root: Path) -> Path:
    """Build a fixture "live" daemon runtime dir under *root*."""
    live = root / "live-runtime"
    (live / "wal").mkdir(parents=True, exist_ok=True)
    (live / "wal" / "0001.json").write_text('{"seq": 1}\n', encoding="utf-8")
    (live / "eawfd.pid").write_text("4242\n", encoding="utf-8")
    (live / "eawfd.lock").write_text("{}\n", encoding="utf-8")
    (live / "eawfd.sock").write_text("", encoding="utf-8")
    (live / "eawfd.log").write_bytes(b"x" * (gate_execution.SNAPSHOT_FILE_BYTE_CAP + 1))
    return live


def _tree_signature(root: Path) -> dict[str, bytes]:
    """Return a path -> bytes map of every regular file under *root*."""
    return {
        entry.relative_to(root).as_posix(): entry.read_bytes()
        for entry in sorted(root.rglob("*"))
        if entry.is_file()
    }


class _GateSpawnSpy:
    """Delegating stand-in for the gate runner's spawn.

    Records the environment the REAL gate runner child is launched with, plus
    the contents of the sandbox that environment points at, and then delegates
    so the child still runs for real. Recording inside the spawn call is the
    only chance to observe the sandbox: it is removed as soon as the gate
    returns, which is itself one of the properties under test.
    """

    def __init__(self, real_run: Callable[..., Any]) -> None:
        self._real = real_run
        self.envs: list[dict[str, str]] = []
        self.sandbox_trees: list[dict[str, bytes]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        env = dict(kwargs.get("env") or {})
        self.envs.append(env)
        runtime = env.get("EAWF_RUNTIME_DIR")
        self.sandbox_trees.append(_tree_signature(Path(runtime)) if runtime else {})
        return self._real(argv, **kwargs)


def _spy_on_gate_spawn(monkeypatch: pytest.MonkeyPatch) -> _GateSpawnSpy:
    """Install a delegating spy over the gate runner's child spawn."""
    spy = _GateSpawnSpy(subprocess.run)
    monkeypatch.setattr("eawf.runtime.daemon.gate_execution.subprocess.run", spy)
    return spy


def _plant_probe(*, cwd: Path, name: str, body: str, observed: Path) -> None:
    """Write a gate-probe pytest module into *cwd*, bound to *observed*.

    The report path is bound as a module constant rather than handed over as
    an argv element: the gate argv is policy-checked, and a planted module is
    what lets the probe ride the allowlisted ``pytest`` head at all.
    """
    (cwd / name).write_text(f"OBSERVED = {str(observed)!r}\n{body}", encoding="utf-8")


def _probe_argv(module: str) -> list[str]:
    """Return the allowlisted gate argv that collects one planted module."""
    return ["pytest", "-p", "no:cacheprovider", "-q", module]


def _gate_spec(name: str, argv: list[str]) -> CheckSpec:
    return CheckSpec(kind="command_exit_zero", name=name, args={"argv": argv, "scope": "all"})


def _run_gate(*, state_path: Path, cwd: Path, argv: list[str]) -> Any:
    return gate_execution.run_gate_out_of_process(
        _gate_spec("G-ISO", argv),
        cwd=cwd,
        context=gate_execution.GateExecutionContext(state_path=state_path, attempt_id="CA-ISO"),
        criterion_id="CR-01",
        gate_id="G-ISO",
    )


def test_gate_child_binds_isolated_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-01: the gate is bound to a snapshot copy, not the live runtime dir.

    Asserts at both process levels. The gate runner child is handed a runtime
    directory that is NOT the live one, that carries the live dir's snapshot
    content, and from which the live daemon's transport handles were left
    behind so a sandboxed client cannot dial the process that owns the live
    ledger; the sandbox is gone once the gate returns. The gate command that
    runs inside it then resolves neither the live runtime dir nor the live
    ledger, and the socket an RPC of its own would travel does not land in the
    live runtime dir.
    """
    live_state = _write_live_state(tmp_path)
    live_runtime = _write_live_runtime_dir(tmp_path)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(live_runtime))
    monkeypatch.setenv("EA_STATE", str(live_state))
    observed = tmp_path / "observed.json"
    _plant_probe(cwd=tmp_path, name=_PROBE_MODULE, body=_PROBE_BODY, observed=observed)
    spy = _spy_on_gate_spawn(monkeypatch)

    result = _run_gate(state_path=live_state, cwd=tmp_path, argv=_probe_argv(_PROBE_MODULE))

    assert result.passed is True, result.stderr_tail
    assert len(spy.envs) == 1, "the gate runner child was not spawned exactly once"
    env = spy.envs[0]
    child_runtime = Path(env["EAWF_RUNTIME_DIR"])
    assert child_runtime != live_runtime, "the gate inherited the live runtime directory"
    assert live_runtime not in child_runtime.parents
    sandbox_tree = spy.sandbox_trees[0]
    assert sandbox_tree.get("wal/0001.json") == b'{"seq": 1}\n', (
        "the sandbox was not seeded from the live snapshot"
    )
    for handle in ("eawfd.pid", "eawfd.lock", "eawfd.sock"):
        assert handle not in sandbox_tree, f"the live daemon {handle} reached the sandbox"
    assert "eawfd.log" not in sandbox_tree, "an oversized log was copied into the sandbox"
    child_state = Path(env["EA_STATE"])
    assert child_state != live_state, "the gate inherited the live ledger path"
    assert live_state.parent not in child_state.parents
    assert "EAWF_SPEC_CACHE_DIR" not in env, "an inherited spec-cache path leaked a live path back"
    assert not child_runtime.exists(), "the sandbox outlived the gate that owned it"

    seen = json.loads(observed.read_text(encoding="utf-8"))
    gate_runtime = Path(seen["runtime_dir"])
    assert gate_runtime != live_runtime, "the gate command resolved the live runtime directory"
    assert live_runtime not in gate_runtime.parents
    assert seen["runtime_dir_env"] != str(live_runtime), "a live runtime binding reached the gate"
    assert Path(seen["socket_path"]).parent != live_runtime, (
        "an RPC from the gate command would have reached the live daemon"
    )
    gate_state = Path(seen["state_path"])
    assert gate_state != live_state, "the gate command resolved the live ledger"
    assert live_state.parent not in gate_state.parents
    assert seen["spec_cache_env"] == "", "an inherited spec-cache path leaked a live path back"


def test_live_state_byte_identical_after_mutating_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-02: a state-mutating gate leaves the live tree byte identical.

    The gate flips ``dispatch_paused`` and appends an event row through the
    same resolver every RPC uses, and its receipt proves the write landed:
    without that, byte equality would only say the gate did nothing.
    Post-conditions: live ``state.json`` bytes, live event-store bytes, the
    live ``dispatch_paused`` value and the live runtime dir are unchanged, the
    write landed outside the live workspace, and the socket a dispatch RPC
    would travel resolves outside the live runtime dir -- so no request can
    reach the daemon that spawns agents against the working tree.
    """
    live_state = _write_live_state(tmp_path)
    live_runtime = _write_live_runtime_dir(tmp_path)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(live_runtime))
    monkeypatch.setenv("EA_STATE", str(live_state))
    live_events = live_state.parent / "store" / "event.jsonl"
    state_before = live_state.read_bytes()
    events_before = live_events.read_bytes()
    runtime_before = _tree_signature(live_runtime)
    assert State.model_validate_json(state_before).dispatch_paused is False
    observed = tmp_path / "observed.json"
    workdir = tmp_path / "gate-cwd"
    workdir.mkdir()
    _plant_probe(cwd=workdir, name=_MUTATOR_MODULE, body=_MUTATOR_BODY, observed=observed)
    spy = _spy_on_gate_spawn(monkeypatch)

    result = _run_gate(state_path=live_state, cwd=workdir, argv=_probe_argv(_MUTATOR_MODULE))

    assert result.passed is True, result.stderr_tail
    assert live_state.read_bytes() == state_before, "the gate rewrote the live ledger"
    assert live_events.read_bytes() == events_before, "the gate appended to the live event store"
    assert State.model_validate_json(live_state.read_bytes()).dispatch_paused is False
    assert "EV-GATE" not in live_events.read_text(encoding="utf-8")
    assert _tree_signature(live_runtime) == runtime_before, "the gate wrote the live runtime dir"
    sandbox_state = Path(spy.envs[0]["EA_STATE"])
    assert sandbox_state != live_state, "the gate runner inherited the live ledger path"
    assert not sandbox_state.exists(), "the sandbox ledger outlived the gate that owned it"

    seen = json.loads(observed.read_text(encoding="utf-8"))
    mutated = Path(seen["state_path"])
    assert mutated != live_state
    assert live_state.parent not in mutated.parents
    # The gate's own read-back: the mutation has to have landed somewhere, or
    # byte equality on the live tree proves nothing about isolation.
    assert seen["paused_after"] is True, (
        "the gate mutation vanished; the test proved nothing about isolation"
    )
    assert any("EV-GATE" in row for row in seen["events_after"]), (
        "the gate never appended its event row"
    )
    assert Path(seen["socket_path"]).parent != live_runtime, (
        "a dispatch RPC from the gate would have reached the live daemon"
    )


def test_seed_gate_sandbox_skips_live_daemon_handles_and_oversized_files(tmp_path: Path) -> None:
    """Boundary: the snapshot copies content but never a live handle."""
    live_state = _write_live_state(tmp_path)
    live_runtime = _write_live_runtime_dir(tmp_path)

    sandbox = gate_execution.seed_gate_sandbox(
        root=tmp_path / "sandbox",
        live_state_path=live_state,
        live_runtime_dir=live_runtime,
    )

    assert (sandbox.runtime_dir / "wal" / "0001.json").read_text(encoding="utf-8") == '{"seq": 1}\n'
    for handle in ("eawfd.pid", "eawfd.lock", "eawfd.sock", "eawfd.log"):
        assert not (sandbox.runtime_dir / handle).exists()
    assert sandbox.state_path.read_bytes() == live_state.read_bytes()
    assert (sandbox.state_path.parent / "store" / "event.jsonl").is_file()
    assert (sandbox.state_path.parent / "config.yaml").is_file()


def test_seed_gate_sandbox_skips_non_regular_entries(tmp_path: Path) -> None:
    """Boundary: a FIFO is skipped rather than crashing the snapshot copy."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are POSIX-only")
    live_state = _write_live_state(tmp_path)
    live_runtime = _write_live_runtime_dir(tmp_path)
    os.mkfifo(live_runtime / "eawfd.fifo")

    sandbox = gate_execution.seed_gate_sandbox(
        root=tmp_path / "sandbox",
        live_state_path=live_state,
        live_runtime_dir=live_runtime,
    )

    assert not (sandbox.runtime_dir / "eawfd.fifo").exists()
    assert (sandbox.runtime_dir / "wal" / "0001.json").is_file()


def test_seed_gate_sandbox_empty_live_tree_yields_empty_sandbox(tmp_path: Path) -> None:
    """Boundary: an absent runtime dir and ledger still produce a sandbox."""
    sandbox = gate_execution.seed_gate_sandbox(
        root=tmp_path / "sandbox",
        live_state_path=tmp_path / "absent" / ".ea" / "state.json",
        live_runtime_dir=tmp_path / "absent-runtime",
    )

    assert sandbox.runtime_dir.is_dir()
    assert list(sandbox.runtime_dir.iterdir()) == []
    assert sandbox.state_path.parent.is_dir()
    assert not sandbox.state_path.exists()


def test_seed_gate_sandbox_rejects_non_file_live_state(tmp_path: Path) -> None:
    """Error path: a live state path that is a directory is a caller bug."""
    directory = tmp_path / ".ea"
    directory.mkdir(parents=True)

    with pytest.raises(ValueError, match="live state path is not a file"):
        gate_execution.seed_gate_sandbox(
            root=tmp_path / "sandbox",
            live_state_path=directory,
            live_runtime_dir=tmp_path / "absent-runtime",
        )


def test_seed_gate_sandbox_rejects_non_directory_root(tmp_path: Path) -> None:
    """Error path: a root that already exists as a file is a caller bug."""
    root = tmp_path / "not-a-dir"
    root.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="gate sandbox root is not a directory"):
        gate_execution.seed_gate_sandbox(
            root=root,
            live_state_path=_write_live_state(tmp_path),
            live_runtime_dir=tmp_path / "absent-runtime",
        )


def test_gate_child_env_repoints_runtime_and_state_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both resolver overrides point at the sandbox; the cache seam is dropped."""
    monkeypatch.setenv("EAWF_RUNTIME_DIR", "/live/runtime")
    monkeypatch.setenv("EA_STATE", "/live/.ea/state.json")
    monkeypatch.setenv("EAWF_SPEC_CACHE_DIR", "/live/runtime/spec-cache")
    sandbox = gate_execution.GateSandbox(
        root=Path("/sandbox"),
        runtime_dir=Path("/sandbox/runtime"),
        state_path=Path("/sandbox/.ea/state.json"),
    )

    env = gate_execution.gate_child_env(sandbox)

    assert env["EAWF_RUNTIME_DIR"] == "/sandbox/runtime"
    assert env["EA_STATE"] == "/sandbox/.ea/state.json"
    assert "EAWF_SPEC_CACHE_DIR" not in env
    assert os.environ["EAWF_RUNTIME_DIR"] == "/live/runtime", "the parent env was mutated"


def test_gate_sandbox_removes_its_root_when_the_body_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: a raising gate still takes its sandbox down with it."""
    live_state = _write_live_state(tmp_path)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(_write_live_runtime_dir(tmp_path)))
    seen: list[Path] = []

    with (
        pytest.raises(RuntimeError, match="gate exploded"),
        gate_execution.gate_sandbox(live_state_path=live_state) as sandbox,
    ):
        seen.append(sandbox.root)
        assert sandbox.state_path.is_file()
        raise RuntimeError("gate exploded")

    assert seen and not seen[0].exists()


def test_gate_sandbox_rejects_an_unknown_field() -> None:
    """Error path: the sandbox model forbids extras like every config model."""
    with pytest.raises(ValueError, match="extra_forbidden"):
        gate_execution.GateSandbox(
            root=Path("/sandbox"),
            runtime_dir=Path("/sandbox/runtime"),
            state_path=Path("/sandbox/.ea/state.json"),
            live_runtime_dir=Path("/live/runtime"),
        )
