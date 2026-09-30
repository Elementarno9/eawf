"""The epoch-2 reads the spine-backed statusline segments share.

A tree cut over to epoch 2 keeps its authority in the selected generation.
The ``scope``, ``budget``, ``mcp_health`` and ``memory`` segments read it
there: the first two through the Run bound to the host's session, the others
through the MCP registry rows and the memory ledger. Every way the spine can
fail to answer comes back as one reason token the segment renders as
``n/a(<reason>)``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.session.vendor_id import hash_vendor_session_id

logger = logging.getLogger(__name__)

#: The producer a segment read from the tree's selected epoch-2 generation names.
SPINE_PRODUCER: Final = "eawf.epoch2-generation"

#: The reason a segment names when no workspace, and so no tree, resolved.
NO_STATE: Final = "no-state"


def selected_generation(state_path: Path) -> Path | str:
    """Return the selected generation's document, or the reason there is none.

    Args:
        state_path: Resolved ``.ea/state.json`` path, whose directory is the
            tree root the authority is resolved for.

    Returns:
        The generation document path, or ``epoch1-<gap>`` when the tree
        answers in epoch 1.
    """
    authority = resolve_authority(state_path.parent)
    target, generation = authority.target, authority.generation_id
    if target is None or generation is None:
        # An epoch-1 answer always names its gap, such as marker_absent.
        return f"epoch1-{str(authority.gap).replace('_', '-')}"
    return target.generation_path(generation) / GENERATION_DOCUMENT


def _session_rows(document: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    """Return the raw Run rows whose vendor session is ``session_id``."""
    digest = hash_vendor_session_id(session_id)
    rows = document.get(Epoch2Collection.RUN.value)
    if not isinstance(rows, dict):
        return []
    return [
        row
        for row in rows.values()
        if isinstance(row, dict)
        and isinstance(row.get("vendor_session"), dict)
        and row["vendor_session"].get("session_digest") == digest
    ]


def session_run(claude_payload: dict[str, Any], document_path: Path) -> Run | str:
    """Return the Run bound to the host's session, or the reason there is none.

    Args:
        claude_payload: Decoded Claude stdin JSON, read for ``session_id``.
        document_path: The selected generation's document.

    Returns:
        The most recently updated Run whose vendor session is the host's, or
        one of ``no-session``, ``generation-unreadable``,
        ``no-run-for-session`` and ``run-invalid``.
    """
    session_id = claude_payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return "no-session"
    try:
        document = read_document(document_path)
    except (OSError, ValueError) as exc:
        logger.debug(f"session_run generation-unreadable error={exc}")
        return "generation-unreadable"
    rows = _session_rows(document, session_id)
    if not rows:
        return "no-run-for-session"
    try:
        runs = [Run.model_validate(row) for row in rows]
    except ValidationError as exc:
        logger.debug(f"session_run run-invalid error={exc}")
        return "run-invalid"
    return max(runs, key=lambda candidate: candidate.updated_at)


__all__ = ["NO_STATE", "SPINE_PRODUCER", "selected_generation", "session_run"]
