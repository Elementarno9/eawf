"""``mcp_health`` statusline module — MCP server up/down summary.

Inspects ``state.mcp_servers`` in the epoch-1 document and emits
``mcp:<up>/<total>``. An unreadable or frozen document, or no declared
server, renders ``mcp:n/a(<reason>)`` with ``status="missing"``.

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

from eawf.runtime.runtimes.claude.statusline_modules._document import (
    DocumentGap,
    document_source,
    read_legacy_document,
)
from eawf.surfaces.render.statusline import (
    StatuslineSegment,
    sourced_segment,
    unavailable_segment,
)

logger = logging.getLogger(__name__)

_MODULE = "mcp_health"
_LABEL = "mcp"
_SOURCE = document_source("mcp_servers")


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
    """Return the ``mcp:<up>/<total>`` (or ``mcp:n/a(<reason>)``) segment.

    Args:
        claude_payload: Unused — Claude does not propagate MCP health on
            stdin. Kept for the uniform module signature.
        state_path: Resolved ``.ea/state.json`` path or ``None``.

    Returns:
        A :class:`StatuslineSegment` with ``module="mcp_health"`` and the
        derived status (see module docstring).
    """
    del claude_payload  # accepted for uniform signature
    payload = read_legacy_document(state_path)
    if isinstance(payload, DocumentGap):
        return unavailable_segment(_MODULE, _LABEL, payload.value, _SOURCE)
    up, total = _count_servers(payload)
    if total == 0:
        return unavailable_segment(_MODULE, _LABEL, "no-mcp-servers", _SOURCE)
    value = f"{up}/{total}"
    if up == total:
        return sourced_segment(_MODULE, _LABEL, value, _SOURCE)
    if up == 0:
        return sourced_segment(_MODULE, _LABEL, value, _SOURCE, status="degraded")
    return sourced_segment(_MODULE, _LABEL, value, _SOURCE, status="warn")


__all__ = ["build"]
