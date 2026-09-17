"""A synthesized BLOCKED report keeps the files the spawn changed.

When a headless spawn answers in prose, the assist loop exhausts its re-asks
and the daemon synthesizes a typed BLOCKED body so the dispatch still
completes. That body used to record ``files_changed=[]`` even when the spawn
had edited files, which hides from the reviewer of a blocked wave exactly the
diff they need first.

These tests drive
:func:`~eawf.runtime.daemon.methods.agent._synthesize_role_report` against real
git working trees: a tracked edit, a staged add, an untracked file, an ignored
file, a clean tree, a repository with no first commit, and two directories that
are not git work trees at all. The degrade contract (BLOCKED verdict, LOW
confidence, synthesized marker) is re-pinned alongside, so filling the list can
never be traded for a green close.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    ReportSource,
)
from eawf.kernel.store.kinds.agent_report import AuditorReportBody, ExecutorReportBody
from eawf.runtime.daemon.methods.agent import _synthesize_role_report
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.dispatch.llm_assist import LLMAssistError, SchemaAttemptFailure

_T0 = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
_T1 = datetime(2026, 9, 17, 12, 5, tzinfo=UTC)
_WAVE = "P01-I01-W01"
_PHASE = "P01"


def _git(repo: Path, *args: str) -> str:
    """Run one git command inside *repo* and return its stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def _repo(root: Path, *, commit: bool = True) -> Path:
    """Return an initialised git work tree under *root*.

    Args:
        root: The directory the checkout is created inside.
        commit: Whether to land a first commit (``False`` leaves HEAD unborn).

    Returns:
        The path of the initialised checkout.
    """
    repo = root / "checkout"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch", "main")
    _git(repo, "config", "user.name", "EAWF Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "commit.gpgSign", "false")
    _git(repo, "config", "core.hooksPath", ".git/hooks")
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    if commit:
        _git(repo, "add", "--all")
        _git(repo, "commit", "--quiet", "--message", "chore: seed the checkout")
    return repo


def _spawn() -> SpawnResult:
    """Build the completed spawn whose output failed report-body validation."""
    return SpawnResult(
        session_id="sess-synth-1",
        runtime="claude-code",
        model="claude-opus-4-8",
        subprocess_pid=4242,
        exit_status=0,
        text="I finished the wave, trust me.",
        input_tokens=100,
        output_tokens=42,
        started_at=_T0,
        ended_at=_T1,
    )


def _exhausted(*, failures: int = 1) -> LLMAssistError:
    """Build the exhausted-assist error the synth path degrades from."""
    return LLMAssistError(
        attempts=failures,
        failures=[
            SchemaAttemptFailure(
                attempt=n + 1,
                reason="schema_mismatch",
                detail="verdict: field required",
            )
            for n in range(failures)
        ],
    )


def _executor_body(working_dir: Path, *, failures: int = 1) -> ExecutorReportBody:
    """Synthesize the executor body for a spawn that ran in *working_dir*."""
    body = _synthesize_role_report(
        _spawn(),
        wave_id=_WAVE,
        role=AgentSessionRole.EXECUTOR,
        phase_id=_PHASE,
        exc=_exhausted(failures=failures),
        working_dir=working_dir,
    )
    assert isinstance(body, ExecutorReportBody)
    return body


# --------------------------------------------------------------------------
# The file list is scanned off the spawn's working tree
# --------------------------------------------------------------------------


def test_synthesized_report_lists_a_tracked_edit(tmp_path: Path) -> None:
    """A file the spawn modified is named in the synthesized file list."""
    repo = _repo(tmp_path)
    (repo / "src" / "module.py").write_text("value = 2\n", encoding="utf-8")

    body = _executor_body(repo)

    assert body.files_changed == ["src/module.py"]


def test_synthesized_report_lists_a_staged_add(tmp_path: Path) -> None:
    """A file the spawn staged still counts as changed (the diff is HEAD-relative)."""
    repo = _repo(tmp_path)
    (repo / "src" / "added.py").write_text("added = True\n", encoding="utf-8")
    _git(repo, "add", "src/added.py")

    body = _executor_body(repo)

    assert body.files_changed == ["src/added.py"]


def test_synthesized_report_lists_untracked_files(tmp_path: Path) -> None:
    """An untracked file the spawn wrote is named beside the tracked edits."""
    repo = _repo(tmp_path)
    (repo / "src" / "module.py").write_text("value = 3\n", encoding="utf-8")
    (repo / "notes.md").write_text("scratch\n", encoding="utf-8")

    body = _executor_body(repo)

    assert body.files_changed == ["notes.md", "src/module.py"]


def test_synthesized_report_skips_ignored_paths(tmp_path: Path) -> None:
    """An ignored path is not the spawn's work, so it stays out of the list."""
    repo = _repo(tmp_path)
    (repo / "ignored").mkdir()
    (repo / "ignored" / "junk.log").write_text("noise\n", encoding="utf-8")

    body = _executor_body(repo)

    assert body.files_changed == []


def test_synthesized_report_clean_tree_is_empty(tmp_path: Path) -> None:
    """A spawn that changed nothing reports nothing (the empty boundary)."""
    body = _executor_body(_repo(tmp_path))

    assert body.files_changed == []


def test_synthesized_report_without_a_first_commit_lists_the_files(tmp_path: Path) -> None:
    """An unborn HEAD falls back to the index-relative diff plus untracked files."""
    repo = _repo(tmp_path, commit=False)

    body = _executor_body(repo)

    assert body.files_changed == [".gitignore", "src/module.py"]


def test_synthesized_report_outside_a_git_work_tree_is_empty(tmp_path: Path) -> None:
    """A working directory outside a work tree yields an empty list, not a guess."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "edited.py").write_text("value = 1\n", encoding="utf-8")

    body = _executor_body(plain)

    assert body.files_changed == []


def test_synthesized_report_missing_working_dir_is_empty(tmp_path: Path) -> None:
    """A working directory that no longer exists degrades instead of raising."""
    body = _executor_body(tmp_path / "gone")

    assert body.files_changed == []


def test_synthesized_report_paths_stay_repo_relative(tmp_path: Path) -> None:
    """Every listed path is repo-relative, so the report-store scrub accepts it."""
    repo = _repo(tmp_path)
    (repo / "src" / "module.py").write_text("value = 4\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("scratch\n", encoding="utf-8")

    body = _executor_body(repo)

    assert body.files_changed
    assert all(not path.startswith("/") for path in body.files_changed)
    assert str(tmp_path) not in " ".join(body.files_changed)


# --------------------------------------------------------------------------
# The degrade contract is unchanged by the file list
# --------------------------------------------------------------------------


def test_synthesized_report_keeps_the_blocked_degrade(tmp_path: Path) -> None:
    """Filling the file list never turns the synthesized body into a pass."""
    repo = _repo(tmp_path)
    (repo / "src" / "module.py").write_text("value = 5\n", encoding="utf-8")

    body = _executor_body(repo)

    assert body.verdict is AgentReportVerdict.BLOCKED
    assert body.confidence is Confidence.LOW
    assert body.report_source is ReportSource.SYNTHESIZED
    assert body.tests_run == []
    assert body.files_changed == ["src/module.py"]


def test_synthesized_report_without_failures_still_scans(tmp_path: Path) -> None:
    """An empty rejection trail still yields the scanned file list."""
    repo = _repo(tmp_path)
    (repo / "src" / "module.py").write_text("value = 6\n", encoding="utf-8")

    body = _synthesize_role_report(
        _spawn(),
        wave_id=_WAVE,
        role=AgentSessionRole.EXECUTOR,
        phase_id=_PHASE,
        exc=LLMAssistError(attempts=1, failures=[]),
        working_dir=repo,
    )

    assert isinstance(body, ExecutorReportBody)
    assert body.files_changed == ["src/module.py"]
    assert "unknown" in body.outcome


def test_synthesized_auditor_report_carries_no_file_list(tmp_path: Path) -> None:
    """A non-executor role has no ``files_changed`` field and synthesizes anyway."""
    repo = _repo(tmp_path)
    (repo / "src" / "module.py").write_text("value = 7\n", encoding="utf-8")

    body = _synthesize_role_report(
        _spawn(),
        wave_id=_WAVE,
        role=AgentSessionRole.AUDITOR,
        phase_id=_PHASE,
        exc=_exhausted(),
        working_dir=repo,
    )

    assert isinstance(body, AuditorReportBody)
    assert not hasattr(body, "files_changed")
    assert body.verdict is AgentReportVerdict.BLOCKED
