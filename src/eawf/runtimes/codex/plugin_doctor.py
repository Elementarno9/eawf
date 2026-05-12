"""Drift inspector for the Eä-owned Codex CLI plugin (P14-I02-W01).

Mirrors :mod:`eawf.runtimes.claude.plugin_doctor`. Returns a structured
:class:`DoctorReport` enumerating ok / drifted / missing entries. Scope
aware — ``scope="project"`` inspects ``<target>/.codex/plugins/eawf/``;
``scope="user"`` inspects ``<home>/.codex/plugins/eawf/``.

Reports legacy-layout paths under ``legacy_paths`` when the previous
flat ``<target>/.codex/{skills,agents,hooks}/`` tree is present
alongside the new plugin-rooted layout. Per the AGENTS.md deletion
rule, the doctor never auto-removes legacy files; it surfaces them so
the operator can prune manually.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from eawf.render.agents import AGENT_REGISTRY
from eawf.render.hooks import HOOK_REGISTRY
from eawf.render.skills import SKILL_REGISTRY
from eawf.runtimes.codex.plugin_install import (
    Scope,
    _agent_target,
    _config_target,
    _hook_target,
    _manifest_target,
    _plugin_root,
    _render_agent,
    _render_manifest,
    _render_skill,
    _sidecar_target,
    _skill_target,
)


@dataclass(frozen=True)
class DoctorEntry:
    """One file inspected by :func:`doctor_plugin`."""

    region_id: str
    path: Path
    kind: str  # Literal["skill", "agent", "hook", "config", "manifest", "sidecar"]
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


def _classify_entry(
    path: Path,
    *,
    region_id: str,
    kind: str,
    expected_body: bytes,
    ok: list[DoctorEntry],
    drifted: list[DoctorEntry],
    missing: list[DoctorEntry],
) -> None:
    expected_hash = _hash_bytes(expected_body)
    if not path.exists():
        missing.append(DoctorEntry(region_id=region_id, path=path, kind=kind))
        return
    on_disk_hash = _hash_bytes(path.read_bytes())
    if on_disk_hash == expected_hash:
        ok.append(
            DoctorEntry(
                region_id=region_id,
                path=path,
                kind=kind,
                on_disk_hash=on_disk_hash,
                expected_hash=expected_hash,
            )
        )
    else:
        drifted.append(
            DoctorEntry(
                region_id=region_id,
                path=path,
                kind=kind,
                on_disk_hash=on_disk_hash,
                expected_hash=expected_hash,
            )
        )


def _detect_legacy_paths(target_dir: Path) -> list[Path]:
    """Return any flat-layout paths under ``<target>/.codex/{skills,agents,hooks}/``.

    Reported but never auto-deleted (AGENTS.md deletion rule).
    """
    legacy: list[Path] = []
    flat_root = target_dir / ".codex"
    for sub in ("skills", "agents", "hooks"):
        candidate = flat_root / sub
        if candidate.is_dir():
            legacy.append(candidate)
    return legacy


def doctor_plugin(
    target_dir: Path,
    *,
    scope: Scope = "project",
    home: Path | None = None,
) -> DoctorReport:
    """Inspect the installed Codex plugin tree at *scope*.

    The body of each skill / agent / hook / manifest is recomputed from
    the same registries the installer uses. ``config.toml`` is not
    body-compared (user is free to author unrelated TOML sections);
    presence-only check. The sidecar (``.eawf-managed.json``) is
    body-compared excluding its timestamp.
    """
    target_dir = Path(target_dir).resolve()
    plugin_root = _plugin_root(target_dir, scope=scope, home=home)
    ok: list[DoctorEntry] = []
    drifted: list[DoctorEntry] = []
    missing: list[DoctorEntry] = []
    for skill_spec in SKILL_REGISTRY:
        _classify_entry(
            _skill_target(plugin_root, skill_spec),
            region_id=f"plugin.codex.skill.{skill_spec.skill_name}",
            kind="skill",
            expected_body=_render_skill(skill_spec).encode("utf-8"),
            ok=ok,
            drifted=drifted,
            missing=missing,
        )
    for agent_spec in AGENT_REGISTRY:
        _classify_entry(
            _agent_target(plugin_root, agent_spec),
            region_id=f"plugin.codex.agent.{agent_spec.role}",
            kind="agent",
            expected_body=_render_agent(agent_spec).encode("utf-8"),
            ok=ok,
            drifted=drifted,
            missing=missing,
        )
    for hook_spec in HOOK_REGISTRY:
        from eawf.render.hooks import render_hook_sh

        _classify_entry(
            _hook_target(plugin_root, hook_spec),
            region_id=f"plugin.codex.hook.{hook_spec.event_type.value}",
            kind="hook",
            expected_body=render_hook_sh(hook_spec.event_type).encode("utf-8"),
            ok=ok,
            drifted=drifted,
            missing=missing,
        )

    _classify_entry(
        _manifest_target(plugin_root),
        region_id="plugin.codex.manifest",
        kind="manifest",
        expected_body=_render_manifest(),
        ok=ok,
        drifted=drifted,
        missing=missing,
    )

    sidecar_path = _sidecar_target(plugin_root)
    if not sidecar_path.exists():
        missing.append(
            DoctorEntry(region_id="plugin.codex.sidecar", path=sidecar_path, kind="sidecar")
        )
    else:
        ok.append(
            DoctorEntry(
                region_id="plugin.codex.sidecar",
                path=sidecar_path,
                kind="sidecar",
                on_disk_hash=_hash_bytes(sidecar_path.read_bytes()),
                expected_hash=None,
            )
        )

    config_path = _config_target(target_dir, scope=scope, home=home)
    if not config_path.exists():
        missing.append(
            DoctorEntry(region_id="plugin.codex.config", path=config_path, kind="config")
        )
    else:
        on_disk_hash = _hash_bytes(config_path.read_bytes())
        ok.append(
            DoctorEntry(
                region_id="plugin.codex.config",
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
