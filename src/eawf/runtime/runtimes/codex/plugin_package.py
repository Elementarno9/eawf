"""Emit a standalone Codex CLI marketplace tree at ``<target>/`` (P14-I02-W01 hotfix).

Per the Codex Build-plugin reference, dropping a plugin directly under
``~/.codex/plugins/<name>/`` does **not** auto-load it — Codex requires
a marketplace registration step. ``eawf plugin install codex`` writes
the plugin tree (skills, hooks, ``.codex-plugin/plugin.json``) at the
scope-correct location and flips ``[plugins.eawf] enabled = true`` in
``config.toml``, but the operator still has to register a marketplace
for Codex to discover it.

This module emits a self-contained marketplace tree the operator can
register with one command::

    eawf plugin package codex --target ./build/eawf-codex-marketplace
    codex plugin marketplace add ./build/eawf-codex-marketplace

After ``marketplace add`` the plugin auto-registers; the
``[plugins.eawf] enabled = true`` block that ``eawf plugin install
codex`` writes to ``config.toml`` activates it. Codex has no separate
``plugin install`` subcommand (only ``plugin marketplace add /
upgrade / remove``).

Layout::

    <target>/
      .agents/plugins/
        marketplace.json                     # Codex marketplace manifest
      plugins/
        eawf/
          .codex-plugin/
            plugin.json                      # canonical plugin manifest
          skills/<name>/SKILL.md
          hooks/<event>.sh

The marketplace manifest sits at ``.agents/plugins/marketplace.json``
per the Codex Build-plugin reference; ``codex plugin marketplace add
<target>`` rejects roots that only carry a root-level
``marketplace.json``.

The renderer is idempotent: re-running produces a byte-identical tree.
Public API mirrors the Claude package adapter:

    PackageResult                              (dataclass: per-file deltas)
    package_plugin(target_dir, *, force, dry_run) -> PackageResult
    IntegrityViolation                          (re-exported)
"""

from __future__ import annotations

import enum
import json
import logging
import os
import tomllib
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from eawf.runtime.runtimes.codex.hook_map import codex_hook_name
from eawf.runtime.runtimes.codex.plugin_install import (
    IntegrityViolation,
    _render_manifest,
    _render_skill,
)
from eawf.surfaces.render._atomic import atomic_write_text
from eawf.surfaces.render.hooks import HOOK_REGISTRY, render_hook_sh
from eawf.surfaces.render.skills import SKILL_REGISTRY

logger = logging.getLogger(__name__)


_MARKETPLACE_NAME: str = "eawf-codex"
_MARKETPLACE_DISPLAY: str = "Eä Workflow (local)"
_PLUGIN_NAME: str = "eawf"
_PLUGIN_CATEGORY: str = "Productivity"
_HOOK_FILE_MODE: int = 0o755
_MANIFEST_DIR: str = ".codex-plugin"
_MANIFEST_FILE: str = "plugin.json"
_MARKETPLACE_SUBDIR: tuple[str, ...] = (".agents", "plugins")
_MARKETPLACE_FILE: str = "marketplace.json"

#: Sub-directory the published ``git-subdir`` pointer references inside the
#: ``plugins-dist`` branch — mirrors the ``plugins/eawf`` layout this packager
#: emits under any local target.
_PUBLISHED_SUBDIR: str = f"./plugins/{_PLUGIN_NAME}"
#: Branch the self-hosted CI publish step pushes the rendered tree onto. The
#: pointer references it declaratively; the branch itself lands in a later wave.
_PUBLISHED_REF: str = "plugins-dist"
#: Fallback repo URL when pyproject metadata cannot be located (wheel install).
#: Kept in sync with ``pyproject.toml [project.urls].Repository``.
_FALLBACK_REPOSITORY: str = "https://github.com/Elementarno9/eawf"

_MAX_PYPROJECT_WALK_LEVELS: int = 2


class PublishSource(enum.StrEnum):
    """Marketplace ``source`` shape the packager emits.

    ``LOCAL`` is the dev default — a relative-path source the operator
    registers with ``codex plugin marketplace add ./path``. ``GIT_SUBDIR``
    is the self-hosted published form that points Codex at the rendered tree
    on the ``plugins-dist`` branch of the canonical repo (the tree need not
    live in the marketplace repo per the Codex ``git-subdir`` source kind).
    """

    LOCAL = "local"
    GIT_SUBDIR = "git-subdir"


def _eawf_repository_url() -> str:
    """Return the canonical repo URL from eawf's own ``pyproject.toml``.

    Anchors on the :mod:`eawf` package so a wheel install nested inside an
    unrelated host project cannot pick up the host's pyproject. Walks up at
    most :data:`_MAX_PYPROJECT_WALK_LEVELS` levels and requires
    ``[project].name == "eawf"`` before trusting the file. Falls back to
    :data:`_FALLBACK_REPOSITORY` when no eawf pyproject is found (wheel
    install) so the published pointer never carries a host project's URL.
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
            logger.debug(f"_eawf_repository_url skip-unreadable path={pyproject_path} error={exc}")
            continue
        project = data.get("project", {})
        if project.get("name") != "eawf":
            # First pyproject found is a host project's; stop walking.
            break
        urls = project.get("urls") or {}
        repository = urls.get("Repository") or urls.get("repository")
        if isinstance(repository, str) and repository:
            return repository
        break
    logger.debug("_eawf_repository_url pyproject-not-located; using fallback repository URL")
    return _FALLBACK_REPOSITORY


@dataclass(frozen=True)
class FileDelta:
    """One file the packager wrote / would have written."""

    path: Path
    action: str  # Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class PackageResult:
    """Summary of one :func:`package_plugin` call."""

    target: Path
    skills: list[FileDelta] = field(default_factory=list)
    hooks: list[FileDelta] = field(default_factory=list)
    manifest: FileDelta | None = None
    marketplace: FileDelta | None = None
    dry_run: bool = False


def _classify(path: Path, payload: bytes) -> str:
    if not path.exists():
        return "created"
    if path.read_bytes() == payload:
        return "unchanged"
    return "updated"


def _plugin_root(target: Path) -> Path:
    return target / "plugins" / _PLUGIN_NAME


def _render_source(publish_source: PublishSource) -> dict[str, str]:
    """Return the ``plugins[].source`` object for *publish_source*.

    ``LOCAL`` emits the relative-path dev source the operator registers via
    ``codex plugin marketplace add ./path``. ``GIT_SUBDIR`` emits the
    self-hosted published pointer: ``url`` from the canonical pyproject
    Repository, ``path`` the rendered sub-tree, and ``ref`` the publish
    branch. Per the Codex ``git-subdir`` source kind ``ref`` and ``sha`` are
    mutually exclusive; this packager pins ``ref`` (a moving branch tip).
    """
    if publish_source is PublishSource.GIT_SUBDIR:
        return {
            "source": "git-subdir",
            "url": _eawf_repository_url(),
            "path": _PUBLISHED_SUBDIR,
            "ref": _PUBLISHED_REF,
        }
    return {"source": "local", "path": _PUBLISHED_SUBDIR}


def _render_marketplace(publish_source: PublishSource = PublishSource.LOCAL) -> bytes:
    """Render ``marketplace.json`` using the Codex marketplace schema.

    Required fields (from Codex Build-plugin reference):
    ``name``, ``interface.displayName``, ``plugins`` array with each
    entry carrying ``name``, ``source`` (shape per :func:`_render_source`),
    ``policy.installation``, ``policy.authentication``, and ``category``.

    Args:
        publish_source: ``LOCAL`` (dev default) or ``GIT_SUBDIR`` (the
            self-hosted published pointer form).
    """
    body: dict[str, object] = {
        "name": _MARKETPLACE_NAME,
        "interface": {"displayName": _MARKETPLACE_DISPLAY},
        "plugins": [
            {
                "name": _PLUGIN_NAME,
                "source": _render_source(publish_source),
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": _PLUGIN_CATEGORY,
            }
        ],
    }
    return (json.dumps(body, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _marketplace_path(target: Path) -> Path:
    path = target
    for part in _MARKETPLACE_SUBDIR:
        path = path / part
    return path / _MARKETPLACE_FILE


def _is_own_previous_output(target: Path) -> bool:
    """Treat *target* as a prior eawf packaging output if it carries the
    expected ``.agents/plugins/marketplace.json`` + ``plugins/eawf/.codex-plugin/``
    layout. Also accepts the legacy root-level ``marketplace.json`` so an
    operator can re-run the packager over an older tree without ``--force``.
    """
    has_marketplace = _marketplace_path(target).is_file() or (target / _MARKETPLACE_FILE).is_file()
    return has_marketplace and (target / "plugins" / _PLUGIN_NAME / _MANIFEST_DIR).is_dir()


def _check_target(target: Path, *, force: bool) -> None:
    """Refuse to write into a non-empty *target* that is not a prior eawf output."""
    if not target.exists():
        return
    if not target.is_dir():
        raise ValueError(f"target {target} exists and is not a directory")
    if not any(target.iterdir()):
        return
    if _is_own_previous_output(target):
        return
    if force:
        return
    raise ValueError(
        f"target {target} is not empty and not a previous eawf package; pass --force to overwrite"
    )


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def package_plugin(
    target: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    publish_source: PublishSource = PublishSource.LOCAL,
) -> PackageResult:
    """Render the Codex marketplace tree at *target*.

    Args:
        target: Output directory. Created if absent; rejected if
            non-empty and not a prior eawf package (use ``force``).
        force: Overwrite a non-empty target that is not a prior eawf
            output. Has no effect on managed-file drift checks (the
            renderer always rewrites the full tree).
        dry_run: When ``True``, returns the :class:`PackageResult`
            describing what would be written but writes nothing.
        publish_source: Marketplace ``source`` shape. ``LOCAL`` (default)
            keeps the relative-path dev source for ``marketplace add
            ./path``; ``GIT_SUBDIR`` emits the self-hosted published
            pointer at the canonical repo's ``plugins-dist`` branch.

    Raises:
        ValueError: when *target* is non-empty and not a previous eawf
            output and ``force`` is not set.
        IntegrityViolation: re-exported for caller convenience; the
            packager itself does not raise it.
    """
    target = Path(target).resolve()
    if not dry_run:
        _check_target(target, force=force)

    plugin_root = _plugin_root(target)

    skill_deltas: list[FileDelta] = []
    for spec in SKILL_REGISTRY:
        path = plugin_root / "skills" / spec.skill_name.lstrip("/") / "SKILL.md"
        payload = _render_skill(spec).encode("utf-8")
        action = _classify(path, payload)
        if not dry_run:
            _ensure_dir(path.parent)
            atomic_write_text(path, payload.decode("utf-8"))
        skill_deltas.append(FileDelta(path=path, action=action))

    hook_deltas: list[FileDelta] = []
    for hook_spec in HOOK_REGISTRY:
        path = plugin_root / "hooks" / f"{codex_hook_name(hook_spec.event_type)}.sh"
        payload = render_hook_sh(hook_spec.event_type).encode("utf-8")
        action = _classify(path, payload)
        if not dry_run:
            _ensure_dir(path.parent)
            atomic_write_text(path, payload.decode("utf-8"))
            os.chmod(path, _HOOK_FILE_MODE)
        hook_deltas.append(FileDelta(path=path, action=action))

    manifest_path = plugin_root / _MANIFEST_DIR / _MANIFEST_FILE
    manifest_payload = _render_manifest()
    manifest_action = _classify(manifest_path, manifest_payload)
    if not dry_run:
        _ensure_dir(manifest_path.parent)
        atomic_write_text(manifest_path, manifest_payload.decode("utf-8"))
    manifest_delta = FileDelta(path=manifest_path, action=manifest_action)

    marketplace_path = _marketplace_path(target)
    marketplace_payload = _render_marketplace(publish_source)
    marketplace_action = _classify(marketplace_path, marketplace_payload)
    if not dry_run:
        _ensure_dir(marketplace_path.parent)
        atomic_write_text(marketplace_path, marketplace_payload.decode("utf-8"))
        legacy_marketplace = target / _MARKETPLACE_FILE
        if legacy_marketplace.is_file():
            legacy_marketplace.unlink()
    marketplace_delta = FileDelta(path=marketplace_path, action=marketplace_action)

    logger.info(
        f"package_plugin runtime=codex target={target} "
        f"skills={len(skill_deltas)} hooks={len(hook_deltas)} "
        f"manifest={manifest_action} marketplace={marketplace_action} "
        f"publish_source={publish_source.value} dry_run={dry_run}"
    )
    return PackageResult(
        target=target,
        skills=skill_deltas,
        hooks=hook_deltas,
        manifest=manifest_delta,
        marketplace=marketplace_delta,
        dry_run=dry_run,
    )


__all__ = [
    "FileDelta",
    "IntegrityViolation",
    "PackageResult",
    "PublishSource",
    "package_plugin",
]
