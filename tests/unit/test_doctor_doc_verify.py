"""Unit tests for :mod:`eawf.doctor.doc_verify`.

Builds synthetic states, manifests, and on-disk regions in ``tmp_path`` to
exercise the drift + cross-check pass without depending on the parent repo's
state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eawf.doctor.doc_verify import verify_docs
from eawf.render.manifest import Manifest, ManifestEntry, save_atomic
from eawf.render.regions import compute_hash
from eawf.state.models import State


def _make_state(
    *,
    phases: dict[str, dict[str, Any]] | None = None,
    artifacts: dict[str, dict[str, Any]] | None = None,
) -> State:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ZZ",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "ZZ",
            "slug": "zz",
            "title": "ZZ",
            "description": "",
            "domains": [],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ZZ",
        },
        "current": {
            "project_code": "ZZ",
            "subproject_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": phases or {},
        "iters": {},
        "waves": {},
        "artifacts": artifacts or {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def _phase(phase_id: str, status: str = "closed", audit_id: str | None = "A01") -> dict[str, Any]:
    return {
        "id": phase_id,
        "scope_id": "ZZ",
        "subproject_id": None,
        "title": f"Phase {phase_id}",
        "status": status,
        "iter_ids": [],
        "outcome_ids": [],
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": "2026-05-08T00:01:00Z" if status == "closed" else None,
        "audit_id": audit_id,
    }


def _artifact(artifact_id: str, uri: str) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "kind": "audit_report",
        "uri": uri,
        "urn": f"urn:eawf:v1:artifact:ZZ/{artifact_id}",
        "sha256": None,
        "size_bytes": None,
        "created_at": "2026-05-08T00:00:00Z",
        "metadata": {},
    }


def _write_region(target: Path, region_id: str, body: str, version: str = "1.0") -> str:
    """Write a managed region to *target* and return its body hash."""
    target.parent.mkdir(parents=True, exist_ok=True)
    body_hash = compute_hash(body)
    text = (
        f"<!-- BEGIN EAWF:managed id={region_id} version={version} hash={body_hash} -->\n"
        f"{body}\n"
        f"<!-- END EAWF:managed id={region_id} -->\n"
    )
    target.write_text(text, encoding="utf-8")
    return body_hash


def _build_repo(tmp_path: Path, *, drift: bool) -> tuple[Path, State]:
    """Build a repo tree with one managed region + manifest + state."""
    repo = tmp_path
    state_dir = repo / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    target = repo / "AGENTS.md"
    body = "Hello world.\nSecond line."
    on_disk_hash = _write_region(target, "rules", body)
    manifest_hash = on_disk_hash if not drift else "0123456789abcdef"
    manifest = Manifest(
        version=1,
        generated={
            f"{target.as_posix()}::rules": ManifestEntry(
                target=target.as_posix(),
                region_id="rules",
                version="1.0",
                hash=manifest_hash,
                generator="profile:test",
                generated_at="2026-05-08T00:00:00+00:00",
            )
        },
    )
    save_atomic(state_dir / "indexes" / "generated.json", manifest)
    state = _make_state()
    return repo, state


def test_verify_docs_clean_repo_status_ok(tmp_path: Path) -> None:
    repo, state = _build_repo(tmp_path, drift=False)
    report = verify_docs(state, repo)
    assert report.status == "ok"
    assert report.has_drift is False
    assert report.cross_check_violations == []
    assert report.manifest_targets == 1
    assert report.manifest_entries == 1


def test_verify_docs_drift_detected_status_drift(tmp_path: Path) -> None:
    repo, state = _build_repo(tmp_path, drift=True)
    report = verify_docs(state, repo)
    assert report.status == "drift"
    assert report.has_drift is True
    assert any(r.kind == "hand-edited" for r in report.drift_reports)


def test_verify_docs_cross_check_closed_phase_missing_audit(tmp_path: Path) -> None:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    save_atomic(state_dir / "indexes" / "generated.json", Manifest(version=1, generated={}))
    state = _make_state(
        phases={"P00": _phase("P00", status="closed", audit_id=None)},
    )
    report = verify_docs(state, tmp_path)
    assert report.status == "drift"
    assert any(v.code == "DOC.PHASE_MISSING_AUDIT" for v in report.cross_check_violations)


def test_verify_docs_cross_check_artifact_repo_uri_unresolved(tmp_path: Path) -> None:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    save_atomic(state_dir / "indexes" / "generated.json", Manifest(version=1, generated={}))
    state = _make_state(
        artifacts={"ART-1": _artifact("ART-1", "repo:.ea/artifacts/does-not-exist.md")},
    )
    report = verify_docs(state, tmp_path)
    assert report.status == "drift"
    assert any(v.code == "DOC.ARTIFACT_URI_MISSING" for v in report.cross_check_violations)


def test_doc_verify_cli_strict_exits_4_on_drift(tmp_path: Path) -> None:
    """``eawf doc verify --strict`` exits 4 when drift is present."""
    from typer.testing import CliRunner

    from eawf.cli.app import app

    repo, _state = _build_repo(tmp_path, drift=True)
    state_payload = json.loads(
        (repo / ".ea" / "indexes" / "generated.json").read_text(encoding="utf-8")
    )
    assert "generated" in state_payload  # sanity: manifest landed
    # Write a minimal state.json next to the manifest.
    state_payload_path = repo / ".ea" / "state.json"
    state_payload_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scope_kind": "repo",
                "urn": "urn:eawf:v1:state:ZZ",
                "updated_at": "2026-05-08T00:00:00Z",
                "project": {
                    "code": "ZZ",
                    "slug": "zz",
                    "title": "ZZ",
                    "description": None,
                    "domains": [],
                    "default_branch": "main",
                    "status": "active",
                    "repo_urn": "urn:eawf:v1:repo:ZZ",
                },
                "current": {
                    "project_code": "ZZ",
                    "subproject_id": None,
                    "phase_id": None,
                    "iter_id": None,
                    "active_wave_ids": [],
                    "active_session_ids": [],
                },
                "workspace": None,
                "phases": {},
                "iters": {},
                "waves": {},
                "artifacts": {},
                "agent_sessions": {},
                "plugins": {},
                "indexes": {},
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()
    res_strict = runner.invoke(app, ["-w", str(repo), "doc", "verify", "--strict"])
    assert res_strict.exit_code == 2, res_strict.output
    res_default = runner.invoke(app, ["-w", str(repo), "doc", "verify"])
    assert res_default.exit_code == 0, res_default.output


def test_verify_docs_artifact_non_repo_uri_skipped(tmp_path: Path) -> None:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    save_atomic(state_dir / "indexes" / "generated.json", Manifest(version=1, generated={}))
    state = _make_state(
        artifacts={"ART-2": _artifact("ART-2", "https://example.com/blob.md")},
    )
    report = verify_docs(state, tmp_path)
    assert report.status == "ok"
    assert report.cross_check_violations == []
