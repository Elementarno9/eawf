"""Each Run's runtime transcript file is indexed in a machine-local store.

Four producers fill the index: the dispatch path records the file a launcher names,
the host session hooks record the session's own file, the subagent stop hook records
the subagent's file under its parent session, and the daemon back-fills Runs no row
names by hashing the session id each transcript file is named for. The index lives
under the tree's ``local/`` directory, which the managed ignore block keeps out of git.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.measurement import VendorSessionRef
from eawf.platform.install.gitignore_writer import write_gitignore
from eawf.runtime.daemon.transcript_backfill import backfill_root_transcripts
from eawf.runtime.hooks import lifecycle_capture
from eawf.runtime.hooks.event import HookEvent, HookEventType, HookRuntime
from eawf.runtime.hooks.lifecycle_capture import SESSION_END_HOOK, SESSION_INDEX_HOOK
from eawf.runtime.hooks.runner import HookResult, HookRunner, register_runtime_capture_hooks
from eawf.runtime.runtimes.adapter import NativeLaunchOutcome, NativeLaunchRequest
from eawf.runtime.runtimes.claude.managed_run import MANAGED_CONFIG_DIRNAME
from eawf.runtime.runtimes.codex.app_server import CODEX_HOME_DIRNAME
from eawf.runtime.session import transcript_index
from eawf.runtime.session.transcript_index import (
    TranscriptIndexRow,
    backfill_transcript_index,
    read_transcript_index,
    record_transcript,
    transcript_index_path,
)
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.runtime.workspace.lease import MCP_ARTIFACT_DIRNAME, WORKSPACE_LOCATOR
from eawf.surfaces.cli.errors import StateConflict
from tests.integration.runtime.daemon.test_native_dispatch import (
    RUN_URN,
    LedgerReadingLauncher,
    dispatch,
    make_canary,
    method_ctx,
    run_row,
)

pytestmark = pytest.mark.integration

AT: Final = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

#: The Claude Code projects root the transcript reader honours in place of the
#: operator's own.
PROJECTS_ENV: Final = "EAWF_CLAUDE_PROJECTS_DIR"

HOST_SESSION: Final = "0b6f1c1e-1111-4222-8333-944455556666"
AGENT_ID: Final = "a1b2c3d4e5f6"
CODEX_THREAD: Final = "0199a3c2-7d4e-7f10-8a2b-3c4d5e6f7a8b"


def eawf_repo(root: Path) -> Path:
    """Return *root* holding an empty ``.ea`` directory, as an initialised tree has."""
    (root / ".ea").mkdir(parents=True)
    return root


def host_event(
    event_type: HookEventType, payload: dict[str, Any], *, runtime: HookRuntime
) -> HookEvent:
    """Return a host lifecycle event carrying *payload* the way the hook CLI folds it."""
    return HookEvent(
        event_type=event_type,
        runtime=runtime,
        occurred_at=AT,
        payloads={event_type.value: payload},
    )


class _Client:
    """A daemon client that answers every verb with *answer*, or raises."""

    def __init__(self, answer: dict[str, Any] | None) -> None:
        self._answer = answer

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._answer is None:
            raise ConnectionRefusedError("no daemon")
        return self._answer


def capture_runner(repo: Path, *, answer: dict[str, Any] | None = None) -> HookRunner:
    """Return the real hook registry over *repo*, its daemon answering *answer*."""
    runner = HookRunner()
    register_runtime_capture_hooks(
        runner, daemon_client_factory=lambda: _Client(answer), repo_root=repo
    )
    return runner


def result_named(results: list[HookResult], name: str) -> HookResult:
    """Return the one result *name* reported."""
    (result,) = (item for item in results if item.name == name)
    return result


# ---- a dispatched Run --------------------------------------------------------


class TranscriptNamingLauncher(LedgerReadingLauncher):
    """A launcher whose provider names the file its session writes, or none."""

    def __init__(self, *args: Any, transcript: str | None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._transcript = transcript

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        outcome = await super().launch(request)
        return outcome.model_copy(update={"transcript_path": self._transcript})


def test_a_dispatched_run_gets_a_row_naming_its_launcher_transcript(tmp_path: Path) -> None:
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    transcript = str(tmp_path / "codex-home" / "sessions" / f"rollout-x-{CODEX_THREAD}.jsonl")

    dispatch(
        method_ctx(runtime),
        canary,
        TranscriptNamingLauncher(canary, runtime, transcript=transcript),
    )

    (row,) = read_transcript_index(canary.root / ".ea")
    assert row.run_ref == str(RUN_URN)
    assert row.runtime == "codex"
    assert row.transcript_path == transcript
    assert row.vendor_session_digest == hash_vendor_session_id("codex-session-1")
    assert row.parent_session_digest is None


def test_a_launcher_naming_no_transcript_leaves_the_index_unwritten(tmp_path: Path) -> None:
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"

    dispatch(
        method_ctx(runtime), canary, TranscriptNamingLauncher(canary, runtime, transcript=None)
    )

    assert not transcript_index_path(canary.root / ".ea").exists()


def test_the_launch_outcome_never_dumps_the_transcript_path(tmp_path: Path) -> None:
    outcome = NativeLaunchOutcome.model_construct(
        provider_session_ref="s", subprocess_pid=1, transcript_path=str(tmp_path)
    )

    assert "transcript_path" not in outcome.model_dump(exclude={"hello"})


# ---- a host session ----------------------------------------------------------


@pytest.mark.parametrize(("runtime", "harness"), [("claude", "claude-code"), ("codex", "codex")])
def test_a_host_session_gets_one_row_from_its_start_and_end_hooks(
    tmp_path: Path, runtime: HookRuntime, harness: str
) -> None:
    repo = eawf_repo(tmp_path / "repo")
    runner = capture_runner(repo)
    payload = {"session_id": HOST_SESSION, "transcript_path": str(tmp_path / "session.jsonl")}

    started = runner.run_event(host_event(HookEventType.SESSION_START, payload, runtime=runtime))
    ended = runner.run_event(host_event(HookEventType.SESSION_END, payload, runtime=runtime))

    assert result_named(started, SESSION_INDEX_HOOK).output == f"{SESSION_INDEX_HOOK} ok"
    assert result_named(ended, SESSION_END_HOOK).output == f"{SESSION_END_HOOK} already indexed"
    (row,) = read_transcript_index(repo / ".ea")
    assert row.runtime == harness
    assert row.run_ref is None
    assert row.parent_session_digest is None
    assert row.vendor_session_digest == hash_vendor_session_id(HOST_SESSION)
    assert row.transcript_path == payload["transcript_path"]
    assert HOST_SESSION not in transcript_index_path(repo / ".ea").read_text(encoding="utf-8")


def test_a_session_end_naming_no_transcript_writes_nothing(tmp_path: Path) -> None:
    repo = eawf_repo(tmp_path / "repo")

    results = capture_runner(repo).run_event(
        host_event(HookEventType.SESSION_END, {"session_id": HOST_SESSION}, runtime="claude")
    )

    stamp = result_named(results, SESSION_END_HOOK)
    assert stamp.output == f"{SESSION_END_HOOK} skipped: no session transcript named"
    assert not stamp.block
    assert read_transcript_index(repo / ".ea") == ()


def test_a_session_outside_an_eawf_tree_writes_nothing(tmp_path: Path) -> None:
    payload = {"session_id": HOST_SESSION, "transcript_path": str(tmp_path / "s.jsonl")}

    results = capture_runner(tmp_path).run_event(
        host_event(HookEventType.SESSION_END, payload, runtime="claude")
    )

    assert result_named(results, SESSION_END_HOOK).output.endswith("skipped: not an eawf tree")
    assert not (tmp_path / ".ea").exists()


def test_a_runtime_without_sessions_writes_nothing(tmp_path: Path) -> None:
    repo = eawf_repo(tmp_path / "repo")
    payload = {"session_id": HOST_SESSION, "transcript_path": str(tmp_path / "s.jsonl")}

    results = capture_runner(repo).run_event(
        host_event(HookEventType.SESSION_END, payload, runtime="generic")
    )

    assert "names no transcript" in result_named(results, SESSION_END_HOOK).output
    assert read_transcript_index(repo / ".ea") == ()


# ---- a subagent --------------------------------------------------------------


def subagent_stop(tmp_path: Path) -> HookEvent:
    """Return the stop event of a Claude Code subagent spawned by the host session."""
    return host_event(
        HookEventType.SUBAGENT_STOP,
        {
            "session_id": HOST_SESSION,
            "agent_id": AGENT_ID,
            "agent_transcript_path": str(tmp_path / "subagents" / f"agent-{AGENT_ID}.jsonl"),
        },
        runtime="claude",
    )


def test_a_subagent_gets_a_row_with_its_run_and_parent_session(tmp_path: Path) -> None:
    repo = eawf_repo(tmp_path / "repo")
    runner = capture_runner(repo, answer={"run_ref": str(RUN_URN)})

    runner.run_event(subagent_stop(tmp_path))

    (row,) = read_transcript_index(repo / ".ea")
    assert row.run_ref == str(RUN_URN)
    assert row.runtime == "claude-code"
    assert row.vendor_session_digest == hash_vendor_session_id(AGENT_ID)
    assert row.parent_session_digest == hash_vendor_session_id(HOST_SESSION)
    assert row.transcript_path.endswith(f"agent-{AGENT_ID}.jsonl")


def test_a_subagent_the_daemon_refused_is_still_indexed_without_a_run(tmp_path: Path) -> None:
    repo = eawf_repo(tmp_path / "repo")

    results = capture_runner(repo, answer=None).run_event(subagent_stop(tmp_path))

    assert "ConnectionRefusedError" in result_named(results, "runtime.host_subagent").output
    (row,) = read_transcript_index(repo / ".ea")
    assert row.run_ref is None
    assert row.parent_session_digest == hash_vendor_session_id(HOST_SESSION)


def test_a_subagent_start_names_no_transcript_and_writes_nothing(tmp_path: Path) -> None:
    repo = eawf_repo(tmp_path / "repo")
    start = host_event(
        HookEventType.SUBAGENT_START,
        {"session_id": HOST_SESSION, "agent_id": AGENT_ID},
        runtime="claude",
    )

    capture_runner(repo, answer={"run_ref": str(RUN_URN)}).run_event(start)

    assert read_transcript_index(repo / ".ea") == ()


# ---- a hook whose row cannot be written ----------------------------------------


def _corrupt_index(repo: Path) -> None:
    path = transcript_index_path(repo / ".ea")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"runtime": "codex"}\n', encoding="utf-8")


def _session_start(tmp_path: Path) -> HookEvent:
    return host_event(
        HookEventType.SESSION_START,
        {"session_id": HOST_SESSION, "transcript_path": str(tmp_path / "s.jsonl")},
        runtime="claude",
    )


def test_a_session_hook_over_a_corrupt_index_names_why_it_skipped(tmp_path: Path) -> None:
    repo = eawf_repo(tmp_path / "repo")
    _corrupt_index(repo)

    results = capture_runner(repo).run_event(_session_start(tmp_path))

    output = result_named(results, SESSION_INDEX_HOOK).output
    assert output.startswith(f"{SESSION_INDEX_HOOK} skipped: ValidationError: ")
    assert not any(result.block for result in results)


@pytest.mark.parametrize(
    "error",
    [StateConflict("lock held", kind="LockConflict"), PermissionError("read-only index")],
    ids=["lock", "oserror"],
)
def test_a_session_hook_whose_append_fails_names_why_it_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    repo = eawf_repo(tmp_path / "repo")

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(lifecycle_capture, "record_transcript", refuse)

    results = capture_runner(repo).run_event(_session_start(tmp_path))

    output = result_named(results, SESSION_INDEX_HOOK).output
    assert output == f"{SESSION_INDEX_HOOK} skipped: {type(error).__name__}: {error}"


def test_a_session_hook_whose_binding_fails_keeps_its_row_and_names_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = eawf_repo(tmp_path / "repo")

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise StateConflict("binding lock held", kind="LockConflict")

    monkeypatch.setattr(
        lifecycle_capture, "checked_out_branch", lambda cwd: "feature/eawf-v0.7-eawf-0646"
    )
    monkeypatch.setattr(lifecycle_capture, "bind_session_to_branch_task", refuse)

    results = capture_runner(repo).run_event(_session_start(tmp_path))

    assert result_named(results, SESSION_INDEX_HOOK).output == (
        f"{SESSION_INDEX_HOOK} ok; binding skipped: StateConflict: binding lock held"
    )
    assert len(read_transcript_index(repo / ".ea")) == 1


def test_a_subagent_stop_over_a_corrupt_index_names_why_it_skipped(tmp_path: Path) -> None:
    repo = eawf_repo(tmp_path / "repo")
    _corrupt_index(repo)

    results = capture_runner(repo, answer={"run_ref": str(RUN_URN)}).run_event(
        subagent_stop(tmp_path)
    )

    output = result_named(results, "runtime.host_subagent").output
    assert output.startswith(
        f"runtime.host_subagent ok run={RUN_URN}; index skipped: ValidationError: "
    )


# ---- back-fill ---------------------------------------------------------------


@pytest.fixture
def planted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Plant Claude Code and Codex transcript files where back-fill looks for them."""
    projects = tmp_path / "claude" / "projects"
    project = projects / "-work-repo"
    subagents = project / HOST_SESSION / "subagents"
    subagents.mkdir(parents=True)
    codex_day = tmp_path / "codex" / "sessions" / "2026" / "10" / "30"
    codex_day.mkdir(parents=True)
    files = {
        "host": project / f"{HOST_SESSION}.jsonl",
        "other": project / "ffffffff-0000-4000-8000-000000000000.jsonl",
        "subagent": subagents / f"agent-{AGENT_ID}.jsonl",
        "codex": codex_day / f"rollout-2026-10-30T12-00-00-{CODEX_THREAD}.jsonl",
    }
    for path in files.values():
        path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv(PROJECTS_ENV, str(projects))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return files


def session(harness: str, raw_id: str) -> VendorSessionRef:
    """Return the vendor session a Run records for *raw_id*."""
    return VendorSessionRef(harness=harness, session_digest=raw_id)


def test_backfill_matches_each_run_to_its_file_by_digest(
    tmp_path: Path, planted: dict[str, Path]
) -> None:
    tree = tmp_path / "repo" / ".ea"
    sessions = [
        ("urn:run:host", session("claude-code", HOST_SESSION)),
        ("urn:run:subagent", session("claude-code", AGENT_ID)),
        ("urn:run:codex", session("codex", CODEX_THREAD)),
        ("urn:run:lost", session("claude-code", "no-such-session")),
    ]

    recorded = backfill_transcript_index(tree, sessions, now=AT)

    rows = {row.run_ref: row for row in read_transcript_index(tree)}
    assert recorded == 3
    assert set(rows) == {"urn:run:host", "urn:run:subagent", "urn:run:codex"}
    assert rows["urn:run:host"].transcript_path == str(planted["host"])
    assert rows["urn:run:subagent"].transcript_path == str(planted["subagent"])
    assert rows["urn:run:subagent"].parent_session_digest == hash_vendor_session_id(HOST_SESSION)
    assert rows["urn:run:codex"].transcript_path == str(planted["codex"])
    assert rows["urn:run:codex"].runtime == "codex"
    assert str(planted["other"]) not in {row.transcript_path for row in rows.values()}


def _run_home(tree: Path, handle: str, dirname: str) -> Path:
    return tree / WORKSPACE_LOCATOR / handle / MCP_ARTIFACT_DIRNAME / dirname


def test_backfill_finds_a_dispatched_run_s_file_in_its_own_home(
    tmp_path: Path, planted: dict[str, Path]
) -> None:
    """A dispatched Run's runtime writes under the Run's home, not the operator's."""
    tree = tmp_path / "repo" / ".ea"
    run_session = "2c8f3e3f-3333-4444-8555-b66677778888"
    run_thread = "0199a3c2-7d4e-7f10-8a2b-ffffffffffff"
    claude_home = _run_home(tree, "wsh-one", MANAGED_CONFIG_DIRNAME)
    claude_file = claude_home / "projects" / "-leased-workspace" / f"{run_session}.jsonl"
    subagent_file = (
        claude_home / "projects" / "-leased-workspace" / run_session / "subagents"
        / f"agent-{AGENT_ID}.jsonl"
    )  # fmt: skip
    codex_file = (
        _run_home(tree, "wsh-two", CODEX_HOME_DIRNAME) / "sessions" / "2026" / "10" / "30"
        / f"rollout-2026-10-30T12-00-00-{run_thread}.jsonl"
    )  # fmt: skip
    for path in (claude_file, subagent_file, codex_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    sessions = [
        ("urn:run:claude", session("claude-code", run_session)),
        ("urn:run:child", session("claude-code", AGENT_ID)),
        ("urn:run:codex", session("codex", run_thread)),
        ("urn:run:host", session("claude-code", HOST_SESSION)),
    ]

    assert backfill_transcript_index(tree, sessions, now=AT) == 5

    paths: dict[str, set[str]] = {}
    for row in read_transcript_index(tree):
        paths.setdefault(str(row.run_ref), set()).add(row.transcript_path)
    assert paths["urn:run:claude"] == {str(claude_file)}
    assert paths["urn:run:codex"] == {str(codex_file)}
    assert paths["urn:run:host"] == {str(planted["host"])}
    # The operator's home holds a subagent of the same id: both files are indexed.
    assert paths["urn:run:child"] == {str(planted["subagent"]), str(subagent_file)}


def test_backfill_with_no_leased_workspace_reads_only_the_operator_s_home(
    tmp_path: Path, planted: dict[str, Path]
) -> None:
    """Boundary: a tree with no workspaces directory back-fills from the operator's home."""
    tree = tmp_path / "repo" / ".ea"
    sessions = [("urn:run:host", session("claude-code", HOST_SESSION))]

    assert backfill_transcript_index(tree, sessions, now=AT) == 1
    assert not (tree / WORKSPACE_LOCATOR).exists()


def test_backfill_reads_the_index_once_however_many_rows_it_appends(
    tmp_path: Path, planted: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "repo" / ".ea"
    record_transcript(tree, runtime="codex", session_id="held", transcript_path="p", now=AT)
    reads: list[Path] = []
    read = transcript_index.read_transcript_index

    def counted(root: Path) -> tuple[TranscriptIndexRow, ...]:
        reads.append(root)
        return read(root)

    monkeypatch.setattr(transcript_index, "read_transcript_index", counted)
    sessions = [
        ("urn:run:host", session("claude-code", HOST_SESSION)),
        ("urn:run:subagent", session("claude-code", AGENT_ID)),
        ("urn:run:codex", session("codex", CODEX_THREAD)),
    ]

    assert backfill_transcript_index(tree, sessions, now=AT) == 3
    assert reads == [tree]
    assert len(read(tree)) == 4


def test_backfill_skips_a_run_already_indexed(tmp_path: Path, planted: dict[str, Path]) -> None:
    tree = tmp_path / "repo" / ".ea"
    sessions = [("urn:run:host", session("claude-code", HOST_SESSION))]

    assert backfill_transcript_index(tree, sessions, now=AT) == 1
    assert backfill_transcript_index(tree, sessions, now=AT) == 0
    assert len(read_transcript_index(tree)) == 1


def test_backfill_with_no_runs_writes_nothing(tmp_path: Path, planted: dict[str, Path]) -> None:
    tree = tmp_path / "repo" / ".ea"

    assert backfill_transcript_index(tree, [], now=AT) == 0
    assert not transcript_index_path(tree).exists()


def test_backfill_over_absent_transcript_directories_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROJECTS_ENV, str(tmp_path / "absent"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent"))
    tree = tmp_path / "repo" / ".ea"
    sessions = [
        ("urn:run:a", session("claude-code", HOST_SESSION)),
        ("urn:run:b", session("codex", CODEX_THREAD)),
    ]

    assert backfill_transcript_index(tree, sessions, now=AT) == 0


def test_the_daemon_backfills_a_root_run_by_its_recorded_session(
    tmp_path: Path, planted: dict[str, Path]
) -> None:
    sessioned = run_row()
    sessioned["vendor_session"] = session("claude-code", HOST_SESSION).model_dump(mode="json")
    canary = make_canary(tmp_path / "canary", rows={sessioned["key"]: sessioned})
    context = method_ctx(tmp_path / "runtime").native_root_context(canary.root / ".ea")

    assert backfill_root_transcripts(context, now=AT) == 1

    (row,) = read_transcript_index(canary.root / ".ea")
    assert row.run_ref == sessioned["urn"]
    assert row.transcript_path == str(planted["host"])


# ---- the row and its file ----------------------------------------------------


def row_fields(**overrides: Any) -> dict[str, Any]:
    """Return the fields of one valid row, with *overrides* applied."""
    fields: dict[str, Any] = {
        "runtime": "claude-code",
        "vendor_session_digest": HOST_SESSION,
        "transcript_path": "session.jsonl",
        "recorded_at": AT,
    }
    fields.update(overrides)
    return fields


def test_a_row_hashes_a_raw_session_id_and_keeps_a_hashed_one() -> None:
    digest = hash_vendor_session_id(HOST_SESSION)

    assert TranscriptIndexRow.model_validate(row_fields()).vendor_session_digest == digest
    assert (
        TranscriptIndexRow.model_validate(
            row_fields(vendor_session_digest=digest)
        ).vendor_session_digest
        == digest
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"unexpected": True},
        {"runtime": "opencode"},
        {"vendor_session_digest": ""},
        {"vendor_session_digest": 7},
        {"transcript_path": ""},
        {"run_ref": ""},
        {"recorded_at": "not a time"},
    ],
)
def test_a_row_refuses_fields_outside_its_schema(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TranscriptIndexRow.model_validate(row_fields(**overrides))


def test_a_row_missing_its_session_is_refused() -> None:
    fields = row_fields()
    del fields["vendor_session_digest"]

    with pytest.raises(ValidationError):
        TranscriptIndexRow.model_validate(fields)


def test_record_refuses_an_unknown_runtime_and_writes_nothing(tmp_path: Path) -> None:
    tree = tmp_path / ".ea"

    with pytest.raises(ValidationError):
        record_transcript(tree, runtime="opencode", session_id="s", transcript_path="p", now=AT)
    assert not transcript_index_path(tree).exists()


def test_an_absent_index_reads_empty_and_a_corrupt_line_is_refused(tmp_path: Path) -> None:
    tree = tmp_path / ".ea"
    assert read_transcript_index(tree) == ()

    record_transcript(tree, runtime="codex", session_id="s", transcript_path="p", now=AT)
    with transcript_index_path(tree).open("a", encoding="utf-8") as handle:
        handle.write('{"runtime": "codex"}\n')

    with pytest.raises(ValidationError):
        read_transcript_index(tree)


# ---- the index stays out of git ----------------------------------------------


def test_the_index_path_is_ignored_by_the_init_gitignore_block(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    write_gitignore(tmp_path)
    index = transcript_index_path(tmp_path / ".ea").relative_to(tmp_path)

    ignored = subprocess.run(
        ["git", "check-ignore", "-q", index.as_posix()], cwd=tmp_path, check=False
    )

    assert ignored.returncode == 0
