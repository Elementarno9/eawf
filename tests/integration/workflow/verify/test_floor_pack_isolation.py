"""Deterministic gates execute out of process, never against the live tree.

A readiness gate is routinely a whole test suite that drives eawf's own
RPCs; run in the calling process it would reach the live runtime directory
and the live ledger. The suite below pins that the advisory single-gate
path and :func:`~eawf.workflow.verify.sandboxed_checks.run_checks_out_of_process`
run their checks in a child interpreter whose ``EAWF_RUNTIME_DIR`` and live
ledger are bound to a throwaway sandbox, not to the live pair.

Every "live" tree in this file is a fixture directory built under
``tmp_path``. The repository's own ``.ea`` is never read or written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.resolve import GATE_LIVE_STATE_ENV, GATE_SANDBOX_STATE_ENV
from eawf.runtime.daemon import gate_execution
from eawf.workflow.audit_dsl.models import CheckSpec
from eawf.workflow.verify import readiness as readiness_mod
from eawf.workflow.verify import sandboxed_checks
from tests._epoch2_helpers import lay_epoch2_tree

pytestmark = pytest.mark.integration


# ---- fixtures ---------------------------------------------------------------


def _git(repo_root: Path, *args: str) -> None:
    """Run a quiet ``git -C <repo_root>`` command, raising on non-zero exit."""
    subprocess.run(["git", "-C", str(repo_root), *args], check=True, capture_output=True)


def _init_repo(repo_root: Path) -> None:
    """Initialise *repo_root* as a git repo with one commit.

    The gate runner resolves a diff base, so the checks need a real tree.
    """
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "init", "-q", "-b", "main")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "test")
    (repo_root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-q", "-m", "seed")


def _write_live_state(root: Path) -> Path:
    """Build a fixture "live" state directory under *root* and return its ledger.

    The ledger sits at ``<root>/.ea/`` -- an ANCESTOR of the fixture repo the
    checks run in -- so an unbound gate finds it by the same pwd-upward walk
    that reaches an operator's real ``.ea``. A ledger tucked out of that walk
    would make the byte-identity assertions pass for the wrong reason.
    """
    state_path = lay_epoch2_tree(root, project_code="ABC")
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
    return live


def _capture_child_envs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Record every env handed to a sandboxed runner child, then delegate.

    The spy wraps the production seam rather than replacing it, so the
    checks still execute for real and the recorded env is the one the child
    actually received.
    """
    seen: list[dict[str, str]] = []
    real = gate_execution.gate_child_env

    def _spy(sandbox: gate_execution.GateSandbox, *, live_state_path: Path) -> dict[str, str]:
        env = real(sandbox, live_state_path=live_state_path)
        seen.append(dict(env))
        return env

    monkeypatch.setattr(gate_execution, "gate_child_env", _spy)
    return seen


# ---- the advisory single-gate path ------------------------------------------


def test_advisory_single_gate_runs_out_of_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-02: the advisory typed-gate path takes the same sandboxed runner.

    The advisory lane carries no durable close identity, so before this it
    was the one deterministic path left executing in the caller's process.
    """
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    live_state = _write_live_state(tmp_path)
    live_runtime = _write_live_runtime_dir(tmp_path)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(live_runtime))
    envs = _capture_child_envs(monkeypatch)

    status = readiness_mod._run_deterministic_gate(
        GateSpec(
            id="G-01",
            criterion_id="CR-01",
            kind="command_exit_zero",
            args={"argv": ["git", "status", "--porcelain"], "scope": "all"},
            policy="block",
            cadence="every-wave",
        ),
        CriterionSpec(
            id="CR-01",
            text="the advisory gate runs out of process",
            kind="behavioral",
            evidence_kind="deterministic",
            acceptance_style="binary",
            quality_dimension="security",
            measurable_signal="the gate exits zero from the sandboxed runner",
        ),
        runner_cwd=repo_root,
        live_state_path=live_state,
    )

    assert status == "pass"
    assert len(envs) == 1, "the advisory gate did not reach the sandboxed runner"
    assert Path(envs[0]["EAWF_RUNTIME_DIR"]) != live_runtime
    assert Path(envs[0][GATE_SANDBOX_STATE_ENV]) != live_state
    assert Path(envs[0][GATE_LIVE_STATE_ENV]) == live_state


# ---- run_checks_out_of_process: boundaries + error paths --------------------


def _spec(name: str, argv: list[str]) -> CheckSpec:
    """A deterministic ``command_exit_zero`` check over *argv*."""
    return CheckSpec(kind="command_exit_zero", name=name, args={"argv": argv, "scope": "all"})


def test_run_checks_out_of_process_empty_batch_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: an empty spec list returns ``[]`` without building a sandbox."""
    envs = _capture_child_envs(monkeypatch)

    assert sandboxed_checks.run_checks_out_of_process([], cwd=tmp_path) == []
    assert envs == []


def test_run_checks_out_of_process_single_spec_returns_one_result(tmp_path: Path) -> None:
    """Boundary: one spec in, exactly one result out, in declaration order."""
    _init_repo(tmp_path)

    results = sandboxed_checks.run_checks_out_of_process(
        [_spec("only", ["git", "status", "--porcelain"])],
        cwd=tmp_path,
    )

    assert [result.name for result in results] == ["only"]
    assert results[0].status == "pass"


def test_run_checks_out_of_process_preserves_declaration_order(tmp_path: Path) -> None:
    """Boundary: a mixed batch returns one result per spec, order preserved."""
    _init_repo(tmp_path)

    results = sandboxed_checks.run_checks_out_of_process(
        [
            _spec("first", ["git", "status", "--porcelain"]),
            _spec("second", ["git", "show", "no-such-ref-w35"]),
            _spec("third", ["git", "status", "--porcelain"]),
        ],
        cwd=tmp_path,
    )

    assert [result.name for result in results] == ["first", "second", "third"]
    assert [result.status for result in results] == ["pass", "fail", "pass"]


def test_run_checks_out_of_process_rejects_a_missing_cwd(tmp_path: Path) -> None:
    """Error path: a cwd that is not a directory is a caller bug, raised eagerly."""
    with pytest.raises(ValueError, match="cwd is not a directory"):
        sandboxed_checks.run_checks_out_of_process(
            [_spec("only", ["git", "status"])],
            cwd=tmp_path / "absent",
        )


def test_run_checks_out_of_process_rejects_a_file_cwd(tmp_path: Path) -> None:
    """Error path: a regular file passed as the cwd is rejected the same way."""
    target = tmp_path / "not-a-dir"
    target.write_text("x\n", encoding="utf-8")

    with pytest.raises(ValueError, match="cwd is not a directory"):
        sandboxed_checks.run_checks_out_of_process([_spec("only", ["git", "status"])], cwd=target)


def test_run_checks_out_of_process_blocks_every_spec_when_the_child_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: a child that writes no response blocks, it never passes.

    A crashed runner proved nothing, so projecting a pass would let a wave
    close on the absence of evidence.
    """
    _init_repo(tmp_path)
    monkeypatch.setattr(
        sandboxed_checks,
        "_read_child_response",
        lambda response_path: None,
    )

    results = sandboxed_checks.run_checks_out_of_process(
        [_spec("a", ["git", "status"]), _spec("b", ["git", "status"])],
        cwd=tmp_path,
    )

    assert [result.name for result in results] == ["a", "b"]
    assert [result.status for result in results] == ["blocked", "blocked"]
    assert all(result.passed is False for result in results)
    assert all("crashed without a terminal result" in (result.details or "") for result in results)


def test_run_checks_out_of_process_blocks_on_a_typed_child_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: a child that reports a fault blocks rather than raising."""
    _init_repo(tmp_path)
    monkeypatch.setattr(
        sandboxed_checks,
        "_read_child_response",
        lambda response_path: sandboxed_checks._ChildResponse(ok=False, error="ValueError: boom"),
    )

    results = sandboxed_checks.run_checks_out_of_process(
        [_spec("a", ["git", "status"])],
        cwd=tmp_path,
    )

    assert results[0].status == "blocked"
    assert "ValueError: boom" in (results[0].details or "")


def test_run_checks_out_of_process_blocks_on_a_result_count_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: a short answer blocks every spec instead of misaligning them.

    The caller zips results against specs, so a truncated batch would
    silently attribute one check's verdict to another check's name.
    """
    _init_repo(tmp_path)
    real = sandboxed_checks._read_child_response

    def _truncate(response_path: Path) -> Any:
        response = real(response_path)
        assert response is not None and response.results is not None
        return response.model_copy(update={"results": response.results[:1]})

    monkeypatch.setattr(sandboxed_checks, "_read_child_response", _truncate)

    results = sandboxed_checks.run_checks_out_of_process(
        [_spec("a", ["git", "status"]), _spec("b", ["git", "status"])],
        cwd=tmp_path,
    )

    assert [result.status for result in results] == ["blocked", "blocked"]
    assert "returned 1 results for 2 checks" in (results[0].details or "")


def test_main_rejects_a_malformed_argument_vector() -> None:
    """Error path: the child entry point exits 2 on a usage error, never 0."""
    assert sandboxed_checks.main([]) == 2
    assert sandboxed_checks.main(["--wrong-flag", "a", "b"]) == 2
    assert sandboxed_checks.main(["--run-checks", "only-one-path"]) == 2


def test_main_runs_the_batch_named_by_its_request(tmp_path: Path) -> None:
    """The child entry point answers a well-formed request with a typed response."""
    _init_repo(tmp_path)
    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    request_path.write_text(
        json.dumps(
            {
                "specs": [_spec("only", ["git", "status", "--porcelain"]).model_dump(mode="json")],
                "cwd": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )

    assert sandboxed_checks.main(["--run-checks", str(request_path), str(response_path)]) == 0

    payload = json.loads(response_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert [row["name"] for row in payload["results"]] == ["only"]


def test_run_checks_out_of_process_seeds_an_empty_ledger_without_a_state_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: no ledger named means an EMPTY sandbox, never a guessed one.

    Guessing would resolve the ambient chain, which points at the live tree
    the sandbox exists to deny, so the fallback must not touch it.
    """
    _init_repo(tmp_path)
    live_state = _write_live_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(live_state))

    seeded: list[Path] = []
    real_seed = gate_execution.seed_gate_sandbox

    def _spy(**kwargs: Any) -> gate_execution.GateSandbox:
        sandbox = real_seed(**kwargs)
        seeded.append(sandbox.state_path)
        return sandbox

    monkeypatch.setattr(gate_execution, "seed_gate_sandbox", _spy)

    sandboxed_checks.run_checks_out_of_process(
        [_spec("only", ["git", "status", "--porcelain"])],
        cwd=tmp_path,
    )

    assert len(seeded) == 1
    assert seeded[0] != live_state
    assert not seeded[0].exists(), "the fallback copied a ledger it was never given"


def test_build_child_env_gate_lane_carries_the_containment_bindings() -> None:
    """The gate lane keeps the sandbox bindings; the agent lanes still drop them.

    The bindings are what makes the sandbox hold across the gate command's
    own subprocess: scrubbed away, the gate falls back to the operator's
    live runtime directory and the live ledger, which is the exposure the
    sandbox exists to close.
    """
    from eawf.runtime.sandbox.env_scrub import GATE_RUNTIME_LANE, build_child_env

    base = {
        "EAWF_RUNTIME_DIR": "/sandbox/runtime",
        "EA_STATE": "/live/.ea/state.json",
        GATE_LIVE_STATE_ENV: "/live/.ea/state.json",
        GATE_SANDBOX_STATE_ENV: "/sandbox/.ea/state.json",
        "EAWF_DAEMONLESS": "1",
        "GH_TOKEN": "not-a-real-value-for-gh-token",
    }

    gate_env = build_child_env(GATE_RUNTIME_LANE, base_env=base)
    assert gate_env["EAWF_RUNTIME_DIR"] == "/sandbox/runtime"
    assert gate_env[GATE_LIVE_STATE_ENV] == "/live/.ea/state.json"
    assert gate_env[GATE_SANDBOX_STATE_ENV] == "/sandbox/.ea/state.json"
    # A ledger pin outranks the gate's own -w and working directory.
    assert "EA_STATE" not in gate_env
    # Containment is not a licence: the rest of the EAWF_* family and every
    # credential stay dropped.
    assert "EAWF_DAEMONLESS" not in gate_env
    assert "GH_TOKEN" not in gate_env

    agent_env = build_child_env("claude-code", base_env=base)
    assert "EAWF_RUNTIME_DIR" not in agent_env
    assert "EA_STATE" not in agent_env
    assert GATE_SANDBOX_STATE_ENV not in agent_env
