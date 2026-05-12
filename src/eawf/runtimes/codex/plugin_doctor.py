"""Drift inspector for the Eä-owned Codex CLI plugin tree (P14-W06).

Mirrors :mod:`eawf.runtimes.claude.plugin_doctor`. Returns a structured
:class:`DoctorReport` enumerating ok / drifted / missing entries.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from eawf.render.agents import AGENT_REGISTRY
from eawf.render.hooks import HOOK_REGISTRY
from eawf.render.skills import SKILL_REGISTRY
from eawf.runtimes.codex.plugin_install import (
    _agent_target,
    _config_target,
    _hook_target,
    _render_agent,
    _render_skill,
    _skill_target,
)


@dataclass(frozen=True)
class DoctorEntry:
    """One file inspected by :func:`doctor_plugin`."""

    region_id: str
    path: Path
    kind: str  # Literal["skill", "agent", "hook", "config"]
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


def doctor_plugin(target_dir: Path) -> DoctorReport:
    """Inspect the installed Codex plugin tree under *target_dir*.

    The body of each file is recomputed from the same registries the
    installer uses. ``config.toml`` is not body-compared (the user is
    free to author unrelated TOML sections); presence-only check.
    """
    target_dir = Path(target_dir).resolve()
    ok: list[DoctorEntry] = []
    drifted: list[DoctorEntry] = []
    missing: list[DoctorEntry] = []
    for skill_spec in SKILL_REGISTRY:
        _classify_entry(
            _skill_target(target_dir, skill_spec),
            region_id=f"plugin.codex.skill.{skill_spec.skill_name}",
            kind="skill",
            expected_body=_render_skill(skill_spec).encode("utf-8"),
            ok=ok,
            drifted=drifted,
            missing=missing,
        )
    for agent_spec in AGENT_REGISTRY:
        _classify_entry(
            _agent_target(target_dir, agent_spec),
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
            _hook_target(target_dir, hook_spec),
            region_id=f"plugin.codex.hook.{hook_spec.event_type.value}",
            kind="hook",
            expected_body=render_hook_sh(hook_spec.event_type).encode("utf-8"),
            ok=ok,
            drifted=drifted,
            missing=missing,
        )
    config_path = _config_target(target_dir)
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
    return DoctorReport(target_dir=target_dir, ok=ok, drifted=drifted, missing=missing)


__all__ = [
    "DoctorEntry",
    "DoctorReport",
    "doctor_plugin",
]
