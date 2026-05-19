"""Daemon-resident spec cache (authority-map row 10).

Per C03 §5.8 the daemon caches a per-phase spec index so
``eawf spec show <urn>`` can recover spec bodies after the on-disk file
has been ``git rm``'d in the ARCHIVED transition. The cache is owned
by the daemon — no other writer touches the file — so every mutation
flows through :mod:`eawf.spec.writer` (called from
:mod:`eawf.daemon.methods.spec`).

Layout::

    <runtime_dir>/spec-cache/<phase_id>.json

Each phase has its own cache file so the daemon does not load the
full catalogue on every read; tests that exercise the cache can point
``EAWF_SPEC_CACHE_DIR`` at a ``tmp_path``-rooted directory.

The cache models live here (alongside :func:`default_cache_dir` +
:func:`cache_path_for_phase` + :func:`read_phase_cache`) rather than in
the daemon-methods module so the typed shape is importable by tests,
the CLI fallback path, and the spec writer without a daemon dependency
cycle.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.daemon.runtime_dir import runtime_dir as default_runtime_dir
from eawf.state.models import PhaseIdStr

logger = logging.getLogger(__name__)


SpecStatusStr = Literal["DRAFT", "READY", "IMPLEMENTED", "ARCHIVED"]


class _StrictModel(BaseModel):
    """Base model that forbids unknown keys (AGENTS rule 2)."""

    model_config = ConfigDict(extra="forbid")


class SpecCacheEntry(_StrictModel):
    """One spec entry in the daemon-resident cache.

    Mirrors C03 §5.8. ``file_sha`` is the git blob SHA of the spec body
    at the last graduation point; ``file_path`` stays repo-relative.
    """

    spec_urn: str = Field(min_length=1)
    file_sha: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    status: SpecStatusStr
    last_modified: str = Field(min_length=1)
    archived_commit: str | None = None


class SpecCachePhase(_StrictModel):
    """Per-phase spec cache document.

    One file per phase keeps random-access reads cheap; the writer
    rewrites the full document on every mutation (deterministic
    serialisation, atomic-rename).
    """

    schema_version: Literal["1.0"] = "1.0"
    phase_id: PhaseIdStr
    entries: list[SpecCacheEntry] = Field(default_factory=list)


class SpecCacheReadError(RuntimeError):
    """Raised when the per-phase cache file cannot be loaded."""


def default_cache_dir() -> Path:
    """Return the daemon-resident spec-cache directory.

    Resolution order:

    1. ``EAWF_SPEC_CACHE_DIR`` env var — operator / test override.
    2. ``<runtime_dir>/spec-cache/`` — co-located with the daemon's
       PID file, socket, and outcome-WAL.

    Returns:
        Path to the spec-cache directory. The caller materialises the
        directory with ``Path.mkdir(parents=True, exist_ok=True)`` on
        first write.
    """
    override = os.environ.get("EAWF_SPEC_CACHE_DIR")
    if override:
        return Path(override)
    return default_runtime_dir() / "spec-cache"


def cache_path_for_phase(phase_id: str, *, cache_dir: Path | None = None) -> Path:
    """Return the cache path for *phase_id*.

    Args:
        phase_id: Phase symbol (e.g. ``P25``). Validated only for
            the file-name suffix; full pattern checks live on the
            Pydantic models that read the cache.
        cache_dir: Optional override; defaults to
            :func:`default_cache_dir`.

    Returns:
        ``<cache_dir>/<phase_id>.json``.
    """
    base = cache_dir if cache_dir is not None else default_cache_dir()
    return base / f"{phase_id}.json"


def read_phase_cache(
    phase_id: str,
    *,
    cache_dir: Path | None = None,
) -> SpecCachePhase:
    """Load the typed cache for *phase_id*.

    Args:
        phase_id: Phase symbol.
        cache_dir: Optional override directory.

    Returns:
        Parsed :class:`SpecCachePhase`. When the file is missing,
        returns an empty cache document for *phase_id*; missing
        cache files are the bootstrap path for the first ``spec
        init`` under a phase.

    Raises:
        SpecCacheReadError: When the file exists but cannot be parsed
            or fails schema validation.
    """
    path = cache_path_for_phase(phase_id, cache_dir=cache_dir)
    if not path.is_file():
        return SpecCachePhase(phase_id=phase_id, entries=[])
    try:
        payload = orjson.loads(path.read_bytes())
    except orjson.JSONDecodeError as exc:
        raise SpecCacheReadError(f"corrupted spec cache at {path}: {exc}") from exc
    except OSError as exc:
        raise SpecCacheReadError(f"cannot read spec cache at {path}: {exc}") from exc
    try:
        return SpecCachePhase.model_validate(payload)
    except ValidationError as exc:
        raise SpecCacheReadError(f"invalid spec cache schema at {path}: {exc}") from exc


def find_cached_entry(
    spec_urn: str,
    *,
    phase_id: str,
    cache_dir: Path | None = None,
) -> SpecCacheEntry | None:
    """Return the cached entry for *spec_urn* under *phase_id*, or ``None``.

    The cache groups by phase so a wave / iter URN lookup only loads
    the parent phase's cache document. Returns ``None`` when the
    phase cache file is missing or has no row for the URN.
    """
    cache = read_phase_cache(phase_id, cache_dir=cache_dir)
    for entry in cache.entries:
        if entry.spec_urn == spec_urn:
            return entry
    return None


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Centralised here so :mod:`eawf.spec.writer` and tests share one
    timestamp format without duplicating the format spec.
    """
    return datetime.now(UTC).isoformat()


__all__ = [
    "SpecCacheEntry",
    "SpecCachePhase",
    "SpecCacheReadError",
    "SpecStatusStr",
    "cache_path_for_phase",
    "default_cache_dir",
    "find_cached_entry",
    "read_phase_cache",
    "utcnow_iso",
]
