"""Emit a standalone Claude Code plugin tree at ``<target>/``.

Per Phase 6 W05, ``eawf plugin package claude`` produces an installable
plugin tree the user can register via ``/plugin marketplace add <path>``
and install via ``/plugin install eawf@eawf-local``. The output layout
differs from :mod:`eawf.runtimes.claude.plugin_install`:

::

    <target>/
      .claude-plugin/
        plugin.json                  # always emitted
        marketplace.json             # gated by ``include_marketplace``
      skills/<name>/SKILL.md         # one per SKILL_REGISTRY entry
      agents/<role>.md               # one per AGENT_REGISTRY entry
      README.md                      # gated by ``include_readme``

Crucially, this tree carries NO ``.claude/`` prefix, NO ``hooks/``, NO
``settings.json``, and NO ``.ea/`` — it is a self-contained plugin, not a
per-repo workspace render. Hooks are deferred to v0.2 (B015): Eä's
custom hook events (``phase_open``/``wave_close``/``iter_open``) do not
map onto the Claude Code standard event taxonomy.

Public API::

    PackageResult                              # dataclass: target, deltas, flags
    package_plugin(target_dir, *, ...) -> PackageResult
    render_plugin_manifest(...)                # pure helper used by unit tests
"""

from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

import eawf
from eawf.render._atomic import atomic_write_text
from eawf.render.agents import AGENT_REGISTRY, AgentSpec, AgentTemplateContext, render_agent_md
from eawf.render.skills import (
    SKILL_REGISTRY,
    SkillSpec,
    SkillTemplateContext,
    render_skill_md,
)
from eawf.runtimes.claude.plugin_install import IntegrityViolation

logger = logging.getLogger(__name__)


_TEMPLATES_PACKAGE: str = "eawf.templates.claude"
_PLUGIN_MANIFEST_TEMPLATE: str = "plugin.json.j2"
_MARKETPLACE_TEMPLATE: str = "marketplace.json.j2"
_README_TEMPLATE: str = "plugin-readme.md.j2"

_PLUGIN_NAME: str = "eawf"


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
    """

    target: Path
    dry_run: bool
    skills: list[str]
    agents: list[str]
    wrote_marketplace: bool
    wrote_readme: bool


# --------------------------------------------------------------------------- #
# Pyproject metadata loader                                                   #
# --------------------------------------------------------------------------- #


def _project_root() -> Path:
    """Return the eawf source root that ships pyproject.toml.

    Walks up from this module's ``__file__`` until a ``pyproject.toml``
    appears. Falls back to the cwd when run from a wheel without
    pyproject (the manifest then uses fallback values).
    """
    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    return Path.cwd()


def _read_pyproject_metadata() -> dict[str, Any]:
    """Parse ``[project]`` from pyproject.toml; return author/url metadata.

    Returns a dict with keys:
        author_name : str
        author_email: str | None  (read but never emitted)
        homepage    : str | None
        repository  : str | None

    All keys are optional — missing fields fall back to safe defaults.
    """
    pyproject_path = _project_root() / "pyproject.toml"
    metadata: dict[str, Any] = {
        "author_name": _PLUGIN_NAME,
        "author_email": None,
        "homepage": None,
        "repository": None,
    }
    if not pyproject_path.exists():
        logger.warning(f"pyproject.toml not found at {pyproject_path}; using fallback metadata")
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

    Mirrors :func:`eawf.render.skills._load_environment` so loader
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
    except OSError, json.JSONDecodeError:
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

    skill_names = [spec.skill_name for spec in SKILL_REGISTRY]
    agent_roles = [spec.role for spec in AGENT_REGISTRY]

    if dry_run:
        logger.info(
            f"package_plugin dry-run target={target_dir} skills={len(skill_names)} "
            f"agents={len(agent_roles)} marketplace={include_marketplace} "
            f"readme={include_readme}"
        )
        return PackageResult(
            target=target_dir,
            dry_run=True,
            skills=skill_names,
            agents=agent_roles,
            wrote_marketplace=include_marketplace,
            wrote_readme=include_readme,
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

    logger.info(
        f"package_plugin target={target_dir} skills={len(skill_names)} "
        f"agents={len(agent_roles)} marketplace={include_marketplace} "
        f"readme={include_readme}"
    )
    return PackageResult(
        target=target_dir,
        dry_run=False,
        skills=skill_names,
        agents=agent_roles,
        wrote_marketplace=include_marketplace,
        wrote_readme=include_readme,
    )


__all__ = [
    "PackageResult",
    "package_plugin",
    "render_plugin_manifest",
]
