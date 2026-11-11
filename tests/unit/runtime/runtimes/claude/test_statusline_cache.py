"""Tests for the statusline cache.

Pins the entry round trip, the atomic write, the hit and miss of
:meth:`StatuslineCache.get`, the payload stamp that keeps an entry answering
a later payload, and a corrupt-cache miss.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.statusline_cache import CacheEntry, StatuslineCache


def _cache(tmp_path: Path) -> StatuslineCache:
    return StatuslineCache(tmp_path / "statusline.json")


# --- read / write round-trip -------------------------------------------------


def test_write_then_read_round_trips_value_and_stamp(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    assert cache.write("line one", stamp=1234.0) is True
    assert cache.read_entry() == CacheEntry(value="line one", written_at=1234.0)


def test_read_entry_miss_when_file_absent(tmp_path: Path) -> None:
    # boundary: no file yet -> miss.
    assert _cache(tmp_path).read_entry() is None


def test_read_entry_miss_on_corrupt_json(tmp_path: Path) -> None:
    # error-path: a corrupt cache file is a miss, never a raise.
    path = tmp_path / "statusline.json"
    path.write_bytes(b"{ not json")
    assert StatuslineCache(path).read_entry() is None


def test_read_entry_miss_on_missing_value_field(tmp_path: Path) -> None:
    # error-path: a mapping without a string value field is a miss.
    path = tmp_path / "statusline.json"
    path.write_bytes(b'{"written_at": 1.0}')
    assert StatuslineCache(path).read_entry() is None


# --- get ---------------------------------------------------------------------


def test_get_returns_none_when_empty(tmp_path: Path) -> None:
    assert _cache(tmp_path).get() is None


def test_get_returns_the_line_whatever_its_age(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.write("old line", stamp=0.0)
    assert cache.get() == "old line"


def test_get_treats_an_entry_under_another_key_as_a_miss(tmp_path: Path) -> None:
    path = tmp_path / "statusline.json"
    StatuslineCache(path, key="a").write("line", stamp=1.0)
    assert StatuslineCache(path, key="b").get() is None
    assert StatuslineCache(path, key="a").get() == "line"


def test_get_treats_a_retired_format_entry_as_a_miss(tmp_path: Path) -> None:
    # error-path: an entry in the retired per-session format is a miss.
    path = tmp_path / "statusline.json"
    path.write_bytes(b'{"session_id": "s", "line": "old format"}')
    assert StatuslineCache(path).get() is None


# --- payload stamp -----------------------------------------------------------


def test_statusline_repro_older_render_overwrites_newer_entry(tmp_path: Path) -> None:
    # A slow background render answering an older payload keeps its hands off.
    cache = _cache(tmp_path)
    cache.write("newer", stamp=200.0)
    assert cache.write("older", stamp=100.0) is False
    assert cache.read_entry() == CacheEntry(value="newer", written_at=200.0)


@pytest.mark.parametrize("stamp", [200.0, 200.001])
def test_an_equal_or_later_stamp_replaces_the_entry(tmp_path: Path, stamp: float) -> None:
    # boundary: the same instant replaces, as a redraw re-rendering one payload does.
    cache = _cache(tmp_path)
    cache.write("first", stamp=200.0)
    assert cache.write("second", stamp=stamp) is True
    assert cache.get() == "second"


# --- atomic write ------------------------------------------------------------


def test_write_leaves_only_the_entry_behind(tmp_path: Path) -> None:
    # The write goes through a temporary sibling that is renamed away.
    cache = _cache(tmp_path)
    cache.write("one", stamp=1000.0)
    cache.write("two", stamp=1000.0)
    assert [path.name for path in tmp_path.iterdir()] == ["statusline.json"]
    assert cache.read_entry() == CacheEntry(value="two", written_at=1000.0)


def test_write_creates_a_missing_cache_directory(tmp_path: Path) -> None:
    cache = StatuslineCache(tmp_path / "a" / "b" / "c.json")
    cache.write("line", stamp=1000.0)
    assert cache.read_entry() == CacheEntry(value="line", written_at=1000.0)


def test_failed_rename_keeps_the_old_entry_and_drops_the_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # error-path: a reader must never see a torn entry, and a failed write
    # must not leave its temporary file in the cache directory.
    cache = _cache(tmp_path)
    cache.write("old", stamp=100.0)

    def _refuse(_src: object, _dst: object) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr("eawf.runtime.statusline_cache.os.replace", _refuse)
    assert cache.write("new", stamp=200.0) is False

    assert cache.read_entry() == CacheEntry(value="old", written_at=100.0)
    assert [path.name for path in tmp_path.iterdir()] == ["statusline.json"]


def test_write_into_an_unwritable_location_is_swallowed(tmp_path: Path) -> None:
    # error-path: the cache root is a file, so mkdir fails; the write degrades.
    (tmp_path / "blocked").write_text("not a directory")
    cache = StatuslineCache(tmp_path / "blocked" / "c.json")
    assert cache.write("line", stamp=1.0) is False
    assert cache.read_entry() is None


def test_write_round_trips_a_long_line(tmp_path: Path) -> None:
    # boundary: a line far past any terminal width still round-trips intact.
    cache = _cache(tmp_path)
    line = "seg:" + "x" * 100_000
    cache.write(line, stamp=1.0)
    assert cache.get() == line
