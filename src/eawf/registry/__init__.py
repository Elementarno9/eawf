"""Read-only registry helpers for ``~/.eawf/registry.json`` (P20-I01-W05).

The registry is a user-scope index of repos the operator has explicitly
initialised or registered. Per the project memory note
``feedback_explicit_registry_only`` it grows ONLY through explicit
``eawf init`` / ``eawf workspace add-repo`` writes — there is no scan,
walk, or import-from-discovery path. This module deliberately ships a
*read-only* surface: callers can :func:`read_registry`, inspect entries,
and compute staleness, but no helper here mutates the file.

Registry shape on disk::

    {
      "version": "1",
      "updated_at": "2026-05-01T12:34:56+00:00",
      "active_code": "EAWF",
      "repos": {
        "EAWF": {"code": "EAWF", "path": "/repos/eawf", "title": "Eä"},
        "DEMO": {"code": "DEMO", "path": "/repos/demo", "title": "Demo"}
      }
    }

Models use Pydantic v2 with ``ConfigDict(extra="forbid")`` so a typo or
schema drift fails at validate time rather than silently dropping fields
(AGENTS rule 2). The TUI workspace dashboard (W05) and the
``eawf workspace registry-status`` / ``registry-list`` CLI subcommands
both consume the same :class:`Registry` surface so the staleness rules
stay consistent between the headless and TTY entry points.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


#: Stale threshold for registry-level mtime + per-repo state.json mtime
#: (success criterion 3, (a) and (b)). Picked at 14 days because the
#: dogfood cadence on EAWF lands a phase every ~2 weeks; longer than
#: that and the entry is genuinely behind.
STALE_AFTER: timedelta = timedelta(days=14)


def default_registry_path(*, home: Path | None = None) -> Path:
    """Return the canonical ``~/.eawf/registry.json`` path.

    The ``home`` kwarg is the test seam — pass a ``tmp_path`` root so
    unit/integration tests never touch the operator's real registry.
    """
    base = home if home is not None else Path.home()
    return base / ".eawf" / "registry.json"


class RegistryRepoEntry(BaseModel):
    """One entry under :attr:`Registry.repos`.

    The shape is intentionally narrow — code + on-disk path + optional
    human-readable title — so the registry stays a *pointer index*
    rather than a denormalised copy of the per-repo ``state.json``.
    The TUI staleness logic re-reads ``state.json`` at render time so
    drift between the two surfaces never persists.

    Attributes:
        code: Project-code-shape repo identifier (``[A-Z][A-Z0-9_-]+``).
        path: Absolute on-disk path to the repo's working tree.
        title: Optional human-readable title; falls back to ``code``
            for display when absent.
        last_seen: Optional timestamp of the last explicit
            init/add-repo touch (informational only — stale
            computation uses the registry file's mtime instead).
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    path: str
    title: str | None = None
    last_seen: datetime | None = None


class Registry(BaseModel):
    """Read-only view over ``~/.eawf/registry.json``.

    The ``version`` field exists so future schema bumps stay
    forward-compatible; today only ``"1"`` is accepted. Callers
    must not mutate the model — the canonical write path lives
    elsewhere (explicit ``init`` / ``add-repo``) and the constructor
    contract here is *load + inspect only*.

    Attributes:
        version: Schema version string (currently ``"1"``).
        updated_at: Registry-level last-touched timestamp. Distinct
            from the file's filesystem mtime so a stale-detect
            fallback exists when the filesystem timestamp drifts.
        active_code: Optional code marking the "active" repo for
            the workspace dashboard's quadrant body.
        repos: Mapping of project-code to :class:`RegistryRepoEntry`.
    """

    model_config = ConfigDict(extra="forbid")

    version: str = "1"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    active_code: str | None = None
    repos: dict[str, RegistryRepoEntry] = Field(default_factory=dict)


class RegistryReadError(Exception):
    """Raised when ``read_registry`` cannot return a typed :class:`Registry`.

    The TUI strip catches this and surfaces an empty-strip placeholder
    so a missing or corrupted registry still renders a deterministic
    workspace view.
    """


def read_registry(path: Path | None = None, *, home: Path | None = None) -> Registry:
    """Load and validate ``~/.eawf/registry.json`` into a typed :class:`Registry`.

    Strictly read-only. Never writes, never grows the registry, never
    triggers a scan — per ``feedback_explicit_registry_only`` the
    registry expands only via explicit ``init`` / ``add-repo``.

    Args:
        path: Explicit registry path. When ``None``, falls back to
            :func:`default_registry_path` so tests can pass a
            ``tmp_path``-rooted location without monkeypatching
            ``Path.home``.
        home: Test seam for the default-path branch. Ignored when
            ``path`` is supplied directly.

    Returns:
        The validated :class:`Registry` document.

    Raises:
        RegistryReadError: When the file is missing, unreadable, or
            fails schema validation. The exception message names the
            failure mode so callers can route on it.
    """
    resolved = path if path is not None else default_registry_path(home=home)
    logger.debug(f"read_registry path={resolved!r}")
    if not resolved.is_file():
        raise RegistryReadError(f"registry file not found: {resolved}")
    try:
        payload: dict[str, Any] = orjson.loads(resolved.read_bytes())
    except orjson.JSONDecodeError as exc:
        raise RegistryReadError(f"corrupted registry at {resolved}: {exc}") from exc
    except OSError as exc:
        raise RegistryReadError(f"cannot read registry at {resolved}: {exc}") from exc
    try:
        return Registry.model_validate(payload)
    except ValidationError as exc:
        raise RegistryReadError(f"invalid registry schema at {resolved}: {exc}") from exc


def registry_mtime(path: Path | None = None, *, home: Path | None = None) -> datetime | None:
    """Return the filesystem mtime of the registry file as UTC, or ``None``.

    Used by the stale-chip rule (success criterion 3, branch (a)) so the
    workspace dashboard can warn the operator when the registry hasn't
    been touched in :data:`STALE_AFTER`.
    """
    resolved = path if path is not None else default_registry_path(home=home)
    if not resolved.is_file():
        return None
    try:
        stat = resolved.stat()
    except OSError as exc:
        logger.debug(f"registry_mtime path={resolved!r} stat failed: {exc!r}")
        return None
    return datetime.fromtimestamp(stat.st_mtime, tz=UTC)


def repo_state_mtime(repo_path: Path) -> datetime | None:
    """Return the mtime of ``<repo_path>/.ea/state.json`` as UTC, or ``None``.

    Per-repo branch of the stale-chip rule (success criterion 3, (b)).
    Treats missing / unreadable files as ``None`` so the caller can
    fold the result into the OR-chain of staleness signals.
    """
    candidate = repo_path / ".ea" / "state.json"
    if not candidate.is_file():
        return None
    try:
        stat = candidate.stat()
    except OSError as exc:
        logger.debug(f"repo_state_mtime path={candidate!r} stat failed: {exc!r}")
        return None
    return datetime.fromtimestamp(stat.st_mtime, tz=UTC)


def read_repo_state(repo_path: Path) -> dict[str, Any] | None:
    """Best-effort load of ``<repo_path>/.ea/state.json`` as a dict.

    Returns ``None`` when the file is missing, unreadable, or fails
    JSON decode — the third branch of the stale-chip rule (success
    criterion 3, (c)). The caller (workspace dashboard) feeds the
    result straight into the layout pane builders, which already
    accept an empty dict gracefully.
    """
    candidate = repo_path / ".ea" / "state.json"
    if not candidate.is_file():
        return None
    try:
        return orjson.loads(candidate.read_bytes())  # type: ignore[no-any-return]
    except (orjson.JSONDecodeError, OSError) as exc:
        logger.debug(f"read_repo_state path={candidate!r} unreadable: {exc!r}")
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
      JSON decode error).

    Args:
        entry: One :class:`RegistryRepoEntry` from the registry.
        registry_mtime_at: Output of :func:`registry_mtime` for the
            same registry file the entry came from. ``None`` is
            treated as fresh — a missing registry is already an
            error-path surfaced elsewhere and should not double-fire.
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
    "Registry",
    "RegistryReadError",
    "RegistryRepoEntry",
    "default_registry_path",
    "is_stale",
    "read_registry",
    "read_repo_state",
    "registry_mtime",
    "repo_state_mtime",
]
