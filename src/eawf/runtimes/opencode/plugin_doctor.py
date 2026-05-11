"""Drift inspector for the Eä-owned OpenCode plugin tree (P14-W07)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from eawf.runtimes.opencode.plugin_install import (
    _config_target,
    _plugin_js_target,
    expected_plugin_js_bytes,
)


@dataclass(frozen=True)
class DoctorEntry:
    """One file inspected by :func:`doctor_plugin`."""

    region_id: str
    path: Path
    kind: str  # Literal["plugin_js", "config"]
    on_disk_hash: str | None = None
    expected_hash: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """Summary of one :func:`doctor_plugin` call."""

    target_dir: Path
    ok: list[DoctorEntry] = field(default_factory=list)
    drifted: list[DoctorEntry] = field(default_factory=list)
    missing: list[DoctorEntry] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.drifted and not self.missing


def _hash_bytes(payload: bytes) -> str:
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


def doctor_plugin(target_dir: Path) -> DoctorReport:
    """Inspect the installed OpenCode plugin tree under *target_dir*.

    ``plugin.js`` is compared byte-for-byte against the template asset
    (stamped with the plugin version). ``opencode.json`` is examined
    for the presence of the ``__eawf_managed`` namespace plus a
    ``plugin_js_hash`` that matches the freshly-rendered template; a
    drift in either flips the entry into ``drifted``.
    """
    target_dir = Path(target_dir).resolve()
    ok: list[DoctorEntry] = []
    drifted: list[DoctorEntry] = []
    missing: list[DoctorEntry] = []

    plugin_js_path = _plugin_js_target(target_dir)
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

    config_path = _config_target(target_dir)
    if not config_path.exists():
        missing.append(
            DoctorEntry(
                region_id="plugin.opencode.config",
                path=config_path,
                kind="config",
            )
        )
    else:
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            parsed = None
        managed = parsed.get("__eawf_managed") if isinstance(parsed, dict) else None
        stored_js_hash = None
        if isinstance(managed, dict):
            stored_js_hash = managed.get("plugin_js_hash")
        entry = DoctorEntry(
            region_id="plugin.opencode.config",
            path=config_path,
            kind="config",
            on_disk_hash=stored_js_hash,
            expected_hash=expected_js_hash,
        )
        if stored_js_hash == expected_js_hash:
            ok.append(entry)
        else:
            drifted.append(entry)

    return DoctorReport(target_dir=target_dir, ok=ok, drifted=drifted, missing=missing)


__all__ = [
    "DoctorEntry",
    "DoctorReport",
    "doctor_plugin",
]
