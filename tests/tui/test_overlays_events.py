"""Tests for the C06 ``EventsModal`` ``/events`` overlay (P26-W21).

Two layers: the pure event-store reader + filter cycle
(:func:`load_recent_events` / :func:`filter_rows` / :func:`next_filter`,
plus the :class:`EventRow` predicates) without Textual, and Pilot-driven
mounting + ``f`` filter cycling of the overlay through the modal-stack cap.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest
from rich.text import Text
from textual.widgets import Static

from eawf.tui.app import EaApp
from eawf.tui.screens.overlays.events import (
    EVENT_FILTERS,
    EVENT_RING_SIZE,
    EventRow,
    EventsModal,
    filter_rows,
    load_recent_events,
    next_filter,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


def _text(widget: Static) -> str:
    rendered = widget.render()
    return rendered.plain if isinstance(rendered, Text) else str(rendered)


def _write_event(
    fh_lines: list[bytes],
    *,
    event_id: str,
    event_type: str,
    status: str,
    summary: str,
) -> None:
    fh_lines.append(
        orjson.dumps(
            {
                "schema_version": "1.0",
                "id": event_id,
                "kind": "event",
                "scope_id": "EAWF",
                "created_at": "2026-05-10T12:00:00Z",
                "updated_at": None,
                "summary": summary,
                "payload": {
                    "timestamp": "2026-05-10T12:00:00Z",
                    "event_type": event_type,
                    "actor": "cli",
                    "command": "x",
                    "args_hash": "h",
                    "status": status,
                    "message": summary,
                },
                "blob_refs": [],
                "artifact_ids": [],
            }
        )
    )


def _store_with_events(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    """Write an ``event.jsonl`` under ``tmp_path/store`` and return its path."""
    store = tmp_path / "store"
    store.mkdir(parents=True, exist_ok=True)
    lines: list[bytes] = []
    for row in rows:
        _write_event(
            lines,
            event_id=row["id"],
            event_type=row["event_type"],
            status=row["status"],
            summary=row["summary"],
        )
    path = store / "event.jsonl"
    path.write_bytes(b"\n".join(lines) + b"\n")
    return path


# --------------------------------------------------------------------------
# EventRow predicates
# --------------------------------------------------------------------------


def test_event_row_is_error_when_status_not_ok() -> None:
    ok = EventRow("EV-1", "t", "wave close", "ok", "s")
    bad = EventRow("EV-2", "t", "dispatch cost", "fail", "s")
    assert ok.is_error is False
    assert bad.is_error is True


def test_event_row_is_report_when_type_has_report() -> None:
    report = EventRow("EV-3", "t", "executor report", "ok", "s")
    plain = EventRow("EV-4", "t", "wave close", "ok", "s")
    assert report.is_report is True
    assert plain.is_report is False


# --------------------------------------------------------------------------
# load_recent_events — read-only tail
# --------------------------------------------------------------------------


def test_load_recent_events_none_path_is_empty() -> None:
    assert load_recent_events(None) == ()


def test_load_recent_events_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_recent_events(tmp_path / "store" / "event.jsonl") == ()


def test_load_recent_events_returns_newest_first(tmp_path: Path) -> None:
    path = _store_with_events(
        tmp_path,
        [
            {"id": "EV-1", "event_type": "first", "status": "ok", "summary": "a"},
            {"id": "EV-2", "event_type": "second", "status": "ok", "summary": "b"},
            {"id": "EV-3", "event_type": "third", "status": "ok", "summary": "c"},
        ],
    )
    rows = load_recent_events(path)
    assert [r.event_id for r in rows] == ["EV-3", "EV-2", "EV-1"]


def test_load_recent_events_truncates_to_limit(tmp_path: Path) -> None:
    rows = [{"id": f"EV-{i}", "event_type": "e", "status": "ok", "summary": "s"} for i in range(10)]
    path = _store_with_events(tmp_path, rows)
    loaded = load_recent_events(path, limit=3)
    assert len(loaded) == 3
    # Newest three (EV-9, EV-8, EV-7).
    assert [r.event_id for r in loaded] == ["EV-9", "EV-8", "EV-7"]


def test_load_recent_events_skips_malformed_lines(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir(parents=True)
    path = store / "event.jsonl"
    good = orjson.dumps(
        {
            "schema_version": "1.0",
            "id": "EV-1",
            "kind": "event",
            "scope_id": "EAWF",
            "created_at": "2026-05-10T12:00:00Z",
            "updated_at": None,
            "summary": "good",
            "payload": {
                "timestamp": "2026-05-10T12:00:00Z",
                "event_type": "ok event",
                "actor": "cli",
                "command": "x",
                "args_hash": "h",
                "status": "ok",
                "message": "good",
            },
            "blob_refs": [],
            "artifact_ids": [],
        }
    )
    path.write_bytes(b"not json\n" + good + b"\n{ broken\n")
    rows = load_recent_events(path)
    assert len(rows) == 1
    assert rows[0].event_id == "EV-1"


def test_load_recent_events_default_limit_is_ring_size() -> None:
    assert EVENT_RING_SIZE == 50


# --------------------------------------------------------------------------
# filter_rows + next_filter — the f cycle
# --------------------------------------------------------------------------


_SAMPLE = (
    EventRow("EV-1", "t", "wave close", "ok", "closed"),
    EventRow("EV-2", "t", "dispatch cost", "fail", "boom"),
    EventRow("EV-3", "t", "executor report", "ok", "report body"),
)


def test_filter_rows_all_returns_everything() -> None:
    assert filter_rows(_SAMPLE, "all") == _SAMPLE


def test_filter_rows_errors_keeps_only_non_ok() -> None:
    rows = filter_rows(_SAMPLE, "errors")
    assert [r.event_id for r in rows] == ["EV-2"]


def test_filter_rows_reports_keeps_only_reports() -> None:
    rows = filter_rows(_SAMPLE, "reports")
    assert [r.event_id for r in rows] == ["EV-3"]


def test_next_filter_cycles_and_wraps() -> None:
    assert next_filter("all") == "errors"
    assert next_filter("errors") == "reports"
    assert next_filter("reports") == "all"


def test_event_filters_order() -> None:
    assert EVENT_FILTERS == ("all", "errors", "reports")


# --------------------------------------------------------------------------
# EventsModal — mounting + f cycle (Pilot)
# --------------------------------------------------------------------------


def test_events_modal_mounts_rows() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(EventsModal(_SAMPLE))
            await pilot.pause()
            assert isinstance(app.screen, EventsModal)
            listing = app.screen.query_one("#events-list")
            assert len(listing.query(".events-row")) == 3
            assert "filter all" in _text(app.screen.query_one("#events-heading", Static))

    asyncio.run(body())


def test_events_modal_f_cycles_filter() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(EventsModal(_SAMPLE))
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()
            heading = _text(app.screen.query_one("#events-heading", Static))
            assert "filter errors" in heading
            assert len(app.screen.query_one("#events-list").query(".events-row")) == 1

    asyncio.run(body())


def test_events_modal_empty_shows_note() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(EventsModal(()))
            await pilot.pause()
            assert app.screen.query_one("#events-list").query(".events-empty")

    asyncio.run(body())


def test_events_verb_opens_modal_through_cap() -> None:
    async def body() -> None:
        from eawf.tui.palette.verbs import VERBS

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            handler = next(v.handler for v in VERBS if v.name == "/events")
            handler(app, "")
            await pilot.pause()
            assert isinstance(app.screen, EventsModal)
            assert app.modal_depth() == 1

    asyncio.run(body())


def test_events_modal_esc_closes() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(EventsModal(_SAMPLE))
            await pilot.pause()
            assert app.modal_depth() == 1
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_filter_rows_errors_empty_when_all_ok() -> None:
    ok_only = (EventRow("EV-1", "t", "wave close", "ok", "s"),)
    assert filter_rows(ok_only, "errors") == ()


def test_load_recent_events_zero_limit(tmp_path: Path) -> None:
    path = _store_with_events(
        tmp_path,
        [{"id": "EV-1", "event_type": "e", "status": "ok", "summary": "s"}],
    )
    assert load_recent_events(path, limit=0) == ()


@pytest.mark.parametrize("bad_filter", ["all", "errors", "reports"])
def test_filter_rows_known_filters_total(bad_filter: str) -> None:
    # Every declared filter returns a (possibly empty) tuple, never raises.
    result = filter_rows(_SAMPLE, bad_filter)  # type: ignore[arg-type]
    assert isinstance(result, tuple)


def test_events_hint_has_top_margin() -> None:
    # W15 polish: the close-hint gets a top margin so it no longer sits
    # flush against the event rows (mirrors the DetailModal hint gap).
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(EventsModal(_SAMPLE))
            await pilot.pause()
            hint = app.screen.query_one(".events-hint", Static)
            assert hint.styles.margin.top == 1

    asyncio.run(body())
