"""Resolve enabled profile IDs from one workspace's layered configuration."""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.config.layered import merge_config
from eawf.platform.profiles.loader import list_profiles


def resolve_enabled_profiles(target: Path, *, workspace: Path | None = None) -> list[str]:
    """Return validated ``profiles.enabled`` values for *target*.

    Args:
        target: Repository root whose layered configuration names the
            enabled profiles and whose ``.ea/profiles/`` is the repository
            profile layer.
        workspace: Optional workspace root whose ``.ea/profiles/`` is the
            workspace profile layer.

    Returns:
        The enabled profile ids, in configured order.

    Raises:
        ValueError: ``profiles.enabled`` is not a list of strings, or names
            an id no profile layer defines.
    """
    merged, _sources = merge_config(repo=target, workspace=target)
    profiles_section = merged.get("profiles") if isinstance(merged, dict) else None
    if not isinstance(profiles_section, dict):
        return []
    raw_enabled = profiles_section.get("enabled")
    if raw_enabled is None:
        return []
    if not isinstance(raw_enabled, list) or any(
        not isinstance(profile_id, str) for profile_id in raw_enabled
    ):
        raise ValueError("profiles.enabled must be a list of profile ids")
    available = set(list_profiles(repo=target, workspace=workspace))
    unknown = sorted(set(raw_enabled) - available)
    if unknown:
        raise ValueError(f"unknown enabled profiles: {unknown!r}")
    return raw_enabled


__all__ = ["resolve_enabled_profiles"]
