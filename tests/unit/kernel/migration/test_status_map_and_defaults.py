"""The closed status map and the two annotated defaults.

Archival is not acceptance, so ``archived`` and ``abandoned`` reach
CANCELLED and never COMPLETED. A status outside the map fails the plan.
Both annotated defaults are data on the record, so both survive the move
to the ledger at compaction.
"""

from __future__ import annotations

import pytest

from eawf.kernel.migration.epoch2.errors import MigrationCountMismatchError
from eawf.kernel.migration.epoch2.status_map import (
    BACKLOG_STATUS_MAP,
    COMPACTION_PRESERVED_ANNOTATIONS,
    DEFAULT_PRIORITY,
    INTENT_FROM_TITLE_ANNOTATION,
    ITER_STATUS_MAP,
    PHASE_STATUS_MAP,
    PRIORITY_DEFAULT_ANNOTATION,
    STATUS_MAPS,
    WAVE_STATUS_MAP,
    SourceLifecycle,
    apply_annotated_defaults,
    compact_annotations,
    map_source_status,
)
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.epoch2.task import TaskStatus


@pytest.mark.parametrize(
    ("lifecycle", "source_status", "expected"),
    [
        (SourceLifecycle.PHASE, "planned", "PLANNED"),
        (SourceLifecycle.PHASE, "active", "ACTIVE"),
        (SourceLifecycle.PHASE, "closed", "COMPLETED"),
        (SourceLifecycle.PHASE, "archived", "CANCELLED"),
        (SourceLifecycle.ITER, "abandoned", "CANCELLED"),
        (SourceLifecycle.WAVE, "pending", "PLANNED"),
        (SourceLifecycle.WAVE, "failed", "FAILED"),
        (SourceLifecycle.BACKLOG, "open", "DRAFT"),
        (SourceLifecycle.BACKLOG, "closed", "DROPPED"),
    ],
)
def test_map_source_status_maps_every_declared_status(
    lifecycle: SourceLifecycle, source_status: str, expected: str
) -> None:
    assert map_source_status(lifecycle, source_status) == expected


def test_map_source_status_archived_is_cancelled_never_completed() -> None:
    """Archival records that the phase stopped, not that anyone accepted it."""
    assert map_source_status(SourceLifecycle.PHASE, "archived") == "CANCELLED"
    assert PHASE_STATUS_MAP["archived"] != "COMPLETED"


def test_map_source_status_abandoned_is_cancelled_never_completed() -> None:
    assert map_source_status(SourceLifecycle.ITER, "abandoned") == "CANCELLED"
    assert ITER_STATUS_MAP["abandoned"] != "COMPLETED"
    assert map_source_status(SourceLifecycle.WAVE, "abandoned") == "CANCELLED"
    assert WAVE_STATUS_MAP["abandoned"] != "COMPLETED"


def test_wave_status_map_keys_match_the_wave_status_enum_exactly() -> None:
    """The wave map is keyed off the enum, not off the statuses a corpus happens to hold.

    Set equality reads in both directions at once: no member may go
    unmapped, and no retired spelling may survive alongside the current
    one as a second accepted key.
    """
    assert set(WAVE_STATUS_MAP) == {status.value for status in WaveStatus}


@pytest.mark.parametrize("status", list(WaveStatus), ids=lambda status: status.value)
def test_map_source_status_classifies_every_wave_status_member(status: WaveStatus) -> None:
    """Every member classifies, so a rename or an addition reds here and not on a live corpus."""
    assert map_source_status(SourceLifecycle.WAVE, status.value) in {
        member.value for member in TaskStatus
    }


def test_map_source_status_maps_the_in_flight_wave_status() -> None:
    assert map_source_status(SourceLifecycle.WAVE, WaveStatus.IN_PROGRESS.value) == "RUNNING"


def test_map_source_status_retired_in_flight_spelling_raises_count_mismatch() -> None:
    """The source never wrote ``running``; accepting it would import a status nobody recorded."""
    with pytest.raises(MigrationCountMismatchError, match="outside the closed status map"):
        map_source_status(SourceLifecycle.WAVE, "running")


def test_map_source_status_only_closed_reaches_completed() -> None:
    """No source status other than ``closed`` may claim acceptance."""
    for lifecycle, table in STATUS_MAPS.items():
        completed = sorted(source for source, target in table.items() if target == "COMPLETED")
        assert completed in ([], ["closed"]), lifecycle


def test_map_source_status_unknown_status_raises_count_mismatch() -> None:
    with pytest.raises(MigrationCountMismatchError, match="outside the closed status map") as exc:
        map_source_status(SourceLifecycle.PHASE, "paused")
    assert exc.value.code == "migration_count_mismatch"


def test_map_source_status_empty_status_raises_count_mismatch() -> None:
    with pytest.raises(MigrationCountMismatchError):
        map_source_status(SourceLifecycle.WAVE, "")


def test_map_source_status_is_case_sensitive() -> None:
    """The source writes lowercase statuses; an uppercase one is not the same fact."""
    with pytest.raises(MigrationCountMismatchError):
        map_source_status(SourceLifecycle.BACKLOG, "CLOSED")


def test_map_source_status_backlog_map_has_exactly_two_arms() -> None:
    assert dict(BACKLOG_STATUS_MAP) == {"open": "DRAFT", "closed": "DROPPED"}


def test_apply_annotated_defaults_fills_priority_p2_with_its_annotation() -> None:
    record, annotations = apply_annotated_defaults({"title": "Linux CI matrix"})
    assert record["priority"] == DEFAULT_PRIORITY
    assert PRIORITY_DEFAULT_ANNOTATION in annotations


def test_apply_annotated_defaults_fills_intent_from_title_with_its_annotation() -> None:
    record, annotations = apply_annotated_defaults({"title": "Linux CI matrix"})
    assert record["intent"] == "Linux CI matrix"
    assert INTENT_FROM_TITLE_ANNOTATION in annotations


def test_apply_annotated_defaults_keeps_a_recorded_priority_unannotated() -> None:
    record, annotations = apply_annotated_defaults(
        {"title": "t", "priority": "P0", "intent": "recorded"}
    )
    assert record["priority"] == "P0"
    assert record["intent"] == "recorded"
    assert annotations == ()


def test_apply_annotated_defaults_does_not_mutate_the_source_row() -> None:
    source = {"title": "t"}
    apply_annotated_defaults(source)
    assert source == {"title": "t"}


def test_apply_annotated_defaults_empty_row_raises_count_mismatch() -> None:
    """Intent defaults from the title, so a row with no title has no rule."""
    with pytest.raises(MigrationCountMismatchError, match="carries no title") as exc:
        apply_annotated_defaults({})
    assert exc.value.code == "migration_count_mismatch"


def test_apply_annotated_defaults_blank_title_raises_count_mismatch() -> None:
    with pytest.raises(MigrationCountMismatchError):
        apply_annotated_defaults({"title": "   "})


def test_apply_annotated_defaults_non_string_title_raises_count_mismatch() -> None:
    with pytest.raises(MigrationCountMismatchError):
        apply_annotated_defaults({"title": 17})


def test_compact_annotations_preserves_both_annotated_defaults() -> None:
    """Compaction moves the record to its ledger; the annotations travel with it."""
    _record, annotations = apply_annotated_defaults({"title": "t"})
    assert set(annotations) == COMPACTION_PRESERVED_ANNOTATIONS
    assert compact_annotations(annotations) == annotations


def test_compact_annotations_drops_an_unpreserved_annotation() -> None:
    annotations = (PRIORITY_DEFAULT_ANNOTATION, "draft_head", INTENT_FROM_TITLE_ANNOTATION)
    assert compact_annotations(annotations) == (
        PRIORITY_DEFAULT_ANNOTATION,
        INTENT_FROM_TITLE_ANNOTATION,
    )


def test_compact_annotations_on_an_empty_tuple_returns_empty() -> None:
    assert compact_annotations(()) == ()
