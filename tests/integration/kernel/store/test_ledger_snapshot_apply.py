"""A sealed ledger passes the append-only gate and is applied by the daemon's doctor repair.

The gate runs over real commits in a scratch repository: a seal whose
snapshot holds exactly the removed bytes and hashes to its name is reported
as a sealed removal, and a removal, a forged snapshot or a tampered one is a
finding. The doctor repair previews the seal and the daemon method applies it
under the session lock, refusing a preview the ledger has moved past.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import plan_legacy_seal, read_document, write_document
from eawf.kernel.store.ledger import (
    SNAPSHOT_DIRNAME,
    append_ledger_record,
    ledger_snapshot,
    render_ledger_line,
    seal_ledger_prefix,
    snapshot_name,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.doctor.checks import CheckResult
from eawf.observability.doctor.repair import (
    LEGACY_SEAL_ACTION,
    build_repair_plan,
    digest_repair_actions,
)
from eawf.platform.lint.ledger_history import scan_ledger_history
from eawf.runtime.daemon import methods
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context
from tests.unit.kernel.store.test_ledger_snapshot import (
    LEGACY_MARK,
    _digests,
    _mixed_ledger,
    _record,
)

runner = CliRunner()


# --- the append-only ledger gate -----------------------------------------------------

_LEDGER_REL: Final = ".ea/generations/gen-x/ledger/task.jsonl"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def gate_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "canary@example.com")
    _git(root, "config", "user.name", "canary")
    _mixed_ledger(root / _LEDGER_REL)
    _commit_all(root, "state: base ledger")
    return root


def test_gate_accepts_a_seal_and_reports_the_removal_sealed(gate_repo: Path) -> None:
    ledger = gate_repo / _LEDGER_REL
    plan = plan_legacy_seal(ledger)
    assert plan is not None
    snapshot = seal_ledger_prefix(ledger, plan.cut)
    append_ledger_record(ledger, _record("EAWF-0050"))
    _commit_all(gate_repo, "chore: seal the legacy import")

    report = scan_ledger_history(gate_repo, "HEAD~1..HEAD")

    assert report.clean, report
    (removal,) = report.sealed
    assert removal.path == _LEDGER_REL
    assert removal.snapshot == snapshot.relative_to(gate_repo).as_posix()
    assert removal.sealed_bytes == plan.cut


def test_gate_accepts_a_seal_in_the_working_tree(gate_repo: Path) -> None:
    ledger = gate_repo / _LEDGER_REL
    plan = plan_legacy_seal(ledger)
    assert plan is not None
    seal_ledger_prefix(ledger, plan.cut)

    report = scan_ledger_history(gate_repo, "HEAD")

    assert report.clean, report
    assert len(report.sealed) == 1


def test_gate_reds_on_a_removal_no_snapshot_explains(gate_repo: Path) -> None:
    ledger = gate_repo / _LEDGER_REL
    content = ledger.read_bytes()
    ledger.write_bytes(content[content.index(b"\n") + 1 :])
    _commit_all(gate_repo, "chore: drop a line")

    report = scan_ledger_history(gate_repo, "HEAD~1..HEAD")

    (finding,) = report.append_only
    assert finding.path == _LEDGER_REL
    assert report.sealed == ()


def test_gate_reds_on_a_snapshot_that_is_not_the_removed_bytes(gate_repo: Path) -> None:
    ledger = gate_repo / _LEDGER_REL
    content = ledger.read_bytes()
    cut = content.index(b"\n") + 1
    forged = render_ledger_line(_record("EAWF-0777")).encode() + b"\n"
    target = ledger.parent / SNAPSHOT_DIRNAME / snapshot_name("task", forged)
    target.parent.mkdir()
    target.write_bytes(forged)
    ledger.write_bytes(content[cut:])
    _commit_all(gate_repo, "chore: forge a seal")

    report = scan_ledger_history(gate_repo, "HEAD~1..HEAD")

    assert [f.path for f in report.append_only] == [_LEDGER_REL]
    assert report.sealed == ()


def test_gate_reds_on_a_tampered_snapshot(gate_repo: Path) -> None:
    ledger = gate_repo / _LEDGER_REL
    plan = plan_legacy_seal(ledger)
    assert plan is not None
    snapshot = seal_ledger_prefix(ledger, plan.cut)
    _commit_all(gate_repo, "chore: seal")
    snapshot.write_bytes(snapshot.read_bytes().replace(b"EAWF-0002", b"EAWF-0003"))
    _commit_all(gate_repo, "chore: tamper")

    report = scan_ledger_history(gate_repo, "HEAD~1..HEAD")

    paths = [f.path for f in report.append_only]
    assert snapshot.relative_to(gate_repo).as_posix() in paths
    assert _LEDGER_REL in paths


# --- the doctor repair, applied by the daemon method -----------------------------------


@pytest.fixture
def managed_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An epoch-2 repository with one project and a Task ledger carrying a legacy import."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(tmp_path / "home" / ".eawf" / "registry.json"))
    for target, value in (
        ("global_config_path", lambda: tmp_path / "missing-global-config.yaml"),
        ("check_manifest_in_sync", lambda **_k: CheckResult(name="manifest_in_sync", status="ok")),
        ("_desired_sync_drift", lambda _workspace: False),
        ("check_launchd_agent", lambda: CheckResult(name="launchd_agent", status="ok")),
        ("find_stray_daemons", lambda: []),
    ):
        monkeypatch.setattr(f"eawf.observability.doctor.repair.{target}", value)
    root = tmp_path / "repo"
    init = ["--no-input", "init", "--target", str(root), "--project-code", "DEMO"]
    assert runner.invoke(app, init).exit_code == exit_codes.OK
    document_path = _document(root)
    document = read_document(document_path)
    document.setdefault("project", {})["DEMO"] = {"payload": {"code": "DEMO"}}
    write_document(document_path, document)
    _mixed_ledger(ledger_path(document_path, Epoch2Collection.TASK))
    return root


def _document(root: Path) -> Path:
    authority = require_native_authority(root / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def _apply(root: Path, runtime: Path, action_ids: list[str]) -> dict[str, Any]:
    plan = build_repair_plan(root)
    selected = [a for a in plan.actions if a.action_id in action_ids]
    params = {
        "repo_root": str(root),
        "preview_digest": plan.preview_digest,
        "selected_preview_digest": digest_repair_actions(selected),
        "action_ids": action_ids,
        "action_previews": {a.action_id: a.preview_digest for a in selected},
    }
    methods.ensure_all_methods_registered()
    answer = asyncio.run(methods.dispatch("doctor.apply_repair", method_context(runtime), params))
    assert isinstance(answer, dict)
    return answer


def test_doctor_previews_then_the_daemon_seals_the_legacy_import(
    managed_repo: Path, tmp_path: Path
) -> None:
    ledger = ledger_path(_document(managed_repo), Epoch2Collection.TASK)
    before = _digests(ledger)
    (action,) = [
        a for a in build_repair_plan(managed_repo).actions if a.action_id == LEGACY_SEAL_ACTION
    ]
    assert action.mutation_class == "committed_store"
    assert action.record_count == 6
    assert ledger_snapshot(ledger) is None

    answer = _apply(managed_repo, tmp_path / "runtime", [LEGACY_SEAL_ACTION])

    (result,) = answer["actions"]
    assert result["status"] == "applied"
    assert result["record_count"] == 6
    assert ledger_snapshot(ledger) is not None
    assert _digests(ledger) == before
    assert LEGACY_MARK not in ledger.read_bytes()
    assert LEGACY_SEAL_ACTION not in [a.action_id for a in build_repair_plan(managed_repo).actions]


def test_daemon_refuses_a_seal_the_ledger_moved_past(managed_repo: Path, tmp_path: Path) -> None:
    from eawf.runtime.daemon.methods.doctor import _apply_legacy_seal

    ledger = ledger_path(_document(managed_repo), Epoch2Collection.TASK)
    (action,) = [
        a for a in build_repair_plan(managed_repo).actions if a.action_id == LEGACY_SEAL_ACTION
    ]
    append_ledger_record(ledger, _record("EAWF-0020", origin="legacy"))
    content = ledger.read_bytes()

    with pytest.raises(methods.DaemonValidationError, match="preview changed"):
        _apply_legacy_seal(method_context(tmp_path / "rt"), managed_repo, action)

    assert ledger.read_bytes() == content
    assert ledger_snapshot(ledger) is None
