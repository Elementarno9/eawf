"""A git command run under the state document lock keeps a concurrent state write.

Each case runs real git in a scratch repository; a push goes to a bare repository
beside it, never over a network. The stash cases use a hook that
does what pre-commit does around its hooks: it saves the unstaged diff, checks
the tree out from the index, waits, and applies the diff again, checking the
tree out once more when the apply conflicts.
"""

from __future__ import annotations

import subprocess  # noqa: EAWF024 the criterion gates are pinned here; they run real git
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import portalocker
import pytest

from eawf.runtime.daemon.epoch2_root import (
    DOCUMENT_LOCK_NAME,
    Epoch2RootContext,
    RootIdentity,
)
from eawf.runtime.lock import portalock
from eawf.runtime.lock.portalock import LockTimeout
from eawf.runtime.vcs import document_hold
from eawf.runtime.vcs.document_hold import (
    DOCUMENT_HOLD_TIMEOUT_SECONDS,
    document_lock_target,
    run_git_holding_document_lock,
)
from tests._epoch2_helpers import lay_epoch2_tree
from tests._linked_worktree import git

#: The tracked state file as committed, as the daemon left it unstaged, and as a
#: daemon write in the hook window rewrites it.
_COMMITTED, _UNSTAGED, _WRITTEN = '{"v": 0}\n', '{"v": 1}\n', '{"v": 2}\n'

#: What the stash hook runs: pre-commit's stash, wait and restore, in brief.
_STASH_HOOK = """\
import pathlib, subprocess, sys, time
marker, patch = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
diff = subprocess.run(
    ["git", "diff", "--binary", "--no-ext-diff", "--no-color"], capture_output=True, check=True
).stdout
if diff:
    patch.write_bytes(diff)
    subprocess.run(["git", "checkout", "--", "."], check=True)
marker.touch()
time.sleep(1.0)
if diff:
    apply = ["git", "apply", "--whitespace=nowarn", str(patch)]
    if subprocess.run(apply, capture_output=True).returncode != 0:
        subprocess.run(["git", "checkout", "--", "."], check=True)
        subprocess.run(apply, check=True)
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a managed repository with a committed state file, as the working directory."""
    root = tmp_path / "repo"
    root.mkdir()
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    for name, value in {
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
        "GIT_AUTHOR_NAME": "ci",
        "GIT_AUTHOR_EMAIL": "ci@example.com",
        "GIT_COMMITTER_NAME": "ci",
        "GIT_COMMITTER_EMAIL": "ci@example.com",
        "PRE_COMMIT_HOME": str(tmp_path / "pre-commit-home"),
    }.items():
        monkeypatch.setenv(name, value)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".ea").mkdir()
    (root / ".ea" / "state.json").write_text(_COMMITTED, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "root")
    monkeypatch.chdir(root)
    return root


def _hook(repo: Path, body: str, name: str = "pre-commit") -> None:
    hook = repo / ".git" / "hooks" / name
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    hook.chmod(0o755)


def _stage(repo: Path, name: str = "change.txt") -> None:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", name)


def _lock_is_free(repo: Path) -> bool:
    try:
        with portalock.acquire(document_lock_target(repo), timeout=0):
            return True
    except LockTimeout:
        return False


def _write_once_stashed(repo: Path, marker: Path, done: list[str]) -> None:
    """Write the state file under the document lock once the hook has stashed."""
    deadline = time.monotonic() + 30
    while not marker.exists():
        if time.monotonic() > deadline:
            done.append("the hook never stashed")
            return
        time.sleep(0.01)
    with portalock.acquire(document_lock_target(repo), timeout=30):
        (repo / ".ea" / "state.json").write_text(_WRITTEN, encoding="utf-8")
    done.append("written")


def _commit_beside_a_state_write(repo: Path, tmp_path: Path, commit: Callable[[], int]) -> str:
    """Commit while a writer rewrites the state file in the hook window; return the file."""
    marker, patch = tmp_path / "stashed", tmp_path / "stash.patch"
    script = tmp_path / "stash_hook.py"
    script.write_text(_STASH_HOOK, encoding="utf-8")
    _hook(repo, f'exec "{sys.executable}" "{script}" "{marker}" "{patch}"')
    (repo / ".ea" / "state.json").write_text(_UNSTAGED, encoding="utf-8")
    _stage(repo)
    done: list[str] = []
    writer = threading.Thread(target=_write_once_stashed, args=(repo, marker, done))
    writer.start()
    status = commit()
    writer.join(timeout=60)
    assert status == 0
    assert done == ["written"]
    return (repo / ".ea" / "state.json").read_text(encoding="utf-8")


def test_a_state_write_during_the_hook_stash_survives_the_wrapped_commit(
    repo: Path, tmp_path: Path
) -> None:
    final = _commit_beside_a_state_write(
        repo, tmp_path, lambda: run_git_holding_document_lock(repo, ["commit", "-q", "-m", "x"])
    )
    assert final == _WRITTEN
    assert git(repo, "log", "--format=%s", "-1").strip() == "x"


def test_a_state_write_during_the_hook_stash_is_lost_without_the_wrapper(
    repo: Path, tmp_path: Path
) -> None:
    final = _commit_beside_a_state_write(
        repo, tmp_path, lambda: subprocess.run(["git", "commit", "-q", "-m", "x"]).returncode
    )
    assert final == _UNSTAGED, "the control must reproduce the loss the wrapper prevents"


def _origin(repo: Path, tmp_path: Path) -> Path:
    """Add a local bare repository as ``origin``, seed it with ``main``, and return it."""
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    return bare


def _push_beside_a_state_write(repo: Path, tmp_path: Path, push: Callable[[], int]) -> str:
    """Push a new commit while a writer rewrites the state file in the pre-push hook window."""
    marker, patch = tmp_path / "stashed", tmp_path / "stash.patch"
    marker.unlink(missing_ok=True)
    script = tmp_path / "stash_hook.py"
    script.write_text(_STASH_HOOK, encoding="utf-8")
    _hook(repo, f'exec "{sys.executable}" "{script}" "{marker}" "{patch}"', name="pre-push")
    # Each call pushes a commit of its own, so git has a ref to update and runs the hook.
    commits = len(git(repo, "rev-list", "HEAD").split())
    _stage(repo, f"change-{commits}.txt")
    git(repo, "commit", "-q", "-m", "pushed")
    (repo / ".ea" / "state.json").write_text(_UNSTAGED, encoding="utf-8")
    done: list[str] = []
    writer = threading.Thread(target=_write_once_stashed, args=(repo, marker, done))
    writer.start()
    status = push()
    writer.join(timeout=60)
    assert status == 0
    assert done == ["written"]
    return (repo / ".ea" / "state.json").read_text(encoding="utf-8")


def test_a_state_write_during_a_wrapped_push_survives(repo: Path, tmp_path: Path) -> None:
    bare = _origin(repo, tmp_path)
    # The control first: a plain push loses the write the wrapper keeps.
    plain = _push_beside_a_state_write(
        repo, tmp_path, lambda: subprocess.run(["git", "push", "-q", "origin", "main"]).returncode
    )
    assert plain == _UNSTAGED, "the control must reproduce the loss the wrapper prevents"
    wrapped = _push_beside_a_state_write(
        repo,
        tmp_path,
        lambda: run_git_holding_document_lock(repo, ["push", "-q", "origin", "main"]),
    )
    assert wrapped == _WRITTEN
    assert git(bare, "rev-parse", "main") == git(repo, "rev-parse", "HEAD")


def test_a_wrapped_push_with_a_system_pre_push_hook_never_times_out(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay_epoch2_tree(repo)
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: eawf-lint\n"
        "        name: eawf lint\n"
        f"        entry: {sys.executable} -m eawf hook email-leak-lint\n"
        "        language: system\n"
        "        pass_filenames: false\n"
        "        always_run: true\n"
        "        stages: [pre-push]\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tree")
    bare = _origin(repo, tmp_path)
    subprocess.run(
        [sys.executable, "-m", "pre_commit", "install", "--hook-type", "pre-push"], check=True
    )
    # A hook that took the document lock would wait out the hold and time out.
    monkeypatch.setenv("EA_LOCK_TIMEOUT", "2.0")
    _stage(repo)
    git(repo, "commit", "-q", "-m", "x")
    # An unstaged change makes pre-commit stash, as it does in a daemon-managed checkout.
    (repo / ".ea" / "state.json").write_text(_UNSTAGED, encoding="utf-8")
    # A hang raises TimeoutExpired; the ceiling never bounds a push that finished.
    result = subprocess.run(
        [sys.executable, "-c", _WRAPPED_PUSH, str(repo)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "LockTimeout" not in output
    assert "eawf lint" in output and "Passed" in output, output
    assert git(bare, "rev-parse", "main") == git(repo, "rev-parse", "HEAD")
    assert (repo / ".ea" / "state.json").read_text(encoding="utf-8") == _UNSTAGED


#: Runs the wrapper in its own process, as ``eawf git push`` does, with the hook
#: output visible to the test.
_WRAPPED_PUSH = """\
import sys
from pathlib import Path
from eawf.runtime.vcs.document_hold import run_git_holding_document_lock
sys.exit(run_git_holding_document_lock(Path(sys.argv[1]), ["push", "origin", "main"]))
"""


class _InterruptedProcess:
    """A git process whose first wait is interrupted and whose second wait sees it exit."""

    waits = 0

    def __init__(self, command: list[str], **_kwargs: object) -> None:
        self.returncode: int | None = None

    def wait(self) -> int:
        type(self).waits += 1
        if type(self).waits == 1:
            raise KeyboardInterrupt
        self.returncode = 130
        return 130


@pytest.mark.parametrize(
    ("case", "git_args", "expected"),
    [
        ("commits", ["commit", "-q", "-m", "x"], 0),
        ("nothing-to-commit", ["commit", "-q", "-m", "x"], 1),
        ("failing-hook", ["commit", "-q", "-m", "x"], 1),
        ("usage-error", ["commit", "--no-such-option"], 129),
        ("no-arguments", [], 1),
        ("single-argument", ["--version"], 0),
        ("git-missing", ["commit", "-q", "-m", "x"], FileNotFoundError),
        ("interrupt", ["commit", "-q", "-m", "x"], KeyboardInterrupt),
    ],
)
def test_the_exit_status_passes_through_and_the_lock_is_released(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    git_args: list[str],
    expected: int | type[BaseException],
) -> None:
    if case in {"commits", "failing-hook"}:
        _stage(repo)
    if case == "failing-hook":
        _hook(repo, "exit 7")
    if case == "git-missing":
        monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    if case == "interrupt":
        _InterruptedProcess.waits = 0
        monkeypatch.setattr(subprocess, "Popen", _InterruptedProcess)
    if isinstance(expected, int):
        assert run_git_holding_document_lock(repo, git_args) == expected
    else:
        with pytest.raises(expected):
            run_git_holding_document_lock(repo, git_args)
    if case == "interrupt":
        assert _InterruptedProcess.waits == 2, "the interrupt must still wait for git to exit"
    assert _lock_is_free(repo)


def test_a_repository_without_an_eawf_tree_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(FileNotFoundError, match=r"holds no \.ea tree"):
        run_git_holding_document_lock(plain, ["status"])
    assert not (plain / ".ea").exists()


def test_a_held_lock_times_out_before_git_starts(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert pytest.approx(120.0) == DOCUMENT_HOLD_TIMEOUT_SECONDS
    monkeypatch.setattr(document_hold, "DOCUMENT_HOLD_TIMEOUT_SECONDS", 0.1)
    _stage(repo)
    with portalock.acquire(document_lock_target(repo), timeout=0), pytest.raises(LockTimeout):
        run_git_holding_document_lock(repo, ["commit", "-q", "-m", "x"])
    assert git(repo, "log", "--format=%s", "-1").strip() == "root", "git must not have run"


def test_a_wrapped_commit_with_a_system_hook_never_times_out(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay_epoch2_tree(repo)
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: eawf-lint\n"
        "        name: eawf lint\n"
        f"        entry: {sys.executable} -m eawf hook email-leak-lint\n"
        "        language: system\n"
        "        pass_filenames: false\n"
        "        stages: [pre-commit]\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tree")
    subprocess.run([sys.executable, "-m", "pre_commit", "install"], check=True)
    # A hook that took the document lock would wait out the hold and time out.
    monkeypatch.setenv("EA_LOCK_TIMEOUT", "2.0")
    _stage(repo)
    # A hang raises TimeoutExpired; the ceiling never bounds a commit that finished.
    result = subprocess.run(
        [sys.executable, "-c", _WRAPPED_COMMIT, str(repo)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "LockTimeout" not in output
    assert "eawf lint" in output and "Passed" in output, output
    assert git(repo, "log", "--format=%s", "-1").strip() == "x"


#: Runs the wrapper in its own process, as ``eawf git commit`` does, with the
#: hook output visible to the test.
_WRAPPED_COMMIT = """\
import sys
from pathlib import Path
from eawf.runtime.vcs.document_hold import run_git_holding_document_lock
sys.exit(run_git_holding_document_lock(Path(sys.argv[1]), ["commit", "-m", "x"]))
"""


class _ProbingProcess:
    """A git process whose every wait first runs a probe, then is interrupted or exits.

    Attributes:
        probe: Called at each wait; returns whether the lock file was held then.
        interrupts: How many waits raise ``KeyboardInterrupt`` before one returns.
    """

    probe: ClassVar[Callable[[], bool]] = staticmethod(lambda: True)
    interrupts: ClassVar[int] = 0
    held: ClassVar[list[bool]] = []

    def __init__(self, command: list[str], **_kwargs: object) -> None:
        self.returncode: int | None = None

    def wait(self) -> int:
        cls = type(self)
        cls.held.append(cls.probe())
        if len(cls.held) <= cls.interrupts:
            raise KeyboardInterrupt
        self.returncode = 0
        return 0


def _held_elsewhere(lock_file: Path) -> bool:
    """Return whether another open of *lock_file* is refused its exclusive lock."""
    with lock_file.open("a+", encoding="utf-8") as handle:
        try:
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.LockException:
            return True
        portalocker.unlock(handle)
        return False


def test_the_wrapper_holds_the_lock_file_the_daemon_sessions_take(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay_epoch2_tree(repo)
    context = Epoch2RootContext(
        identity=RootIdentity.of(repo / ".ea"), daemon_wal_dir=tmp_path / "wal"
    )
    daemon_lock = context.lock_path(DOCUMENT_LOCK_NAME)
    daemon_lock.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        _ProbingProcess, "probe", staticmethod(lambda: _held_elsewhere(daemon_lock))
    )
    monkeypatch.setattr(_ProbingProcess, "interrupts", 0)
    monkeypatch.setattr(_ProbingProcess, "held", [])
    monkeypatch.setattr(subprocess, "Popen", _ProbingProcess)
    assert run_git_holding_document_lock(repo.resolve(), ["commit", "-m", "x"]) == 0
    assert _ProbingProcess.held == [True], "git must run while the daemon's lock file is held"
    assert not _held_elsewhere(daemon_lock)


def test_repeated_interrupts_keep_the_lock_until_git_exits(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_ProbingProcess, "probe", staticmethod(lambda: not _lock_is_free(repo)))
    monkeypatch.setattr(_ProbingProcess, "interrupts", 2)
    monkeypatch.setattr(_ProbingProcess, "held", [])
    monkeypatch.setattr(subprocess, "Popen", _ProbingProcess)
    with pytest.raises(KeyboardInterrupt):
        run_git_holding_document_lock(repo, ["commit", "-m", "x"])
    assert _ProbingProcess.held == [True, True, True], "every wait must run under the lock"
    assert _lock_is_free(repo)
