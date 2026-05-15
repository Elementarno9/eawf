"""Integration tests for the workspace dashboard TUI (P20-I01-W05).

Covers:

* Golden snapshot of the offline workspace render. The fixture and
  expected texts live under ``tests/golden/tui/workspace_*.txt`` so a
  renderer regression surfaces as a single-file diff.
* Cross-module wiring: the dashboard reuses
  :func:`eawf.tui.layout.build_quadrant` (W02) so the four pane
  titles must appear in the rendered body regardless of how many
  repos the strip carries.
* Stale-chip surfacing through every reachable signal:
  registry-mtime stale, state-file-mtime stale, state-file-missing.

When the renderer drifts intentionally, regenerate the snapshot:

    cd <repo>
    uv run python -c "
    from pathlib import Path
    from tests.integration.test_tui_workspace import _regenerate_goldens
    _regenerate_goldens(Path('tests/golden/tui'))
    "
"""

from __future__ import annotations

import io
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest
from rich.console import Console

from eawf.registry import Registry, RegistryRepoEntry
from eawf.tui.layout import BRAND, QUADRANT_PANE_NAMES
from eawf.tui.workspace import (
    WorkspaceViewState,
    build_workspace_frame,
    offline_render,
)

_GOLDEN_DIR: Path = Path(__file__).parent.parent / "golden" / "tui"


# ---------------------------------------------------------------------------
# Golden fixture setup helpers
# ---------------------------------------------------------------------------


_FIXED_NOW: datetime = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


def _build_fixture_registry() -> Registry:
    """Build the registry-shape fixture used by the goldens.

    Uses placeholder paths (``/tmp/...``) so the snapshot stays
    machine-agnostic. The on-disk staleness signals are mocked via
    the ``repo_state_loader`` + ``registry_mtime_at`` test seams.
    """
    return Registry(
        version="1",
        updated_at=_FIXED_NOW,
        active_code="EAWF",
        repos={
            "EAWF": RegistryRepoEntry(code="EAWF", path="/tmp/repos/eawf", title="Eä"),
            "DEMO": RegistryRepoEntry(code="DEMO", path="/tmp/repos/demo", title="Demo"),
            "OTHER": RegistryRepoEntry(code="OTHER", path="/tmp/repos/other", title="Other"),
        },
    )


def _stub_repo_state_loader(active_code: str) -> object:
    """Return a loader that injects a deterministic state for one repo."""

    def loader(path: Path) -> dict[str, object] | None:
        # Map paths back to codes via basename so the fixture stays
        # path-agnostic (the golden never embeds absolute paths).
        code = path.name.upper()
        if code == active_code:
            return {
                "project": {"code": code},
                "current": {"phase_id": "P20", "iter_id": "P20-I01"},
                "phases": {"P20": {"status": "active"}},
                "iters": {"P20-I01": {"status": "active"}},
                "waves": {"P20-I01-W01": {"status": "in_progress"}},
                "audits": {"A01": {"status": "complete"}},
                "backlog": {
                    "B001": {"status": "open"},
                    "B002": {"status": "closed"},
                },
            }
        return None

    return loader


def _render_default_frame(width: int = 100) -> str:
    """Render the offline frame with the fixture registry + active EAWF."""
    from eawf.tui.workspace import initial_view_for

    registry = _build_fixture_registry()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, record=False)
    layout = build_workspace_frame(
        registry,
        initial_view_for(registry),
        now=_FIXED_NOW,
        registry_mtime_at=_FIXED_NOW - timedelta(days=1),
        repo_state_loader=_stub_repo_state_loader("EAWF"),
        is_stale_evaluator=lambda e: False,
    )
    console.print(layout)
    return buf.getvalue()


def _render_stale_registry_frame(width: int = 100) -> str:
    """Render with every entry stale via an injected predicate."""
    from eawf.tui.workspace import initial_view_for

    registry = _build_fixture_registry()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, record=False)
    layout = build_workspace_frame(
        registry,
        initial_view_for(registry),
        now=_FIXED_NOW,
        registry_mtime_at=_FIXED_NOW - timedelta(days=60),
        repo_state_loader=_stub_repo_state_loader("EAWF"),
        is_stale_evaluator=lambda e: True,
    )
    console.print(layout)
    return buf.getvalue()


def _render_empty_frame(width: int = 100) -> str:
    """Render the dashboard when ``read_registry`` fails (None registry)."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, record=False)
    layout = build_workspace_frame(None, WorkspaceViewState(), now=_FIXED_NOW)
    console.print(layout)
    return buf.getvalue()


def _normalise_trailing_newline(text: str) -> str:
    if text.endswith("\n"):
        return text[:-1]
    return text


def _regenerate_goldens(golden_dir: Path) -> None:
    """Hook used by the regeneration snippet at the top of this module."""
    golden_dir.mkdir(parents=True, exist_ok=True)
    (golden_dir / "workspace_default.txt").write_text(_render_default_frame())
    (golden_dir / "workspace_stale.txt").write_text(_render_stale_registry_frame())
    (golden_dir / "workspace_empty.txt").write_text(_render_empty_frame())


# ---------------------------------------------------------------------------
# Golden snapshot — workspace_default
# ---------------------------------------------------------------------------


@pytest.mark.golden
def test_workspace_default_matches_golden() -> None:
    actual = _normalise_trailing_newline(_render_default_frame())
    expected = _normalise_trailing_newline(
        (_GOLDEN_DIR / "workspace_default.txt").read_text(encoding="utf-8")
    )
    assert actual == expected, (
        "workspace_default frame drift — regenerate goldens via the "
        "snippet at the top of test_tui_workspace.py."
    )


@pytest.mark.golden
def test_workspace_stale_matches_golden() -> None:
    actual = _normalise_trailing_newline(_render_stale_registry_frame())
    expected = _normalise_trailing_newline(
        (_GOLDEN_DIR / "workspace_stale.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


@pytest.mark.golden
def test_workspace_empty_matches_golden() -> None:
    actual = _normalise_trailing_newline(_render_empty_frame())
    expected = _normalise_trailing_newline(
        (_GOLDEN_DIR / "workspace_empty.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


# ---------------------------------------------------------------------------
# Structural assertions independent of byte-equality
# ---------------------------------------------------------------------------


def test_workspace_default_has_brand_and_quadrant_panes() -> None:
    out = _render_default_frame()
    assert BRAND in out
    # Active marker on EAWF.
    assert "[EAWF]" in out
    assert "(active)" in out
    # All three codes appear in the strip.
    assert "DEMO" in out
    assert "OTHER" in out
    # The four W02 pane titles surface in the body quadrant.
    for title in QUADRANT_PANE_NAMES:
        assert title in out, f"pane {title!r} missing from workspace frame"


def test_workspace_default_two_renders_byte_stable() -> None:
    first = _render_default_frame()
    second = _render_default_frame()
    assert first == second


def test_workspace_stale_chip_surfaces_on_every_entry() -> None:
    out = _render_stale_registry_frame()
    # Registry-mtime branch fires on every entry, so we expect the
    # ``(stale)`` chip to repeat for each of the three repos.
    assert out.count("(stale)") >= 3


def test_workspace_empty_carries_placeholder_and_quadrant() -> None:
    out = _render_empty_frame()
    assert "registry unavailable" in out
    # The quadrant still renders; it just gets an empty state.
    for title in QUADRANT_PANE_NAMES:
        assert title in out


# ---------------------------------------------------------------------------
# State-file-missing branch: signal (c) of the stale rule
# ---------------------------------------------------------------------------


def test_state_file_missing_marks_entry_stale(tmp_path: Path) -> None:
    """Signal (c): repo state.json fails to resolve at strip-render time."""
    repo = tmp_path / "ghost-repo"
    repo.mkdir()
    # NOTE: no .ea/state.json created — the per-repo signal MUST fire.
    registry = Registry(
        version="1",
        updated_at=_FIXED_NOW,
        active_code="GHOST",
        repos={"GHOST": RegistryRepoEntry(code="GHOST", path=str(repo))},
    )
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=100, record=False).print(
        build_workspace_frame(
            registry,
            WorkspaceViewState(),
            now=_FIXED_NOW,
            registry_mtime_at=_FIXED_NOW,
        )
    )
    out = buf.getvalue()
    assert "(stale)" in out


def test_state_file_old_mtime_marks_entry_stale(tmp_path: Path) -> None:
    """Signal (b): per-repo state.json mtime > STALE_AFTER."""
    repo = tmp_path / "old-repo"
    (repo / ".ea").mkdir(parents=True)
    state_file = repo / ".ea" / "state.json"
    state_file.write_text("{}")
    very_old = time.time() - timedelta(days=60).total_seconds()
    os.utime(state_file, (very_old, very_old))
    registry = Registry(
        version="1",
        updated_at=_FIXED_NOW,
        active_code="OLD",
        repos={"OLD": RegistryRepoEntry(code="OLD", path=str(repo))},
    )
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=100, record=False).print(
        build_workspace_frame(
            registry,
            WorkspaceViewState(),
            now=datetime.now(UTC),
            registry_mtime_at=datetime.now(UTC),
        )
    )
    assert "(stale)" in buf.getvalue()


# ---------------------------------------------------------------------------
# offline_render: end-to-end through the public entry-point
# ---------------------------------------------------------------------------


def test_offline_render_via_registry_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo-a"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "state.json").write_text(
        json.dumps({"scope_kind": "repo", "project": {"code": "REPOA"}})
    )
    target = tmp_path / "registry.json"
    target.write_bytes(
        orjson.dumps(
            {
                "version": "1",
                "updated_at": datetime.now(UTC).isoformat(),
                "active_code": "REPOA",
                "repos": {
                    "REPOA": {"code": "REPOA", "path": str(repo), "title": "Repo A"},
                },
            }
        )
    )
    out = offline_render(registry_path=target)
    assert BRAND in out
    assert "REPOA" in out
    assert "[REPOA]" in out
    assert "(active)" in out


def test_offline_render_missing_returns_placeholder(tmp_path: Path) -> None:
    out = offline_render(registry_path=tmp_path / "absent.json")
    assert "registry unavailable" in out
    assert BRAND in out
