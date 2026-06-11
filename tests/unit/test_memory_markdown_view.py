"""Unit tests for ``memory.markdown_view`` — read-only projection of memory.jsonl."""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.state.enums import Confidence, MemoryStatus
from eawf.kernel.state.models import State
from eawf.platform.memory.markdown_view import (
    SCOPE_ALL,
    render_all_views,
    render_markdown_view,
)
from eawf.platform.memory.store import add_memory
from eawf.surfaces.render.regions import find_regions


def _make_state() -> State:
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def test_render_markdown_view_emits_managed_region_markers(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="rule",
        body="body text",
        confidence=Confidence.HIGH,
    )
    body = render_markdown_view(state=state, memory_path=memory_path, scope_id="QR")
    regions = find_regions(body)
    assert len(regions) == 1
    assert regions[0].id == "memory-view-QR"
    assert regions[0].version == "1.0"
    # Body table has the entry id in it.
    assert "MEM-" in regions[0].body
    assert "QR" in body


def test_render_markdown_view_excludes_pruned_and_superseded_by_default(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    active = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="active",
        body="body",
    )
    superseded = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="superseded",
        body="body",
    )
    pruned = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="pruned",
        body="body",
    )
    assert state.memory_index is not None
    state.memory_index[superseded.summary.id] = state.memory_index[
        superseded.summary.id
    ].model_copy(update={"status": MemoryStatus.SUPERSEDED})
    state.memory_index[pruned.summary.id] = state.memory_index[pruned.summary.id].model_copy(
        update={"status": MemoryStatus.PRUNED}
    )

    body = render_markdown_view(state=state, memory_path=memory_path, scope_id="QR")
    assert active.summary.id in body
    assert superseded.summary.id not in body
    assert pruned.summary.id not in body

    body_with_super = render_markdown_view(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        include_superseded=True,
    )
    assert active.summary.id in body_with_super
    assert superseded.summary.id in body_with_super
    assert pruned.summary.id not in body_with_super


def test_render_all_views_writes_one_file_per_scope(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
    add_memory(state=state, memory_path=memory_path, scope_id="P01", title="t", body="b")
    output_dir = tmp_path / "rendered" / "memory"
    paths = render_all_views(
        state=state, memory_path=memory_path, output_dir=output_dir, write=True
    )
    names = sorted(p.name for p in paths)
    assert names == ["P01.md", "QR.md", "_all.md"]
    for path in paths:
        assert path.exists()
        # Each file body has exactly one managed region.
        regions = find_regions(path.read_text(encoding="utf-8"))
        assert len(regions) == 1


def test_render_all_views_idempotent_byte_equal_on_re_run(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="body")
    output_dir = tmp_path / "rendered" / "memory"
    render_all_views(state=state, memory_path=memory_path, output_dir=output_dir, write=True)
    first_bytes = {p: p.read_bytes() for p in output_dir.glob("*.md")}
    render_all_views(state=state, memory_path=memory_path, output_dir=output_dir, write=True)
    second_bytes = {p: p.read_bytes() for p in output_dir.glob("*.md")}
    assert first_bytes == second_bytes


def test_render_markdown_view_handles_empty_memory_index(tmp_path: Path) -> None:
    """Direct render still produces a body with the no-entries fallback."""
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"  # never created
    body = render_markdown_view(state=state, memory_path=memory_path, scope_id=SCOPE_ALL)
    regions = find_regions(body)
    assert len(regions) == 1
    assert "no entries" in regions[0].body


def test_render_all_views_emits_nothing_when_no_entries(tmp_path: Path) -> None:
    """The orchestrator returns an empty list when memory_index is empty.

    ``init``-only workspaces therefore see no view drift on subsequent
    ``sync --check`` calls.
    """
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    output_dir = tmp_path / "rendered" / "memory"
    paths = render_all_views(
        state=state, memory_path=memory_path, output_dir=output_dir, write=True
    )
    assert paths == []
    assert not output_dir.exists()


def test_render_markdown_view_sorts_entries_by_id_descending(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    first = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="a", body="b")
    second = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="c", body="d")
    body = render_markdown_view(state=state, memory_path=memory_path, scope_id="QR")
    # The newer (larger NN suffix) entry must appear before the older one.
    assert body.find(second.summary.id) < body.find(first.summary.id)
