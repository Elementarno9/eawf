"""The epoch-1 ``state.json`` read the document-backed statusline segments share.

Four segments (``budget``, ``mcp_health``, ``hooks_plugins``, ``memory``) read
fields the epoch-1 document carries while the tree is in epoch 1, where the
document is the live authority. On a tree cut over to epoch 2 that document
is frozen at the cutover, so a value read from it is a past state rendered as
a present one; the read answers ``no-epoch2-source`` and each segment turns
to the producer that is current in epoch 2 instead.

The file is decoded with :mod:`orjson` rather than validated, because the
statusline must stay fast (target <100 ms cold) and a malformed payload
degrades one segment rather than the line.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import orjson

from eawf.kernel.projection.truth import TruthKind
from eawf.kernel.state.io import epoch_marker_present
from eawf.surfaces.render.statusline import SegmentSource

logger = logging.getLogger(__name__)


#: The producer every document-backed segment names.
DOCUMENT_PRODUCER: Final = "eawf.state-document"


class DocumentGap(StrEnum):
    """Why the epoch-1 document cannot source a segment."""

    NO_STATE = "no-state"
    NO_EPOCH2_SOURCE = "no-epoch2-source"
    STATE_UNREADABLE = "state-unreadable"


def document_source(field: str) -> SegmentSource:
    """Return the source a value stored at ``field`` of the document is read from.

    Args:
        field: The document field the value is stored under.

    Returns:
        A stored, exact source naming the repo-relative document field.
    """
    return SegmentSource(
        producer=DOCUMENT_PRODUCER,
        provenance=f".ea/state.json#{field}",
        truth_kind=TruthKind.STORED,
    )


def read_legacy_document(state_path: Path | None) -> dict[str, Any] | DocumentGap:
    """Return the epoch-1 document at ``state_path``, or why it cannot be read.

    Args:
        state_path: The resolved ``.ea/state.json`` path, or ``None``.

    Returns:
        The decoded document while the tree is in epoch 1; otherwise the gap,
        which is ``no-epoch2-source`` on any tree carrying the epoch marker.
    """
    if state_path is None:
        return DocumentGap.NO_STATE
    # Checked before existence: a tree provisioned in epoch 2 has no document at all.
    if epoch_marker_present(state_path.parent):
        return DocumentGap.NO_EPOCH2_SOURCE
    if not state_path.exists():
        return DocumentGap.NO_STATE
    try:
        payload = orjson.loads(state_path.read_bytes())
    except (OSError, orjson.JSONDecodeError) as exc:
        logger.debug(f"read_legacy_document state-read-decode-failed error={exc}")
        return DocumentGap.STATE_UNREADABLE
    if not isinstance(payload, dict):
        return DocumentGap.STATE_UNREADABLE
    return payload


__all__ = ["DOCUMENT_PRODUCER", "DocumentGap", "document_source", "read_legacy_document"]
