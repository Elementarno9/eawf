"""A test-suite gate passes through the gate harness exactly when it passes outside.

The daemon runs a ``command_exit_zero`` gate in a child bound to a throwaway
sandbox. A gate that is a repository's own test suite builds its own
workspaces and names them through ``-w``, ``EA_STATE`` and the working
directory; the harness must leave those resolutions alone, or the suite fails
under the harness where it passes in an operator shell. The suite below runs
the same planted pytest modules both ways and compares the outcomes, then
proves the harness still keeps the live ledger, the live runtime directory and
the operator's credentials out of the gate's reach.

Every "live" tree here is a fixture directory under ``tmp_path``; the
repository's own ``.ea`` is never read or written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.kernel.state.resolve import (
    GATE_LIVE_STATE_ENV,
    GATE_SANDBOX_STATE_ENV,
    fence_live_ledger,
)
from eawf.runtime.daemon import gate_execution
from eawf.runtime.sandbox.env_scrub import GATE_RUNTIME_LANE, build_child_env, resolve_binary_dir
from eawf.workflow.audit_dsl.models import CheckSpec

pytestmark = pytest.mark.integration

_T0 = "2026-09-28T00:00:00+00:00"

#: A suite that drives the CLI against a workspace it builds itself: the shape
#: of every CLI test that failed under the harness while ``EA_STATE`` was
#: pinned to the sandbox ledger and so outranked the suite's own ``-w``.
_OWN_WORKSPACE_SUITE = """
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.cli.app import app


def _seed(repo: Path) -> Path:
    ea = repo / ".ea"
    ea.mkdir(parents=True)
    state = ea / "state.json"
    state.write_text('{"schema_version": "1.0"}\\n', encoding="utf-8")
    (ea / "config.yaml").write_text("ui:\\n  theme: dark\\n", encoding="utf-8")
    (ea / "profile.yaml").write_text("ids:\\n  - core\\n", encoding="utf-8")
    return state


def test_cli_backs_up_the_workspace_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _seed(repo)
    monkeypatch.setenv("EAWF_HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(app, ["-w", str(repo), "--json", "backup", "create"])
    assert result.exit_code == 0, result.output


def test_walk_up_reaches_the_workspace_it_enters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _seed(tmp_path / "repo")
    inner = tmp_path / "repo" / "pkg"
    inner.mkdir()
    monkeypatch.chdir(inner)
    resolved, reason = resolve_with_reason(None)
    assert resolved.resolve() == state.resolve()
    assert reason == "pwd_upward"
"""

#: A suite that fails outside the harness; the harness must not turn it green.
_FAILING_SUITE = """
def test_fails_everywhere() -> None:
    assert 1 == 2
"""

#: A hostile gate: it resolves the ledger every way a CLI does, aims each
#: resolution at the live tree, writes to whatever it gets, and reports what it
#: resolved plus the credentials and runtime directory it can see.
_MUTATOR_SUITE = """
import json
import os
from pathlib import Path

import pytest

from eawf.kernel.state.resolve import resolve_with_reason
from eawf.runtime.daemon.runtime_dir import runtime_dir, socket_path
from eawf.surfaces.cli.scope import resolve_state_path

_WATCHED = ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY", "EA_STATE")


def _pause(path: Path) -> bool:
    payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    payload["dispatch_paused"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))["dispatch_paused"]


def test_reach_for_the_live_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_env = {key: os.environ.get(key) for key in _WATCHED}
    walked, _ = resolve_with_reason(None)
    named = resolve_state_path(Path(LIVE_ROOT))
    monkeypatch.setenv("EA_STATE", LIVE_STATE)
    pinned = resolve_state_path(None)
    resolutions = {"walk_up": walked, "workspace_flag": named, "ea_state": pinned}
    Path(OBSERVED).write_text(
        json.dumps(
            {
                "env": seen_env,
                "runtime_dir": str(runtime_dir()),
                "socket_path": str(socket_path()),
                "resolved": {name: str(path) for name, path in resolutions.items()},
                "paused": {name: _pause(path) for name, path in resolutions.items()},
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


def _write_live_repo(root: Path) -> Path:
    """Build a fixture live repository under *root* and return its ledger."""
    state_path = root / "live" / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(State.model_validate(_state_payload()).model_dump_json(), "utf-8")
    (state_path.parent / "store").mkdir()
    (state_path.parent / "store" / "event.jsonl").write_text('{"id": "EV-LIVE"}\n', "utf-8")
    return state_path


def _write_live_runtime_dir(root: Path) -> Path:
    """Build a fixture live daemon runtime dir under *root*."""
    live = root / "live-runtime"
    (live / "wal").mkdir(parents=True)
    (live / "wal" / "0001.json").write_text('{"seq": 1}\n', encoding="utf-8")
    (live / "eawfd.pid").write_text("4242\n", encoding="utf-8")
    return live


def _tree_signature(root: Path) -> dict[str, bytes]:
    """Return a path -> bytes map of every regular file under *root*."""
    return {
        entry.relative_to(root).as_posix(): entry.read_bytes()
        for entry in sorted(root.rglob("*"))
        if entry.is_file()
    }


def _suite_argv(module: str) -> list[str]:
    return ["pytest", "-p", "no:cacheprovider", "-q", module]


def _run_through_harness(*, state_path: Path, cwd: Path, argv: list[str]) -> Any:
    spec = CheckSpec(kind="command_exit_zero", name="G-PAR", args={"argv": argv, "scope": "all"})
    return gate_execution.run_gate_out_of_process(
        spec,
        cwd=cwd,
        context=gate_execution.GateExecutionContext(state_path=state_path, attempt_id="CA-PAR"),
        criterion_id="CR-01",
        gate_id="G-PAR",
    )


def _run_outside(*, cwd: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run *argv* the way an operator shell would, under the same env scrub."""
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=build_child_env(GATE_RUNTIME_LANE, extra_path_dir=resolve_binary_dir(argv[0])),
    )


@pytest.mark.parametrize(
    ("body", "passes"),
    [(_OWN_WORKSPACE_SUITE, True), (_FAILING_SUITE, False)],
    ids=["own-workspace-suite", "failing-suite"],
)
def test_suite_gate_outcome_matches_outside_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, passes: bool
) -> None:
    """The harness verdict equals the outside exit status, green and red alike."""
    live_state = _write_live_repo(tmp_path)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(_write_live_runtime_dir(tmp_path)))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "test_suite_under_gate.py").write_text(body, encoding="utf-8")
    argv = _suite_argv("test_suite_under_gate.py")

    outside = _run_outside(cwd=checkout, argv=argv)
    result = _run_through_harness(state_path=live_state, cwd=checkout, argv=argv)

    assert (outside.returncode == 0) is passes, outside.stdout[-2000:]
    assert result.passed is passes, result.details


@pytest.mark.parametrize(
    "shape",
    ["gate-runs-in-live-tree", "proof-checkout-under-live-ea"],
)
def test_suite_gate_cannot_reach_live_ledger_runtime_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """Every resolution aimed at the live ledger lands in the sandbox instead.

    Two shapes: a close gate whose working tree IS the live repository, and a
    delivery proof whose claim ledger and checkout both sit under the live
    ``.ea`` so the checkout's upward walk reaches the repository ledger.
    """
    live_state = _write_live_repo(tmp_path)
    live_root = live_state.parent.parent
    live_runtime = _write_live_runtime_dir(tmp_path)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(live_runtime))
    monkeypatch.setenv("EA_STATE", str(live_state))
    for key in ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, f"secret-{key.lower()}")
    if shape == "gate-runs-in-live-tree":
        context_state, cwd = live_state, live_root
    else:
        run_dir = live_state.parent / "local" / "proof-runs" / "RUN"
        context_state, cwd = run_dir / "claims" / "state.json", run_dir / "checkout"
        cwd.mkdir(parents=True)
    ledger_before = _tree_signature(live_state.parent / "store") | {
        "state.json": live_state.read_bytes()
    }
    runtime_before = _tree_signature(live_runtime)
    observed = tmp_path / "observed.json"
    (cwd / "test_mutator_under_gate.py").write_text(
        f"OBSERVED = {str(observed)!r}\nLIVE_ROOT = {str(live_root)!r}\n"
        f"LIVE_STATE = {str(live_state)!r}\n{_MUTATOR_SUITE}",
        encoding="utf-8",
    )

    result = _run_through_harness(
        state_path=context_state, cwd=cwd, argv=_suite_argv("test_mutator_under_gate.py")
    )

    assert result.passed is True, result.details
    seen = json.loads(observed.read_text(encoding="utf-8"))
    assert all(seen["paused"].values()), "a mutation vanished; byte identity proves nothing"
    for name, resolved in seen["resolved"].items():
        assert Path(resolved).resolve() != live_state.resolve(), f"{name} reached the live ledger"
    assert live_state.read_bytes() == ledger_before["state.json"], "the gate rewrote the ledger"
    assert (
        _tree_signature(live_state.parent / "store") | {"state.json": live_state.read_bytes()}
        == ledger_before
    )
    assert _tree_signature(live_runtime) == runtime_before, "the gate wrote the live runtime dir"
    assert Path(seen["runtime_dir"]) != live_runtime
    assert Path(seen["socket_path"]).parent != live_runtime
    assert seen["env"] == {
        "GH_TOKEN": None,
        "AWS_SECRET_ACCESS_KEY": None,
        "ANTHROPIC_API_KEY": None,
        "EA_STATE": None,
    }, "a credential or the daemon's ledger binding reached the gate command"


def test_fence_swaps_only_the_live_ledger(tmp_path: Path) -> None:
    """Boundary: the live ledger and its enclosing .ea swap; siblings do not."""
    live = tmp_path / "repo" / ".ea" / "local" / "proof-runs" / "RUN" / "claims" / "state.json"
    sandbox = tmp_path / "sandbox" / "claims" / "state.json"
    env = {GATE_LIVE_STATE_ENV: str(live), GATE_SANDBOX_STATE_ENV: str(sandbox)}
    checkout_ledger = live.parent.parent / "checkout" / ".ea" / "state.json"
    unrelated = tmp_path / "other" / ".ea" / "state.json"
    home_file = tmp_path / "state.json"

    assert fence_live_ledger(live, env) == sandbox
    assert fence_live_ledger(tmp_path / "repo" / ".ea" / "state.json", env) == sandbox
    assert fence_live_ledger(checkout_ledger, env) == checkout_ledger
    assert fence_live_ledger(unrelated, env) == unrelated
    assert fence_live_ledger(home_file, env) == home_file
    assert fence_live_ledger(live, {}) == live
    assert fence_live_ledger(live, {GATE_LIVE_STATE_ENV: str(live)}) == live
