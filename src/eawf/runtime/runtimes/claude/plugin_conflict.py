"""Detect competing CC marketplace installs that would clash with a project-local render.

``eawf plugin install claude`` writes ``.claude/{skills,agents,hooks}/`` under
the workspace root — project-local mode. Operators can *also* install the
packaged plugin via ``/plugin marketplace add ./build/eawf-plugin`` +
``/plugin install eawf@eawf``; that copy lives under
``~/.claude/plugins/`` and is global to the user.

Running both modes simultaneously makes Claude Code see every skill / agent /
hook twice. CC dedups by name but the active body is undefined. This module
surfaces the conflict so ``install claude`` can prompt the operator to pick
one path (or pass ``--force`` to acknowledge).

The detector is best-effort:

- False positives (warning fires on an unrelated plugin whose name happens to
  contain ``eawf``) are cheap — the operator overrides via prompt or
  ``--force``.
- False negatives (CC re-homes plugins to a path we do not probe) are
  tolerable — ``plugin doctor`` later catches duplicate-render artifacts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


_CC_PLUGINS_DIR: str = ".claude/plugins"

#: Claude Code's record of what is installed, and the authoritative answer.
#: Probing it beats walking directories: the layout under ``plugins/`` is
#: Claude Code's to change, and it did — the tree nests one level deeper than
#: the original directory scan assumed.
_INSTALLED_MANIFEST: str = "installed_plugins.json"

#: Directories under ``plugins/`` that hold per-plugin trees. Only used when
#: the manifest is unreadable, so a hand-managed install is still seen.
_NESTED_ROOTS: tuple[str, ...] = ("cache", "marketplaces")


@dataclass(frozen=True)
class CCPluginConflict:
    """A pre-existing CC marketplace install that would clash with project-local mode."""

    plugin_dir: Path


def _user_plugin_root(home: Path | None = None) -> Path:
    """Return ``~/.claude/plugins/`` (overridable for tests)."""
    base = home if home is not None else Path.home()
    return base / _CC_PLUGINS_DIR


def _from_manifest(root: Path) -> CCPluginConflict | None:
    """Return a conflict named by ``installed_plugins.json``, or ``None``.

    The manifest maps ``"<plugin>@<marketplace>"`` to a list of install
    records carrying ``installPath``. A key naming eawf is a marketplace
    install regardless of where on disk Claude Code chose to put it.
    """
    manifest = root / _INSTALLED_MANIFEST
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    plugins = payload.get("plugins")
    if not isinstance(plugins, dict):
        return None
    for key, records in sorted(plugins.items()):
        if "eawf" not in key.lower():
            continue
        install_path = None
        if isinstance(records, list):
            install_path = next(
                (
                    record.get("installPath")
                    for record in records
                    if isinstance(record, dict) and record.get("installPath")
                ),
                None,
            )
        plugin_dir = Path(install_path) if install_path else root
        logger.info(f"detect_marketplace_install hit source=manifest key={key!r}")
        return CCPluginConflict(plugin_dir=plugin_dir)
    return None


def _from_directories(root: Path) -> CCPluginConflict | None:
    """Return a conflict found by walking ``plugins/`` and its per-plugin roots."""
    candidates = [root, *(root / name for name in _NESTED_ROOTS)]
    for parent in candidates:
        if not parent.is_dir():
            continue
        for entry in sorted(parent.iterdir()):
            if entry.is_dir() and "eawf" in entry.name.lower():
                logger.info(f"detect_marketplace_install hit source=dir plugin_dir={entry}")
                return CCPluginConflict(plugin_dir=entry)
    return None


def detect_marketplace_install(*, home: Path | None = None) -> CCPluginConflict | None:
    """Return ``CCPluginConflict`` when an ``eawf`` plugin is installed for the user.

    Reads ``installed_plugins.json`` first, then falls back to walking
    ``plugins/`` and its ``cache`` / ``marketplaces`` children.

    The fallback exists because the original detector walked only the
    immediate children of ``plugins/``, where the real children are
    ``cache`` / ``data`` / ``marketplaces`` / ``npm-cache`` — none named for a
    plugin. It therefore returned ``None`` against a live install and the
    conflict gate never fired.

    Args:
        home: Override for ``$HOME`` (tests pass ``tmp_path`` here). Defaults
            to :func:`pathlib.Path.home`.

    Returns:
        The conflict, or ``None`` when no eawf plugin is installed for the user.
    """
    root = _user_plugin_root(home=home)
    if not root.is_dir():
        return None
    return _from_manifest(root) or _from_directories(root)


__all__ = ["CCPluginConflict", "detect_marketplace_install"]
