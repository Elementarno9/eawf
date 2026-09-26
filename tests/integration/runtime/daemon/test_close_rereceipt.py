"""The ``close.rereceipt`` daemon method over a CLOSED wave's gates.

Thirty-four P32 waves closed carrying required gates and zero receipts, and
no verb could go back and produce the missing proof. ``close.rereceipt``
is that verb: it replays a closed wave's recorded gates in a detached
workspace at the commit the wave landed on, persists one receipt per gate,
and appends one ``gate_rereceipt`` binding row naming the landed SHA, the
receipt ids and both manifest digests.

The gates here run for real -- the same out-of-process child the durable
close drives -- against pytest modules committed into a fixture repository,
so the workspace, the argv policy and the receipt persistence are all
exercised end to end rather than stubbed. Coverage:

* happy path: two required gates produce two receipts and exactly one
  binding row carrying the landed SHA, both receipt ids and both manifest
  digests;
* binding proof: the gates travel through
  :func:`~eawf.runtime.daemon.gate_execution.run_gate_out_of_process` at a
  workspace prepared at ``Wave.commit``, and that workspace is gone
  afterwards;
* immutability: the wave's ``frozen_wave_fingerprint`` is byte-identical
  across the run, and a second run appends a SECOND row with fresh
  receipt ids rather than rewriting the first;
* boundary: a wave carrying exactly one gate binds exactly one receipt.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.gate_receipt import GateReceipt
from eawf.kernel.store.kinds.gate_rereceipt import GateRereceiptBinding
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION, gate_execution
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods import close_rereceipt as rereceipt_method
from eawf.runtime.daemon.methods.close_rereceipt import rereceipt
from eawf.workflow.lifecycle.gate_repoint import frozen_wave_fingerprint

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
_WAVE_ID = "P32-I01-W07"

#: A committed pytest module whose single test passes: the cheapest honest
#: ``command_exit_zero`` gate a fixture repository can carry at a landed
#: commit.
_PASS_MODULE = "probe_pass.py"
_PASS_BODY = "def test_probe_passes() -> None:\n    assert True\n"

#: The paired always-failing module, so a wave can carry one gate of each
#: verdict without the fixture inventing a second repository.
_FAIL_MODULE = "probe_fail.py"
_FAIL_BODY = (
    'def test_probe_fails() -> None:\n    raise AssertionError("probe failed on purpose")\n'
)


def git(repo: Path, *args: str) -> str:
    """Run one Git command in *repo* and return its stripped stdout."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def build_repo(tmp_path: Path) -> tuple[Path, str]:
    """Build a fixture repository holding both probe modules.

    Returns:
        The repository root and the landed commit SHA the gates run at.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / _PASS_MODULE).write_text(_PASS_BODY, encoding="utf-8")
    (repo / _FAIL_MODULE).write_text(_FAIL_BODY, encoding="utf-8")
    git(repo, "add", _PASS_MODULE, _FAIL_MODULE)
    git(repo, "commit", "-m", "test: land the probe gates")
    landed = git(repo, "rev-parse", "HEAD")
    # A later commit keeps the pin a strict ancestor rather than HEAD
    # itself, which is the shape every historical wave actually has.
    (repo / "follow_up.txt").write_text("later\n", encoding="utf-8")
    git(repo, "add", "follow_up.txt")
    git(repo, "commit", "-m", "test: land later work")
    return repo, landed


def criterion(index: int) -> dict[str, Any]:
    """A deterministic criterion row bound to gate ``GATE-<index>``."""
    return {
        "id": f"CR-{index:02d}",
        "text": f"the probe gate {index} exits zero at the landed revision",
        "kind": "deterministic",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "gate_ids": [f"GATE-{index:02d}"],
        "quality_dimension": "functional_suitability",
        "measurable_signal": f"probe gate {index} exit status is zero",
    }


def gate(index: int, module: str) -> dict[str, Any]:
    """A ``command_exit_zero`` gate row collecting one committed module."""
    return {
        "id": f"GATE-{index:02d}",
        "criterion_id": f"CR-{index:02d}",
        "kind": "command_exit_zero",
        "args": {
            "argv": ["pytest", "-p", "no:cacheprovider", "-q", module],
            "scope": "all",
        },
        "policy": "block",
        "cadence": "every-wave",
    }


def build_state_payload(
    *,
    commit: str | None,
    criteria: list[dict[str, Any]],
    gates: list[dict[str, Any]],
    status: str = "closed",
) -> dict[str, Any]:
    """A minimal valid State carrying one wave under P32-I01."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": _T0.isoformat(),
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "EAWF",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {"project_code": "EAWF"},
        "workspace": None,
        "phases": {
            "P32": {
                "id": "P32",
                "scope_id": "EAWF",
                "track_id": None,
                "title": "P32",
                "status": "active",
                "iter_ids": ["P32-I01"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P32-I01": {
                "id": "P32-I01",
                "phase_id": "P32",
                "title": "I01",
                "status": "closed",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": _T0.isoformat(),
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P32-I01",
                "title": "closed wave whose gates never produced a receipt",
                "status": status,
                "file_scopes": ["probe_pass.py"],
                "success_criteria": criteria,
                "gates": gates,
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "closed_at": _T0.isoformat() if status == "closed" else None,
                "outcome": "ok" if status == "closed" else None,
                "commit": commit,
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def write_state(repo: Path, payload: dict[str, Any]) -> Path:
    """Persist *payload* at ``<repo>/.ea/state.json`` and return the path."""
    state_path = repo / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = State.model_validate(payload)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


def build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    """A daemon method context anchored on the fixture ledger."""
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at=_T0.isoformat(),
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


def run(body: Callable[[], Awaitable[None]]) -> None:
    """Drive one async daemon-method body to completion."""
    asyncio.run(body())


def read_bindings(state_path: Path) -> list[GateRereceiptBinding]:
    """Return every persisted re-receipt binding, oldest first."""
    path = store_path(state_path, StoreKind.GATE_RERECEIPT)
    if not path.is_file():
        return []
    return [
        GateRereceiptBinding.model_validate(
            Envelope.model_validate(orjson.loads(line)).payload,
        )
        for line in path.read_bytes().splitlines()
        if line.strip()
    ]


def read_receipts(state_path: Path) -> dict[str, GateReceipt]:
    """Return every persisted gate receipt keyed by receipt id."""
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    if not path.is_file():
        return {}
    receipts = [
        GateReceipt.model_validate(Envelope.model_validate(orjson.loads(line)).payload)
        for line in path.read_bytes().splitlines()
        if line.strip()
    ]
    return {receipt.id: receipt for receipt in receipts}


def load_wave(state_path: Path) -> Any:
    """Return the fixture wave row as currently persisted."""
    return State.model_validate(orjson.loads(state_path.read_bytes())).waves[_WAVE_ID]


def two_gate_state(repo: Path, landed: str) -> Path:
    """Write the two-required-gate fixture state and return its ledger path."""
    return write_state(
        repo,
        build_state_payload(
            commit=landed,
            criteria=[criterion(1), criterion(2)],
            gates=[gate(1, _PASS_MODULE), gate(2, _PASS_MODULE)],
        ),
    )


def test_rereceipt_binds_two_receipts_for_a_closed_wave(tmp_path: Path) -> None:
    """CR-01: two gates produce two receipts and exactly one binding row."""
    repo, landed = build_repo(tmp_path)
    state_path = two_gate_state(repo, landed)
    ctx = build_ctx(tmp_path, state_path)
    expected = frozen_wave_fingerprint(load_wave(state_path))

    async def body() -> None:
        result = await rereceipt(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo)})
        assert result["operation"] == "rereceipt"
        assert result["landed_sha"] == landed
        assert result["passed_count"] == 2, result["gates"]
        assert result["failed_count"] == 0
        assert len(result["receipt_ids"]) == 2

    run(body)

    bindings = read_bindings(state_path)
    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.wave_id == _WAVE_ID
    assert binding.landed_sha == landed
    assert binding.landed_tree_sha == git(repo, "rev-parse", f"{landed}^{{tree}}")
    assert binding.criteria_digest
    assert binding.gate_manifest_digest
    assert binding.criteria_digest != binding.gate_manifest_digest
    assert [row.gate_id for row in binding.gates] == ["GATE-01", "GATE-02"]
    assert len(binding.receipt_ids) == 2

    receipts = read_receipts(state_path)
    assert set(binding.receipt_ids) <= set(receipts)
    for receipt_id in binding.receipt_ids:
        receipt = receipts[receipt_id]
        assert receipt.scope_id == _WAVE_ID
        assert receipt.integrated_sha == landed
        assert receipt.result.value == "pass"
        assert receipt.integration_id == binding.id

    assert frozen_wave_fingerprint(load_wave(state_path)) == expected


def test_rereceipt_runs_gates_out_of_process_at_the_landed_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-01: the runner is the shared gate child, bound to the landed tree."""
    repo, landed = build_repo(tmp_path)
    state_path = two_gate_state(repo, landed)
    ctx = build_ctx(tmp_path, state_path)

    prepared: list[tuple[str, Path]] = []
    real_prepare = rereceipt_method.prepare_close_workspace

    def _spy_prepare(repo_root: Path, **kwargs: Any) -> Any:
        workspace = real_prepare(repo_root, **kwargs)
        prepared.append((workspace.commit_sha, workspace.path))
        return workspace

    monkeypatch.setattr(rereceipt_method, "prepare_close_workspace", _spy_prepare)

    ran: list[tuple[str, Path]] = []
    real_runner = gate_execution.run_gate_out_of_process

    def _spy_runner(spec: Any, **kwargs: Any) -> Any:
        ran.append((kwargs["gate_id"], Path(kwargs["cwd"])))
        return real_runner(spec, **kwargs)

    monkeypatch.setattr(gate_execution, "run_gate_out_of_process", _spy_runner)

    async def body() -> None:
        result = await rereceipt(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo)})
        assert result["passed_count"] == 2

    run(body)

    assert len(prepared) == 1, "the landed workspace was not prepared exactly once"
    workspace_commit, workspace_path = prepared[0]
    assert workspace_commit == landed
    assert [gate_id for gate_id, _cwd in ran] == ["GATE-01", "GATE-02"]
    assert {cwd for _gate_id, cwd in ran} == {workspace_path}
    assert not workspace_path.exists(), "the close workspace outlived the re-receipt"


def test_rereceipt_second_run_appends_a_new_row(tmp_path: Path) -> None:
    """CR-01: re-receipting twice appends, it never rewrites the first row."""
    repo, landed = build_repo(tmp_path)
    state_path = two_gate_state(repo, landed)
    ctx = build_ctx(tmp_path, state_path)
    expected = frozen_wave_fingerprint(load_wave(state_path))

    async def body() -> None:
        await rereceipt(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo)})
        await rereceipt(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo)})

    run(body)

    bindings = read_bindings(state_path)
    assert len(bindings) == 2
    first, second = bindings
    assert first.id != second.id
    assert first.landed_sha == second.landed_sha == landed
    assert first.criteria_digest == second.criteria_digest
    assert set(first.receipt_ids).isdisjoint(second.receipt_ids), (
        "a re-run reused the earlier run's receipt ids"
    )
    assert set(first.receipt_ids) | set(second.receipt_ids) <= set(read_receipts(state_path))
    assert frozen_wave_fingerprint(load_wave(state_path)) == expected


def test_rereceipt_single_gate_wave_binds_one_receipt(tmp_path: Path) -> None:
    """CR-01 boundary: the smallest gate-bearing wave binds exactly one row."""
    repo, landed = build_repo(tmp_path)
    state_path = write_state(
        repo,
        build_state_payload(
            commit=landed,
            criteria=[criterion(1)],
            gates=[gate(1, _PASS_MODULE)],
        ),
    )
    ctx = build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await rereceipt(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo)})
        assert result["passed_count"] == 1
        assert result["failed_count"] == 0

    run(body)

    bindings = read_bindings(state_path)
    assert len(bindings) == 1
    assert len(bindings[0].gates) == 1
    assert len(bindings[0].receipt_ids) == 1


def _land_fix_for_failing_probe(repo: Path) -> str:
    """Commit a fix that turns the failing probe green; return the fix SHA."""
    (repo / _FAIL_MODULE).write_text(_PASS_BODY, encoding="utf-8")
    git(repo, "add", _FAIL_MODULE)
    git(repo, "commit", "-m", "test: fix the failing probe")
    return git(repo, "rev-parse", "HEAD")


def test_rereceipt_at_rebinds_receipts_to_the_given_commit(tmp_path: Path) -> None:
    """A gate red at its landed commit passes when re-bound to the fix commit."""
    repo, landed = build_repo(tmp_path)
    fix = _land_fix_for_failing_probe(repo)
    state_path = write_state(
        repo,
        build_state_payload(
            commit=landed,
            criteria=[criterion(1)],
            gates=[gate(1, _FAIL_MODULE)],
        ),
    )
    ctx = build_ctx(tmp_path, state_path)
    expected = frozen_wave_fingerprint(load_wave(state_path))

    async def body() -> None:
        result = await rereceipt(
            ctx,
            {"wave_id": _WAVE_ID, "repo_root": str(repo), "at": fix[:12]},
        )
        assert result["landed_sha"] == landed
        assert result["bound_sha"] == fix
        assert result["passed_count"] == 1, result["gates"]

    run(body)

    (binding,) = read_bindings(state_path)
    assert binding.landed_sha == landed
    assert binding.landed_tree_sha == git(repo, "rev-parse", f"{landed}^{{tree}}")
    assert binding.bound_sha == fix
    assert binding.bound_tree_sha == git(repo, "rev-parse", f"{fix}^{{tree}}")
    (receipt_id,) = binding.receipt_ids
    receipt = read_receipts(state_path)[receipt_id]
    assert receipt.result.value == "pass"
    assert receipt.integrated_sha == fix
    assert receipt.tree_sha == binding.bound_tree_sha
    assert frozen_wave_fingerprint(load_wave(state_path)) == expected


def test_rereceipt_at_the_landed_commit_records_no_rebind(tmp_path: Path) -> None:
    """Boundary: ``at`` equal to the landed commit is a plain landed run."""
    repo, landed = build_repo(tmp_path)
    state_path = write_state(
        repo,
        build_state_payload(
            commit=landed,
            criteria=[criterion(1)],
            gates=[gate(1, _PASS_MODULE)],
        ),
    )
    ctx = build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await rereceipt(
            ctx,
            {"wave_id": _WAVE_ID, "repo_root": str(repo), "at": landed},
        )
        assert result["bound_sha"] is None
        assert result["passed_count"] == 1

    run(body)

    (binding,) = read_bindings(state_path)
    assert binding.bound_sha is None
    assert binding.bound_tree_sha is None
    assert read_receipts(state_path)[binding.receipt_ids[0]].integrated_sha == landed
