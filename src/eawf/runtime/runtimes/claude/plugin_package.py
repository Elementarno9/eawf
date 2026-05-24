"""Emit a standalone Claude Code plugin tree at ``<target>/``.

Per Phase 6 W05, ``eawf plugin package claude`` produces an installable
plugin tree the user can register via ``/plugin marketplace add <path>``
and install via ``/plugin install eawf@eawf-local``. The output layout
differs from :mod:`eawf.runtime.runtimes.claude.plugin_install`:

::

    <target>/
      .claude-plugin/
        plugin.json                  # always emitted
        marketplace.json             # gated by ``include_marketplace``
      skills/<name>/SKILL.md         # one per SKILL_REGISTRY entry
      agents/<role>.md               # one per AGENT_REGISTRY entry
      README.md                      # gated by ``include_readme``

Crucially, this tree carries NO ``.claude/`` prefix, NO ``settings.json``,
and NO ``.ea/`` — it is a self-contained plugin, not a per-repo workspace
render.

Per P13 W05 (B015), the packaged tree now ALSO emits a session-level
``hooks.json`` at the plugin root plus the corresponding wrapper scripts
under ``hooks/``. Only the six session-level events Claude Code can
observe reliably (``SessionStart``, ``Stop``, ``PreToolUse``/
``PostToolUse`` on bash ``git commit``/``git push``) appear in the
manifest — workflow-internal lifecycle events (``wave_*``, ``iter_*``,
``phase_*``, ``*_audit``) stay fired by the state CLI through
``eawf hook run`` because CC's ``UserPromptSubmit`` matcher cannot
observe slash-command sub-skill dispatch or agent-driven state writes.

Public API::

    PackageResult                              # dataclass: target, deltas, flags
    package_plugin(target_dir, *, ...) -> PackageResult
    render_plugin_manifest(...)                # pure helper used by unit tests
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

import eawf
from eawf.runtime.runtimes.claude.hook_map import PLUGIN_HOOK_REGISTRY, render_plugin_hooks_json
from eawf.runtime.runtimes.claude.plugin_install import IntegrityViolation
from eawf.surfaces.render._atomic import atomic_write_text
from eawf.surfaces.render.agents import (
    AGENT_REGISTRY,
    AgentSpec,
    AgentTemplateContext,
    render_agent_md,
)
from eawf.surfaces.render.hooks import render_hook_sh
from eawf.surfaces.render.skills import (
    SKILL_REGISTRY,
    SkillSpec,
    SkillTemplateContext,
    render_skill_md,
)

logger = logging.getLogger(__name__)


_TEMPLATES_PACKAGE: str = "eawf.platform.templates.claude"
_PLUGIN_MANIFEST_TEMPLATE: str = "plugin.json.j2"
_MARKETPLACE_TEMPLATE: str = "marketplace.json.j2"
_README_TEMPLATE: str = "plugin-readme.md.j2"

_PLUGIN_NAME: str = "eawf"

# File mode for hook wrapper scripts — POSIX rwxr-xr-x. Mirrors
# :data:`eawf.runtime.runtimes.claude.plugin_install._HOOK_FILE_MODE` so the
# packaged tree behaves like the repo-install tree once mounted.
_HOOK_FILE_MODE: int = 0o755


# --------------------------------------------------------------------------- #
# Result dataclass                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PackageResult:
    """Summary of one :func:`package_plugin` call.

    Attributes:
        target: Resolved output directory.
        dry_run: True when no bytes were written.
        skills: Skill names emitted (registry order).
        agents: Agent role names emitted (registry order).
        wrote_marketplace: True when ``marketplace.json`` was emitted
            (or *would have been*, in a dry-run).
        wrote_readme: True when ``README.md`` was emitted (or would
            have been, in a dry-run).
        wrote_hooks: True when ``hooks.json`` + ``hooks/*.sh`` were
            emitted (or would have been, in a dry-run).
    """

    target: Path
    dry_run: bool
    skills: list[str]
    agents: list[str]
    wrote_marketplace: bool
    wrote_readme: bool
    wrote_hooks: bool


# --------------------------------------------------------------------------- #
# Pyproject metadata loader                                                   #
# --------------------------------------------------------------------------- #


_MAX_PYPROJECT_WALK_LEVELS: int = 2


def _eawf_pyproject() -> Path | None:
    """Return the path to eawf's own pyproject.toml, or ``None`` if not found.

    Anchors on :mod:`eawf` via :func:`importlib.resources.files` so the
    lookup cannot wander into a host project's pyproject when eawf is
    installed as a wheel inside another project tree. We walk up at
    most :data:`_MAX_PYPROJECT_WALK_LEVELS` directories and require the
    discovered ``pyproject.toml`` to declare ``[project].name == "eawf"``
    — otherwise we treat it as foreign and return ``None``.

    For source-tree (editable) installs this resolves to the repo's own
    pyproject; for wheel installs it returns ``None`` and the caller
    falls back to safe defaults.
    """
    package_root = Path(str(files("eawf"))).resolve()
    candidates = [package_root, *package_root.parents][: _MAX_PYPROJECT_WALK_LEVELS + 1]
    for candidate in candidates:
        pyproject_path = candidate / "pyproject.toml"
        if not pyproject_path.is_file():
            continue
        try:
            with pyproject_path.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.debug(f"_eawf_pyproject skip-unreadable path={pyproject_path} error={exc}")
            continue
        project = data.get("project", {})
        if project.get("name") == "eawf":
            return pyproject_path
        # First pyproject we found is for a host project; stop —
        # the eawf wheel does not ship a pyproject of its own.
        return None
    return None


def _read_pyproject_metadata() -> dict[str, Any]:
    """Parse ``[project]`` from eawf's pyproject.toml; return author/url metadata.

    Anchors on the eawf package (via :func:`_eawf_pyproject`) so wheel
    installs that live inside an unrelated host project do not pick up
    the host's pyproject metadata.

    Returns a dict with keys:
        author_name : str
        author_email: str | None  (read but never emitted)
        homepage    : str | None
        repository  : str | None

    All keys are optional — missing fields fall back to safe defaults.
    """
    metadata: dict[str, Any] = {
        "author_name": _PLUGIN_NAME,
        "author_email": None,
        "homepage": None,
        "repository": None,
    }
    pyproject_path = _eawf_pyproject()
    if pyproject_path is None:
        logger.debug("_read_pyproject_metadata pyproject-not-located; using fallback metadata")
        return metadata
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    project = data.get("project", {})
    authors = project.get("authors") or []
    if authors:
        first = authors[0]
        if isinstance(first, dict):
            metadata["author_name"] = first.get("name") or _PLUGIN_NAME
            metadata["author_email"] = first.get("email")
    urls = project.get("urls") or {}
    homepage = urls.get("Homepage") or urls.get("homepage")
    repository = urls.get("Repository") or urls.get("repository")
    metadata["homepage"] = homepage or repository  # repo doubles as homepage when only one URL.
    metadata["repository"] = repository
    return metadata


# --------------------------------------------------------------------------- #
# Template environment                                                        #
# --------------------------------------------------------------------------- #


def _load_environment() -> Environment:
    """Load a Jinja2 environment rooted at the bundled claude templates dir.

    Mirrors :func:`eawf.surfaces.render.skills._load_environment` so loader
    behaviour is consistent across renderers (StrictUndefined,
    ``keep_trailing_newline=False``, autoescape off).
    """
    templates_dir = files(_TEMPLATES_PACKAGE)
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=False,
        autoescape=False,
    )
    return env


# --------------------------------------------------------------------------- #
# Manifest rendering                                                          #
# --------------------------------------------------------------------------- #


def render_plugin_manifest(
    *,
    version: str,
    author_name: str,
    author_email: str | None,
    homepage: str | None,
    repository: str | None,
) -> str:
    """Render ``plugin.json`` text. ``author_email`` is consumed but never emitted.

    The Jinja template only references ``author_name``/``homepage``/
    ``repository`` so the email argument is consciously dropped at the
    boundary. The rendered template body is parsed and re-serialised
    through :func:`json.dumps` with ``sort_keys=True`` + 2-space indent
    so the output is byte-stable across Jinja whitespace tweaks.

    Args:
        version: eawf package version (e.g. ``"0.1.0.dev0"``).
        author_name: Author display name.
        author_email: Read for symmetry with pyproject; never written.
        homepage: Optional URL; key omitted when ``None``.
        repository: Optional URL; key omitted when ``None``.

    Returns:
        Canonical JSON text (sorted keys, 2-space indent, trailing
        newline). The ``author`` object never carries an ``email`` field.
    """
    # ``author_email`` is intentionally consumed but not used — the
    # template does not reference it. The argument exists so callers
    # that already pull the field from pyproject can pass it through
    # without a separate scrub step.
    del author_email
    env = _load_environment()
    template = env.get_template(_PLUGIN_MANIFEST_TEMPLATE)
    rendered = template.render(
        version=version,
        author_name=author_name,
        homepage=homepage,
        repository=repository,
    )
    parsed = json.loads(rendered)
    canonical = json.dumps(parsed, sort_keys=True, indent=2) + "\n"
    return canonical


def _render_marketplace(*, author_name: str) -> str:
    """Render ``marketplace.json`` text (sorted-keys canonical form)."""
    env = _load_environment()
    template = env.get_template(_MARKETPLACE_TEMPLATE)
    rendered = template.render(author_name=author_name)
    parsed = json.loads(rendered)
    canonical = json.dumps(parsed, sort_keys=True, indent=2) + "\n"
    return canonical


def _render_readme() -> str:
    """Render the plugin-tree ``README.md``."""
    env = _load_environment()
    template = env.get_template(_README_TEMPLATE)
    rendered = template.render()
    if not rendered.endswith("\n"):
        rendered = rendered + "\n"
    return rendered


def _render_skill(spec: SkillSpec) -> str:
    """Render one skill's SKILL.md. Mirrors plugin_install._render_skill."""
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
    """Render one agent's markdown. Mirrors plugin_install._render_agent."""
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


# --------------------------------------------------------------------------- #
# Target-directory safety                                                     #
# --------------------------------------------------------------------------- #


def _is_own_previous_output(target_dir: Path) -> bool:
    """Return True when *target_dir* already holds an eawf plugin tree.

    Detected by ``.claude-plugin/plugin.json`` whose top-level ``name``
    equals ``"eawf"``. This lets re-packaging into the same directory
    succeed without ``--force`` while still refusing to clobber a
    foreign tree.
    """
    manifest_path = target_dir / ".claude-plugin" / "plugin.json"
    if not manifest_path.is_file():
        return False
    try:
        body = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug(
            f"_is_own_previous_output unreadable-manifest path={manifest_path} error={exc}"
        )
        return False
    if not isinstance(body, dict):
        return False
    return body.get("name") == _PLUGIN_NAME


def _check_target(target_dir: Path, *, force: bool) -> None:
    """Raise :class:`IntegrityViolation` if *target_dir* is unsafe to write.

    Rules:

    - target does not exist → safe.
    - target is empty       → safe.
    - target holds a previous eawf plugin output → safe (re-package).
    - target holds anything else → unsafe; require ``force=True``.
    """
    if force:
        return
    if not target_dir.exists():
        return
    if not target_dir.is_dir():
        raise IntegrityViolation(
            f"package target {target_dir} exists but is not a directory; refusing to overwrite."
        )
    children = list(target_dir.iterdir())
    if not children:
        return
    if _is_own_previous_output(target_dir):
        return
    raise IntegrityViolation(
        f"package target {target_dir} is non-empty and is not a previous "
        f"eawf plugin output; rerun with --force to overwrite."
    )


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #


def package_plugin(
    target_dir: Path,
    *,
    include_marketplace: bool = True,
    include_readme: bool = True,
    include_hooks: bool = True,
    force: bool = False,
    dry_run: bool = False,
) -> PackageResult:
    """Emit an installable Claude Code plugin tree under *target_dir*.

    Args:
        target_dir: Destination directory. Created on demand. Refuses
            to write into a non-empty directory unless that directory
            already holds an eawf plugin output, or ``force=True``.
        include_marketplace: Emit ``.claude-plugin/marketplace.json``
            so the directory works as a single-plugin local marketplace.
        include_readme: Emit ``README.md`` describing install steps.
        include_hooks: Emit ``hooks.json`` at the plugin root plus
            ``hooks/<event>.sh`` wrappers for the six session-level
            events in
            :data:`eawf.runtime.runtimes.claude.hook_map.PLUGIN_HOOK_REGISTRY`.
            Defaults to ``True``; pass ``False`` to package a
            skills/agents-only tree.
        force: Bypass the non-empty-target check.
        dry_run: Resolve everything (registry walk, manifest render)
            but write no bytes.

    Returns:
        :class:`PackageResult` summarising the emit. ``skills`` /
        ``agents`` are populated even on dry-run so the caller can
        surface the plan.

    Raises:
        IntegrityViolation: ``target_dir`` is unsafe to overwrite (see
            :func:`_check_target`).
    """
    target_dir = Path(target_dir).resolve()

    _check_target(target_dir, force=force)

    metadata = _read_pyproject_metadata()
    version = eawf.__version__
    author_name = metadata["author_name"]
    author_email = metadata["author_email"]
    homepage = metadata["homepage"]
    repository = metadata["repository"]

    # Pre-render everything before touching disk so a render error in
    # the middle of the walk does not leave a half-written tree.
    plugin_manifest = render_plugin_manifest(
        version=version,
        author_name=author_name,
        author_email=author_email,
        homepage=homepage,
        repository=repository,
    )
    marketplace = _render_marketplace(author_name=author_name) if include_marketplace else None
    readme = _render_readme() if include_readme else None

    skill_outputs: list[tuple[Path, str]] = []
    for spec in SKILL_REGISTRY:
        path = target_dir / "skills" / spec.skill_name / "SKILL.md"
        skill_outputs.append((path, _render_skill(spec)))

    agent_outputs: list[tuple[Path, str]] = []
    for agent_spec in AGENT_REGISTRY:
        path = target_dir / "agents" / f"{agent_spec.role}.md"
        agent_outputs.append((path, _render_agent(agent_spec)))

    # Pre-render hooks.json + per-event wrappers (B015). Only the six
    # session-level events in PLUGIN_HOOK_REGISTRY are emitted; the
    # workflow-internal lifecycle events stay fired by the state CLI
    # through ``eawf hook run`` (see hook_map.py for the rationale).
    hooks_manifest: str | None = None
    hook_outputs: list[tuple[Path, str]] = []
    if include_hooks:
        hooks_manifest = render_plugin_hooks_json()
        # Deduplicate by event_type — the registry may legitimately
        # list the same event under multiple CC events in a future
        # extension; we only need one wrapper per event_type on disk.
        seen: set[str] = set()
        for hook_spec in PLUGIN_HOOK_REGISTRY:
            value = hook_spec.event_type.value
            if value in seen:
                continue
            seen.add(value)
            path = target_dir / "hooks" / f"{value}.sh"
            hook_outputs.append((path, render_hook_sh(hook_spec.event_type)))

    skill_names = [spec.skill_name for spec in SKILL_REGISTRY]
    agent_roles = [spec.role for spec in AGENT_REGISTRY]

    if dry_run:
        logger.info(
            f"package_plugin dry-run target={target_dir} skills={len(skill_names)} "
            f"agents={len(agent_roles)} marketplace={include_marketplace} "
            f"readme={include_readme} hooks={include_hooks}"
        )
        return PackageResult(
            target=target_dir,
            dry_run=True,
            skills=skill_names,
            agents=agent_roles,
            wrote_marketplace=include_marketplace,
            wrote_readme=include_readme,
            wrote_hooks=include_hooks,
        )

    target_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = target_dir / ".claude-plugin" / "plugin.json"
    atomic_write_text(manifest_path, plugin_manifest)

    if marketplace is not None:
        market_path = target_dir / ".claude-plugin" / "marketplace.json"
        atomic_write_text(market_path, marketplace)

    if readme is not None:
        readme_path = target_dir / "README.md"
        atomic_write_text(readme_path, readme)

    for path, body in skill_outputs:
        atomic_write_text(path, body)

    for path, body in agent_outputs:
        atomic_write_text(path, body)

    if hooks_manifest is not None:
        # hooks.json lives at the plugin tree root (NOT under
        # ``.claude-plugin/``) per the Claude Code plugin manifest schema.
        hooks_json_path = target_dir / "hooks.json"
        atomic_write_text(hooks_json_path, hooks_manifest)
        for path, body in hook_outputs:
            atomic_write_text(path, body)
            os.chmod(path, _HOOK_FILE_MODE)

    logger.info(
        f"package_plugin target={target_dir} skills={len(skill_names)} "
        f"agents={len(agent_roles)} marketplace={include_marketplace} "
        f"readme={include_readme} hooks={include_hooks}"
    )
    return PackageResult(
        target=target_dir,
        dry_run=False,
        skills=skill_names,
        agents=agent_roles,
        wrote_marketplace=include_marketplace,
        wrote_readme=include_readme,
        wrote_hooks=include_hooks,
    )


__all__ = [
    "PackageResult",
    "package_plugin",
    "render_plugin_manifest",
]
