"""``commit_prefix_lint.py --check-rewrite`` and ``--pre-push`` against real history.

Each test squashes a scratch phase branch the way a phase-close squash would
and asks the guard whether the squash may replace the branch: it may only
when every wave the branch named by ``Eawf-Wave`` trailer is still named.
The pure comparison is covered in ``tests/unit/test_commit_prefix_lint_trailers.py``.

``--pre-push-lint`` runs over the same kind of scratch repository: free-form
scratch commits are refused, and the range a publish squashes them into is
accepted. ``eawf publish`` itself is modelled by its two git steps, a soft
reset to the base and one commit per Batch, then the trailing state commit.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_DIR = _REPO_ROOT / "tools"


def _load(name: str) -> Any:
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location(name, _TOOL_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def lint() -> Any:
    return _load("commit_prefix_lint")


@pytest.fixture()
def guard(lint: Any) -> Any:
    return _load("wave_trailer_guard")


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True, timeout=30
    )
    return out.stdout.strip()


def _commit(root: Path, name: str, message: str) -> str:
    (root / name).write_text(f"{name}\n", encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo whose ``phase`` branch holds W01, a W02+W03 batch, then W04."""
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
    _commit(root, "root.txt", "chore: root")
    _git(root, "switch", "-q", "-c", "phase")
    _commit(root, "a.txt", "feat: a\n\nEawf-Wave: P34-I01-W01")
    _commit(root, "b.txt", "feat: b\n\nEawf-Wave: P34-I01-W02\nEawf-Wave: P34-I01-W03")
    _commit(root, "c.txt", "feat: c\n\nEawf-Wave: P34-W04")
    return root


def _squash(root: Path, message: str) -> str:
    """Squash ``phase`` onto ``main`` as one commit on a new branch; return its tip."""
    _git(root, "switch", "-q", "-c", "squashed", "main")
    _git(root, "merge", "-q", "--squash", "phase")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# --check-rewrite against real history
# ---------------------------------------------------------------------------


def test_check_rewrite_accepts_a_fast_forward(scratch: Path, guard: Any) -> None:
    old = _git(scratch, "rev-parse", "phase~1")

    assert guard.check_rewrite(old, "phase", repo_root=scratch) == (0, "")


def test_check_rewrite_accepts_a_squash_keeping_every_trailer(scratch: Path, guard: Any) -> None:
    trailers = "\n".join(f"Eawf-Wave: P34-I01-W0{n}" for n in range(1, 5))
    new = _squash(scratch, f"feat: squash\n\n{trailers}")

    assert guard.check_rewrite("phase", new, repo_root=scratch) == (0, "")


def test_check_rewrite_refuses_a_squash_dropping_a_trailer(scratch: Path, guard: Any) -> None:
    new = _squash(scratch, "feat: squash\n\nEawf-Wave: P34-I01-W01\nEawf-Wave: P34-I01-W02")

    code, diag = guard.check_rewrite("phase", new, repo_root=scratch)

    assert code == 1
    assert "drops the Eawf-Wave trailer of P34-I01-W03, P34-I01-W04" in diag


def test_check_rewrite_refuses_an_unknown_tip(scratch: Path, guard: Any) -> None:
    code, diag = guard.check_rewrite("f" * 40, "phase", repo_root=scratch)

    assert code == 1
    assert "fetch the remote tip first" in diag


def test_check_pre_push_reads_the_pre_commit_tips(scratch: Path, guard: Any) -> None:
    old = _git(scratch, "rev-parse", "phase")
    new = _squash(scratch, "feat: squash\n\nEawf-Wave: P34-I01-W01")
    env = {"PRE_COMMIT_FROM_REF": old, "PRE_COMMIT_TO_REF": new}

    code, diag = guard.check_pre_push(env, repo_root=scratch)

    assert code == 1
    assert "P34-I01-W02" in diag


@pytest.mark.parametrize(
    "env",
    [{}, {"PRE_COMMIT_FROM_REF": "0" * 40, "PRE_COMMIT_TO_REF": "a" * 40}],
    ids=["no-variables", "new-ref"],
)
def test_check_pre_push_passes_without_history_to_replace(
    scratch: Path, guard: Any, env: dict[str, str]
) -> None:
    assert guard.check_pre_push(env, repo_root=scratch) == (0, "")


# ---------------------------------------------------------------------------
# The lint entry point dispatches both modes
# ---------------------------------------------------------------------------


def test_main_check_rewrite_exit_codes(
    scratch: Path,
    lint: Any,
    guard: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(scratch)
    new = _squash(scratch, "feat: squash\n\nEawf-Wave: P34-I01-W01")

    assert lint.main(["lint", "--check-rewrite", "phase", new]) == 1
    assert "P34-I01-W04" in capsys.readouterr().err
    assert lint.main(["lint", "--check-rewrite", "phase~1", "phase"]) == 0
    assert lint.main(["lint", "--check-rewrite", "phase"]) == 1
    assert "usage" in capsys.readouterr().err


def test_main_pre_push_uses_the_environment(
    scratch: Path, lint: Any, guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(scratch)
    old = _git(scratch, "rev-parse", "phase")
    new = _squash(scratch, "feat: squash\n\nEawf-Wave: P34-I01-W01")
    monkeypatch.setenv("PRE_COMMIT_FROM_REF", old)
    monkeypatch.setenv("PRE_COMMIT_TO_REF", new)

    assert lint.main(["lint", "--pre-push"]) == 1


# ---------------------------------------------------------------------------
# --pre-push-lint: scratch commits are refused until published as Batch commits
# ---------------------------------------------------------------------------

_GENERATION_ID = "gen-0123456789abcdef"


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture()
def epoch2_scratch(scratch: Path) -> tuple[Path, str]:
    """``scratch`` with an epoch-2 generation and three free-form commits on ``work``.

    The generation stays untracked until the trailing state commit, as the
    daemon's writes do between pushes. Returns the root and the base SHA.
    """
    _write_json(scratch / ".ea" / "epoch2-opt-in.json", {"opt_in": True})
    _write_json(
        scratch / ".ea" / "generations" / "EPOCH2_ACTIVE.json",
        {"epoch": 2, "generation_id": _GENERATION_ID},
    )
    _write_json(
        scratch / ".ea" / "generations" / _GENERATION_ID / "state.json",
        {
            "batch": {
                "BAT-0305": {"key": "BAT-0305", "status": "ACTIVE"},
                "BAT-0306": {"key": "BAT-0306", "status": "PLANNED"},
            },
            "task": {
                "EAWF-0331": {"batch_ref": "eawf://EAWF/EAWF/EAWF/batch/BAT-0306"},
                "EAWF-0332": {"batch_ref": "eawf://EAWF/EAWF/EAWF/batch/BAT-0306"},
                "EAWF-0321": {"batch_ref": "eawf://EAWF/EAWF/EAWF/batch/BAT-0305"},
            },
        },
    )
    base = _git(scratch, "rev-parse", "phase")
    _git(scratch, "switch", "-q", "-c", "work", "phase")
    for name, subject in (("d.txt", "wip"), ("e.txt", "more stuff"), ("f.txt", "fix it")):
        _commit(scratch, name, subject)
    return scratch, base


def _publish(root: Path, base: str, *trailers: str) -> str:
    """Squash ``base..HEAD`` into one Batch commit plus the trailing state commit."""
    _git(root, "reset", "-q", "--soft", base)
    _git(root, "commit", "-q", "-m", "feat: land the batch\n\n" + "\n".join(trailers))
    _git(root, "add", ".ea")
    _git(root, "commit", "-q", "-m", "state: record the published batch")
    return _git(root, "rev-parse", "HEAD")


def test_pre_push_lint_refuses_scratch_then_accepts_the_published_range(
    epoch2_scratch: tuple[Path, str], guard: Any
) -> None:
    root, base = epoch2_scratch
    scratch_tip = _git(root, "rev-parse", "HEAD")

    code, diag = guard.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": scratch_tip}, repo_root=root
    )

    assert code == 1
    assert diag.count("subject is not '<type>: <summary>'") == 3
    assert "eawf publish" in diag

    published = _publish(root, base, "Batch: BAT-0306", "Task: EAWF-0331", "Task: EAWF-0332")

    assert guard.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": published}, repo_root=root
    ) == (0, "")


@pytest.mark.parametrize(
    ("trailers", "expected"),
    [
        (("Task: EAWF-0331",), "carries no Batch trailers"),
        (("Batch: BAT-0305", "Batch: BAT-0306"), "carries 2 (BAT-0305, BAT-0306)"),
        (("Batch: BAT-0306", "Task: EAWF-0321"), "belongs to Batch 'BAT-0305'"),
    ],
    ids=["no-batch", "two-batches", "foreign-task"],
)
def test_pre_push_lint_refuses_a_malformed_publish(
    epoch2_scratch: tuple[Path, str], guard: Any, trailers: tuple[str, ...], expected: str
) -> None:
    root, base = epoch2_scratch
    published = _publish(root, base, *trailers)

    code, diag = guard.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": published}, repo_root=root
    )

    assert code == 1
    assert expected in diag
    # The trailing state commit is not the one refused.
    assert diag.count("\n") == 2


def test_pre_push_lint_refuses_a_state_commit_carrying_source(
    epoch2_scratch: tuple[Path, str], guard: Any
) -> None:
    root, base = epoch2_scratch
    _git(root, "reset", "-q", "--soft", base)
    _git(root, "add", ".ea")
    _git(root, "commit", "-q", "-m", "state: everything at once")
    tip = _git(root, "rev-parse", "HEAD")

    code, diag = guard.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": tip}, repo_root=root
    )

    assert code == 1
    assert "state commit touches non-state paths: ['d.txt', 'e.txt', 'f.txt']" in diag


def test_pre_push_lint_boundaries(epoch2_scratch: tuple[Path, str], guard: Any) -> None:
    root, _base = epoch2_scratch
    tip = _git(root, "rev-parse", "HEAD")

    assert guard.check_pushed_commits({}, repo_root=root) == (0, "")
    assert guard.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": "0" * 40, "PRE_COMMIT_TO_REF": tip}, repo_root=root
    ) == (0, "")
    # An empty range adds nothing.
    assert guard.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": tip, "PRE_COMMIT_TO_REF": tip}, repo_root=root
    ) == (0, "")
    # A single scratch commit is refused on its own.
    code, diag = guard.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": f"{tip}~1", "PRE_COMMIT_TO_REF": tip}, repo_root=root
    )
    assert code == 1
    assert diag.count("subject is not") == 1
    code, diag = guard.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": "f" * 40, "PRE_COMMIT_TO_REF": tip}, repo_root=root
    )
    assert code == 1
    assert "cannot read the commits" in diag


def test_pre_push_lint_refuses_an_unreadable_generation(
    epoch2_scratch: tuple[Path, str], guard: Any
) -> None:
    root, base = epoch2_scratch
    _write_json(root / ".ea" / "generations" / _GENERATION_ID / "state.json", {"batch": []})
    tip = _git(root, "rev-parse", "HEAD")

    code, diag = guard.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": tip}, repo_root=root
    )

    assert code == 1
    assert "managed state decode failed" in diag


def test_main_pre_push_lint_uses_the_environment(
    epoch2_scratch: tuple[Path, str],
    lint: Any,
    guard: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, base = epoch2_scratch
    monkeypatch.chdir(root)
    monkeypatch.setenv("PRE_COMMIT_FROM_REF", base)
    monkeypatch.setenv("PRE_COMMIT_TO_REF", _git(root, "rev-parse", "HEAD"))

    assert lint.main(["lint", "--pre-push-lint"]) == 1
    assert "outside the Batch regime" in capsys.readouterr().err

    monkeypatch.setenv("PRE_COMMIT_TO_REF", _publish(root, base, "Batch: BAT-0306"))

    assert lint.main(["lint", "--pre-push-lint"]) == 0
