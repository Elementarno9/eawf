"""``commit_prefix_lint.py --pre-push-lint`` against real history.

Free-form scratch commits are refused, and the range a publish squashes
them into is accepted. A push is modelled by its two git steps, a soft
reset to the base and one commit per Task, then the trailing state commit.
The pure check is covered in ``tests/unit/test_commit_prefix_lint_trailers.py``.
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


_GENERATION_ID = "gen-0123456789abcdef"


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture()
def epoch2_scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A repo with an epoch-2 generation and three free-form commits on ``work``.

    The generation stays untracked until the trailing state commit, as the
    daemon's writes do between pushes. Returns the root and the base SHA.
    """
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
    base = _commit(root, "root.txt", "chore: root")
    _write_json(root / ".ea" / "epoch2-opt-in.json", {"opt_in": True})
    _write_json(
        root / ".ea" / "generations" / "EPOCH2_ACTIVE.json",
        {"epoch": 2, "generation_id": _GENERATION_ID},
    )
    _write_json(
        root / ".ea" / "generations" / _GENERATION_ID / "state.json",
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
    _git(root, "switch", "-q", "-c", "work")
    for name, subject in (("d.txt", "wip"), ("e.txt", "more stuff"), ("f.txt", "fix it")):
        _commit(root, name, subject)
    return root, base


def _publish(root: Path, base: str, *trailers: str) -> str:
    """Squash ``base..HEAD`` into one Task commit plus the trailing state commit."""
    _git(root, "reset", "-q", "--soft", base)
    _git(root, "commit", "-q", "-m", "feat: land the task\n\n" + "\n".join(trailers))
    _git(root, "add", ".ea")
    _git(root, "commit", "-q", "-m", "state: record the landed task")
    return _git(root, "rev-parse", "HEAD")


def test_pre_push_lint_refuses_scratch_then_accepts_the_published_range(
    epoch2_scratch: tuple[Path, str], lint: Any
) -> None:
    root, base = epoch2_scratch
    scratch_tip = _git(root, "rev-parse", "HEAD")

    code, diag = lint.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": scratch_tip}, repo_root=root
    )

    assert code == 1
    assert diag.count("subject is not '<type>: <summary>'") == 3
    assert "a 'Task: <KEY>' trailer per Task it delivers" in diag

    published = _publish(root, base, "Task: EAWF-0331")

    assert lint.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": published}, repo_root=root
    ) == (0, "")


@pytest.mark.parametrize(
    ("trailers", "expected"),
    [
        ((), "carries no Task trailers"),
        (("Task: EAWF-0331", "Task: EAWF-0332"), "carries 2 (EAWF-0331, EAWF-0332)"),
        (("Batch: BAT-0305", "Batch: BAT-0306", "Task: EAWF-0321"), "carries 2 (BAT-0305"),
        (("Batch: BAT-0306", "Task: EAWF-0321"), "belongs to Batch 'BAT-0305'"),
        (
            ("Batch: BAT-0306", "Task: EAWF-0331", "Task: EAWF-0321"),
            "Task trailer 'EAWF-0321' belongs to Batch 'BAT-0305', not 'BAT-0306'",
        ),
    ],
    ids=["no-trailer", "two-tasks", "two-batches", "foreign-task", "foreign-second-task"],
)
def test_pre_push_lint_refuses_a_malformed_publish(
    epoch2_scratch: tuple[Path, str], lint: Any, trailers: tuple[str, ...], expected: str
) -> None:
    root, base = epoch2_scratch
    published = _publish(root, base, *trailers)

    code, diag = lint.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": published}, repo_root=root
    )

    assert code == 1
    assert expected in diag
    # The trailing state commit is not the one refused.
    assert diag.count("\n") == 2


def test_pre_push_lint_accepts_one_batch_commit_naming_each_of_its_tasks(
    epoch2_scratch: tuple[Path, str], lint: Any
) -> None:
    root, base = epoch2_scratch
    published = _publish(root, base, "Batch: BAT-0306", "Task: EAWF-0331", "Task: EAWF-0332")

    assert lint.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": published}, repo_root=root
    ) == (0, "")


def test_pre_push_lint_refuses_a_state_commit_carrying_source(
    epoch2_scratch: tuple[Path, str], lint: Any
) -> None:
    root, base = epoch2_scratch
    _git(root, "reset", "-q", "--soft", base)
    _git(root, "add", ".ea")
    _git(root, "commit", "-q", "-m", "state: everything at once")
    tip = _git(root, "rev-parse", "HEAD")

    code, diag = lint.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": tip}, repo_root=root
    )

    assert code == 1
    assert "state commit touches non-state paths: ['d.txt', 'e.txt', 'f.txt']" in diag


def test_pre_push_lint_boundaries(epoch2_scratch: tuple[Path, str], lint: Any) -> None:
    root, _base = epoch2_scratch
    tip = _git(root, "rev-parse", "HEAD")

    assert lint.check_pushed_commits({}, repo_root=root) == (0, "")
    assert lint.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": "0" * 40, "PRE_COMMIT_TO_REF": tip}, repo_root=root
    ) == (0, "")
    # An empty range adds nothing.
    assert lint.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": tip, "PRE_COMMIT_TO_REF": tip}, repo_root=root
    ) == (0, "")
    # A single scratch commit is refused on its own.
    code, diag = lint.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": f"{tip}~1", "PRE_COMMIT_TO_REF": tip}, repo_root=root
    )
    assert code == 1
    assert diag.count("subject is not") == 1
    code, diag = lint.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": "f" * 40, "PRE_COMMIT_TO_REF": tip}, repo_root=root
    )
    assert code == 1
    assert "cannot read the commits" in diag


def test_pre_push_lint_refuses_an_unreadable_generation(
    epoch2_scratch: tuple[Path, str], lint: Any
) -> None:
    root, base = epoch2_scratch
    _write_json(root / ".ea" / "generations" / _GENERATION_ID / "state.json", {"batch": []})
    tip = _git(root, "rev-parse", "HEAD")

    code, diag = lint.check_pushed_commits(
        {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": tip}, repo_root=root
    )

    assert code == 1
    assert "managed state decode failed" in diag


def test_main_pre_push_lint_uses_the_environment(
    epoch2_scratch: tuple[Path, str],
    lint: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, base = epoch2_scratch
    monkeypatch.chdir(root)
    monkeypatch.setenv("PRE_COMMIT_FROM_REF", base)
    monkeypatch.setenv("PRE_COMMIT_TO_REF", _git(root, "rev-parse", "HEAD"))

    assert lint.main(["lint", "--pre-push-lint"]) == 1
    assert "outside the per-Batch regime" in capsys.readouterr().err

    monkeypatch.setenv("PRE_COMMIT_TO_REF", _publish(root, base, "Task: EAWF-0331"))

    assert lint.main(["lint", "--pre-push-lint"]) == 0
