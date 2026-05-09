"""Profile enable: write to a config layer + materialise required state keys.

Per ``ea-proposal.md`` §"v0.1 profile bodies", each profile may declare
``state_extensions.fields_required`` — a list of top-level state keys that
must exist for the profile's skills/audits to function. ``config profile
enable <id>`` is responsible for two things:

1. Update the chosen layer's ``config.yaml`` to record ``profiles.enabled``
   contains *id* (idempotent — duplicate enables are no-ops).
2. Materialise any *fields_required* on ``state.json`` as empty containers
   ``{}`` so later mutations write into typed dicts instead of attribute
   errors. This mirrors the "materialize newly-required keys as ``{}``" rule
   from §9 of ``ea-proposal.md``.

Mutation discipline (per ``AGENTS.md`` rule 4 + spec):

- Layer file write: acquire ``portalock.acquire(layer_path)`` → read existing
  YAML → merge → atomic temp-file → fsync → ``os.replace`` → release.
- State write: route through :func:`eawf.state.writer.atomic_write_json`
  which already handles the lock + atomic write.

Public API:

    KNOWN_PROFILES               # mapping id → required state field keys
    enable_profile(profile_id, *, layer, layer_file_path, state_path) -> dict
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

from eawf.cli.errors import InvalidInput, NotFound
from eawf.config.layered import LAYER_ORDER, WRITABLE_LAYERS
from eawf.config.loader import load_yaml_layer
from eawf.lock import portalock

logger = logging.getLogger(__name__)


# Profile registry. The mapping id → required state-field names mirrors the
# ``state_extensions.fields_required`` block of each v0.1 profile body in
# ``ea-proposal.md``. Profiles without state_extensions appear with an empty
# list. Adding a profile here makes it enableable; rendering details belong to
# the Phase 5 W04 profile composition pass.
KNOWN_PROFILES: dict[str, list[str]] = {
    "core": [],
    "python": [],
    "research": ["hypotheses", "audits"],
    "docs": [],
    "apps": [],
    "infra": [],
    "ml": [],
    "quant": [],
    "re": [],
    "game": [],
    "robotics": [],
}


def _atomic_write_yaml(target: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``target`` atomically (tempfile + fsync + replace).

    Mirrors the procedure used by :func:`eawf.state.writer.atomic_write_json`,
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
        parent_fd = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        logger.info(f"_atomic_write_yaml wrote {target}")
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
    ``eawf.cli._mutation.state_transaction``) cannot drop the freshly-added
    top-level keys via a stale-view dump. Routing through
    ``state_transaction`` itself is unsuitable here because that wrapper
    raises ``NotFound`` on missing state, breaking the
    profile-enable-before-init flow.
    """
    if not state_path.exists():
        logger.info(
            f"_materialise_state_keys: state file absent at {state_path} — "
            "skipping materialisation; the next eawf init/sync will pick the "
            "profile up from the config file."
        )
        return []

    from eawf.state.writer import atomic_write_json_locked

    try:
        with portalock.acquire(state_path, timeout=5.0):
            try:
                raw = state_path.read_bytes()
            except OSError as exc:
                raise NotFound(f"cannot read state file {state_path}: {exc}") from exc
            try:
                body = orjson.loads(raw)
            except orjson.JSONDecodeError as exc:
                raise NotFound(f"state file {state_path} is not valid JSON: {exc}") from exc
            if not isinstance(body, dict):
                raise NotFound(f"state file {state_path} top-level must be a mapping")

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
        raise NotFound(f"could not acquire state lock for {state_path}: {exc}") from exc


def enable_profile(
    profile_id: str,
    *,
    layer: str,
    layer_file_path: Path,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """Enable *profile_id* by writing it to *layer* and materialising state keys.

    Args:
        profile_id: Profile name (must appear in :data:`KNOWN_PROFILES`).
        layer: One of :data:`WRITABLE_LAYERS`. The literal layer label is
            stored verbatim in the response envelope; the read-only
            ``"built-in"`` layer is rejected with :class:`InvalidInput`.
        layer_file_path: Resolved on-disk path to the layer's ``config.yaml``.
        state_path: Optional state-file path. When given, missing
            ``fields_required`` for the profile are materialised as ``{}``.
            When ``None``, materialisation is skipped (the next
            ``eawf init/sync`` performs it).

    Returns:
        Response envelope (dict) with keys ``profile``, ``layer``,
        ``layer_path``, ``already_enabled``, ``state_keys_materialised``.

    Raises:
        InvalidInput: ``profile_id`` unknown or ``layer`` not writable.
        NotFound: state file is malformed (read-time only).
    """
    if profile_id not in KNOWN_PROFILES:
        raise InvalidInput(f"unknown profile {profile_id!r}; choose from {sorted(KNOWN_PROFILES)}")
    if layer not in LAYER_ORDER:
        raise InvalidInput(f"unknown layer {layer!r}")
    if layer not in WRITABLE_LAYERS:
        raise InvalidInput(f"layer {layer!r} is read-only; cannot enable a profile here")

    required_fields = KNOWN_PROFILES[profile_id]

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
                f"enable_profile: {profile_id!r} already enabled in "
                f"{layer_file_path}; no write performed."
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
