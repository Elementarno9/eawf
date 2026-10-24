"""``eawf validate`` checks a generation document against the native models.

A tree born by ``eawf init`` keeps its records in a generation document,
not in the epoch-1 ``.ea/state.json``. Validating that document against the
epoch-1 State model refuses every row as an unknown key, so the command
must recognise the document and check each row against its own model and
the records it names.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME
from eawf.kernel.migration.epoch2.cutover import StagedRecord, document_row
from eawf.kernel.migration.epoch2.native_records import NativeRecordCollection, map_native_record
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()

SEED_RECORDS = Path(__file__).resolve().parents[3] / "fixtures/epoch2/transitions/seed_records.yaml"


def _born_generation(root: Path) -> Path:
    """Run ``eawf init`` under *root* and return its generation document."""
    result = runner.invoke(
        app, ["--json", "--no-input", "init", "--target", str(root), "--project-code", "DEMO"]
    )
    assert result.exit_code == exit_codes.OK, result.output
    tree = root / ".ea"
    generation_id = resolve_authority(tree).generation_id
    assert generation_id is not None
    return tree / GENERATIONS_DIRNAME / generation_id / "state.json"


def _seed(entity: str, status: str) -> dict[str, Any]:
    """Return a deep copy of one seeded native record."""
    records = yaml.safe_load(SEED_RECORDS.read_text(encoding="utf-8"))["records"]
    return copy.deepcopy(records[entity][status])


def _rekeyed(row: dict[str, Any], key: str) -> dict[str, Any]:
    """Return *row* under *key*, its URN re-spelled to agree."""
    row["key"] = key
    row["urn"] = f"{str(row['urn']).rsplit('/', 1)[0]}/{key}"
    return row


def _place(path: Path, rows: dict[str, dict[str, dict[str, Any]]]) -> None:
    """Add *rows*, keyed by collection then key, to the document at *path*."""
    document = read_document(path)
    for collection, keyed in rows.items():
        document.setdefault(collection, {}).update(keyed)
    write_document(path, document)


def _validate(path: Path) -> tuple[int, dict[str, Any], str]:
    """Run ``eawf --json validate`` on *path*; return exit code, report and raw output."""
    result = runner.invoke(app, ["--json", "validate", str(path)])
    return result.exit_code, json.loads(result.output.strip().splitlines()[-1]), result.output


def _chain() -> dict[str, dict[str, dict[str, Any]]]:
    """Return a Milestone, its Batch and a Task placed in that Batch."""
    milestone = _seed("milestone", "PLANNED")
    batch = _seed("batch", "PLANNED")
    task = _rekeyed(_seed("task", "PLANNED"), "EAWF-0001")
    assert batch["milestone_ref"] == milestone["urn"]
    assert task["batch_ref"] == batch["urn"]
    return {
        "milestone": {milestone["key"]: milestone},
        "batch": {batch["key"]: batch},
        "task": {task["key"]: task},
    }


def test_validate_accepts_a_native_generation(tmp_path: Path) -> None:
    path = _born_generation(tmp_path)

    code, report, output = _validate(path)
    assert code == exit_codes.OK, output
    assert report["ok"] is True
    assert report["mode"] == "epoch2"
    assert "Extra inputs not permitted" not in output

    _place(path, _chain())
    code, report, output = _validate(path)
    assert code == exit_codes.OK, output
    assert report == {
        "ok": True,
        "mode": "epoch2",
        "schema_errors": [],
        "violations": [],
        "warnings": [],
    }


def test_validate_names_the_invalid_native_row(tmp_path: Path) -> None:
    path = _born_generation(tmp_path)
    chain = _chain()
    chain["task"]["EAWF-0001"]["status"] = "NOT_A_STATUS"
    _place(path, chain)

    code, report, output = _validate(path)
    assert code == exit_codes.VALIDATION_ERROR, output
    assert report["ok"] is False
    assert report["mode"] == "epoch2"
    assert [v["path"] for v in report["violations"]] == ["task/EAWF-0001"]
    assert report["violations"][0]["code"] == "EPOCH2.ROW.INVALID"
    assert "status" in report["violations"][0]["message"]
    assert "NOT_A_STATUS" not in output

    document = read_document(path)
    document["task"]["EAWF-0001"]["status"] = "PLANNED"
    del document["milestone"]
    write_document(path, document)

    code, report, output = _validate(path)
    assert code == exit_codes.VALIDATION_ERROR, output
    assert [(v["code"], v["path"]) for v in report["violations"]] == [
        ("EPOCH2.REF.MILESTONE_MISSING", "batch/BAT-0007")
    ]
    assert "MLS-0030" in report["violations"][0]["message"]

    code_human = runner.invoke(app, ["validate", str(path)])
    assert code_human.exit_code == exit_codes.VALIDATION_ERROR
    assert "EPOCH2.REF.MILESTONE_MISSING at batch/BAT-0007" in code_human.output


def test_validate_refuses_a_document_that_is_not_a_generation_shape(tmp_path: Path) -> None:
    path = _born_generation(tmp_path)
    document = read_document(path)
    document["not_a_collection"] = {}
    document["track"] = []
    write_document(path, document)

    code, report, output = _validate(path)
    assert code == exit_codes.VALIDATION_ERROR, output
    assert report["schema_errors"] == [
        "not_a_collection: not an epoch-2 collection",
        "track: holds a list, not an object",
    ]
    assert report["violations"] == []


def test_validate_names_a_task_whose_batch_is_absent(tmp_path: Path) -> None:
    path = _born_generation(tmp_path)
    chain = _chain()
    del chain["batch"]
    _place(path, chain)

    code, report, output = _validate(path)
    assert code == exit_codes.VALIDATION_ERROR, output
    assert [(v["code"], v["path"]) for v in report["violations"]] == [
        ("EPOCH2.REF.BATCH_MISSING", "task/EAWF-0001")
    ]


def _imported_policy(key: str) -> dict[str, Any]:
    """Return the document row the cutover writes for one imported sandbox policy."""
    record = map_native_record(
        collection=NativeRecordCollection.SANDBOX_POLICIES,
        record_key=key,
        row={"id": key, "mode": "read-only"},
        project_code="DEMO",
        source_schema_version="1.20",
    )
    staged = StagedRecord(
        collection=Epoch2Collection.SANDBOX_POLICY,
        record_key=key,
        status="imported",
        payload=record.model_dump(mode="json"),
    )
    return document_row(staged, recorded_at=datetime(2026, 9, 8, tzinfo=UTC))


def test_validate_accepts_an_imported_row_and_refuses_a_payload_that_is_not_one(
    tmp_path: Path,
) -> None:
    path = _born_generation(tmp_path)
    _place(path, {"sandbox_policy": {"POL-1": _imported_policy("POL-1")}})
    code, report, output = _validate(path)
    assert code == exit_codes.OK, output

    _place(
        path,
        {
            "task": {"EAWF-0001": {"payload": 1, "garbage": True}},
            "sandbox_policy": {
                "POL-2": {
                    "status": "imported",
                    "recorded_at": "2026-09-08T00:00:00Z",
                    "payload": {},
                }
            },
        },
    )
    code, report, output = _validate(path)
    assert code == exit_codes.VALIDATION_ERROR, output
    assert [(v["code"], v["path"]) for v in report["violations"]] == [
        ("EPOCH2.ROW.INVALID", "sandbox_policy/POL-2"),
        ("EPOCH2.ROW.INVALID", "task/EAWF-0001"),
    ]


def test_validate_names_a_row_filed_under_another_key(tmp_path: Path) -> None:
    path = _born_generation(tmp_path)
    chain = _chain()
    chain["task"] = {"EAWF-9999": chain["task"]["EAWF-0001"]}
    chain["sandbox_policy"] = {"POL-9": _imported_policy("POL-1")}
    _place(path, chain)

    code, report, output = _validate(path)
    assert code == exit_codes.VALIDATION_ERROR, output
    assert [(v["code"], v["path"]) for v in report["violations"]] == [
        ("EPOCH2.ROW.KEY_MISMATCH", "sandbox_policy/POL-9"),
        ("EPOCH2.ROW.KEY_MISMATCH", "task/EAWF-9999"),
    ]
    assert "EAWF-0001" in report["violations"][1]["message"]


@pytest.mark.parametrize("collection", ["run", "project", "release"])
def test_validate_checks_every_modelled_collection(tmp_path: Path, collection: str) -> None:
    path = _born_generation(tmp_path)
    _place(path, {collection: {"GARBAGE-1": {"garbage": True}}})

    code, report, output = _validate(path)
    assert code == exit_codes.VALIDATION_ERROR, output
    assert [(v["code"], v["path"]) for v in report["violations"]] == [
        ("EPOCH2.ROW.INVALID", f"{collection}/GARBAGE-1")
    ]


def test_validate_warns_when_a_copied_document_has_no_ledger(tmp_path: Path) -> None:
    path = _born_generation(tmp_path / "tree")
    chain = _chain()
    del chain["milestone"]
    _place(path, chain)
    copied = tmp_path / "copy" / "state.json"
    copied.parent.mkdir()
    copied.write_bytes(path.read_bytes())

    code, report, output = _validate(copied)
    assert code == exit_codes.OK, output
    assert report["violations"] == []
    assert [(w["code"], w["path"]) for w in report["warnings"]] == [
        ("EPOCH2.REF.MILESTONE_MISSING", "batch/BAT-0007")
    ]
    assert "MLS-0030" in report["warnings"][0]["message"]

    code, report, output = _validate(path)
    assert code == exit_codes.VALIDATION_ERROR, output
    assert [(v["code"], v["path"]) for v in report["violations"]] == [
        ("EPOCH2.REF.MILESTONE_MISSING", "batch/BAT-0007")
    ]


def test_the_lifecycle_payload_model_tracks_the_import_model() -> None:
    from eawf.kernel.migration.epoch2.lifecycle import ImportedLifecycleRecord
    from eawf.kernel.validate.epoch2 import ImportedLifecyclePayload

    assert (
        ImportedLifecyclePayload.model_fields.keys() == ImportedLifecycleRecord.model_fields.keys()
    )
