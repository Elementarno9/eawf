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
      },
      "workspaces": {
        "MONO": {
          "key": "MONO",
          "title": "Mono",
          "member_project_codes": ["DEMO", "EAWF"],
          "home_project_code": "EAWF",
          "revision": 1
        }
      }
    }

``workspaces`` is optional: a registry file written before workspaces
existed loads unchanged and resolves to an empty mapping.

The mutator side lives in :mod:`eawf.surfaces.cli.commands.repo` (which dispatches
to the daemon's ``registry.update`` RPC by default per D-SUP-01); this
module never writes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from eawf.kernel.state.ids import is_project_code

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


class WorkspaceRecord(BaseModel):
    """One entry under :attr:`Registry.workspaces`.

    A workspace is an operator-declared grouping of already-registered
    repos. It is the unit a qualified URN is minted against, so the
    record has to be unambiguous: every member is named explicitly by
    project code and one of those members is the home repo whose
    ``state.json`` anchors the workspace.

    Membership is declared, never discovered. Per the
    ``feedback_explicit_registry_only`` rule there is no filesystem
    scan behind this record; an operator adds members with
    ``eawf workspace member add``.

    Attributes:
        key: Workspace identifier, project-code shape
            (``[A-Z][A-Z0-9_-]{1,15}``). Also the mapping key under
            :attr:`Registry.workspaces`.
        title: Optional human-readable title; display falls back to
            :attr:`key` when absent.
        member_project_codes: Non-empty set of project codes that
            belong to the workspace. Serialised sorted so a rewrite of
            an unchanged registry is byte-stable.
        home_project_code: The member whose repo anchors the
            workspace. MUST be an element of
            :attr:`member_project_codes`.
        revision: Compare-and-set counter. A membership mutation that
            declares an expected revision is refused when the on-disk
            record has moved on, so two concurrent editors cannot
            silently clobber one another.
        updated_at: Optional stamp of the last explicit mutation.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    title: str | None = None
    member_project_codes: frozenset[str] = Field(min_length=1)
    home_project_code: str
    revision: int = Field(default=1, ge=1)
    updated_at: datetime | None = None

    @field_validator("key", "home_project_code")
    @classmethod
    def _validate_code_shape(cls, value: str) -> str:
        """Reject a key or home code that is not project-code shaped."""
        if not is_project_code(value):
            raise ValueError(f"not a project code: {value!r}")
        return value

    @field_validator("member_project_codes")
    @classmethod
    def _validate_member_shapes(cls, value: frozenset[str]) -> frozenset[str]:
        """Reject a member set containing a non-project-code entry."""
        bad = sorted(code for code in value if not is_project_code(code))
        if bad:
            raise ValueError(f"member project codes are not project codes: {bad}")
        return value

    @model_validator(mode="after")
    def _validate_home_is_member(self) -> WorkspaceRecord:
        """Reject a home repo that is not part of the declared membership.

        A workspace whose anchor sits outside its own member set would
        mint URNs against a repo the workspace does not track, so this
        is rejected at load time rather than at mint time.
        """
        if self.home_project_code not in self.member_project_codes:
            raise ValueError(
                f"home_project_code {self.home_project_code!r} is not in "
                f"member_project_codes {sorted(self.member_project_codes)}"
            )
        return self

    @field_serializer("member_project_codes")
    def _serialize_members(self, value: frozenset[str]) -> list[str]:
        """Emit members as a sorted list so on-disk bytes stay stable."""
        return sorted(value)


class Registry(BaseModel):
    """Read-only view over ``~/.eawf/registry.json``.

    The ``version`` field exists so future schema bumps stay
    forward-compatible; today only ``"1"`` is accepted. Callers must
    not mutate the model directly: the canonical write path lives in
    :mod:`eawf.surfaces.cli.commands.repo` (daemon-proxied per D-SUP-01) and
    the constructor contract here is load + inspect only.

    Attributes:
        version: Schema version string (currently ``"1"``).
        updated_at: Registry-level last-touched timestamp. Distinct
            from the file's filesystem mtime so a stale-detect
            fallback exists when the filesystem timestamp drifts.
        active_code: Optional code marking the "active" repo for the
            workspace dashboard's quadrant body.
        repos: Mapping of project-code to :class:`RegistryRepoEntry`.
        workspaces: Mapping of workspace key to
            :class:`WorkspaceRecord`. Defaults to empty so a registry
            file written before workspaces existed still loads.
    """

    model_config = ConfigDict(extra="forbid")

    version: str = "1"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    active_code: str | None = None
    repos: dict[str, RegistryRepoEntry] = Field(default_factory=dict)
    workspaces: dict[str, WorkspaceRecord] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_workspace_keys(self) -> Registry:
        """Reject a workspace filed under a key that is not its own.

        The mapping key is what every lookup path uses; letting it
        drift from ``record.key`` would make ``get`` and ``resolve``
        disagree about the same record.
        """
        mismatched = sorted(key for key, record in self.workspaces.items() if record.key != key)
        if mismatched:
            raise ValueError(f"workspace records filed under a mismatched key: {mismatched}")
        return self


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
    "WorkspaceRecord",
    "default_registry_path",
    "read_registry",
    "reject_implicit_growth",
]
