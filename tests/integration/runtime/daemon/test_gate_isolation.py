"""Close gates execute against a sandbox, never the live runtime tree.

A gate child used to inherit the daemon's runtime directory and state path, so
a gate suite that exercises eawf's own RPCs drove them against the live ledger
and the live dispatch loop. The suite below pins the two properties that close
that hole: the child resolves a runtime directory that is a SNAPSHOT COPY, and
a gate that flips ``dispatch_paused`` and appends to the event store leaves the
live tree byte identical.

Every "live" tree in this file is a fixture directory built under ``tmp_path``.
The repository's own ``.ea`` is never read or written: the gate runner reaches
the live tree only through ``GateExecutionContext.state_path`` and the
``EAWF_RUNTIME_DIR`` env var, both of which each test points at its fixture.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.runtime.daemon import gate_execution
from eawf.workflow.audit_dsl.models import CheckSpec

pytestmark = pytest.mark.integration

_T0 = "2026-09-08T00:00:00+00:00"

#: Probe gate: records what the runtime-dir resolver answers INSIDE the gate,
#: plus which snapshot entries reached the sandbox.
_PROBE_SOURCE = """
import json
import os
import sys
from pathlib import Path

from eawf.runtime.daemon.runtime_dir import runtime_dir, socket_path

resolved = runtime_dir()
Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "runtime_dir": str(resolved),
            "socket_path": str(socket_path()),
            "ea_state": os.environ.get("EA_STATE", ""),
            "spec_cache_env": os.environ.get("EAWF_SPEC_CACHE_DIR", ""),
            "seeded_wal_entry": (resolved / "wal" / "0001.json").is_file(),
            "seeded_pid": (resolved / "eawfd.pid").exists(),
            "seeded_lock": (resolved / "eawfd.lock").exists(),
            "seeded_log": (resolved / "eawfd.log").exists(),
        }
    ),
    encoding="utf-8",
)
"""

#: Mutating gate: resolves state the way every RPC does, flips the dispatch
#: pause flag, and appends an event row -- the shape of a suite that drives the
#: pause RPC and the dispatch path behind it.
_MUTATOR_SOURCE = """
import json
import sys
from pathlib import Path

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.runtime_dir import socket_path

state_path, reason = resolve_with_reason(None)
payload = json.loads(state_path.read_text(encoding="utf-8"))
payload["dispatch_paused"] = True
state_path.write_text(json.dumps(payload), encoding="utf-8")

events = store_path(state_path, StoreKind.EVENT)
events.parent.mkdir(parents=True, exist_ok=True)
with events.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"id": "EV-GATE", "kind": "dispatch_pause"}) + "\\n")

Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "state_path": str(state_path),
            "reason": reason,
            "socket_path": str(socket_path()),
            "paused_after": json.loads(state_path.read_text(encoding="utf-8"))["dispatch_paused"],
            "events_after": events.read_text(encoding="utf-8").splitlines(),
        }
    ),
    encoding="utf-8",
)
"""


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


def _script(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


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
    """CR-01: the gate resolves a snapshot copy, not the live runtime dir.

    Asserts three things at once: the resolved directory is NOT the live one,
    it carries the live dir's snapshot content, and the live daemon's transport
    handles were left behind so a sandboxed client cannot dial the process that
    owns the live ledger.
    """
    live_state = _write_live_state(tmp_path)
    live_runtime = _write_live_runtime_dir(tmp_path)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(live_runtime))
    monkeypatch.setenv("EA_STATE", str(live_state))
    observed = tmp_path / "observed.json"
    probe = _script(tmp_path, "probe.py", _PROBE_SOURCE)

    result = _run_gate(
        state_path=live_state,
        cwd=tmp_path,
        argv=[sys.executable, str(probe), str(observed)],
    )

    assert result.passed is True, result.stderr_tail
    seen = json.loads(observed.read_text(encoding="utf-8"))
    child_runtime = Path(seen["runtime_dir"])
    assert child_runtime != live_runtime, "the gate inherited the live runtime directory"
    assert live_runtime not in child_runtime.parents
    assert seen["seeded_wal_entry"] is True, "the sandbox was not seeded from the live snapshot"
    assert seen["seeded_pid"] is False, "the live daemon PID file reached the sandbox"
    assert seen["seeded_lock"] is False, "the live daemon lock file reached the sandbox"
    assert seen["seeded_log"] is False, "an oversized log was copied into the sandbox"
    child_state = Path(seen["ea_state"])
    assert child_state != live_state, "the gate inherited the live ledger path"
    assert live_state.parent not in child_state.parents
    assert seen["spec_cache_env"] == "", "an inherited spec-cache path leaked a live path back"
    assert not child_runtime.exists(), "the sandbox outlived the gate that owned it"


def test_live_state_byte_identical_after_mutating_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-02: a state-mutating gate leaves the live tree byte identical.

    The gate flips ``dispatch_paused`` and appends an event row through the
    same resolver every RPC uses. Post-conditions: live ``state.json`` bytes,
    live event-store bytes, and the live ``dispatch_paused`` value are
    unchanged, the write landed in the sandbox copy instead, and the socket a
    dispatch RPC would travel resolves inside the sandbox -- so no request can
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
    mutator = _script(tmp_path, "mutator.py", _MUTATOR_SOURCE)

    result = _run_gate(
        state_path=live_state,
        cwd=tmp_path,
        argv=[sys.executable, str(mutator), str(observed)],
    )

    assert result.passed is True, result.stderr_tail
    assert live_state.read_bytes() == state_before, "the gate rewrote the live ledger"
    assert live_events.read_bytes() == events_before, "the gate appended to the live event store"
    assert State.model_validate_json(live_state.read_bytes()).dispatch_paused is False
    assert _tree_signature(live_runtime) == runtime_before, "the gate wrote the live runtime dir"
    seen = json.loads(observed.read_text(encoding="utf-8"))
    assert seen["reason"] == "env"
    sandbox_state = Path(seen["state_path"])
    assert sandbox_state != live_state
    assert live_state.parent not in sandbox_state.parents
    # Read back inside the gate: the sandbox dies with the gate that owned it,
    # so the mutation is only observable through what the gate reported.
    assert seen["paused_after"] is True, (
        "the gate mutation vanished; the test proved nothing about isolation"
    )
    assert len(seen["events_after"]) == 2, "the gate appended to some other event store"
    assert Path(seen["socket_path"]).parent != live_runtime, (
        "a dispatch RPC from the gate would have reached the live daemon"
    )
    assert not sandbox_state.exists(), "the sandbox ledger outlived the gate that owned it"


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
