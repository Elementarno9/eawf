"""``mcp_health`` statusline module — the MCP registry of the selected generation.

A registered MCP server is a ``capability`` row of the tree's selected
generation; a removed one stays under its key and reads as absent. The segment
counts the servers that stand and how many of them the registry records as
installed into a runtime, rendering ``mcp:<installed>/<registered>``. The
status is the recorded one: nothing here probes a server, so the segment
reports what the registry states rather than a liveness it never measured.

Status meaning:

- ``ok`` — every registered server is recorded as installed.
- ``warn`` — some registered servers are not installed yet.
- ``degraded`` — none is installed, or one is recorded as degraded.
- ``missing`` — no tree, a tree still in epoch 1, an unreadable registry, or
  no registered server; rendered as ``mcp:n/a(<reason>)``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from eawf.kernel.projection.truth import TruthKind
from eawf.kernel.state.enums import McpStatus
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.mcp.book import read_servers
from eawf.runtime.runtimes.claude.statusline_modules._spine import (
    NO_STATE,
    SPINE_PRODUCER,
    selected_generation,
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
_PROVENANCE: Final = f"generation#{Epoch2Collection.CAPABILITY.value}"
_SOURCE: Final = SegmentSource(
    producer=SPINE_PRODUCER, provenance=_PROVENANCE, truth_kind=TruthKind.STORED
)


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``mcp:<installed>/<registered>`` segment, or why it has none.

    Args:
        claude_payload: Unused — Claude does not propagate MCP health on
            stdin. Kept for the uniform module signature.
        state_path: Resolved ``.ea/state.json`` path or ``None``.

    Returns:
        A :class:`StatuslineSegment` with ``module="mcp_health"``, stated by the
        registry at the newest revision among its standing rows.
    """
    del claude_payload  # accepted for uniform signature
    if state_path is None:
        return unavailable_segment(_MODULE, _LABEL, NO_STATE, _SOURCE)
    document_path = selected_generation(state_path)
    if isinstance(document_path, str):
        return unavailable_segment(_MODULE, _LABEL, document_path, _SOURCE)
    try:
        standing = read_servers(read_document(document_path))
    except (OSError, ValueError) as exc:
        # McpRowError and pydantic's ValidationError are both ValueErrors.
        logger.debug(f"build mcp-registry-unreadable error={exc}")
        return unavailable_segment(_MODULE, _LABEL, "mcp-registry-unreadable", _SOURCE)
    rows = [row for row in standing.values() if row.server is not None]
    if not rows:
        return unavailable_segment(_MODULE, _LABEL, "no-mcp-servers", _SOURCE)
    statuses = [row.server.status for row in rows if row.server is not None]
    installed = statuses.count(McpStatus.INSTALLED)
    source = SegmentSource(
        producer=SPINE_PRODUCER,
        provenance=_PROVENANCE,
        revision=max(row.revision for row in rows),
        truth_kind=TruthKind.STORED,
    )
    value = f"{installed}/{len(rows)}"
    if installed == 0 or McpStatus.DEGRADED in statuses:
        return sourced_segment(_MODULE, _LABEL, value, source, status="degraded")
    if installed < len(rows):
        return sourced_segment(_MODULE, _LABEL, value, source, status="warn")
    return sourced_segment(_MODULE, _LABEL, value, source)


__all__ = ["build"]
