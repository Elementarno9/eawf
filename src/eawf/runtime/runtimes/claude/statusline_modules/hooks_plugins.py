"""``hooks_plugins`` statusline module — installed hook + plugin counts.

Counts entries under ``state.plugins`` and inspects the local
``.claude/hooks/`` directory (when reachable from the workspace root).
Renders ``hooks:<n> plugins:<m>``. When no workspace resolves the segment
renders ``hooks:n/a(no-state)``; when the plugins source is unreadable the
hook count still shows and the plugins half renders ``plugins:n/a(<reason>)``.

The count is *informational only*: it reports how many ``.sh`` files sit
on disk, not whether any of them ran or exited cleanly. The module never
reads a hook exit code, so it must not paint the segment ``status="ok"``
(a health claim it cannot back). The readable path therefore reports
``status="degraded"`` — the count is shown, but the segment does not
assert health it never measured.

The module deliberately reads the on-disk ``.claude/hooks/`` directory
because Claude-installed hooks are sidecar shell scripts not tracked in
``state.plugins``. On an epoch-1 tree plugins are read from ``state.json``.
On an epoch-2 tree that document is frozen and no epoch-2 record succeeds
it, so the plugins half reads Claude Code's own record of what is installed,
``~/.claude/plugins/installed_plugins.json``, counting the installs that
reach this workspace: user-scoped ones and those scoped to this project.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Final

from eawf.runtime.runtimes.claude.statusline_modules._document import (
    DocumentGap,
    document_source,
    read_legacy_document,
)
from eawf.surfaces.render.statusline import (
    UNAVAILABLE_MARK,
    SegmentSource,
    StatuslineSegment,
    sourced_segment,
    unavailable_segment,
)

logger = logging.getLogger(__name__)

_MODULE = "hooks_plugins"
_LABEL = "hooks"
_HOOKS_SOURCE = SegmentSource(producer="workspace-filesystem", provenance=".claude/hooks")
_PLUGINS_SOURCE = document_source("plugins")

#: Where Claude Code records installed plugins, relative to the home directory.
_INSTALLED_PLUGINS: Final = Path(".claude") / "plugins" / "installed_plugins.json"

#: The install scope that reaches every workspace of the user.
_USER_SCOPE: Final = "user"


def _count_plugins(payload: dict[str, Any]) -> int:
    """Return the number of entries in ``state.plugins`` (0 if absent)."""
    plugins = payload.get("plugins")
    if isinstance(plugins, dict):
        return len(plugins)
    return 0


def _reaches(record: object, workspace: Path) -> bool:
    """Return whether one install *record* is active for *workspace*."""
    if not isinstance(record, dict):
        return False
    if record.get("scope") == _USER_SCOPE:
        return True
    project = record.get("projectPath")
    return isinstance(project, str) and Path(project) == workspace


def _installed_plugins(workspace: Path) -> int | str:
    """Return how many installed plugins reach *workspace*, or why none can be counted."""
    path = Path.home() / _INSTALLED_PLUGINS
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "no-plugin-record"
    except (OSError, ValueError) as exc:
        logger.debug(f"_installed_plugins plugin-record-unreadable error={exc}")
        return "plugin-record-unreadable"
    plugins = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(plugins, dict):
        return "plugin-record-unreadable"
    return sum(
        1
        for records in plugins.values()
        if isinstance(records, list) and any(_reaches(record, workspace) for record in records)
    )


def _count_hooks(state_path: Path | None) -> int:
    """Return the number of ``.sh`` files under ``<workspace>/.claude/hooks/``.

    ``.claude/`` is always relative to the workspace root, which is the
    parent of the resolved ``.ea/`` directory. When ``state_path`` is
    ``None`` or the hooks directory does not exist, the count is ``0``.
    """
    if state_path is None:
        return 0
    ea_dir = state_path.parent
    if ea_dir.name != ".ea":
        return 0
    hooks_dir = ea_dir.parent / ".claude" / "hooks"
    if not hooks_dir.is_dir():
        return 0
    try:
        return sum(1 for entry in hooks_dir.iterdir() if entry.is_file())
    except OSError as exc:
        logger.debug(f"_count_hooks hooks-dir-scan-failed error={exc}")
        return 0


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``hooks:<n> plugins:<m>`` segment.

    Args:
        claude_payload: Unused — kept for the uniform module signature.
        state_path: Resolved ``.ea/state.json`` path or ``None``.

    Returns:
        A :class:`StatuslineSegment` with ``module="hooks_plugins"``.
        ``status="missing"`` when no workspace resolves; ``status="degraded"``
        for any other case (the counts are informational — never a health
        claim — because the module never inspects a hook exit code).
    """
    del claude_payload  # accepted for uniform signature
    if state_path is None:
        return unavailable_segment(_MODULE, _LABEL, DocumentGap.NO_STATE.value, _HOOKS_SOURCE)
    hooks = _count_hooks(state_path)
    payload = read_legacy_document(state_path)
    if payload is DocumentGap.NO_EPOCH2_SOURCE:
        installed = _installed_plugins(state_path.parent.parent)
        plugins = installed if isinstance(installed, int) else f"{UNAVAILABLE_MARK}({installed})"
    elif isinstance(payload, DocumentGap):
        plugins = f"{UNAVAILABLE_MARK}({payload.value})"
    else:
        plugins = _count_plugins(payload)
    # Informational only: a raw file count is not a health signal (no exit
    # code is read), so the segment stays "degraded" rather than claiming ok.
    return sourced_segment(
        _MODULE, _LABEL, f"{hooks} plugins:{plugins}", _HOOKS_SOURCE, status="degraded"
    )


__all__ = ["build"]
