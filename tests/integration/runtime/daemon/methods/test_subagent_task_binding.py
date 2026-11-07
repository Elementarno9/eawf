"""A host subagent's Run is bound to the Task its prompt names, and keys never collide.

The subagent hooks lift a ``[eawf task=KEY]`` marker off the subagent's prompt, which
Claude's subagent events do not carry, so it is read off the subagent's own
transcript. The start verb scopes the Run to that Task when the tree holds it; it
decides the Run's key under the document lock, so parallel subagents never pick the
same one; and a Task binds only to a queued or running Run scoped to it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.state.epoch2.run import RepositoryScope, Run, RunPurpose, RunStatus, TaskScope
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.transitions import TransitionGuard
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, host_subagent
from eawf.runtime.daemon.methods.domain_guards import GUARD_COMPUTERS, GuardInputs
from eawf.runtime.daemon.methods.host_subagent import (
    HOST_SUBAGENT_START_METHOD,
    HOST_SUBAGENT_STOP_METHOD,
)
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.lifecycle_capture import adopt_host_subagent
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    TASK_URN,
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

REPOSITORY_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
TASK_KEY: Final = "EAWF-0042"
OTHER_TASK_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0043"
RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
AGENT_ID: Final = "a0a45f67519cb14dd"
HOST_SESSION: Final = "30f683b4-388a-4c04-89f4-612b7fe60362"
_LOGGER: Final = "eawf.runtime.daemon.methods.host_subagent"


def repository_row() -> dict[str, Any]:
    """Return the one Repository row a single-repository tree admits."""
    return {
        "key": "REP-EAWF",
        "urn": REPOSITORY_URN,
        "revision": 1,
        "head_sha": "a" * 40,
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
    }


@pytest.fixture(autouse=True)
def hermetic_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep canary runtime dirs and Claude transcript discovery under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary admitting one repository and holding one planned Task."""
    provisioned = provision(tmp_path / "repo", code="BIND")
    seed(
        provisioned,
        {
            Epoch2Collection.REPOSITORY.value: {"REP-EAWF": repository_row()},
            Epoch2Collection.TASK.value: {TASK_KEY: seed_row("task", "PLANNED")},
        },
    )
    return provisioned


def dispatch(runtime: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run one verb as the daemon would, on a context of its own."""
    ctx = method_context(runtime)
    ctx.bus = EventBus()
    return asyncio.run(methods.dispatch(method, ctx, params))


class _DaemonClient:
    """The hook's daemon client, answering through the daemon's own dispatch."""

    def __init__(self, runtime: Path) -> None:
        self._runtime = runtime

    def __enter__(self) -> _DaemonClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return dispatch(self._runtime, method, params)


def start(canary: CanaryProvision, runtime: Path, agent_id: str, **params: Any) -> dict[str, Any]:
    """Dispatch one subagent start against the canary."""
    return dispatch(
        runtime,
        HOST_SUBAGENT_START_METHOD,
        {"repo_root": str(canary.root), "harness": "claude-code", "agent_id": agent_id, **params},
    )


def runs(canary: CanaryProvision) -> dict[str, dict[str, Any]]:
    return document_rows(read_document(document_path(canary)), Epoch2Collection.RUN)


def run_of(canary: CanaryProvision, run_ref: str) -> Run:
    return Run.model_validate(runs(canary)[run_ref.rsplit("/", 1)[1]])


def session_transcript(tmp_path: Path) -> Path:
    """Return the spawning session's transcript path, as Claude lays it out."""
    return tmp_path / "projects" / f"{HOST_SESSION}.jsonl"


def write_prompt(path: Path, content: Any) -> Path:
    """Write a subagent transcript whose first record is the prompt *content*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"type": "user", "agentId": AGENT_ID, "message": {"role": "user", "content": content}}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def subagent_transcript(tmp_path: Path) -> Path:
    """Return where Claude keeps the subagent's transcript, beside the session's."""
    return session_transcript(tmp_path).with_suffix("") / "subagents" / f"agent-{AGENT_ID}.jsonl"


def hook_event(event_type: HookEventType, runtime: str = "claude", **payload: Any) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        scope_id="",
        command="",
        args={},
        runtime=runtime,  # type: ignore[arg-type]
        occurred_at=AT,
        payloads={event_type.value: payload},
    )


class _Recorder:
    """A daemon client that records the parameters and answers with no Run."""

    def __init__(self) -> None:
        self.params: list[dict[str, Any]] = []

    def __enter__(self) -> _Recorder:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, _method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.params.append(params)
        return {}


def sent_task_ref(tmp_path: Path, event: HookEvent) -> str | None:
    """Return the task_ref the hook sends the daemon for *event*, or ``None``."""
    recorder = _Recorder()
    adopt_host_subagent(event, daemon_client_factory=lambda: recorder, repo_root=tmp_path)
    (params,) = recorder.params
    return params.get("task_ref")


# ---- CR-710: the prompt marker scopes the adopted Run to its Task ----------------


@pytest.mark.parametrize(
    ("prompt", "scoped_to_task", "warnings"),
    [
        pytest.param(f"[eawf task={TASK_KEY}] Implement it.", True, 0, id="marker"),
        pytest.param("Implement it.", False, 0, id="no-marker"),
        pytest.param("[eawf task=EAWF-9999] Implement it.", False, 1, id="unknown-key"),
    ],
)
def test_marker_binds_task(
    canary: CanaryProvision,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    prompt: str,
    scoped_to_task: bool,
    warnings: int,
) -> None:
    """A real SubagentStart payload names no prompt; the marker is read off the transcript."""
    write_prompt(subagent_transcript(tmp_path), prompt)
    event = hook_event(
        HookEventType.SUBAGENT_START,
        agent_id=AGENT_ID,
        agent_type="general-purpose",
        session_id=HOST_SESSION,
        transcript_path=str(session_transcript(tmp_path)),
        hook_event_name="SubagentStart",
    )
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    result = adopt_host_subagent(
        event,
        daemon_client_factory=lambda: _DaemonClient(tmp_path / "runtime"),
        repo_root=canary.root,
    )

    assert result.output.startswith("runtime.host_subagent ok run=eawf://"), result.output
    ((key, row),) = runs(canary).items()
    run = Run.model_validate(row)
    assert run.status is RunStatus.RUNNING
    if scoped_to_task:
        assert isinstance(run.scope, TaskScope)
        assert str(run.scope.task_ref) == TASK_URN
    else:
        assert isinstance(run.scope, RepositoryScope)
        assert str(run.scope.repository_ref) == REPOSITORY_URN
    assert run.scope.purpose is RunPurpose.OBSERVE
    warned = [r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING]
    assert len(warned) == warnings
    assert all(f"run={key}" in r.getMessage() for r in warned)


@pytest.mark.parametrize(
    ("status", "scoped_to_task"),
    [
        pytest.param("PLANNED", True, id="planned"),
        pytest.param("CLAIMED", True, id="claimed"),
        pytest.param("RUNNING", True, id="running"),
        pytest.param("READY_TO_INTEGRATE", True, id="ready-to-integrate"),
        pytest.param("COMPLETED", False, id="completed"),
        pytest.param("CANCELLED", False, id="cancelled"),
        pytest.param("FAILED", False, id="failed"),
    ],
)
def test_marker_binds_only_an_open_task(
    canary: CanaryProvision, tmp_path: Path, status: str, scoped_to_task: bool
) -> None:
    """A marker naming a Task in a terminal state adopts the Run repository-scoped."""
    seed(canary, {Epoch2Collection.TASK.value: {TASK_KEY: seed_row("task", status)}})
    write_prompt(subagent_transcript(tmp_path), f"[eawf task={TASK_KEY}] Implement it.")
    event = hook_event(
        HookEventType.SUBAGENT_START,
        agent_id=AGENT_ID,
        session_id=HOST_SESSION,
        transcript_path=str(session_transcript(tmp_path)),
    )

    result = adopt_host_subagent(
        event,
        daemon_client_factory=lambda: _DaemonClient(tmp_path / "runtime"),
        repo_root=canary.root,
    )

    assert " ok run=" in result.output, result.output
    ((_, row),) = runs(canary).items()
    run = Run.model_validate(row)
    assert run.scope.purpose is RunPurpose.OBSERVE
    if scoped_to_task:
        assert isinstance(run.scope, TaskScope)
        assert str(run.scope.task_ref) == TASK_URN
    else:
        assert isinstance(run.scope, RepositoryScope)
        assert str(run.scope.repository_ref) == REPOSITORY_URN
        assert "task_ref" not in row["scope"]


def test_marker_binds_task_on_a_stop_whose_start_was_lost(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """A stop adopting the subagent first reads the marker off the transcript it names."""
    transcript = write_prompt(tmp_path / "agent.jsonl", f"[eawf task={TASK_KEY}] go")
    event = hook_event(
        HookEventType.SUBAGENT_STOP,
        agent_id=AGENT_ID,
        session_id=HOST_SESSION,
        agent_transcript_path=str(transcript),
    )

    result = adopt_host_subagent(
        event,
        daemon_client_factory=lambda: _DaemonClient(tmp_path / "runtime"),
        repo_root=canary.root,
    )

    assert " ok run=" in result.output, result.output
    key = result.output.rsplit("/", 1)[1]
    ledger = read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))
    rows = [r.payload for r in ledger if r.record_key == key and "scope" in r.payload]
    run = Run.model_validate(rows[-1])
    assert run.status is RunStatus.COMPLETED
    assert isinstance(run.scope, TaskScope)
    assert str(run.scope.task_ref) == TASK_URN


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(f"[eawf task={TASK_KEY}] x", TASK_KEY, id="string"),
        pytest.param("[eawf task=EAWF-0007] go", "EAWF-0007", id="leading"),
        pytest.param("  \t[eawf task=EAWF-0007] go", "EAWF-0007", id="leading-whitespace"),
        pytest.param(
            [{"type": "text", "text": "[eawf task=AB-0001]"}, {"type": "text", "text": "go"}],
            "AB-0001",
            id="blocks",
        ),
        pytest.param(
            [{"type": "text", "text": "go"}, {"type": "text", "text": "[eawf task=AB-0001]"}],
            None,
            id="blocks-trailing",
        ),
        pytest.param("body [eawf task=EAWF-0007] later", None, id="mid-prompt"),
        pytest.param("Implement it.\n[eawf task=EAWF-0007]", None, id="second-line"),
        pytest.param("\n[eawf task=EAWF-0007] go", None, id="blank-first-line"),
        pytest.param("[eawf task=eawf-0042]", None, id="lower-case"),
        pytest.param("[eawf task=EAWF-42]", None, id="short-ordinal"),
        pytest.param("[eawf task=]", None, id="empty-key"),
        pytest.param("", None, id="empty-prompt"),
        pytest.param({"text": "[eawf task=EAWF-0042]"}, None, id="not-a-prompt"),
    ],
)
def test_marker_read_off_the_transcript(tmp_path: Path, content: Any, expected: str | None) -> None:
    """Only a well-formed marker in the first record's text is lifted."""
    write_prompt(subagent_transcript(tmp_path), content)
    event = hook_event(
        HookEventType.SUBAGENT_START,
        agent_id=AGENT_ID,
        transcript_path=str(session_transcript(tmp_path)),
    )

    assert sent_task_ref(tmp_path, event) == expected


def test_marker_unreadable_transcript_sends_no_task(tmp_path: Path) -> None:
    """A missing or corrupt transcript leaves the Run unscoped instead of failing the hook."""
    missing = hook_event(
        HookEventType.SUBAGENT_START,
        agent_id=AGENT_ID,
        transcript_path=str(session_transcript(tmp_path)),
    )
    assert sent_task_ref(tmp_path, missing) is None
    subagent_transcript(tmp_path).parent.mkdir(parents=True)
    subagent_transcript(tmp_path).write_text("{not json\n", encoding="utf-8")
    assert sent_task_ref(tmp_path, missing) is None
    no_paths = hook_event(HookEventType.SUBAGENT_START, agent_id=AGENT_ID)
    assert sent_task_ref(tmp_path, no_paths) is None


def test_marker_payload_prompt_is_read_first(tmp_path: Path) -> None:
    """A harness that hands the prompt over directly is read without a transcript."""
    codex = hook_event(
        HookEventType.SUBAGENT_START, "codex", agent_id=AGENT_ID, prompt="[eawf task=AB-0002]"
    )
    assert sent_task_ref(tmp_path, codex) == "AB-0002"
    write_prompt(subagent_transcript(tmp_path), f"[eawf task={TASK_KEY}]")
    codex_no_prompt = hook_event(
        HookEventType.SUBAGENT_START,
        "codex",
        agent_id=AGENT_ID,
        transcript_path=str(session_transcript(tmp_path)),
    )
    assert sent_task_ref(tmp_path, codex_no_prompt) is None


@pytest.mark.parametrize(
    "task_ref",
    [
        pytest.param("eawf-0042", id="lower-case"),
        pytest.param("EAWF-42", id="short-ordinal"),
        pytest.param("", id="empty"),
        pytest.param(42, id="not-a-string"),
    ],
)
def test_marker_malformed_task_ref_is_refused(
    canary: CanaryProvision, tmp_path: Path, task_ref: Any
) -> None:
    """The verb refuses a task_ref that is not a Task key, writing nothing."""
    with pytest.raises(DaemonValidationError):
        start(canary, tmp_path / "runtime", AGENT_ID, task_ref=task_ref)
    assert runs(canary) == {}


# ---- CR-711: parallel adoptions decide distinct keys under the lock --------------


def test_parallel_adopt(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seven subagents starting at once are adopted as seven Runs under seven keys.

    The unlocked read is slowed so every adoption reads the tree before any
    commits, which is what a large live document makes of parallel starts.
    """

    def slow_read(path: Path) -> dict[str, Any]:
        document = read_document(path)
        time.sleep(0.3)
        return document

    monkeypatch.setattr(host_subagent, "read_document", slow_read)
    agents = [f"a{index:016x}" for index in range(7)]
    with ThreadPoolExecutor(max_workers=7) as pool:
        answers = list(pool.map(lambda agent: start(canary, tmp_path / "runtime", agent), agents))

    assert len({answer["run_ref"] for answer in answers}) == 7
    rows = runs(canary)
    assert len(rows) == 7
    assert {row["status"] for row in rows.values()} == {RunStatus.RUNNING.value}


@pytest.mark.parametrize(
    "deliveries",
    [
        pytest.param((HOST_SUBAGENT_START_METHOD, HOST_SUBAGENT_START_METHOD), id="start-start"),
        pytest.param((HOST_SUBAGENT_START_METHOD, HOST_SUBAGENT_STOP_METHOD), id="start-stop"),
        pytest.param((HOST_SUBAGENT_STOP_METHOD, HOST_SUBAGENT_STOP_METHOD), id="stop-stop"),
    ],
)
def test_parallel_adopt_repro_same_subagent_refused(
    canary: CanaryProvision,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deliveries: tuple[str, str],
) -> None:
    """Two deliveries for one subagent racing each other adopt one Run and both answer.

    The slowed unlocked reads let both read the tree, and then the Run, before the
    other commits, so each decides its create and its edges after the other wrote.
    """

    def slow_read(path: Path) -> dict[str, Any]:
        document = read_document(path)
        time.sleep(0.3)
        return document

    read_run = host_subagent._read_run

    def slow_read_run(context: Any, urn: Any) -> Run:
        run = read_run(context, urn)
        time.sleep(0.3)
        return run

    monkeypatch.setattr(host_subagent, "read_document", slow_read)
    monkeypatch.setattr(host_subagent, "_read_run", slow_read_run)
    params = {"repo_root": str(canary.root), "harness": "claude-code", "agent_id": AGENT_ID}
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(
            pool.map(lambda method: dispatch(tmp_path / "runtime", method, params), deliveries)
        )

    assert answers[0]["run_ref"] == answers[1]["run_ref"]
    ledger = read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))
    keys = set(runs(canary)) | {record.record_key for record in ledger}
    assert keys == {answers[0]["run_ref"].rsplit("/", 1)[1]}
    for method, answer in zip(deliveries, answers, strict=True):
        if method == HOST_SUBAGENT_STOP_METHOD:
            assert answer["status"] == RunStatus.COMPLETED.value
        elif HOST_SUBAGENT_STOP_METHOD not in deliveries:
            assert answer["status"] == RunStatus.RUNNING.value


def test_parallel_adopt_retried_start_replays_its_run(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """A re-delivered start answers with the Run the first one adopted."""
    first = start(canary, tmp_path / "runtime", AGENT_ID, task_ref=TASK_KEY)
    again = start(canary, tmp_path / "runtime", AGENT_ID, task_ref=TASK_KEY)

    assert again["run_ref"] == first["run_ref"]
    assert len(runs(canary)) == 1


# ---- CR-712: a Task binds only to an open Run scoped to it -------------------


def run_row(status: str, *, task_urn: str | None) -> dict[str, Any]:
    """Return the seeded Run in *status*, scoped to *task_urn* or to the repository."""
    row = seed_row("run", status)
    if task_urn is None:
        row["scope"] = {
            "scope_kind": "repository",
            "repository_ref": REPOSITORY_URN,
            "purpose": "observe",
        }
    else:
        row["scope"]["task_ref"] = task_urn
    return row


def run_bound(canary: CanaryProvision, row: dict[str, Any] | None, active_run_ref: Any) -> bool:
    """Return the run_bound verdict for the Task starting under *active_run_ref*."""
    document = read_document(document_path(canary))
    if row is not None:
        document.setdefault(Epoch2Collection.RUN.value, {})["RUN-00000010"] = row
    inputs = GuardInputs(
        document=document,
        document_path=document_path(canary),
        record=Task.model_validate(seed_row("task", "CLAIMED")),
        updates={"active_run_ref": active_run_ref},
        reason_code=None,
        actor="OP-TEST",
    )
    return GUARD_COMPUTERS[TransitionGuard.RUN_BOUND](inputs)


@pytest.mark.parametrize(
    ("row", "active_run_ref", "bound"),
    [
        pytest.param(run_row("RUNNING", task_urn=TASK_URN), RUN_URN, True, id="running-scoped"),
        pytest.param(run_row("QUEUED", task_urn=TASK_URN), RUN_URN, True, id="queued-scoped"),
        pytest.param(run_row("SUSPENDED", task_urn=TASK_URN), RUN_URN, False, id="suspended"),
        pytest.param(run_row("COMPLETED", task_urn=TASK_URN), RUN_URN, False, id="completed"),
        pytest.param(run_row("FAILED", task_urn=TASK_URN), RUN_URN, False, id="failed"),
        pytest.param(
            run_row("QUEUED", task_urn=OTHER_TASK_URN), RUN_URN, False, id="queued-foreign"
        ),
        pytest.param(
            run_row("RUNNING", task_urn=OTHER_TASK_URN), RUN_URN, False, id="foreign-task"
        ),
        pytest.param(run_row("RUNNING", task_urn=None), RUN_URN, False, id="repository-scoped"),
        pytest.param(None, RUN_URN, False, id="absent"),
        pytest.param(run_row("RUNNING", task_urn=TASK_URN), None, False, id="unnamed"),
        pytest.param(run_row("RUNNING", task_urn=TASK_URN), "", False, id="empty"),
        pytest.param(run_row("RUNNING", task_urn=TASK_URN), 7, False, id="not-a-string"),
        pytest.param(run_row("RUNNING", task_urn=TASK_URN), "RUN-00000010", False, id="bare-key"),
        pytest.param(run_row("RUNNING", task_urn=TASK_URN), TASK_URN, False, id="not-a-run"),
    ],
)
def test_run_bound_guard(
    canary: CanaryProvision, row: dict[str, Any] | None, active_run_ref: Any, bound: bool
) -> None:
    """Only a queued or running Run whose scope names the Task opens the start edge."""
    assert run_bound(canary, row, active_run_ref) is bound


def test_run_bound_guard_admits_an_adopted_subagent(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The Run a marked subagent is adopted as is one its Task may start under."""
    answer = start(canary, tmp_path / "runtime", AGENT_ID, task_ref=TASK_KEY)

    assert run_of(canary, answer["run_ref"]).status is RunStatus.RUNNING
    assert run_bound(canary, None, answer["run_ref"]) is True
