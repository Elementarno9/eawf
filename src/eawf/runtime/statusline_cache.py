"""Stale-while-revalidate file cache for the rendered statusline (B018 W40).

Claude Code asks for the statusline on a hot path (every prompt redraw), but
the full render pipeline walks every module and shells out to git, so a cold
render is too slow to block the redraw on. The stale-while-revalidate (SWR)
contract fixes that: a cached line is served *immediately* -- even once it
has gone stale -- and the caller kicks off a background refresh when the
entry is past its time-to-live (TTL). The next redraw then picks up the
freshly-written line.

This module owns only the cache read/write + staleness decision; it carries
no rendering and no threading. The orchestrator threads the render callable
and decides how to run the refresh (background worker / next-tick prewarm).
The clock is injectable so the TTL boundary is deterministic under test.

Public surface:

- :class:`CacheEntry` -- the cached value plus its write timestamp.
- :class:`CacheLookup` -- the SWR read result (value, present, stale).
- :class:`StatuslineCache` -- the file-backed SWR cache.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS: float = 5.0
"""Default freshness window: a line older than this is served stale and a
refresh is signalled. Tuned for the redraw cadence, not a hard expiry --
SWR never evicts, it only flags staleness."""

_VALUE_KEY: str = "value"
_WRITTEN_AT_KEY: str = "written_at"


@dataclass(frozen=True)
class CacheEntry:
    """One cached statusline value plus the wall-clock time it was written.

    Attributes:
        value: The cached statusline line.
        written_at: Unix timestamp (seconds) of the write, used to compute
            staleness against the cache TTL.
    """

    value: str
    written_at: float


@dataclass(frozen=True)
class CacheLookup:
    """Result of an SWR cache read.

    Attributes:
        value: The cached line, or ``None`` on a miss.
        present: ``True`` when a cache entry was found (even if stale).
        stale: ``True`` when the entry is older than the TTL and the caller
            should trigger a background refresh. Always ``False`` on a miss.
    """

    value: str | None
    present: bool
    stale: bool


class StatuslineCache:
    """File-backed stale-while-revalidate cache for the rendered statusline.

    A cache entry is a small JSON file at *path* carrying the line and its
    write timestamp. :meth:`get` returns the cached line immediately and
    flags staleness; :meth:`serve` wraps the SWR loop -- serve the cached
    line, refresh in the background when stale, render synchronously only on
    a true miss.
    """

    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialise the cache.

        Args:
            path: JSON file backing this cache entry.
            ttl_seconds: Freshness window in seconds (> 0). An entry older
                than this is served stale and flags a refresh.
            clock: Zero-arg wall-clock source returning Unix seconds.
                Injectable so the TTL boundary is deterministic under test.

        Raises:
            ValueError: When *ttl_seconds* is not positive.
        """
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive: {ttl_seconds!r}")
        self._path = path
        self._ttl_seconds = ttl_seconds
        self._clock = clock

    def read_entry(self) -> CacheEntry | None:
        """Return the stored :class:`CacheEntry`, or ``None`` on any miss.

        A miss is any of: the file does not exist, is empty, fails JSON
        decode, is not a mapping, or lacks a string ``value`` / numeric
        ``written_at``. A corrupt cache is a miss, never a raise -- the
        statusline must degrade, not crash.
        """
        if not self._path.exists():
            return None
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            logger.debug(f"read_entry cache-read-failed path={self._path!r} error={exc}")
            return None
        if not raw.strip():
            return None
        try:
            decoded: Any = orjson.loads(raw)
        except orjson.JSONDecodeError as exc:
            logger.debug(f"read_entry cache-json-decode-failed path={self._path!r} error={exc}")
            return None
        if not isinstance(decoded, dict):
            return None
        value = decoded.get(_VALUE_KEY)
        written_at = decoded.get(_WRITTEN_AT_KEY)
        if not isinstance(value, str) or not isinstance(written_at, (int, float)):
            return None
        return CacheEntry(value=value, written_at=float(written_at))

    def write(self, value: str) -> None:
        """Write *value* to the cache, stamping it with the current clock.

        Best-effort: a write failure is logged at debug and swallowed so a
        read-only cache directory degrades to an always-cold render rather
        than crashing the statusline.
        """
        entry = {_VALUE_KEY: value, _WRITTEN_AT_KEY: self._clock()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_bytes(orjson.dumps(entry))
        except OSError as exc:
            logger.debug(f"write cache-write-failed path={self._path!r} error={exc}")

    def is_stale(self, entry: CacheEntry) -> bool:
        """Return ``True`` when *entry* is older than the TTL.

        The boundary is inclusive of the TTL itself: an entry whose age
        equals ``ttl_seconds`` exactly is still fresh; staleness begins the
        instant the age exceeds the TTL.
        """
        age = self._clock() - entry.written_at
        return age > self._ttl_seconds

    def get(self) -> CacheLookup:
        """Read the cache and classify it for the SWR loop.

        Returns:
            A :class:`CacheLookup`: a miss yields ``(None, False, False)``;
            a fresh hit yields ``(value, True, False)``; a stale hit yields
            ``(value, True, True)`` -- the value is still served, but the
            caller should trigger a background refresh.
        """
        entry = self.read_entry()
        if entry is None:
            return CacheLookup(value=None, present=False, stale=False)
        return CacheLookup(value=entry.value, present=True, stale=self.is_stale(entry))

    def serve(
        self,
        render: Callable[[], str],
        *,
        refresh: Callable[[str], None] | None = None,
    ) -> str:
        """Serve the cached line under the SWR contract.

        On a fresh hit the cached line is returned with no render. On a stale
        hit the cached line is returned immediately and, when *refresh* is
        supplied, the caller is handed the freshly-rendered line to persist
        in the background (this method does not spawn threads). On a true
        miss the line is rendered synchronously, written, and returned.

        Args:
            render: Zero-arg callable producing the statusline line.
            refresh: Optional sink invoked with the freshly-rendered line on
                a stale hit so the caller can write it off the hot path. When
                ``None``, a stale hit serves the cached line and skips the
                refresh (the next miss re-renders).

        Returns:
            The line to display now -- cached on a hit, freshly rendered on a
            miss.
        """
        lookup = self.get()
        if lookup.present and lookup.value is not None:
            if lookup.stale and refresh is not None:
                refresh(render())
            return lookup.value
        fresh = render()
        self.write(fresh)
        return fresh


__all__ = [
    "CacheEntry",
    "CacheLookup",
    "StatuslineCache",
]
