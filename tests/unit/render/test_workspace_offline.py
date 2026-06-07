"""Unit tests: the offline workspace render emits the live totals layout.

The headless ``workspace registry-status`` frame (``offline_render``) must
emit the same totals-row layout as the live workspace table when no daemon
is reachable. Both surfaces fold every registered repo's off-disk state
through the shared
:func:`~eawf.surfaces.tui.widgets.workspace_table.portfolio_totals` reducer and
the shared
:func:`~eawf.surfaces.tui.widgets.workspace_table.format_totals_line` formatter,
so the offline frame's totals line is byte-identical to the formatter the
live render uses. These tests pin that contract: the offline frame carries
the totals line, the line matches the shared formatter over the same
registry, and an empty / unavailable registry still emits an honest
``Σ 0 repos`` totals line. Repo codes are abstract placeholders (``ABC`` /
``DEF``), never real-looking project names.
"""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest

from eawf.surfaces.tui.offline import offline_render
from eawf.surfaces.tui.widgets.workspace_table import (
    TOTALS_ROW_LABEL,
    format_totals_line,
    portfolio_totals,
    repo_row_from_path,
)


def _write_registry(home: Path, repos: dict[str, str]) -> Path:
    """Write a minimal ``~/.eawf/registry.json`` under *home*.

    Args:
        home: The ``home`` seam root; the registry lands at
            ``<home>/.eawf/registry.json``.
        repos: Mapping of repo code to absolute on-disk path.

    Returns:
        The registry-file path.
    """
    registry_dir = home / ".eawf"
    registry_dir.mkdir(parents=True, exist_ok=True)
    path = registry_dir / "registry.json"
    payload = {
        "active_code": next(iter(repos), None),
        "repos": {code: {"code": code, "path": repo_path} for code, repo_path in repos.items()},
    }
    path.write_bytes(orjson.dumps(payload))
    return path


def _write_repo_state(repo_root: Path, *, done: int, total: int, eu: float) -> None:
    """Write a per-repo ``state.json`` with a one-active-phase wave ledger.

    Args:
        repo_root: The repo working-tree root; state lands at
            ``<repo_root>/.ea/state.json``.
        done: Closed-wave count for the active phase.
        total: Total-wave count for the active phase.
        eu: EU estimate + actual recorded on a single summary row.
    """
    ea_dir = repo_root / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    waves = {
        f"W{i}": {"iter_id": "P01-I01", "status": "closed" if i < done else "pending"}
        for i in range(total)
    }
    payload = {
        "current": {"phase_id": "P01"},
        "phases": {"P01": {"id": "P01", "status": "active"}},
        "iters": {"P01-I01": {"id": "P01-I01", "phase_id": "P01", "status": "active"}},
        "waves": waves,
        "estimates": {"e1": {"expected_eu": eu}},
        "actuals": {"a1": {"elapsed_eu": eu}},
    }
    (ea_dir / "state.json").write_bytes(orjson.dumps(payload))


def _totals_line_in(frame: str) -> str:
    """Return the totals line (starting with the sigma label) from *frame*."""
    for line in frame.splitlines():
        if line.startswith(TOTALS_ROW_LABEL):
            return line
    raise AssertionError(f"no totals line in offline frame: {frame!r}")


# --------------------------------------------------------------------------
# The offline frame carries the live totals layout
# --------------------------------------------------------------------------


def test_offline_render_emits_totals_line(tmp_path: Path) -> None:
    """A populated registry's offline frame carries the shared totals line."""
    repo_a = tmp_path / "abc"
    repo_b = tmp_path / "def"
    _write_repo_state(repo_a, done=3, total=6, eu=4.0)
    _write_repo_state(repo_b, done=1, total=4, eu=2.0)
    registry_path = _write_registry(tmp_path, {"ABC": str(repo_a), "DEF": str(repo_b)})

    frame = offline_render(registry_path=registry_path, width=200)

    expected_rows = [
        repo_row_from_path("ABC", str(repo_a)),
        repo_row_from_path("DEF", str(repo_b)),
    ]
    expected_line = format_totals_line(portfolio_totals(expected_rows))
    assert _totals_line_in(frame) == expected_line


def test_offline_totals_sums_match_live_reducer(tmp_path: Path) -> None:
    """The offline totals line reports the same sums the live reducer folds."""
    repo_a = tmp_path / "abc"
    repo_b = tmp_path / "def"
    _write_repo_state(repo_a, done=3, total=6, eu=4.0)
    _write_repo_state(repo_b, done=1, total=4, eu=2.0)
    registry_path = _write_registry(tmp_path, {"ABC": str(repo_a), "DEF": str(repo_b)})

    frame = offline_render(registry_path=registry_path, width=200)
    line = _totals_line_in(frame)

    assert "2 repos" in line
    assert "waves 4/10" in line
    assert "EU 6/6" in line
    assert "PR —" in line


# --------------------------------------------------------------------------
# Boundary / error paths -- empty + unavailable registry
# --------------------------------------------------------------------------


def test_offline_render_empty_registry_zero_totals(tmp_path: Path) -> None:
    """An empty registry still emits an honest ``Σ 0 repos`` totals line."""
    registry_path = _write_registry(tmp_path, {})
    frame = offline_render(registry_path=registry_path, width=200)
    line = _totals_line_in(frame)
    assert line == format_totals_line(portfolio_totals([]))
    assert "0 repos" in line


def test_offline_render_missing_registry_zero_totals(tmp_path: Path) -> None:
    """A missing registry file degrades to the zero-valued totals line."""
    missing = tmp_path / "absent" / "registry.json"
    frame = offline_render(registry_path=missing, width=200)
    line = _totals_line_in(frame)
    assert line == format_totals_line(portfolio_totals([]))
    assert "0 repos" in line


def test_offline_render_repo_without_state_counts_zero(tmp_path: Path) -> None:
    """A registered repo with no on-disk state contributes zero to the totals."""
    repo_a = tmp_path / "abc"
    repo_a.mkdir()  # no .ea/state.json
    registry_path = _write_registry(tmp_path, {"ABC": str(repo_a)})
    frame = offline_render(registry_path=registry_path, width=200)
    line = _totals_line_in(frame)
    assert "1 repos" in line
    assert "waves 0/0" in line
    assert pytest.approx(0.0) == portfolio_totals([repo_row_from_path("ABC", str(repo_a))]).eu_total
