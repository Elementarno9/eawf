"""The readiness floor pack executes out of process, never against the live tree.

The close-readiness compute used to run its deterministic falsifiers in the
calling process. In the daemon that meant a floor check -- routinely a whole
test suite that drives eawf's own RPCs -- reached the live runtime directory
and the live ledger: it wrote the live state, flipped the live dispatch pause
flag, and drove the production dispatch path for real.

The suite below pins the two properties that close that hole for the floor
pack and for the advisory single-gate path:

* the checks run in a child interpreter whose ``EAWF_RUNTIME_DIR`` /
  ``EA_STATE`` are bound to a throwaway sandbox, not to the live pair; and
* a floor-pack gate that deliberately mutates whatever ledger it resolves
  leaves the live tree byte identical.

Every "live" tree in this file is a fixture directory built under
``tmp_path``. The repository's own ``.ea`` is never read or written: the
readiness compute reaches the live tree only through the ``store_dir``
argument and the ``EAWF_RUNTIME_DIR`` env var, both of which each test
points at its fixture.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.enums import ProjectStatus, ScopeKind
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.kernel.store.paths import store_dir as _store_dir
from eawf.platform.profiles.models import FloorCheck, VerifyBlock
from eawf.runtime.daemon import gate_execution
from eawf.workflow.audit_dsl.models import CheckSpec
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave
from eawf.workflow.verify import readiness as readiness_mod
from eawf.workflow.verify import sandboxed_checks
from tests._criteria_helpers import legacy_criteria
from tests._session_helpers import claim_wave_with_session as claim_wave
from tests.conftest import make_claim_criterion, make_floor_waiver, make_intent

pytestmark = pytest.mark.integration

WAVE_ID = "P01-I01-W01"

_T0 = "2026-09-08T00:00:00+00:00"

#: The floor-pack probe. ``pytest`` is the allowlisted vehicle for running
#: arbitrary observation code inside a gate, so the probe is a collected
#: module: import time reports what the gate resolved, and the collected
#: test keeps the gate's exit status zero.
_PROBE_MODULE = "test_floor_probe.py"

#: Mutator probe: resolves its ledger and runtime directory exactly the way
#: every eawf RPC does, then writes to BOTH -- the shape of a floor suite
#: that drives the pause RPC and the dispatch path behind it. The report is
#: written to an absolute path handed in by env so the parent can read what
#: the gate actually reached after the sandbox is gone.
_MUTATOR_SOURCE = """
import json
import os
from pathlib import Path

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.runtime_dir import runtime_dir, socket_path

state_path, reason = resolve_with_reason(None)
resolved_runtime = runtime_dir()

mutated = False
events_after = []
if state_path.is_file():
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["dispatch_paused"] = True
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    events = store_path(state_path, StoreKind.EVENT)
    events.parent.mkdir(parents=True, exist_ok=True)
    with events.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": "EV-FLOOR"}) + "\\n")
    events_after = events.read_text(encoding="utf-8").splitlines()
    mutated = True

resolved_runtime.mkdir(parents=True, exist_ok=True)
(resolved_runtime / "floor-probe.marker").write_text("reached\\n", encoding="utf-8")

Path(os.environ["LC_FLOOR_PROBE_REPORT"]).write_text(
    json.dumps(
        {
            "state_path": str(state_path),
            "reason": reason,
            "runtime_dir": str(resolved_runtime),
            "socket_path": str(socket_path()),
            "mutated": mutated,
            "events_after": events_after,
        }
    ),
    encoding="utf-8",
)


def test_probe_collected() -> None:
    pass
"""


# ---- fixtures ---------------------------------------------------------------


def _state_payload() -> dict[str, Any]:
    """Return a minimal valid state with dispatch running."""
    return {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.REPO.value,
        "urn": "urn:eawf:v1:state:ISO",
        "updated_at": _T0,
        "project": Project(
            code="ISO",
            slug="iso",
            title="ISO",
            description=None,
            domains=["x"],
            default_branch="main",
            status=ProjectStatus.ACTIVE,
            repo_urn="urn:eawf:v1:repo:ISO",
        ).model_dump(mode="json"),
        "current": CurrentPointers(project_code="ISO").model_dump(mode="json"),
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


def _seed_wave(state: State, *, typed: bool) -> None:
    """Plan + claim the scored wave.

    Args:
        state: The in-memory state the wave is planned on.
        typed: When ``False`` the wave keeps only legacy criteria, which is
            what makes the profile floor pack render; when ``True`` the
            typed criterion stays and the floor pack is suppressed.
    """
    open_phase(state, phase_id="P01", title="phase")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="iter")
    plan_wave(
        state,
        wave_id=WAVE_ID,
        iter_id="P01-I01",
        title="wave",
        file_scopes=["src/"],
        success_criteria=[make_claim_criterion()],
        effort_bucket="M",
        intent=make_intent(),
    )
    claim_wave(state, wave_id=WAVE_ID, session_id="SES-iso")
    if not typed:
        state.waves[WAVE_ID].success_criteria = legacy_criteria("legacy a")
        state.waves[WAVE_ID].criteria_floor_waiver = make_floor_waiver()


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
    state_path = root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        State.model_validate(_state_payload()).model_dump_json(),
        encoding="utf-8",
    )
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


def _tree_signature(root: Path) -> dict[str, bytes]:
    """Return a path -> bytes map of every regular file under *root*."""
    return {
        entry.relative_to(root).as_posix(): entry.read_bytes()
        for entry in sorted(root.rglob("*"))
        if entry.is_file()
    }


def _passing_floor(name: str) -> FloorCheck:
    """A floor check whose argv exits zero in a seeded repo."""
    return FloorCheck(
        name=name,
        cmd=["git", "status", "--porcelain"],
        scope="all",
        cadence="every-wave",
        policy="warn",
    )


def _probe_floor(name: str) -> FloorCheck:
    """A floor check that runs the probe module planted in the repo root."""
    return FloorCheck(
        name=name,
        cmd=["pytest", "-p", "no:cacheprovider", "-q", _PROBE_MODULE],
        scope="all",
        cadence="every-wave",
        policy="warn",
    )


def _attach_block(monkeypatch: pytest.MonkeyPatch, block: VerifyBlock) -> None:
    """Make *block* the active profile verify block for the readiness compute."""
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda scope_id, state_arg, **kwargs: block,
    )


def _capture_child_envs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Record every env handed to a sandboxed runner child, then delegate.

    The spy wraps the production seam rather than replacing it, so the
    checks still execute for real and the recorded env is the one the child
    actually received.
    """
    seen: list[dict[str, str]] = []
    real = gate_execution.gate_child_env

    def _spy(sandbox: gate_execution.GateSandbox) -> dict[str, str]:
        env = real(sandbox)
        seen.append(dict(env))
        return env

    monkeypatch.setattr(gate_execution, "gate_child_env", _spy)
    return seen


# ---- CR-01: the floor pack runs out of process in the sandbox ---------------


def test_floor_pack_runs_out_of_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-01: the floor pack executes in a child bound to a sandbox.

    Asserts the whole chain at once: the readiness compute reached the
    sandboxed runner, the child's ``EAWF_RUNTIME_DIR`` / ``EA_STATE`` name a
    throwaway sandbox rather than the live fixture pair, the sandbox carried
    the live snapshot content (so gate reads stay faithful) without the live
    daemon's transport handles, and the floor views came from a real run.
    """
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    live_state = _write_live_state(tmp_path)
    live_runtime = _write_live_runtime_dir(tmp_path)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(live_runtime))
    envs = _capture_child_envs(monkeypatch)

    state = State.model_validate(_state_payload())
    _seed_wave(state, typed=False)
    _attach_block(
        monkeypatch,
        VerifyBlock(argv_allowlist=[], floor_checks=[_passing_floor("c-1"), _passing_floor("c-2")]),
    )

    result = readiness_mod.compute(
        WAVE_ID,
        state=state,
        store_dir=_store_dir(live_state),
        repo_root=repo_root,
    )

    floor_views = [view for view in result.criteria if view.source == "floor"]
    assert [view.id for view in floor_views] == ["c-1", "c-2"]
    assert {view.status for view in floor_views} == {"pass"}

    assert len(envs) == 1, "the floor pack did not run as one sandboxed batch"
    child_env = envs[0]
    child_runtime = Path(child_env["EAWF_RUNTIME_DIR"])
    child_state = Path(child_env["EA_STATE"])
    assert child_runtime != live_runtime, "the floor pack inherited the live runtime directory"
    assert live_runtime not in child_runtime.parents
    assert child_state != live_state, "the floor pack inherited the live ledger"
    assert live_state.parent not in child_state.parents
    assert "EAWF_SPEC_CACHE_DIR" not in child_env
    assert not child_runtime.exists(), "the sandbox outlived the floor pack that owned it"


def test_floor_pack_sandbox_is_seeded_from_the_live_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-01: the sandbox is a faithful copy, minus the live daemon handles.

    Isolation that answered reads with an empty tree would break every floor
    check that reads the ledger, so the copy has to be real; the live
    daemon's socket / PID / lock must NOT be in it, because copying them
    points a sandboxed client straight back at the live process.
    """
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    live_state = _write_live_state(tmp_path)
    live_runtime = _write_live_runtime_dir(tmp_path)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(live_runtime))

    seeded: dict[str, bool] = {}
    real_seed = gate_execution.seed_gate_sandbox

    def _spy(**kwargs: Any) -> gate_execution.GateSandbox:
        sandbox = real_seed(**kwargs)
        seeded["wal"] = (sandbox.runtime_dir / "wal" / "0001.json").is_file()
        seeded["sock"] = (sandbox.runtime_dir / "eawfd.sock").exists()
        seeded["pid"] = (sandbox.runtime_dir / "eawfd.pid").exists()
        seeded["ledger"] = sandbox.state_path.is_file()
        seeded["store"] = (sandbox.state_path.parent / "store" / "event.jsonl").is_file()
        return sandbox

    monkeypatch.setattr(gate_execution, "seed_gate_sandbox", _spy)

    state = State.model_validate(_state_payload())
    _seed_wave(state, typed=False)
    _attach_block(monkeypatch, VerifyBlock(argv_allowlist=[], floor_checks=[_passing_floor("c-1")]))

    readiness_mod.compute(
        WAVE_ID,
        state=state,
        store_dir=_store_dir(live_state),
        repo_root=repo_root,
    )

    assert seeded == {
        "wal": True,
        "sock": False,
        "pid": False,
        "ledger": True,
        "store": True,
    }


def test_floor_pack_waived_check_spawns_no_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: an all-waived floor pack runs zero checks and spawns no child."""
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    live_state = _write_live_state(tmp_path)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(_write_live_runtime_dir(tmp_path)))
    envs = _capture_child_envs(monkeypatch)

    state = State.model_validate(_state_payload())
    _seed_wave(state, typed=False)
    _attach_block(monkeypatch, VerifyBlock(argv_allowlist=[], floor_checks=[_passing_floor("c-1")]))
    monkeypatch.setattr(readiness_mod, "_floor_check_waived", lambda name, evidence: True)

    result = readiness_mod.compute(
        WAVE_ID,
        state=state,
        store_dir=_store_dir(live_state),
        repo_root=repo_root,
    )

    floor_views = [view for view in result.criteria if view.source == "floor"]
    assert [view.status for view in floor_views] == ["waived"]
    assert envs == [], "a fully waived floor pack still spawned a runner child"


# ---- CR-02: the advisory single-gate path + live-state byte identity --------


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
    assert Path(envs[0]["EA_STATE"]) != live_state


def test_live_state_byte_identical_after_floor_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-02: a state-mutating floor gate leaves the live tree byte identical.

    The floor check flips ``dispatch_paused``, appends an event row, and
    writes into the runtime directory -- all through the resolvers every RPC
    uses. Post-conditions: the live ``state.json`` bytes, the live event
    store bytes, the live ``dispatch_paused`` value, and the whole live
    runtime directory are unchanged, while the gate's own report proves the
    mutation really happened somewhere else.
    """
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    live_state = _write_live_state(tmp_path)
    live_runtime = _write_live_runtime_dir(tmp_path)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(live_runtime))
    report = tmp_path / "probe-report.json"
    # ``LC_*`` is the one prefix family the gate env-scrub floor carries, so
    # it is how the probe learns where to write a report that outlives the
    # sandbox it ran in.
    monkeypatch.setenv("LC_FLOOR_PROBE_REPORT", str(report))
    (repo_root / _PROBE_MODULE).write_text(_MUTATOR_SOURCE, encoding="utf-8")

    live_events = live_state.parent / "store" / "event.jsonl"
    state_before = live_state.read_bytes()
    events_before = live_events.read_bytes()
    runtime_before = _tree_signature(live_runtime)
    assert State.model_validate_json(state_before).dispatch_paused is False

    state = State.model_validate(_state_payload())
    _seed_wave(state, typed=False)
    _attach_block(
        monkeypatch, VerifyBlock(argv_allowlist=[], floor_checks=[_probe_floor("mutate")])
    )

    result = readiness_mod.compute(
        WAVE_ID,
        state=state,
        store_dir=_store_dir(live_state),
        repo_root=repo_root,
    )

    floor_views = [view for view in result.criteria if view.source == "floor"]
    assert [view.status for view in floor_views] == ["pass"], floor_views

    assert live_state.read_bytes() == state_before, "the floor gate rewrote the live ledger"
    assert live_events.read_bytes() == events_before, "the floor gate appended to the live store"
    assert State.model_validate_json(live_state.read_bytes()).dispatch_paused is False
    assert _tree_signature(live_runtime) == runtime_before, (
        "the floor gate wrote the live runtime directory"
    )

    seen = json.loads(report.read_text(encoding="utf-8"))
    assert seen["mutated"] is True, "the gate mutated nothing; the test proved no isolation"
    assert seen["reason"] == "env"
    assert Path(seen["state_path"]) != live_state
    assert live_state.parent not in Path(seen["state_path"]).parents
    assert len(seen["events_after"]) == 2, "the gate appended to some other event store"
    assert Path(seen["runtime_dir"]) != live_runtime
    assert Path(seen["socket_path"]).parent != live_runtime, (
        "a dispatch RPC from the floor gate would have reached the live daemon"
    )


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
        "EA_STATE": "/sandbox/.ea/state.json",
        "EAWF_DAEMONLESS": "1",
        "GH_TOKEN": "not-a-real-value-for-gh-token",
    }

    gate_env = build_child_env(GATE_RUNTIME_LANE, base_env=base)
    assert gate_env["EAWF_RUNTIME_DIR"] == "/sandbox/runtime"
    assert gate_env["EA_STATE"] == "/sandbox/.ea/state.json"
    # Containment is not a licence: the rest of the EAWF_* family and every
    # credential stay dropped.
    assert "EAWF_DAEMONLESS" not in gate_env
    assert "GH_TOKEN" not in gate_env

    agent_env = build_child_env("claude-code", base_env=base)
    assert "EAWF_RUNTIME_DIR" not in agent_env
    assert "EA_STATE" not in agent_env
