"""End-to-end tests for ``eawf artifact verify`` (B021)."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)
runner = CliRunner()


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Lay out a fake repo root with ``.ea/state.json`` and ``store/``."""
    ea = tmp_path / ".ea"
    ea.mkdir()
    state_path = ea / "state.json"
    shutil.copy(FIXTURE, state_path)
    (ea / "store").mkdir()
    monkeypatch.setenv("EA_STATE", str(state_path))
    return tmp_path


def _seed_artifact(
    repo_root: Path,
    *,
    artifact_id: str,
    body: bytes,
    relpath: str = ".ea/artifacts/seed.md",
    sha256: str | None = None,
) -> str:
    """Write *body* under *repo_root/relpath* and register an artifact.

    Returns the lowercase sha256 of *body* for convenience.
    """
    target = repo_root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    sha256_arg = digest if sha256 is None else sha256
    result = runner.invoke(
        app,
        [
            "artifact",
            "add",
            artifact_id,
            "--kind",
            "audit_report",
            "--uri",
            f"repo:{relpath}",
            "--sha256",
            sha256_arg,
        ],
    )
    assert result.exit_code == 0, result.stdout
    return digest


# ---- boundary --------------------------------------------------------------


def test_artifact_verify_all_no_artifacts_exits_ok(repo_root: Path) -> None:
    result = runner.invoke(app, ["--json", "artifact", "verify", "--all"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["checked"] == 0
    assert payload["results"] == []


def test_artifact_verify_single_artifact_matches(repo_root: Path) -> None:
    _seed_artifact(repo_root, artifact_id="ART-001", body=b"hello eawf")
    result = runner.invoke(app, ["--json", "artifact", "verify", "ART-001"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["checked"] == 1
    row = payload["results"][0]
    assert row["status"] == "ok"
    assert row["artifact_id"] == "ART-001"
    assert row["computed_sha256"] == row["registered_sha256"]


def test_artifact_verify_all_multiple_artifacts_ok(repo_root: Path) -> None:
    _seed_artifact(
        repo_root,
        artifact_id="ART-001",
        body=b"alpha body",
        relpath=".ea/artifacts/a.md",
    )
    _seed_artifact(
        repo_root,
        artifact_id="ART-002",
        body=b"beta body",
        relpath=".ea/artifacts/b.md",
    )
    result = runner.invoke(app, ["--json", "artifact", "verify", "--all"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["checked"] == 2
    assert payload["mismatches"] == 0
    assert payload["missing"] == 0
    statuses = {r["artifact_id"]: r["status"] for r in payload["results"]}
    assert statuses == {"ART-001": "ok", "ART-002": "ok"}


# ---- error paths -----------------------------------------------------------


def test_artifact_verify_missing_id_exits_not_found(repo_root: Path) -> None:
    result = runner.invoke(app, ["artifact", "verify", "ART-MISSING"])
    assert result.exit_code == 1
    assert "artifact not found" in result.stdout


def test_artifact_verify_missing_file_exits_integrity_violation(repo_root: Path) -> None:
    _seed_artifact(repo_root, artifact_id="ART-001", body=b"x")
    # Delete the on-disk body but keep the registered artifact entry.
    (repo_root / ".ea" / "artifacts" / "seed.md").unlink()
    result = runner.invoke(app, ["--json", "artifact", "verify", "ART-001"])
    assert result.exit_code == 3  # INTEGRITY_VIOLATION
    payload = json.loads(result.stdout)
    assert payload["missing"] == 1
    assert payload["results"][0]["status"] == "missing_file"


def test_artifact_verify_hash_mismatch_exits_integrity_violation(repo_root: Path) -> None:
    _seed_artifact(
        repo_root,
        artifact_id="ART-001",
        body=b"original",
        sha256="0" * 64,  # registered with a deliberately wrong hash.
    )
    result = runner.invoke(app, ["--json", "artifact", "verify", "ART-001"])
    assert result.exit_code == 3  # INTEGRITY_VIOLATION
    payload = json.loads(result.stdout)
    assert payload["mismatches"] == 1
    row = payload["results"][0]
    assert row["status"] == "mismatch"
    assert row["registered_sha256"] == "0" * 64
    assert row["computed_sha256"] == hashlib.sha256(b"original").hexdigest()


def test_artifact_verify_requires_id_or_all(repo_root: Path) -> None:
    result = runner.invoke(app, ["artifact", "verify"])
    assert result.exit_code == 1  # INVALID_INPUT
    assert "exactly one of <artifact-id> or --all" in result.stdout


def test_artifact_verify_rejects_id_and_all_together(repo_root: Path) -> None:
    _seed_artifact(repo_root, artifact_id="ART-001", body=b"x")
    result = runner.invoke(app, ["artifact", "verify", "ART-001", "--all"])
    assert result.exit_code == 1  # INVALID_INPUT


def test_artifact_verify_remote_uri_without_refresh_is_skipped(repo_root: Path) -> None:
    # Register an https-style remote artifact (no body in repo).
    result_add = runner.invoke(
        app,
        [
            "artifact",
            "add",
            "ART-REMOTE",
            "--kind",
            "audit_report",
            "--uri",
            "https://example.com/audit.md",
            "--sha256",
            "a" * 64,
        ],
    )
    assert result_add.exit_code == 0, result_add.stdout

    result = runner.invoke(app, ["--json", "artifact", "verify", "ART-REMOTE"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["results"][0]["status"] == "skipped_remote"


def test_artifact_verify_unregistered_hash_passes(repo_root: Path) -> None:
    """An artifact without a registered sha256 cannot mismatch — status=no_hash."""
    target = repo_root / ".ea" / "artifacts" / "no-hash.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"body")
    result_add = runner.invoke(
        app,
        [
            "artifact",
            "add",
            "ART-NOHASH",
            "--kind",
            "audit_report",
            "--uri",
            "repo:.ea/artifacts/no-hash.md",
        ],
    )
    assert result_add.exit_code == 0, result_add.stdout
    result = runner.invoke(app, ["--json", "artifact", "verify", "ART-NOHASH"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["results"][0]["status"] == "no_hash"
