"""14-day OR-chain staleness check for ``~/.eawf/registry.json`` entries.

Per C07b brief §5.3 a registry entry is considered stale when ANY of:

- (a) the registry file's mtime is older than :data:`STALE_AFTER`; OR
- (b) the entry's per-repo ``<entry.path>/.ea/state.json`` mtime is
  older than :data:`STALE_AFTER`; OR
- (c) the entry's per-repo ``state.json`` failed to load (missing or
  JSON decode error).

The 14-day threshold tracks the eawf dogfood cadence: a phase lands
roughly every two weeks, so an entry untouched longer than that is
genuinely behind and the TUI strip surfaces a ``(stale)`` chip.

The helpers stay read-only: no caller of :func:`is_stale` ever mutates
the registry. The TUI workspace dashboard, the scope dispatch ladder,
and ``eawf workspace registry-status`` all consume the same boundary
so the stale rules stay consistent across surfaces.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson

from eawf.platform.registry.models import RegistryRepoEntry, default_registry_path

logger = logging.getLogger(__name__)


#: Stale threshold for registry-level mtime + per-repo state.json
#: mtime. Picked at 14 days because the dogfood cadence on EAWF
#: lands a phase every ~2 weeks; longer than that and the entry is
#: genuinely behind. Re-exported via :mod:`eawf.platform.registry` for
#: back-compat with the pre-W08 single-file layout.
STALE_AFTER: timedelta = timedelta(days=14)


def registry_mtime(path: Path | None = None, *, home: Path | None = None) -> datetime | None:
    """Return the filesystem mtime of the registry file as UTC, or ``None``.

    Used by branch (a) of the staleness OR-chain so the workspace
    dashboard can warn the operator when the registry itself has not
    been touched in :data:`STALE_AFTER`. Returns ``None`` when the
    file is absent or the ``stat`` call fails so the caller can fold
    a missing registry into the OR-chain without raising.

    Args:
        path: Explicit registry path. When ``None``, falls back to
            :func:`default_registry_path` so tests can pass a
            ``tmp_path``-rooted location.
        home: Test seam for the default-path branch. Ignored when
            *path* is supplied directly.

    Returns:
        The UTC mtime, or ``None`` when *path* is missing/unreadable.
    """
    resolved = path if path is not None else default_registry_path(home=home)
    if not resolved.is_file():
        return None
    try:
        stat = resolved.stat()
    except OSError as exc:
        logger.debug(f"registry_mtime stat-failed path={resolved!r} error={exc!r}")
        return None
    return datetime.fromtimestamp(stat.st_mtime, tz=UTC)


def repo_state_mtime(repo_path: Path) -> datetime | None:
    """Return the mtime of ``<repo_path>/.ea/state.json`` as UTC.

    Branch (b) of the staleness OR-chain. Treats missing / unreadable
    files as ``None`` so the caller can fold the result into the
    OR-chain without raising; branch (c) picks up the load-failure
    signal separately.

    Args:
        repo_path: The :attr:`RegistryRepoEntry.path` value.

    Returns:
        The UTC mtime of the state file, or ``None`` when the file
        is missing or its ``stat`` call fails.
    """
    candidate = repo_path / ".ea" / "state.json"
    if not candidate.is_file():
        return None
    try:
        stat = candidate.stat()
    except OSError as exc:
        logger.debug(f"repo_state_mtime stat-failed path={candidate!r} error={exc!r}")
        return None
    return datetime.fromtimestamp(stat.st_mtime, tz=UTC)


def read_repo_state(repo_path: Path) -> dict[str, Any] | None:
    """Best-effort load of ``<repo_path>/.ea/state.json`` as a dict.

    Returns ``None`` when the file is missing, unreadable, or fails
    JSON decode (branch (c) of the staleness OR-chain). The TUI
    workspace dashboard consumes the result directly; its layout
    pane builders accept an empty dict gracefully so a broken
    state file still renders a deterministic surface.

    Args:
        repo_path: The :attr:`RegistryRepoEntry.path` value.

    Returns:
        The decoded JSON dict, or ``None`` when the file is missing
        / unreadable / fails JSON decode.
    """
    candidate = repo_path / ".ea" / "state.json"
    if not candidate.is_file():
        return None
    try:
        return orjson.loads(candidate.read_bytes())  # type: ignore[no-any-return]
    except (orjson.JSONDecodeError, OSError) as exc:
        logger.debug(f"read_repo_state unreadable path={candidate!r} error={exc!r}")
        return None


def is_stale(
    entry: RegistryRepoEntry,
    *,
    registry_mtime_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Return ``True`` when *entry* should render the ``(stale)`` chip.

    Combines the three success-criterion-3 signals into one OR-chain:

    - (a) registry mtime older than :data:`STALE_AFTER`.
    - (b) ``<entry.path>/.ea/state.json`` mtime older than
      :data:`STALE_AFTER`.
    - (c) ``<entry.path>/.ea/state.json`` failed to load (missing or
      JSON decode error) — surfaced as a missing state mtime here
      since both branches funnel into the same ``None`` sentinel.

    Boundary semantics (exercised by the test suite):

    - exactly :data:`STALE_AFTER` (14 days): NOT stale — strict
      ``>`` comparison.
    - one second over :data:`STALE_AFTER`: stale.
    - one second under :data:`STALE_AFTER`: not stale.

    Args:
        entry: One :class:`RegistryRepoEntry` from the registry.
        registry_mtime_at: Output of :func:`registry_mtime` for the
            same registry file the entry came from. ``None`` is
            treated as fresh; a missing registry is already an error
            surfaced elsewhere and must not double-fire here.
        now: Override for the "current" timestamp; defaults to
            :func:`datetime.now` (UTC). Tests inject a fixed value
            so freshness comparisons stay deterministic.

    Returns:
        ``True`` when any of the three signals fires; ``False``
        otherwise.
    """
    current = now if now is not None else datetime.now(UTC)
    if registry_mtime_at is not None and (current - registry_mtime_at) > STALE_AFTER:
        return True
    state_mtime = repo_state_mtime(Path(entry.path))
    if state_mtime is None:
        return True
    return (current - state_mtime) > STALE_AFTER


__all__ = [
    "STALE_AFTER",
    "is_stale",
    "read_repo_state",
    "registry_mtime",
    "repo_state_mtime",
]
