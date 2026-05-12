"""Render an Eä-owned Codex CLI plugin under ``.codex/plugins/eawf/``.

Native Codex layout (P14-I02-W01) replaces the prior flat ``.codex/{skills,
agents,hooks}/`` dump. Output under *plugin_root*:

::

    <plugin_root>/
      .codex-plugin/
        plugin.json              # canonical Codex manifest
        .eawf-managed.json       # sidecar — hash registry for doctor
      skills/<name>/SKILL.md
      hooks/<event>.sh

The scope-correct ``config.toml`` (``<target>/.codex/config.toml`` for
project scope, ``<home>/.codex/config.toml`` for user scope) is patched
between the ``# ---- __eawf_managed begin/end ----`` markers with a
single ``[plugins.eawf] enabled = true`` table. User-authored TOML
outside the markers is preserved verbatim.

The Codex manifest schema (``name``, ``version``, ``description``,
``skills``, ``hooks``) is taken from the Codex Build-plugin reference.
Codex's ``plugin.json`` schema has no ``agents`` key — agents live
nested inside skills (per the skill manifest), so a top-level
``agents/<role>.md`` render is unreachable and intentionally omitted.

Public API:

    InstallResult                                       (per-file deltas)
    install_plugin(target_dir, *, scope=..., ...)       → InstallResult
    expected_paths(target_dir, *, scope=...)            → ({region_id: Path}, config_path)
    IntegrityViolation                                  (raised on managed-file hand-edits)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from eawf.render._atomic import atomic_write_text
from eawf.render.hooks import HOOK_REGISTRY, HookSpec, render_hook_sh
from eawf.render.skills import (
    SKILL_REGISTRY,
    SkillSpec,
    SkillTemplateContext,
    render_skill_md,
)
from eawf.runtimes.codex.hook_map import codex_hook_name

logger = logging.getLogger(__name__)


Scope = Literal["project", "user"]

_PLUGIN_NAME: str = "eawf"
_PLUGIN_VERSION: str = "1.0"
_PLUGIN_DESCRIPTION: str = (
    "Eä Workflow plugin — agent-driven development skills, agents, and hooks."
)
_GENERATOR: str = "eawf-plugin-codex"
_MANAGED_TABLE: str = "__eawf_managed"
_HOOK_FILE_MODE: int = 0o755
_DEFAULT_TIMESTAMP: str = "1970-01-01T00:00:00+00:00"
_MANIFEST_DIR: str = ".codex-plugin"
_MANIFEST_FILE: str = "plugin.json"
_SIDECAR_FILE: str = ".eawf-managed.json"


@dataclass(frozen=True)
class FileDelta:
    """One file the installer wrote / would have written."""

    path: Path
    action: str  # Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class InstallResult:
    """Summary of one :func:`install_plugin` call."""

    target_dir: Path
    scope: Scope = "project"
    skills: list[FileDelta] = field(default_factory=list)
    hooks: list[FileDelta] = field(default_factory=list)
    manifest: FileDelta | None = None
    sidecar: FileDelta | None = None
    config: FileDelta | None = None
    dry_run: bool = False


class IntegrityViolation(Exception):  # noqa: N818 — mirrors eawf.cli.errors.IntegrityViolation
    """Raised when a managed plugin file has drifted from its recorded hash."""


def _classify(path: Path, payload: bytes) -> str:
    if not path.exists():
        return "created"
    if path.read_bytes() == payload:
        return "unchanged"
    return "updated"


def _plugin_root(target_dir: Path, *, scope: Scope, home: Path | None = None) -> Path:
    """Return ``<plugin_root>`` for *scope* — Codex-native plugin dir.

    - ``project`` → ``<target_dir>/.codex/plugins/eawf/``
    - ``user``    → ``<home>/.codex/plugins/eawf/`` (``home`` defaults to
      :func:`pathlib.Path.home`; the kwarg exists for tests).
    """
    if scope == "project":
        return target_dir / ".codex" / "plugins" / _PLUGIN_NAME
    base = home if home is not None else Path.home()
    return base / ".codex" / "plugins" / _PLUGIN_NAME


def _config_target(target_dir: Path, *, scope: Scope, home: Path | None = None) -> Path:
    """Return the scope-correct ``config.toml`` path."""
    if scope == "project":
        return target_dir / ".codex" / "config.toml"
    base = home if home is not None else Path.home()
    return base / ".codex" / "config.toml"


def _skill_target(plugin_root: Path, spec: SkillSpec) -> Path:
    return plugin_root / "skills" / spec.skill_name.lstrip("/") / "SKILL.md"


def _hook_target(plugin_root: Path, spec: HookSpec) -> Path:
    return plugin_root / "hooks" / f"{codex_hook_name(spec.event_type)}.sh"


def _manifest_target(plugin_root: Path) -> Path:
    return plugin_root / _MANIFEST_DIR / _MANIFEST_FILE


def _sidecar_target(plugin_root: Path) -> Path:
    return plugin_root / _MANIFEST_DIR / _SIDECAR_FILE


def _render_skill(spec: SkillSpec) -> str:
    return render_skill_md(
        SkillTemplateContext(
            skill_name=spec.skill_name,
            description=spec.description,
            argument_hint=spec.argument_hint,
            user_invocable=spec.user_invocable,
            disable_model_invocation=spec.disable_model_invocation,
            body=spec.body,
        )
    )


def _render_manifest() -> bytes:
    """Render the Codex-native ``.codex-plugin/plugin.json`` body.

    Schema (per Codex Build-plugin reference): ``name``, ``version``,
    ``description``, ``skills``, ``hooks``. Codex has no top-level
    ``agents`` key in ``plugin.json`` — agents live nested inside
    skills, so no top-level ``agents/`` directory is emitted.
    """
    manifest: dict[str, object] = {
        "name": _PLUGIN_NAME,
        "version": _PLUGIN_VERSION,
        "description": _PLUGIN_DESCRIPTION,
        "skills": "./skills/",
        "hooks": "./hooks/",
    }
    return (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _build_sidecar_body(timestamp: str) -> dict[str, object]:
    skills_payload = [{"name": spec.skill_name, "version": spec.version} for spec in SKILL_REGISTRY]
    hooks_payload = [
        {
            "event_type": spec.event_type.value,
            "path": f"hooks/{codex_hook_name(spec.event_type)}.sh",
        }
        for spec in HOOK_REGISTRY
    ]
    body: dict[str, object] = {
        "version": _PLUGIN_VERSION,
        "generator": _GENERATOR,
        "generated_at": timestamp,
        "skills": skills_payload,
        "hooks": hooks_payload,
    }
    body_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["hash"] = hashlib.blake2b(body_json.encode("utf-8"), digest_size=8).hexdigest()
    return body


def _render_sidecar(timestamp: str) -> bytes:
    body = _build_sidecar_body(timestamp)
    return (json.dumps(body, sort_keys=True, indent=2) + "\n").encode("utf-8")


_MANAGED_BLOCK_RE = re.compile(
    rf"(?ms)^# ---- {re.escape(_MANAGED_TABLE)} begin ----"
    rf".*?^# ---- {re.escape(_MANAGED_TABLE)} end ----\n?"
)
_BEGIN_MARKER: str = f"# ---- {_MANAGED_TABLE} begin ----"
_END_MARKER: str = f"# ---- {_MANAGED_TABLE} end ----"


def _render_enabled_block() -> str:
    """Render the marker-wrapped ``[plugins.eawf]`` enabled block."""
    lines: list[str] = [
        _BEGIN_MARKER,
        f"[plugins.{_PLUGIN_NAME}]",
        "enabled = true",
        _END_MARKER,
    ]
    return "\n".join(lines) + "\n"


def _patch_config_toml(target_path: Path) -> bytes:
    """Return rewritten ``config.toml`` bytes with the managed block patched in.

    User-authored TOML outside the ``__eawf_managed begin/end`` markers
    is preserved verbatim. When the file does not yet exist, the
    managed block is the entire file body.
    """
    rendered_block = _render_enabled_block()
    if target_path.exists():
        existing = target_path.read_text(encoding="utf-8")
        if _BEGIN_MARKER in existing and _END_MARKER in existing:
            replaced = _MANAGED_BLOCK_RE.sub(rendered_block, existing, count=1)
            return replaced.encode("utf-8")
        prefix = existing.rstrip("\n")
        joined = (prefix + "\n\n" + rendered_block) if prefix else rendered_block
        return joined.encode("utf-8")
    return rendered_block.encode("utf-8")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def install_plugin(
    target_dir: Path,
    *,
    scope: Scope = "project",
    force: bool = False,
    dry_run: bool = False,
    timestamp: str | None = None,
    home: Path | None = None,
) -> InstallResult:
    """Render the Codex CLI plugin tree under *target_dir* / user home.

    Args:
        target_dir: Workspace root (used only for ``scope="project"``).
        scope: ``"project"`` (default) writes under
            ``<target_dir>/.codex/plugins/eawf/``; ``"user"`` writes
            under ``<home>/.codex/plugins/eawf/`` and patches
            ``<home>/.codex/config.toml``.
        force: When ``True``, hand-edits to managed files are
            overwritten silently. When ``False`` (default), a hand-edit
            raises :class:`IntegrityViolation`.
        dry_run: When ``True``, returns the :class:`InstallResult`
            describing what would be written but writes nothing.
        timestamp: ISO 8601 UTC timestamp baked into the sidecar.
            Defaults to ``"1970-01-01T00:00:00+00:00"`` for byte
            stability across runs.
        home: Override for ``Path.home()`` (tests pass ``tmp_path``).

    Raises:
        IntegrityViolation: when a managed file under the plugin root
            has been hand-edited and ``force`` is not set.
    """
    target_dir = Path(target_dir).resolve()
    ts = timestamp or _DEFAULT_TIMESTAMP
    plugin_root = _plugin_root(target_dir, scope=scope, home=home)

    skill_deltas: list[FileDelta] = []
    hook_deltas: list[FileDelta] = []

    for spec in SKILL_REGISTRY:
        path = _skill_target(plugin_root, spec)
        payload = _render_skill(spec).encode("utf-8")
        if path.exists() and not force and path.read_bytes() != payload:
            raise IntegrityViolation(
                f"managed file {path} differs from rendered body; rerun with --force to overwrite"
            )
        action = _classify(path, payload)
        if not dry_run:
            _ensure_dir(path.parent)
            atomic_write_text(path, payload.decode("utf-8"))
        skill_deltas.append(FileDelta(path=path, action=action))

    for hook_spec in HOOK_REGISTRY:
        path = _hook_target(plugin_root, hook_spec)
        payload = render_hook_sh(hook_spec.event_type).encode("utf-8")
        if path.exists() and not force and path.read_bytes() != payload:
            raise IntegrityViolation(
                f"managed file {path} differs from rendered body; rerun with --force to overwrite"
            )
        action = _classify(path, payload)
        if not dry_run:
            _ensure_dir(path.parent)
            atomic_write_text(path, payload.decode("utf-8"))
            os.chmod(path, _HOOK_FILE_MODE)
        hook_deltas.append(FileDelta(path=path, action=action))

    manifest_path = _manifest_target(plugin_root)
    manifest_payload = _render_manifest()
    if manifest_path.exists() and not force and manifest_path.read_bytes() != manifest_payload:
        raise IntegrityViolation(
            f"managed file {manifest_path} differs from rendered body; "
            f"rerun with --force to overwrite"
        )
    manifest_action = _classify(manifest_path, manifest_payload)
    if not dry_run:
        _ensure_dir(manifest_path.parent)
        atomic_write_text(manifest_path, manifest_payload.decode("utf-8"))
    manifest_delta = FileDelta(path=manifest_path, action=manifest_action)

    sidecar_path = _sidecar_target(plugin_root)
    sidecar_payload = _render_sidecar(ts)
    sidecar_action = _classify(sidecar_path, sidecar_payload)
    if not dry_run:
        _ensure_dir(sidecar_path.parent)
        atomic_write_text(sidecar_path, sidecar_payload.decode("utf-8"))
    sidecar_delta = FileDelta(path=sidecar_path, action=sidecar_action)

    config_path = _config_target(target_dir, scope=scope, home=home)
    config_bytes = _patch_config_toml(config_path)
    config_action = _classify(config_path, config_bytes)
    if not dry_run:
        _ensure_dir(config_path.parent)
        atomic_write_text(config_path, config_bytes.decode("utf-8"))
    config_delta = FileDelta(path=config_path, action=config_action)

    logger.info(
        f"install_plugin runtime=codex scope={scope} plugin_root={plugin_root} "
        f"skills={len(skill_deltas)} hooks={len(hook_deltas)} "
        f"manifest={manifest_action} sidecar={sidecar_action} config={config_action} "
        f"dry_run={dry_run}"
    )
    return InstallResult(
        target_dir=target_dir,
        scope=scope,
        skills=skill_deltas,
        hooks=hook_deltas,
        manifest=manifest_delta,
        sidecar=sidecar_delta,
        config=config_delta,
        dry_run=dry_run,
    )


def expected_paths(
    target_dir: Path,
    *,
    scope: Scope = "project",
    home: Path | None = None,
) -> tuple[Mapping[str, Path], Path]:
    """Return ``({region_id: path, ...}, config_path)`` for *target_dir* at *scope*."""
    target_dir = Path(target_dir).resolve()
    plugin_root = _plugin_root(target_dir, scope=scope, home=home)
    paths: dict[str, Path] = {}
    for spec in SKILL_REGISTRY:
        paths[f"plugin.codex.skill.{spec.skill_name}"] = _skill_target(plugin_root, spec)
    for hook_spec in HOOK_REGISTRY:
        paths[f"plugin.codex.hook.{hook_spec.event_type.value}"] = _hook_target(
            plugin_root, hook_spec
        )
    paths["plugin.codex.manifest"] = _manifest_target(plugin_root)
    paths["plugin.codex.sidecar"] = _sidecar_target(plugin_root)
    return paths, _config_target(target_dir, scope=scope, home=home)


__all__ = [
    "FileDelta",
    "InstallResult",
    "IntegrityViolation",
    "Scope",
    "expected_paths",
    "install_plugin",
]
