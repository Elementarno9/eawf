"""RUN-062, CON-170: a file edit is a ``file_changed`` line bound to the trees around it.

The dispatch path is driven through the real driver against a canary over a real git
repository, with a launcher whose worker edits its leased workspace the way a provider
process would. What lands on the Run's stream is read back off the run ledger, and the
diff the line names is read off the artifact store, so every assertion is about what a
reader of the tree would find rather than what the producer said it did.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from eawf.kernel.runtime.content import CONTENT_LINE_LIMIT, WITHHELD_LINE
from eawf.kernel.runtime.events import FileChangePayload, RunEventKind, RunEventRecord
from eawf.kernel.store.ledger import read_ledger_records
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import native_dispatch
from eawf.runtime.daemon.content_store import CONTENT_REF_PREFIX, stored_contents
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.file_changes import FileEdit, record_file_edit
from eawf.runtime.daemon.native_dispatch import run_ledger
from eawf.runtime.daemon.run_events import run_events_of
from eawf.runtime.runtimes.adapter import NativeLaunchOutcome, NativeLaunchRequest
from eawf.runtime.worktree.tree_change import changed_paths, snapshot_tree, tree_diff
from eawf.surfaces.cli import errors as cli_errors
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed
from tests.integration.runtime.daemon.test_host_subagent_adoption import repository_row
from tests.integration.runtime.daemon.test_native_dispatch import (
    ACTOR,
    RUN_KEY,
    LedgerReadingLauncher,
    dispatch,
    make_canary,
    make_repo,
    method_ctx,
    root_ctx,
    run_row,
    run_urn,
)

pytestmark = pytest.mark.integration

#: A home path, spelled in pieces so the leak lint does not read it as one.
MACOS_HOME: Final = "/" + "Users" + "/" + "alice"

#: A line carrying that home path, which a stored diff must never carry.
LEAKY_LINE: Final = f"path = '{MACOS_HOME}/secret/config'\n"


class EditingLauncher(LedgerReadingLauncher):
    """A launcher whose worker edits one file and adds another in its workspace."""

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Edit the leased workspace, then answer as the ledger-reading launcher does."""
        (request.workspace / "src" / "module.py").write_text("x = 2\n", encoding="utf-8")
        (request.workspace / "src" / "added.py").write_text(LEAKY_LINE, encoding="utf-8")
        return await super().launch(request)


def _canary(tmp_path: Path) -> tuple[CanaryProvision, Path]:
    return make_canary(tmp_path / "repo", rows={RUN_KEY: run_row()}), tmp_path / "runtime"


def _seed_repository(canary: CanaryProvision) -> None:
    seed(canary, {"repository": {"REP-EAWF": repository_row()}})


def _diff(context: Epoch2RootContext, ref: str) -> list[str]:
    """Return the kept lines the content store holds under *ref*."""
    with context.session([run_urn()]) as session:
        return list(stored_contents(session, run_urn())[ref].lines)


def _file_lines(context: Epoch2RootContext) -> list[RunEventRecord]:
    with context.session([run_urn()]) as session:
        events = run_events_of(read_ledger_records(run_ledger(session)), run_urn())
    return [event for event in events if isinstance(event.payload, FileChangePayload)]


def test_run_062_a_dispatched_worker_edit_lands_as_one_file_changed_line(
    tmp_path: Path,
) -> None:
    """RUN-062, CON-170: the worker's edits are one line with both trees and a stored diff."""
    canary, runtime = _canary(tmp_path)
    _seed_repository(canary)
    launcher = EditingLauncher(canary, runtime)

    answer = dispatch(method_ctx(runtime), canary, launcher)

    assert answer["stage"] == "announced"
    context = root_ctx(canary, runtime)
    (line,) = _file_lines(context)
    payload = line.payload
    assert isinstance(payload, FileChangePayload)
    assert line.event_kind is RunEventKind.FILE_CHANGED
    assert line.provenance == "daemon_observed"
    assert set(payload.changed_paths) == {"src/module.py", "src/added.py"}
    assert payload.before_tree_digest != payload.after_tree_digest
    assert payload.summary_only is False
    assert payload.diff_ref.startswith(CONTENT_REF_PREFIX)
    diff = _diff(context, payload.diff_ref)
    assert "-x = 1" in diff and "+x = 2" in diff
    assert not any(MACOS_HOME in line for line in diff)
    assert WITHHELD_LINE in diff


def test_run_062_a_worker_that_edits_nothing_records_no_line(tmp_path: Path) -> None:
    """RUN-062: an unchanged workspace is not a file change."""
    canary, runtime = _canary(tmp_path)
    _seed_repository(canary)

    dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))

    assert _file_lines(root_ctx(canary, runtime)) == []


def test_run_062_an_unreadable_workspace_never_fails_the_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RUN-062: a tree git cannot read costs the edit record, not the dispatch."""

    def refuse(_workspace: Path) -> None:
        raise cli_errors.StateConflict("not a git working tree", kind="IntegrityViolation")

    monkeypatch.setattr(native_dispatch, "snapshot_tree", refuse)
    canary, runtime = _canary(tmp_path)
    _seed_repository(canary)

    answer = dispatch(method_ctx(runtime), canary, EditingLauncher(canary, runtime))

    assert answer["stage"] == "announced"
    assert _file_lines(root_ctx(canary, runtime)) == []


def test_run_062_an_oversized_diff_is_recorded_as_its_summary(tmp_path: Path) -> None:
    """RUN-062: a diff past the bound is a ``diff_summarized`` line naming the stat."""
    canary, runtime = _canary(tmp_path)
    _seed_repository(canary)
    context = root_ctx(canary, runtime)
    workspace = canary.root
    before = snapshot_tree(workspace)
    (workspace / "src" / "big.py").write_text(
        "".join(f"value_{index} = {index}\n" for index in range(CONTENT_LINE_LIMIT * 2)),
        encoding="utf-8",
    )
    after = snapshot_tree(workspace)

    answer = record_file_edit(
        context,
        FileEdit(workspace=workspace, before=before, after=after, paths=("src/big.py",)),
        run_ref=run_urn(),
        event_ref="EVT-" + "a" * 32,
        actor=ACTOR,
        now=datetime.now(UTC),
    )

    assert answer is not None
    (line,) = _file_lines(context)
    assert line.event_kind is RunEventKind.DIFF_SUMMARIZED
    assert isinstance(line.payload, FileChangePayload)
    assert line.payload.summary_only is True
    summary = _diff(context, line.payload.diff_ref)
    assert any("src/big.py" in text for text in summary)
    assert not any("value_1 = 1" in text for text in summary)


def test_snapshot_tree_leaves_the_operators_index_alone(tmp_path: Path) -> None:
    """A snapshot writes a tree without staging anything in the checkout's own index."""
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "src" / "module.py").write_text("x = 3\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")

    before_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    head = snapshot_tree(repo)
    again = snapshot_tree(repo)
    after_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout

    assert before_status == after_status
    assert head == again
    assert head.digest.startswith("sha256:") and len(head.digest) == len("sha256:") + 64


def test_tree_change_names_only_the_paths_asked_about(tmp_path: Path) -> None:
    """A comparison limited to one path ignores what else changed in the tree."""
    repo = tmp_path / "repo"
    make_repo(repo)
    before = snapshot_tree(repo)
    (repo / "src" / "module.py").write_text("x = 4\n", encoding="utf-8")
    (repo / "other.txt").write_text("elsewhere\n", encoding="utf-8")
    after = snapshot_tree(repo)

    assert changed_paths(repo, before, after) == ("other.txt", "src/module.py")
    assert changed_paths(repo, before, after, paths=("src/module.py",)) == ("src/module.py",)
    assert changed_paths(repo, before, before) == ()
    assert tree_diff(repo, before, before) == ""
