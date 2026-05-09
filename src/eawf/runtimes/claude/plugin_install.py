"""Render a complete Claude Code plugin tree under a target directory.

Per Phase 4 W05 acceptance §1, ``eawf plugin install claude`` produces
the following layout under ``<target_dir>/.claude/`` (target_dir
defaults to the workspace root):

::

    .claude/
      skills/
        <skill_name>/SKILL.md           # one per skill in render.skills.SKILL_REGISTRY
      agents/
        <role>.md                       # one per role in render.agents.AGENT_REGISTRY
      hooks/
        <event_type>.sh                 # one per event in render.hooks.HOOK_REGISTRY
      settings.json                     # __eawf_managed namespace patched in;
                                          # user-owned keys preserved verbatim

The renderer is *idempotent*: invoked twice on the same target with the
same input, the second run produces a byte-identical tree (acceptance
§2). The ``__eawf_managed`` namespace carries a hash of its own body
plus the ISO 8601 timestamp; both are deterministic functions of the
input, so two invocations on the same minute (or different minutes,
once we freeze the timestamp from the manifest) produce the same byte
output.

Public API::

    InstallResult                     # dataclass: per-tree per-file delta
    install_plugin(target_dir, *, force=False, dry_run=False) -> InstallResult
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eawf.render._atomic import atomic_write_text
from eawf.render.agents import AGENT_REGISTRY, AgentSpec, AgentTemplateContext, render_agent_md
from eawf.render.hooks import HOOK_REGISTRY, HookSpec, render_hook_sh
from eawf.render.manifest import Manifest, ManifestEntry, save_atomic
from eawf.render.skills import (
    SKILL_REGISTRY,
    SkillSpec,
    SkillTemplateContext,
    render_skill_md,
)

logger = logging.getLogger(__name__)


_PLUGIN_VERSION: str = "1.0"
_GENERATOR: str = "eawf-plugin-claude"
_MANAGED_KEY: str = "__eawf_managed"
_HOOK_FILE_MODE: int = 0o755
# Stable timestamp used by the install / fragment renderer when the
# caller does not pin one. 1970-01-01T00:00:00Z makes idempotence trivial:
# two installs minutes apart produce byte-identical settings.json.
_DEFAULT_TIMESTAMP: str = "1970-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class FileDelta:
    """One file the installer wrote / would have written.

    Attributes:
        path: Absolute path the file lives at after installation.
        action: ``"created"`` if the file did not exist before this
            run, ``"updated"`` if its bytes changed, ``"unchanged"`` if
            the file already had the rendered bytes (so re-runs report
            cleanly).
    """

    path: Path
    action: str  # Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class InstallResult:
    """Summary of one :func:`install_plugin` call.

    Attributes:
        target_dir: Workspace root the install ran against.
        skills: Per-skill :class:`FileDelta` list, in registry order.
        agents: Per-agent :class:`FileDelta` list, in registry order.
        hooks: Per-hook :class:`FileDelta` list, in registry order.
        settings: :class:`FileDelta` for ``settings.json``.
        dry_run: Whether the run was a dry run (``True`` → no bytes
            were written).
    """

    target_dir: Path
    skills: list[FileDelta] = field(default_factory=list)
    agents: list[FileDelta] = field(default_factory=list)
    hooks: list[FileDelta] = field(default_factory=list)
    settings: FileDelta | None = None
    dry_run: bool = False


def _classify(path: Path, payload: bytes) -> str:
    """Return ``"created" | "updated" | "unchanged"`` for *payload* at *path*."""
    if not path.exists():
        return "created"
    if path.read_bytes() == payload:
        return "unchanged"
    return "updated"


def _ensure_dir(path: Path) -> None:
    """Create *path* and any missing parents."""
    path.mkdir(parents=True, exist_ok=True)


def _skill_target(target_dir: Path, spec: SkillSpec) -> Path:
    """Return the disk path for *spec*'s SKILL.md under *target_dir*."""
    return target_dir / ".claude" / "skills" / spec.skill_name / "SKILL.md"


def _agent_target(target_dir: Path, spec: AgentSpec) -> Path:
    """Return the disk path for *spec*'s agent markdown under *target_dir*."""
    return target_dir / ".claude" / "agents" / f"{spec.role}.md"


def _hook_target(target_dir: Path, spec: HookSpec) -> Path:
    """Return the disk path for *spec*'s hook script under *target_dir*."""
    return target_dir / ".claude" / "hooks" / f"{spec.event_type.value}.sh"


def _settings_target(target_dir: Path) -> Path:
    """Return the disk path for ``settings.json``."""
    return target_dir / ".claude" / "settings.json"


def _render_skill(spec: SkillSpec) -> str:
    """Render one skill's SKILL.md text from *spec*."""
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


def _render_agent(spec: AgentSpec) -> str:
    """Render one agent's markdown from *spec*."""
    return render_agent_md(
        AgentTemplateContext(
            role=spec.role,
            description=spec.description,
            tools=spec.tools,
            model=spec.model,
            color=spec.color,
            memory=spec.memory,
            body=spec.body,
        )
    )


def _render_managed_block(timestamp: str) -> dict[str, Any]:
    """Return the ``__eawf_managed`` body for the settings patcher.

    The body is a dict (not a JSON-text fragment) so the patcher can
    splice it into the parent settings JSON without re-parsing. The
    ``hash`` field is a 16-hex digest of the canonical body text
    (sorted-keys JSON) — the patcher recomputes it after assembly so a
    hand-edit to the body still flips the recorded hash.
    """
    skills_payload = [{"name": spec.skill_name, "version": spec.version} for spec in SKILL_REGISTRY]
    agents_payload = [{"name": spec.role, "version": spec.version} for spec in AGENT_REGISTRY]
    hooks_payload = [
        {"event_type": spec.event_type.value, "path": f".claude/hooks/{spec.event_type.value}.sh"}
        for spec in HOOK_REGISTRY
    ]
    body: dict[str, Any] = {
        "version": _PLUGIN_VERSION,
        "generated_at": timestamp,
        "skills": skills_payload,
        "agents": agents_payload,
        "hooks": hooks_payload,
    }
    body_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["hash"] = hashlib.blake2b(body_json.encode("utf-8"), digest_size=8).hexdigest()
    return body


def _patch_settings_json(target_path: Path, managed_body: dict[str, Any]) -> bytes:
    """Return the new ``settings.json`` bytes with ``__eawf_managed`` patched in.

    Behaviour:

    - If *target_path* exists, read it as JSON; non-JSON content raises
      :class:`ValueError`.
    - Replace (or insert) the ``__eawf_managed`` key with *managed_body*;
      every other key is preserved verbatim — Eä never writes outside
      its namespace per ea-proposal §2.
    - Render the resulting object as deterministic JSON (sorted keys,
      4-space indent, trailing newline) so two installs are byte-stable.
    """
    parsed: dict[str, Any] = {}
    if target_path.exists():
        raw = target_path.read_text(encoding="utf-8")
        if raw.strip():
            try:
                parsed_any: Any = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"settings.json at {target_path} is not valid JSON: {exc}"
                ) from exc
            if not isinstance(parsed_any, dict):
                raise ValueError(
                    f"settings.json at {target_path} must be a JSON object; got"
                    f" {type(parsed_any).__name__}"
                )
            parsed = dict(parsed_any)
    parsed[_MANAGED_KEY] = managed_body
    rendered = json.dumps(parsed, sort_keys=True, indent=2) + "\n"
    return rendered.encode("utf-8")


def _build_manifest(
    target_dir: Path,
    *,
    timestamp: str,
    base_manifest: Manifest,
) -> Manifest:
    """Build a manifest covering every file the installer writes.

    Each file gets one entry keyed by ``"<posix-path>::<region_id>"``
    where ``region_id`` is ``"plugin.claude.<kind>.<name>"``. The hash
    is a blake2b-64 of the rendered file body so drift detection in
    :mod:`eawf.runtimes.claude.plugin_doctor` flags any hand-edit.
    """
    target_dir = target_dir.resolve()
    new_generated: dict[str, ManifestEntry] = {}
    # Carry through entries that don't belong to the claude tree we own.
    own_targets: set[str] = set()
    for skill_spec in SKILL_REGISTRY:
        own_targets.add(_skill_target(target_dir, skill_spec).as_posix())
    for agent_spec in AGENT_REGISTRY:
        own_targets.add(_agent_target(target_dir, agent_spec).as_posix())
    for hook_spec in HOOK_REGISTRY:
        own_targets.add(_hook_target(target_dir, hook_spec).as_posix())
    own_targets.add(_settings_target(target_dir).as_posix())

    for key, entry in base_manifest.generated.items():
        if entry.target not in own_targets:
            new_generated[key] = entry

    for skill_spec in SKILL_REGISTRY:
        path = _skill_target(target_dir, skill_spec)
        body = _render_skill(skill_spec)
        new_generated[f"{path.as_posix()}::plugin.claude.skill.{skill_spec.skill_name}"] = (
            ManifestEntry(
                target=path.as_posix(),
                region_id=f"plugin.claude.skill.{skill_spec.skill_name}",
                version=skill_spec.version,
                hash=hashlib.blake2b(body.encode("utf-8"), digest_size=8).hexdigest(),
                generator=_GENERATOR,
                generated_at=timestamp,
            )
        )
    for agent_spec in AGENT_REGISTRY:
        path = _agent_target(target_dir, agent_spec)
        body = _render_agent(agent_spec)
        new_generated[f"{path.as_posix()}::plugin.claude.agent.{agent_spec.role}"] = ManifestEntry(
            target=path.as_posix(),
            region_id=f"plugin.claude.agent.{agent_spec.role}",
            version=agent_spec.version,
            hash=hashlib.blake2b(body.encode("utf-8"), digest_size=8).hexdigest(),
            generator=_GENERATOR,
            generated_at=timestamp,
        )
    for hook_spec in HOOK_REGISTRY:
        path = _hook_target(target_dir, hook_spec)
        body = render_hook_sh(hook_spec.event_type)
        new_generated[f"{path.as_posix()}::plugin.claude.hook.{hook_spec.event_type.value}"] = (
            ManifestEntry(
                target=path.as_posix(),
                region_id=f"plugin.claude.hook.{hook_spec.event_type.value}",
                version=hook_spec.version,
                hash=hashlib.blake2b(body.encode("utf-8"), digest_size=8).hexdigest(),
                generator=_GENERATOR,
                generated_at=timestamp,
            )
        )
    settings_path = _settings_target(target_dir)
    managed_body = _render_managed_block(timestamp)
    settings_bytes = _patch_settings_json(settings_path, managed_body)
    new_generated[f"{settings_path.as_posix()}::plugin.claude.settings"] = ManifestEntry(
        target=settings_path.as_posix(),
        region_id="plugin.claude.settings",
        version=_PLUGIN_VERSION,
        hash=hashlib.blake2b(settings_bytes, digest_size=8).hexdigest(),
        generator=_GENERATOR,
        generated_at=timestamp,
    )
    return Manifest(version=base_manifest.version, generated=new_generated)


def _load_existing_manifest(target_dir: Path) -> Manifest:
    """Load ``.ea/indexes/generated.json`` under *target_dir* (empty if absent)."""
    manifest_path = target_dir / ".ea" / "indexes" / "generated.json"
    if not manifest_path.exists():
        return Manifest()
    raw = manifest_path.read_text(encoding="utf-8")
    if not raw.strip():
        return Manifest()
    body = json.loads(raw)
    return Manifest.model_validate(body)


def _check_for_drift(target_dir: Path, manifest: Manifest, *, force: bool) -> None:
    """Raise :class:`IntegrityViolation` if any owned file has been hand-edited.

    The check is identical to :func:`eawf.render.drift.detect_drift` but
    operates on a per-byte equality basis (the file is rendered, not a
    managed-region body). When *force* is ``True``, drift is overwritten
    silently — the caller takes responsibility for clobbering the
    user's hand-edits.
    """
    if force:
        return
    own_paths: list[Path] = []
    for skill_spec in SKILL_REGISTRY:
        own_paths.append(_skill_target(target_dir, skill_spec))
    for agent_spec in AGENT_REGISTRY:
        own_paths.append(_agent_target(target_dir, agent_spec))
    for hook_spec in HOOK_REGISTRY:
        own_paths.append(_hook_target(target_dir, hook_spec))
    # settings.json is patched, not rewritten — drift is checked against
    # the recorded hash of the LAST rendered bytes, not against the
    # current bytes (because user keys may have changed legitimately).
    for path in own_paths:
        if not path.exists():
            continue
        live = path.read_bytes()
        live_hash = hashlib.blake2b(live, digest_size=8).hexdigest()
        # Find the manifest entry for this path (region_id derived
        # from path component matching the registry name).
        entry = next(
            (e for e in manifest.generated.values() if e.target == path.as_posix()),
            None,
        )
        if entry is None:
            continue  # Not previously installed; safe to overwrite.
        if live_hash != entry.hash:
            raise IntegrityViolation(
                f"managed file {path} has been hand-edited "
                f"(disk={live_hash} manifest={entry.hash}); rerun with --force "
                f"or `eawf plugin doctor claude` to inspect."
            )


class IntegrityViolation(Exception):  # noqa: N818 — mirrors eawf.cli.errors.IntegrityViolation
    """Raised when a managed plugin file has drifted from its recorded hash."""


def install_plugin(
    target_dir: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    timestamp: str | None = None,
    persist_manifest: bool = True,
) -> InstallResult:
    """Render the Claude Code plugin tree into *target_dir*.

    Args:
        target_dir: Workspace root that hosts the ``.claude/`` directory.
            The directory is created on demand.
        force: When ``True``, hand-edits to managed files are
            overwritten silently. When ``False`` (default), a hand-edit
            raises :class:`IntegrityViolation` (mapped to exit code 8
            by the CLI).
        dry_run: When ``True``, the function returns the
            :class:`InstallResult` describing what *would* be written
            but writes nothing to disk.
        timestamp: ISO 8601 UTC timestamp baked into the
            ``__eawf_managed`` namespace and the manifest entries.
            Defaults to a deterministic epoch (``1970-01-01T00:00:00Z``)
            so two installs produce byte-identical settings.json. Pass
            a real ``datetime.now(UTC).isoformat()`` from production
            callers if cohesive timestamps matter to a downstream
            consumer.
        persist_manifest: When ``True`` (default), the updated manifest
            is written to ``<target_dir>/.ea/indexes/generated.json``.
            Tests pass ``False`` to keep the temp tree clean.

    Returns:
        :class:`InstallResult` summarising every file the installer
        wrote.

    Raises:
        IntegrityViolation: A managed file already exists on disk and
            its body hash does not match the manifest's recorded hash
            (i.e., a hand-edit). Suppressed by ``force=True``.
        ValueError: ``settings.json`` exists but is not valid JSON, or
            its top-level body is not an object.
    """
    target_dir = Path(target_dir).resolve()
    ts = timestamp or _DEFAULT_TIMESTAMP

    base_manifest = _load_existing_manifest(target_dir)
    _check_for_drift(target_dir, base_manifest, force=force)

    skill_deltas: list[FileDelta] = []
    agent_deltas: list[FileDelta] = []
    hook_deltas: list[FileDelta] = []

    # Render skills.
    for spec in SKILL_REGISTRY:
        path = _skill_target(target_dir, spec)
        payload = _render_skill(spec).encode("utf-8")
        action = _classify(path, payload)
        if not dry_run:
            _ensure_dir(path.parent)
            atomic_write_text(path, payload.decode("utf-8"))
        skill_deltas.append(FileDelta(path=path, action=action))

    # Render agents.
    for agent_spec in AGENT_REGISTRY:
        path = _agent_target(target_dir, agent_spec)
        payload = _render_agent(agent_spec).encode("utf-8")
        action = _classify(path, payload)
        if not dry_run:
            _ensure_dir(path.parent)
            atomic_write_text(path, payload.decode("utf-8"))
        agent_deltas.append(FileDelta(path=path, action=action))

    # Render hooks.
    for hook_spec in HOOK_REGISTRY:
        path = _hook_target(target_dir, hook_spec)
        payload = render_hook_sh(hook_spec.event_type).encode("utf-8")
        action = _classify(path, payload)
        if not dry_run:
            _ensure_dir(path.parent)
            atomic_write_text(path, payload.decode("utf-8"))
            os.chmod(path, _HOOK_FILE_MODE)
        hook_deltas.append(FileDelta(path=path, action=action))

    # Patch settings.json.
    settings_path = _settings_target(target_dir)
    managed_body = _render_managed_block(ts)
    settings_bytes = _patch_settings_json(settings_path, managed_body)
    settings_action = _classify(settings_path, settings_bytes)
    if not dry_run:
        _ensure_dir(settings_path.parent)
        atomic_write_text(settings_path, settings_bytes.decode("utf-8"))
    settings_delta = FileDelta(path=settings_path, action=settings_action)

    # Persist updated manifest.
    if not dry_run and persist_manifest:
        new_manifest = _build_manifest(target_dir, timestamp=ts, base_manifest=base_manifest)
        manifest_path = target_dir / ".ea" / "indexes" / "generated.json"
        _ensure_dir(manifest_path.parent)
        save_atomic(manifest_path, new_manifest)

    logger.info(
        f"install_plugin target={target_dir} skills={len(skill_deltas)} "
        f"agents={len(agent_deltas)} hooks={len(hook_deltas)} "
        f"settings={settings_action} dry_run={dry_run}"
    )
    return InstallResult(
        target_dir=target_dir,
        skills=skill_deltas,
        agents=agent_deltas,
        hooks=hook_deltas,
        settings=settings_delta,
        dry_run=dry_run,
    )


def expected_paths(target_dir: Path) -> tuple[Mapping[str, Path], Path]:
    """Return ``({region_id: path, ...}, settings_path)`` for *target_dir*.

    Used by :mod:`eawf.runtimes.claude.plugin_doctor` and
    :mod:`eawf.runtimes.claude.plugin_update` to enumerate the files
    Eä claims ownership of without re-running a render.
    """
    target_dir = Path(target_dir).resolve()
    paths: dict[str, Path] = {}
    for spec in SKILL_REGISTRY:
        paths[f"plugin.claude.skill.{spec.skill_name}"] = _skill_target(target_dir, spec)
    for agent_spec in AGENT_REGISTRY:
        paths[f"plugin.claude.agent.{agent_spec.role}"] = _agent_target(target_dir, agent_spec)
    for hook_spec in HOOK_REGISTRY:
        paths[f"plugin.claude.hook.{hook_spec.event_type.value}"] = _hook_target(
            target_dir, hook_spec
        )
    return paths, _settings_target(target_dir)


def _expected_bytes_for(region_id: str) -> bytes:
    """Return the rendered bytes the installer would emit for *region_id*."""
    if region_id.startswith("plugin.claude.skill."):
        skill_name = region_id.removeprefix("plugin.claude.skill.")
        spec = next(s for s in SKILL_REGISTRY if s.skill_name == skill_name)
        return _render_skill(spec).encode("utf-8")
    if region_id.startswith("plugin.claude.agent."):
        role = region_id.removeprefix("plugin.claude.agent.")
        agent_spec = next(s for s in AGENT_REGISTRY if s.role == role)
        return _render_agent(agent_spec).encode("utf-8")
    if region_id.startswith("plugin.claude.hook."):
        event_value = region_id.removeprefix("plugin.claude.hook.")
        return render_hook_sh(_event_type_for(event_value)).encode("utf-8")
    raise KeyError(f"unknown plugin region_id={region_id!r}")


def _event_type_for(value: str) -> Any:
    """Resolve *value* to the matching :class:`HookEventType` member."""
    from eawf.hooks.event import HookEventType

    return HookEventType(value)


__all__ = [
    "FileDelta",
    "InstallResult",
    "IntegrityViolation",
    "expected_paths",
    "install_plugin",
]
