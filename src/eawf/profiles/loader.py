"""Profile YAML loader.

Built-in bundle: ``eawf.profiles.data/*.yaml`` (importlib.resources).
P14-W04 (D18) adds workspace + user overlay roots; see
:mod:`eawf.profiles.discovery` for the search-order helpers. The
public ``load_profile`` keeps its v0.1 signature and delegates to
:func:`eawf.profiles.discovery.load_profile_with_discovery` so existing
callers stay binary-compatible.

Public API:

    load_profile(profile_id, workspace=None) -> ProfileBody
    list_profiles(workspace=None)           -> tuple[str, ...]
"""

from __future__ import annotations

import logging
from pathlib import Path

from eawf.profiles.discovery import (
    list_profiles_all,
    load_profile_with_discovery,
)
from eawf.profiles.models import ProfileBody

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
        InvalidInput: ``profile_id`` is not present in any layer.
        ValidationFailed: The YAML body fails schema validation.
    """
    return load_profile_with_discovery(profile_id, workspace=workspace)
