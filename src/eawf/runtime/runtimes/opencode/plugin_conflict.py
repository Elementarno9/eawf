"""Detect competing user-scope OpenCode plugin installs.

``eawf plugin install opencode`` writes ``.opencode/plugins/eawf.js``
under the workspace root (project scope) or
``$OPENCODE_CONFIG_DIR/plugins/eawf.js`` (user scope, defaults to
``~/.config/opencode/plugins/eawf.js``).

Running a project install while a user-scope install already exists
makes OpenCode auto-load two ``eawf`` plugins; the active body is
undefined. This module surfaces the conflict so the CLI can prompt
(or override via ``--force``). Mirrors
:mod:`eawf.runtime.runtimes.claude.plugin_conflict`.

The detector is best-effort: false positives are cheap; false
negatives are tolerable.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


_PLUGIN_FILENAME: str = "eawf.js"
_OPENCODE_CONFIG_DIR_ENV: str = "OPENCODE_CONFIG_DIR"
_DEFAULT_XDG_SUBDIR: str = ".config/opencode"


@dataclass(frozen=True)
class OpenCodeUserPluginConflict:
    """A pre-existing user-scope OpenCode install of the eawf plugin."""

    plugin_file: Path


def _user_plugin_root(
    *,
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> Path:
    """Return ``$OPENCODE_CONFIG_DIR/plugins/`` or its XDG default."""
    if opencode_config_dir is not None:
        return Path(opencode_config_dir) / "plugins"
    env_value = os.environ.get(_OPENCODE_CONFIG_DIR_ENV)
    if env_value:
        return Path(env_value) / "plugins"
    base = home if home is not None else Path.home()
    return base / _DEFAULT_XDG_SUBDIR / "plugins"


def detect_user_install(
    *,
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> OpenCodeUserPluginConflict | None:
    """Return :class:`OpenCodeUserPluginConflict` if a user-scope eawf install exists.

    Args:
        home: Override for ``$HOME`` (tests pass ``tmp_path``).
            Defaults to :func:`pathlib.Path.home`.
        opencode_config_dir: Override for ``$OPENCODE_CONFIG_DIR``.

    Returns:
        The conflict record when ``<root>/eawf.js`` exists,
        else ``None``.
    """
    plugin_file = _user_plugin_root(home=home, opencode_config_dir=opencode_config_dir) / (
        _PLUGIN_FILENAME
    )
    if not plugin_file.is_file():
        return None
    logger.info(f"detect_user_install hit runtime=opencode plugin_file={plugin_file}")
    return OpenCodeUserPluginConflict(plugin_file=plugin_file)


__all__ = ["OpenCodeUserPluginConflict", "detect_user_install"]
