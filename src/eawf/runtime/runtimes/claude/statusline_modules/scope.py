"""``scope`` statusline module — the scope this session's Run executes against.

The scope resolves through the epoch-2 delivery spine: the host's session id
is hashed the way a Run's ``vendor_session`` stores it, the Run bound to that
session is read from the tree's selected generation, and the segment renders
the key of the record the Run is scoped to (a Task, a Batch, a Milestone...)
as ``scope:<key>``. The frozen epoch-1 document is never read.

Every way the spine can fail to answer renders ``scope:n/a(<reason>)``, where
the reason names what is missing so the operator knows what would fix it:
no workspace, a tree still in epoch 1, no session id from the host, an
unreadable generation document, no Run bound to this session, or a Run row
that does not validate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eawf.kernel.projection.truth import TruthKind
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.runtimes.claude.statusline_modules._spine import (
    SPINE_PRODUCER,
    selected_generation,
    session_run,
)
from eawf.surfaces.render.statusline import (
    SegmentSource,
    StatuslineSegment,
    sourced_segment,
    unavailable_segment,
)

_MODULE = "scope"
_SPINE_SOURCE = SegmentSource(
    producer=SPINE_PRODUCER,
    provenance=f"generation#{Epoch2Collection.RUN.value}",
    truth_kind=TruthKind.STORED,
)


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
    document_path = selected_generation(state_path)
    if isinstance(document_path, str):
        return unavailable_segment(_MODULE, _MODULE, document_path, _SPINE_SOURCE)
    run = session_run(claude_payload, document_path)
    if isinstance(run, str):
        return unavailable_segment(_MODULE, _MODULE, run, _SPINE_SOURCE)
    source = SegmentSource(
        producer=SPINE_PRODUCER,
        provenance=str(run.urn),
        revision=run.revision,
        truth_kind=TruthKind.STORED,
    )
    return sourced_segment(_MODULE, _MODULE, _scope_key(run), source)


__all__ = ["SPINE_PRODUCER", "build"]
