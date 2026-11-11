"""The status line counts recent unacknowledged hook failures, and an ack clears them.

A failure counts when its row is newer than both the last acknowledgement and
24 hours ago; the record is read backward and the count stops at 100, which
the hooks segment shows as ``hooks!99+``. ``eawf hook health ack`` writes the
acknowledgement, prints how many failures it covered, moves an oversized
record aside and drops the cached status lines, so the next render is clean.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the output line is the contract

from eawf.runtime.hooks import health
from eawf.runtime.hooks.health import (
    HEALTH_ACK,
    HEALTH_RECORD,
    HEALTH_ROTATE_BYTES,
    HEALTH_ROTATED,
    WINDOW_ROWS,
    acknowledge,
    read_ack,
    read_cursor,
    recent_failure_count,
    write_cursor,
)
from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.runtime.runtimes.claude.statusline_modules import hooks_plugins
from eawf.surfaces.cli.app import app

_NOW: Final = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

#: A recorded status line payload; the session id makes the render cached.
_PAYLOAD: Final[dict[str, Any]] = {
    "session_id": "ses-hook-window",
    "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
    "cost": {"total_cost_usd": 0.5, "total_duration_ms": 60_000, "total_api_duration_ms": 30_000},
}

#: Variables of the developer's shell that would change what is read or drawn.
_SCRUBBED: Final = ("EA_STATE", "NO_COLOR", "EAWF_STATUSLINE_THEME", "EAWF_STATUSLINE_GLYPHS")


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a workspace with an ``.ea`` directory, its home and cache under *tmp_path*."""
    for variable in _SCRUBBED:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "120")
    (tmp_path / "work" / ".ea").mkdir(parents=True)
    return tmp_path / "work"


def _line(at: datetime | str, code: str = "hook_exit") -> str:
    stamp = at if isinstance(at, str) else at.isoformat()
    return json.dumps(
        {
            "at": stamp,
            "event": "post_tool_use",
            "hook": "wrapper",
            "code": code,
            "error": "exit 1",
            "detail": "",
            "session_id": None,
        }
    )


def _write(ea: Path, relative: Path, stamps: list[datetime], tail: str = "") -> None:
    path = ea / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{_line(at)}\n" for at in stamps) + tail, encoding="utf-8")


def _recent(count: int, now: datetime) -> list[datetime]:
    """Return *count* stamps within the last hour before *now*, oldest first."""
    return [now - timedelta(seconds=count - index) for index in range(count)]


def _segment(tree: Path) -> str:
    return hooks_plugins.build({}, tree / ".ea" / "state.json").text


def _render(tree: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(_PAYLOAD).encode())))
    return orchestrator.run_with_cache(workspace=tree, theme_name="none")


def _ack(tree: Path | None) -> Any:
    argv = (
        ["hook", "health", "ack"]
        if tree is None
        else ["--workspace", str(tree), "hook", "health", "ack"]
    )
    return CliRunner().invoke(app, argv)


# ---- the window count and the segment token ----------------------------------


def test_window_count(tree: Path) -> None:
    ea = tree / ".ea"
    assert recent_failure_count(ea, now=_NOW) == 0

    # Only rows after the window start count; the row at the boundary is older.
    window = _NOW - timedelta(hours=24)
    _write(ea, HEALTH_RECORD, [window - timedelta(hours=1), window, *_recent(3, _NOW)])
    assert recent_failure_count(ea, now=_NOW) == 3

    # An acknowledgement later than the window start moves it.
    ack = ea / HEALTH_ACK
    ack.write_text(json.dumps({"acknowledged_at": (_NOW - timedelta(seconds=2)).isoformat()}))
    assert recent_failure_count(ea, now=_NOW) == 1
    # An acknowledgement older than the window leaves the window start in force.
    ack.write_text(json.dumps({"acknowledged_at": (window - timedelta(days=3)).isoformat()}))
    assert recent_failure_count(ea, now=_NOW) == 3
    ack.unlink()

    # 99 rows count 99; the read stops at 100 rows however many are recent.
    _write(ea, HEALTH_RECORD, _recent(WINDOW_ROWS - 1, _NOW))
    assert recent_failure_count(ea, now=_NOW) == 99
    _write(ea, HEALTH_RECORD, _recent(WINDOW_ROWS, _NOW))
    assert recent_failure_count(ea, now=_NOW) == 100
    _write(ea, HEALTH_RECORD, _recent(WINDOW_ROWS + 50, _NOW))
    assert recent_failure_count(ea, now=_NOW) == 100

    # The segment shows nothing at 0, hooks!99 at 99 and hooks!99+ from 100.
    now = datetime.now(UTC)
    (ea / HEALTH_RECORD).unlink()
    assert "hooks!" not in _segment(tree)
    _write(ea, HEALTH_RECORD, _recent(1, now))
    assert _segment(tree).endswith(" hooks!1")
    _write(ea, HEALTH_RECORD, _recent(WINDOW_ROWS - 1, now))
    assert _segment(tree).endswith(" hooks!99")
    _write(ea, HEALTH_RECORD, _recent(WINDOW_ROWS, now))
    assert _segment(tree).endswith(" hooks!99+")
    # Rows older than the window show nothing however many there are.
    _write(ea, HEALTH_RECORD, _recent(WINDOW_ROWS, now - timedelta(hours=25)))
    assert "hooks!" not in _segment(tree)


def test_window_count_reads_the_older_generation_after_the_current(tree: Path) -> None:
    ea = tree / ".ea"
    _write(ea, HEALTH_ROTATED, _recent(4, _NOW - timedelta(minutes=10)))
    _write(ea, HEALTH_RECORD, _recent(2, _NOW))
    assert recent_failure_count(ea, now=_NOW) == 6


def test_window_count_skips_an_unfinished_blank_or_malformed_line(tree: Path) -> None:
    ea = tree / ".ea"
    recent = _recent(2, _NOW)
    path = ea / HEALTH_RECORD
    path.parent.mkdir(parents=True)
    body = f"{_line(recent[0])}\n\nnot json\n{_line('not a time')}\n{_line(recent[1])}\n"
    path.write_text(body + _line(_NOW)[:-5], encoding="utf-8")
    assert recent_failure_count(ea, now=_NOW) == 2
    # A record of one unfinished line holds no row yet.
    path.write_text(_line(_NOW - timedelta(seconds=1)), encoding="utf-8")
    assert recent_failure_count(ea, now=_NOW) == 0
    path.write_text("", encoding="utf-8")
    assert recent_failure_count(ea, now=_NOW) == 0


def test_window_count_reads_rows_across_block_boundaries(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "_READ_BLOCK", 7)
    ea = tree / ".ea"
    _write(ea, HEALTH_RECORD, [_NOW - timedelta(days=2), *_recent(5, _NOW)])
    assert recent_failure_count(ea, now=_NOW) == 5


def test_window_count_reads_a_row_without_an_offset_as_utc(tree: Path) -> None:
    ea = tree / ".ea"
    naive = (_NOW - timedelta(minutes=1)).replace(tzinfo=None)
    _write(ea, HEALTH_RECORD, [naive])
    assert recent_failure_count(ea, now=_NOW) == 1
    assert recent_failure_count(ea, now=_NOW + timedelta(hours=24)) == 0


@pytest.mark.parametrize(
    "ack",
    [
        "garbage",
        "{}",
        '{"acknowledged_at": "2026-11-10T12:00:00"}',
        '{"acknowledged_at": "2099-01-01T00:00:00+00:00", "x": 1}',
    ],
)
def test_statusline_repro_bad_ack_hides_hook_failures(tree: Path, ack: str) -> None:
    # error-path: a corrupt acknowledgement acknowledges nothing, never silences the alert.
    ea = tree / ".ea"
    now = datetime.now(UTC)
    _write(ea, HEALTH_RECORD, _recent(2, now))
    (ea / HEALTH_ACK).write_text(ack)
    assert recent_failure_count(ea, now=now) == 2
    assert "hooks!2" in _segment(tree)


def test_read_ack_without_a_file_or_with_a_bad_one_is_none(tree: Path) -> None:
    ea = tree / ".ea"
    assert read_ack(ea) is None
    (ea / HEALTH_ACK).mkdir(parents=True)
    # error-path: a directory in the file's place cannot be read.
    assert read_ack(ea) is None
    (ea / HEALTH_ACK).rmdir()
    (ea / HEALTH_ACK).write_text('{"acknowledged_at": "2026-11-10T12:00:00"}')
    assert read_ack(ea) is None
    (ea / HEALTH_ACK).write_text('{"acknowledged_at": "2026-11-10T12:00:00+00:00"}')
    assert read_ack(ea) == _NOW


def test_window_count_raises_when_the_record_cannot_be_read(tree: Path) -> None:
    (tree / ".ea" / HEALTH_RECORD).mkdir(parents=True)
    with pytest.raises(OSError):
        recent_failure_count(tree / ".ea", now=_NOW)


# ---- eawf hook health ack ---------------------------------------------------


def test_ack(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ea = tree / ".ea"
    now = datetime.now(UTC)
    _write(ea, HEALTH_RECORD, [now - timedelta(days=2), *_recent(3, now)])

    # The first render is cached and shows the failures.
    assert "hooks!3" in _render(tree, monkeypatch)

    result = _ack(tree)

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "acknowledged 3 hook failures"
    stored = json.loads((ea / HEALTH_ACK).read_text(encoding="utf-8"))
    assert list(stored) == ["acknowledged_at"]
    acknowledged = datetime.fromisoformat(stored["acknowledged_at"])
    assert acknowledged.utcoffset() == timedelta(0)
    assert now <= acknowledged <= datetime.now(UTC)
    # The next render, through the same session's cache, shows no failure.
    assert "hooks!" not in _render(tree, monkeypatch)

    # A second ack covers nothing new.
    again = _ack(tree)
    assert (again.exit_code, again.stdout.strip()) == (0, "acknowledged 0 hook failures")


def test_ack_outside_a_managed_repository_exits_one(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert _ack(None).exit_code == 1
    # A workspace without a tree is no managed repository either.
    assert _ack(elsewhere).exit_code == 1
    assert not (elsewhere / ".ea").exists()


def test_acknowledge_refuses_a_naive_instant(tree: Path) -> None:
    with pytest.raises(ValueError):
        acknowledge(tree / ".ea", now=_NOW.replace(tzinfo=None))


def test_acknowledge_replaces_a_malformed_ack(tree: Path) -> None:
    ea = tree / ".ea"
    _write(ea, HEALTH_RECORD, _recent(2, _NOW))
    (ea / HEALTH_ACK).write_text("not json")
    assert acknowledge(ea, now=_NOW) == 2
    assert read_ack(ea) == _NOW


def test_ack_drops_cached_lines_and_keeps_their_sidecars(tree: Path) -> None:
    assert orchestrator.drop_cached_lines() == 0
    cache = tree.parent / "cache"
    cache.mkdir()
    for name in ("a.json", "b.json", "a.runtime-counters.json", "a.levels.json", "c.txt"):
        (cache / name).write_text("{}")

    assert orchestrator.drop_cached_lines() == 2

    assert sorted(path.name for path in cache.iterdir()) == [
        "a.levels.json",
        "a.runtime-counters.json",
        "c.txt",
    ]


def test_dropping_cached_lines_skips_an_entry_it_cannot_remove(tree: Path) -> None:
    # error-path: a directory named like an entry is skipped, the rest still go.
    cache = tree.parent / "cache"
    (cache / "x.json").mkdir(parents=True)
    for name in ("a.json", "z.json"):
        (cache / name).write_text("{}")
    assert orchestrator.drop_cached_lines() == 2
    assert sorted(path.name for path in cache.iterdir()) == ["x.json"]


def test_statusline_repro_ack_crashes_on_an_undroppable_entry(tree: Path) -> None:
    ea = tree / ".ea"
    _write(ea, HEALTH_RECORD, _recent(1, datetime.now(UTC)))
    (tree.parent / "cache" / "x.json").mkdir(parents=True)
    result = _ack(tree)
    assert (result.exit_code, result.stdout.strip()) == (0, "acknowledged 1 hook failures")
    assert read_ack(ea) is not None


# ---- rotation ---------------------------------------------------------------


def _fill(ea: Path, size: int) -> None:
    """Write old rows to the record until it holds exactly *size* bytes."""
    row = f"{_line(_NOW - timedelta(days=3))}\n"
    rows, rest = divmod(size, len(row))
    path = ea / HEALTH_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(row * rows + "\n" * rest, encoding="utf-8")


def test_rotation(tree: Path) -> None:
    ea = tree / ".ea"
    _fill(ea, HEALTH_ROTATE_BYTES + 1)
    older = f"{_line(_NOW - timedelta(days=4))}\n"
    (ea / HEALTH_ROTATED).write_text(older, encoding="utf-8")
    # The session-start warning has read past every row of both files.
    write_cursor(ea, len(older) + HEALTH_ROTATE_BYTES + 1)

    assert acknowledge(ea, now=_NOW) == 0

    # The record became health.1.jsonl, replacing the older one, and the
    # cursor moved back by the bytes dropped: it ends where readers now end.
    assert HEALTH_ROTATED.name == "health.1.jsonl"
    assert not (ea / HEALTH_RECORD).exists()
    assert (ea / HEALTH_ROTATED).stat().st_size == HEALTH_ROTATE_BYTES + 1
    assert read_cursor(ea) == HEALTH_ROTATE_BYTES + 1
    assert (ea / HEALTH_ACK).is_file()


def test_rotation_clamps_a_cursor_past_what_readers_see(tree: Path) -> None:
    ea = tree / ".ea"
    _fill(ea, HEALTH_ROTATE_BYTES + 1)
    write_cursor(ea, 5 * HEALTH_ROTATE_BYTES)

    acknowledge(ea, now=_NOW)

    assert read_cursor(ea) == HEALTH_ROTATE_BYTES + 1


def test_rotation_leaves_a_record_of_exactly_the_limit(tree: Path) -> None:
    ea = tree / ".ea"
    _fill(ea, HEALTH_ROTATE_BYTES)
    write_cursor(ea, 10)

    acknowledge(ea, now=_NOW)

    assert (ea / HEALTH_RECORD).stat().st_size == HEALTH_ROTATE_BYTES
    assert not (ea / HEALTH_ROTATED).exists()
    assert read_cursor(ea) == 10


def test_ack_without_a_record_writes_only_the_ack(tree: Path) -> None:
    ea = tree / ".ea"
    assert acknowledge(ea, now=_NOW) == 0
    assert read_ack(ea) == _NOW
    assert not (ea / HEALTH_RECORD).exists()
    assert not (ea / HEALTH_ROTATED).exists()
