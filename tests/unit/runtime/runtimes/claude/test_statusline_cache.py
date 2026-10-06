"""Tests for the stale-while-revalidate statusline cache.

Pins the SWR contract: a fresh hit serves the cached line with no render, a
stale hit serves the cached line immediately, re-stamps it AND triggers a
background refresh, and a true miss renders synchronously then writes
atomically. Covers the inclusive TTL boundary, a corrupt-cache miss, and the
non-positive-TTL constructor error path. The clock is injected so the
staleness boundary is deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.statusline_cache import CacheEntry, CacheLookup, StatuslineCache


class _Clock:
    """Mutable injectable clock so tests step time deterministically."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _cache(tmp_path: Path, clock: _Clock, *, ttl: float = 5.0) -> StatuslineCache:
    return StatuslineCache(tmp_path / "statusline.json", ttl_seconds=ttl, clock=clock)


# --- constructor -------------------------------------------------------------


def test_ttl_must_be_positive(tmp_path: Path) -> None:
    # error-path: a zero / negative TTL has no meaningful freshness window.
    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        StatuslineCache(tmp_path / "c.json", ttl_seconds=0)
    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        StatuslineCache(tmp_path / "c.json", ttl_seconds=-1.0)


# --- read / write round-trip -------------------------------------------------


def test_write_then_read_round_trips_value_and_timestamp(tmp_path: Path) -> None:
    clock = _Clock(start=1234.0)
    cache = _cache(tmp_path, clock)
    cache.write("line one")
    entry = cache.read_entry()
    assert entry == CacheEntry(value="line one", written_at=1234.0)


def test_read_entry_miss_when_file_absent(tmp_path: Path) -> None:
    # boundary: no file yet -> miss.
    cache = _cache(tmp_path, _Clock())
    assert cache.read_entry() is None


def test_read_entry_miss_on_corrupt_json(tmp_path: Path) -> None:
    # error-path: a corrupt cache file is a miss, never a raise.
    path = tmp_path / "statusline.json"
    path.write_bytes(b"{ not json")
    cache = StatuslineCache(path, clock=_Clock())
    assert cache.read_entry() is None


def test_read_entry_miss_on_missing_value_field(tmp_path: Path) -> None:
    # error-path: a mapping without a string value field is a miss.
    path = tmp_path / "statusline.json"
    path.write_bytes(b'{"written_at": 1.0}')
    cache = StatuslineCache(path, clock=_Clock())
    assert cache.read_entry() is None


# --- staleness boundary ------------------------------------------------------


def test_entry_is_fresh_at_exactly_ttl(tmp_path: Path) -> None:
    # boundary: age == TTL is still fresh (staleness begins past the TTL).
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    cache.write("line")
    clock.now = 105.0  # age == ttl
    entry = cache.read_entry()
    assert entry is not None
    assert cache.is_stale(entry) is False


def test_entry_is_stale_just_past_ttl(tmp_path: Path) -> None:
    # boundary: age just past the TTL flips to stale.
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    cache.write("line")
    clock.now = 105.001
    entry = cache.read_entry()
    assert entry is not None
    assert cache.is_stale(entry) is True


# --- get classification ------------------------------------------------------


def test_get_returns_miss_when_empty(tmp_path: Path) -> None:
    cache = _cache(tmp_path, _Clock())
    assert cache.get() == CacheLookup(value=None, present=False, stale=False)


def test_get_returns_fresh_hit(tmp_path: Path) -> None:
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    cache.write("fresh line")
    clock.now = 102.0
    assert cache.get() == CacheLookup(value="fresh line", present=True, stale=False)


def test_get_returns_stale_hit(tmp_path: Path) -> None:
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    cache.write("stale line")
    clock.now = 110.0
    assert cache.get() == CacheLookup(value="stale line", present=True, stale=True)


# --- atomic write ------------------------------------------------------------


def test_write_leaves_only_the_entry_behind(tmp_path: Path) -> None:
    # The write goes through a temporary sibling that is renamed away.
    cache = _cache(tmp_path, _Clock())
    cache.write("one")
    cache.write("two")
    assert [path.name for path in tmp_path.iterdir()] == ["statusline.json"]
    assert cache.read_entry() == CacheEntry(value="two", written_at=1000.0)


def test_write_creates_a_missing_cache_directory(tmp_path: Path) -> None:
    cache = StatuslineCache(tmp_path / "a" / "b" / "c.json", clock=_Clock())
    cache.write("line")
    assert cache.read_entry() == CacheEntry(value="line", written_at=1000.0)


def test_failed_rename_keeps_the_old_entry_and_drops_the_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # error-path: a reader must never see a torn entry, and a failed write
    # must not leave its temporary file in the cache directory.
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock)
    cache.write("old")

    def _refuse(_src: object, _dst: object) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr("eawf.runtime.statusline_cache.os.replace", _refuse)
    clock.now = 200.0
    cache.write("new")

    assert cache.read_entry() == CacheEntry(value="old", written_at=100.0)
    assert [path.name for path in tmp_path.iterdir()] == ["statusline.json"]


def test_write_into_an_unwritable_location_is_swallowed(tmp_path: Path) -> None:
    # error-path: the cache root is a file, so mkdir fails; the write degrades.
    (tmp_path / "blocked").write_text("not a directory")
    cache = StatuslineCache(tmp_path / "blocked" / "c.json", clock=_Clock())
    cache.write("line")
    assert cache.read_entry() is None


def test_write_round_trips_a_long_line(tmp_path: Path) -> None:
    # boundary: a line far past any terminal width still round-trips intact.
    cache = _cache(tmp_path, _Clock())
    line = "seg:" + "x" * 100_000
    cache.write(line)
    entry = cache.read_entry()
    assert entry is not None
    assert entry.value == line


# --- serve_cached (hit-only SWR) ---------------------------------------------


class _Refresh:
    """Counts refresh triggers."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def test_serve_cached_miss_returns_none_without_refresh(tmp_path: Path) -> None:
    refresh = _Refresh()
    assert _cache(tmp_path, _Clock()).serve_cached(refresh) is None
    assert refresh.calls == 0


def test_serve_cached_fresh_hit_does_not_refresh(tmp_path: Path) -> None:
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    cache.write("cached")
    clock.now = 105.0  # boundary: age == TTL is still fresh
    refresh = _Refresh()
    assert cache.serve_cached(refresh) == "cached"
    assert refresh.calls == 0


def test_entry_past_ttl_triggers_one_refresh(tmp_path: Path) -> None:
    # Injected clock: an entry just past its TTL is served and refreshed.
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    cache.write("old line")
    clock.now = 105.001
    refresh = _Refresh()

    assert cache.serve_cached(refresh) == "old line"
    assert refresh.calls == 1
    # The stale entry is re-stamped, so a redraw during the refresh does not
    # start a second one.
    assert cache.read_entry() == CacheEntry(value="old line", written_at=105.001)
    clock.now = 106.0
    assert cache.serve_cached(refresh) == "old line"
    assert refresh.calls == 1


def test_refresh_retries_after_another_ttl_when_the_worker_never_writes(
    tmp_path: Path,
) -> None:
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    cache.write("old line")
    refresh = _Refresh()
    clock.now = 110.0
    cache.serve_cached(refresh)
    clock.now = 115.5
    cache.serve_cached(refresh)
    assert refresh.calls == 2


def test_serve_cached_treats_a_retired_format_entry_as_a_miss(tmp_path: Path) -> None:
    # error-path: an entry in the retired per-session format is a miss.
    path = tmp_path / "statusline.json"
    path.write_bytes(b'{"session_id": "s", "line": "old format"}')
    refresh = _Refresh()
    assert StatuslineCache(path, clock=_Clock()).serve_cached(refresh) is None
    assert refresh.calls == 0


# --- serve (SWR loop) --------------------------------------------------------


def test_serve_fresh_hit_does_not_render(tmp_path: Path) -> None:
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    cache.write("cached")
    clock.now = 101.0
    calls: list[str] = []

    def _render() -> str:
        calls.append("rendered")
        return "fresh"

    assert cache.serve(_render, refresh=_Refresh()) == "cached"
    assert calls == []  # a fresh hit never renders


def test_serve_miss_renders_and_writes(tmp_path: Path) -> None:
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    refresh = _Refresh()
    assert cache.serve(lambda: "rendered line", refresh=refresh) == "rendered line"
    # The cold render is persisted for the next reader.
    assert cache.read_entry() == CacheEntry(value="rendered line", written_at=100.0)
    assert refresh.calls == 0


def test_serve_stale_hit_serves_cached_and_triggers_refresh_without_rendering(
    tmp_path: Path,
) -> None:
    # SWR core: a stale hit serves the cached line immediately and leaves the
    # render to the background refresh.
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    cache.write("old line")
    clock.now = 110.0
    calls: list[str] = []
    refresh = _Refresh()

    def _render() -> str:
        calls.append("rendered")
        return "new line"

    assert cache.serve(_render, refresh=refresh) == "old line"
    assert refresh.calls == 1
    assert calls == []


def test_serve_propagates_a_render_error_on_a_miss(tmp_path: Path) -> None:
    # error-path: the cache does not hide a render failure, and writes nothing.
    cache = _cache(tmp_path, _Clock())

    def _render() -> str:
        raise RuntimeError("render failed")

    with pytest.raises(RuntimeError, match="render failed"):
        cache.serve(_render, refresh=_Refresh())
    assert cache.read_entry() is None
