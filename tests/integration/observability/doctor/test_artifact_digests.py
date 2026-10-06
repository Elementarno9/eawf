"""``eawf doctor --fix`` gives each cited local artifact its digest by appending.

A fixture repository's artifact ledger cites three local artifacts by
``repo:`` path without a digest -- two whose files are present, one whose
file is not -- beside rows the repair must leave alone: one already
carrying a digest, one citing a source file, and one a later line already
superseded. The preview names one ``ledger.artifact-digests`` action over
the two present files and reports the absent one; the apply, served by the
registered daemon method in process, only appends; and a rerun previews
nothing while still reporting the absent file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.origins import source_digest
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerTornTailError,
    append_ledger_record,
    effective_records,
    line_digest,
    read_ledger_records,
    render_ledger_line,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.doctor.artifact_digests import (
    ARTIFACT_DIGEST_ACTION,
    ArtifactDigest,
    ArtifactDigestPlan,
    artifact_digest_preview,
    digested_record,
    plan_artifact_digests,
    selected_artifact_ledger,
)
from eawf.observability.doctor.checks import CheckResult
from eawf.observability.doctor.repair import build_repair_plan
from eawf.runtime.daemon import methods
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context

runner = CliRunner()

AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

PRESENT = (".ea/artifacts/audits/2026-06-01-a.md", ".ea/artifacts/research/2026-06-02-b.md")
ABSENT = ".ea/artifacts/audits/2026-06-03-gone.md"


@pytest.fixture(autouse=True)
def isolated_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep registry, canary scratch and host probes inside this test."""
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


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", f"core.hooksPath={root / '.no-hooks'}", *args],
        cwd=root,
        capture_output=True,
        check=True,
    )


def _imported(key: str, uri: str, *, sha256: str | None = None) -> LedgerRecord:
    """Return an imported legacy artifact line, shaped as the cutover writes it."""
    inner: dict[str, Any] = {
        "id": key,
        "kind": "audit_report",
        "uri": uri,
        "sha256": sha256,
        "size_bytes": None,
        "created_at": "2026-06-01T00:00:00Z",
        "metadata": {},
        "urn": f"urn:eawf:v1:artifact:P01/{key}",
    }
    digest = source_digest(inner)
    return LedgerRecord(
        collection=Epoch2Collection.ARTIFACT,
        record_key=f"legacy:artifacts/{key}",
        status="imported",
        recorded_at=AT,
        payload={
            "ledger": "artifact",
            "source_id": key,
            "origin": {"kind": "legacy", "source_digest": digest},
            "payload": inner,
            "payload_digest": digest,
            "store_only": False,
        },
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Build a managed repository whose artifact ledger cites local files."""
    built = tmp_path / "repo"
    init = ["--no-input", "init", "--target", str(built), "--project-code", "DEMO"]
    assert runner.invoke(app, init).exit_code == exit_codes.OK
    authority = require_native_authority(built / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(document_path)
    document.setdefault("project", {})["DEMO"] = {"payload": {"code": "DEMO"}}
    write_document(document_path, document)
    if not (built / ".git").exists():
        _git(built, "init", "-q", "-b", "main")
    for index, path in enumerate(PRESENT):
        (built / path).parent.mkdir(parents=True, exist_ok=True)
        (built / path).write_text(f"# artifact {index}\n", encoding="utf-8")
    ledger = selected_artifact_ledger(built)
    assert ledger is not None
    for index, path in enumerate(PRESENT):
        append_ledger_record(ledger, _imported(f"A-{index}", f"repo:{path}"))
    append_ledger_record(ledger, _imported("A-GONE", f"repo:{ABSENT}"))
    append_ledger_record(ledger, _imported("A-HELD", f"repo:{PRESENT[0]}", sha256="a" * 64))
    append_ledger_record(ledger, _imported("A-SRC", "repo:src/demo.py"))
    stale = _imported("A-OLD", f"repo:{PRESENT[1]}")
    append_ledger_record(ledger, stale)
    corrected = _imported("A-OLD", f"repo:{PRESENT[1]}", sha256="b" * 64)
    append_ledger_record(
        ledger,
        corrected.model_copy(update={"supersedes": line_digest(render_ledger_line(stale))}),
    )
    return built


def _ledger(root: Path) -> Path:
    ledger = selected_artifact_ledger(root)
    assert ledger is not None
    return ledger


def _digest_actions(answer: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in answer.get("actions", []) if a["action_id"] == ARTIFACT_DIGEST_ACTION]


def _doctor(root: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["--workspace", str(root), *argv, "doctor", "--fix"])
    payload = json.loads(result.stdout) if result.stdout.strip().startswith("{") else {}
    return result.exit_code, payload.get("result", payload)


class _InProcessClient:
    """The CLI's daemon client, answering from the registered methods in process."""

    context: methods.MethodContext | None = None

    def __init__(self, *_a: object, **_k: object) -> None:
        return None

    def __enter__(self) -> _InProcessClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any], **_k: object) -> dict[str, Any]:
        context = type(self).context
        assert context is not None
        answer = asyncio.run(methods.dispatch(method, context, params))
        assert isinstance(answer, dict)
        return answer


def test_plan_covers_each_effective_citing_row_and_reports_the_absent_file(root: Path) -> None:
    plan = plan_artifact_digests(root, _ledger(root))

    assert [row.path for row in plan.rows] == list(PRESENT)
    assert plan.missing == (ABSENT,)
    for row in plan.rows:
        content = (root / row.path).read_bytes()
        assert row.sha256 == hashlib.sha256(content).hexdigest()
        assert row.size_bytes == len(content)


def test_fix_previews_applies_by_appending_and_reruns_clean(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(root)
    before = ledger.read_bytes()

    code, preview = _doctor(root, "--json")

    assert code == exit_codes.OK, preview
    (action,) = _digest_actions(preview)
    assert action["record_count"] == len(PRESENT)
    assert action["mutation_class"] == "committed_store"
    assert "ledger.artifact_file_absent=1" in preview["unresolved_findings"]
    assert ledger.read_bytes() == before

    monkeypatch.setattr("eawf.surfaces.doctor_repair.DaemonClient", _InProcessClient)
    _InProcessClient.context = method_context(tmp_path / "runtime")
    methods.ensure_all_methods_registered()
    argv = ["--workspace", str(root), "--no-input", "doctor", "--fix", "--yes"]
    result = runner.invoke(app, argv)
    assert result.exit_code == exit_codes.OK, result.stdout

    after = ledger.read_bytes()
    assert after.startswith(before)
    assert after[len(before) :].count(b"\n") == len(PRESENT)
    rows = {r.record_key: r for r in effective_records(read_ledger_records(ledger))}
    for index, path in enumerate(PRESENT):
        inner = rows[f"legacy:artifacts/A-{index}"].payload["payload"]
        content = (root / path).read_bytes()
        assert inner["sha256"] == hashlib.sha256(content).hexdigest()
        assert inner["size_bytes"] == len(content)
    assert rows["legacy:artifacts/A-GONE"].payload["payload"]["sha256"] is None

    code, rerun = _doctor(root, "--json")
    assert code == exit_codes.OK, rerun
    assert _digest_actions(rerun) == []
    assert "ledger.artifact_file_absent=1" in rerun["unresolved_findings"]
    assert plan_artifact_digests(root, ledger).rows == ()


def test_apply_refuses_when_a_cited_file_changed_since_the_preview(
    root: Path, tmp_path: Path
) -> None:
    from eawf.runtime.daemon.methods.doctor import _apply_artifact_digests

    (action,) = [
        a for a in build_repair_plan(root).actions if a.action_id == ARTIFACT_DIGEST_ACTION
    ]
    (root / PRESENT[0]).write_text("# edited after the preview\n", encoding="utf-8")
    before = _ledger(root).read_bytes()

    with pytest.raises(methods.DaemonValidationError, match="preview changed"):
        _apply_artifact_digests(method_context(tmp_path / "rt"), root, action)

    assert _ledger(root).read_bytes() == before


def test_digested_record_keeps_the_row_and_restates_its_payload_digest(root: Path) -> None:
    ledger = _ledger(root)
    (row, _second) = plan_artifact_digests(root, ledger).rows
    original = read_ledger_records(ledger)[row.line - 1]

    record = digested_record(ledger, row, recorded_at=AT)

    assert record.supersedes == row.supersedes
    assert (record.collection, record.record_key, record.status) == (
        original.collection,
        original.record_key,
        original.status,
    )
    inner = record.payload["payload"]
    assert (inner["sha256"], inner["size_bytes"]) == (row.sha256, row.size_bytes)
    assert record.payload["payload_digest"] == source_digest(inner)
    assert record.payload["origin"] == original.payload["origin"]
    assert original.payload["payload"]["sha256"] is None


def test_a_missing_ledger_plans_nothing(tmp_path: Path) -> None:
    assert plan_artifact_digests(tmp_path, tmp_path / "artifact.jsonl") == ArtifactDigestPlan()


def test_an_empty_ledger_plans_nothing(tmp_path: Path) -> None:
    ledger = tmp_path / "artifact.jsonl"
    ledger.write_bytes(b"")

    assert plan_artifact_digests(tmp_path, ledger) == ArtifactDigestPlan()


def test_a_single_citing_row_plans_one(tmp_path: Path) -> None:
    (tmp_path / PRESENT[0]).parent.mkdir(parents=True)
    (tmp_path / PRESENT[0]).write_bytes(b"")
    ledger = tmp_path / "artifact.jsonl"
    append_ledger_record(ledger, _imported("A-0", f"repo:{PRESENT[0]}"))

    (row,) = plan_artifact_digests(tmp_path, ledger).rows

    assert (row.line, row.size_bytes) == (1, 0)
    assert row.sha256 == hashlib.sha256(b"").hexdigest()


def test_a_torn_ledger_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "artifact.jsonl"
    ledger.write_bytes(render_ledger_line(_imported("A-0", "repo:x")).encode())

    with pytest.raises(LedgerTornTailError):
        plan_artifact_digests(tmp_path, ledger)


def test_a_line_that_is_not_a_record_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "artifact.jsonl"
    ledger.write_bytes(b'{"not": "a record"}\n')

    with pytest.raises(ValidationError):
        plan_artifact_digests(tmp_path, ledger)


def test_an_epoch_one_tree_has_no_artifact_ledger(tmp_path: Path) -> None:
    assert selected_artifact_ledger(tmp_path) is None


def test_the_preview_moves_with_the_ledger(root: Path) -> None:
    ledger = _ledger(root)
    plan = plan_artifact_digests(root, ledger)
    first = artifact_digest_preview(ledger, plan)
    append_ledger_record(ledger, _imported("A-SRC2", "repo:src/other.py"))

    assert artifact_digest_preview(ledger, plan) != first


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("line", 0),
        ("supersedes", "sha256:short"),
        ("path", ""),
        ("sha256", "A" * 64),
        ("size_bytes", -1),
        ("extra", 1),
    ],
)
def test_a_malformed_planned_row_is_refused(field: str, value: object) -> None:
    fields: dict[str, object] = {
        "line": 1,
        "supersedes": "sha256:" + "0" * 64,
        "path": PRESENT[0],
        "sha256": "0" * 64,
        "size_bytes": 0,
    }
    fields[field] = value

    with pytest.raises(ValidationError):
        ArtifactDigest.model_validate(fields)
