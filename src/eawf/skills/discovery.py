"""Layered skill discovery for the eawf skill registry (P14-W09 / B061).

Sources, highest precedence first:

1. Workspace catalogue — ``<workspace>/.ea/skills/<name>/SKILL.md``.
2. User catalogue — ``~/.eawf/skills/<name>/SKILL.md``.
3. Built-in registry — :data:`eawf.render.skills.SKILL_REGISTRY`.

The discovered union is what ``eawf skill list`` exposes through the
new ``--scope`` filter. Workspace and user SKILL.md files carry YAML
frontmatter parsed by :func:`parse_user_skill`; invalid frontmatter
surfaces with the file path so the operator can fix it in place
without crashing the CLI.

Frontmatter contract (workspace / user catalogue entries):

::

    ---
    name: <skill_name>           # required, slash-prefixed
    description: <one-liner>     # required
    runtimes: [claude, codex]    # optional; empty/absent == visible to all
    user_invocable: true         # optional; defaults to True
    disable_model_invocation: false  # optional; defaults to False
    argument_hint: ""            # optional; defaults to ""
    version: "1.0"               # optional; defaults to "1.0"
    ---
    <body markdown>

Public API:

    discover_skills(workspace=None)  -> list[DiscoveredSkill]
    parse_user_skill(path)           -> DiscoveredSkill
    user_skills_dir()                -> Path
    workspace_skills_dir(workspace)  -> Path
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from eawf.render.skills import SKILL_REGISTRY

logger = logging.getLogger(__name__)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


class SkillFrontmatterError(Exception):
    """Raised when a workspace/user SKILL.md fails frontmatter validation."""


@dataclass(frozen=True)
class DiscoveredSkill:
    """One skill resolvable through the layered registry."""

    name: str
    source: str  # Literal["builtin", "user", "workspace"]
    path: Path | None
    description: str
    body: str = ""
    argument_hint: str = ""
    user_invocable: bool = True
    disable_model_invocation: bool = False
    version: str = "1.0"
    runtimes: tuple[str, ...] = field(default_factory=tuple)


def user_skills_dir() -> Path:
    """``~/.eawf/skills`` — user-scope skill catalogue root."""
    return Path.home() / ".eawf" / "skills"


def workspace_skills_dir(workspace: Path | str) -> Path:
    """``<workspace>/.ea/skills`` — workspace-scope skill catalogue root."""
    return Path(workspace) / ".ea" / "skills"


def _iter_skill_files(root: Path) -> dict[str, Path]:
    """Return ``{skill_name: SKILL.md path}`` under *root*.

    Each direct subdirectory of *root* must contain a ``SKILL.md`` to
    register as a discoverable skill. Skill names are slash-prefixed to
    match the registry literal contract; the subdirectory stem provides
    the bare name.
    """
    if not root.is_dir():
        return {}
    out: dict[str, Path] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        out[f"/{entry.name}"] = skill_md
    return out


def parse_user_skill(path: Path, *, source: str) -> DiscoveredSkill:
    """Parse a workspace/user SKILL.md and return a :class:`DiscoveredSkill`.

    Args:
        path: Filesystem path to ``SKILL.md``.
        source: ``"user"`` or ``"workspace"`` — recorded on the
            returned dataclass so callers can attribute provenance.

    Raises:
        SkillFrontmatterError: Missing/invalid frontmatter, missing
            required fields, or runtimes containing non-string entries.
    """
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillFrontmatterError(
            f"{path}: missing or malformed frontmatter block (expected '---' delimiters)"
        )
    yaml_block = match.group(1)
    body = match.group(2)
    try:
        meta: Any = yaml.safe_load(yaml_block) or {}
    except yaml.YAMLError as exc:
        raise SkillFrontmatterError(f"{path}: YAML frontmatter parse failed: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillFrontmatterError(
            f"{path}: frontmatter must be a mapping, got {type(meta).__name__}"
        )
    raw_name = meta.get("name")
    if not isinstance(raw_name, str) or not raw_name:
        raise SkillFrontmatterError(f"{path}: 'name' field is required (string)")
    description = meta.get("description")
    if not isinstance(description, str):
        raise SkillFrontmatterError(f"{path}: 'description' field is required (string)")
    runtimes_raw = meta.get("runtimes", [])
    if not isinstance(runtimes_raw, list):
        raise SkillFrontmatterError(f"{path}: 'runtimes' must be a list of strings")
    runtimes = tuple(runtimes_raw)
    if any(not isinstance(r, str) for r in runtimes):
        raise SkillFrontmatterError(f"{path}: every entry in 'runtimes' must be a string")
    name = raw_name if raw_name.startswith("/") else f"/{raw_name}"
    return DiscoveredSkill(
        name=name,
        source=source,
        path=path,
        description=description,
        body=body,
        argument_hint=str(meta.get("argument_hint", "")),
        user_invocable=bool(meta.get("user_invocable", True)),
        disable_model_invocation=bool(meta.get("disable_model_invocation", False)),
        version=str(meta.get("version", "1.0")),
        runtimes=runtimes,
    )


def discover_skills(
    *,
    workspace: Path | str | None = None,
    runtime: str | None = None,
) -> list[DiscoveredSkill]:
    """Enumerate every skill resolvable across the three layers.

    Args:
        workspace: Optional workspace root for the workspace overlay.
        runtime: When set, drops entries whose ``runtimes`` frontmatter
            field excludes *runtime*. Empty / absent ``runtimes`` is
            interpreted as "visible to all runtimes".

    Returns:
        List of :class:`DiscoveredSkill` in deterministic sorted order
        by name. The highest-precedence entry per name wins; lower
        layers are dropped silently. Invalid workspace/user entries are
        logged at WARNING level and skipped.
    """
    discovered: dict[str, DiscoveredSkill] = {}

    if workspace is not None:
        for name, path in _iter_skill_files(workspace_skills_dir(workspace)).items():
            try:
                parsed = parse_user_skill(path, source="workspace")
            except SkillFrontmatterError as exc:
                logger.warning(f"skill discovery skip workspace {name!r}: {exc}")
                continue
            # The directory stem is canonical; override the body's name so
            # ``<root>/<id>/SKILL.md`` always discoverable as ``/<id>``.
            discovered[name] = DiscoveredSkill(**{**parsed.__dict__, "name": name})

    for name, path in _iter_skill_files(user_skills_dir()).items():
        if name in discovered:
            continue
        try:
            parsed = parse_user_skill(path, source="user")
        except SkillFrontmatterError as exc:
            logger.warning(f"skill discovery skip user {name!r}: {exc}")
            continue
        discovered[name] = DiscoveredSkill(**{**parsed.__dict__, "name": name})

    for spec in SKILL_REGISTRY:
        normalized = (
            spec.skill_name if spec.skill_name.startswith("/") else f"/{spec.skill_name}"
        )
        if normalized in discovered:
            continue
        discovered[normalized] = DiscoveredSkill(
            name=normalized,
            source="builtin",
            path=None,
            description=spec.description,
            body=spec.body,
            argument_hint=spec.argument_hint,
            user_invocable=spec.user_invocable,
            disable_model_invocation=spec.disable_model_invocation,
            version=spec.version,
            runtimes=(),
        )

    rows = sorted(discovered.values(), key=lambda d: d.name)
    if runtime is not None:
        rows = [r for r in rows if not r.runtimes or runtime in r.runtimes]
    return rows


__all__ = [
    "DiscoveredSkill",
    "SkillFrontmatterError",
    "discover_skills",
    "parse_user_skill",
    "user_skills_dir",
    "workspace_skills_dir",
]
