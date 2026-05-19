"""Typed Pydantic models for ``~/.eawf/registry.json``.

The user-scope registry is the index of repos the operator has
explicitly initialised or registered. Per the project memory note
``feedback_explicit_registry_only`` the registry grows ONLY through
explicit ``eawf init`` / ``eawf repo add`` writes. This module ships
the read-side surface: models, default-path resolver, JSON loader,
and the explicit-growth guard that names the supported bootstrap so
ad-hoc scan/walk attempts fail fast with a directive error.

Registry shape on disk::

    {
      "version": "1",
      "updated_at": "2026-05-01T12:34:56+00:00",
      "active_code": "EAWF",
      "repos": {
        "EAWF": {"code": "EAWF", "path": "/repos/eawf", "title": "Ea"},
        "DEMO": {"code": "DEMO", "path": "/repos/demo", "title": "Demo"}
      }
    }

The mutator side lives in :mod:`eawf.cli.commands.repo` (which dispatches
to the daemon's ``registry.update`` RPC by default per D-SUP-01); this
module never writes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


#: Explicit-growth surfaces operators must use to add a repo to the
#: registry. Surfaced in :class:`ImplicitRegistryGrowthError` so the
#: error message names the supported bootstrap path.
EXPLICIT_GROWTH_SURFACES: tuple[str, ...] = (
    "eawf init",
    "eawf repo add <path>",
    "eawf workspace add-repo <code>",
)


#: Free-form labels for the implicit-growth surfaces the registry
#: refuses to honour. Used by :func:`reject_implicit_growth` so any
#: future "auto-discovery" caller gets a clear error pointing at the
#: explicit bootstrap rule.
FORBIDDEN_GROWTH_PATHS: tuple[str, ...] = (
    "scan",
    "walk",
    "import-from-scan",
    "auto-discovery",
)


class RegistryRepoEntry(BaseModel):
    """One entry under :attr:`Registry.repos`.

    The shape is intentionally narrow: code + on-disk path + optional
    human-readable title + optional last-seen stamp. The registry stays
    a pointer index rather than a denormalised copy of the per-repo
    ``state.json``; the TUI staleness logic re-reads ``state.json`` at
    render time so drift between the two surfaces never persists.

    Attributes:
        code: Project-code-shape repo identifier
            (``[A-Z][A-Z0-9_-]+``).
        path: Absolute on-disk path to the repo's working tree.
        title: Optional human-readable title; falls back to ``code``
            for display when absent.
        last_seen: Optional timestamp of the last explicit
            init/add-repo touch (informational only; staleness uses
            the registry file's mtime + state.json mtime instead).
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    path: str
    title: str | None = None
    last_seen: datetime | None = None


class Registry(BaseModel):
    """Read-only view over ``~/.eawf/registry.json``.

    The ``version`` field exists so future schema bumps stay
    forward-compatible; today only ``"1"`` is accepted. Callers must
    not mutate the model directly: the canonical write path lives in
    :mod:`eawf.cli.commands.repo` (daemon-proxied per D-SUP-01) and
    the constructor contract here is load + inspect only.

    Attributes:
        version: Schema version string (currently ``"1"``).
        updated_at: Registry-level last-touched timestamp. Distinct
            from the file's filesystem mtime so a stale-detect
            fallback exists when the filesystem timestamp drifts.
        active_code: Optional code marking the "active" repo for the
            workspace dashboard's quadrant body.
        repos: Mapping of project-code to :class:`RegistryRepoEntry`.
    """

    model_config = ConfigDict(extra="forbid")

    version: str = "1"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    active_code: str | None = None
    repos: dict[str, RegistryRepoEntry] = Field(default_factory=dict)


class RegistryReadError(Exception):
    """Raised when ``read_registry`` cannot return a typed :class:`Registry`.

    Callers that wrap the registry (TUI strip, scope dispatch ladder)
    catch this and either surface an empty-strip placeholder or fall
    through to the next scope tier so a missing or corrupted registry
    still renders a deterministic surface.
    """


class ImplicitRegistryGrowthError(Exception):
    """Raised when a caller attempts to grow the registry implicitly.

    The registry deliberately refuses scan/walk/import-from-discovery
    bootstrap paths per the ``feedback_explicit_registry_only`` memory
    note. Operators MUST use one of :data:`EXPLICIT_GROWTH_SURFACES`
    to add entries; this exception surfaces that directive with the
    forbidden surface name so the failure mode is unambiguous.

    Attributes:
        surface: The forbidden surface name that triggered the
            rejection (e.g. ``"scan"``, ``"walk"``).
    """

    def __init__(self, surface: str) -> None:
        self.surface = surface
        super().__init__(
            f"implicit registry growth via {surface!r} is forbidden; "
            f"use one of {EXPLICIT_GROWTH_SURFACES} to register a repo"
        )


def default_registry_path(*, home: Path | None = None) -> Path:
    """Return the canonical ``~/.eawf/registry.json`` path.

    The ``home`` kwarg is the test seam: pass a ``tmp_path`` root so
    unit/integration tests never touch the operator's real registry.
    """
    base = home if home is not None else Path.home()
    return base / ".eawf" / "registry.json"


def read_registry(path: Path | None = None, *, home: Path | None = None) -> Registry:
    """Load and validate ``~/.eawf/registry.json`` into a typed Registry.

    Strictly read-only. Never writes, never grows the registry, never
    triggers a scan; per ``feedback_explicit_registry_only`` the
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


def reject_implicit_growth(surface: str) -> None:
    """Refuse an implicit registry-growth attempt with a directive error.

    Centralised guard so any future ``scan`` / ``walk`` /
    ``import-from-scan`` / ``auto-discovery`` caller gets a single,
    consistent error message naming the supported explicit-bootstrap
    surfaces. Per the ``feedback_explicit_registry_only`` memory note
    the registry grows only via explicit operator commands; manual
    backfill is the supported bootstrap.

    Args:
        surface: Name of the forbidden growth surface (free-form;
            no validation against :data:`FORBIDDEN_GROWTH_PATHS` so
            callers can pass project-specific labels).

    Raises:
        ImplicitRegistryGrowthError: Always; this helper has no
            success path. The error names *surface* and the supported
            explicit-bootstrap commands.
    """
    raise ImplicitRegistryGrowthError(surface)


__all__ = [
    "EXPLICIT_GROWTH_SURFACES",
    "FORBIDDEN_GROWTH_PATHS",
    "ImplicitRegistryGrowthError",
    "Registry",
    "RegistryReadError",
    "RegistryRepoEntry",
    "default_registry_path",
    "read_registry",
    "reject_implicit_growth",
]
