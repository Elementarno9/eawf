"""Detect competing user-scope Codex plugin installs that would clash with project mode.

``eawf plugin install codex`` writes ``.codex/plugins/eawf/`` under the
workspace root (project scope) or ``~/.codex/plugins/eawf/`` (user
scope). Running a project install while a user-scope install already
exists makes Codex see two ``eawf`` plugins; the active body is
undefined.

This module surfaces the conflict so the CLI can prompt the operator
(or be silenced via ``--force``). Mirrors
:mod:`eawf.runtimes.claude.plugin_conflict`.

The detector is best-effort: false positives are cheap (operator
overrides via prompt or ``--force``); false negatives are tolerable
(``plugin doctor`` later catches duplicate-render artifacts).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


_CODEX_USER_PLUGINS_DIR: str = ".codex/plugins"
_PLUGIN_NAME: str = "eawf"


@dataclass(frozen=True)
class CodexUserPluginConflict:
    """A pre-existing user-scope Codex install of the eawf plugin."""

    plugin_dir: Path


def _user_plugin_root(home: Path | None = None) -> Path:
    """Return ``~/.codex/plugins/`` (overridable for tests)."""
    base = home if home is not None else Path.home()
    return base / _CODEX_USER_PLUGINS_DIR


def detect_user_install(*, home: Path | None = None) -> CodexUserPluginConflict | None:
    """Return :class:`CodexUserPluginConflict` if a user-scope eawf install exists.

    Args:
        home: Override for ``$HOME`` (tests pass ``tmp_path`` here).
            Defaults to :func:`pathlib.Path.home`.

    Returns:
        The conflict record when ``~/.codex/plugins/eawf/`` exists,
        else ``None``.
    """
    plugin_dir = _user_plugin_root(home=home) / _PLUGIN_NAME
    if not plugin_dir.is_dir():
        return None
    logger.info(f"detect_user_install hit runtime=codex plugin_dir={plugin_dir}")
    return CodexUserPluginConflict(plugin_dir=plugin_dir)


__all__ = ["CodexUserPluginConflict", "detect_user_install"]
