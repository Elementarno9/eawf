"""``hooks_plugins`` statusline module — installed hook + plugin counts.

Counts entries under ``state.plugins`` and inspects the local
``.claude/hooks/`` directory (when reachable from the workspace root).
Renders ``hooks:<n> plugins:<m>``. When the state file is missing or
unreadable, the segment collapses to ``hooks:- plugins:-`` with
``status="missing"``.

The count is *informational only*: it reports how many ``.sh`` files sit
on disk, not whether any of them ran or exited cleanly. The module never
reads a hook exit code, so it must not paint the segment ``status="ok"``
(a health claim it cannot back). The readable path therefore reports
``status="degraded"`` — the count is shown, but the segment does not
assert health it never measured.

The module deliberately reads the on-disk ``.claude/hooks/`` directory
because Claude-installed hooks are sidecar shell scripts not tracked in
``state.plugins``. Plugins are tracked centrally so we read those from
``state.json`` directly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import orjson

from eawf.surfaces.render.statusline import StatuslineSegment

logger = logging.getLogger(__name__)


def _count_plugins(payload: dict[str, Any]) -> int:
    """Return the number of entries in ``state.plugins`` (0 if absent)."""
    plugins = payload.get("plugins")
    if isinstance(plugins, dict):
        return len(plugins)
    return 0


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
        ``status="missing"`` when state is unreadable; ``status="degraded"``
        for any other case (the counts are informational — never a health
        claim — because the module never inspects a hook exit code).
    """
    del claude_payload  # accepted for uniform signature
    if state_path is None or not state_path.exists():
        return StatuslineSegment(
            module="hooks_plugins",
            text="hooks:- plugins:-",
            status="missing",
        )
    try:
        raw = state_path.read_bytes()
        payload = orjson.loads(raw)
    except (OSError, orjson.JSONDecodeError) as exc:
        logger.debug(f"build hooks-read-decode-failed error={exc}")
        return StatuslineSegment(
            module="hooks_plugins",
            text="hooks:- plugins:-",
            status="missing",
        )
    if not isinstance(payload, dict):
        return StatuslineSegment(
            module="hooks_plugins",
            text="hooks:- plugins:-",
            status="missing",
        )
    plugins = _count_plugins(payload)
    hooks = _count_hooks(state_path)
    # Informational only: a raw file count is not a health signal (no exit
    # code is read), so the segment stays "degraded" rather than claiming ok.
    return StatuslineSegment(
        module="hooks_plugins",
        text=f"hooks:{hooks} plugins:{plugins}",
        status="degraded",
    )


__all__ = ["build"]
