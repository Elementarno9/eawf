"""``mcp_health`` statusline module — MCP server up/down summary.

On an epoch-1 tree, inspects ``state.mcp_servers`` in the document and
emits ``mcp:<up>/<total>``.

On an epoch-2 tree the frozen document's statuses are a past state, and no
epoch-2 producer measures whether a server is up. The current fact is the
set of servers the Claude runtime config registers -- the workspace's
``.mcp.json``, which ``eawf mcp install`` writes and Claude Code loads --
so the segment emits ``mcp:<n> registered`` and makes no health claim.

An unreadable source, or no server, renders ``mcp:n/a(<reason>)`` with
``status="missing"``.

Status meaning:

- ``ok`` — every server is ``up``.
- ``warn`` — some servers are not ``up`` (config-mode, etc.).
- ``degraded`` — every server is ``down``, or the servers are counted from
  the runtime config and their health was never measured.
- ``missing`` — no source, or no servers declared.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from eawf.runtime.mcp.installer import list_runtime_entries
from eawf.runtime.runtimes.claude.statusline_modules._document import (
    DocumentGap,
    document_source,
    read_legacy_document,
)
from eawf.surfaces.render.statusline import (
    SegmentSource,
    StatuslineSegment,
    sourced_segment,
    unavailable_segment,
)

logger = logging.getLogger(__name__)

_MODULE = "mcp_health"
_LABEL = "mcp"
_SOURCE = document_source("mcp_servers")

#: The producer an epoch-2 count names: the Claude runtime's MCP config.
RUNTIME_CONFIG_PRODUCER: Final = "claude-code.mcp-config"
_RUNTIME = "claude"
_RUNTIME_SOURCE = SegmentSource(producer=RUNTIME_CONFIG_PRODUCER, provenance=".mcp.json#mcpServers")


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


def _registered_servers(workspace: Path) -> StatuslineSegment:
    """Return the count of servers the Claude runtime config registers."""
    try:
        entries = list_runtime_entries(runtime=_RUNTIME, target_dir=workspace)
    except (OSError, ValueError) as exc:
        logger.debug(f"_registered_servers mcp-config-unreadable error={exc}")
        return unavailable_segment(_MODULE, _LABEL, "mcp-config-unreadable", _RUNTIME_SOURCE)
    if not entries:
        return unavailable_segment(_MODULE, _LABEL, "no-mcp-servers", _RUNTIME_SOURCE)
    # Registered is not up: nothing measured health, so the segment claims none.
    return sourced_segment(
        _MODULE, _LABEL, f"{len(entries)} registered", _RUNTIME_SOURCE, status="degraded"
    )


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``mcp:<up>/<total>`` or ``mcp:<n> registered`` segment, or why not.

    Args:
        claude_payload: Unused — Claude does not propagate MCP health on
            stdin. Kept for the uniform module signature.
        state_path: Resolved ``.ea/state.json`` path or ``None``.

    Returns:
        A :class:`StatuslineSegment` with ``module="mcp_health"`` and the
        derived status (see module docstring).
    """
    del claude_payload  # accepted for uniform signature
    if state_path is None:
        return unavailable_segment(_MODULE, _LABEL, DocumentGap.NO_STATE.value, _SOURCE)
    payload = read_legacy_document(state_path)
    if payload is DocumentGap.NO_EPOCH2_SOURCE:
        return _registered_servers(state_path.parent.parent)
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


__all__ = ["RUNTIME_CONFIG_PRODUCER", "build"]
