"""Profile enable: write the profile to a config layer.

``config profile enable <id>`` updates the chosen layer's ``config.yaml``
so ``profiles.enabled`` contains *id* (idempotent -- duplicate enables are
no-ops). The layer file is written under ``portalock.acquire(layer_path)``:
read the existing YAML, merge, write a temp file, fsync, ``os.replace``.

``enable_profile`` resolves *profile_id* and its ``fields_required`` through
:mod:`eawf.platform.profiles.loader` (repository overlay > workspace overlay >
user overlay > built-in bundle) -- the same registry
:func:`eawf.platform.profiles.selection.resolve_enabled_profiles` feeds to
the AGENTS.md renderer -- so a profile defined only under a workspace
overlay enables the same way a built-in one does.

Public API:

    KNOWN_PROFILES               # built-in-only id -> required state field keys
    enable_profile(profile_id, *, layer, layer_file_path,
                    repo=None, workspace=None) -> dict
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import yaml

from eawf.kernel.config.layered import LAYER_ORDER, WRITABLE_LAYERS
from eawf.kernel.config.loader import load_yaml_layer
from eawf.kernel.fsync import fsync_parent_dir
from eawf.platform.profiles.loader import list_profiles, load_profile
from eawf.runtime.lock import portalock
from eawf.surfaces.cli.errors import UserError

logger = logging.getLogger(__name__)


def _build_known_profiles() -> dict[str, list[str]]:
    """Derive the ``id → fields_required`` registry from ``profiles/data/``.

    The source of truth is the on-disk profile body. Adding a new profile is one
    YAML file under :mod:`eawf.platform.profiles.data` plus an entry in any test that
    enumerates the v0.1 profile set.

    The shape is preserved verbatim (``dict[str, list[str]]``) so callers
    that key into :data:`KNOWN_PROFILES` continue to work without change.
    """
    return {
        pid: list(load_profile(pid).state_extensions.fields_required) for pid in list_profiles()
    }


# Built-in-only profile registry (no workspace/user overlay). Kept for
# callers that enumerate the shipped v0.1 profile set; ``enable_profile``
# below resolves ids through the overlay-aware ``list_profiles``/``load_profile``
# instead so a workspace-overlay profile id is accepted too.
KNOWN_PROFILES: dict[str, list[str]] = _build_known_profiles()


def _atomic_write_yaml(target: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``target`` atomically (tempfile + fsync + replace).

    Mirrors the procedure used by :func:`eawf.kernel.state.writer.atomic_write_json`,
    but the on-disk format is YAML so users can hand-edit committed config
    files. The caller is expected to already hold a portalock on ``target``.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = target.with_name(f"{target.name}.tmp.{suffix}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, sort_keys=True, default_flow_style=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        fsync_parent_dir(target)
        logger.info(f"_atomic_write_yaml wrote path={target}")
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink(missing_ok=True)


def enable_profile(
    profile_id: str,
    *,
    layer: str,
    layer_file_path: Path,
    repo: Path | None = None,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Enable *profile_id* by writing it to *layer*.

    Args:
        profile_id: Profile name. Resolved through the same
            workspace-overlay-aware registry the profile renderer uses
            (:func:`eawf.platform.profiles.loader.list_profiles` /
            :func:`eawf.platform.profiles.loader.load_profile`) rather than
            the built-in-only :data:`KNOWN_PROFILES` table, so a profile
            id defined only under a workspace overlay resolves here too.
        layer: One of :data:`WRITABLE_LAYERS`. The literal layer label is
            stored verbatim in the response envelope; the read-only
            ``"built-in"`` layer is rejected with :class:`UserError`
            (``kind="InvalidInput"``).
        layer_file_path: Resolved on-disk path to the layer's ``config.yaml``.
        repo: Optional repository root. When given, its ``.ea/profiles/``
            overlay is consulted first when resolving *profile_id*.
        workspace: Optional workspace root. When given, its
            ``.ea/profiles/`` overlay is consulted (after the repository
            overlay, ahead of the user overlay and the built-in bundle)
            when resolving *profile_id*.

    Returns:
        Response envelope (dict) with keys ``profile``, ``layer``,
        ``layer_path``, ``already_enabled`` and ``state_keys_required`` (the
        profile's ``fields_required``).

    Raises:
        UserError: ``profile_id`` unknown or ``layer`` not writable
            (``kind="InvalidInput"``).
        ValidationError: A workspace or user overlay profile with a
            matching id is present but its YAML body is malformed or
            fails schema validation.
    """
    known_ids = list_profiles(repo=repo, workspace=workspace)
    if profile_id not in known_ids:
        raise UserError(
            f"unknown profile {profile_id!r}; choose from {sorted(known_ids)}",
            kind="InvalidInput",
        )
    if layer not in LAYER_ORDER:
        raise UserError(f"unknown layer {layer!r}", kind="InvalidInput")
    if layer not in WRITABLE_LAYERS:
        raise UserError(
            f"layer {layer!r} is read-only; cannot enable a profile here", kind="InvalidInput"
        )

    profile_body = load_profile(profile_id, repo=repo, workspace=workspace)
    required_fields = list(profile_body.state_extensions.fields_required)

    with portalock.acquire(layer_file_path):
        existing = load_yaml_layer(layer_file_path)
        profiles_section = existing.get("profiles")
        if not isinstance(profiles_section, dict):
            profiles_section = {}
        enabled_list = profiles_section.get("enabled")
        if not isinstance(enabled_list, list):
            enabled_list = []

        already_enabled = profile_id in enabled_list
        if not already_enabled:
            enabled_list.append(profile_id)
        profiles_section["enabled"] = enabled_list
        existing["profiles"] = profiles_section

        if not already_enabled:
            _atomic_write_yaml(layer_file_path, existing)
        else:
            logger.info(
                f"enable_profile already-enabled profile_id={profile_id!r} "
                f"layer={layer_file_path}; no write performed"
            )

    return {
        "profile": profile_id,
        "layer": layer,
        "layer_path": str(layer_file_path),
        "already_enabled": already_enabled,
        "state_keys_required": required_fields,
    }
