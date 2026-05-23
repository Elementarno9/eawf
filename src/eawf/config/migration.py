"""Schema-version migration runner for layered config YAML.

The on-disk ``.ea/config.yaml`` (and every other layer file) carries a
``schema_version`` marker so the loader can detect drift between the
running code and the persisted shape. P25-W14 (cluster C08) introduces
the canonical ``"1.0"`` marker; earlier markers ``"1.1"`` (P14-W03
``runtime.adapters`` shim) and ``"2"`` (interim experimental, never
shipped to durable releases) are accepted on input and upgraded to
``"1.0"`` in place.

The migration is *additive* — every new C08 section (``telemetry``,
``dispatch``, ``language``, ``runtime.fallback``, ``profiles.trusted``,
``project.goals``, ``project.success_metrics``, ``config.layers_visible``)
gets a defaulted value when absent, but the operator's existing values
are NEVER overwritten. Re-running on an already-``"1.0"`` body is a
no-op.

Public API:

- :func:`migrate_config_payload` — in-memory upgrade; the daemon writer
  calls this before atomic-rename.
- :func:`migrate_config_file` — read-migrate-write helper for one
  layer file; primary entry point used by the daemon startup walker +
  the daemonless CLI fallback path.

Both helpers are idempotent. The on-disk variant writes a backup at
``<path>.bak.<old-marker>.<unix-epoch>`` so the operator can roll
back. Backups are kept indefinitely; periodic pruning is an operator
concern.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

import yaml

from eawf.cli.errors import ValidationError
from eawf.config.defaults import CONFIG_SCHEMA_VERSION

logger = logging.getLogger(__name__)


# Schema markers that the migrator recognises on input. Anything else
# raises :class:`ValidationError` — we refuse to silently accept an
# unknown future marker, since the body shape may differ in ways we
# cannot guess at.
CURRENT_MARKER: Final[str] = CONFIG_SCHEMA_VERSION  # "1.0"
LEGACY_MARKERS: Final[frozenset[str]] = frozenset({"1.1", "2"})
ACCEPTED_MARKERS: Final[frozenset[str]] = LEGACY_MARKERS | {CURRENT_MARKER}


SchemaMarker = Literal["1.0", "1.1", "2"]


def migrate_config_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Upgrade *payload* to the current schema marker.

    Args:
        payload: Parsed YAML body from one layer file. The mapping is
            not mutated; the helper returns a fresh copy.

    Returns:
        ``(upgraded, changed)`` — ``upgraded`` is the new body keyed at
        ``schema_version: "1.0"``. ``changed`` is ``True`` when the
        input marker was a legacy value (so callers know to write the
        upgrade back to disk + emit an envelope); ``False`` when the
        input was already ``"1.0"`` and the migration was a no-op.

    Raises:
        ValidationError: When ``schema_version`` is missing entirely,
            or is a value outside :data:`ACCEPTED_MARKERS`. Missing
            markers are rejected because the legacy code path always
            wrote one; an absent marker indicates corruption.
    """
    if "schema_version" not in payload:
        raise ValidationError("config layer missing required key 'schema_version'")
    raw_marker = payload["schema_version"]
    marker = str(raw_marker)
    if marker not in ACCEPTED_MARKERS:
        raise ValidationError(
            f"unknown config schema_version: {raw_marker!r} (accepted: {sorted(ACCEPTED_MARKERS)})"
        )

    upgraded: dict[str, Any] = _deep_copy(payload)
    if marker == CURRENT_MARKER:
        # On-marker "1.0" payloads may still carry legacy keys from old
        # wizard runs (top-level ``lifecycle`` / ``plugins`` blocks,
        # ``runtime.kind`` without ``runtime.adapters``). The
        # cleanup step below is idempotent: when the body is already
        # canonical, ``changed`` stays ``False`` and the call is a
        # no-op as advertised.
        cleaned = _cleanup_legacy_keys(upgraded)
        return upgraded, cleaned

    # Legacy → "1.0" upgrade. Apply each fix in order; every step is
    # idempotent on its own so re-running mid-migration is safe.
    upgraded["schema_version"] = CURRENT_MARKER
    _cleanup_legacy_keys(upgraded)
    _shim_runtime_preference(upgraded)
    _ensure_section(upgraded, "config", {"layers_visible": True})
    _ensure_section(
        upgraded,
        "telemetry",
        {
            "enabled": False,
            "export": {"format": "prom"},
            "window_default": "7d",
            "aggregate_window": "24h",
            "db_kind": "sqlite",
        },
    )
    _ensure_section(
        upgraded,
        "dispatch",
        {
            "session_policy_default": "hybrid",
            "session_handle_ttl_seconds": 86400,
        },
    )
    _ensure_section(
        upgraded,
        "language",
        {"runtime": "python", "fast_extras": []},
    )
    _ensure_subsection(
        upgraded,
        parent="runtime",
        child="fallback",
        default={
            "on_errors": [
                "RUNTIME_RATE_LIMIT",
                "RUNTIME_SERVER_ERROR",
                "RUNTIME_TIMEOUT",
                "RUNTIME_API_ERROR",
            ],
            "retry_policy": "hybrid",
            "max_backoff_seconds": 90,
        },
    )
    _ensure_subkey(upgraded, parent="profiles", key="trusted", default={})
    _ensure_subkey(upgraded, parent="project", key="goals", default=[])
    _ensure_subkey(upgraded, parent="project", key="success_metrics", default={})

    logger.info(f"migrate_config_payload upgraded from={marker!r} to={CURRENT_MARKER!r}")
    return upgraded, True


def migrate_config_file(path: Path) -> tuple[dict[str, Any], bool, Path | None]:
    """Read *path*, upgrade to the current marker, write back if changed.

    Args:
        path: Filesystem location of one layer YAML.

    Returns:
        ``(upgraded, changed, backup_path)``. ``upgraded`` is the new
        body. ``changed`` is ``True`` when a legacy marker was seen.
        ``backup_path`` is the location of the rollback copy
        (``<path>.bak.<old-marker>.<unix-epoch>``) when a write
        occurred; ``None`` otherwise.

    Raises:
        ValidationError: When the YAML cannot be parsed, the body is
            not a mapping, or the schema marker is unrecognised.
    """
    if not path.exists():
        # An absent file is treated as an empty layer; nothing to upgrade.
        return {}, False, None

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"cannot read config file {path}: {exc}") from exc

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValidationError(f"malformed YAML in {path}: {exc}") from exc

    if parsed is None:
        return {}, False, None
    if not isinstance(parsed, dict):
        raise ValidationError(
            f"config file {path} must contain a YAML mapping at the top "
            f"level, got {type(parsed).__name__}"
        )

    old_marker = str(parsed.get("schema_version", "<missing>"))
    upgraded, changed = migrate_config_payload(parsed)
    if not changed:
        return upgraded, False, None

    backup = path.with_name(f"{path.name}.bak.{old_marker}.{int(time.time())}")
    backup.write_text(raw, encoding="utf-8")
    path.write_text(
        yaml.safe_dump(upgraded, sort_keys=True, default_flow_style=False),
        encoding="utf-8",
    )
    logger.info(
        f"migrate_config_file path={path} from={old_marker!r} to={CURRENT_MARKER!r} backup={backup}"
    )
    return upgraded, True, backup


# --- internal helpers -------------------------------------------------------


def _deep_copy(value: Any) -> Any:
    """Recursive plain-dict / list copy. Avoids ``copy.deepcopy`` overhead.

    Inputs are guaranteed to be YAML-derived (dict/list/scalar) so the
    handwritten variant is fine.
    """
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def _ensure_section(
    payload: dict[str, Any],
    section: str,
    default: dict[str, Any],
) -> None:
    """Insert *default* at ``payload[section]`` when absent.

    When the section already exists, the helper deep-fills missing
    leaves under it (e.g. operator set ``telemetry.enabled`` but not
    the other keys) so the post-migration body has every C08 leaf
    addressable.
    """
    existing = payload.get(section)
    if not isinstance(existing, dict):
        payload[section] = _deep_copy(default)
        return
    for key, value in default.items():
        if isinstance(value, dict) and isinstance(existing.get(key), dict):
            _ensure_section(existing, key, value)
        elif key not in existing:
            existing[key] = _deep_copy(value)


def _ensure_subsection(
    payload: dict[str, Any],
    *,
    parent: str,
    child: str,
    default: dict[str, Any],
) -> None:
    """Insert ``payload[parent][child] = default`` when absent.

    Creates ``payload[parent]`` first if needed so the operation is
    safe on payloads that omitted the entire parent block.
    """
    if not isinstance(payload.get(parent), dict):
        payload[parent] = {}
    parent_block = payload[parent]
    if not isinstance(parent_block.get(child), dict):
        parent_block[child] = _deep_copy(default)
        return
    for key, value in default.items():
        if key not in parent_block[child]:
            parent_block[child][key] = _deep_copy(value)


def _ensure_subkey(
    payload: dict[str, Any],
    *,
    parent: str,
    key: str,
    default: Any,
) -> None:
    """Insert ``payload[parent][key] = default`` when absent."""
    if not isinstance(payload.get(parent), dict):
        payload[parent] = {}
    if key not in payload[parent]:
        payload[parent][key] = _deep_copy(default)


def _cleanup_legacy_keys(payload: dict[str, Any]) -> bool:
    """Strip legacy C08-superseded keys + rewrite ``runtime.kind``.

    Three signals identify a legacy body irrespective of the
    ``schema_version`` marker:

    1. Top-level ``lifecycle`` block — the pre-C08 wizard wrote
       ``{depth: phase}`` here; nothing in the runtime reads it any
       more (the wave allocator pins depth from the iter, not config).
    2. Top-level ``plugins`` block — superseded by the per-runtime
       ``runtime.adapter_catalog`` map.
    3. ``runtime.kind`` scalar — superseded by ``runtime.adapters``
       (list) + ``runtime.preference`` (ordered fallback). The shim in
       :mod:`eawf.config.layered` warns on every CLI invocation until
       the on-disk file drops the legacy key.

    The cleanup is idempotent: a body that already passes all three
    checks returns ``False`` and the input is untouched.

    Returns:
        ``True`` when at least one legacy key was removed or
        rewritten; ``False`` when the body was already canonical.
    """
    changed = False

    if "lifecycle" in payload:
        del payload["lifecycle"]
        changed = True

    if "plugins" in payload:
        del payload["plugins"]
        changed = True

    # ``telemetry.export.endpoint`` was dropped when telemetry became
    # strict-local (no external export target); strip the orphan leaf so
    # the daemon's unknown-config-key gate does not reject the migrated body.
    telemetry = payload.get("telemetry")
    if isinstance(telemetry, dict):
        export = telemetry.get("export")
        if isinstance(export, dict) and "endpoint" in export:
            del export["endpoint"]
            changed = True

    runtime = payload.get("runtime")
    if isinstance(runtime, dict) and "kind" in runtime:
        kind = runtime.pop("kind")
        adapters = runtime.get("adapters")
        if not (isinstance(adapters, list) and adapters):
            # Promote the legacy scalar so the merged config still
            # carries a usable adapter selector.
            runtime["adapters"] = [kind]
        changed = True

    # Ensure ``runtime.preference`` shadows ``adapters`` whenever the
    # cleanup synthesised the adapter list — keeps the C08 ladder
    # populated without a second migration pass.
    if changed:
        _shim_runtime_preference(payload)

    return changed


def _shim_runtime_preference(payload: dict[str, Any]) -> None:
    """Synthesise ``runtime.preference`` from ``runtime.adapters`` when absent.

    The 1.1 → 1.0 (C08) upgrade introduces ``preference`` as the new
    canonical fallback-ladder selector. When the legacy payload set
    ``adapters`` but never gained ``preference`` (because nobody had
    typed it yet), the migrator promotes the list verbatim.
    """
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        return
    if isinstance(runtime.get("preference"), list) and runtime["preference"]:
        return
    adapters = runtime.get("adapters")
    if isinstance(adapters, list) and adapters:
        runtime["preference"] = list(adapters)


__all__ = [
    "ACCEPTED_MARKERS",
    "CURRENT_MARKER",
    "LEGACY_MARKERS",
    "SchemaMarker",
    "migrate_config_file",
    "migrate_config_payload",
]
