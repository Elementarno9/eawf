"""Unit tests for the workspace-scope TUI dashboard (P20-I01-W05).

Covers:

- Strip + chip rendering: active marker, stale marker, combined.
- ``WorkspaceViewState`` strict construction + cursor bounds.
- ``apply_strip_key`` transitions (left/right/Enter/Esc).
- Empty-registry / read-failed placeholder shapes.
- Frame composition: header + strip + body (W02 quadrant) + footer.
- Pure ``is_stale`` evaluation against the three success-criterion
  signals (registry mtime, repo state mtime, state load failure).
"""

from __future__ import annotations

import io
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel

from eawf.registry import (
    STALE_AFTER,
    Registry,
    RegistryReadError,
    RegistryRepoEntry,
    is_stale,
    read_registry,
    registry_mtime,
    repo_state_mtime,
)
from eawf.tui.layout import QUADRANT_PANE_NAMES
from eawf.tui.workspace import (
    ACTIVE_CHIP_STYLE,
    STALE_CHIP_STYLE,
    WORKSPACE_FOOTER_KEYMAP,
    WorkspaceViewState,
    active_code,
    apply_strip_key,
    build_empty_strip_panel,
    build_strip_panel,
    build_strip_text,
    build_workspace_breadcrumb_text,
    build_workspace_footer_panel,
    build_workspace_frame,
    offline_render,
    render_chip_for,
    render_workspace,
    sorted_entries,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry(
    codes: list[str],
    *,
    active: str | None = None,
    tmp_path: Path | None = None,
    title: str | None = None,
) -> Registry:
    """Build a :class:`Registry` with the given codes for tests.

    When ``tmp_path`` is supplied the entries' ``path`` fields point
    under it (one subdir per code) so the staleness helpers find a
    real on-disk location to stat.
    """
    repos: dict[str, RegistryRepoEntry] = {}
    for code in codes:
        repo_path = (tmp_path / code) if tmp_path is not None else Path(f"/tmp/{code}")
        if tmp_path is not None:
            repo_path.mkdir(parents=True, exist_ok=True)
            (repo_path / ".ea").mkdir(parents=True, exist_ok=True)
            (repo_path / ".ea" / "state.json").write_text(
                json.dumps({"scope_kind": "repo", "project": {"code": code}})
            )
        repos[code] = RegistryRepoEntry(
            code=code,
            path=str(repo_path),
            title=title or code,
        )
    return Registry(
        version="1",
        updated_at=datetime.now(UTC),
        active_code=active,
        repos=repos,
    )


def _write_registry_file(tmp_path: Path, payload: dict[str, Any]) -> Path:
    """Write a registry payload under ``<tmp_path>/.eawf/registry.json``."""
    registry_dir = tmp_path / ".eawf"
    registry_dir.mkdir(parents=True, exist_ok=True)
    target = registry_dir / "registry.json"
    target.write_bytes(orjson.dumps(payload))
    return target


def _render_to_string(renderable: Any, *, width: int = 100) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=width, record=False).print(renderable)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def test_read_registry_loads_valid_payload(tmp_path: Path) -> None:
    target = _write_registry_file(
        tmp_path,
        {
            "version": "1",
            "updated_at": "2026-05-01T12:00:00+00:00",
            "active_code": "EAWF",
            "repos": {
                "EAWF": {"code": "EAWF", "path": "/repos/eawf", "title": "Eä"},
                "DEMO": {"code": "DEMO", "path": "/repos/demo", "title": "Demo"},
            },
        },
    )
    registry = read_registry(path=target)
    assert registry.version == "1"
    assert registry.active_code == "EAWF"
    assert set(registry.repos) == {"EAWF", "DEMO"}
    assert registry.repos["EAWF"].title == "Eä"


def test_read_registry_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RegistryReadError, match="not found"):
        read_registry(path=tmp_path / "nope.json")


def test_read_registry_corrupted_json_raises(tmp_path: Path) -> None:
    target = tmp_path / "bad.json"
    target.write_bytes(b"{not json")
    with pytest.raises(RegistryReadError, match="corrupted"):
        read_registry(path=target)


def test_read_registry_invalid_schema_raises(tmp_path: Path) -> None:
    target = _write_registry_file(tmp_path, {"version": "1", "repos": "nope"})
    with pytest.raises(RegistryReadError, match="invalid registry schema"):
        read_registry(path=target)


def test_read_registry_extras_forbidden(tmp_path: Path) -> None:
    """Pydantic ``extra='forbid'`` rejects unknown top-level keys."""
    target = _write_registry_file(
        tmp_path,
        {
            "version": "1",
            "updated_at": "2026-05-01T12:00:00+00:00",
            "repos": {},
            "unknown_field": "bad",
        },
    )
    with pytest.raises(RegistryReadError, match="invalid registry schema"):
        read_registry(path=target)


def test_registry_mtime_returns_utc_datetime(tmp_path: Path) -> None:
    target = _write_registry_file(tmp_path, {"version": "1", "repos": {}})
    mtime = registry_mtime(path=target)
    assert mtime is not None
    assert mtime.tzinfo is UTC


def test_registry_mtime_missing_returns_none(tmp_path: Path) -> None:
    assert registry_mtime(path=tmp_path / "absent.json") is None


def test_repo_state_mtime_returns_utc_datetime(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "state.json").write_text("{}")
    mtime = repo_state_mtime(repo)
    assert mtime is not None
    assert mtime.tzinfo is UTC


def test_repo_state_mtime_missing_returns_none(tmp_path: Path) -> None:
    assert repo_state_mtime(tmp_path / "absent") is None


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_is_stale_when_registry_mtime_old(tmp_path: Path) -> None:
    registry = _make_registry(["EAWF"], tmp_path=tmp_path)
    entry = registry.repos["EAWF"]
    now = datetime.now(UTC)
    old_registry_mtime = now - timedelta(days=30)
    assert is_stale(entry, registry_mtime_at=old_registry_mtime, now=now) is True


def test_is_stale_when_state_file_missing(tmp_path: Path) -> None:
    entry = RegistryRepoEntry(code="GHOST", path=str(tmp_path / "missing"))
    assert is_stale(entry, registry_mtime_at=datetime.now(UTC)) is True


def test_is_stale_when_state_mtime_old(tmp_path: Path) -> None:
    registry = _make_registry(["EAWF"], tmp_path=tmp_path)
    entry = registry.repos["EAWF"]
    state_path = Path(entry.path) / ".ea" / "state.json"
    very_old = time.time() - timedelta(days=30).total_seconds()
    import os

    os.utime(state_path, (very_old, very_old))
    assert is_stale(entry, registry_mtime_at=datetime.now(UTC)) is True


def test_is_stale_fresh_returns_false(tmp_path: Path) -> None:
    registry = _make_registry(["EAWF"], tmp_path=tmp_path)
    entry = registry.repos["EAWF"]
    now = datetime.now(UTC)
    assert is_stale(entry, registry_mtime_at=now, now=now) is False


def test_is_stale_none_registry_mtime_treated_as_fresh(tmp_path: Path) -> None:
    """A missing registry mtime should not falsely mark fresh state.json stale."""
    registry = _make_registry(["EAWF"], tmp_path=tmp_path)
    entry = registry.repos["EAWF"]
    now = datetime.now(UTC)
    # registry mtime None + fresh state.json → entry is fresh.
    assert is_stale(entry, registry_mtime_at=None, now=now) is False


def test_stale_after_constant_is_14_days() -> None:
    assert timedelta(days=14) == STALE_AFTER


# ---------------------------------------------------------------------------
# WorkspaceViewState
# ---------------------------------------------------------------------------


def test_view_state_default_values() -> None:
    view = WorkspaceViewState()
    assert view.selected_index == 0
    assert view.focused_code is None


def test_view_state_rejects_negative_index() -> None:
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError shape varies
        WorkspaceViewState(selected_index=-1)


def test_view_state_rejects_extra_fields() -> None:
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        WorkspaceViewState(selected_index=0, mystery="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# sorted_entries + active_code
# ---------------------------------------------------------------------------


def test_sorted_entries_alphabetical(tmp_path: Path) -> None:
    registry = _make_registry(["ZED", "ALPHA", "MIDDLE"], tmp_path=tmp_path)
    codes = [e.code for e in sorted_entries(registry)]
    assert codes == ["ALPHA", "MIDDLE", "ZED"]


def test_active_code_prefers_focused(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B"], active="A", tmp_path=tmp_path)
    view = WorkspaceViewState(selected_index=1, focused_code="A")
    assert active_code(registry, view) == "A"


def test_active_code_falls_back_to_cursor(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B"], tmp_path=tmp_path)
    view = WorkspaceViewState(selected_index=1)
    assert active_code(registry, view) == "B"


def test_active_code_falls_back_to_registry_active(tmp_path: Path) -> None:
    # Empty entries can't index → falls through to registry.active_code branch.
    registry = Registry(repos={}, active_code="GHOST")
    view = WorkspaceViewState()
    assert active_code(registry, view) is None  # ghost not in repos


def test_active_code_empty_registry_returns_none() -> None:
    registry = Registry()
    view = WorkspaceViewState()
    assert active_code(registry, view) is None


# ---------------------------------------------------------------------------
# Strip rendering
# ---------------------------------------------------------------------------


def test_render_chip_active_includes_brackets_and_chip(tmp_path: Path) -> None:
    entry = RegistryRepoEntry(code="EAWF", path=str(tmp_path))
    text = render_chip_for(entry, is_active=True, stale=False)
    plain = text.plain
    assert "[EAWF]" in plain
    assert "(active)" in plain
    assert "(stale)" not in plain


def test_render_chip_stale_only_adds_stale_chip(tmp_path: Path) -> None:
    entry = RegistryRepoEntry(code="GHOST", path=str(tmp_path))
    text = render_chip_for(entry, is_active=False, stale=True)
    plain = text.plain
    assert plain.startswith("GHOST")
    assert "(stale)" in plain
    assert "(active)" not in plain


def test_render_chip_both_active_and_stale(tmp_path: Path) -> None:
    entry = RegistryRepoEntry(code="EAWF", path=str(tmp_path))
    text = render_chip_for(entry, is_active=True, stale=True)
    plain = text.plain
    assert "[EAWF]" in plain
    assert "(active)" in plain
    assert "(stale)" in plain


def test_render_chip_plain_no_chips(tmp_path: Path) -> None:
    entry = RegistryRepoEntry(code="DEMO", path=str(tmp_path))
    text = render_chip_for(entry, is_active=False, stale=False)
    assert text.plain == "DEMO"


def test_build_strip_text_lists_all_entries(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B", "C"], active="B", tmp_path=tmp_path)
    text = build_strip_text(registry, WorkspaceViewState(selected_index=1))
    plain = text.plain
    # Each code should surface.
    for code in ("A", "B", "C"):
        assert code in plain


def test_build_strip_text_empty_registry_placeholder() -> None:
    text = build_strip_text(Registry(), WorkspaceViewState())
    assert "no repos registered" in text.plain


def test_build_strip_text_marks_cursor_as_active(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B", "C"], tmp_path=tmp_path)
    text = build_strip_text(registry, WorkspaceViewState(selected_index=2))
    # Active marker brackets the cursor's code.
    assert "[C]" in text.plain
    assert "(active)" in text.plain


# ---------------------------------------------------------------------------
# Strip panel + footer
# ---------------------------------------------------------------------------


def test_build_strip_panel_returns_panel(tmp_path: Path) -> None:
    registry = _make_registry(["A"], tmp_path=tmp_path)
    panel = build_strip_panel(registry, WorkspaceViewState())
    assert isinstance(panel, Panel)
    rendered = _render_to_string(panel)
    assert "workspace" in rendered  # panel title
    assert "A" in rendered


def test_build_empty_strip_panel_carries_message() -> None:
    panel = build_empty_strip_panel("registry unavailable (read failed)")
    rendered = _render_to_string(panel)
    assert "registry unavailable" in rendered


def test_workspace_footer_keymap_lists_strip_keys_first() -> None:
    assert WORKSPACE_FOOTER_KEYMAP.startswith("←/→ strip")
    assert "Enter focus" in WORKSPACE_FOOTER_KEYMAP
    assert "Esc back" in WORKSPACE_FOOTER_KEYMAP
    rendered = _render_to_string(build_workspace_footer_panel())
    assert "strip" in rendered


def test_workspace_breadcrumb_text_has_brand_outside_left() -> None:
    state = {"project": {"code": "DEMO"}, "current": {"phase_id": "P03"}}
    text = build_workspace_breadcrumb_text(state)
    plain = text.plain
    assert plain.startswith("Eä")
    assert "DEMO" in plain


# ---------------------------------------------------------------------------
# Style assertions on chips (spans carry expected styles)
# ---------------------------------------------------------------------------


def test_chip_active_style_is_bold_green(tmp_path: Path) -> None:
    entry = RegistryRepoEntry(code="EAWF", path=str(tmp_path))
    text = render_chip_for(entry, is_active=True, stale=False)
    # Verify the (active) chip carries ACTIVE_CHIP_STYLE among its spans.
    style_strings = [str(s.style) for s in text.spans]
    assert ACTIVE_CHIP_STYLE in style_strings


def test_chip_stale_style_is_muted(tmp_path: Path) -> None:
    entry = RegistryRepoEntry(code="GHOST", path=str(tmp_path))
    text = render_chip_for(entry, is_active=False, stale=True)
    style_strings = [str(s.style) for s in text.spans]
    assert STALE_CHIP_STYLE in style_strings


# ---------------------------------------------------------------------------
# apply_strip_key (pure transitions)
# ---------------------------------------------------------------------------


def test_apply_strip_key_right_advances_cursor(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B", "C"], tmp_path=tmp_path)
    view = WorkspaceViewState(selected_index=0)
    after = apply_strip_key(view, "\x1b[C", registry=registry)
    assert after.selected_index == 1


def test_apply_strip_key_right_clamps_at_end(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B"], tmp_path=tmp_path)
    view = WorkspaceViewState(selected_index=1)
    after = apply_strip_key(view, "\x1b[C", registry=registry)
    assert after.selected_index == 1  # already at the right edge


def test_apply_strip_key_left_decrements_cursor(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B"], tmp_path=tmp_path)
    view = WorkspaceViewState(selected_index=1)
    after = apply_strip_key(view, "\x1b[D", registry=registry)
    assert after.selected_index == 0


def test_apply_strip_key_left_clamps_at_zero(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B"], tmp_path=tmp_path)
    view = WorkspaceViewState(selected_index=0)
    after = apply_strip_key(view, "\x1b[D", registry=registry)
    assert after.selected_index == 0


def test_apply_strip_key_enter_focuses_cursor_code(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B"], tmp_path=tmp_path)
    view = WorkspaceViewState(selected_index=1)
    after = apply_strip_key(view, "\r", registry=registry)
    assert after.focused_code == "B"


def test_apply_strip_key_esc_clears_focus(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B"], tmp_path=tmp_path)
    view = WorkspaceViewState(selected_index=0, focused_code="B")
    after = apply_strip_key(view, "\x1b", registry=registry)
    assert after.focused_code is None


def test_apply_strip_key_unknown_returns_view_unchanged(tmp_path: Path) -> None:
    registry = _make_registry(["A"], tmp_path=tmp_path)
    view = WorkspaceViewState()
    assert apply_strip_key(view, "z", registry=registry) == view


def test_apply_strip_key_enter_on_empty_registry_noop() -> None:
    """Enter on an empty registry must not crash — return the view unchanged."""
    view = WorkspaceViewState()
    after = apply_strip_key(view, "\r", registry=Registry())
    assert after.focused_code is None


def test_apply_strip_key_vim_l_advances_cursor(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B"], tmp_path=tmp_path)
    view = WorkspaceViewState(selected_index=0)
    after = apply_strip_key(view, "l", registry=registry)
    assert after.selected_index == 1


def test_apply_strip_key_vim_h_decrements_cursor(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B"], tmp_path=tmp_path)
    view = WorkspaceViewState(selected_index=1)
    after = apply_strip_key(view, "h", registry=registry)
    assert after.selected_index == 0


# ---------------------------------------------------------------------------
# Frame composition
# ---------------------------------------------------------------------------


def test_build_workspace_frame_layout_has_four_rows(tmp_path: Path) -> None:
    registry = _make_registry(["A"], tmp_path=tmp_path)
    frame = build_workspace_frame(registry, WorkspaceViewState())
    assert isinstance(frame, Layout)
    names = {c.name for c in frame.children}
    assert names == {"header", "strip", "body", "footer"}


def test_build_workspace_frame_renders_brand_and_strip(tmp_path: Path) -> None:
    registry = _make_registry(["A", "B"], tmp_path=tmp_path)
    out = _render_to_string(build_workspace_frame(registry, WorkspaceViewState()))
    assert "Eä" in out
    assert "A" in out and "B" in out
    # W02 pane titles must surface in the body.
    for title in QUADRANT_PANE_NAMES:
        assert title in out


def test_build_workspace_frame_handles_none_registry(tmp_path: Path) -> None:
    """Read-failed registry path renders an unavailable placeholder."""
    out = _render_to_string(build_workspace_frame(None, WorkspaceViewState()))
    assert "Eä" in out
    assert "registry unavailable" in out
    # The quadrant still renders even with an empty active state.
    for title in QUADRANT_PANE_NAMES:
        assert title in out


def test_render_workspace_wraps_frame_into_string(tmp_path: Path) -> None:
    registry = _make_registry(["EAWF"], tmp_path=tmp_path)
    out = render_workspace(registry)
    assert "Eä" in out
    assert "EAWF" in out


def test_render_workspace_writes_into_external_console(tmp_path: Path) -> None:
    registry = _make_registry(["EAWF"], tmp_path=tmp_path)
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, record=False)
    out = render_workspace(registry, console=console)
    assert out == ""  # external console contract
    assert "Eä" in buf.getvalue()


# ---------------------------------------------------------------------------
# offline_render: end-to-end (registry + strip + quadrant)
# ---------------------------------------------------------------------------


def test_offline_render_renders_registry_file(tmp_path: Path) -> None:
    """Full path: registry file -> dashboard frame."""
    # Layout one repo with its own .ea/state.json so the active state
    # populates the breadcrumb.
    repo = tmp_path / "repo-A"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "state.json").write_text(
        json.dumps({"scope_kind": "repo", "project": {"code": "REPOA"}})
    )
    target = _write_registry_file(
        tmp_path,
        {
            "version": "1",
            "updated_at": datetime.now(UTC).isoformat(),
            "active_code": "REPOA",
            "repos": {
                "REPOA": {"code": "REPOA", "path": str(repo), "title": "Repo A"},
            },
        },
    )
    out = offline_render(registry_path=target)
    assert "REPOA" in out
    assert "Eä" in out


def test_offline_render_missing_registry_gives_placeholder(tmp_path: Path) -> None:
    out = offline_render(registry_path=tmp_path / "missing.json")
    assert "registry unavailable" in out


def test_offline_render_stale_entry_carries_chip(tmp_path: Path) -> None:
    repo = tmp_path / "repo-stale"
    (repo / ".ea").mkdir(parents=True)
    state_file = repo / ".ea" / "state.json"
    state_file.write_text("{}")
    # Backdate state.json mtime past STALE_AFTER.
    very_old = time.time() - timedelta(days=30).total_seconds()
    import os

    os.utime(state_file, (very_old, very_old))
    target = _write_registry_file(
        tmp_path,
        {
            "version": "1",
            "updated_at": datetime.now(UTC).isoformat(),
            "active_code": "STALE",
            "repos": {
                "STALE": {"code": "STALE", "path": str(repo), "title": "Stale Repo"},
            },
        },
    )
    out = offline_render(registry_path=target)
    assert "(stale)" in out
