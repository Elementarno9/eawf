"""Stale-while-revalidate file cache for the rendered statusline.

Claude Code asks for the statusline on a hot path (every prompt redraw), but
the full render pipeline walks every module and shells out to git, so a cold
render is too slow to block the redraw on. The stale-while-revalidate (SWR)
contract fixes that: a cached line is served *immediately* -- even once it
has gone stale -- and the caller kicks off a background refresh when the
entry is past its time-to-live (TTL). The next redraw then picks up the
freshly-written line.

This module owns only the cache read/write + staleness decision; it carries
no rendering and no process management. The orchestrator supplies the render
callable and the refresh trigger (a detached background render). Writes are
atomic (temporary file + rename) because the refresh worker writes while
other redraws read. The clock is injectable so the TTL boundary is
deterministic under test.

Public surface:

- :class:`CacheEntry` -- the cached value plus its write timestamp.
- :class:`CacheLookup` -- the SWR read result (value, present, stale).
- :class:`StatuslineCache` -- the file-backed SWR cache.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson

if TYPE_CHECKING:
    import logging


def _logger() -> logging.Logger:
    """Return this module's logger, importing :mod:`logging` on first use.

    ``logging`` costs about a third of a cached statusline redraw, and a
    cache hit logs nothing, so the import waits for the first log line.
    """
    import logging

    return logging.getLogger(__name__)


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
            _logger().debug(f"read_entry cache-read-failed path={self._path!r} error={exc}")
            return None
        if not raw.strip():
            return None
        try:
            decoded: Any = orjson.loads(raw)
        except orjson.JSONDecodeError as exc:
            _logger().debug(f"read_entry cache-json-decode-failed path={self._path!r} error={exc}")
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

        The bytes land in a sibling temporary file that is then renamed over
        the entry, so a concurrent reader sees either the old entry or the
        new one, never a torn write. The temporary name carries the process
        id so two writers never share one.

        Best-effort: a write failure is logged at debug and swallowed (and
        the temporary file removed) so a read-only cache directory degrades
        to an always-cold render rather than crashing the statusline.
        """
        entry = {_VALUE_KEY: value, _WRITTEN_AT_KEY: self._clock()}
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(orjson.dumps(entry))
            os.replace(tmp, self._path)
        except OSError as exc:
            _logger().debug(f"write cache-write-failed path={self._path!r} error={exc}")
            # The temporary may never have been created (missing or blocked
            # directory), so its removal fails the same way the write did.
            with contextlib.suppress(OSError):
                tmp.unlink()

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

    def serve_cached(self, refresh: Callable[[], None]) -> str | None:
        """Serve a cache hit without rendering; ``None`` on a miss.

        A fresh hit returns the cached line. A stale hit re-stamps the
        entry, calls *refresh* and returns the cached line: the re-stamp
        makes the redraws that arrive while the refresh runs read a fresh
        entry, so they do not each start another refresh. Should the
        refresh die, the entry goes stale again after one TTL and the next
        redraw retries.

        Args:
            refresh: Zero-arg trigger that starts a background render which
                writes the new line to this cache. It must not block on the
                render.

        Returns:
            The cached line, or ``None`` when there is no usable entry and
            the caller has to render.
        """
        lookup = self.get()
        if lookup.value is None:
            return None
        if lookup.stale:
            self.write(lookup.value)
            refresh()
        return lookup.value

    def serve(self, render: Callable[[], str], *, refresh: Callable[[], None]) -> str:
        """Serve the line under the SWR contract, rendering only on a miss.

        A hit is handled by :meth:`serve_cached`; on a true miss the line is
        rendered synchronously, written, and returned.

        Args:
            render: Zero-arg callable producing the statusline line.
            refresh: Background refresh trigger, see :meth:`serve_cached`.

        Returns:
            The line to display now -- cached on a hit, freshly rendered on a
            miss.
        """
        cached = self.serve_cached(refresh)
        if cached is not None:
            return cached
        fresh = render()
        self.write(fresh)
        return fresh


__all__ = [
    "CacheEntry",
    "CacheLookup",
    "StatuslineCache",
]
