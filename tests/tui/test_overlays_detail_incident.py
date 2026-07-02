"""Tests for the incident-timeline projection (P30-I07-W16).

Two layers: the pure store-load + row-projection helpers in
:mod:`eawf.surfaces.tui.screens.overlays.detail_incident`, and the
``resolve_detail`` incident-card wiring that folds the loaded timeline into the
reused detail chassis. An incident with recorded events renders a chronological
timeline; an incident with no event renders the honest-empty line, never a
fabricated entry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.screens.overlays.detail_incident import (
    NO_EVENTS_LINE,
    TimelineEvent,
    incident_timeline_rows,
    load_incident_timeline,
)

_NOW = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)


def _write_incident_record(
    state_path: Path,
    *,
    record_id: str,
    scope_id: str,
    timeline: list[tuple[datetime, str]],
) -> None:
    """Append one INCIDENT store record carrying *timeline* entries.

    Mirrors the open / close mutators in
    :mod:`eawf.workflow.evidence.incident`: each record is keyed by an
    incident-derived id and carries a list of ``{at, entry}`` timeline rows.
    """
    envelope = Envelope(
        id=record_id,
        kind=StoreKind.INCIDENT,
        scope_id=scope_id,
        created_at=_NOW,
        updated_at=None,
        summary=f"incident record {record_id}",
        payload={
            "severity": "high",
            "timeline": [{"at": at.isoformat(), "entry": entry} for at, entry in timeline],
            "cause": "unknown",
            "corrective_action_ids": [],
        },
    )
    path = store_path(state_path, StoreKind.INCIDENT)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(envelope.model_dump_json() + "\n")


# --------------------------------------------------------------------------
# load_incident_timeline — store load + chronological gather
# --------------------------------------------------------------------------


def test_load_incident_timeline_missing_store_returns_empty(tmp_path: Path) -> None:
    # No store/ directory at all: honest empty, not a crash.
    events = load_incident_timeline(tmp_path / "state.json", "INC-001")
    assert events == ()


def test_load_incident_timeline_no_record_for_incident(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_incident_record(
        state_path,
        record_id="INC-OTHER",
        scope_id="QR",
        timeline=[(_NOW, "opened: other")],
    )
    # The store has a record, but none for INC-001.
    assert load_incident_timeline(state_path, "INC-001") == ()


def test_load_incident_timeline_gathers_open_and_close_records(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    opened = _NOW
    closed = datetime(2026, 5, 9, 9, 0, tzinfo=UTC)
    # Write the close record FIRST so the load must sort by timestamp, not by
    # store order, to read oldest-first.
    _write_incident_record(
        state_path,
        record_id="INC-001-CLOSE",
        scope_id="QR",
        timeline=[(closed, "closed: fixed the guard")],
    )
    _write_incident_record(
        state_path,
        record_id="INC-001",
        scope_id="QR",
        timeline=[(opened, "opened: validate exits 0")],
    )
    events = load_incident_timeline(state_path, "INC-001")
    assert [event.entry for event in events] == [
        "opened: validate exits 0",
        "closed: fixed the guard",
    ]
    assert [event.at for event in events] == [opened, closed]


def test_load_incident_timeline_skips_malformed_line(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_incident_record(
        state_path,
        record_id="INC-001",
        scope_id="QR",
        timeline=[(_NOW, "opened: validate exits 0")],
    )
    # A malformed (non-JSON) line in the store must be skipped, not crash.
    path = store_path(state_path, StoreKind.INCIDENT)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
        handle.write("\n")  # blank line ignored too
    events = load_incident_timeline(state_path, "INC-001")
    assert [event.entry for event in events] == ["opened: validate exits 0"]


def test_load_incident_timeline_ignores_other_store_kinds(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    # A non-incident envelope sharing the same id must not be folded in.
    foreign = Envelope(
        id="INC-001",
        kind=StoreKind.AUDIT,
        scope_id="QR",
        created_at=_NOW,
        updated_at=None,
        summary="audit, not incident",
        payload={},
    )
    path = store_path(state_path, StoreKind.INCIDENT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(foreign.model_dump_json() + "\n", encoding="utf-8")
    assert load_incident_timeline(state_path, "INC-001") == ()


# --------------------------------------------------------------------------
# incident_timeline_rows — row projection + honest empty
# --------------------------------------------------------------------------


def test_incident_timeline_rows_empty_is_honest_line() -> None:
    rows = incident_timeline_rows(())
    assert rows == (("timeline", NO_EVENTS_LINE),)


def test_incident_timeline_rows_one_event_per_entry() -> None:
    events = (
        TimelineEvent(at=_NOW, entry="opened: validate exits 0"),
        TimelineEvent(
            at=datetime(2026, 5, 9, 9, 0, tzinfo=UTC),
            entry="closed: fixed the guard",
        ),
    )
    rows = incident_timeline_rows(events)
    assert all(label == "event" for label, _ in rows)
    assert len(rows) == 2
    # I22-W09 compact UTC stamps: detail surfaces render "YYYY-MM-DD HH:MM:SS".
    assert _NOW.strftime("%Y-%m-%d %H:%M:%S") in rows[0][1]
    assert "opened: validate exits 0" in rows[0][1]
    assert "closed: fixed the guard" in rows[1][1]
