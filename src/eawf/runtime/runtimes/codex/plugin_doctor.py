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
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from eawf.runtime.runtimes.codex.hook_map import (
    codex_hook_event_name,
    codex_hook_name,
)
from eawf.runtime.runtimes.codex.plugin_install import (
    _DEFAULT_TIMESTAMP,
    Scope,
    _agent_target,
    _codex_hook_specs,
    _config_target,
    _hook_config_target,
    _hook_target,
    _hook_timeout_seconds,
    _manifest_target,
    _plugin_root,
    _render_agent_toml,
    _render_hook_config,
    _render_manifest,
    _render_sidecar,
    _render_skill,
    _sidecar_fingerprint,
    _sidecar_target,
    _skill_target,
)
from eawf.surfaces.render.agents import AGENT_REGISTRY
from eawf.surfaces.render.hooks import HookSpec
from eawf.surfaces.render.skills import SKILL_REGISTRY


@dataclass(frozen=True)
class DoctorEntry:
    """One file inspected by :func:`doctor_plugin`."""

    region_id: str
    path: Path
    kind: str  # Literal["skill", "agent", "hook", "config", "manifest", "sidecar"]
    on_disk_hash: str | None = None
    expected_hash: str | None = None


HookTrustStatus = Literal["trusted", "untrusted", "disabled", "unavailable"]


@dataclass(frozen=True)
class HookTrustEntry:
    """Codex trust state for one installed Eä hook command."""

    event_name: str
    state_key: str
    status: HookTrustStatus
    reason: str


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
    hook_trust: list[HookTrustEntry] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return (
            not self.drifted
            and not self.missing
            and all(entry.status == "trusted" for entry in self.hook_trust)
        )


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


def _detect_legacy_paths(
    target_dir: Path,
    *,
    plugin_root: Path,
    scope: Scope,
    home: Path | None,
) -> list[Path]:
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
    if scope == "user":
        base = home if home is not None else Path.home()
        direct_root = base / ".codex" / "plugins" / "eawf"
        if direct_root.is_dir() and direct_root.resolve() != plugin_root.resolve():
            legacy.append(direct_root)
    return legacy


def _read_hook_trust_state(
    home: Path | None,
) -> tuple[dict[str, object] | None, str | None]:
    codex_home = home if home is not None else Path.home()
    config_path = codex_home / ".codex" / "config.toml"
    if not config_path.is_file():
        return None, "Codex user config is missing"
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"Codex trust state is unreadable: {type(exc).__name__}"
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return {}, None
    raw_state = hooks.get("state")
    return (raw_state if isinstance(raw_state, dict) else {}), None


def _normalized_hook_hash(spec: HookSpec) -> str:
    """Return Codex's current normalized trust hash for *spec*.

    Codex hashes a config-derived handler identity, not source JSON. Its
    normalization resolves the command timeout, omits absent matcher and
    optional handler fields, canonicalizes JSON object keys recursively, then
    prefixes the SHA-256 digest with ``sha256:``.
    """
    identity = {
        "event_name": spec.event_type.value,
        "hooks": [
            {
                "async": False,
                "command": f'"${{PLUGIN_ROOT}}/hooks/{codex_hook_name(spec.event_type)}.sh"',
                "timeout": _hook_timeout_seconds(spec),
                "type": "command",
            }
        ],
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _hook_trust_entry(
    spec: HookSpec,
    *,
    state: dict[str, object] | None,
    unavailable_reason: str | None,
) -> HookTrustEntry:
    event_name = codex_hook_event_name(spec.event_type)
    state_key = f"eawf@eawf:hooks/hooks.json:{spec.event_type.value}:0:0"
    if unavailable_reason is not None:
        return HookTrustEntry(
            event_name=event_name,
            state_key=state_key,
            status="unavailable",
            reason=unavailable_reason,
        )
    assert state is not None
    raw_entry = state.get(state_key)
    if not isinstance(raw_entry, dict):
        return HookTrustEntry(
            event_name=event_name,
            state_key=state_key,
            status="untrusted",
            reason="hook needs review in Codex /hooks",
        )
    if raw_entry.get("enabled") is False:
        return HookTrustEntry(
            event_name=event_name,
            state_key=state_key,
            status="disabled",
            reason="hook is disabled in Codex /hooks",
        )
    trusted_hash = raw_entry.get("trusted_hash")
    if not isinstance(trusted_hash, str) or not trusted_hash:
        return HookTrustEntry(
            event_name=event_name,
            state_key=state_key,
            status="untrusted",
            reason="hook needs review in Codex /hooks",
        )
    current_hash = _normalized_hook_hash(spec)
    if trusted_hash != current_hash:
        return HookTrustEntry(
            event_name=event_name,
            state_key=state_key,
            status="untrusted",
            reason="hook changed since approval; review in Codex /hooks",
        )
    return HookTrustEntry(
        event_name=event_name,
        state_key=state_key,
        status="trusted",
        reason="persisted Codex trust hash matches current hook",
    )


def _hook_trust_entries(home: Path | None) -> list[HookTrustEntry]:
    """Read Codex's persisted per-hook trust/enablement state.

    Codex owns trust changes through its ``/hooks`` browser. Eä only reads the
    public config state so doctor can explain why an installed hook will be
    skipped. The doctor mirrors Codex's normalized hash computation for
    comparison but never writes trust state.
    """
    state, unavailable_reason = _read_hook_trust_state(home)
    return [
        _hook_trust_entry(
            spec,
            state=state,
            unavailable_reason=unavailable_reason,
        )
        for spec in _codex_hook_specs()
    ]


def doctor_plugin(
    target_dir: Path,
    *,
    scope: Scope = "project",
    home: Path | None = None,
    plugin_root: Path | None = None,
) -> DoctorReport:
    """Inspect the installed Codex plugin tree at *scope*.

    The body of each skill / hook / manifest is recomputed from the
    same registries the installer uses. ``config.toml`` is not
    body-compared (user is free to author unrelated TOML sections);
    presence-only check. The sidecar (``.eawf-managed.json``) is
    semantically compared excluding its timestamp/hash pair.
    """
    target_dir = Path(target_dir).resolve()
    plugin_root = _plugin_root(
        target_dir,
        scope=scope,
        home=home,
        plugin_root=plugin_root,
    )
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
    for hook_spec in _codex_hook_specs():
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
        _hook_config_target(plugin_root),
        region_id="plugin.codex.hook_config",
        kind="hook_config",
        expected_body=_render_hook_config(),
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

    legacy = _detect_legacy_paths(
        target_dir,
        plugin_root=plugin_root,
        scope=scope,
        home=home,
    )
    return DoctorReport(
        target_dir=target_dir,
        plugin_root=plugin_root,
        scope=scope,
        ok=ok,
        drifted=drifted,
        missing=missing,
        legacy_paths=legacy,
        hook_trust=_hook_trust_entries(home),
    )


__all__ = [
    "DoctorEntry",
    "DoctorReport",
    "HookTrustEntry",
    "HookTrustStatus",
    "doctor_plugin",
]
