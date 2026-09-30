"""Read-only Markdown projections of the memory notes for human reading.

The views live at ``<state_dir>/artifacts/rendered/memory/<scope>.md`` plus
``_all.md`` (union view). They are **derived artefacts**: ``eawf sync``
regenerates them from the notes the tree stands at -- the generation's memory
ledger on an epoch-2 tree. Hand-edits are NOT preserved between syncs because
the entire file body lives inside a managed region (the canonical
``<!-- BEGIN EAWF:managed ... -->`` markers from
:mod:`eawf.surfaces.render.regions`).

A view is a static, byte-stable projection of the notes: two consecutive
:func:`render_note_views` calls over unchanged notes produce byte-identical
files (idempotent). Today's view is summary-only.

Concurrency:

The view writes go through :func:`eawf.surfaces.render._atomic.atomic_write_text`
which uses tempfile + ``os.fsync`` + ``os.replace`` + parent-dir fsync under
a sibling portalock. A reader that opens a view mid-sync therefore sees
either the prior bytes or the fresh bytes — never a torn write.

Default filtering:

:class:`~eawf.kernel.state.enums.MemoryStatus.PRUNED` and
:class:`~eawf.kernel.state.enums.MemoryStatus.SUPERSEDED` entries are excluded from
the views by default; ``include_superseded=True`` admits ``SUPERSEDED`` so
the view doubles as an "is this rule still alive?" reference. ``PRUNED``
entries are NEVER rendered (they are tombstones).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from eawf.kernel.state.enums import MemoryStatus
from eawf.kernel.store.kinds.memory import MemoryNote
from eawf.surfaces.render._atomic import atomic_write_text
from eawf.surfaces.render.regions import replace_region

logger = logging.getLogger(__name__)


_VIEW_VERSION: str = "1.0"
_VIEW_FILENAME_ALL: str = "_all.md"


def _safe_summary(text: str) -> str:
    """Collapse whitespace/newlines + escape pipes for a markdown table cell."""
    flat = " ".join(text.split())
    return flat.replace("|", r"\|")


def _format_scope_table(entries: list[MemoryNote]) -> str:
    """Return the body table (no markers) for a sorted entry list.

    The body is deterministic: rows are pre-sorted by descending ID (newer
    timestamps first when the canonical ``MEM-<UTC-date>-<NN>`` allocator is
    in use), pipes in cell content are escaped, and trailing whitespace is
    trimmed so the body hashes byte-stably across re-runs.
    """
    header = "| ID | Confidence | Status | Summary |\n|---|---|---|---|"
    rows = [
        (f"| {e.id} | {e.confidence.value} | {e.status.value} | {_safe_summary(e.summary)} |")
        for e in entries
    ]
    if not rows:
        rows = ["| _(no entries)_ |  |  |  |"]
    return "\n".join([header, *rows])


def _filter_entries(
    *,
    index: Mapping[str, MemoryNote],
    scope_id: str | None,
    include_superseded: bool,
) -> list[MemoryNote]:
    """Return summaries matching *scope_id*, sorted by ID descending.

    ``PRUNED`` is always excluded. ``SUPERSEDED`` is excluded unless
    *include_superseded* is True.
    """
    out: list[MemoryNote] = []
    for summary in index.values():
        if summary.status == MemoryStatus.PRUNED:
            continue
        if summary.status == MemoryStatus.SUPERSEDED and not include_superseded:
            continue
        if scope_id is not None and summary.scope_id != scope_id:
            continue
        out.append(summary)
    out.sort(key=lambda s: s.id, reverse=True)
    return out


def render_note_view(
    notes: Mapping[str, MemoryNote], *, scope_id: str, include_superseded: bool = False
) -> str:
    """Render the canonical body for ``<scope>.md`` from *notes*.

    Args:
        notes: The notes, by id.
        scope_id: Scope filter, or :data:`SCOPE_ALL` for the union view.
        include_superseded: Also render SUPERSEDED notes; PRUNED never.

    Returns:
        The complete file body, managed-region markers included; identical
        inputs return byte-identical strings.
    """
    is_all = scope_id == SCOPE_ALL
    filter_scope = None if is_all else scope_id
    entries = _filter_entries(
        index=notes,
        scope_id=filter_scope,
        include_superseded=include_superseded,
    )
    title_scope = "all scopes" if is_all else scope_id
    region_id = f"memory-view-{scope_id}"
    body_lines = [
        f"# Memory — {title_scope}",
        "",
        _format_scope_table(entries),
    ]
    body = "\n".join(body_lines)
    return replace_region(text="", id=region_id, version=_VIEW_VERSION, body=body)


SCOPE_ALL: str = "_all"


def render_note_views(
    notes: Mapping[str, MemoryNote],
    *,
    output_dir: Path,
    write: bool = True,
    include_superseded: bool = False,
) -> list[Path]:
    """Render one ``<scope>.md`` per distinct rendered scope of *notes*, plus ``_all.md``.

    Args:
        notes: The notes, by id.
        output_dir: Destination directory; created on demand.
        write: When ``False``, compute the paths without touching the filesystem.
        include_superseded: Forwarded to :func:`render_note_view`.

    Returns:
        The sorted view paths emitted (or that would be); empty when no
        note qualifies.
    """
    scopes: set[str] = {
        s.scope_id
        for s in notes.values()
        if s.status != MemoryStatus.PRUNED
        and (include_superseded or s.status != MemoryStatus.SUPERSEDED)
    }
    if not scopes:
        # No memory entries qualify — emit nothing. ``init``-only workspaces
        # therefore see no view drift on subsequent ``sync --check`` calls.
        logger.info(f"render_note_views write={write} count=0 dir={output_dir}; no entries")
        return []
    paths: list[Path] = []
    if write:
        output_dir.mkdir(parents=True, exist_ok=True)
    for scope in sorted(scopes):
        path = output_dir / f"{scope}.md"
        body = render_note_view(notes, scope_id=scope, include_superseded=include_superseded)
        if write:
            atomic_write_text(path, body + "\n")
        paths.append(path)
    all_path = output_dir / _VIEW_FILENAME_ALL
    all_body = render_note_view(notes, scope_id=SCOPE_ALL, include_superseded=include_superseded)
    if write:
        atomic_write_text(all_path, all_body + "\n")
    paths.append(all_path)
    paths.sort()
    logger.info(f"render_note_views write={write} count={len(paths)} dir={output_dir}")
    return paths


__all__ = [
    "SCOPE_ALL",
    "render_note_view",
    "render_note_views",
]
