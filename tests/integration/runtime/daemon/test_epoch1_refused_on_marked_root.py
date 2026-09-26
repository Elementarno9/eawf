"""Epoch-1 mutations are refused on a tree that carries the epoch marker.

A cut-over tree keeps its epoch-1 ``state.json`` as the frozen source the
cutover digested, while native writes go to ``generations/``. Every
epoch-1 writer therefore has to refuse there -- the CLI's in-process
path, the daemon's ``state.mutate`` and the daemon's boot sweep -- or the
tree has two writers. Each test digests ``state.json`` and the event log
before the attempt and compares after, so "refused" means nothing moved.

The epoch-1 repository is laid down through the CLI, then either marked
by hand (the marker alone, as a half-done cutover leaves it) or
provisioned as a disposable canary on top of that skeleton, which is the
full epoch-2 tree the native writer must keep serving.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
from pathlib import Path
from typing import Any, Final

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME, MARKER_FILENAME
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.io import (
    LEGACY_OPERATION_REMOVED,
    LegacyOperationRemovedError,
    write_state_unlocked,
)
from eawf.kernel.state.models import State
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.run_budget import RUN_BUDGET_METER_METHOD
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests._session_helpers import seed_active_session_on_disk
from tests.conftest import make_claim_criterion
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

runner = CliRunner()

WAVE_ID: Final = "P01-I01-W01"
SESSION_ID: Final = "S"
RUN_KEY: Final = "RUN-00000010"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep canary runtime directories and the registry under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(tmp_path / "registry.json"))


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An epoch-1 repository holding one PENDING wave and one ACTIVE session."""
    root = tmp_path / "repo"
    root.mkdir()
    state_path = root / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    for argv in (
        ["project", "init", "QR", "--title", "Q", "--domains", "x"],
        ["phase", "open", "--auto", "--title", "x"],
        ["iter", "open", "--phase", "P01", "--title", "I1"],
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            WAVE_ID,
            "--title",
            "one",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
        ],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output
    state = State.model_validate(orjson.loads(state_path.read_bytes()))
    for wave in state.waves.values():
        wave.success_criteria = [make_claim_criterion()]
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))
    seed_active_session_on_disk(state_path, session_id=SESSION_ID)
    return root


def _ea(repo: Path) -> Path:
    return repo / ".ea"


def _mark(repo: Path) -> None:
    """Write the epoch marker alone, as a cutover interrupted after it would."""
    marker = _ea(repo) / GENERATIONS_DIRNAME / MARKER_FILENAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"{}")


def _canary(repo: Path) -> CanaryProvision:
    """Provision the epoch-1 repository as a declared, activated canary."""
    provisioned = provision(repo)
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _epoch1_digests(repo: Path) -> tuple[str | None, str | None]:
    """Digest ``state.json`` and the epoch-1 event log."""
    return _digest(_ea(repo) / "state.json"), _digest(_ea(repo) / "store" / "event.jsonl")


def _fallback_wal(repo: Path) -> list[str]:
    """List the in-process fallback WAL, which a refusal must not grow."""
    wal = _ea(repo) / "locks" / "wal"
    return sorted(path.name for path in wal.iterdir()) if wal.exists() else []


def _claim() -> Any:
    return runner.invoke(app, ["wave", "claim", WAVE_ID, "--session", SESSION_ID])


def _mutate(ctx: MethodContext, repo: Path) -> dict[str, Any]:
    params: dict[str, Any] = {
        "repo_root": str(repo),
        "mutation": {
            "kind": "wave_claim",
            "scope_id": WAVE_ID,
            "mutation_id": "m-0001",
            "params": {"wave_id": WAVE_ID, "session_id": SESSION_ID},
        },
    }
    return asyncio.run(methods.dispatch("state.mutate", ctx, params))


def _boot(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the daemon's synchronous boot prologue without binding a socket."""
    rt_dir = tmp_path / "runtime"
    monkeypatch.setenv("EA_STATE", str(_ea(repo) / "state.json"))
    monkeypatch.setattr(daemon_main, "ensure_runtime_dir", lambda: rt_dir)
    monkeypatch.setattr(daemon_main, "pid_path", lambda: rt_dir / "eawfd.pid")
    monkeypatch.setattr(daemon_main, "log_path", lambda: rt_dir / "eawfd.log")
    monkeypatch.setattr(daemon_main, "socket_path", lambda: rt_dir / "eawfd.sock")

    def _close(coro: object) -> None:
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(daemon_main.asyncio, "run", _close)
    assert daemon_main.run(foreground=True) == 0


def _active_sessions(repo: Path) -> list[str]:
    state = State.model_validate(orjson.loads((_ea(repo) / "state.json").read_bytes()))
    return [sid for sid, row in state.agent_sessions.items() if row.status.value == "active"]


# ---- CLI: eawf wave claim ---------------------------------------------------


def test_wave_claim_lands_on_an_unmarked_root(repo: Path) -> None:
    """The control: the same claim writes when no marker is present."""
    before = _epoch1_digests(repo)
    result = _claim()
    assert result.exit_code == exit_codes.OK, result.output
    assert _epoch1_digests(repo)[0] != before[0]


def test_wave_claim_refused_on_a_marked_root(repo: Path) -> None:
    """The gate: the claim exits with the refusal and moves nothing."""
    _mark(repo)
    before = _epoch1_digests(repo)
    wal_before = _fallback_wal(repo)
    result = _claim()
    assert result.exit_code == exit_codes.VALIDATION_ERROR, result.output
    assert LEGACY_OPERATION_REMOVED in result.output
    assert _epoch1_digests(repo) == before
    assert _fallback_wal(repo) == wal_before


def test_wave_claim_refused_on_a_provisioned_canary(repo: Path) -> None:
    """A declared, activated tree refuses the epoch-1 claim the same way."""
    _canary(repo)
    before = _epoch1_digests(repo)
    result = _claim()
    assert result.exit_code == exit_codes.VALIDATION_ERROR, result.output
    assert LEGACY_OPERATION_REMOVED in result.output
    assert _epoch1_digests(repo) == before


def test_wave_claim_lands_again_after_the_marker_is_removed(repo: Path) -> None:
    """A rollback that removes the marker hands the tree back to epoch 1."""
    _mark(repo)
    assert _claim().exit_code == exit_codes.VALIDATION_ERROR
    (_ea(repo) / GENERATIONS_DIRNAME / MARKER_FILENAME).unlink()
    result = _claim()
    assert result.exit_code == exit_codes.OK, result.output


# ---- daemon: state.mutate ---------------------------------------------------


def test_state_mutate_refused_on_a_marked_root(tmp_path: Path, repo: Path) -> None:
    """The daemon refuses before the WAL, the lock or the event log."""
    _mark(repo)
    ctx = method_context(tmp_path / "runtime")
    before = _epoch1_digests(repo)
    with pytest.raises(DaemonValidationError, match=LEGACY_OPERATION_REMOVED) as caught:
        _mutate(ctx, repo)
    assert str(caught.value).startswith(f"validation_failed: {LEGACY_OPERATION_REMOVED}")
    assert _epoch1_digests(repo) == before
    assert isinstance(ctx.wal_dir, Path)
    assert not ctx.wal_dir.exists() or not any(ctx.wal_dir.iterdir())


def test_state_mutate_lands_on_an_unmarked_root(tmp_path: Path, repo: Path) -> None:
    """The control: the same mutation commits without the marker."""
    ctx = method_context(tmp_path / "runtime")
    before = _epoch1_digests(repo)
    _mutate(ctx, repo)
    assert _epoch1_digests(repo)[0] != before[0]


# ---- daemon boot sweep ------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="boot path needs the POSIX runtime")
def test_daemon_boot_writes_nothing_on_a_marked_root(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphan reconcile would flip the ACTIVE session; on a marked root it does not."""
    _mark(repo)
    before = _epoch1_digests(repo)
    _boot(tmp_path, repo, monkeypatch)
    assert _epoch1_digests(repo) == before
    assert _active_sessions(repo) == [SESSION_ID]


@pytest.mark.skipif(sys.platform == "win32", reason="boot path needs the POSIX runtime")
def test_daemon_boot_reconciles_an_unmarked_root(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: without the marker the boot sweep does rewrite state.json."""
    before = _epoch1_digests(repo)
    _boot(tmp_path, repo, monkeypatch)
    assert _epoch1_digests(repo)[0] != before[0]
    assert _active_sessions(repo) == []


def test_schedule_sweeps_skip_a_marked_root(tmp_path: Path, repo: Path) -> None:
    """The periodic session-TTL and stale-wave sweeps are never scheduled there."""
    _mark(repo)
    ctx = method_context(tmp_path / "runtime")
    ctx.state_path = _ea(repo) / "state.json"
    assert daemon_main._schedule_session_ttl_sweep(ctx) is None
    assert daemon_main._schedule_stale_wave_sweep(ctx) is None


# ---- the epoch-2 writer keeps working ---------------------------------------


def test_native_writer_commits_on_a_marked_root(tmp_path: Path, repo: Path) -> None:
    """A native verb over the cap still appends its ledger; state.json stays frozen."""
    provisioned = _canary(repo)
    ctx = method_context(tmp_path / "runtime")
    before = _epoch1_digests(repo)
    params: dict[str, Any] = {
        "repo_root": str(provisioned.root),
        "urn": f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}",
        "control_request_ref": "CTL-0000000a",
        "actor": "OP-0001",
        "samples": [{"input_tokens": 100, "output_tokens": 5_000}],
        "base_budget": 1_000,
        "enforce": "hard",
        "multiplier": 1.0,
        "idempotency_key": "idem-budget",
    }
    asyncio.run(methods.dispatch(RUN_BUDGET_METER_METHOD, ctx, params))
    ledger = ledger_path(document_path(provisioned), Epoch2Collection.RUN)
    kinds = [item.payload.get("payload_kind") for item in read_ledger_records(ledger)]
    assert kinds == ["budget_notice", "control", "control"]
    # The native firehose shares the event log, so only state.json is frozen.
    assert _epoch1_digests(repo)[0] == before[0]


def test_write_state_unlocked_admits_the_trees_own_native_authority(
    tmp_path: Path, repo: Path
) -> None:
    """Only the epoch-2 answer for this very tree lets the v1 document be written."""
    _canary(repo)
    other = tmp_path / "other"
    other.mkdir()
    provision(other, code="OTH")
    state_path = _ea(repo) / "state.json"
    payload = orjson.loads(state_path.read_bytes())

    with pytest.raises(LegacyOperationRemovedError):
        write_state_unlocked(state_path, payload)
    with pytest.raises(LegacyOperationRemovedError):
        write_state_unlocked(state_path, payload, native_authority=resolve_authority(_ea(other)))
    with pytest.raises(LegacyOperationRemovedError):
        write_state_unlocked(
            state_path, payload, native_authority=resolve_authority(tmp_path / "plain")
        )

    write_state_unlocked(state_path, payload, native_authority=resolve_authority(_ea(repo)))
    assert orjson.loads(state_path.read_bytes()) == payload
