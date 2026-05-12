"""Drift inspector for the Eä-owned OpenCode plugin (P14-I02-W01).

Scope-aware: ``scope="project"`` inspects ``<target>/.opencode/plugins/``;
``scope="user"`` inspects ``$OPENCODE_CONFIG_DIR/plugins/`` or
``<home>/.config/opencode/plugins/``. Reports legacy-layout paths
(``<target>/plugin.js`` plus ``opencode.json`` carrying an
``__eawf_managed`` key or a ``plugins`` array entry referencing the
legacy file) under ``legacy_paths``; the doctor never auto-deletes
them (AGENTS.md deletion rule).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from eawf.runtimes.opencode.plugin_install import (
    Scope,
    _config_target,
    _plugin_js_target,
    _sidecar_target,
    expected_plugin_js_bytes,
)


@dataclass(frozen=True)
class DoctorEntry:
    """One file inspected by :func:`doctor_plugin`."""

    region_id: str
    path: Path
    kind: str  # Literal["plugin_js", "config", "sidecar"]
    on_disk_hash: str | None = None
    expected_hash: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """Summary of one :func:`doctor_plugin` call."""

    target_dir: Path
    scope: Scope = "project"
    ok: list[DoctorEntry] = field(default_factory=list)
    drifted: list[DoctorEntry] = field(default_factory=list)
    missing: list[DoctorEntry] = field(default_factory=list)
    legacy_paths: list[Path] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.drifted and not self.missing


def _hash_bytes(payload: bytes) -> str:
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


def _detect_legacy_paths(target_dir: Path) -> list[Path]:
    """Return legacy workspace-root paths the old installer used to drop.

    Reported but never auto-deleted. Includes ``<target>/plugin.js``
    when present, and ``<target>/opencode.json`` when its top level
    contains an ``__eawf_managed`` key or a ``plugins`` array referring
    to ``plugin.js``.
    """
    legacy: list[Path] = []
    flat_plugin = target_dir / "plugin.js"
    if flat_plugin.is_file():
        legacy.append(flat_plugin)
    flat_config = target_dir / "opencode.json"
    if flat_config.is_file():
        try:
            parsed = json.loads(flat_config.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            plugins = parsed.get("plugins")
            has_managed_key = "__eawf_managed" in parsed
            has_plugin_js_array_entry = isinstance(plugins, list) and "plugin.js" in plugins
            if has_managed_key or has_plugin_js_array_entry:
                legacy.append(flat_config)
    return legacy


def doctor_plugin(
    target_dir: Path,
    *,
    scope: Scope = "project",
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> DoctorReport:
    """Inspect the installed OpenCode plugin at *scope*.

    ``eawf.js`` is compared byte-for-byte against the template asset
    (stamped with the plugin version). The sidecar
    (``.eawf-managed.json``) is presence-checked with its on-disk hash
    recorded. ``opencode.json`` is presence-checked (user is free to
    author unrelated top-level keys).
    """
    target_dir = Path(target_dir).resolve()
    ok: list[DoctorEntry] = []
    drifted: list[DoctorEntry] = []
    missing: list[DoctorEntry] = []

    plugin_js_path = _plugin_js_target(
        target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir
    )
    expected_js_bytes = expected_plugin_js_bytes()
    expected_js_hash = _hash_bytes(expected_js_bytes)
    if not plugin_js_path.exists():
        missing.append(
            DoctorEntry(
                region_id="plugin.opencode.plugin_js",
                path=plugin_js_path,
                kind="plugin_js",
                expected_hash=expected_js_hash,
            )
        )
    else:
        live = plugin_js_path.read_bytes()
        live_hash = _hash_bytes(live)
        entry = DoctorEntry(
            region_id="plugin.opencode.plugin_js",
            path=plugin_js_path,
            kind="plugin_js",
            on_disk_hash=live_hash,
            expected_hash=expected_js_hash,
        )
        if live_hash == expected_js_hash:
            ok.append(entry)
        else:
            drifted.append(entry)

    sidecar_path = _sidecar_target(
        target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir
    )
    if not sidecar_path.exists():
        missing.append(
            DoctorEntry(
                region_id="plugin.opencode.sidecar",
                path=sidecar_path,
                kind="sidecar",
                expected_hash=expected_js_hash,
            )
        )
    else:
        try:
            sidecar_parsed = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sidecar_parsed = None
        stored_js_hash = None
        if isinstance(sidecar_parsed, dict):
            stored_js_hash = sidecar_parsed.get("plugin_js_hash")
        entry = DoctorEntry(
            region_id="plugin.opencode.sidecar",
            path=sidecar_path,
            kind="sidecar",
            on_disk_hash=stored_js_hash,
            expected_hash=expected_js_hash,
        )
        if stored_js_hash == expected_js_hash:
            ok.append(entry)
        else:
            drifted.append(entry)

    config_path = _config_target(
        target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir
    )
    if not config_path.exists():
        missing.append(
            DoctorEntry(
                region_id="plugin.opencode.config",
                path=config_path,
                kind="config",
            )
        )
    else:
        on_disk_hash = _hash_bytes(config_path.read_bytes())
        ok.append(
            DoctorEntry(
                region_id="plugin.opencode.config",
                path=config_path,
                kind="config",
                on_disk_hash=on_disk_hash,
                expected_hash=None,
            )
        )

    legacy = _detect_legacy_paths(target_dir) if scope == "project" else []
    return DoctorReport(
        target_dir=target_dir,
        scope=scope,
        ok=ok,
        drifted=drifted,
        missing=missing,
        legacy_paths=legacy,
    )


__all__ = [
    "DoctorEntry",
    "DoctorReport",
    "doctor_plugin",
]
