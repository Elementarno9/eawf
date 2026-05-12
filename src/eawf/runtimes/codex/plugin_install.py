"""Render an Eä-owned Codex CLI plugin tree under ``<target>/.codex/`` (P14-W06).

Mirrors :mod:`eawf.runtimes.claude.plugin_install` in shape, but emits:

::

    .codex/
      skills/<skill_name>.md
      agents/<role>.md
      hooks/<event>.sh
      config.toml

``config.toml`` carries an ``[__eawf_managed]`` section indexing every
file the installer owns plus a hash-of-bodies; user-edited sections
elsewhere in the TOML are preserved verbatim. The renderer is
idempotent — two runs against the same input produce byte-identical
output.

Public API mirrors the Claude adapter:

    InstallResult                    (dataclass: per-file deltas)
    install_plugin(target_dir, ...)  → InstallResult
    expected_paths(target_dir)       → ({region_id: Path}, config_path)
    IntegrityViolation               (raised on managed-file hand-edits)
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
from typing import cast

from eawf.render._atomic import atomic_write_text
from eawf.render.agents import AGENT_REGISTRY, AgentSpec, AgentTemplateContext, render_agent_md
from eawf.render.hooks import HOOK_REGISTRY, HookSpec, render_hook_sh
from eawf.render.skills import (
    SKILL_REGISTRY,
    SkillSpec,
    SkillTemplateContext,
    render_skill_md,
)
from eawf.runtimes.codex.hook_map import codex_hook_name

logger = logging.getLogger(__name__)


_PLUGIN_VERSION: str = "1.0"
_GENERATOR: str = "eawf-plugin-codex"
_MANAGED_TABLE: str = "__eawf_managed"
_HOOK_FILE_MODE: int = 0o755
_DEFAULT_TIMESTAMP: str = "1970-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class FileDelta:
    """One file the installer wrote / would have written."""

    path: Path
    action: str  # Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class InstallResult:
    """Summary of one :func:`install_plugin` call."""

    target_dir: Path
    skills: list[FileDelta] = field(default_factory=list)
    agents: list[FileDelta] = field(default_factory=list)
    hooks: list[FileDelta] = field(default_factory=list)
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


def _skill_target(target_dir: Path, spec: SkillSpec) -> Path:
    return target_dir / ".codex" / "skills" / f"{spec.skill_name.lstrip('/')}.md"


def _agent_target(target_dir: Path, spec: AgentSpec) -> Path:
    return target_dir / ".codex" / "agents" / f"{spec.role}.md"


def _hook_target(target_dir: Path, spec: HookSpec) -> Path:
    return target_dir / ".codex" / "hooks" / f"{codex_hook_name(spec.event_type)}.sh"


def _config_target(target_dir: Path) -> Path:
    return target_dir / ".codex" / "config.toml"


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


def _render_agent(spec: AgentSpec) -> str:
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


def _build_managed_body(timestamp: str) -> dict[str, object]:
    skills_payload = [{"name": spec.skill_name, "version": spec.version} for spec in SKILL_REGISTRY]
    agents_payload = [{"name": spec.role, "version": spec.version} for spec in AGENT_REGISTRY]
    hooks_payload = [
        {
            "event_type": spec.event_type.value,
            "path": f".codex/hooks/{codex_hook_name(spec.event_type)}.sh",
        }
        for spec in HOOK_REGISTRY
    ]
    body: dict[str, object] = {
        "version": _PLUGIN_VERSION,
        "generated_at": timestamp,
        "skills": skills_payload,
        "agents": agents_payload,
        "hooks": hooks_payload,
    }
    body_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["hash"] = hashlib.blake2b(body_json.encode("utf-8"), digest_size=8).hexdigest()
    return body


_MANAGED_BLOCK_RE = re.compile(
    rf"(?ms)^# ---- {re.escape(_MANAGED_TABLE)} begin ----"
    rf".*?^# ---- {re.escape(_MANAGED_TABLE)} end ----\n?"
)
_BEGIN_MARKER: str = f"# ---- {_MANAGED_TABLE} begin ----"
_END_MARKER: str = f"# ---- {_MANAGED_TABLE} end ----"


def _render_managed_toml(body: dict[str, object]) -> str:
    """Serialise *body* into a TOML fragment under ``[__eawf_managed]``.

    Lists of tables use the ``[[__eawf_managed.<name>]]`` form so the
    schema reads cleanly under any TOML 1.0-compliant parser.
    """
    lines: list[str] = [
        _BEGIN_MARKER,
        f"[{_MANAGED_TABLE}]",
        f'version = "{body["version"]}"',
        f'generated_at = "{body["generated_at"]}"',
        f'hash = "{body["hash"]}"',
        "",
    ]
    skills_rows = cast(list[dict[str, str]], body["skills"])
    agents_rows = cast(list[dict[str, str]], body["agents"])
    hooks_rows = cast(list[dict[str, str]], body["hooks"])
    for skill in skills_rows:
        lines.append(f"[[{_MANAGED_TABLE}.skills]]")
        lines.append(f'name = "{skill["name"]}"')
        lines.append(f'version = "{skill["version"]}"')
        lines.append("")
    for agent in agents_rows:
        lines.append(f"[[{_MANAGED_TABLE}.agents]]")
        lines.append(f'name = "{agent["name"]}"')
        lines.append(f'version = "{agent["version"]}"')
        lines.append("")
    for hook in hooks_rows:
        lines.append(f"[[{_MANAGED_TABLE}.hooks]]")
        lines.append(f'event_type = "{hook["event_type"]}"')
        lines.append(f'path = "{hook["path"]}"')
        lines.append("")
    lines.append(_END_MARKER)
    return "\n".join(lines) + "\n"


def _patch_config_toml(target_path: Path, managed_body: dict[str, object]) -> bytes:
    """Return the rewritten ``config.toml`` bytes with the managed block patched in.

    User-authored TOML outside the ``__eawf_managed begin/end`` markers
    is preserved verbatim. When the file does not yet exist, the
    managed block is the entire file body.
    """
    rendered_block = _render_managed_toml(managed_body)
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
    force: bool = False,
    dry_run: bool = False,
    timestamp: str | None = None,
) -> InstallResult:
    """Render the Codex CLI plugin tree under *target_dir*.

    Args:
        target_dir: Workspace root that hosts the ``.codex/`` directory.
        force: When ``True``, hand-edits to managed files are
            overwritten silently. When ``False`` (default), a hand-edit
            raises :class:`IntegrityViolation`.
        dry_run: When ``True``, returns the :class:`InstallResult`
            describing what would be written but writes nothing.
        timestamp: ISO 8601 UTC timestamp baked into the managed block.
            Defaults to ``"1970-01-01T00:00:00+00:00"`` so two installs
            produce byte-identical output.
    """
    target_dir = Path(target_dir).resolve()
    ts = timestamp or _DEFAULT_TIMESTAMP

    skill_deltas: list[FileDelta] = []
    agent_deltas: list[FileDelta] = []
    hook_deltas: list[FileDelta] = []

    for spec in SKILL_REGISTRY:
        path = _skill_target(target_dir, spec)
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

    for agent_spec in AGENT_REGISTRY:
        path = _agent_target(target_dir, agent_spec)
        payload = _render_agent(agent_spec).encode("utf-8")
        if path.exists() and not force and path.read_bytes() != payload:
            raise IntegrityViolation(
                f"managed file {path} differs from rendered body; rerun with --force to overwrite"
            )
        action = _classify(path, payload)
        if not dry_run:
            _ensure_dir(path.parent)
            atomic_write_text(path, payload.decode("utf-8"))
        agent_deltas.append(FileDelta(path=path, action=action))

    for hook_spec in HOOK_REGISTRY:
        path = _hook_target(target_dir, hook_spec)
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

    config_path = _config_target(target_dir)
    managed_body = _build_managed_body(ts)
    config_bytes = _patch_config_toml(config_path, managed_body)
    config_action = _classify(config_path, config_bytes)
    if not dry_run:
        _ensure_dir(config_path.parent)
        atomic_write_text(config_path, config_bytes.decode("utf-8"))
    config_delta = FileDelta(path=config_path, action=config_action)

    logger.info(
        f"install_plugin runtime=codex target={target_dir} skills={len(skill_deltas)} "
        f"agents={len(agent_deltas)} hooks={len(hook_deltas)} "
        f"config={config_action} dry_run={dry_run}"
    )
    return InstallResult(
        target_dir=target_dir,
        skills=skill_deltas,
        agents=agent_deltas,
        hooks=hook_deltas,
        config=config_delta,
        dry_run=dry_run,
    )


def expected_paths(target_dir: Path) -> tuple[Mapping[str, Path], Path]:
    """Return ``({region_id: path, ...}, config_path)`` for *target_dir*."""
    target_dir = Path(target_dir).resolve()
    paths: dict[str, Path] = {}
    for spec in SKILL_REGISTRY:
        paths[f"plugin.codex.skill.{spec.skill_name}"] = _skill_target(target_dir, spec)
    for agent_spec in AGENT_REGISTRY:
        paths[f"plugin.codex.agent.{agent_spec.role}"] = _agent_target(target_dir, agent_spec)
    for hook_spec in HOOK_REGISTRY:
        paths[f"plugin.codex.hook.{hook_spec.event_type.value}"] = _hook_target(
            target_dir, hook_spec
        )
    return paths, _config_target(target_dir)


__all__ = [
    "FileDelta",
    "InstallResult",
    "IntegrityViolation",
    "expected_paths",
    "install_plugin",
]
