"""A host subagent's adoption waits out a held lock, finishes a lost edge, and binds at stop.

Side-by-side subagents contend for the one document lock. Every write of an
adoption is keyed by the subagent's id, so the verbs retry a lock timeout up to a
deadline and a retry replays what already landed. Past the deadline the verb names
the subagent and how long it waited. The host writes a subagent's transcript only
after its start returns, so the Task its prompt names is first known at the stop,
which binds a repository-scoped Run to that Task on its completion edge.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.epoch2.run import RepositoryScope, Run, RunPurpose, RunStatus, TaskScope
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.epoch2_transaction import run_transaction
from eawf.runtime.daemon.methods import host_subagent
from eawf.runtime.daemon.methods.host_subagent import (
    HOST_SUBAGENT_START_METHOD,
    HOST_SUBAGENT_STOP_METHOD,
)
from eawf.runtime.daemon.native_dispatch import run_ledger, stored_run
from eawf.runtime.lock.portalock import LockTimeout
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    TASK_URN,
    document_path,
    method_context,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

REPOSITORY_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
TASK_KEY: Final = "EAWF-0042"
OTHER_TASK_KEY: Final = "EAWF-0043"
AGENT_ID: Final = "ab6f6cd02b93e3de4"
OTHER_AGENT_ID: Final = "a136590a075e1ba94"


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
    """Keep canary runtime dirs under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary admitting one repository and holding two planned Tasks."""
    provisioned = provision(tmp_path / "repo", code="RETRY")
    seed(
        provisioned,
        {
            Epoch2Collection.REPOSITORY.value: {"REP-EAWF": repository_row()},
            Epoch2Collection.TASK.value: {
                TASK_KEY: seed_row("task", "PLANNED"),
                OTHER_TASK_KEY: rekeyed(seed_row("task", "PLANNED"), key=OTHER_TASK_KEY),
            },
        },
    )
    return provisioned


def call(
    canary: CanaryProvision, runtime: Path, method: str, agent_id: str, **params: Any
) -> dict[str, Any]:
    """Dispatch one adoption verb as the daemon would, on a context of its own."""
    ctx = method_context(runtime)
    ctx.bus = EventBus()
    body = {"repo_root": str(canary.root), "harness": "claude-code", "agent_id": agent_id}
    return asyncio.run(methods.dispatch(method, ctx, {**body, **params}))


def run_keys(canary: CanaryProvision) -> set[str]:
    """Return every Run key the document and the run ledger hold."""
    path = document_path(canary)
    held = set(document_rows(read_document(path), Epoch2Collection.RUN))
    ledger = read_ledger_records(ledger_path(path, Epoch2Collection.RUN))
    return held | {record.record_key for record in ledger}


def stored(canary: CanaryProvision, runtime: Path, run_ref: str) -> Run:
    """Return the Run *run_ref* names, from whichever tier holds it."""
    urn = parse_qualified_urn(run_ref)
    with root_context(canary, runtime).session([urn]) as session:
        return stored_run(session, read_ledger_records(run_ledger(session)), urn)


@contextmanager
def held_document_lock(
    canary: CanaryProvision, runtime: Path, *, seconds: float | None
) -> Iterator[None]:
    """Hold the document lock from another thread for *seconds*, or until the block exits."""
    acquired, release = threading.Event(), threading.Event()

    def hold() -> None:
        with root_context(canary, runtime).session([REPOSITORY_URN]):
            acquired.set()
            release.wait(seconds)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert acquired.wait(5.0), "the holder never took the document lock"
    try:
        yield
    finally:
        release.set()
        holder.join(5.0)


# ---- CR-787: adoption waits out a held document lock, and names a lost one ---------


def test_adoption_waits_out_a_held_document_lock(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start and stop outlast a hold past the lock timeout; past the deadline they name it."""
    monkeypatch.setenv("EA_LOCK_TIMEOUT", "0.2")
    runtime = tmp_path / "runtime"

    began = time.monotonic()
    with held_document_lock(canary, runtime, seconds=1.0):
        started = call(canary, runtime, HOST_SUBAGENT_START_METHOD, AGENT_ID)
    assert time.monotonic() - began >= 1.0
    assert started["status"] == RunStatus.RUNNING.value
    with held_document_lock(canary, runtime, seconds=1.0):
        stopped = call(canary, runtime, HOST_SUBAGENT_STOP_METHOD, AGENT_ID)
    assert stopped["run_ref"] == started["run_ref"]
    assert stopped["status"] == RunStatus.COMPLETED.value
    assert stored(canary, runtime, stopped["run_ref"]).status is RunStatus.COMPLETED
    assert run_keys(canary) == {started["run_ref"].rsplit("/", 1)[1]}

    monkeypatch.setattr(host_subagent, "ADOPTION_RETRY_DEADLINE_SECONDS", 0.5)
    with (
        held_document_lock(canary, runtime, seconds=None),
        pytest.raises(LockTimeout) as lost,
    ):
        call(canary, runtime, HOST_SUBAGENT_START_METHOD, OTHER_AGENT_ID)
    message = str(lost.value)
    assert OTHER_AGENT_ID in message
    assert "waited" in message
    waited = float(message.split("waited ", 1)[1].split("s", 1)[0])
    assert waited >= 0.5
    assert run_keys(canary) == {started["run_ref"].rsplit("/", 1)[1]}


def test_adoption_gives_up_on_a_delivery_that_holds_the_daemon_past_the_deadline(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivery queued behind another past the deadline names its subagent and writes nothing."""
    monkeypatch.setattr(host_subagent, "ADOPTION_RETRY_DEADLINE_SECONDS", 0.2)
    with host_subagent._ADOPTIONS, pytest.raises(LockTimeout, match=AGENT_ID):
        call(canary, tmp_path / "runtime", HOST_SUBAGENT_STOP_METHOD, AGENT_ID)
    assert run_keys(canary) == set()


# ---- CR-788: a stop retries a lost edge through to completion ----------------------


@pytest.mark.parametrize(
    ("started_first", "lost_edge"),
    [
        pytest.param(False, RunStatus.RUNNING, id="queued-shape"),
        pytest.param(True, RunStatus.COMPLETED, id="running-shape"),
    ],
)
def test_a_stop_retries_a_lost_edge_to_completion(
    canary: CanaryProvision,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    started_first: bool,
    lost_edge: RunStatus,
) -> None:
    """One lock timeout on an edge is retried, and the stop leaves one Run, completed."""
    runtime = tmp_path / "runtime"
    if started_first:
        call(canary, runtime, HOST_SUBAGENT_START_METHOD, AGENT_ID)
    lost: list[str] = []

    def flaky(**kwargs: Any) -> Any:
        if kwargs["request"].to_status == lost_edge.value and not lost:
            lost.append(kwargs["request"].idempotency_key)
            raise LockTimeout("Could not acquire lock document.lock within 5.0s")
        return run_transaction(**kwargs)

    monkeypatch.setattr(host_subagent, "run_transaction", flaky)

    answer = call(canary, runtime, HOST_SUBAGENT_STOP_METHOD, AGENT_ID)

    assert len(lost) == 1
    assert answer["status"] == RunStatus.COMPLETED.value
    run = stored(canary, runtime, answer["run_ref"])
    assert run.status is RunStatus.COMPLETED
    assert run.ended_at is not None
    assert run_keys(canary) == {run.key}


# ---- CR-789: the stop binds a repository-scoped Run to the Task it names ------------


@pytest.mark.parametrize(
    ("start_ref", "stop_ref", "task_status", "bound_to"),
    [
        pytest.param(None, TASK_KEY, "PLANNED", TASK_URN, id="open-task"),
        pytest.param(None, TASK_KEY, "COMPLETED", None, id="terminal-task"),
        pytest.param(None, None, "PLANNED", None, id="no-marker"),
        pytest.param(None, "EAWF-9999", "PLANNED", None, id="unknown-task"),
        pytest.param(TASK_KEY, OTHER_TASK_KEY, "PLANNED", TASK_URN, id="already-task-scoped"),
        pytest.param(TASK_KEY, None, "PLANNED", TASK_URN, id="task-scoped-no-marker"),
    ],
)
def test_the_stop_binds_the_marked_task(
    canary: CanaryProvision,
    tmp_path: Path,
    start_ref: str | None,
    stop_ref: str | None,
    task_status: str,
    bound_to: str | None,
) -> None:
    """The stop's scope is the start's, or the open Task the stop names when the start had none."""
    seed(canary, {Epoch2Collection.TASK.value: {TASK_KEY: seed_row("task", task_status)}})
    runtime = tmp_path / "runtime"
    start_params = {} if start_ref is None else {"task_ref": start_ref}
    stop_params = {} if stop_ref is None else {"task_ref": stop_ref}

    started = call(canary, runtime, HOST_SUBAGENT_START_METHOD, AGENT_ID, **start_params)
    stopped = call(canary, runtime, HOST_SUBAGENT_STOP_METHOD, AGENT_ID, **stop_params)

    assert stopped["run_ref"] == started["run_ref"]
    run = stored(canary, runtime, stopped["run_ref"])
    assert run.status is RunStatus.COMPLETED
    assert run.scope.purpose is RunPurpose.OBSERVE
    if bound_to is None:
        assert isinstance(run.scope, RepositoryScope)
        assert str(run.scope.repository_ref) == REPOSITORY_URN
    else:
        assert isinstance(run.scope, TaskScope)
        assert str(run.scope.task_ref) == bound_to
    assert run_keys(canary) == {run.key}
