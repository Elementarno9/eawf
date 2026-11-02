"""A Run's runtime transcripts outlive the workspace eawf removes after it.

A dispatched Run's Claude Code or Codex child writes its own transcript inside the
Run's isolation directory, and the transcript index points there. Removing a clean
leased worktree, or a Campaign round's checkout, deletes that directory; the files
are copied under the tree's git-ignored ``local/`` first and indexed there, so
``eawf run transcript`` on the Run's Task still prints every entry.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from eawf.kernel.runtime.lease import LeaseStatus
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.platform.install.canary import CanaryProvision
from eawf.platform.install.gitignore_writer import write_gitignore
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods.campaign_run import _round_checkout
from eawf.runtime.daemon.run_supervisor import supervisor_of
from eawf.runtime.runtimes.claude.managed_run import MANAGED_CONFIG_DIRNAME
from eawf.runtime.runtimes.codex.app_server import CODEX_HOME_DIRNAME
from eawf.runtime.runtimes.run_transcript import (
    TranscriptReader,
    TranscriptSource,
    follow_transcripts,
    select_transcripts,
)
from eawf.runtime.session import transcript_index
from eawf.runtime.session.transcript_index import read_transcript_index, record_transcript
from eawf.runtime.workspace import lease as lease_module
from eawf.runtime.workspace.lease import (
    MCP_ARTIFACT_DIRNAME,
    issue_lease,
    read_lease,
    reconcile_root,
    workspace_path,
)
from tests.integration.runtime.daemon.test_native_dispatch import (
    RUN_URN,
    TASK_URN,
    make_canary,
    root_ctx,
    run_row,
)

pytestmark = pytest.mark.integration

ISSUED: Final = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
JUDGED: Final = ISSUED + timedelta(hours=3)
TASK_KEY: Final = TASK_URN.rsplit("/", 1)[-1]

SESSION: Final = "0b6f1c1e-1111-4222-8333-944455556666"
AGENT: Final = "subagent-one"
THREAD: Final = "0199a3c2-7d4e-7f10-8a2b-3c4d5e6f7a8b"


def _claude_line(role: str, text: str) -> str:
    return json.dumps({"type": role, "message": {"role": role, "content": text}})


def _codex_line(role: str, text: str) -> str:
    block = "input_text" if role == "user" else "output_text"
    payload = {"type": "message", "role": role, "content": [{"type": block, "text": text}]}
    return json.dumps({"type": "response_item", "payload": payload})


def plant_transcripts(run_dir: Path) -> dict[str, Path]:
    """Write a Claude session, its subagent and a Codex rollout where each runtime does."""
    project = run_dir / MANAGED_CONFIG_DIRNAME / "projects" / "-work-tree"
    files = {
        "session": project / f"{SESSION}.jsonl",
        "subagent": project / SESSION / "subagents" / f"agent-{AGENT}.jsonl",
        "rollout": run_dir
        / CODEX_HOME_DIRNAME
        / "sessions"
        / "2026"
        / "09"
        / "18"
        / f"rollout-2026-09-18T09-00-00-{THREAD}.jsonl",
    }
    lines = {
        "session": [_claude_line("user", "Survey src."), _claude_line("assistant", "One module.")],
        "subagent": [_claude_line("user", "Check alpha."), _claude_line("assistant", "Alpha ok.")],
        "rollout": [_codex_line("user", "Count tests."), _codex_line("assistant", "Two tests.")],
    }
    for name, path in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines[name]) + "\n", encoding="utf-8")
    return files


def index_as_runtime_does(tree_root: Path, files: dict[str, Path]) -> None:
    """Index each file at its original path, as the launcher and subagent hook do."""
    record_transcript(
        tree_root,
        runtime="claude-code",
        session_id=SESSION,
        transcript_path=str(files["session"]),
        now=ISSUED,
        run_ref=RUN_URN,
    )
    record_transcript(
        tree_root,
        runtime="claude-code",
        session_id=AGENT,
        transcript_path=str(files["subagent"]),
        now=ISSUED,
        parent_session_id=SESSION,
    )
    record_transcript(
        tree_root,
        runtime="codex",
        session_id=THREAD,
        transcript_path=str(files["rollout"]),
        now=ISSUED,
        run_ref=RUN_URN,
    )


def printed(tree_root: Path, key: str) -> list[tuple[TranscriptSource, str, str]]:
    """Return every entry ``eawf run transcript <key>`` reads, with its source."""
    selection = select_transcripts(tree_root, key)
    readers = [TranscriptReader(source) for source in selection.sources]
    return [
        (source, entry.kind, entry.text)
        for source, entry in follow_transcripts(readers, lambda: False)
    ]


@pytest.fixture
def leased(tmp_path: Path) -> tuple[CanaryProvision, Epoch2RootContext, str]:
    """A canary holding one completed Run of the Task, over a lease three hours old."""
    canary = make_canary(tmp_path / "repo", rows={"RUN-00000001": run_row("COMPLETED")})
    context = root_ctx(canary, tmp_path / "runtime")
    lease = issue_lease(
        context,
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=("src",),
        now=ISSUED,
        ttl=timedelta(hours=1),
    )
    return canary, context, lease.lease_id


def test_a_released_lease_keeps_its_runs_transcripts_readable(
    leased: tuple[CanaryProvision, Epoch2RootContext, str],
) -> None:
    """Session, subagent and rollout all print after the clean worktree is removed."""
    _, context, lease_id = leased
    tree_root = context.identity.tree_root
    handle = read_lease(context, lease_id=lease_id).workspace_handle
    target = workspace_path(context, handle=handle)
    index_as_runtime_does(tree_root, plant_transcripts(target / MCP_ARTIFACT_DIRNAME))

    report = reconcile_root(context, now=JUDGED, busy=supervisor_of(context).busy)

    assert report.released == 1
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.RELEASED
    assert not target.exists()
    entries = printed(tree_root, TASK_KEY)
    assert [(kind, text) for _, kind, text in entries] == [
        ("user", "Survey src."),
        ("assistant", "One module."),
        ("user", "Count tests."),
        ("assistant", "Two tests."),
        ("user", "Check alpha."),
        ("assistant", "Alpha ok."),
    ]
    preserved = tree_root / "local" / transcript_index.PRESERVED_TRANSCRIPTS_DIRNAME / handle
    assert all(source.path.is_relative_to(preserved) for source, _, _ in entries)
    assert [source.subagent for source, _, _ in entries] == [False] * 4 + [True] * 2


def test_the_preserved_copies_are_ignored_by_git(
    leased: tuple[CanaryProvision, Epoch2RootContext, str],
) -> None:
    """The copies sit under the tree's local directory, which the init ignore block covers."""
    canary, context, lease_id = leased
    write_gitignore(canary.root)
    handle = read_lease(context, lease_id=lease_id).workspace_handle
    plant_transcripts(workspace_path(context, handle=handle) / MCP_ARTIFACT_DIRNAME)

    reconcile_root(context, now=JUDGED, busy=supervisor_of(context).busy)

    copies = [row.transcript_path for row in read_transcript_index(context.identity.tree_root)]
    assert len(copies) == 3
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", *copies],
        cwd=canary.root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0
    assert len(ignored.stdout.splitlines()) == 3


def test_a_failed_copy_keeps_the_worktree_and_still_releases_the_lease(
    leased: tuple[CanaryProvision, Epoch2RootContext, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transcript that cannot be kept is not deleted, and the release is not stranded."""
    _, context, lease_id = leased
    target = workspace_path(context, handle=read_lease(context, lease_id=lease_id).workspace_handle)
    files = plant_transcripts(target / MCP_ARTIFACT_DIRNAME)

    def refused(*_args: object, **_kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(lease_module, "preserve_run_transcripts", refused)

    report = reconcile_root(context, now=JUDGED, busy=supervisor_of(context).busy)

    assert report.released == 1
    assert read_lease(context, lease_id=lease_id).status is LeaseStatus.RELEASED
    assert all(path.is_file() for path in files.values())


# ---- preserve_run_transcripts --------------------------------------------------------


def test_preserving_copies_each_file_and_indexes_it_under_its_run(tmp_path: Path) -> None:
    """Top-level files are indexed under the Run, a subagent under its parent session."""
    tree_root = tmp_path / ".ea"
    run_dir = tmp_path / "workspace" / MCP_ARTIFACT_DIRNAME
    files = plant_transcripts(run_dir)

    recorded = transcript_index.preserve_run_transcripts(
        tree_root, run_dir, workspace_handle="wsh-one", run_ref=RUN_URN, now=JUDGED
    )

    assert recorded == 3
    rows = read_transcript_index(tree_root)
    preserved = tree_root / "local" / transcript_index.PRESERVED_TRANSCRIPTS_DIRNAME / "wsh-one"
    for row in rows:
        copy = Path(row.transcript_path)
        original = run_dir / copy.relative_to(preserved)
        assert copy.read_bytes() == original.read_bytes()
    shapes = sorted(
        (row.runtime, row.run_ref or "", row.parent_session_digest is not None) for row in rows
    )
    assert shapes == [
        ("claude-code", "", True),
        ("claude-code", RUN_URN, False),
        ("codex", RUN_URN, False),
    ]
    assert {path.name for path in files.values()} == {
        Path(row.transcript_path).name for row in rows
    }


def test_preserving_a_run_dir_with_no_transcript_writes_nothing(tmp_path: Path) -> None:
    """A Run whose runtime never wrote a transcript leaves no copy and no row."""
    tree_root = tmp_path / ".ea"
    run_dir = tmp_path / "workspace" / MCP_ARTIFACT_DIRNAME
    (run_dir / "tmp").mkdir(parents=True)

    recorded = transcript_index.preserve_run_transcripts(
        tree_root, run_dir, workspace_handle="wsh-empty", run_ref=RUN_URN, now=JUDGED
    )

    assert recorded == 0
    assert read_transcript_index(tree_root) == ()
    assert not (tree_root / "local" / transcript_index.PRESERVED_TRANSCRIPTS_DIRNAME).exists()


def test_preserving_twice_appends_no_second_row(tmp_path: Path) -> None:
    """An already preserved Run is copied over and indexed once."""
    tree_root = tmp_path / ".ea"
    run_dir = tmp_path / "workspace" / MCP_ARTIFACT_DIRNAME
    plant_transcripts(run_dir)
    transcript_index.preserve_run_transcripts(
        tree_root, run_dir, workspace_handle="wsh-twice", run_ref=RUN_URN, now=JUDGED
    )

    again = transcript_index.preserve_run_transcripts(
        tree_root, run_dir, workspace_handle="wsh-twice", run_ref=RUN_URN, now=JUDGED
    )

    assert again == 0
    assert len(read_transcript_index(tree_root)) == 3


# ---- the reader prefers a row whose file exists ----------------------------------------


def test_a_still_present_original_is_printed_once(
    leased: tuple[CanaryProvision, Epoch2RootContext, str],
) -> None:
    """A session indexed at both its original and its copy prints from one of them."""
    _, context, lease_id = leased
    tree_root = context.identity.tree_root
    handle = read_lease(context, lease_id=lease_id).workspace_handle
    run_dir = workspace_path(context, handle=handle) / MCP_ARTIFACT_DIRNAME
    index_as_runtime_does(tree_root, plant_transcripts(run_dir))
    transcript_index.preserve_run_transcripts(
        tree_root, run_dir, workspace_handle=handle, run_ref=RUN_URN, now=JUDGED
    )

    entries = printed(tree_root, TASK_KEY)

    assert len(entries) == 6
    assert all(source.path.is_relative_to(run_dir) for source, _, _ in entries)


def test_a_session_with_no_surviving_file_is_still_noticed(
    leased: tuple[CanaryProvision, Epoch2RootContext, str],
) -> None:
    """With no copy to fall back on, a deleted transcript is still reported missing."""
    _, context, lease_id = leased
    tree_root = context.identity.tree_root
    run_dir = (
        workspace_path(context, handle=read_lease(context, lease_id=lease_id).workspace_handle)
        / MCP_ARTIFACT_DIRNAME
    )
    files = plant_transcripts(run_dir)
    index_as_runtime_does(tree_root, files)
    files["rollout"].unlink()

    kinds = [(source.runtime, kind) for source, kind, _ in printed(tree_root, TASK_KEY)]

    assert ("codex", "notice") in kinds


# ---- a Campaign round's checkout ---------------------------------------------------------


def test_a_campaign_round_checkout_keeps_its_transcript(tmp_path: Path) -> None:
    """The round's checkout is removed, and its Run's transcript is kept and indexed."""
    repo = tmp_path / "repo"
    canary = make_canary(repo, rows={})
    tree_root = canary.root / ".ea"

    async def round_writes_a_transcript() -> Path:
        async with _round_checkout(
            tree_root, run_ref=RUN_URN, workspace_handle="wsh-round"
        ) as workspace:
            plant_transcripts(workspace / MCP_ARTIFACT_DIRNAME)
        return workspace

    workspace = asyncio.run(round_writes_a_transcript())

    assert not workspace.exists()
    rows = read_transcript_index(tree_root)
    assert len(rows) == 3
    assert all(Path(row.transcript_path).is_file() for row in rows)
    assert {row.run_ref for row in rows if row.parent_session_digest is None} == {RUN_URN}
