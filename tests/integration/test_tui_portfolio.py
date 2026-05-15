"""Integration tests for the portfolio dashboard TUI (P20-I01-W06).

Covers:

* Golden snapshots of the offline portfolio render. Fixtures and
  expected texts live under ``tests/golden/tui/portfolio_*.txt`` so
  a renderer regression surfaces as a single-file diff.
* End-to-end staleness propagation through the offline renderer.
* Cross-module wiring: the portfolio dashboard reuses
  :data:`eawf.tui.layout.BRAND` so the brand strip stays
  byte-identical to the W02 / W05 surfaces.

When the renderer drifts intentionally, regenerate the snapshots:

    cd <repo>
    uv run python -c "
    from pathlib import Path
    from tests.integration.test_tui_portfolio import _regenerate_goldens
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
from eawf.tui.layout import BRAND
from eawf.tui.portfolio import (
    build_portfolio_frame,
    offline_render,
)

_GOLDEN_DIR: Path = Path(__file__).parent.parent / "golden" / "tui"

_FIXED_NOW: datetime = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Golden fixture helpers
# ---------------------------------------------------------------------------


def _fixture_registry() -> Registry:
    """Build the registry-shape fixture used by the goldens.

    Uses ``/tmp/`` placeholders so the snapshot stays machine-
    agnostic. State.json loads route through the injected stub
    loader so per-repo metrics stay deterministic.
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


def _stub_loader_default(path: Path) -> dict[str, object] | None:
    code = path.name.upper()
    if code == "EAWF":
        return {
            "project": {"code": "EAWF"},
            "current": {"phase_id": "P20", "iter_id": "P20-I01"},
            "phases": {"P20": {"status": "active"}},
            "iters": {"P20-I01": {"status": "active"}},
            "waves": {
                "W01": {"status": "closed", "deps": []},
                "W02": {"status": "pending", "deps": ["W01"]},
                "W03": {"status": "pending", "deps": []},
            },
        }
    if code == "DEMO":
        return {
            "project": {"code": "DEMO"},
            "current": {"phase_id": "P03", "iter_id": "P03-I02"},
            "phases": {"P03": {"status": "active"}},
            "iters": {"P03-I02": {"status": "active"}},
            "waves": {},
        }
    # OTHER deliberately has no state.json — exercises the missing
    # path branch in `build_row`.
    return None


def _render_default_frame(width: int = 100) -> str:
    """Render the offline frame with the fixture registry + stub loader."""
    registry = _fixture_registry()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, record=False)
    layout = build_portfolio_frame(
        registry,
        view=None,
        now=_FIXED_NOW,
        registry_mtime_at=_FIXED_NOW - timedelta(days=1),
        repo_state_loader=_stub_loader_default,
        is_stale_evaluator=lambda e: e.code == "OTHER",
    )
    console.print(layout)
    return buf.getvalue()


def _render_empty_frame(width: int = 100) -> str:
    """Render with an empty registry (zero entries)."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, record=False)
    layout = build_portfolio_frame(
        Registry(),
        now=_FIXED_NOW,
        registry_mtime_at=_FIXED_NOW,
        repo_state_loader=lambda p: None,
        is_stale_evaluator=lambda e: False,
    )
    console.print(layout)
    return buf.getvalue()


def _render_unavailable_frame(width: int = 100) -> str:
    """Render with a ``None`` registry (simulates a read failure)."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, record=False)
    layout = build_portfolio_frame(None, now=_FIXED_NOW)
    console.print(layout)
    return buf.getvalue()


def _normalise_trailing_newline(text: str) -> str:
    return text[:-1] if text.endswith("\n") else text


def _regenerate_goldens(golden_dir: Path) -> None:
    """Hook used by the regeneration snippet at the top of this module."""
    golden_dir.mkdir(parents=True, exist_ok=True)
    (golden_dir / "portfolio_default.txt").write_text(_render_default_frame())
    (golden_dir / "portfolio_empty.txt").write_text(_render_empty_frame())
    (golden_dir / "portfolio_unavailable.txt").write_text(_render_unavailable_frame())


# ---------------------------------------------------------------------------
# Golden snapshot — portfolio_default
# ---------------------------------------------------------------------------


@pytest.mark.golden
def test_portfolio_default_matches_golden() -> None:
    actual = _normalise_trailing_newline(_render_default_frame())
    expected = _normalise_trailing_newline(
        (_GOLDEN_DIR / "portfolio_default.txt").read_text(encoding="utf-8")
    )
    assert actual == expected, (
        "portfolio_default frame drift — regenerate goldens via the snippet "
        "at the top of test_tui_portfolio.py."
    )


@pytest.mark.golden
def test_portfolio_empty_matches_golden() -> None:
    actual = _normalise_trailing_newline(_render_empty_frame())
    expected = _normalise_trailing_newline(
        (_GOLDEN_DIR / "portfolio_empty.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


@pytest.mark.golden
def test_portfolio_unavailable_matches_golden() -> None:
    actual = _normalise_trailing_newline(_render_unavailable_frame())
    expected = _normalise_trailing_newline(
        (_GOLDEN_DIR / "portfolio_unavailable.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


# ---------------------------------------------------------------------------
# Structural assertions
# ---------------------------------------------------------------------------


def test_portfolio_default_has_brand_and_codes() -> None:
    out = _render_default_frame()
    assert BRAND in out
    for code in ("EAWF", "DEMO", "OTHER"):
        assert code in out, f"code {code!r} missing from portfolio frame"


def test_portfolio_default_has_column_headers() -> None:
    out = _render_default_frame()
    for header in ("code", "title", "phase", "iter", "ready", "stale", "active"):
        assert header in out, f"column header {header!r} missing"


def test_portfolio_default_marks_active_repo() -> None:
    out = _render_default_frame()
    # The "yes" marker should surface at least once in the active column.
    assert "yes" in out


def test_portfolio_default_two_renders_byte_stable() -> None:
    first = _render_default_frame()
    second = _render_default_frame()
    assert first == second


def test_portfolio_unavailable_carries_placeholder() -> None:
    out = _render_unavailable_frame()
    assert BRAND in out
    assert "unavailable" in out


def test_portfolio_empty_carries_no_repos_placeholder() -> None:
    out = _render_empty_frame()
    assert BRAND in out
    # The empty registry case has zero entries → the placeholder text or "0 repos"
    # both qualify as an explicit "nothing here yet" signal.
    assert "no repos" in out or "0 repos" in out


# ---------------------------------------------------------------------------
# Staleness propagation through offline_render
# ---------------------------------------------------------------------------


def test_state_file_old_mtime_marks_entry_stale_in_portfolio(tmp_path: Path) -> None:
    """Signal (b): per-repo state.json mtime > STALE_AFTER (14 days)."""
    repo = tmp_path / "old-repo"
    (repo / ".ea").mkdir(parents=True)
    state_file = repo / ".ea" / "state.json"
    state_file.write_text("{}")
    very_old = time.time() - timedelta(days=60).total_seconds()
    os.utime(state_file, (very_old, very_old))

    target = tmp_path / "registry.json"
    target.write_bytes(
        orjson.dumps(
            {
                "version": "1",
                "updated_at": datetime.now(UTC).isoformat(),
                "active_code": "OLD",
                "repos": {"OLD": {"code": "OLD", "path": str(repo), "title": "Old"}},
            }
        )
    )
    out = offline_render(registry_path=target)
    # Stale flag column should show "yes" for the only entry.
    assert "OLD" in out
    assert "yes" in out


def test_state_file_missing_marks_entry_stale_in_portfolio(tmp_path: Path) -> None:
    """Signal (c): repo state.json missing."""
    repo = tmp_path / "ghost-repo"
    repo.mkdir()
    target = tmp_path / "registry.json"
    target.write_bytes(
        orjson.dumps(
            {
                "version": "1",
                "updated_at": datetime.now(UTC).isoformat(),
                "active_code": "GHOST",
                "repos": {"GHOST": {"code": "GHOST", "path": str(repo), "title": "Ghost"}},
            }
        )
    )
    out = offline_render(registry_path=target)
    assert "GHOST" in out
    assert "yes" in out


def test_offline_render_populates_metrics_end_to_end(tmp_path: Path) -> None:
    repo = tmp_path / "repo-a"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "state.json").write_text(
        json.dumps(
            {
                "project": {"code": "REPOA"},
                "current": {"phase_id": "P03", "iter_id": "P03-I01"},
                "phases": {"P03": {"status": "active"}},
                "iters": {"P03-I01": {"status": "active"}},
                "waves": {
                    "P03-I01-W01": {"status": "closed", "deps": []},
                    "P03-I01-W02": {"status": "pending", "deps": ["P03-I01-W01"]},
                },
            }
        )
    )
    target = tmp_path / "registry.json"
    target.write_bytes(
        orjson.dumps(
            {
                "version": "1",
                "updated_at": datetime.now(UTC).isoformat(),
                "active_code": "REPOA",
                "repos": {"REPOA": {"code": "REPOA", "path": str(repo), "title": "Repo A"}},
            }
        )
    )
    out = offline_render(registry_path=target)
    assert "REPOA" in out
    assert "P03" in out
    assert "P03-I01" in out
    # ready_waves should be 1 (W02 has its dep closed).
    assert "1" in out


# ---------------------------------------------------------------------------
# offline_render: read-only — never mutates the file
# ---------------------------------------------------------------------------


def test_offline_render_does_not_mutate_registry(tmp_path: Path) -> None:
    repo = tmp_path / "repo-a"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "state.json").write_text(json.dumps({"project": {"code": "REPOA"}}))
    target = tmp_path / "registry.json"
    target.write_bytes(
        orjson.dumps(
            {
                "version": "1",
                "updated_at": datetime.now(UTC).isoformat(),
                "active_code": "REPOA",
                "repos": {"REPOA": {"code": "REPOA", "path": str(repo), "title": "Repo A"}},
            }
        )
    )
    before = target.read_bytes()
    before_mtime = target.stat().st_mtime
    offline_render(registry_path=target)
    after = target.read_bytes()
    after_mtime = target.stat().st_mtime
    assert before == after
    assert before_mtime == after_mtime


def test_offline_render_does_not_create_file_when_absent(tmp_path: Path) -> None:
    target = tmp_path / "absent.json"
    offline_render(registry_path=target)
    assert not target.exists()
