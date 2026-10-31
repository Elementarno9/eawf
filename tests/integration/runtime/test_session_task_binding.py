"""A host session is bound to the Task it serves, and its transcript is read with that Task's.

The session-start hook binds a session started on a canonical Task worktree branch to
the Task the branch names; ``eawf session bind`` re-scopes it by appending a superseding
row; and ``eawf run transcript <TASK>`` lists the transcript of every host session bound
to the Task. A branch that names no Task, a detached ``HEAD`` and an unknown Task key
each bind nothing.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.hooks.event import HookEventType
from eawf.runtime.hooks.lifecycle_capture import SESSION_END_HOOK, SESSION_INDEX_HOOK
from eawf.runtime.session import task_binding
from eawf.runtime.session.task_binding import (
    SessionTaskBinding,
    bind_session_to_branch_task,
    current_session_id,
    rebind_session,
    session_binding_path,
    session_task_keys,
)
from eawf.runtime.session.transcript_index import read_transcript_index, record_transcript
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.runtime.vcs.branch_name import branch_task_key, checked_out_branch, main_checkout
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import CODE, native_tree
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed_row
from tests.integration.runtime.test_transcript_index import (
    AT,
    HOST_SESSION,
    capture_runner,
    host_event,
    result_named,
)

pytestmark = pytest.mark.integration

cli = CliRunner()

TASK_KEY: Final = f"{CODE}-0001"
OTHER_TASK_KEY: Final = f"{CODE}-0002"
TASK_BRANCH: Final = f"feature/{CODE.lower()}-v0.1-{CODE.lower()}-0001"
OTHER_SESSION: Final = "1c7e2d2f-2222-4333-8444-a55566667777"

#: The environment variables a host runtime names its session in; a test run from
#: inside a host session inherits them.
SESSION_ENV: Final = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "EA_STATE")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo: Path) -> None:
    _git(repo, "-c", "user.name=ci", "-c", "user.email=ci@example.com", "commit", "-q",
         "--allow-empty", "-m", "base")  # fmt: skip


def _linked_worktree(repo: Path, task_key: str = TASK_KEY) -> Path:
    """Return a linked worktree of *repo* under its ``.ea/worktrees``, on *task_key*'s branch.

    The main checkout moves to the Milestone branch, which names no Task, and the
    worktree holds its own ``.ea``, as a checkout of a branch carrying the
    committed tree does.
    """
    _git(repo, "symbolic-ref", "HEAD", f"refs/heads/feature/{CODE.lower()}-v0.1")
    _commit(repo)
    worktree = repo / ".ea" / "worktrees" / task_key.lower()
    branch = f"feature/{CODE.lower()}-v0.1-{task_key.lower()}"
    _git(repo, "worktree", "add", "-q", "-b", branch, str(worktree))
    (worktree / ".ea").mkdir()
    return worktree


def _seed_tasks(tree_root: Path, *keys: str) -> None:
    authority = resolve_authority(tree_root)
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(path)
    for key in keys:
        row = seed_row("task", "PLANNED")
        row["key"] = key
        row["urn"] = f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/task/{key}"
        document.setdefault(Epoch2Collection.TASK.value, {})[key] = row
    write_document(path, document)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a git repository on a Task branch, holding a tree with two Tasks."""
    for name in SESSION_ENV:
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    tree_root = native_tree(root, [], [])
    _seed_tasks(tree_root, TASK_KEY, OTHER_TASK_KEY)
    _git(root, "init", "-q")
    _git(root, "symbolic-ref", "HEAD", f"refs/heads/{TASK_BRANCH}")
    return root.resolve()


def _transcript(tmp_path: Path) -> Path:
    path = tmp_path / "session.jsonl"
    record = {"type": "user", "message": {"role": "user", "content": "Bind me to the Task."}}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _start(repo: Path, transcript: Path, *, session: str = HOST_SESSION) -> str:
    payload = {"session_id": session, "transcript_path": str(transcript), "cwd": str(repo)}
    results = capture_runner(repo).run_event(
        host_event(HookEventType.SESSION_START, payload, runtime="claude")
    )
    return result_named(results, SESSION_INDEX_HOOK).output


def _invoke(repo: Path, *args: str) -> Any:
    return cli.invoke(app, ["-w", str(repo), *args])


def _error_kind(result: Any) -> str:
    return str(json.loads(result.stdout)["data"]["kind"])


# ---- the start hook ----------------------------------------------------------


def test_a_session_started_on_a_task_branch_is_bound_to_that_task(
    repo: Path, tmp_path: Path
) -> None:
    output = _start(repo, _transcript(tmp_path))

    assert output == f"{SESSION_INDEX_HOOK} ok task={TASK_KEY}"
    assert session_task_keys(repo / ".ea") == {hash_vendor_session_id(HOST_SESSION): TASK_KEY}
    (line,) = session_binding_path(repo / ".ea").read_text(encoding="utf-8").splitlines()
    row = SessionTaskBinding.model_validate_json(line)
    assert row.source == "branch"
    assert HOST_SESSION not in line


def test_the_session_end_hook_appends_no_second_binding(repo: Path, tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    _start(repo, transcript)
    payload = {"session_id": HOST_SESSION, "transcript_path": str(transcript), "cwd": str(repo)}

    results = capture_runner(repo).run_event(
        host_event(HookEventType.SESSION_END, payload, runtime="claude")
    )

    assert result_named(results, SESSION_END_HOOK).output == f"{SESSION_END_HOOK} already indexed"
    assert len(session_binding_path(repo / ".ea").read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        f"feature/{CODE.lower()}-v0.1",
        f"feature/{CODE.lower()}-v0.1-final",
        f"feature/{CODE.lower()}-v0.1-{CODE.lower()}-0001-fix",
    ],
)
def test_a_branch_naming_no_task_binds_nothing(repo: Path, tmp_path: Path, branch: str) -> None:
    _git(repo, "symbolic-ref", "HEAD", f"refs/heads/{branch}")

    assert _start(repo, _transcript(tmp_path)) == f"{SESSION_INDEX_HOOK} ok"
    assert session_task_keys(repo / ".ea") == {}
    assert not session_binding_path(repo / ".ea").exists()


def test_a_detached_head_binds_nothing(repo: Path, tmp_path: Path) -> None:
    _commit(repo)
    _git(repo, "checkout", "-q", "--detach")

    assert _start(repo, _transcript(tmp_path)) == f"{SESSION_INDEX_HOOK} ok"
    assert session_task_keys(repo / ".ea") == {}


def test_a_session_outside_a_repository_binds_nothing(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    payload = {"session_id": HOST_SESSION, "transcript_path": str(tmp_path / "s.jsonl")}
    payload["cwd"] = str(outside)

    results = capture_runner(repo).run_event(
        host_event(HookEventType.SESSION_START, payload, runtime="claude")
    )

    assert result_named(results, SESSION_INDEX_HOOK).output == f"{SESSION_INDEX_HOOK} ok"
    assert session_task_keys(repo / ".ea") == {}


def test_a_session_in_a_linked_worktree_writes_to_the_main_tree(repo: Path, tmp_path: Path) -> None:
    """The index and binding rows land in the main checkout's tree, never the worktree's."""
    worktree = _linked_worktree(repo)
    transcript = _transcript(tmp_path)

    assert _start(worktree, transcript) == f"{SESSION_INDEX_HOOK} ok task={TASK_KEY}"

    assert session_task_keys(repo / ".ea") == {hash_vendor_session_id(HOST_SESSION): TASK_KEY}
    assert [row.transcript_path for row in read_transcript_index(repo / ".ea")] == [str(transcript)]
    assert not (worktree / ".ea" / "local").exists()
    listed = _invoke(repo, "run", "transcript", TASK_KEY)
    assert listed.exit_code == 0, listed.output
    assert listed.stdout.splitlines()[0] == f"== claude-code transcript of no Run: {transcript}"


def test_session_bind_in_a_linked_worktree_writes_to_the_main_tree(
    repo: Path, tmp_path: Path
) -> None:
    worktree = _linked_worktree(repo)
    _start(worktree, _transcript(tmp_path))

    result = _invoke(worktree, "session", "bind", OTHER_TASK_KEY, "--session", HOST_SESSION)

    assert result.exit_code == 0, result.output
    assert session_task_keys(repo / ".ea") == {hash_vendor_session_id(HOST_SESSION): OTHER_TASK_KEY}
    assert not (worktree / ".ea" / "local").exists()


def test_an_operator_bind_racing_a_branch_binding_wins(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator binds between the start hook's check and its append; the operator's row stands.

    The hook's check runs under the binding file's lock, so the operator's append
    waits for the hook's and lands after it.
    """
    tree_root = repo / ".ea"
    read = task_binding.session_task_keys
    started = threading.Event()
    failures: list[BaseException] = []

    def operator_bind() -> None:
        started.set()
        try:
            rebind_session(tree_root, task_key=OTHER_TASK_KEY, session_id=HOST_SESSION, now=AT)
        except BaseException as error:
            failures.append(error)

    operator = threading.Thread(target=operator_bind)

    def check_then_race(root: Path) -> dict[str, str]:
        held = read(root)
        operator.start()
        started.wait()
        # Without the lock the operator's row lands now, before the hook's.
        operator.join(timeout=0.5)
        return held

    monkeypatch.setattr(task_binding, "session_task_keys", check_then_race)

    bound = bind_session_to_branch_task(
        tree_root, session_id=HOST_SESSION, task_key=TASK_KEY, now=AT
    )
    operator.join()

    assert failures == []
    assert bound is not None
    assert read(tree_root) == {hash_vendor_session_id(HOST_SESSION): OTHER_TASK_KEY}


# ---- eawf session bind -------------------------------------------------------


def test_session_bind_re_scopes_a_branch_bound_session(repo: Path, tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    _start(repo, transcript)

    result = _invoke(repo, "--json", "session", "bind", OTHER_TASK_KEY, "--session", HOST_SESSION)

    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout)
    assert answer["task_key"] == OTHER_TASK_KEY
    assert answer["source"] == "operator"
    assert session_task_keys(repo / ".ea") == {hash_vendor_session_id(HOST_SESSION): OTHER_TASK_KEY}
    # Both rows stand: the re-scope supersedes, it does not rewrite.
    assert len(session_binding_path(repo / ".ea").read_text(encoding="utf-8").splitlines()) == 2
    # A resumed session's start hook does not undo the operator's re-scope.
    _start(repo, transcript)
    assert session_task_keys(repo / ".ea") == {hash_vendor_session_id(HOST_SESSION): OTHER_TASK_KEY}


def test_session_bind_back_to_an_earlier_task_appends_again(repo: Path, tmp_path: Path) -> None:
    _start(repo, _transcript(tmp_path))
    for key in (OTHER_TASK_KEY, TASK_KEY):
        result = _invoke(repo, "session", "bind", key, "--session", HOST_SESSION)
        assert result.exit_code == 0, result.output

    assert result.stdout.strip() == (
        f"bound session {hash_vendor_session_id(HOST_SESSION)} to {TASK_KEY}"
    )
    assert session_task_keys(repo / ".ea") == {hash_vendor_session_id(HOST_SESSION): TASK_KEY}
    assert len(session_binding_path(repo / ".ea").read_text(encoding="utf-8").splitlines()) == 3


def test_session_bind_refuses_an_unknown_task(repo: Path, tmp_path: Path) -> None:
    _start(repo, _transcript(tmp_path))

    result = _invoke(repo, "--json", "session", "bind", f"{CODE}-0099", "--session", HOST_SESSION)

    assert result.exit_code == exit_codes.USER_ERROR
    assert _error_kind(result) == "NotFound"
    assert f"{CODE}-0099" in result.stdout
    assert session_task_keys(repo / ".ea") == {hash_vendor_session_id(HOST_SESSION): TASK_KEY}


@pytest.mark.parametrize("key", ["", "rfl-0001", f"{CODE}-1", f"{CODE}0001", "R-0001"])
def test_session_bind_refuses_a_malformed_task_key(repo: Path, key: str) -> None:
    result = _invoke(repo, "--json", "session", "bind", key, "--session", HOST_SESSION)

    assert result.exit_code == exit_codes.USER_ERROR
    assert _error_kind(result) == "InvalidInput"
    assert not session_binding_path(repo / ".ea").exists()


def test_session_bind_refuses_an_empty_session_id(repo: Path) -> None:
    result = _invoke(repo, "--json", "session", "bind", TASK_KEY, "--session", "")

    assert result.exit_code == exit_codes.USER_ERROR
    assert _error_kind(result) == "InvalidInput"


def test_session_bind_defaults_to_the_running_session(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(repo, _transcript(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OTHER_SESSION)

    result = _invoke(repo, "session", "bind", OTHER_TASK_KEY)

    assert result.exit_code == 0, result.output
    assert session_task_keys(repo / ".ea") == {
        hash_vendor_session_id(HOST_SESSION): TASK_KEY,
        hash_vendor_session_id(OTHER_SESSION): OTHER_TASK_KEY,
    }


def test_session_bind_never_guesses_the_last_indexed_session(repo: Path) -> None:
    """Error path: with no session in the environment the bind is refused, not guessed.

    Sessions run side by side, so the host session indexed last may be another one.
    """
    record_transcript(
        repo / ".ea", runtime="claude-code", session_id=HOST_SESSION, transcript_path="b", now=AT
    )

    result = _invoke(repo, "--json", "session", "bind", TASK_KEY)

    assert result.exit_code == exit_codes.USER_ERROR
    assert _error_kind(result) == "NotFound"
    assert "--session" in result.stdout
    assert not session_binding_path(repo / ".ea").exists()


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"CODEX_THREAD_ID": OTHER_SESSION}, OTHER_SESSION),
        ({"CLAUDE_CODE_SESSION_ID": HOST_SESSION, "CODEX_THREAD_ID": OTHER_SESSION}, HOST_SESSION),
        ({"CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": OTHER_SESSION}, OTHER_SESSION),
    ],
)
def test_current_session_id_reads_the_environment(env: dict[str, str], expected: str) -> None:
    assert current_session_id(env) == expected


@pytest.mark.parametrize("env", [{}, {"CLAUDE_CODE_SESSION_ID": ""}, {"OTHER": HOST_SESSION}])
def test_current_session_id_without_a_session_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(LookupError, match="--session"):
        current_session_id(env)


def test_session_bind_with_no_session_to_find_is_refused(repo: Path) -> None:
    result = _invoke(repo, "--json", "session", "bind", TASK_KEY)

    assert result.exit_code == exit_codes.USER_ERROR
    assert _error_kind(result) == "NotFound"
    assert "--session" in result.stdout


# ---- eawf run transcript -----------------------------------------------------


def test_run_transcript_of_the_task_lists_the_bound_host_session(
    repo: Path, tmp_path: Path
) -> None:
    transcript = _transcript(tmp_path)
    _start(repo, transcript)

    result = _invoke(repo, "run", "transcript", TASK_KEY)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == f"== claude-code transcript of no Run: {transcript}"
    assert "user: Bind me to the Task." in lines


def test_run_transcript_follows_the_re_scope(repo: Path, tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    _start(repo, transcript)
    assert (
        _invoke(repo, "session", "bind", OTHER_TASK_KEY, "--session", HOST_SESSION).exit_code == 0
    )

    moved = _invoke(repo, "--json", "run", "transcript", OTHER_TASK_KEY)
    left = _invoke(repo, "--json", "run", "transcript", TASK_KEY)

    assert moved.exit_code == 0, moved.output
    (entry,) = json.loads(moved.stdout)["result"]["entries"]
    assert entry["path"] == str(transcript)
    assert entry["run_key"] is None
    assert left.exit_code == exit_codes.USER_ERROR
    assert _error_kind(left) == "NotFound"


def test_run_transcript_lists_a_bound_session_s_subagent(repo: Path, tmp_path: Path) -> None:
    _start(repo, _transcript(tmp_path))
    record_transcript(
        repo / ".ea",
        runtime="claude-code",
        session_id="agent-1",
        transcript_path=str(tmp_path / "agent.jsonl"),
        now=AT,
        parent_session_id=HOST_SESSION,
    )

    result = _invoke(repo, "--json", "run", "transcript", TASK_KEY)

    assert result.exit_code == 0, result.output
    entries = json.loads(result.stdout)["result"]["entries"]
    assert [entry["subagent"] for entry in entries] == [False, True]


def test_run_transcript_of_a_bound_session_without_a_transcript_row_is_refused(
    repo: Path,
) -> None:
    bind_session_to_branch_task(repo / ".ea", session_id=HOST_SESSION, task_key=TASK_KEY, now=AT)

    result = _invoke(repo, "--json", "run", "transcript", TASK_KEY)

    assert result.exit_code == exit_codes.USER_ERROR
    assert "no transcript is indexed" in result.stdout


# ---- the library -------------------------------------------------------------


@pytest.mark.parametrize(
    ("branch", "task_key"),
    [
        ("feature/eawf-v0.7-eawf-0649", "EAWF-0649"),
        ("feature/my-proj-v10.12-my-proj-0001", "MY-PROJ-0001"),
        ("feature/ab-v0.1-ab-9999", "AB-9999"),
        ("feature/eawf-v0.7", None),
        ("feature/eawf-v0.7-EAWF-0649", None),
        ("feature/eawf-v0.7-other-0649", None),
        ("feature/eawf-v0.7-eawf-064", None),
        ("feature/eawf-v0.7-eawf-06490", None),
        ("", None),
        ("main", None),
    ],
)
def test_branch_task_key(branch: str, task_key: str | None) -> None:
    assert branch_task_key(branch) == task_key


def test_checked_out_branch_outside_a_repository_is_none(tmp_path: Path) -> None:
    assert checked_out_branch(tmp_path) is None


def test_checked_out_branch_reads_the_branch(repo: Path) -> None:
    assert checked_out_branch(repo) == TASK_BRANCH


def test_main_checkout_of_a_linked_worktree_is_the_main_tree(repo: Path) -> None:
    worktree = _linked_worktree(repo)

    assert main_checkout(worktree) == repo
    assert main_checkout(worktree / ".ea") == repo
    assert main_checkout(repo) == repo


def test_main_checkout_outside_a_repository_is_the_directory_itself(tmp_path: Path) -> None:
    assert main_checkout(tmp_path) == tmp_path


def test_main_checkout_of_a_bare_repository_is_the_directory_itself(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    assert main_checkout(bare) == bare


def test_a_branch_binding_is_skipped_for_a_bound_session(repo: Path) -> None:
    tree_root = repo / ".ea"
    first = bind_session_to_branch_task(
        tree_root, session_id=HOST_SESSION, task_key=TASK_KEY, now=AT
    )
    second = bind_session_to_branch_task(
        tree_root, session_id=HOST_SESSION, task_key=OTHER_TASK_KEY, now=AT
    )

    assert first is not None
    assert second is None
    assert session_task_keys(tree_root) == {hash_vendor_session_id(HOST_SESSION): TASK_KEY}


def test_a_corrupt_binding_line_is_refused(repo: Path) -> None:
    path = session_binding_path(repo / ".ea")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"task_key": "RFL-0001"}\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        session_task_keys(repo / ".ea")


def test_a_binding_row_refuses_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        SessionTaskBinding.model_validate(
            {
                "vendor_session_digest": HOST_SESSION,
                "task_key": TASK_KEY,
                "source": "branch",
                "recorded_at": AT,
                "extra": 1,
            }
        )
