"""A Batch close completes its Tasks on a binding that writes no unverified digest.

The Task's integrated binding records the head, the tree and the contract
revision; the contract and evidence digests were never recomputed by any
reader, so a new row leaves them out. The Batch's own head binding keeps
them. The closed tree stays healthy under ``eawf doctor``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.epoch2 import Task
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.platform.install.instrument_probe import PROBE_VERSION, ProbeReport, ProbeResult
from eawf.runtime.daemon.batch_close import close_landed_batch
from eawf.surfaces.cli.app import app
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    TASK_URN,
    document_path,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
)

ACTOR: Final = "OP-0001"
BRANCH: Final = "feature/eawf-v0.7"
DIGEST_KEYS: Final = frozenset({"contract_digest", "evidence_digest"})

runner = CliRunner()


@pytest.fixture(autouse=True)
def _sandbox_host_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's global config, live daemons and host probe out of doctor."""
    from eawf.runtime.daemon.service_install import SupervisedAgentReport

    report = SupervisedAgentReport(
        supervisor="none",
        label="",
        installed=False,
        loaded=False,
        program=None,
        drift=False,
        rival_pid=None,
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install.detect_supervised_agent",
        lambda *_a, **_k: report,
    )
    monkeypatch.setattr(
        "eawf.kernel.config.layered.global_config_path",
        lambda: tmp_path / "global-config.yaml",
    )
    monkeypatch.setattr(
        "eawf.observability.doctor.checks._probe_running_daemon_version", lambda: None
    )
    monkeypatch.setattr("eawf.observability.doctor.daemon_strays.find_stray_daemons", lambda: [])
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)


def _green_probe(*_args: object, **_kwargs: object) -> ProbeReport:
    return ProbeReport(
        probe_version=PROBE_VERSION,
        profile_ids=["core"],
        results=[
            ProbeResult(name="git", kind="hard", status="ok", path="git"),
            ProbeResult(name="python", kind="hard", status="ok", path="python"),
            ProbeResult(name="uv", kind="hard", status="ok", path="uv"),
        ],
    )


def _git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, path: str, message: str) -> str:
    """Commit one new file at *path* with *message* and return the commit."""
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# {message}\n", encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _task(key: str) -> dict[str, Any]:
    """Return a READY_TO_INTEGRATE Task keyed *key* in the Batch under test."""
    row = rekeyed(seed_row("task", "READY_TO_INTEGRATE"), key=key)
    row["batch_ref"] = BATCH_URN
    row.pop("active_run_ref", None)
    return row


def _tree(tmp_path: Path, keys: list[str]) -> tuple[CanaryProvision, Path]:
    """Provision a canary whose PLANNED Batch lists the ready Tasks *keys*."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", BRANCH)
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "ci")
    _commit(repo, "src/module.py", "initial")
    canary = provision(repo, code="BTB")
    batch = rekeyed(seed_row("batch", "PLANNED"), key="BAT-0007")
    batch["task_refs"] = [f"{TASK_URN.rsplit('/', 1)[0]}/{key}" for key in keys]
    seed(
        canary,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {"MLS-0030": seed_row("milestone", "ACTIVE")},
            "batch": {"BAT-0007": batch},
            "task": {key: _task(key) for key in keys},
        },
    )
    return canary, repo


def _payload(canary: CanaryProvision, collection: Epoch2Collection, key: str) -> Any:
    """Return the standing ledger payload of *key*."""
    lines = read_ledger_records(ledger_path(document_path(canary), collection))
    return next(item.payload for item in effective_records(lines) if item.record_key == key)


def _close(canary: CanaryProvision, tmp_path: Path) -> str:
    """Close the Batch under test and return the head it closed on."""
    closure, _ = close_landed_batch(
        root_context(canary, tmp_path / "runtime"),
        parse_qualified_urn(BATCH_URN),
        actor=ACTOR,
        now=AT,
    )
    assert closure.closed, closure.reason
    assert closure.head_sha is not None
    return closure.head_sha


def test_batch_close_writes_no_task_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary, repo = _tree(tmp_path, ["EAWF-0042"])
    _commit(repo, "src/alpha.py", "feat: alpha\n\nTask: EAWF-0042")

    head = _close(canary, tmp_path)

    row = _payload(canary, Epoch2Collection.TASK, "EAWF-0042")
    binding = row["integrated_binding"]
    assert DIGEST_KEYS.isdisjoint(binding)
    assert binding == {
        "head_sha": head,
        "tree_sha": _git(repo, "rev-parse", f"{head}^{{tree}}"),
        "policy_revision": row["contract_revision"],
    }
    assert Task.model_validate(row).integrated_binding is not None
    monkeypatch.chdir(repo)
    doctor = runner.invoke(app, ["--json", "-w", str(repo), "doctor"])
    failing = [
        check["name"]
        for check in json.loads(doctor.stdout)["checks"]
        if check["status"] not in {"ok", "skip", "info"}
    ]
    assert doctor.exit_code == 0, failing


def test_the_batch_head_binding_keeps_its_digests(tmp_path: Path) -> None:
    canary, repo = _tree(tmp_path, ["EAWF-0042", "EAWF-0043"])
    _commit(repo, "src/alpha.py", "feat: alpha\n\nTask: EAWF-0042")

    _close(canary, tmp_path)

    batch = _payload(canary, Epoch2Collection.BATCH, "BAT-0007")
    assert set(batch["current_head_binding"]) >= DIGEST_KEYS
    for key in ("EAWF-0042", "EAWF-0043"):
        binding = _payload(canary, Epoch2Collection.TASK, key)["integrated_binding"]
        assert DIGEST_KEYS.isdisjoint(binding)
        assert binding["head_sha"] == batch["current_head_binding"]["head_sha"]
