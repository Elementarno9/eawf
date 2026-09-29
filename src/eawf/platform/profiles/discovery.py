"""Layered profile discovery + mtime-keyed cache (P14-W04 / D18).

Discovery roots, highest precedence first:

1. Repository overlay — ``<repo>/.ea/profiles/*.yaml``.
2. Workspace overlay — ``<workspace>/.ea/profiles/*.yaml``.
3. User overlay (the global layer) — ``~/.eawf/profiles/*.yaml``.
4. Built-in bundle — ``eawf.platform.profiles.data`` (``importlib.resources``).

A profile id present in a higher layer wins over the same id in a
lower layer. Discovery + load operate on the union of ids; lookup
yields the file path from the highest available layer. These declared
roots are the only places a custom profile is found: each is read one
level deep for ``*.yaml`` and never walked, and no other directory is
inferred from the layout around it.

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
from pydantic import ValidationError as PydValidationError

from eawf.platform.profiles.models import ProfileBody
from eawf.surfaces.cli.errors import UserError, ValidationError

logger = logging.getLogger(__name__)


_DATA_PACKAGE: str = "eawf.platform.profiles.data"
_INIT_TEMPLATE_PACKAGE: str = "eawf.platform.templates.init"
_YAML_SUFFIX: str = ".yaml"


@dataclass(frozen=True)
class ProfileLocation:
    """Where a profile id resolved from + its on-disk mtime (if any).

    ``source`` is one of ``"repo"``, ``"workspace"``, ``"user"``, or
    ``"builtin"``. ``path`` is the resolved file path (overlays) or
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
    """``<root>/.ea/profiles`` — the overlay root of a workspace or repository."""
    return Path(workspace) / ".ea" / "profiles"


def _overlay_roots(
    *, repo: Path | str | None, workspace: Path | str | None
) -> tuple[tuple[str, Path], ...]:
    """Return the declared ``(source, root)`` overlay pairs, highest precedence first."""
    roots: list[tuple[str, Path]] = []
    if repo is not None:
        roots.append(("repo", workspace_profiles_dir(repo)))
    if workspace is not None:
        roots.append(("workspace", workspace_profiles_dir(workspace)))
    roots.append(("user", user_profiles_dir()))
    return tuple(roots)


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
    repo: Path | str | None = None,
    workspace: Path | str | None = None,
) -> ProfileLocation:
    """Resolve *profile_id* across repo > workspace > user > builtin layers.

    Args:
        profile_id: Profile name (YAML stem; e.g. ``"core"``).
        repo: Optional repository root. When given, its ``.ea/profiles/``
            is consulted first.
        workspace: Optional workspace root. When given, its
            ``.ea/profiles/`` is consulted after the repository overlay and
            before the user overlay.

    Raises:
        UserError: The id is not present in any layer (``kind="InvalidInput"``).
    """
    for source, root in _overlay_roots(repo=repo, workspace=workspace):
        path = _iter_yaml(root).get(profile_id)
        if path is not None:
            return ProfileLocation(
                profile_id=profile_id,
                source=source,
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
    choices = list(list_profiles_all(repo=repo, workspace=workspace))
    raise UserError(f"unknown profile {profile_id!r}; choose from {choices}", kind="InvalidInput")


def list_profiles_all(
    *,
    repo: Path | str | None = None,
    workspace: Path | str | None = None,
) -> tuple[str, ...]:
    """Return the union of profile ids visible across every layer.

    Each id is reported once. Order is stable (sorted). Useful when the
    CLI surfaces a "choose a profile" list — the operator should see
    every id resolvable via :func:`discover_profile`.
    """
    ids: set[str] = set(_builtin_ids())
    for _source, root in _overlay_roots(repo=repo, workspace=workspace):
        ids.update(_iter_yaml(root).keys())
    return tuple(sorted(ids))


# Cache key: (source, path-or-stem, mtime_ns). Builtin entries cache
# permanently with mtime_ns=None.
_PROFILE_CACHE: dict[tuple[str, str, int | None], ProfileBody] = {}


def _parse_and_validate(profile_id: str, raw: str) -> ProfileBody:
    parsed: Any
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValidationError(f"profile {profile_id!r}: malformed YAML: {exc}") from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ValidationError(
            f"profile {profile_id!r}: top-level must be a mapping, got {type(parsed).__name__}"
        )
    try:
        return ProfileBody.model_validate(parsed)
    except PydValidationError as exc:
        raise ValidationError(f"profile {profile_id!r}: schema rejected: {exc}") from exc


def load_profile_with_discovery(
    profile_id: str,
    *,
    repo: Path | str | None = None,
    workspace: Path | str | None = None,
) -> ProfileBody:
    """Discover + read + Pydantic-validate *profile_id*.

    Uses the repo > workspace > user > builtin precedence order. Each
    on-disk location is cached per ``(source, path, mtime_ns)`` — touching
    a profile YAML invalidates only its own slot.
    """
    loc = discover_profile(profile_id, repo=repo, workspace=workspace)
    key: tuple[str, str, int | None]
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


# ---- init bootstrap templates (C08 — P25-W16) ------------------------------
#
# Three YAML templates ship in v0.3 per C08 D7 / D10. Each is a
# ``.ea/config.yaml`` seed consumed by ``eawf init --template <name>``;
# the loader merges template values with operator answers (project_code,
# project_title) before the wizard writes the canonical config.
#
# Discovery is wheel-bundle-only (no workspace/user overlay): operators
# fork the YAML in their repo if they need to customise. Discovery never
# scans the disk for ad-hoc templates — explicit-only growth, matching
# the registry conventions for built-in profile bodies.


def list_init_templates() -> tuple[str, ...]:
    """Return the names of bundled init templates, sorted.

    Reads from the :mod:`eawf.platform.templates.init` package via
    :func:`importlib.resources.files` so the templates work from a
    wheel install, an editable install, and the source tree alike.

    Returns:
        Sorted tuple of template names (YAML stems; e.g.
        ``("engineering", "research", "reverse-engineering")``).
    """
    data = files(_INIT_TEMPLATE_PACKAGE)
    names: list[str] = []
    for entry in data.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if not name.endswith(_YAML_SUFFIX):
            continue
        names.append(name.removesuffix(_YAML_SUFFIX))
    return tuple(sorted(names))


def load_init_template(template_name: str) -> dict[str, Any]:
    """Read + parse the bundled init template named *template_name*.

    Returns the raw parsed YAML mapping; the caller merges it with
    operator-supplied answers (project_code, project_title) before
    writing ``.ea/config.yaml``. The returned dict is freshly parsed on
    every call — templates are small (a few keys) and immutable on disk
    so caching gives no measurable benefit.

    Args:
        template_name: YAML stem (e.g. ``"research"``). Must be one of
            the values returned by :func:`list_init_templates`.

    Returns:
        Parsed YAML mapping (always a ``dict[str, Any]``).

    Raises:
        UserError: ``template_name`` is not in the bundled set
            (``kind="InvalidInput"``).
        ValidationError: Template YAML is malformed or non-mapping.
    """
    known = list_init_templates()
    if template_name not in known:
        raise UserError(
            f"unknown init template: {template_name!r}; choose from {list(known)}",
            kind="InvalidInput",
        )
    raw = (
        files(_INIT_TEMPLATE_PACKAGE)
        .joinpath(f"{template_name}{_YAML_SUFFIX}")
        .read_text(encoding="utf-8")
    )
    try:
        parsed: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValidationError(f"init template {template_name!r}: malformed YAML: {exc}") from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ValidationError(
            f"init template {template_name!r}: top-level must be a mapping, "
            f"got {type(parsed).__name__}"
        )
    logger.debug(f"load_init_template template={template_name!r} keys={sorted(parsed)}")
    return parsed
