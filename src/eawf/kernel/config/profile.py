"""Profile enable: write to a config layer + materialise required state keys.

Per ``docs/architecture/profiles.md``, each profile may declare
``state_extensions.fields_required`` — a list of top-level state keys that
must exist for the profile's skills/audits to function. ``config profile
enable <id>`` is responsible for two things:

1. Update the chosen layer's ``config.yaml`` to record ``profiles.enabled``
   contains *id* (idempotent — duplicate enables are no-ops).
2. Materialise any *fields_required* on ``state.json`` as empty containers
   ``{}`` so later mutations write into typed dicts instead of attribute
   errors. This mirrors the "materialize newly-required keys as ``{}``" rule
   from ``docs/architecture/installation.md``.

Mutation discipline (per ``AGENTS.md`` rule 4 + spec):

- Layer file write: acquire ``portalock.acquire(layer_path)`` → read existing
  YAML → merge → atomic temp-file → fsync → ``os.replace`` → release.
- State write: route through :func:`eawf.kernel.state.writer.atomic_write_json`
  which already handles the lock + atomic write.

``enable_profile`` resolves *profile_id* and its ``fields_required`` through
:mod:`eawf.platform.profiles.loader` (workspace overlay > user overlay >
built-in bundle) -- the same registry
:func:`eawf.platform.profiles.selection.resolve_enabled_profiles` feeds to
the AGENTS.md renderer -- so a profile defined only under a workspace
overlay enables the same way a built-in one does.

Public API:

    KNOWN_PROFILES               # built-in-only id -> required state field keys
    enable_profile(profile_id, *, layer, layer_file_path, state_path,
                    workspace=None) -> dict
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import orjson
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

    Replaces the hand-coded table that lived here through Phase 2: the source
    of truth is now the on-disk profile body. Adding a new profile is one
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


def _materialise_state_keys(state_path: Path, fields: list[str]) -> list[str]:
    """Add missing top-level keys to ``state.json`` as empty dicts.

    Returns the list of keys actually added (so the caller can include them in
    the response envelope). Idempotent: keys already present are left alone.
    Skips materialisation entirely if ``state_path`` does not exist (the
    profile is being enabled before ``eawf project init``).

    The read+mutate+write of ``state.json`` is serialised under
    ``portalock(state_path)`` so a concurrent writer (e.g. an
    ``eawf.surfaces.cli._mutation.state_transaction``) cannot drop the freshly-added
    top-level keys via a stale-view dump. Routing through
    ``state_transaction`` itself is unsuitable here because that wrapper
    raises ``UserError`` (``kind="NotFound"``) on missing state, breaking
    the profile-enable-before-init flow.
    """
    if not state_path.exists():
        logger.info(
            f"_materialise_state_keys state-file-absent path={state_path}; "
            "skipping materialisation; the next eawf init/sync will pick the "
            "profile up from the config file."
        )
        return []

    from eawf.kernel.state.writer import atomic_write_json_locked

    try:
        with portalock.acquire(state_path, timeout=5.0):
            try:
                raw = state_path.read_bytes()
            except OSError as exc:
                raise UserError(
                    f"cannot read state file {state_path}: {exc}", kind="NotFound"
                ) from exc
            try:
                body = orjson.loads(raw)
            except orjson.JSONDecodeError as exc:
                raise UserError(
                    f"state file {state_path} is not valid JSON: {exc}", kind="NotFound"
                ) from exc
            if not isinstance(body, dict):
                raise UserError(
                    f"state file {state_path} top-level must be a mapping", kind="NotFound"
                )

            added: list[str] = []
            for key in fields:
                if key not in body:
                    body[key] = {}
                    added.append(key)
            if not added:
                return []

            atomic_write_json_locked(state_path, body)
            return added
    except portalock.LockTimeout as exc:
        raise UserError(
            f"could not acquire state lock for {state_path}: {exc}", kind="NotFound"
        ) from exc


def enable_profile(
    profile_id: str,
    *,
    layer: str,
    layer_file_path: Path,
    state_path: Path | None = None,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Enable *profile_id* by writing it to *layer* and materialising state keys.

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
        state_path: Optional state-file path. When given, missing
            ``fields_required`` for the profile are materialised as ``{}``.
            When ``None``, materialisation is skipped (the next
            ``eawf init/sync`` performs it).
        workspace: Optional workspace root. When given, its
            ``.ea/profiles/`` overlay is consulted (ahead of the user
            overlay and the built-in bundle) when resolving *profile_id*.

    Returns:
        Response envelope (dict) with keys ``profile``, ``layer``,
        ``layer_path``, ``already_enabled``, ``state_keys_materialised``.

    Raises:
        UserError: ``profile_id`` unknown or ``layer`` not writable
            (``kind="InvalidInput"``); or state file is malformed
            (read-time only) (``kind="NotFound"``).
        ValidationError: A workspace or user overlay profile with a
            matching id is present but its YAML body is malformed or
            fails schema validation.
    """
    known_ids = list_profiles(workspace=workspace)
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

    profile_body = load_profile(profile_id, workspace=workspace)
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

    materialised: list[str] = []
    if state_path is not None and required_fields:
        materialised = _materialise_state_keys(state_path, required_fields)

    return {
        "profile": profile_id,
        "layer": layer,
        "layer_path": str(layer_file_path),
        "already_enabled": already_enabled,
        "state_keys_materialised": materialised,
    }
