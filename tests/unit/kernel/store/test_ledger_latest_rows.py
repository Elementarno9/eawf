"""A ledger reads back the latest standing row of each record that compacted into it."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerTornTailError,
    append_correction,
    append_ledger_record,
    latest_rows,
    line_digest,
    render_ledger_line,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection

pytestmark = pytest.mark.unit


def _record(
    key: str,
    *,
    revision: int = 1,
    payload: dict[str, Any] | None = None,
    supersedes: str | None = None,
) -> LedgerRecord:
    return LedgerRecord(
        collection=Epoch2Collection.TASK,
        record_key=key,
        status="COMPLETED",
        recorded_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        supersedes=supersedes,
        payload={"key": key, "revision": revision} if payload is None else payload,
    )


def _ledger(tmp_path: Path) -> Path:
    return ledger_path(tmp_path / ".ea" / "state.json", Epoch2Collection.TASK)


def test_a_missing_ledger_reads_as_no_rows(tmp_path: Path) -> None:
    """The empty boundary: a collection that never compacted has no ledger file."""
    assert dict(latest_rows(_ledger(tmp_path))) == {}


def test_a_single_row_reads_back_under_its_key(tmp_path: Path) -> None:
    """One line, one row."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))

    assert dict(latest_rows(path)) == {"EAWF-0001": {"key": "EAWF-0001", "revision": 1}}


def test_the_last_line_of_a_key_wins(tmp_path: Path) -> None:
    """A record filed twice reads back as its later line."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001", revision=1))
    append_ledger_record(path, _record("EAWF-0002", revision=1))
    append_ledger_record(path, _record("EAWF-0001", revision=2))

    rows = latest_rows(path)

    assert rows["EAWF-0001"]["revision"] == 2
    assert sorted(rows) == ["EAWF-0001", "EAWF-0002"]


def test_a_line_filed_for_another_reason_is_not_a_row(tmp_path: Path) -> None:
    """An imported legacy record states no key of its own, so it is passed over."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("P08-I01-W01", payload={"record": {"intent": "legacy"}}))
    append_ledger_record(path, _record("EAWF-0001", payload={"key": "EAWF-0009"}))

    assert dict(latest_rows(path)) == {}


def test_a_superseded_line_does_not_stand(tmp_path: Path) -> None:
    """A correction replaces the line it names, so the corrected line is never read back."""
    path = _ledger(tmp_path)
    wrong = _record("EAWF-0001", revision=7)
    append_ledger_record(path, wrong)
    append_correction(
        path,
        _record(
            "EAWF-0002",
            payload={"key": "EAWF-0002"},
            supersedes=line_digest(render_ledger_line(wrong)),
        ),
    )

    assert sorted(latest_rows(path)) == ["EAWF-0002"]


def test_an_unchanged_ledger_is_read_once_and_an_append_is_seen(tmp_path: Path) -> None:
    """The read is cached on the file's size and stamp, and an append invalidates it."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    first = latest_rows(path)

    assert latest_rows(path) is first

    append_ledger_record(path, _record("EAWF-0002"))
    assert sorted(latest_rows(path)) == ["EAWF-0001", "EAWF-0002"]


def test_a_torn_tail_is_refused(tmp_path: Path) -> None:
    """A ledger that ends mid-line cannot be read until it is repaired."""
    path = _ledger(tmp_path)
    append_ledger_record(path, _record("EAWF-0001"))
    with path.open("ab") as handle:
        handle.write(b'{"schema_version"')

    with pytest.raises(LedgerTornTailError):
        latest_rows(path)


def test_a_line_that_is_not_a_ledger_record_is_refused(tmp_path: Path) -> None:
    """A schema mismatch is corruption, raised rather than read around."""
    path = _ledger(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"collection":"task"}\n')

    with pytest.raises(ValidationError):
        latest_rows(path)
