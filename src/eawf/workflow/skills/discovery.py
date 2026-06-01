"""Layered skill discovery for the eawf skill registry (P14-W09 / B061).

Sources, highest precedence first:

1. Workspace catalogue — ``<workspace>/.ea/skills/<name>/SKILL.md``.
2. User catalogue — ``~/.eawf/skills/<name>/SKILL.md``.
3. Built-in registry — :data:`eawf.surfaces.render.skills.SKILL_REGISTRY`.

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
    reconcile_skills(skills_root)    -> SkillReconcileReport

The reconcile path compares the frozen built-in
:data:`~eawf.surfaces.render.skills.SKILL_REGISTRY` against the rendered
plugin skill tree on disk (``<root>/<name>/SKILL.md``). It reports three
drift classes: skills in the registry with no SKILL.md on disk
(``missing_on_disk``), SKILL.md dirs on disk with no registry row
(``extra_on_disk``), and on-disk frontmatter flags that disagree with
the registry (``flag_mismatches``). The rendered tree uses hyphenated
frontmatter keys (``user-invocable`` / ``disable-model-invocation``),
distinct from the underscore frontmatter the user/workspace overlay
catalogue uses, so the reconcile parser reads the hyphenated keys
directly rather than reusing :func:`parse_user_skill`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from eawf.surfaces.render.skills import SKILL_REGISTRY

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
                logger.warning(f"discover_skills source=workspace name={name!r} skipped={exc}")
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
            logger.warning(f"discover_skills source=user name={name!r} skipped={exc}")
            continue
        discovered[name] = DiscoveredSkill(**{**parsed.__dict__, "name": name})

    for spec in SKILL_REGISTRY:
        normalized = spec.skill_name if spec.skill_name.startswith("/") else f"/{spec.skill_name}"
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


# ---------------------------------------------------------------------------
# Registry-vs-disk reconcile. Compares the frozen built-in registry against
# the rendered plugin skill tree (``<root>/<name>/SKILL.md``) so an operator
# can spot a plugin tree that drifted from the canonical skill set after an
# out-of-band edit or a stale ``plugin install``.
# ---------------------------------------------------------------------------


_RENDERED_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_RENDERED_FLAG_RE = re.compile(r"^(user-invocable|disable-model-invocation):\s*(\S+)\s*$")


@dataclass(frozen=True)
class SkillFlags:
    """The two invocation flags carried in a SKILL.md frontmatter block."""

    user_invocable: bool
    disable_model_invocation: bool


@dataclass(frozen=True)
class SkillFlagMismatch:
    """One skill whose on-disk flags disagree with the registry."""

    name: str
    registry_flags: SkillFlags
    disk_flags: SkillFlags


@dataclass(frozen=True)
class SkillReconcileReport:
    """Drift report comparing the built-in registry against the disk tree.

    Attributes:
        skills_root: The rendered-skill-tree root the report scanned.
        missing_on_disk: Bare registry skill names with no SKILL.md on
            disk under *skills_root*, sorted.
        extra_on_disk: Bare skill names present on disk under
            *skills_root* with no matching registry row, sorted.
        flag_mismatches: Per-skill flag disagreements (registry vs disk),
            sorted by name.
    """

    skills_root: Path
    missing_on_disk: tuple[str, ...]
    extra_on_disk: tuple[str, ...]
    flag_mismatches: tuple[SkillFlagMismatch, ...]

    @property
    def has_drift(self) -> bool:
        """Return whether any drift class is non-empty."""
        return bool(self.missing_on_disk or self.extra_on_disk or self.flag_mismatches)


def _coerce_flag(path: Path, key: str, raw: str) -> bool:
    """Coerce a rendered ``true``/``false`` flag token into a bool.

    Raises:
        SkillFrontmatterError: When *raw* is not the lowercase ``true`` or
            ``false`` the renderer emits.
    """
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise SkillFrontmatterError(f"{path}: {key!r} must be 'true' or 'false', got {raw!r}")


def _parse_rendered_flags(path: Path) -> SkillFlags:
    """Read the two invocation flags from a rendered SKILL.md frontmatter.

    The rendered plugin tree uses hyphenated keys (``user-invocable`` /
    ``disable-model-invocation``) rendered as plain ``key: true|false``
    lines. The block is line-scanned rather than YAML-parsed because the
    sibling ``description`` value renders unquoted and may carry an
    embedded ``": "`` that a YAML mapping load would misread. Absent flag
    lines fall back to the registry defaults (``user_invocable=True`` /
    ``disable_model_invocation=False``).

    Raises:
        SkillFrontmatterError: When *path* has no ``---`` frontmatter
            block, or a flag line holds a non-boolean token.
    """
    text = path.read_text(encoding="utf-8")
    match = _RENDERED_FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillFrontmatterError(
            f"{path}: missing or malformed frontmatter block (expected '---' delimiters)"
        )
    user_invocable = True
    disable_model_invocation = False
    for line in match.group(1).splitlines():
        flag_match = _RENDERED_FLAG_RE.match(line.strip())
        if flag_match is None:
            continue
        key, raw = flag_match.group(1), flag_match.group(2)
        value = _coerce_flag(path, key, raw)
        if key == "user-invocable":
            user_invocable = value
        else:
            disable_model_invocation = value
    return SkillFlags(
        user_invocable=user_invocable,
        disable_model_invocation=disable_model_invocation,
    )


def reconcile_skills(skills_root: Path | str) -> SkillReconcileReport:
    """Diff the built-in skill registry against a rendered skill tree.

    Walks ``<skills_root>/<name>/SKILL.md`` and compares the discovered
    names + frontmatter flags against
    :data:`~eawf.surfaces.render.skills.SKILL_REGISTRY` (the frozen,
    canonical skill set). The registry is the source of truth; disk
    entries that disagree are reported as drift rather than silently
    reconciled.

    Args:
        skills_root: Root of the rendered plugin skill tree (e.g.
            ``<workspace>/.claude/skills``). A non-existent root yields a
            report whose every registry skill is ``missing_on_disk`` and
            whose other drift classes are empty.

    Returns:
        A :class:`SkillReconcileReport`. ``report.has_drift`` is ``False``
        iff the tree exactly mirrors the registry (same names, same flags).
    """
    root = Path(skills_root)
    registry_flags: dict[str, SkillFlags] = {
        spec.skill_name: SkillFlags(
            user_invocable=spec.user_invocable,
            disable_model_invocation=spec.disable_model_invocation,
        )
        for spec in SKILL_REGISTRY
    }

    disk_flags: dict[str, SkillFlags] = {}
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                disk_flags[entry.name] = _parse_rendered_flags(skill_md)
            except SkillFrontmatterError as exc:
                logger.warning(f"reconcile_skills name={entry.name!r} skipped={exc}")
                continue

    registry_names = set(registry_flags)
    disk_names = set(disk_flags)
    missing_on_disk = tuple(sorted(registry_names - disk_names))
    extra_on_disk = tuple(sorted(disk_names - registry_names))
    mismatches = tuple(
        SkillFlagMismatch(
            name=name,
            registry_flags=registry_flags[name],
            disk_flags=disk_flags[name],
        )
        for name in sorted(registry_names & disk_names)
        if registry_flags[name] != disk_flags[name]
    )
    logger.info(
        f"reconcile_skills root={str(root)!r} missing={len(missing_on_disk)} "
        f"extra={len(extra_on_disk)} mismatched={len(mismatches)}"
    )
    return SkillReconcileReport(
        skills_root=root,
        missing_on_disk=missing_on_disk,
        extra_on_disk=extra_on_disk,
        flag_mismatches=mismatches,
    )


__all__ = [
    "DiscoveredSkill",
    "SkillFlagMismatch",
    "SkillFlags",
    "SkillFrontmatterError",
    "SkillReconcileReport",
    "discover_skills",
    "parse_user_skill",
    "reconcile_skills",
    "user_skills_dir",
    "workspace_skills_dir",
]
