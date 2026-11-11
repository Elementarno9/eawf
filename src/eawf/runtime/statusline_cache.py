"""File cache of the last rendered statusline, one entry per session.

Every redraw renders synchronously, but a render can raise or overrun its
budget; the cached line is what the redraw prints then, while a background
render rewrites the entry. This module owns only the cache read/write; it
carries no rendering and no process management. Each entry records the key it
was rendered under, and an entry written under another key is a miss: the key
carries what changes the line without changing the payload, such as the
resolved palette, so a theme switch never serves a line painted in the old
colours. Each entry also records when the payload it answers was received, and
a write never replaces an entry answering a later payload, so a slow
background render cannot put an older line back over a newer one. Writes are
atomic (temporary file + rename) because the refresh worker writes while
other redraws read.

Public surface:

- :class:`CacheEntry` -- the cached value plus its payload stamp.
- :class:`StatuslineCache` -- the file-backed cache.
"""

from __future__ import annotations

import contextlib
import os
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


_VALUE_KEY: str = "value"
_WRITTEN_AT_KEY: str = "written_at"
_RENDER_KEY: str = "key"


@dataclass(frozen=True)
class CacheEntry:
    """One cached statusline value plus the payload stamp it answers.

    Attributes:
        value: The cached statusline line.
        written_at: Unix timestamp (seconds) at which the redraw that asked
            for this line received its payload.
        key: The render key the value was written under; ``""`` for an
            entry that recorded none.
    """

    value: str
    written_at: float
    key: str = ""


class StatuslineCache:
    """File-backed cache of the last rendered statusline.

    A cache entry is a small JSON file at *path* carrying the line, its
    payload stamp and its render key.
    """

    def __init__(self, path: Path, *, key: str = "") -> None:
        """Initialise the cache.

        Args:
            path: JSON file backing this cache entry.
            key: The render key this cache serves; an entry written under
                another key reads as a miss.
        """
        self._path = path
        self._key = key

    def read_entry(self) -> CacheEntry | None:
        """Return the stored :class:`CacheEntry`, or ``None`` on any miss.

        A miss is any of: the file does not exist, is empty, fails JSON
        decode, is not a mapping, lacks a string ``value`` / numeric
        ``written_at``, or carries a non-string ``key``. A corrupt cache is
        a miss, never a raise -- the statusline must degrade, not crash.
        The entry is returned whatever its key; :meth:`get` compares it.
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
        key = decoded.get(_RENDER_KEY, "")
        if (
            not isinstance(value, str)
            or not isinstance(written_at, (int, float))
            or not isinstance(key, str)
        ):
            return None
        return CacheEntry(value=value, written_at=float(written_at), key=key)

    def write(self, value: str, *, stamp: float) -> bool:
        """Write *value* under this cache's key unless the entry answers a later payload.

        The bytes land in a sibling temporary file that is then renamed over
        the entry, so a concurrent reader sees either the old entry or the
        new one, never a torn write. The temporary name carries the process
        id so two writers never share one. The check against the stored
        stamp and the rename are two steps, so two writers finishing at the
        same instant may still land out of order; the next redraw corrects it.

        Best-effort: a write failure is logged at debug and swallowed (and
        the temporary file removed) so a read-only cache directory degrades
        to an always-cold render rather than crashing the statusline.

        Args:
            value: The rendered line.
            stamp: Unix seconds at which the redraw asking for *value*
                received its payload; an entry stamped later is kept.

        Returns:
            ``True`` when the entry now holds *value*; ``False`` when a later
            entry was kept or the write failed.
        """
        current = self.read_entry()
        if current is not None and current.written_at > stamp:
            return False
        entry = {_VALUE_KEY: value, _WRITTEN_AT_KEY: stamp, _RENDER_KEY: self._key}
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
            return False
        return True

    def get(self) -> str | None:
        """Return the cached line, or ``None`` on a miss.

        An entry written under another key is a miss.
        """
        entry = self.read_entry()
        if entry is None or entry.key != self._key:
            return None
        return entry.value


__all__ = [
    "CacheEntry",
    "StatuslineCache",
]
