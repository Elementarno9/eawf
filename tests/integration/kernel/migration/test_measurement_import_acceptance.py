"""Every estimate and actual of this repository's own corpus imports whole.

The unit tests prove the re-point rule over a handful of shaped rows. This
drives the importer over the largest real corpus there is -- this
repository's committed ``.ea`` tree at HEAD, staged the way the operator
verb stages it -- and reconciles the imported measurements against the
source document row for row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.measurements import (
    CALIBRATION_EXCLUSION_FIELD,
    MEASUREMENT_COLLECTIONS,
    QUALITY_MARKER_FIELDS,
    MeasurementKind,
)
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot
from tests.integration.kernel.migration._live_corpus import stage_live_corpus

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[4]
ALLOWLIST_PATH = REPO_ROOT / "tests" / "fixtures" / "migration" / "allowed_legacy_symbols.txt"


@pytest.fixture(scope="module")
def imported(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], CorpusImportPlan]:
    """The live corpus's source document and its import plan, built once."""
    staged = stage_live_corpus(
        repo_root=REPO_ROOT, destination=tmp_path_factory.mktemp("live") / "snapshot"
    )
    snapshot = SourceSnapshot.read(staged)
    return snapshot.document, CorpusImportPlan.build(
        snapshot=snapshot, allowlist_path=ALLOWLIST_PATH
    )


@pytest.mark.parametrize("kind", list(MeasurementKind))
def test_prx_042_every_row_imports_and_the_counts_reconcile(
    imported: tuple[dict[str, Any], CorpusImportPlan], kind: MeasurementKind
) -> None:
    document, plan = imported
    source = document[MEASUREMENT_COLLECTIONS[kind]]
    rows = plan.measurements.for_kind(kind)

    assert len(source) > 0, "the live corpus carries no rows, so the count proves nothing"
    assert sorted(row.map_key for row in rows) == sorted(source)
    assert len(rows) == len(source)


def test_prx_042_every_subject_is_repointed_and_none_is_orphaned(
    imported: tuple[dict[str, Any], CorpusImportPlan],
) -> None:
    _document, plan = imported
    task_ids = plan.lifecycle.task_source_ids()

    assert plan.measurements.orphan_keys == ()
    for row in plan.measurements.measurements:
        expected = row.map_key if row.map_key in task_ids else None
        assert row.task_ref == expected
    assert any(row.task_ref is not None for row in plan.measurements.measurements)


@pytest.mark.parametrize("kind", list(MeasurementKind))
def test_prx_042_quality_and_exclusion_state_are_preserved_verbatim(
    imported: tuple[dict[str, Any], CorpusImportPlan], kind: MeasurementKind
) -> None:
    document, plan = imported
    source = document[MEASUREMENT_COLLECTIONS[kind]]

    for row in plan.measurements.for_kind(kind):
        original = source[row.map_key]
        quality = original.get(QUALITY_MARKER_FIELDS[kind])
        excluded = original.get(CALIBRATION_EXCLUSION_FIELD)
        assert row.quality_marker == (quality if isinstance(quality, str) and quality else None)
        assert row.calibration_excluded == (excluded if isinstance(excluded, bool) else None)
        assert row.payload == original
