"""``mcp_health`` statusline module — MCP server up/down summary.

Inspects ``state.mcp_servers`` (Pydantic-validated server records) on disk
and emits ``mcp:<up>/<total>``. Missing state file or empty mcp_servers
collapses to ``mcp:?`` with ``status="missing"``.

Status meaning:

- ``ok`` — every server is ``up``.
- ``warn`` — some servers are not ``up`` (config-mode, etc.).
- ``degraded`` — every server is ``down``.
- ``missing`` — state unavailable or no servers declared.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import orjson

from eawf.render.statusline import StatuslineSegment

logger = logging.getLogger(__name__)


def _count_servers(payload: dict[str, Any]) -> tuple[int, int]:
    """Return ``(up_count, total_count)`` for ``state.mcp_servers``.

    "Up" is defined as ``status == "up"``. Other statuses (``down``,
    ``config-only``, etc.) count toward ``total`` but not ``up``. Missing
    or non-mapping payload returns ``(0, 0)``.
    """
    servers = payload.get("mcp_servers")
    if not isinstance(servers, dict) or not servers:
        return (0, 0)
    total = len(servers)
    up = 0
    for srv in servers.values():
        if isinstance(srv, dict) and srv.get("status") == "up":
            up += 1
    return (up, total)


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``mcp:<up>/<total>`` (or ``mcp:?``) segment.

    Args:
        claude_payload: Unused — Claude does not propagate MCP health on
            stdin. Kept for the uniform module signature.
        state_path: Resolved ``.ea/state.json`` path or ``None``.

    Returns:
        A :class:`StatuslineSegment` with ``module="mcp_health"`` and the
        derived status (see module docstring).
    """
    del claude_payload  # accepted for uniform signature
    if state_path is None or not state_path.exists():
        return StatuslineSegment(module="mcp_health", text="mcp:?", status="missing")
    try:
        raw = state_path.read_bytes()
        payload = orjson.loads(raw)
    except (OSError, orjson.JSONDecodeError) as exc:
        logger.debug(f"statusline_modules.mcp_health: read/decode failed: {exc}")
        return StatuslineSegment(module="mcp_health", text="mcp:?", status="missing")
    if not isinstance(payload, dict):
        return StatuslineSegment(module="mcp_health", text="mcp:?", status="missing")
    up, total = _count_servers(payload)
    if total == 0:
        return StatuslineSegment(module="mcp_health", text="mcp:?", status="missing")
    text = f"mcp:{up}/{total}"
    if up == total:
        return StatuslineSegment(module="mcp_health", text=text, status="ok")
    if up == 0:
        return StatuslineSegment(module="mcp_health", text=text, status="degraded")
    return StatuslineSegment(module="mcp_health", text=text, status="warn")


__all__ = ["build"]
