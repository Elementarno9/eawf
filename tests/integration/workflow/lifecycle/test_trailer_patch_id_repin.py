"""Wave pins survive a rewritten ``main`` by trailer, with patch-id breaking ties.

Every test builds a scratch repository whose phase branch was replayed onto a
moved ``main`` the way a hosted rebase merge does it, so every old pin names a
commit that is no longer on first-parent ``main``. The branch carries a batch
commit naming two waves and a later commit that names ``W01`` a second time,
which is the case a trailer-only matcher cannot decide.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from eawf.workflow.lifecycle import wave_trailer_repin
from eawf.workflow.lifecycle.wave_trailer_repin import (
    TrailerRepin,
    TrailerRepinError,
    patch_id,
    resolve_trailer_repins,
)

W01 = "P34-I01-W01"
W02 = "P34-I01-W02"
W03 = "P34-I01-W03"
W04 = "P34-I01-W04"


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True, timeout=30
    )
    return out.stdout.strip()


def _commit(root: Path, *, name: str, subject: str, waves: tuple[str, ...]) -> str:
    (root / name).write_text(f"{name}\n", encoding="utf-8")
    _git(root, "add", name)
    trailers = "\n".join(f"Eawf-Wave: {wave}" for wave in waves)
    message = f"{subject}\n\nBody.\n\n{trailers}\n" if waves else f"{subject}\n"
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    _commit(root, name="root.txt", subject="chore: root", waves=())
    return root


def _phase_branch(root: Path) -> dict[str, str]:
    """Commit the phase branch and return each wave's pre-rewrite pin."""
    _git(root, "switch", "-q", "-c", "feature/zz-v1.0", "main")
    c1 = _commit(root, name="a.txt", subject="feat: add a", waves=(W01,))
    c2 = _commit(root, name="b.txt", subject="feat: add b", waves=(W02, W03))
    c3 = _commit(root, name="c.txt", subject="fix: add c", waves=(W01, W04))
    _git(root, "switch", "-q", "main")
    return {W01: c1, W02: c2, W03: c2, W04: c3}


def _rebase_merge(root: Path, pins: dict[str, str]) -> list[str]:
    """Replay the branch onto a moved ``main``; return the new commits, oldest first."""
    _commit(root, name="other.txt", subject="chore: other phase", waves=())
    for old in dict.fromkeys(pins.values()):
        _git(root, "cherry-pick", old)
    return _git(root, "rev-list", "--reverse", "-3", "main").split()


def _assert_every_wave_repinned(rows: list[TrailerRepin], expected: dict[str, str]) -> None:
    moved = {row.wave_id: row.new_commit for row in rows}
    assert moved == expected, f"repin mismatch: {rows}"


def test_resolve_trailer_repins_maps_every_wave_after_rebase_merge(scratch: Path) -> None:
    pins = _phase_branch(scratch)
    new_c1, new_c2, new_c3 = _rebase_merge(scratch, pins)

    rows = resolve_trailer_repins(pins, repo_root=scratch)

    _assert_every_wave_repinned(rows, {W01: new_c1, W02: new_c2, W03: new_c2, W04: new_c3})
    outcomes = {row.wave_id: row.outcome for row in rows}
    assert outcomes == {
        W01: "patch_id",
        W02: "unique_trailer",
        W03: "unique_trailer",
        W04: "unique_trailer",
    }
    assert [row.old_commit for row in rows] == [pins[w] for w in (W01, W02, W03, W04)]


def test_resolve_trailer_repins_without_patch_id_reds_on_duplicated_trailer(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = _phase_branch(scratch)
    new_c1, new_c2, new_c3 = _rebase_merge(scratch, pins)
    monkeypatch.setattr(wave_trailer_repin, "patch_id", lambda *_a, **_kw: None)

    rows = resolve_trailer_repins(pins, repo_root=scratch)

    w01 = next(row for row in rows if row.wave_id == W01)
    assert w01.outcome == "ambiguous"
    # A newest-trailer-wins matcher would take the later commit that merely
    # re-names W01, which is the wrong wave commit.
    assert w01.candidates == (new_c3, new_c1)
    with pytest.raises(AssertionError, match="repin mismatch"):
        _assert_every_wave_repinned(rows, {W01: new_c1, W02: new_c2, W03: new_c2, W04: new_c3})


def test_resolve_trailer_repins_fast_forward_leaves_pins_unchanged(scratch: Path) -> None:
    pins = _phase_branch(scratch)
    _git(scratch, "merge", "-q", "--ff-only", "feature/zz-v1.0")

    rows = resolve_trailer_repins(pins, repo_root=scratch)

    assert {row.outcome for row in rows} == {"unchanged"}
    _assert_every_wave_repinned(rows, pins)


def test_resolve_trailer_repins_reports_unresolved_and_ambiguous(scratch: Path) -> None:
    pins = _phase_branch(scratch)
    _rebase_merge(scratch, pins)
    missing = "f" * 40

    rows = resolve_trailer_repins(
        {W01: missing, "P34-I01-W09": missing},
        repo_root=scratch,
    )

    by_wave = {row.wave_id: row for row in rows}
    assert by_wave[W01].outcome == "ambiguous"
    assert by_wave[W01].new_commit is None
    assert by_wave["P34-I01-W09"].outcome == "unresolved"
    assert by_wave["P34-I01-W09"].candidates == ()


def test_resolve_trailer_repins_empty_pins(scratch: Path) -> None:
    assert resolve_trailer_repins({}, repo_root=scratch) == []


def test_resolve_trailer_repins_unknown_ref_raises(scratch: Path) -> None:
    with pytest.raises(TrailerRepinError, match="refs/heads/nope"):
        resolve_trailer_repins({W01: "f" * 40}, target_ref="refs/heads/nope", repo_root=scratch)


def test_patch_id_is_stable_across_replay_and_none_for_empty_commit(scratch: Path) -> None:
    pins = _phase_branch(scratch)
    new_c1, _new_c2, _new_c3 = _rebase_merge(scratch, pins)
    _git(scratch, "commit", "-q", "--allow-empty", "-m", "chore: empty")

    assert patch_id(pins[W01], repo_root=scratch) == patch_id(new_c1, repo_root=scratch)
    assert patch_id(pins[W01], repo_root=scratch) != patch_id(pins[W04], repo_root=scratch)
    assert patch_id("HEAD", repo_root=scratch) is None
    assert patch_id("f" * 40, repo_root=scratch) is None
