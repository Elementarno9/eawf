"""Tests for the stale-while-revalidate statusline cache (P29-I13-W40).

Pins the SWR contract: a fresh hit serves the cached line with no render, a
stale hit serves the cached line immediately AND hands a freshly-rendered
line to the refresh sink, and a true miss renders synchronously then writes.
Covers the inclusive TTL boundary, a corrupt-cache miss, and the
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

    assert cache.serve(_render) == "cached"
    assert calls == []  # a fresh hit never renders


def test_serve_miss_renders_and_writes(tmp_path: Path) -> None:
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    assert cache.serve(lambda: "rendered line") == "rendered line"
    # The cold render is persisted for the next reader.
    assert cache.read_entry() == CacheEntry(value="rendered line", written_at=100.0)


def test_serve_stale_hit_serves_cached_and_signals_refresh(tmp_path: Path) -> None:
    # SWR core: a stale hit serves the cached line immediately and hands the
    # freshly-rendered line to the refresh sink for off-hot-path persistence.
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    cache.write("old line")
    clock.now = 110.0
    refreshed: list[str] = []

    served = cache.serve(lambda: "new line", refresh=refreshed.append)

    assert served == "old line"  # cached value served immediately
    assert refreshed == ["new line"]  # fresh render handed to the sink


def test_serve_stale_hit_without_refresh_serves_cached_only(tmp_path: Path) -> None:
    # boundary: with no refresh sink, a stale hit still serves the cached
    # line and does not render.
    clock = _Clock(start=100.0)
    cache = _cache(tmp_path, clock, ttl=5.0)
    cache.write("old line")
    clock.now = 110.0
    calls: list[str] = []

    def _render() -> str:
        calls.append("rendered")
        return "new"

    assert cache.serve(_render) == "old line"
    assert calls == []
