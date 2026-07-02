"""The seeded jury gold-label cohort (P30-I23-W18).

The jury-calibration substrate had a writer (W17) but an empty cohort, so
``eawf metrics jury-validation`` stayed honest-empty and the jury could
never earn (or be denied) authority on real evidence. W18 seeds the
committed ``.ea/store/gold_label.jsonl`` from known outcomes: known-bad
waves from the reopen / incident population (INC-P30-01..03/06 classes)
and known-good waves from clean audited closes. This suite pins the seed's
shape and proves the seed alone does NOT flip any authority floor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.kernel.state.models import State
from eawf.observability.eval.jury_validation import (
    BlockAuthority,
    GoldLabel,
    build_jury_validation_cohort,
)
from eawf.platform.profiles.models import VerifyBlock
from eawf.runtime.daemon.methods.state import _resolve_jury_block_authority

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATE_PATH = _REPO_ROOT / ".ea" / "state.json"
_GOLD_STORE = _REPO_ROOT / ".ea" / "store" / "gold_label.jsonl"


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


def test_cohort_reads_non_empty_and_authority_stays_advisory() -> None:
    """CR-02: the validation cohort is no longer honest-empty, yet the seed
    alone does not flip the block-authority floor -- BLOCKING stays earned,
    never seeded (P31 calibration scope)."""
    state = State.model_validate_json(_STATE_PATH.read_text(encoding="utf-8"))
    cohort = build_jury_validation_cohort(state, _STATE_PATH)
    # Cohort rows are the JOIN of gold labels with observed verdict
    # outcomes -- a label without a recorded verdict does not fabricate a
    # row. The seed makes the join non-empty; the 20-row floor on the raw
    # store is CR-01's assertion above.
    assert len(cohort.gold) >= 1

    authority = _resolve_jury_block_authority(
        state, state_path=_STATE_PATH, verify_block=VerifyBlock()
    )
    assert authority is BlockAuthority.ADVISORY


def test_metrics_cli_survives_ballotless_labelled_cohort() -> None:
    """CR-02: ``eawf metrics jury-validation`` reports the seeded cohort
    honestly (no crash on labelled waves that predate the ballot store)
    with block authority still advisory."""
    from typer.testing import CliRunner

    from eawf.surfaces.cli.app import app

    result = CliRunner().invoke(app, ["--workspace", str(_REPO_ROOT), "metrics", "jury-validation"])
    assert result.exit_code == 0, result.output
    assert "advisory" in result.output.lower()
    assert "labelled cohort rows" in result.output
