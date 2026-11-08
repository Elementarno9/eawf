"""Side-by-side marked subagents end as Task-scoped Runs through the host's real hook shape.

Claude Code sends a SubagentStart with no prompt and creates the subagent's
transcript only after that hook answers, so the ``[eawf task=KEY]`` marker is first
readable at the SubagentStop, which names the transcript. Five subagents spawned at
once deliver their starts together and their stops together, and each delivery
contends for the one document lock that every adoption takes several times.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.state.epoch2.run import Run, RunPurpose, RunStatus, TaskScope
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import host_subagent
from eawf.runtime.daemon.native_dispatch import run_ledger, stored_run
from eawf.runtime.hooks.event import HookEvent
from eawf.runtime.hooks.health import HookFailure
from eawf.runtime.hooks.lifecycle_capture import adopt_host_subagent
from eawf.runtime.hooks.runner import HookResult, HookRunner, register_runtime_capture_hooks
from eawf.runtime.runtimes.claude.hooks_router import route_claude_payload
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    document_path,
    method_context,
    provision,
    rekeyed,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

REPOSITORY_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
TASK_CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task"
HOST_SESSION: Final = "2375c975-1749-41a9-a716-d2007c5c390a"
#: Five subagents, each prompted for a Task of its own, so a stop that bound a
#: neighbour's marker would show.
FANOUT: Final = {f"a{index:016x}": f"EAWF-{42 + index:04d}" for index in range(5)}
#: How long a slowed Run read holds the document lock, past EA_LOCK_TIMEOUT, as the
#: live tree's 20 MB run ledger does.
LOCKED_READ_SECONDS: Final = 0.3


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
    """A canary admitting one repository and holding one planned Task per subagent."""
    provisioned = provision(tmp_path / "repo", code="FANOUT")
    tasks = {key: rekeyed(seed_row("task", "PLANNED"), key=key) for key in FANOUT.values()}
    seed(
        provisioned,
        {
            Epoch2Collection.REPOSITORY.value: {"REP-EAWF": repository_row()},
            Epoch2Collection.TASK.value: tasks,
        },
    )
    return provisioned


class _DaemonClient:
    """The hook's daemon client, answering through the daemon's own dispatch."""

    def __init__(self, runtime: Path) -> None:
        self._runtime = runtime

    def __enter__(self) -> _DaemonClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        ctx = method_context(self._runtime)
        ctx.bus = EventBus()
        return asyncio.run(methods.dispatch(method, ctx, params))


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


def session_transcript(tmp_path: Path) -> Path:
    """Return the spawning session's transcript path, as Claude lays it out."""
    return tmp_path / "projects" / f"{HOST_SESSION}.jsonl"


def subagent_transcript(tmp_path: Path, agent_id: str) -> Path:
    """Return where Claude writes a subagent's transcript, beside the session's."""
    return session_transcript(tmp_path).with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"


def write_prompt(path: Path, agent_id: str, task_key: str) -> Path:
    """Write a subagent transcript whose first record is a prompt marked for *task_key*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f"[eawf task={task_key}] Reply with the single word OK."
    record = {"type": "user", "agentId": agent_id, "message": {"role": "user", "content": content}}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def start_payload(tmp_path: Path, agent_id: str) -> dict[str, Any]:
    """Return a SubagentStart stdin carrying exactly the keys Claude Code sends."""
    return {
        "session_id": HOST_SESSION,
        "transcript_path": str(session_transcript(tmp_path)),
        "cwd": str(tmp_path / "repo"),
        "prompt_id": f"prompt-{agent_id}",
        "agent_id": agent_id,
        "agent_type": "general-purpose",
        "hook_event_name": "SubagentStart",
    }


def stop_payload(tmp_path: Path, agent_id: str) -> dict[str, Any]:
    """Return a SubagentStop stdin naming the subagent's transcript."""
    return {
        "session_id": HOST_SESSION,
        "transcript_path": str(session_transcript(tmp_path)),
        "cwd": str(tmp_path / "repo"),
        "agent_id": agent_id,
        "agent_type": "general-purpose",
        "agent_transcript_path": str(subagent_transcript(tmp_path, agent_id)),
        "stop_hook_active": False,
        "hook_event_name": "SubagentStop",
    }


def routed(payload: dict[str, Any]) -> HookEvent:
    """Return the event the Claude hook router makes of a raw stdin payload."""
    event = route_claude_payload(payload)
    assert event is not None, payload
    return event


def deliver(
    canary: CanaryProvision, runtime: Path, payload: dict[str, Any]
) -> tuple[HookResult, list[HookFailure]]:
    """Run one hook process's dispatch of *payload*: its answer and its health failures."""
    runner = HookRunner()
    register_runtime_capture_hooks(
        runner, daemon_client_factory=lambda: _DaemonClient(runtime), repo_root=canary.root
    )
    (result,) = runner.run_event(routed(payload))
    return result, runner.failures


def deliver_together(
    canary: CanaryProvision, runtime: Path, payloads: list[dict[str, Any]]
) -> list[tuple[HookResult, list[HookFailure]]]:
    """Deliver *payloads* from one thread each, released at the same instant."""
    gate = threading.Barrier(len(payloads))

    def one(payload: dict[str, Any]) -> tuple[HookResult, list[HookFailure]]:
        gate.wait()
        return deliver(canary, runtime, payload)

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        return list(pool.map(one, payloads))


def latest_runs(canary: CanaryProvision) -> dict[str, Run]:
    """Return each Run's latest record, from the document or the run ledger."""
    path = document_path(canary)
    latest = {
        key: Run.model_validate(row)
        for key, row in document_rows(read_document(path), Epoch2Collection.RUN).items()
    }
    for record in read_ledger_records(ledger_path(path, Epoch2Collection.RUN)):
        if "scope" in record.payload:
            latest[record.record_key] = Run.model_validate(record.payload)
    return latest


# ---- CR-790: five contended marked subagents end Task-scoped and completed ---------


def test_five_marked_subagents_end_as_task_scoped_runs(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five starts with no transcript, then five marked stops, all at once under contention."""
    monkeypatch.setenv("EA_LOCK_TIMEOUT", "0.2")

    def slow_locked_read(context: Epoch2RootContext, urn: QualifiedUrn) -> Run:
        with context.session([urn]) as session:
            time.sleep(LOCKED_READ_SECONDS)
            return stored_run(session, read_ledger_records(run_ledger(session)), urn)

    monkeypatch.setattr(host_subagent, "_read_run", slow_locked_read)
    runtime = tmp_path / "runtime"

    starts = deliver_together(canary, runtime, [start_payload(tmp_path, a) for a in FANOUT])
    assert not any(subagent_transcript(tmp_path, a).exists() for a in FANOUT)
    for agent_id, task_key in FANOUT.items():
        write_prompt(subagent_transcript(tmp_path, agent_id), agent_id, task_key)
    stops = deliver_together(canary, runtime, [stop_payload(tmp_path, a) for a in FANOUT])

    for result, failures in [*starts, *stops]:
        assert result.output.startswith("runtime.host_subagent ok run=eawf://"), result.output
        assert failures == []
    stopped = {
        agent_id: result.output.split(" ok run=", 1)[1].split(";", 1)[0]
        for agent_id, (result, _) in zip(FANOUT, stops, strict=True)
    }
    for agent_id, (result, _) in zip(FANOUT, starts, strict=True):
        assert result.output.split(" ok run=", 1)[1] == stopped[agent_id]
    runs = latest_runs(canary)
    assert len(runs) == 5
    assert {run.status for run in runs.values()} == {RunStatus.COMPLETED}
    by_ref = {str(run.urn): run for run in runs.values()}
    for agent_id, task_key in FANOUT.items():
        run = by_ref[stopped[agent_id]]
        assert isinstance(run.scope, TaskScope)
        assert str(run.scope.task_ref) == f"{TASK_CONTAINER}/{task_key}"
        assert run.scope.purpose is RunPurpose.OBSERVE


# ---- CR-791: the marker is read at the stop, never at the start ---------------------


def sent_params(tmp_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Return the parameters the hook sends the daemon for one raw payload."""
    recorder = _Recorder()
    adopt_host_subagent(routed(payload), daemon_client_factory=lambda: recorder, repo_root=tmp_path)
    (params,) = recorder.params
    return params


def test_the_marker_is_read_at_the_stop(tmp_path: Path) -> None:
    """A start before the transcript exists sends no Task; the stop naming it sends KEY."""
    agent_id, task_key = next(iter(FANOUT.items()))
    session_transcript(tmp_path).parent.mkdir(parents=True)
    session_transcript(tmp_path).write_text("", encoding="utf-8")

    at_start = sent_params(tmp_path, start_payload(tmp_path, agent_id))
    write_prompt(subagent_transcript(tmp_path, agent_id), agent_id, task_key)
    at_stop = sent_params(tmp_path, stop_payload(tmp_path, agent_id))

    assert "task_ref" not in at_start
    assert "transcript_path" not in at_start
    assert at_stop["task_ref"] == task_key
    assert at_stop["transcript_path"] == str(subagent_transcript(tmp_path, agent_id))
    assert at_start["agent_id"] == at_stop["agent_id"] == agent_id
    assert at_start["host_session_id"] == at_stop["host_session_id"] == HOST_SESSION


def test_the_marker_is_not_read_off_a_neighbours_transcript(tmp_path: Path) -> None:
    """A stop naming an unmarked or missing transcript sends no Task, whatever a neighbour says."""
    (first, first_key), (second, _), (third, _) = list(FANOUT.items())[:3]
    write_prompt(subagent_transcript(tmp_path, first), first, first_key)
    unmarked = subagent_transcript(tmp_path, second)
    unmarked.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "OK?"}}) + "\n",
        encoding="utf-8",
    )

    assert "task_ref" not in sent_params(tmp_path, stop_payload(tmp_path, second))
    assert "task_ref" not in sent_params(tmp_path, stop_payload(tmp_path, third))
