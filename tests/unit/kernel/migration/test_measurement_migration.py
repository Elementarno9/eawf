"""The measurement collections migrate whole, verbatim and re-pointed.

The real-shape fixture is a slice of this repository's own epoch-1
estimate and actual rows: float costs that are exactly zero, token
tallies of zero, null effort and attribution fields, a calibration
exclusion with no recorded reason, and one actual whose wave no longer
exists. Those are the shapes a coercing importer gets wrong -- a null
read as a zero, a zero cost read as a price, an orphan dropped or pinned
to a guessed subject -- so they are what the migration is proven over.
"""

from __future__ import annotations

import copy
import io
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.errors import (
    MigrationCountMismatchError,
    MigrationFabricationDetectedError,
)
from eawf.kernel.migration.epoch2.measurements import (
    MEASUREMENT_COLLECTIONS,
    QUALITY_MARKER_FIELDS,
    MeasurementImportPlan,
    MeasurementKind,
)
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan
from eawf.kernel.migration.epoch2.snapshot import DOCUMENT_LOCATOR, SourceSnapshot
from eawf.runtime.runtime_counter_sidecar import RuntimeCounterSidecar
from eawf.runtime.runtimes.claude import statusline
from tests.unit.kernel.migration.conftest import (
    ALLOWLIST_PATH,
    EPOCH1_FULL_SNAPSHOT,
    MIGRATION_FIXTURES,
)

REAL_SHAPE_PATH = MIGRATION_FIXTURES / "measurements" / "real-shape.json"
SOURCE_SCHEMA_VERSION = "1.20"

#: The actual whose wave was pruned from the source before migration.
UNRESOLVED_SUBJECT = "P01-I01-W01"
#: A real actual excluded from calibration, recorded with no reason.
EXCLUDED_SUBJECT = "P30-I21-W46"
#: A real priced actual.
PRICED_SUBJECT = "P30-I23-W28"
#: A real actual that is null across effort and attribution and zero in cost.
UNPRICED_SUBJECT = "P28-I02-W07"

#: A statusline session cache entry and its counter sidecar, as the
#: statusline writes them. Neither is a canonical fact.
STATUSLINE_CACHE_NAME = "session-1.json"
COUNTER_SIDECAR_NAME = "session-1.runtime-counters.json"


def _real_shape() -> tuple[dict[str, Any], frozenset[str]]:
    """Return the real-shape document and the Task ids it imports under."""
    fixture = json.loads(REAL_SHAPE_PATH.read_text(encoding="utf-8"))
    return fixture["document"], frozenset(fixture["task_ids"])


def _build(document: dict[str, Any], task_ids: frozenset[str]) -> MeasurementImportPlan:
    """Re-point ``document`` against ``task_ids``."""
    return MeasurementImportPlan.build(
        document=document, task_ids=task_ids, source_schema_version=SOURCE_SCHEMA_VERSION
    )


def _copy_snapshot(tmp_path: Path) -> Path:
    """Copy the full-shape snapshot somewhere a test may edit it."""
    root = tmp_path / "snapshot"
    shutil.copytree(EPOCH1_FULL_SNAPSHOT, root)
    return root


def _edit_document(root: Path, edit: dict[str, Any]) -> None:
    """Overwrite top-level keys of the snapshot document at ``root``."""
    path = root / DOCUMENT_LOCATOR
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(edit)
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def test_meas_012_every_real_actuals_row_resolves_or_stays_a_legacy_record() -> None:
    document, task_ids = _real_shape()

    plan = _build(document, task_ids)
    actuals = plan.for_kind(MeasurementKind.ACTUAL)

    assert {row.map_key for row in actuals} == set(document["actuals"])
    for row in actuals:
        if row.map_key == UNRESOLVED_SUBJECT:
            assert row.is_immutable_legacy_record
        else:
            assert row.task_ref == row.map_key
            assert row.task_ref in task_ids
    assert plan.legacy_keys == (UNRESOLVED_SUBJECT,)


def test_meas_012_an_unresolved_actual_is_never_reattributed() -> None:
    """The row names its own subject; the import neither guesses nor rewrites it."""
    document, task_ids = _real_shape()

    row = _build(document, task_ids).measurement_for(
        kind=MeasurementKind.ACTUAL, map_key=UNRESOLVED_SUBJECT
    )

    assert row.task_ref is None
    assert row.origin.confidence == "supported"
    source = document["actuals"][UNRESOLVED_SUBJECT]
    assert row.payload == {**source, "actual_cost_usd": None, "actual_tokens": None}


def test_meas_012_corpus_import_refuses_to_drop_an_unreadable_actuals_row(
    tmp_path: Path,
) -> None:
    """A row the re-point cannot read is a refusal, never a silent skip."""
    root = _copy_snapshot(tmp_path)
    source = SourceSnapshot.read(EPOCH1_FULL_SNAPSHOT).document
    _edit_document(root, {"actuals": {**source["actuals"], "P01-I01-W01": "not a row"}})

    with pytest.raises(MigrationCountMismatchError, match=r"actuals holds 5 rows .* 4"):
        CorpusImportPlan.build(snapshot=SourceSnapshot.read(root), allowlist_path=ALLOWLIST_PATH)


def test_meas_035_repoint_is_total_over_every_subject_shape() -> None:
    """A wave, a backlog row, an iter, a phase and a pruned wave all map."""
    subjects = ("P30-I23-W28", "B003", "P01-I02", "P01", UNRESOLVED_SUBJECT)
    row = {"id": "ACT-X", "scope_id": "X", "status": "done", "updated_at": "2026-01-01T00:00:00Z"}
    document = {"actuals": {subject: {**row, "scope_id": subject} for subject in subjects}}
    task_ids = frozenset({"P30-I23-W28", "B003"})

    plan = _build(document, task_ids)

    assert len(plan.measurements) == len(subjects)
    for measurement in plan.measurements:
        expected = measurement.map_key if measurement.map_key in task_ids else None
        assert measurement.task_ref == expected


def test_meas_035_repoint_is_pure_and_reruns_identically() -> None:
    document, task_ids = _real_shape()
    untouched = copy.deepcopy(document)
    reordered = {
        name: dict(reversed(list(collection.items()))) for name, collection in document.items()
    }

    first = _build(document, task_ids)
    second = _build(document, task_ids)

    assert first == second
    assert _build(reordered, task_ids) == first
    assert document == untouched


def test_meas_036_quality_and_exclusion_markers_import_verbatim() -> None:
    document, task_ids = _real_shape()

    plan = _build(document, task_ids)

    for row in plan.measurements:
        source = document[row.source_collection][row.map_key]
        assert row.quality_marker == source[QUALITY_MARKER_FIELDS[row.kind]]
        if row.kind is MeasurementKind.ACTUAL:
            assert row.calibration_excluded is source["calibration_excluded"]
    excluded = plan.measurement_for(kind=MeasurementKind.ACTUAL, map_key=EXCLUDED_SUBJECT)
    assert excluded.calibration_excluded is True


def test_meas_036_an_exclusion_reason_is_carried_verbatim_and_never_invented() -> None:
    """Epoch 1 recorded no reason; the import must not supply one."""
    document, task_ids = _real_shape()
    reasoned = copy.deepcopy(document)
    reasoned["actuals"][EXCLUDED_SUBJECT]["exclusion_reason"] = "runtime shared across waves"

    bare = _build(document, task_ids).measurement_for(
        kind=MeasurementKind.ACTUAL, map_key=EXCLUDED_SUBJECT
    )
    carried = _build(reasoned, task_ids).measurement_for(
        kind=MeasurementKind.ACTUAL, map_key=EXCLUDED_SUBJECT
    )

    assert "exclusion_reason" not in bare.payload
    assert carried.payload["exclusion_reason"] == "runtime shared across waves"


def test_meas_036_a_null_stays_null_and_an_unpriced_row_gains_no_price() -> None:
    """The record as the cutover writes it, not only as the plan holds it."""
    document, task_ids = _real_shape()
    plan = _build(document, task_ids)

    unpriced = plan.measurement_for(kind=MeasurementKind.ACTUAL, map_key=UNPRICED_SUBJECT)
    priced = plan.measurement_for(kind=MeasurementKind.ACTUAL, map_key=PRICED_SUBJECT)
    written = unpriced.model_dump(mode="json")["payload"]

    for field in ("agent_runtime_eu", "attention_eu", "harness", "model"):
        assert written[field] is None
    assert written["actual_cost_usd"] is None
    assert written["actual_tokens"] is None
    assert unpriced.nulled_fields == ("actual_cost_usd", "actual_tokens")
    assert "price_source" not in written
    assert priced.model_dump(mode="json")["payload"]["actual_cost_usd"] == pytest.approx(
        document["actuals"][PRICED_SUBJECT]["actual_cost_usd"]
    )


def test_meas_041_an_uncaptured_default_zero_imports_as_null() -> None:
    """Every real-shape actual that names no harness or model was never priced."""
    document, task_ids = _real_shape()
    plan = _build(document, task_ids)

    for row in plan.for_kind(MeasurementKind.ACTUAL):
        source = document["actuals"][row.map_key]
        captured = source["harness"] is not None or source["model"] is not None
        for field in ("actual_cost_usd", "actual_tokens"):
            if captured or source[field] != 0:
                assert row.payload[field] == source[field]
            else:
                assert row.payload[field] is None
    assert (
        plan.measurement_for(kind=MeasurementKind.ACTUAL, map_key=UNPRICED_SUBJECT).payload[
            "actual_cost_usd"
        ]
        is None
    )


def test_meas_041_a_captured_zero_stays_a_measured_zero() -> None:
    """A row that names its harness and model was captured; its zero is a reading."""
    document, task_ids = _real_shape()

    excluded = _build(document, task_ids).measurement_for(
        kind=MeasurementKind.ACTUAL, map_key=EXCLUDED_SUBJECT
    )

    assert excluded.payload["actual_cost_usd"] == pytest.approx(0.0)
    assert excluded.payload["actual_tokens"] == 0
    assert excluded.nulled_fields == ()


def test_meas_041_require_reconciled_refuses_a_zero_restored_on_an_unpriced_row() -> None:
    """Writing the default zero back is a fabricated measurement, not a round-trip."""
    document, task_ids = _real_shape()
    plan = _build(document, task_ids)
    target = plan.measurement_for(kind=MeasurementKind.ACTUAL, map_key=UNPRICED_SUBJECT)
    zeroed = target.model_copy(update={"payload": {**target.payload, "actual_cost_usd": 0.0}})
    tampered = plan.model_copy(
        update={
            "measurements": tuple(zeroed if row is target else row for row in plan.measurements)
        }
    )

    with pytest.raises(MigrationFabricationDetectedError, match=UNPRICED_SUBJECT):
        tampered.require_reconciled(document)


def test_meas_036_require_reconciled_refuses_a_rewritten_row() -> None:
    """A null cost turned into a zero is a fabricated measurement."""
    document, task_ids = _real_shape()
    plan = _build(document, task_ids)
    target = plan.measurement_for(kind=MeasurementKind.ACTUAL, map_key=UNPRICED_SUBJECT)
    rewritten = target.model_copy(update={"payload": {**target.payload, "attention_eu": 0.0}})
    tampered = plan.model_copy(
        update={
            "measurements": tuple(rewritten if row is target else row for row in plan.measurements)
        }
    )

    with pytest.raises(MigrationFabricationDetectedError, match=UNPRICED_SUBJECT):
        tampered.require_reconciled(document)


def test_meas_037_the_read_barrier_never_pins_a_statusline_cache_or_sidecar(
    tmp_path: Path,
) -> None:
    """Derived projections beside the corpus are not part of it."""
    root = _copy_snapshot(tmp_path)
    for directory in (root, root / "store", root / "config"):
        (directory / STATUSLINE_CACHE_NAME).write_text(
            json.dumps({"session_id": "session-1", "line": "cached"}), encoding="utf-8"
        )
        (directory / COUNTER_SIDECAR_NAME).write_text(
            json.dumps({"schema_version": "1.0", "counters": {"cost_usd": "0.42"}}),
            encoding="utf-8",
        )

    snapshot = SourceSnapshot.read(root)

    assert snapshot.identity == SourceSnapshot.read(EPOCH1_FULL_SNAPSHOT).identity
    for surface in snapshot.identity.surfaces:
        assert not surface.locator.endswith((STATUSLINE_CACHE_NAME, COUNTER_SIDECAR_NAME))


def test_meas_037_the_first_render_rebuilds_the_sidecar_from_the_host_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a cutover nothing is cached; the first render derives it again."""
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path))
    payload = {"session_id": "session-1", "cost": {"api_duration_ms": 17000, "cost_usd": 0.42}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(statusline, "render_pipeline", lambda *_args, **_kwargs: "rendered")
    sidecar = RuntimeCounterSidecar(tmp_path / COUNTER_SIDECAR_NAME)
    assert sidecar.read() is None

    line = statusline.run_with_cache(workspace=None, theme_name=None)

    assert line == "rendered"
    counters = sidecar.read()
    assert counters is not None
    assert counters.api_duration_ms == 17000


def test_meas_042_every_real_row_migrates_with_reconciling_counts() -> None:
    document, task_ids = _real_shape()

    plan = _build(document, task_ids)
    plan.require_reconciled(document)

    for kind, name in MEASUREMENT_COLLECTIONS.items():
        imported = plan.for_kind(kind)
        field = QUALITY_MARKER_FIELDS[kind]
        assert len(imported) == len(document[name])
        assert Counter(row.quality_marker for row in imported) == Counter(
            row[field] for row in document[name].values()
        )
    repointed = [row for row in plan.measurements if not row.is_immutable_legacy_record]
    assert len(repointed) + len(plan.legacy_keys) == len(plan.measurements)


def test_meas_042_the_full_corpus_plan_reconciles(full_corpus_plan: CorpusImportPlan) -> None:
    document = SourceSnapshot.read(EPOCH1_FULL_SNAPSHOT).document

    full_corpus_plan.measurements.require_reconciled(document)


def test_meas_042_require_reconciled_over_an_empty_document() -> None:
    _build({}, frozenset()).require_reconciled({})


def test_meas_042_require_reconciled_over_a_null_collection() -> None:
    """A null slot holds no rows, so an import of none reconciles."""
    document: dict[str, Any] = {"estimates": None, "actuals": None}

    _build(document, frozenset()).require_reconciled(document)


def test_meas_042_require_reconciled_over_a_single_row() -> None:
    document, task_ids = _real_shape()
    single = {"actuals": {PRICED_SUBJECT: document["actuals"][PRICED_SUBJECT]}}

    _build(single, task_ids).require_reconciled(single)


def test_meas_042_require_reconciled_refuses_one_row_too_few() -> None:
    document, task_ids = _real_shape()
    plan = _build(document, task_ids)
    short = plan.model_copy(update={"measurements": plan.measurements[:-1]})

    with pytest.raises(MigrationCountMismatchError, match="not carried"):
        short.require_reconciled(document)


def test_meas_042_require_reconciled_refuses_one_row_too_many() -> None:
    """A duplicated record reconciles by key but not by count."""
    document, task_ids = _real_shape()
    plan = _build(document, task_ids)
    doubled = plan.model_copy(update={"measurements": (*plan.measurements, plan.measurements[-1])})

    with pytest.raises(MigrationCountMismatchError, match="carries 6"):
        doubled.require_reconciled(document)


def test_meas_042_require_reconciled_refuses_rows_the_source_does_not_hold() -> None:
    document, task_ids = _real_shape()

    with pytest.raises(MigrationCountMismatchError, match="estimates holds 0 rows"):
        _build(document, task_ids).require_reconciled({})
