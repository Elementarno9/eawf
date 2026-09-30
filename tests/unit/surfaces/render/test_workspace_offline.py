"""Unit tests: the offline workspace render emits the portfolio totals line.

The headless ``workspace registry-status`` frame (``offline_render``) folds
every registered repo's off-disk state into one totals line: the repo count,
the active-phase waves closed over total, the summed EU and the open-PR count.
An empty or unavailable registry still emits an honest ``Σ 0 repos`` line. Repo
codes are abstract placeholders (``ABC`` / ``DEF``), never real-looking project
names.
"""

from __future__ import annotations

from pathlib import Path

import orjson

from eawf.surfaces.tui.chassis.offline import TOTALS_ROW_LABEL, offline_render

#: The totals line of a registry whose repos report nothing.
EMPTY_TOTALS = "Σ 0 repos  waves 0/0  EU —  PR —"


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
# The offline frame carries the portfolio totals
# --------------------------------------------------------------------------


def test_offline_render_sums_every_repo_into_one_totals_line(tmp_path: Path) -> None:
    """Two repos fold their active-phase waves and EU into one line."""
    repo_a = tmp_path / "abc"
    repo_b = tmp_path / "def"
    _write_repo_state(repo_a, done=3, total=6, eu=4.0)
    _write_repo_state(repo_b, done=1, total=4, eu=2.0)
    registry_path = _write_registry(tmp_path, {"ABC": str(repo_a), "DEF": str(repo_b)})

    frame = offline_render(registry_path=registry_path, width=200)

    assert _totals_line_in(frame) == "Σ 2 repos  waves 4/10  EU 6/6  PR —"


def test_offline_render_counts_only_the_active_phase(tmp_path: Path) -> None:
    """A wave under a closed phase stays out of the active-phase totals."""
    repo = tmp_path / "abc"
    _write_repo_state(repo, done=1, total=2, eu=1.5)
    state_path = repo / ".ea" / "state.json"
    payload = orjson.loads(state_path.read_bytes())
    payload["phases"]["P00"] = {"id": "P00", "status": "closed"}
    payload["iters"]["P00-I01"] = {"id": "P00-I01", "phase_id": "P00", "status": "closed"}
    payload["waves"]["old"] = {"iter_id": "P00-I01", "status": "closed"}
    state_path.write_bytes(orjson.dumps(payload))
    registry_path = _write_registry(tmp_path, {"ABC": str(repo)})

    frame = offline_render(registry_path=registry_path, width=200)

    assert _totals_line_in(frame) == "Σ 1 repos  waves 1/2  EU 1.5/1.5  PR —"


# --------------------------------------------------------------------------
# Boundary / error paths -- empty + unavailable registry
# --------------------------------------------------------------------------


def test_offline_render_empty_registry_zero_totals(tmp_path: Path) -> None:
    """An empty registry still emits an honest ``Σ 0 repos`` totals line."""
    registry_path = _write_registry(tmp_path, {})
    frame = offline_render(registry_path=registry_path, width=200)
    assert _totals_line_in(frame) == EMPTY_TOTALS


def test_offline_render_missing_registry_zero_totals(tmp_path: Path) -> None:
    """A missing registry file degrades to the zero-valued totals line."""
    missing = tmp_path / "absent" / "registry.json"
    frame = offline_render(registry_path=missing, width=200)
    assert _totals_line_in(frame) == EMPTY_TOTALS


def test_offline_render_repo_without_state_counts_zero(tmp_path: Path) -> None:
    """A registered repo with no on-disk state contributes zero to the totals."""
    repo_a = tmp_path / "abc"
    repo_a.mkdir()  # no .ea/state.json
    registry_path = _write_registry(tmp_path, {"ABC": str(repo_a)})
    frame = offline_render(registry_path=registry_path, width=200)
    assert _totals_line_in(frame) == "Σ 1 repos  waves 0/0  EU —  PR —"


def test_offline_render_repo_with_malformed_state_counts_zero(tmp_path: Path) -> None:
    """A repo whose state carries no phase table folds to zero, never raises."""
    repo_a = tmp_path / "abc"
    (repo_a / ".ea").mkdir(parents=True)
    (repo_a / ".ea" / "state.json").write_bytes(orjson.dumps({"phases": [], "estimates": 3}))
    registry_path = _write_registry(tmp_path, {"ABC": str(repo_a)})
    frame = offline_render(registry_path=registry_path, width=200)
    assert _totals_line_in(frame) == "Σ 1 repos  waves 0/0  EU —  PR —"


def test_offline_render_has_no_keymap_line(tmp_path: Path) -> None:
    """The headless frame advertises no keys: nothing reads a keypress there."""
    frame = offline_render(registry_path=_write_registry(tmp_path, {}), width=200)
    assert "keymap" not in frame
