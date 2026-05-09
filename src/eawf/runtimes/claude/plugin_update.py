"""Re-render the Claude Code plugin tree, aborting on hand-edits.

``eawf plugin update claude`` shares 95% of its behaviour with
:func:`eawf.runtimes.claude.plugin_install.install_plugin`: enumerate
the registry, render every file, write the bytes back, refresh the
manifest. The only difference is that ``update`` never accepts
``force=True`` — a hand-edit must be detected and surfaced via
:class:`~eawf.runtimes.claude.plugin_install.IntegrityViolation`. The
caller is expected to either back out the hand-edit, copy it into a
managed-region body change, or re-run ``install --force`` to clobber.

Public API::

    UpdateResult                                   # alias of InstallResult
    update_plugin(target_dir) -> UpdateResult
"""

from __future__ import annotations

import logging
from pathlib import Path

from eawf.runtimes.claude.plugin_install import InstallResult, install_plugin

logger = logging.getLogger(__name__)


# Update is a thin re-rendering wrapper — it shares the dataclass with
# install for now (fields match exactly). A future divergence (e.g.
# update returns the manifest delta) can promote this to its own type.
UpdateResult = InstallResult


def update_plugin(target_dir: Path, *, timestamp: str | None = None) -> UpdateResult:
    """Re-render the Claude plugin tree under *target_dir*.

    Args:
        target_dir: Workspace root that hosts ``.claude/``.
        timestamp: ISO 8601 UTC timestamp baked into the manifest /
            ``__eawf_managed`` namespace. Defaults to the same epoch
            as :func:`install_plugin` so re-runs stay byte-stable.

    Returns:
        :class:`UpdateResult` summarising the re-rendered tree. Each
        :class:`FileDelta` reports whether the file was created (a new
        managed file appeared), updated (registry changed), or
        unchanged (bytes already match).

    Raises:
        IntegrityViolation: A managed file on disk has been
            hand-edited; the update would clobber the user's change.
    """
    logger.info(f"update_plugin target={target_dir}")
    return install_plugin(
        Path(target_dir),
        force=False,
        dry_run=False,
        timestamp=timestamp,
    )


__all__ = [
    "UpdateResult",
    "update_plugin",
]
