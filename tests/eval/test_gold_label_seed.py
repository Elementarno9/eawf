"""The seeded jury gold-label cohort.

The jury-calibration substrate had a writer but an empty cohort, so
``eawf metrics jury-validation`` stayed honest-empty and the jury could
never earn (or be denied) authority on real evidence. W18 seeds the
committed ``.ea/store/gold_label.jsonl`` from known outcomes: known-bad
waves from the reopen / incident population (INC-P30-01..03/06 classes)
and known-good waves from clean audited closes. This suite pins the seed's
shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.observability.eval.jury_validation import GoldLabel

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT = _REPO_ROOT / "tests" / "fixtures" / "epoch1-snapshot"
_STATE_PATH = _SNAPSHOT / "state.json"
_GOLD_STORE = _SNAPSHOT / "store" / "gold_label.jsonl"


def _load_rows() -> list[GoldLabel]:
    lines = _GOLD_STORE.read_text(encoding="utf-8").splitlines()
    return [GoldLabel.model_validate_json(line) for line in lines if line.strip()]


def test_gold_label_seed_meets_cohort_floor() -> None:
    """CR-01: >= 20 schema-valid rows, >= 8 'bad', every reason non-empty."""
    rows = _load_rows()
    assert len(rows) >= 20
    bad = [row for row in rows if row.ground_truth is False]
    assert len(bad) >= 8
    for row in rows:
        assert row.note is not None and row.note.strip(), (
            f"gold label for {row.wave_id} has no rationale"
        )


def test_gold_label_seed_reasons_name_their_evidence() -> None:
    """CR-01: each bad reason cites an incident id, repair wave, or commit;
    each good reason cites an audit row, gate suite, or auditor report.

    Checked on the LATEST row per wave -- the store is append-only and a
    re-label supersedes the earlier record without rewriting history.
    """
    latest: dict[str, GoldLabel] = {}
    for row in _load_rows():
        current = latest.get(row.wave_id)
        if current is None or row.labeled_at >= current.labeled_at:
            latest[row.wave_id] = row
    for row in latest.values():
        note = row.note or ""
        if row.ground_truth is False:
            cites = ("INC-P30-" in note) or ("repaired by" in note)
        else:
            cites = (
                ("audit A-" in note)
                or ("tests/" in note)
                or ("auditor" in note)
                or ("gauntlet" in note)
            )
        assert cites, f"gold label for {row.wave_id} names no evidence: {note!r}"


def test_gold_label_waves_all_present_in_state() -> None:
    """Every seeded label anchors on a wave present in the committed state."""
    raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    waves = raw.get("waves") or {}
    for row in _load_rows():
        assert row.wave_id in waves, f"label anchors on unknown wave {row.wave_id}"
