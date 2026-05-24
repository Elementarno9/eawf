"""Profile YAML loader + composition entry point.

Built-in bundle: ``eawf.platform.profiles.data/*.yaml`` (importlib.resources).
P14-W04 (D18) adds workspace + user overlay roots; see
:mod:`eawf.platform.profiles.discovery` for the search-order helpers. P25-W15 adds
the explicit composition loader :func:`load_composed_profile` which
discovers, validates, then composes a deterministic
:class:`~eawf.platform.profiles.models.ComposedProfile` from a list of profile ids.

The composition loader sorts its input ids before dispatch so that
``load_composed_profile(["python", "core"])`` and
``load_composed_profile(["core", "python"])`` produce the same composed
output bytes (per W15 success criterion 2 — deterministic deep-merge by
id). Callers that need a non-default merge order should call
:func:`load_profile` themselves and pass an explicit list to
:func:`eawf.platform.profiles.compose.compose`.

Public API:

    load_profile(profile_id, workspace=None) -> ProfileBody
    list_profiles(workspace=None)           -> tuple[str, ...]
    load_composed_profile(profile_ids, *, workspace=None,
                          conflict_resolution="fail") -> ComposedProfile
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from eawf.platform.profiles.compose import ConflictResolution, compose
from eawf.platform.profiles.discovery import (
    list_profiles_all,
    load_profile_with_discovery,
)
from eawf.platform.profiles.models import ComposedProfile, ProfileBody

logger = logging.getLogger(__name__)


def list_profiles(*, workspace: Path | str | None = None) -> tuple[str, ...]:
    """Enumerate profile ids visible across builtin + user + workspace layers.

    Args:
        workspace: Optional workspace root. When given, its
            ``.ea/profiles/`` overlay is included in the union.

    Returns:
        Tuple of profile ids in deterministic sorted order.
    """
    return list_profiles_all(workspace=workspace)


def load_profile(
    profile_id: str,
    *,
    workspace: Path | str | None = None,
) -> ProfileBody:
    """Discover and validate ``<profile_id>.yaml`` from the layered roots.

    Args:
        profile_id: Profile name (YAML stem).
        workspace: Optional workspace root. When given, its
            ``.ea/profiles/`` overlay wins over the user overlay
            (``~/.eawf/profiles/``), which wins over the built-in
            bundle.

    Returns:
        Pydantic-validated :class:`ProfileBody`.

    Raises:
        UserError: ``profile_id`` is not present in any layer
            (``kind="InvalidInput"``).
        ValidationError: The YAML body fails schema validation.
    """
    return load_profile_with_discovery(profile_id, workspace=workspace)


def load_composed_profile(
    profile_ids: Iterable[str],
    *,
    workspace: Path | str | None = None,
    conflict_resolution: ConflictResolution = "fail",
) -> ComposedProfile:
    """Discover, validate, and compose a deterministic view of ``profile_ids``.

    The loader sorts ``profile_ids`` by id before composing so two callers
    that enable the same set of profiles in different orders produce the
    same output bytes (W15 success criterion 2 — deterministic deep-merge).
    Duplicate ids in the input are deduplicated; an empty iterable returns
    the canonical ``"composed:empty"`` envelope.

    Conflict + override discharge follows the v2 rules in
    :mod:`eawf.platform.profiles.compose`. With ``conflict_resolution="fail"``
    (default — V3 fail-fast) an undeclared conflict raises
    :class:`~eawf.platform.profiles.compose.ProfileConflict`; ``"first-wins"``
    keeps the earlier-declared profile and records a warning on the
    composed envelope.

    Args:
        profile_ids: Iterable of profile YAML stems. Sorted + deduplicated
            before dispatch.
        workspace: Optional workspace root forwarded to the discovery layer.
            When given, the workspace overlay wins over the user overlay
            and the built-in bundle.
        conflict_resolution: ``"fail"`` (default) raises
            :class:`~eawf.platform.profiles.compose.ProfileConflict` on undeclared
            conflicts; ``"first-wins"`` keeps the caller-first profile.

    Returns:
        :class:`ComposedProfile` with all merged fields, provenance,
        override-audit, and conflict-warning records.

    Raises:
        UserError: One of the requested ids is not present in any
            discovery layer (``kind="InvalidInput"``).
        ValidationError: A discovered YAML body fails schema validation.
        ProfileConflict: ``conflict_resolution="fail"`` and the composition
            has at least one undeclared conflict edge.
    """
    sorted_unique_ids = sorted(set(profile_ids))
    bodies = [load_profile(pid, workspace=workspace) for pid in sorted_unique_ids]
    logger.debug(
        f"load_composed_profile ids={sorted_unique_ids!r} resolution={conflict_resolution}",
    )
    return compose(bodies, conflict_resolution=conflict_resolution)
