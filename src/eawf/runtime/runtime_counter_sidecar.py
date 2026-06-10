"""File-backed sidecar for latest parsed runtime counters.

The rendered statusline cache stores a display string. This sidecar stores the
latest structured :class:`RuntimeCounters` snapshot beside that cache entry so
claim/close code can read fresh cumulative counters without parsing the
rendered line.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1.0"


class _SidecarPayload(BaseModel):
    """Strict on-disk payload for a counter sidecar."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    counters: RuntimeCounters


def sidecar_path_for_statusline_cache(cache_path: Path) -> Path:
    """Return the counter sidecar path beside a rendered statusline cache file."""
    return cache_path.with_name(f"{cache_path.stem}.runtime-counters.json")


class RuntimeCounterSidecar:
    """Read/write latest runtime counters from one sidecar JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        """Filesystem path backing this sidecar."""
        return self._path

    def read(self) -> RuntimeCounters | None:
        """Return stored counters, or ``None`` on miss/corrupt payload."""
        if not self._path.exists():
            return None
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            logger.debug(
                f"read runtime-counter-sidecar-read-failed path={self._path!r} error={exc}"
            )
            return None
        if not raw.strip():
            return None
        try:
            decoded: Any = orjson.loads(raw)
            payload = _SidecarPayload.model_validate(decoded)
        except (orjson.JSONDecodeError, ValidationError) as exc:
            logger.debug(f"read runtime-counter-sidecar-json-miss path={self._path!r} error={exc}")
            return None
        if payload.schema_version != _SCHEMA_VERSION:
            return None
        return payload.counters

    def write(self, counters: RuntimeCounters) -> None:
        """Persist *counters* best-effort; write failures are degraded."""
        payload = _SidecarPayload(schema_version=_SCHEMA_VERSION, counters=counters)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_bytes(orjson.dumps(payload.model_dump(mode="json")))
        except OSError as exc:
            logger.debug(
                f"write runtime-counter-sidecar-write-failed path={self._path!r} error={exc}"
            )


__all__ = [
    "RuntimeCounterSidecar",
    "sidecar_path_for_statusline_cache",
]
