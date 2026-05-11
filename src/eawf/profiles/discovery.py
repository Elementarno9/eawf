"""Layered profile discovery + mtime-keyed cache (P14-W04 / D18).

Discovery roots, highest precedence first:

1. Workspace overlay — ``<workspace>/.ea/profiles/*.yaml``.
2. User overlay — ``~/.eawf/profiles/*.yaml``.
3. Built-in bundle — ``eawf.profiles.data`` (``importlib.resources``).

A profile id present in a higher layer wins over the same id in a
lower layer. Discovery + load operate on the union of ids; lookup
yields the file path from the highest available layer.

Cache strategy: ``@functools.cache`` is replaced with a manual
mtime-keyed dict so editing a profile YAML invalidates that one slot
without nuking the rest. Built-in bundle entries cache permanently
(the bundle is shipped read-only — no mtime to track).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from eawf.cli.errors import InvalidInput, ValidationFailed
from eawf.profiles.models import ProfileBody

logger = logging.getLogger(__name__)


_DATA_PACKAGE: str = "eawf.profiles.data"
_YAML_SUFFIX: str = ".yaml"


@dataclass(frozen=True)
class ProfileLocation:
    """Where a profile id resolved from + its on-disk mtime (if any).

    ``source`` is one of ``"workspace"``, ``"user"``, or ``"builtin"``.
    ``path`` is the resolved file path (workspace/user overlays) or
    ``None`` for built-in bundle entries (the ``importlib.resources``
    traversable is not a filesystem path on every platform).
    ``mtime_ns`` is ``None`` for built-in entries.
    """

    profile_id: str
    source: str
    path: Path | None
    mtime_ns: int | None


def user_profiles_dir() -> Path:
    """``~/.eawf/profiles`` — user-scope profile overlay root."""
    return Path.home() / ".eawf" / "profiles"


def workspace_profiles_dir(workspace: Path | str) -> Path:
    """``<workspace>/.ea/profiles`` — workspace-scope profile overlay root."""
    return Path(workspace) / ".ea" / "profiles"


def _iter_yaml(root: Path) -> dict[str, Path]:
    """Return ``{profile_id: path}`` for every ``*.yaml`` directly under *root*.

    Non-existent or non-dir roots resolve to an empty dict (a missing
    overlay is the common case, not an error).
    """
    if not root.is_dir():
        return {}
    out: dict[str, Path] = {}
    for entry in root.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix != _YAML_SUFFIX:
            continue
        out[entry.stem] = entry
    return out


def _builtin_ids() -> tuple[str, ...]:
    data = files(_DATA_PACKAGE)
    ids: list[str] = []
    for entry in data.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if not name.endswith(_YAML_SUFFIX):
            continue
        ids.append(name.removesuffix(_YAML_SUFFIX))
    return tuple(sorted(ids))


def discover_profile(
    profile_id: str,
    *,
    workspace: Path | str | None = None,
) -> ProfileLocation:
    """Resolve *profile_id* across workspace > user > builtin layers.

    Args:
        profile_id: Profile name (YAML stem; e.g. ``"core"``).
        workspace: Optional workspace root. When given, its
            ``.ea/profiles/`` is consulted before the user overlay.

    Raises:
        InvalidInput: The id is not present in any layer.
    """
    if workspace is not None:
        ws_map = _iter_yaml(workspace_profiles_dir(workspace))
        if profile_id in ws_map:
            path = ws_map[profile_id]
            return ProfileLocation(
                profile_id=profile_id,
                source="workspace",
                path=path,
                mtime_ns=path.stat().st_mtime_ns,
            )
    user_map = _iter_yaml(user_profiles_dir())
    if profile_id in user_map:
        path = user_map[profile_id]
        return ProfileLocation(
            profile_id=profile_id,
            source="user",
            path=path,
            mtime_ns=path.stat().st_mtime_ns,
        )
    if profile_id in _builtin_ids():
        return ProfileLocation(
            profile_id=profile_id,
            source="builtin",
            path=None,
            mtime_ns=None,
        )
    raise InvalidInput(
        f"unknown profile {profile_id!r}; choose from {list(list_profiles_all(workspace=workspace))}"
    )


def list_profiles_all(*, workspace: Path | str | None = None) -> tuple[str, ...]:
    """Return the union of profile ids visible across all three layers.

    Each id is reported once. Order is stable (sorted). Useful when the
    CLI surfaces a "choose a profile" list — the operator should see
    every id resolvable via :func:`discover_profile`.
    """
    ids: set[str] = set(_builtin_ids())
    ids.update(_iter_yaml(user_profiles_dir()).keys())
    if workspace is not None:
        ids.update(_iter_yaml(workspace_profiles_dir(workspace)).keys())
    return tuple(sorted(ids))


# Cache key: (source, path-or-stem, mtime_ns). Builtin entries cache
# permanently with mtime_ns=None.
_PROFILE_CACHE: dict[tuple[str, str, int | None], ProfileBody] = {}


def _parse_and_validate(profile_id: str, raw: str) -> ProfileBody:
    parsed: Any
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValidationFailed(f"profile {profile_id!r}: malformed YAML: {exc}") from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ValidationFailed(
            f"profile {profile_id!r}: top-level must be a mapping, got {type(parsed).__name__}"
        )
    try:
        return ProfileBody.model_validate(parsed)
    except ValidationError as exc:
        raise ValidationFailed(f"profile {profile_id!r}: schema rejected: {exc}") from exc


def load_profile_with_discovery(
    profile_id: str,
    *,
    workspace: Path | str | None = None,
) -> ProfileBody:
    """Discover + read + Pydantic-validate *profile_id*.

    Uses the workspace > user > builtin precedence order. Each on-disk
    location is cached per ``(source, path, mtime_ns)`` — touching a
    profile YAML invalidates only its own slot.
    """
    loc = discover_profile(profile_id, workspace=workspace)
    if loc.source == "builtin":
        key = ("builtin", profile_id, None)
        cached = _PROFILE_CACHE.get(key)
        if cached is not None:
            return cached
        data = files(_DATA_PACKAGE)
        raw = data.joinpath(f"{profile_id}{_YAML_SUFFIX}").read_text(encoding="utf-8")
        body = _parse_and_validate(profile_id, raw)
        _PROFILE_CACHE[key] = body
        return body
    assert loc.path is not None and loc.mtime_ns is not None
    key = (loc.source, str(loc.path), loc.mtime_ns)
    cached = _PROFILE_CACHE.get(key)
    if cached is not None:
        return cached
    body = _parse_and_validate(profile_id, loc.path.read_text(encoding="utf-8"))
    _PROFILE_CACHE[key] = body
    return body


def _clear_cache_for_tests() -> None:
    """Drop the entire cache. Used only by the test suite."""
    _PROFILE_CACHE.clear()
