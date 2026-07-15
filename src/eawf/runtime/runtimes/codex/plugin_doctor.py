"""Drift inspector for the Eä-owned Codex CLI plugin (P14-I02-W01).

Mirrors :mod:`eawf.runtime.runtimes.claude.plugin_doctor`. Returns a structured
:class:`DoctorReport` enumerating ok / drifted / missing entries. Scope
aware — ``scope="project"`` inspects ``<target>/.codex/plugins/eawf/``;
``scope="user"`` inspects ``<home>/.codex/plugins/eawf/``.

Reports legacy-layout paths under ``legacy_paths`` when previous flat
skill/hook paths or the removed plugin-rooted ``agents/`` tree are
present. Per the AGENTS.md deletion rule, the doctor never auto-removes
legacy files; it surfaces them so the operator can prune manually.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from eawf.runtime.runtimes.codex.plugin_install import (
    _DEFAULT_TIMESTAMP,
    Scope,
    _agent_target,
    _config_target,
    _hook_target,
    _manifest_target,
    _plugin_root,
    _render_agent_toml,
    _render_manifest,
    _render_sidecar,
    _render_skill,
    _sidecar_fingerprint,
    _sidecar_target,
    _skill_target,
)
from eawf.surfaces.render.agents import AGENT_REGISTRY
from eawf.surfaces.render.hooks import HOOK_REGISTRY
from eawf.surfaces.render.skills import SKILL_REGISTRY


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
    plugin_root: Path
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


def _detect_legacy_paths(target_dir: Path, *, plugin_root: Path) -> list[Path]:
    """Return stale Codex plugin paths from prior installer layouts.

    ``.codex/agents`` is no longer legacy: it is the current Codex
    custom-agent surface. Reported paths are never auto-deleted
    (AGENTS.md deletion rule).
    """
    legacy: list[Path] = []
    flat_root = target_dir / ".codex"
    for sub in ("skills", "hooks"):
        candidate = flat_root / sub
        if candidate.is_dir():
            legacy.append(candidate)
    plugin_agents = plugin_root / "agents"
    if plugin_agents.is_dir():
        legacy.append(plugin_agents)
    return legacy


def doctor_plugin(
    target_dir: Path,
    *,
    scope: Scope = "project",
    home: Path | None = None,
) -> DoctorReport:
    """Inspect the installed Codex plugin tree at *scope*.

    The body of each skill / hook / manifest is recomputed from the
    same registries the installer uses. ``config.toml`` is not
    body-compared (user is free to author unrelated TOML sections);
    presence-only check. The sidecar (``.eawf-managed.json``) is
    semantically compared excluding its timestamp/hash pair.
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
            _agent_target(target_dir, agent_spec, scope=scope, home=home),
            region_id=f"plugin.codex.agent.{agent_spec.role}",
            kind="agent",
            expected_body=_render_agent_toml(agent_spec).encode("utf-8"),
            ok=ok,
            drifted=drifted,
            missing=missing,
        )
    for hook_spec in HOOK_REGISTRY:
        from eawf.surfaces.render.hooks import render_hook_sh

        _classify_entry(
            _hook_target(plugin_root, hook_spec),
            region_id=f"plugin.codex.hook.{hook_spec.event_type.value}",
            kind="hook",
            expected_body=render_hook_sh(hook_spec.event_type, runtime="codex").encode("utf-8"),
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
        expected_sidecar_hash = _sidecar_fingerprint(_render_sidecar(_DEFAULT_TIMESTAMP))
        on_disk_hash = _sidecar_fingerprint(sidecar_path.read_bytes())
        entry = DoctorEntry(
            region_id="plugin.codex.sidecar",
            path=sidecar_path,
            kind="sidecar",
            on_disk_hash=on_disk_hash,
            expected_hash=expected_sidecar_hash,
        )
        if on_disk_hash == expected_sidecar_hash:
            ok.append(entry)
        else:
            drifted.append(entry)

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

    legacy = _detect_legacy_paths(target_dir, plugin_root=plugin_root) if scope == "project" else []
    return DoctorReport(
        target_dir=target_dir,
        plugin_root=plugin_root,
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
