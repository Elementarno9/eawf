"""Tests for :mod:`eawf.observability.doctor.doc_verify`.

Builds synthetic manifests, managed regions and artifact ledgers in
``tmp_path`` to exercise the drift + artifact cross-check pass without
depending on the parent repo's tree.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.store.ledger import LedgerRecord, LedgerTornTailError, append_ledger_record
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.doctor.doc_verify import read_artifact_uris, verify_docs
from eawf.surfaces.cli.app import app
from eawf.surfaces.render.manifest import Manifest, ManifestEntry, save_atomic
from eawf.surfaces.render.regions import compute_hash
from tests._epoch2_helpers import lay_epoch2_tree


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


def _build_repo(tmp_path: Path, *, drift: bool) -> Path:
    """Build a repo tree with one managed region and its manifest."""
    repo = tmp_path
    state_dir = repo / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    target = repo / "AGENTS.md"
    on_disk_hash = _write_region(target, "rules", "Hello world.\nSecond line.")
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
    return repo


def _empty_manifest(tmp_path: Path) -> None:
    save_atomic(tmp_path / ".ea" / "indexes" / "generated.json", Manifest(version=1, generated={}))


def _write_audit_store(tmp_path: Path) -> None:
    store_dir = tmp_path / ".ea" / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "audit.jsonl").write_text(
        '{"schema_version":"1.0","id":"A01","kind":"audit"}\n', encoding="utf-8"
    )


def _artifact_line(key: str, payload: dict[str, Any], *, status: str = "imported") -> LedgerRecord:
    return LedgerRecord(
        collection=Epoch2Collection.ARTIFACT,
        record_key=key,
        status=status,
        recorded_at=datetime(2026, 5, 8, tzinfo=UTC),
        supersedes=None,
        payload=payload,
    )


# ---- verify_docs -------------------------------------------------------------


def test_verify_docs_clean_repo_status_ok(tmp_path: Path) -> None:
    report = verify_docs({}, _build_repo(tmp_path, drift=False))
    assert report.status == "ok"
    assert report.has_drift is False
    assert report.cross_check_violations == []
    assert report.manifest_targets == 1
    assert report.manifest_entries == 1


def test_verify_docs_drift_detected_status_drift(tmp_path: Path) -> None:
    report = verify_docs({}, _build_repo(tmp_path, drift=True))
    assert report.status == "drift"
    assert report.has_drift is True
    assert any(r.kind == "hand-edited" for r in report.drift_reports)


def test_verify_docs_cross_check_artifact_repo_uri_unresolved(tmp_path: Path) -> None:
    _empty_manifest(tmp_path)
    report = verify_docs({"ART-1": "repo:.ea/artifacts/does-not-exist.md"}, tmp_path)
    assert report.status == "drift"
    assert [v.code for v in report.cross_check_violations] == ["DOC.ARTIFACT_URI_MISSING"]
    assert report.cross_check_violations[0].target == "/artifacts/ART-1"


def test_verify_docs_cross_check_artifact_jsonl_fragment_resolved(tmp_path: Path) -> None:
    _empty_manifest(tmp_path)
    _write_audit_store(tmp_path)
    report = verify_docs({"ART-1": "repo:.ea/store/audit.jsonl#A01"}, tmp_path)
    assert report.status == "ok"
    assert report.cross_check_violations == []


def test_verify_docs_cross_check_artifact_jsonl_fragment_missing(tmp_path: Path) -> None:
    _empty_manifest(tmp_path)
    _write_audit_store(tmp_path)
    report = verify_docs({"ART-1": "repo:.ea/store/audit.jsonl#A02"}, tmp_path)
    assert report.status == "drift"
    assert [v.code for v in report.cross_check_violations] == ["DOC.ARTIFACT_URI_FRAGMENT_MISSING"]


def test_verify_docs_missing_manifest_is_empty_gate_input(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir(parents=True, exist_ok=True)
    report = verify_docs({}, tmp_path)
    assert report.status == "ok"
    assert report.manifest_entries == 0
    assert report.manifest_targets == 0


def test_verify_docs_artifact_non_repo_uri_skipped(tmp_path: Path) -> None:
    _empty_manifest(tmp_path)
    report = verify_docs({"ART-2": "https://example.com/blob.md"}, tmp_path)
    assert report.status == "ok"
    assert report.cross_check_violations == []


# ---- read_artifact_uris ------------------------------------------------------


def test_read_artifact_uris_absent_ledger_is_empty(tmp_path: Path) -> None:
    assert read_artifact_uris(tmp_path / "ledger" / "artifact.jsonl") == {}


def test_read_artifact_uris_reads_imported_and_native_lines(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "artifact.jsonl"
    append_ledger_record(ledger, _artifact_line("ART-1", {"payload": {"uri": "repo:a.md"}}))
    append_ledger_record(ledger, _artifact_line("ART-2", {"uri": "repo:b.md"}, status="recorded"))
    append_ledger_record(ledger, _artifact_line("ART-3", {"payload": {"content": "inline"}}))
    assert read_artifact_uris(ledger) == {"ART-1": "repo:a.md", "ART-2": "repo:b.md"}


def test_read_artifact_uris_rejects_a_torn_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "artifact.jsonl"
    append_ledger_record(ledger, _artifact_line("ART-1", {"payload": {"uri": "repo:a.md"}}))
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write('{"collection": "artifact"')
    with pytest.raises(LedgerTornTailError):
        read_artifact_uris(ledger)


# ---- the CLI -------------------------------------------------------------------


def test_doc_verify_cli_strict_exits_2_on_drift(tmp_path: Path) -> None:
    """``eawf doc verify --strict`` exits 2 when drift is present; the default exits 0."""
    repo = _build_repo(tmp_path, drift=True)
    lay_epoch2_tree(repo)
    runner = CliRunner()
    res_strict = runner.invoke(app, ["-w", str(repo), "doc", "verify", "--strict"])
    assert res_strict.exit_code == 2, res_strict.output
    res_default = runner.invoke(app, ["-w", str(repo), "doc", "verify"])
    assert res_default.exit_code == 0, res_default.output


def test_doc_verify_cli_refuses_an_epoch1_tree(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path, drift=False)
    (repo / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(app, ["-w", str(repo), "doc", "verify"])
    assert result.exit_code == 4, result.output
    assert "holds no epoch-2 generation" in result.output
