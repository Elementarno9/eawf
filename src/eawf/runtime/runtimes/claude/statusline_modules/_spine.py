"""The epoch-2 reads the spine-backed statusline segments share.

A tree cut over to epoch 2 keeps its authority in the selected generation.
The ``scope``, ``budget``, ``mcp_health`` and ``memory`` segments read it
there: the first two through the Run bound to the host's session, the others
through the MCP registry rows and the memory ledger. A live Run sits in the
generation's status projection, which reading the document merges in, so the
Run ``eawf run start`` started inside the session is found there; its binding
and usage readings sit on the run ledger beside it. Every way the spine can
fail to answer comes back as one reason token the segment renders as
``n/a(<reason>)``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.session.vendor_id import hash_vendor_session_id

if TYPE_CHECKING:
    from eawf.kernel.state.epoch2.run import Run

logger = logging.getLogger(__name__)

#: The producer a segment read from the tree's selected epoch-2 generation names.
SPINE_PRODUCER: Final = "eawf.epoch2-generation"

#: The reason a segment names when no workspace, and so no tree, resolved.
NO_STATE: Final = "no-state"

#: The documents read during the render in progress, by path. ``None``
#: outside :func:`one_read_per_render`, where every read goes to disk.
_RENDER_DOCUMENTS: ContextVar[dict[Path, dict[str, Any]] | None] = ContextVar(
    "_RENDER_DOCUMENTS", default=None
)


@contextmanager
def one_read_per_render() -> Iterator[None]:
    """Share each generation document read among the segments of one render.

    The ``scope``, ``budget`` and ``mcp_health`` segments each read the same
    document, which runs to megabytes on a long-lived tree; within one
    render they read the same bytes, so the first read serves the others.
    """
    token = _RENDER_DOCUMENTS.set({})
    try:
        yield
    finally:
        _RENDER_DOCUMENTS.reset(token)


def read_generation(document_path: Path) -> dict[str, Any]:
    """Return the generation document at *document_path*, its projection merged in.

    Args:
        document_path: The selected generation's document.

    Returns:
        The decoded document; inside :func:`one_read_per_render` the one
        object every segment of the render reads, so callers must not
        mutate it.

    Raises:
        OSError: The document could not be read.
        ValueError: The document or its projection is not a JSON object.
    """
    from eawf.kernel.store.compaction import read_document

    documents = _RENDER_DOCUMENTS.get()
    if documents is None:
        return read_document(document_path)
    if document_path not in documents:
        documents[document_path] = read_document(document_path)
    return documents[document_path]


def selected_generation(state_path: Path) -> Path | str:
    """Return the selected generation's document, or the reason there is none.

    Args:
        state_path: Resolved ``.ea/state.json`` path, whose directory is the
            tree root the authority is resolved for.

    Returns:
        The generation document path, or ``epoch1-<gap>`` when the tree
        answers in epoch 1.
    """
    # Imported here, not at module load: the authority and generation models
    # pull in most of the kernel's state models, and every statusline redraw
    # imports this module whether or not a segment reaches the spine.
    from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
    from eawf.kernel.state.epoch2.authority import resolve_authority

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
        document = read_generation(document_path)
    except (OSError, ValueError) as exc:
        logger.debug(f"session_run generation-unreadable error={exc}")
        return "generation-unreadable"
    rows = _session_rows(document, session_id)
    if not rows:
        return "no-run-for-session"
    # The Run model is built only once a row needs validating; most redraws
    # find no Run for the session.
    from eawf.kernel.state.epoch2.run import Run

    try:
        runs = [Run.model_validate(row) for row in rows]
    except ValidationError as exc:
        logger.debug(f"session_run run-invalid error={exc}")
        return "run-invalid"
    return max(runs, key=lambda candidate: candidate.updated_at)


__all__ = [
    "NO_STATE",
    "SPINE_PRODUCER",
    "one_read_per_render",
    "read_generation",
    "selected_generation",
    "session_run",
]
