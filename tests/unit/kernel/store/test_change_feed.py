"""The change feed's models, diff, tiering, size bound and paging, without a daemon."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.store.changes import (
    MAX_PAGE,
    PREVIEW_CHARS,
    VALUE_BYTE_CAP,
    ChangeRecord,
    ChangeTier,
    StoredValue,
    append_change_record,
    append_change_record_once,
    change_log_path,
    document_changes,
    field_changes,
    read_change_page,
)
from eawf.kernel.store.tiers import Epoch2Collection

AT: Final = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _record(key: str, sequence: int, *, tier: ChangeTier = ChangeTier.COMMITTED) -> ChangeRecord:
    return ChangeRecord(
        change_id=f"evt-{sequence}:task:{key}",
        tier=tier,
        collection=Epoch2Collection.TASK,
        record_key=key,
        event_name="domain.task.planned",
        revision_before=1,
        revision_after=2,
        canonical_sequence=sequence,
        actor_ref="OP-0001",
        recorded_at=AT,
        changes=field_changes({"status": "DRAFT"}, {"status": "PLANNED"}),
    )


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "gen-abc" / "state.json"


# ---------- the size bound ----------


def test_a_value_at_the_cap_is_kept_verbatim() -> None:
    value = "x" * (VALUE_BYTE_CAP - 2)  # the JSON quotes make up the cap
    stored = StoredValue.of(value)
    assert (stored.value, stored.size, stored.cut) == (value, VALUE_BYTE_CAP, False)


def test_a_value_one_byte_over_the_cap_keeps_its_preview_and_digest() -> None:
    value = "x" * (VALUE_BYTE_CAP - 1)
    stored = StoredValue.of(value)
    assert stored.cut and stored.size == VALUE_BYTE_CAP + 1
    assert stored.value == f'"{value}"'[:PREVIEW_CHARS]
    assert stored.digest is not None and len(stored.digest) == 64


@pytest.mark.parametrize("value", [None, 0, True, [], {}, {"a": [1, 2]}])
def test_small_json_values_round_trip(value: Any) -> None:
    assert StoredValue.of(value).value == value


def test_a_value_json_cannot_encode_is_refused() -> None:
    with pytest.raises(TypeError):
        StoredValue.of(object())


# ---------- the diff ----------


def test_identical_rows_change_nothing() -> None:
    assert field_changes({}, {}) == ()
    assert field_changes({"a": 1}, {"a": 1}) == ()


def test_revision_and_updated_at_are_never_listed() -> None:
    assert (
        field_changes({"revision": 1, "updated_at": "a"}, {"revision": 2, "updated_at": "b"}) == ()
    )


def test_an_added_a_removed_and_a_changed_field_are_listed_in_name_order() -> None:
    changes = field_changes({"b": 1, "c": 1}, {"a": 1, "c": 2})
    assert [change.field for change in changes] == ["a", "b", "c"]
    assert changes[0].before is None and changes[1].after is None
    assert (changes[2].before, changes[2].after) == (StoredValue.of(1), StoredValue.of(2))


def test_a_document_diff_files_one_record_per_changed_row() -> None:
    before = {"task": {"T-1": {"status": "DRAFT", "revision": 1}, "T-2": {"status": "X"}}}
    after = {
        "task": {"T-1": {"status": "PLANNED", "revision": 2}, "T-2": {"status": "X"}},
        "run": {"RUN-1": {"status": "QUEUED", "revision": 1}},
        "canonical_sequence": 5,
    }
    records = document_changes(
        before,
        after,
        event_id="evt-1",
        event_name="domain.task.planned",
        canonical_sequence=5,
        actor_ref=None,
        recorded_at=AT,
    )
    assert [(r.collection.value, r.record_key, r.tier) for r in records] == [
        ("task", "T-1", ChangeTier.COMMITTED),
        ("run", "RUN-1", ChangeTier.LOCAL),
    ]
    assert (records[0].revision_before, records[0].revision_after) == (1, 2)
    assert (records[1].revision_before, records[1].revision_after) == (None, 1)


def test_a_row_whose_only_move_is_its_revision_files_nothing() -> None:
    records = document_changes(
        {"task": {"T-1": {"revision": 1}}},
        {"task": {"T-1": {"revision": 2}}},
        event_id="evt-1",
        event_name="domain.task.planned",
        canonical_sequence=1,
        actor_ref=None,
        recorded_at=AT,
    )
    assert records == ()


# ---------- the record ----------


def test_a_record_with_no_field_or_an_unknown_key_is_refused() -> None:
    payload = _record("T-1", 1).model_dump(mode="json")
    with pytest.raises(ValidationError):
        ChangeRecord.model_validate({**payload, "changes": []})
    with pytest.raises(ValidationError):
        ChangeRecord.model_validate({**payload, "extra": 1})
    with pytest.raises(ValidationError):
        ChangeRecord.model_validate({**payload, "canonical_sequence": 0})
    with pytest.raises(ValidationError):
        ChangeRecord.model_validate({key: v for key, v in payload.items() if key != "tier"})


# ---------- tiers, appends and pages ----------


def test_each_tier_has_its_own_file(state: Path) -> None:
    assert change_log_path(state, ChangeTier.COMMITTED) == state.parent / "history" / "change.jsonl"
    assert (
        change_log_path(state, ChangeTier.LOCAL)
        == state.parent / "local" / "history" / "change.jsonl"
    )
    append_change_record(state, _record("T-1", 1, tier=ChangeTier.LOCAL))
    assert not change_log_path(state, ChangeTier.COMMITTED).exists()
    assert change_log_path(state, ChangeTier.LOCAL).is_file()


def test_an_append_once_skips_a_record_already_filed(state: Path) -> None:
    record = _record("T-1", 1)
    assert append_change_record_once(state, record) is True
    assert append_change_record_once(state, record) is False
    assert len(read_change_page(state, record_key=None, cursor=None, limit=5).changes) == 1


def test_an_empty_feed_reads_an_empty_last_page(state: Path) -> None:
    page = read_change_page(state, record_key=None, cursor=None, limit=1)
    assert (page.changes, page.next_cursor, page.since.isoformat()) == ((), None, "2026-10-02")


def test_pages_run_newest_first_across_both_tiers(state: Path) -> None:
    append_change_record(state, _record("T-1", 1))
    append_change_record(state, _record("T-2", 2, tier=ChangeTier.LOCAL))
    append_change_record(state, _record("T-1", 3))
    first = read_change_page(state, record_key=None, cursor=None, limit=2)
    assert [r.canonical_sequence for r in first.changes] == [3, 2]
    second = read_change_page(state, record_key=None, cursor=first.next_cursor, limit=2)
    assert [r.canonical_sequence for r in second.changes] == [1]
    assert second.next_cursor is None
    only = read_change_page(state, record_key="T-1", cursor=None, limit=MAX_PAGE)
    assert [r.canonical_sequence for r in only.changes] == [3, 1]


def test_a_page_never_splits_one_commit(state: Path) -> None:
    for key in ("A-1", "B-1"):
        append_change_record(state, _record(key, 2))
    append_change_record(state, _record("C-1", 1))
    first = read_change_page(state, record_key=None, cursor=None, limit=1)
    assert sorted(r.record_key for r in first.changes) == ["A-1", "B-1"]
    assert first.next_cursor == 2
    second = read_change_page(state, record_key=None, cursor=2, limit=1)
    assert [r.record_key for r in second.changes] == ["C-1"]


def test_a_torn_tail_is_skipped(state: Path) -> None:
    append_change_record(state, _record("T-1", 1))
    path = change_log_path(state, ChangeTier.COMMITTED)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"change_id": "evt-2:ta')
    page = read_change_page(state, record_key=None, cursor=None, limit=5)
    assert [r.record_key for r in page.changes] == ["T-1"]


@pytest.mark.parametrize("limit", [0, MAX_PAGE + 1])
def test_a_page_size_outside_the_bound_is_refused(state: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="change page"):
        read_change_page(state, record_key=None, cursor=None, limit=limit)
