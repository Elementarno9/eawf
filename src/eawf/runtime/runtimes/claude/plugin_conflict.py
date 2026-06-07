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

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


_CC_PLUGINS_DIR: str = ".claude/plugins"


@dataclass(frozen=True)
class CCPluginConflict:
    """A pre-existing CC marketplace install that would clash with project-local mode."""

    plugin_dir: Path


def _user_plugin_root(home: Path | None = None) -> Path:
    """Return ``~/.claude/plugins/`` (overridable for tests)."""
    base = home if home is not None else Path.home()
    return base / _CC_PLUGINS_DIR


def detect_marketplace_install(*, home: Path | None = None) -> CCPluginConflict | None:
    """Return ``CCPluginConflict`` if an ``eawf`` plugin tree exists under CC's user-plugin dir.

    Args:
        home: Override for ``$HOME`` (tests pass ``tmp_path`` here). Defaults
            to :func:`pathlib.Path.home`.

    Returns:
        :class:`CCPluginConflict` describing the first matching entry under
        ``~/.claude/plugins/``, or ``None`` when the directory is absent /
        empty / contains no ``eawf``-named subdirectory.
    """
    root = _user_plugin_root(home=home)
    if not root.is_dir():
        return None
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if "eawf" in entry.name.lower():
            logger.info(f"detect_marketplace_install hit plugin_dir={entry}")
            return CCPluginConflict(plugin_dir=entry)
    return None


__all__ = ["CCPluginConflict", "detect_marketplace_install"]
