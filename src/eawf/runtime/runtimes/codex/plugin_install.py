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

from eawf.runtime.runtimes.codex.hook_map import codex_hook_name
from eawf.surfaces.render._atomic import atomic_write_text
from eawf.surfaces.render.hooks import HOOK_REGISTRY, HookSpec, render_hook_sh
from eawf.surfaces.render.manifest import (
    Manifest,
    ManifestEntry,
)
from eawf.surfaces.render.manifest import (
    load as load_manifest,
)
from eawf.surfaces.render.manifest import (
    save_atomic as save_manifest_atomic,
)
from eawf.surfaces.render.skills import (
    SKILL_REGISTRY,
    SkillSpec,
    SkillTemplateContext,
    render_skill_md,
)

logger = logging.getLogger(__name__)


Scope = Literal["project", "user"]

_PLUGIN_NAME: str = "eawf"
_PLUGIN_VERSION: str = "1.0"
_PLUGIN_DESCRIPTION: str = "Eä Workflow plugin — agent-driven development skills and hooks."
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


class IntegrityViolation(Exception):  # noqa: N818 — mirrors the kind="IntegrityViolation" CLI error bucket
    """Raised when a managed plugin file has drifted from its recorded hash."""


def _classify(path: Path, payload: bytes) -> str:
    if not path.exists():
        return "created"
    if path.read_bytes() == payload:
        return "unchanged"
    return "updated"


def _guard_managed_file(path: Path, payload: bytes, *, force: bool) -> None:
    """Refuse to clobber a hand-edited managed file unless *force* is set.

    Raises:
        IntegrityViolation: when *path* exists, differs from *payload*, and
            *force* is ``False``.
    """
    if path.exists() and not force and path.read_bytes() != payload:
        raise IntegrityViolation(
            f"managed file {path} differs from rendered body; rerun with --force to overwrite"
        )


def _write_managed_file(
    path: Path,
    payload: bytes,
    *,
    force: bool,
    dry_run: bool,
    chmod_mode: int | None = None,
) -> FileDelta:
    """Integrity-check, classify, and (unless *dry_run*) write one managed file.

    Returns:
        The :class:`FileDelta` describing the create/update/unchanged action.

    Raises:
        IntegrityViolation: when the on-disk file was hand-edited and *force*
            is not set.
    """
    _guard_managed_file(path, payload, force=force)
    action = _classify(path, payload)
    if not dry_run:
        _ensure_dir(path.parent)
        atomic_write_text(path, payload.decode("utf-8"))
        if chmod_mode is not None:
            os.chmod(path, chmod_mode)
    return FileDelta(path=path, action=action)


def _write_sidecar(plugin_root: Path, ts: str, *, force: bool, dry_run: bool) -> FileDelta:
    """Render + write the sidecar, guarding on its fingerprint (not raw bytes).

    The sidecar carries a volatile timestamp, so a hand-edit is detected via
    :func:`_sidecar_fingerprint` rather than a byte-for-byte comparison.

    Raises:
        IntegrityViolation: when the on-disk sidecar fingerprint diverges and
            *force* is not set.
    """
    sidecar_path = _sidecar_target(plugin_root)
    sidecar_payload = _render_sidecar(ts)
    if (
        sidecar_path.exists()
        and not force
        and _sidecar_fingerprint(sidecar_path.read_bytes()) != _sidecar_fingerprint(sidecar_payload)
    ):
        raise IntegrityViolation(
            f"managed file {sidecar_path} differs from rendered body; "
            f"rerun with --force to overwrite"
        )
    action = _classify(sidecar_path, sidecar_payload)
    if not dry_run:
        _ensure_dir(sidecar_path.parent)
        atomic_write_text(sidecar_path, sidecar_payload.decode("utf-8"))
    return FileDelta(path=sidecar_path, action=action)


def _write_config(target_dir: Path, *, scope: Scope, home: Path | None, dry_run: bool) -> FileDelta:
    """Patch + write the Codex ``config.toml`` block (no integrity guard)."""
    config_path = _config_target(target_dir, scope=scope, home=home)
    config_bytes = _patch_config_toml(config_path)
    action = _classify(config_path, config_bytes)
    if not dry_run:
        _ensure_dir(config_path.parent)
        atomic_write_text(config_path, config_bytes.decode("utf-8"))
    return FileDelta(path=config_path, action=action)


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
    ``description``, ``skills``, ``hooks``, plus the ``interface`` block
    consumed by the Codex marketplace picker (display name, category,
    short / long descriptions, default prompt). Codex has no top-level
    ``agents`` key in ``plugin.json`` — agents live nested inside
    skills, so no top-level ``agents/`` directory is emitted.

    URL fields (``websiteURL``, ``privacyPolicyURL``,
    ``termsOfServiceURL``) and asset paths (``composerIcon``, ``logo``)
    are intentionally omitted: bundling per-developer URLs or assets
    that may not exist on disk would violate the PII / path-hygiene
    rule and could break the manifest schema for downstream installs.
    """
    manifest: dict[str, object] = {
        "name": _PLUGIN_NAME,
        "version": _PLUGIN_VERSION,
        "description": _PLUGIN_DESCRIPTION,
        "skills": "./skills/",
        "hooks": "./hooks/",
        "interface": {
            "displayName": "Eä Workflow",
            "shortDescription": (
                "Agent-driven development workflow — research, plan, ship in waves."
            ),
            "longDescription": (
                "Eä Workflow (eawf) is an agent-driven software development framework. "
                "Skills wrap the research → plan → execute → audit → ship → review → polish "
                "pipeline; hooks gate state mutations on lifecycle events."
            ),
            "developerName": "Eä Workflow",
            "category": "Productivity",
            "capabilities": ["Write"],
            "defaultPrompt": [
                "Use the Eä Workflow skills to drive this iteration. "
                "Start with /flow or pick a specific stage like /research, /prep, /ship."
            ],
            "screenshots": [],
            "brandColor": "#6B7280",
        },
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


def _sidecar_fingerprint(payload: bytes) -> str:
    """Return a semantic fingerprint for sidecar bytes.

    ``generated_at`` and the derived ``hash`` field are intentionally ignored so
    older installs with a non-default timestamp still compare cleanly.
    """
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return hashlib.blake2b(b"raw:" + payload, digest_size=8).hexdigest()
    if not isinstance(parsed, dict):
        return hashlib.blake2b(b"raw:" + payload, digest_size=8).hexdigest()
    comparable = dict(parsed)
    comparable.pop("generated_at", None)
    comparable.pop("hash", None)
    body = json.dumps(comparable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(body, digest_size=8).hexdigest()


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


def _persist_manifest(
    target_dir: Path,
    *,
    scope: Scope,
    timestamp: str,
    plugin_root: Path,
    home: Path | None,
) -> None:
    """Append codex plugin entries to ``.ea/indexes/generated.json``.

    The manifest is the single source of truth for the cross-runtime
    drift reconciler (``eawf doctor`` / ``eawf plugin doctor``); the
    codex sidecar at ``.codex-plugin/.eawf-managed.json`` stays the
    runtime-local fingerprint store, but every entry the installer
    writes also lands in the shared manifest with ``scope`` set so the
    plugin-cross-scope-dup detector can flag the same region_id at both
    scopes.

    The manifest lives under ``<target_dir>/.ea/indexes/generated.json``
    regardless of *scope* — the .ea/ directory is the workspace-anchored
    drift-reconciliation store, not a per-runtime artifact. Entries that
    do not belong to the codex tree are carried through unchanged.
    """
    manifest_path = target_dir / ".ea" / "indexes" / "generated.json"
    existing = load_manifest(manifest_path)
    new_generated: dict[str, ManifestEntry] = {}

    # Compute the codex region_id set so we can drop stale codex rows
    # without touching unrelated entries owned by other generators.
    codex_region_prefix = "plugin.codex."
    for key, entry in existing.generated.items():
        if not entry.region_id.startswith(codex_region_prefix):
            new_generated[key] = entry
            continue
        # Keep codex entries that target a different scope — the same
        # region_id can legitimately appear under both scopes; the
        # cross-scope-dup detector consumes that state.
        if entry.scope != scope:
            new_generated[key] = entry

    config_path = _config_target(target_dir, scope=scope, home=home)
    sidecar_path = _sidecar_target(plugin_root)

    for skill_spec in SKILL_REGISTRY:
        path = _skill_target(plugin_root, skill_spec)
        body = _render_skill(skill_spec).encode("utf-8")
        region_id = f"plugin.codex.skill.{skill_spec.skill_name}"
        new_generated[f"{path.as_posix()}::{region_id}"] = ManifestEntry(
            target=path.as_posix(),
            region_id=region_id,
            version=skill_spec.version,
            hash=hashlib.blake2b(body, digest_size=8).hexdigest(),
            generator=_GENERATOR,
            generated_at=timestamp,
            scope=scope,
        )
    for hook_spec in HOOK_REGISTRY:
        path = _hook_target(plugin_root, hook_spec)
        body = render_hook_sh(hook_spec.event_type).encode("utf-8")
        region_id = f"plugin.codex.hook.{hook_spec.event_type.value}"
        new_generated[f"{path.as_posix()}::{region_id}"] = ManifestEntry(
            target=path.as_posix(),
            region_id=region_id,
            version=hook_spec.version,
            hash=hashlib.blake2b(body, digest_size=8).hexdigest(),
            generator=_GENERATOR,
            generated_at=timestamp,
            scope=scope,
        )

    manifest_body = _render_manifest()
    new_generated[f"{_manifest_target(plugin_root).as_posix()}::plugin.codex.manifest"] = (
        ManifestEntry(
            target=_manifest_target(plugin_root).as_posix(),
            region_id="plugin.codex.manifest",
            version=_PLUGIN_VERSION,
            hash=hashlib.blake2b(manifest_body, digest_size=8).hexdigest(),
            generator=_GENERATOR,
            generated_at=timestamp,
            scope=scope,
        )
    )
    sidecar_body = _render_sidecar(timestamp)
    new_generated[f"{sidecar_path.as_posix()}::plugin.codex.sidecar"] = ManifestEntry(
        target=sidecar_path.as_posix(),
        region_id="plugin.codex.sidecar",
        version=_PLUGIN_VERSION,
        hash=_sidecar_fingerprint(sidecar_body),
        generator=_GENERATOR,
        generated_at=timestamp,
        scope=scope,
    )
    config_body = _patch_config_toml(config_path)
    new_generated[f"{config_path.as_posix()}::plugin.codex.config"] = ManifestEntry(
        target=config_path.as_posix(),
        region_id="plugin.codex.config",
        version=_PLUGIN_VERSION,
        hash=hashlib.blake2b(config_body, digest_size=8).hexdigest(),
        generator=_GENERATOR,
        generated_at=timestamp,
        scope=scope,
    )

    _ensure_dir(manifest_path.parent)
    save_manifest_atomic(manifest_path, Manifest(version=existing.version, generated=new_generated))
    logger.info(
        f"_persist_manifest runtime=codex scope={scope} "
        f"manifest_path={manifest_path} entries={len(new_generated)}"
    )


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

    skill_deltas = [
        _write_managed_file(
            _skill_target(plugin_root, spec),
            _render_skill(spec).encode("utf-8"),
            force=force,
            dry_run=dry_run,
        )
        for spec in SKILL_REGISTRY
    ]
    hook_deltas = [
        _write_managed_file(
            _hook_target(plugin_root, hook_spec),
            render_hook_sh(hook_spec.event_type).encode("utf-8"),
            force=force,
            dry_run=dry_run,
            chmod_mode=_HOOK_FILE_MODE,
        )
        for hook_spec in HOOK_REGISTRY
    ]

    manifest_delta = _write_managed_file(
        _manifest_target(plugin_root), _render_manifest(), force=force, dry_run=dry_run
    )
    sidecar_delta = _write_sidecar(plugin_root, ts, force=force, dry_run=dry_run)
    config_delta = _write_config(target_dir, scope=scope, home=home, dry_run=dry_run)

    if not dry_run:
        _persist_manifest(target_dir, scope=scope, timestamp=ts, plugin_root=plugin_root, home=home)

    logger.info(
        f"install_plugin runtime=codex scope={scope} plugin_root={plugin_root} "
        f"skills={len(skill_deltas)} hooks={len(hook_deltas)} "
        f"manifest={manifest_delta.action} sidecar={sidecar_delta.action} "
        f"config={config_delta.action} dry_run={dry_run}"
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
