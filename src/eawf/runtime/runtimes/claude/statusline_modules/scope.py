"""``scope`` statusline module — the scope this session's Run executes against.

The scope resolves through the epoch-2 delivery spine: the host's session id
is hashed the way a Run's ``vendor_session`` stores it, the Run bound to that
session is read from the tree's selected generation, and the segment renders
the key of the record the Run is scoped to (a Task, a Batch, a Milestone...)
as ``scope:<key>``. The epoch-1 document's wave pointers are never read.

Every way the spine can fail to answer renders ``scope:n/a(<reason>)``, where
the reason names what is missing so the operator knows what would fix it:
no workspace, a tree still in epoch 1, no session id from the host, an
unreadable generation document, no Run bound to this session, or a Run row
that does not validate.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.projection.truth import TruthKind
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.surfaces.render.statusline import (
    SegmentSource,
    StatuslineSegment,
    sourced_segment,
    unavailable_segment,
)

logger = logging.getLogger(__name__)

#: The producer the scope segment names: the tree's selected epoch-2 generation.
SPINE_PRODUCER: Final = "eawf.epoch2-generation"

_MODULE = "scope"
_SPINE_SOURCE = SegmentSource(
    producer=SPINE_PRODUCER,
    provenance=f"generation#{Epoch2Collection.RUN.value}",
    truth_kind=TruthKind.STORED,
)


def _session_runs(document: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
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


def _scope_key(run: Run) -> str:
    """Return the key of the record ``run`` is scoped to."""
    ref = next(value for name, value in run.scope if name.endswith("_ref"))
    return str(ref.entity_key)


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``scope:<key>`` segment, or the marker naming why it has none.

    Args:
        claude_payload: Decoded Claude stdin JSON, read for ``session_id``.
        state_path: Resolved ``.ea/state.json`` path, whose directory is the
            tree root the authority is resolved for; ``None`` when none resolved.

    Returns:
        The scope of the most recently updated Run bound to this session, stated
        by the selected generation at that Run's revision.
    """
    if state_path is None:
        return unavailable_segment(_MODULE, _MODULE, "no-workspace", _SPINE_SOURCE)
    authority = resolve_authority(state_path.parent)
    target, generation = authority.target, authority.generation_id
    if target is None or generation is None:
        # An epoch-1 answer always names its gap, such as marker_absent.
        gap = str(authority.gap).replace("_", "-")
        return unavailable_segment(_MODULE, _MODULE, f"epoch1-{gap}", _SPINE_SOURCE)
    session_id = claude_payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return unavailable_segment(_MODULE, _MODULE, "no-session", _SPINE_SOURCE)
    path = target.generation_path(generation) / GENERATION_DOCUMENT
    try:
        document = read_document(path)
    except (OSError, ValueError) as exc:
        logger.debug(f"build generation-unreadable error={exc}")
        return unavailable_segment(_MODULE, _MODULE, "generation-unreadable", _SPINE_SOURCE)
    rows = _session_runs(document, session_id)
    if not rows:
        return unavailable_segment(_MODULE, _MODULE, "no-run-for-session", _SPINE_SOURCE)
    try:
        runs = [Run.model_validate(row) for row in rows]
    except ValidationError as exc:
        logger.debug(f"build run-invalid error={exc}")
        return unavailable_segment(_MODULE, _MODULE, "run-invalid", _SPINE_SOURCE)
    run = max(runs, key=lambda candidate: candidate.updated_at)
    source = SegmentSource(
        producer=SPINE_PRODUCER,
        provenance=str(run.urn),
        revision=run.revision,
        truth_kind=TruthKind.STORED,
    )
    return sourced_segment(_MODULE, _MODULE, _scope_key(run), source)


__all__ = ["SPINE_PRODUCER", "build"]
