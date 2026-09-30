"""``hooks_plugins`` statusline module — installed hook + plugin counts.

Counts the files under the workspace's ``.claude/hooks/`` directory and the
plugins Claude Code records as installed for this workspace, rendering
``hooks:<n> plugins:<m>``. No record of the tree states installed plugins, so
the plugin half reads the host's own record,
``~/.claude/plugins/installed_plugins.json``, counting the installs that reach
this workspace: user-scoped ones and those scoped to this project. When no
workspace resolves the segment renders ``hooks:n/a(no-state)``; when the host
record is absent or unreadable the hook count still shows and the plugins half
renders ``plugins:n/a(<reason>)``.

The count is *informational only*: it reports how many files sit on disk, not
whether any of them ran or exited cleanly. The module never reads a hook exit
code, so it must not paint the segment ``status="ok"`` (a health claim it
cannot back). The readable path therefore reports ``status="degraded"`` — the
count is shown, but the segment does not assert health it never measured.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from eawf.kernel.projection.truth import TruthKind
from eawf.runtime.runtimes.claude.statusline_modules._host import (
    HOST_PLUGIN_PRODUCER,
    HOST_PLUGIN_RECORD,
)
from eawf.runtime.runtimes.claude.statusline_modules._spine import NO_STATE
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
_HOOKS_PROVENANCE: Final = ".claude/hooks"
_HOOKS_SOURCE: Final = SegmentSource(producer="workspace-filesystem", provenance=_HOOKS_PROVENANCE)
_PLUGINS_SOURCE: Final = SegmentSource(
    producer=HOST_PLUGIN_PRODUCER,
    provenance=f"~/{HOST_PLUGIN_RECORD.as_posix()}#plugins",
    truth_kind=TruthKind.STORED,
)

#: The install scope that reaches every workspace of the user.
_USER_SCOPE: Final = "user"


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
    path = Path.home() / HOST_PLUGIN_RECORD
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


def _count_hooks(state_path: Path) -> int:
    """Return the number of files under ``<workspace>/.claude/hooks/``.

    ``.claude/`` is always relative to the workspace root, which is the
    parent of the resolved ``.ea/`` directory. When the hooks directory does
    not exist, the count is ``0``.
    """
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
        claim — because the module never inspects a hook exit code). A counted
        plugin half names the host's install record as the producer, with the
        hooks directory as a second provenance.
    """
    del claude_payload  # accepted for uniform signature
    if state_path is None:
        return unavailable_segment(_MODULE, _LABEL, NO_STATE, _HOOKS_SOURCE)
    hooks = _count_hooks(state_path)
    installed = _installed_plugins(state_path.parent.parent)
    if isinstance(installed, str):
        plugins = f"{UNAVAILABLE_MARK}({installed})"
        return sourced_segment(
            _MODULE, _LABEL, f"{hooks} plugins:{plugins}", _HOOKS_SOURCE, status="degraded"
        )
    segment = sourced_segment(
        _MODULE, _LABEL, f"{hooks} plugins:{installed}", _PLUGINS_SOURCE, status="degraded"
    )
    refs = (*segment.truth.provenance_refs, _HOOKS_PROVENANCE)
    return replace(segment, truth=segment.truth.model_copy(update={"provenance_refs": refs}))


__all__ = ["build"]
