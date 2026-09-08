"""The four-arm classifier over closed backlog resolution prose.

One case per arm, the ambiguous phase-wave shorthand, and a totality
case over every closed row of the pinned epoch-1 corpus.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.backlog import (
    AMBIGUOUS_SHORTHAND_ANNOTATION,
    DROPPED_TARGET_STATUS,
    RESOLUTION_ARM_ORDER,
    ResolutionArm,
    ResolutionCorpus,
    classify_backlog_resolution,
)
from eawf.kernel.migration.epoch2.corpus import Epoch1BacklogCorpus
from eawf.kernel.migration.epoch2.errors import MigrationCountMismatchError

# Measured over the pinned corpus at revision ae04a5c1: 71 closed rows,
# every one reaching an arm, so the classifier leaves zero prose survivors.
EXPECTED_CLOSED_ROWS = 71
EXPECTED_ARM_COUNTS = {
    ResolutionArm.DELIVERED_BY_TASK: 21,
    ResolutionArm.SUPERSEDED_BY_DRAFT: 9,
    ResolutionArm.OBSOLETED_BY_AUDIT: 6,
    ResolutionArm.CLOSED_WITH_COMMIT_ONLY: 35,
}


@pytest.fixture
def corpus() -> ResolutionCorpus:
    """A small corpus with one unique and one four-way ambiguous shorthand."""
    return ResolutionCorpus.build(
        wave_ids=(
            "P14-I01-W01",
            "P14-I01-W02",
            "P14-I02-W02",
            "P14-I03-W02",
            "P14-I04-W02",
            "P30-I05-W07",
        ),
        backlog_ids=("B001", "B002", "B003"),
        audit_ids=("A12", "A30"),
    )


def _row(resolution: str, *, commit: str | None = "c0ffee1") -> dict[str, Any]:
    """Build a closed backlog row carrying ``resolution``."""
    row: dict[str, Any] = {"status": "closed", "resolution": resolution}
    if commit is not None:
        row["commit"] = commit
    return row


def test_classify_backlog_resolution_delivered_by_task_arm(corpus: ResolutionCorpus) -> None:
    result = classify_backlog_resolution(
        backlog_id="B001",
        row=_row("Delivered by P30-I05-W07 and superseded B002 along the way; see A12."),
        corpus=corpus,
    )
    assert result.arm is ResolutionArm.DELIVERED_BY_TASK
    assert result.delivered_by_wave_id == "P30-I05-W07"
    assert result.superseded_by_backlog_id is None
    assert result.obsoleted_by_audit_id is None
    assert result.close_commit is None


def test_classify_backlog_resolution_superseded_by_draft_arm(corpus: ResolutionCorpus) -> None:
    result = classify_backlog_resolution(
        backlog_id="B001",
        row=_row("Folded into B003; audit A12 recorded the merge."),
        corpus=corpus,
    )
    assert result.arm is ResolutionArm.SUPERSEDED_BY_DRAFT
    assert result.superseded_by_backlog_id == "B003"
    assert result.delivered_by_wave_id is None


def test_classify_backlog_resolution_obsoleted_by_audit_arm(corpus: ResolutionCorpus) -> None:
    result = classify_backlog_resolution(
        backlog_id="B001",
        row=_row("Audit A30 found the subject already gone."),
        corpus=corpus,
    )
    assert result.arm is ResolutionArm.OBSOLETED_BY_AUDIT
    assert result.obsoleted_by_audit_id == "A30"


def test_classify_backlog_resolution_closed_with_commit_only_arm(corpus: ResolutionCorpus) -> None:
    result = classify_backlog_resolution(
        backlog_id="B001",
        row=_row("Landed as part of the release sweep."),
        corpus=corpus,
    )
    assert result.arm is ResolutionArm.CLOSED_WITH_COMMIT_ONLY
    assert result.close_commit == "c0ffee1"
    assert result.close_note == "Landed as part of the release sweep."


def test_classify_backlog_resolution_unresolvable_ids_reach_the_fourth_arm(
    corpus: ResolutionCorpus,
) -> None:
    """An id shaped like a wave, draft or audit but absent from the source resolves nothing."""
    result = classify_backlog_resolution(
        backlog_id="B001",
        row=_row("Closed against P99-I09-W09, B099 and A99."),
        corpus=corpus,
    )
    assert result.arm is ResolutionArm.CLOSED_WITH_COMMIT_ONLY


def test_classify_backlog_resolution_own_id_never_supersedes(corpus: ResolutionCorpus) -> None:
    result = classify_backlog_resolution(
        backlog_id="B002",
        row=_row("B002 was closed by hand."),
        corpus=corpus,
    )
    assert result.arm is ResolutionArm.CLOSED_WITH_COMMIT_ONLY
    assert result.superseded_by_backlog_id is None


def test_classify_backlog_resolution_ambiguous_shorthand_records_four_candidates(
    corpus: ResolutionCorpus,
) -> None:
    """P14-W02 matches four waves, so it is recorded rather than guessed at."""
    result = classify_backlog_resolution(
        backlog_id="B001",
        row=_row("Delivered by P14-W02."),
        corpus=corpus,
    )
    assert result.arm is ResolutionArm.CLOSED_WITH_COMMIT_ONLY
    assert result.delivered_by_wave_id is None
    assert [item.token for item in result.ambiguous_delivered_by] == ["P14-W02"]
    assert result.ambiguous_delivered_by[0].candidates == (
        "P14-I01-W02",
        "P14-I02-W02",
        "P14-I03-W02",
        "P14-I04-W02",
    )
    assert AMBIGUOUS_SHORTHAND_ANNOTATION in result.annotations


def test_classify_backlog_resolution_unique_shorthand_resolves(corpus: ResolutionCorpus) -> None:
    result = classify_backlog_resolution(
        backlog_id="B001",
        row=_row("Delivered by P14-W01."),
        corpus=corpus,
    )
    assert result.arm is ResolutionArm.DELIVERED_BY_TASK
    assert result.delivered_by_wave_id == "P14-I01-W01"
    assert result.ambiguous_delivered_by == ()


def test_classify_backlog_resolution_empty_resolution_and_no_commit_fails(
    corpus: ResolutionCorpus,
) -> None:
    """A row with nothing to record reaches no arm and fails the plan."""
    with pytest.raises(MigrationCountMismatchError, match="reaches no classifier arm") as excinfo:
        classify_backlog_resolution(
            backlog_id="B001",
            row={"status": "closed", "resolution": "", "commit": None},
            corpus=corpus,
        )
    assert excinfo.value.code == "migration_count_mismatch"


def test_classify_backlog_resolution_open_row_is_refused(corpus: ResolutionCorpus) -> None:
    with pytest.raises(ValueError, match="only 'closed' rows are classified"):
        classify_backlog_resolution(
            backlog_id="B001",
            row={"status": "open", "resolution": "still open"},
            corpus=corpus,
        )


def test_resolution_corpus_build_rejects_a_non_canonical_wave_id() -> None:
    with pytest.raises(ValueError, match="not canonical"):
        ResolutionCorpus.build(wave_ids=("P14-W01",), backlog_ids=(), audit_ids=())


def test_resolution_corpus_build_accepts_an_empty_corpus() -> None:
    corpus = ResolutionCorpus.build(wave_ids=(), backlog_ids=(), audit_ids=())
    assert corpus.wave_short_index == {}


def test_classify_backlog_resolution_arm_order_is_declared_and_total() -> None:
    assert RESOLUTION_ARM_ORDER == (
        ResolutionArm.DELIVERED_BY_TASK,
        ResolutionArm.SUPERSEDED_BY_DRAFT,
        ResolutionArm.OBSOLETED_BY_AUDIT,
        ResolutionArm.CLOSED_WITH_COMMIT_ONLY,
    )


def test_classify_backlog_resolution_totality_over_the_epoch1_full_corpus(
    epoch1_full: Epoch1BacklogCorpus,
) -> None:
    """Every closed row of the pinned corpus reaches an arm, all of them DROPPED."""
    corpus = epoch1_full.resolution_corpus()
    closed = epoch1_full.closed_rows()
    assert len(closed) == EXPECTED_CLOSED_ROWS

    arms: Counter[ResolutionArm] = Counter()
    ambiguous: list[tuple[str, str, tuple[str, ...]]] = []
    for backlog_id, row in sorted(closed.items()):
        result = classify_backlog_resolution(backlog_id=backlog_id, row=row, corpus=corpus)
        assert result.target_status == DROPPED_TARGET_STATUS
        arms[result.arm] += 1
        for item in result.ambiguous_delivered_by:
            ambiguous.append((backlog_id, item.token, item.candidates))

    assert sum(arms.values()) == EXPECTED_CLOSED_ROWS
    assert dict(arms) == EXPECTED_ARM_COUNTS
    assert ambiguous == [
        (
            "B054",
            "P14-W01",
            ("P14-I01-W01", "P14-I02-W01", "P14-I03-W01", "P14-I04-W01"),
        )
    ]


def test_classify_backlog_resolution_is_deterministic_over_the_corpus(
    epoch1_full: Epoch1BacklogCorpus,
) -> None:
    """The same string always yields the same arm, so a re-run is identical."""
    corpus = epoch1_full.resolution_corpus()
    closed = epoch1_full.closed_rows()
    first = [
        classify_backlog_resolution(backlog_id=key, row=row, corpus=corpus).model_dump(mode="json")
        for key, row in sorted(closed.items())
    ]
    second = [
        classify_backlog_resolution(backlog_id=key, row=row, corpus=corpus).model_dump(mode="json")
        for key, row in sorted(closed.items())
    ]
    assert first == second
