"""Read-only Markdown projections of ``memory.jsonl`` for human reading.

The views live at ``<state_dir>/artifacts/rendered/memory/<scope>.md`` plus
``_all.md`` (union view). They are **derived artefacts**: ``eawf sync``
regenerates them from the cache + JSONL. Hand-edits are NOT preserved between
syncs because the entire file body lives inside a managed region (the
canonical ``<!-- BEGIN EAWF:managed ... -->`` markers from
:mod:`eawf.render.regions`).

Authority chain:

1. ``memory.jsonl`` is the source of truth for memory content (full body and
   supersede history).
2. ``state.memory_index`` is the cache; this view module reads the cache for
   the summary table and reads the JSONL only when the body would be quoted
   (today's view is summary-only).
3. ``<scope>.md`` is a static, byte-stable projection of the cache. Two
   consecutive ``render_all_views`` calls against an unchanged cache produce
   byte-identical files (idempotent).

Concurrency:

The view writes go through :func:`eawf.render._atomic.atomic_write_text`
which uses tempfile + ``os.fsync`` + ``os.replace`` + parent-dir fsync under
a sibling portalock. A reader that opens a view mid-sync therefore sees
either the prior bytes or the fresh bytes — never a torn write.

Default filtering:

:class:`~eawf.state.enums.MemoryStatus.PRUNED` and
:class:`~eawf.state.enums.MemoryStatus.SUPERSEDED` entries are excluded from
the views by default; ``include_superseded=True`` admits ``SUPERSEDED`` so
the view doubles as an "is this rule still alive?" reference. ``PRUNED``
entries are NEVER rendered (they are tombstones).
"""

from __future__ import annotations

import logging
from pathlib import Path

from eawf.memory.store import read_envelopes
from eawf.render._atomic import atomic_write_text
from eawf.render.regions import replace_region
from eawf.state.enums import MemoryStatus
from eawf.state.models import MemorySummary, State

logger = logging.getLogger(__name__)


_VIEW_VERSION: str = "1.0"
_VIEW_FILENAME_ALL: str = "_all.md"


def _safe_summary(text: str) -> str:
    """Collapse whitespace/newlines + escape pipes for a markdown table cell."""
    flat = " ".join(text.split())
    return flat.replace("|", r"\|")


def _format_scope_table(entries: list[MemorySummary]) -> str:
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
    index: dict[str, MemorySummary],
    scope_id: str | None,
    include_superseded: bool,
) -> list[MemorySummary]:
    """Return summaries matching *scope_id*, sorted by ID descending.

    ``PRUNED`` is always excluded. ``SUPERSEDED`` is excluded unless
    *include_superseded* is True.
    """
    out: list[MemorySummary] = []
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


def render_markdown_view(
    *,
    state: State,
    memory_path: Path,
    scope_id: str,
    include_superseded: bool = False,
) -> str:
    """Render the canonical body for ``<scope>.md``.

    Args:
        state: Loaded :class:`State`. ``state.memory_index`` is read for the
            summary table.
        memory_path: Path to ``memory.jsonl``. Reserved for future bodies that
            quote the full payload; today's view is summary-only.
        scope_id: Scope filter. The reserved sentinel
            :data:`SCOPE_ALL` (literally the string ``"_all"``) renders the
            union view rather than filtering.
        include_superseded: When ``True``, ``SUPERSEDED`` entries are also
            included. ``PRUNED`` is never rendered.

    Returns:
        The complete file body (managed-region markers included). Two calls
        with identical inputs return byte-identical strings.
    """
    # `read_envelopes(...)` is invoked here (not stored) to surface a JSONL
    # parse failure early — callers that care about cache vs. on-disk drift
    # should call `find_envelope` directly. Today's view is summary-only.
    if memory_path.exists():
        _ = read_envelopes(memory_path)
    index = state.memory_index or {}
    is_all = scope_id == SCOPE_ALL
    filter_scope = None if is_all else scope_id
    entries = _filter_entries(
        index=index,
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


def render_all_views(
    *,
    state: State,
    memory_path: Path,
    output_dir: Path,
    write: bool = True,
    include_superseded: bool = False,
) -> list[Path]:
    """Render one ``<scope>.md`` per distinct active scope, plus ``_all.md``.

    When the memory index is empty (or every entry has been filtered out)
    no files are produced — a freshly-initialised workspace with no memory
    entries leaves the ``rendered/memory/`` directory absent. As soon as a
    single memory entry is added the next sync emits both the per-scope
    file and the ``_all.md`` union view.

    Args:
        state: Loaded :class:`State`. ``state.memory_index`` is the source.
        memory_path: Path to ``memory.jsonl``. Used by
            :func:`render_markdown_view` for early parse-error detection.
        output_dir: Destination directory; created on demand.
        write: When ``False``, the function computes the file paths and
            content but does not touch the filesystem (dry-run mode used by
            ``eawf sync --dry-run`` / ``--check``).
        include_superseded: Forwarded to :func:`render_markdown_view`.

    Returns:
        The sorted list of view paths the renderer emitted (or would emit
        when ``write=False``). Sorted alphabetically so the output is stable
        across runs. Empty when no memory entries qualify for rendering.
    """
    index = state.memory_index or {}
    scopes: set[str] = {
        s.scope_id
        for s in index.values()
        if s.status != MemoryStatus.PRUNED
        and (include_superseded or s.status != MemoryStatus.SUPERSEDED)
    }
    if not scopes:
        # No memory entries qualify — emit nothing. ``init``-only workspaces
        # therefore see no view drift on subsequent ``sync --check`` calls.
        logger.info(f"render_all_views write={write} count=0 dir={output_dir} (no entries)")
        return []
    paths: list[Path] = []
    if write:
        output_dir.mkdir(parents=True, exist_ok=True)
    for scope in sorted(scopes):
        path = output_dir / f"{scope}.md"
        body = render_markdown_view(
            state=state,
            memory_path=memory_path,
            scope_id=scope,
            include_superseded=include_superseded,
        )
        if write:
            atomic_write_text(path, body + "\n")
        paths.append(path)
    all_path = output_dir / _VIEW_FILENAME_ALL
    all_body = render_markdown_view(
        state=state,
        memory_path=memory_path,
        scope_id=SCOPE_ALL,
        include_superseded=include_superseded,
    )
    if write:
        atomic_write_text(all_path, all_body + "\n")
    paths.append(all_path)
    paths.sort()
    logger.info(f"render_all_views write={write} count={len(paths)} dir={output_dir}")
    return paths


__all__ = [
    "SCOPE_ALL",
    "render_all_views",
    "render_markdown_view",
]
