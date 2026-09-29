"""SURF-084 and SURF-086: long verbs submit, followers read, reads never write.

The handlers run against a stand-in long verb registered for the test, so
each state the operation passes through can be held open and observed:
``operation.submit`` answers queued before the work starts, the trail
records running and then the terminal outcome the direct call would have
answered with, ``wait`` blocks the same submission until that outcome, a
resubmitted key replays, and ``operation.follow`` reads from a cursor
without ever appending -- an abandoned operation reads as unknown by
projection only.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.publication import PublicationOperation, PublicationOperationKind
from eawf.kernel.store.kinds.operation import OperationRecord, OperationState
from eawf.runtime.daemon import methods, operations
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods import operation as operation_methods
from eawf.runtime.daemon.methods.delivery import DELIVERY_INTEGRATE_METHOD
from eawf.runtime.daemon.methods.delivery_proof import DELIVERY_PROVE_METHOD
from eawf.workflow.release.ledger import record_operation

pytestmark = pytest.mark.integration

SLOW = "test.operation.slow"
URN = "eawf://WS/PRJ/REP/task/T-0001"
AT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def op_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[MethodContext]:
    """A daemon context bound to *tmp_path*, with the stand-in verb admitted as long."""
    monkeypatch.setattr(operation_methods, "LONG_RUNNING_METHODS", frozenset({SLOW}))
    monkeypatch.setattr(operation_methods, "_OPERATION_TASKS", {})
    (tmp_path / ".ea").mkdir()
    yield MethodContext(
        started_at=AT.isoformat(),
        pid=41,
        protocol_version="1",
        version="test",
        state_path=tmp_path / ".ea" / "state.json",
    )


def _register(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[MethodContext, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> None:
    monkeypatch.setitem(methods._REGISTRY, SLOW, handler)


async def _submit(
    ctx: MethodContext, root: Path, *, key: str = "k-1", wait: bool = False, n: int = 1
) -> dict[str, Any]:
    return await methods.dispatch(
        operation_methods.OPERATION_SUBMIT_METHOD,
        ctx,
        {
            "repo_root": str(root),
            "method": SLOW,
            "params": {"urn": URN, "n": n},
            "idempotency_key": key,
            "wait": wait,
        },
    )


async def _follow(ctx: MethodContext, root: Path, ref: str, cursor: int = 0) -> dict[str, Any]:
    return await methods.dispatch(
        operation_methods.OPERATION_FOLLOW_METHOD,
        ctx,
        {"repo_root": str(root), "operation_ref": ref, "cursor": cursor},
    )


def _states(answer: dict[str, Any]) -> list[str]:
    return [record["state"] for record in answer["records"]]


def _store_bytes(root: Path) -> bytes:
    return operations.operation_store_path(root / ".ea" / "state.json").read_bytes()


# ---- SURF-084: submission returns at once, the trail carries every state -------------


def test_surf_084_the_long_verbs_are_prove_and_integrate() -> None:
    assert (
        frozenset({DELIVERY_PROVE_METHOD, DELIVERY_INTEGRATE_METHOD})
        == operation_methods.LONG_RUNNING_METHODS
    )


def test_surf_084_submission_answers_queued_before_the_work_runs_then_follows_to_success(
    tmp_path: Path, op_ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()
    observed_in_flight: list[int] = []

    async def slow(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
        observed_in_flight.append(ctx.in_flight_mutations)
        await gate.wait()
        return {"echo": params["n"], "repo_root": params["repo_root"]}

    _register(monkeypatch, slow)

    async def scenario() -> None:
        submitted = await _submit(op_ctx, tmp_path)
        assert submitted["operation"]["state"] == "queued"
        assert submitted["replayed"] is False
        ref = submitted["operation_ref"]
        first = await _follow(op_ctx, tmp_path, ref)
        assert (_states(first), first["terminal"], first["cursor"]) == (["queued"], False, 1)
        await asyncio.sleep(0.01)
        second = await _follow(op_ctx, tmp_path, ref, cursor=first["cursor"])
        assert (_states(second), second["terminal"]) == (["running"], False)
        gate.set()
        await operation_methods._OPERATION_TASKS[uuid.UUID(ref.removeprefix("operation://"))]
        third = await _follow(op_ctx, tmp_path, ref, cursor=second["cursor"])
        assert (_states(third), third["terminal"]) == (["succeeded"], True)
        assert third["operation"]["result"] == {"echo": 1, "repo_root": str(tmp_path)}
        whole = await _follow(op_ctx, tmp_path, ref)
        assert _states(whole) == ["queued", "running", "succeeded"]
        past_the_end = await _follow(op_ctx, tmp_path, ref, cursor=third["cursor"])
        assert past_the_end["records"] == []

    asyncio.run(scenario())
    assert observed_in_flight == [1]
    assert op_ctx.in_flight_mutations == 0


def test_surf_084_a_refusal_settles_failed_with_the_direct_calls_error(
    tmp_path: Path, op_ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
        raise DaemonValidationError("validation_failed: revision_conflict: stale anchor")

    _register(monkeypatch, refused)
    answer = asyncio.run(_submit(op_ctx, tmp_path, wait=True))
    operation = answer["operation"]
    assert operation["state"] == "failed"
    assert operation["error"]["code"] == -32002
    assert operation["error"]["message"] == "validation_failed: revision_conflict: stale anchor"


def test_surf_084_wait_blocks_the_same_submission_until_it_is_terminal(
    tmp_path: Path, op_ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def quick(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.01)
        return {"passed": True}

    _register(monkeypatch, quick)
    answer = asyncio.run(_submit(op_ctx, tmp_path, wait=True))
    assert answer["operation"]["state"] == "succeeded"
    assert answer["operation"]["result"] == {"passed": True}
    trail = operations.read_trail(
        tmp_path / ".ea" / "state.json", uuid.UUID(answer["operation"]["operation_id"])
    )
    assert [record.state for record in trail] == [
        OperationState.QUEUED,
        OperationState.RUNNING,
        OperationState.SUCCEEDED,
    ]


def test_surf_084_a_resubmitted_key_replays_and_a_changed_request_conflicts(
    tmp_path: Path, op_ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    async def counted(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params["n"])
        return {"n": params["n"]}

    _register(monkeypatch, counted)

    async def scenario() -> None:
        first = await _submit(op_ctx, tmp_path, wait=True)
        again = await _submit(op_ctx, tmp_path, wait=True)
        assert again["replayed"] is True
        assert again["operation_ref"] == first["operation_ref"]
        assert again["operation"] == first["operation"]
        with pytest.raises(DaemonValidationError, match="idempotency_conflict"):
            await _submit(op_ctx, tmp_path, n=2)

    asyncio.run(scenario())
    assert calls == [1]


def test_surf_084_a_verb_that_answers_directly_is_not_submittable(
    tmp_path: Path, op_ctx: MethodContext
) -> None:
    params = {
        "repo_root": str(tmp_path),
        "method": "daemon.ping",
        "params": {"urn": URN},
        "idempotency_key": "k-1",
    }
    with pytest.raises(DaemonValidationError, match="operation_not_long_running"):
        asyncio.run(methods.dispatch(operation_methods.OPERATION_SUBMIT_METHOD, op_ctx, params))


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"method": SLOW, "params": {}, "idempotency_key": "k"}, "check params.urn"),
        ({"method": SLOW, "params": {"urn": URN}, "idempotency_key": ""}, "idempotency_key"),
        ({"method": SLOW, "params": {"urn": URN}}, "idempotency_key"),
        ({"method": SLOW, "params": {"urn": URN}, "idempotency_key": "k", "x": 1}, "x"),
    ],
    ids=["no-subject", "empty-key", "missing-key", "unknown-field"],
)
def test_surf_084_a_malformed_submission_is_refused_before_anything_is_recorded(
    tmp_path: Path, op_ctx: MethodContext, params: dict[str, Any], code: str
) -> None:
    wire = {"repo_root": str(tmp_path), **params}
    with pytest.raises(DaemonValidationError, match=code):
        asyncio.run(methods.dispatch(operation_methods.OPERATION_SUBMIT_METHOD, op_ctx, wire))
    assert not operations.operation_store_path(op_ctx.state_path).exists()


def test_surf_084_a_submission_naming_no_tree_is_refused(tmp_path: Path) -> None:
    ctx = MethodContext(started_at=AT.isoformat(), pid=1, protocol_version="1", version="t")
    params = {"method": SLOW, "params": {"urn": URN}, "idempotency_key": "k"}
    with (
        pytest.MonkeyPatch.context() as patch,
        pytest.raises(DaemonValidationError, match="operation_tree_unresolved"),
    ):
        patch.setattr(operation_methods, "LONG_RUNNING_METHODS", frozenset({SLOW}))
        asyncio.run(methods.dispatch(operation_methods.OPERATION_SUBMIT_METHOD, ctx, params))


def test_surf_084_a_cancelled_operation_records_unknown(
    tmp_path: Path, op_ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forever(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}

    _register(monkeypatch, forever)

    async def scenario() -> str:
        submitted = await _submit(op_ctx, tmp_path)
        await asyncio.sleep(0.01)
        task = next(iter(operation_methods._OPERATION_TASKS.values()))
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        ref: str = submitted["operation_ref"]
        return ref

    ref = asyncio.run(scenario())
    trail = operations.read_trail(op_ctx.state_path, operations.parse_reference(ref)[1])
    assert [record.state for record in trail][-1] is OperationState.UNKNOWN
    assert op_ctx.in_flight_mutations == 0


# ---- follow: a read that never writes ------------------------------------------------


def test_surf_086_following_an_abandoned_operation_reads_unknown_without_writing(
    tmp_path: Path, op_ctx: MethodContext
) -> None:
    stranded = OperationRecord(
        operation_id=operations.operation_id_for(SLOW, "k-9"),
        verb=SLOW,
        subject=URN,
        idempotency_key="k-9",
        state=OperationState.RUNNING,
        revision=1,
        submitted_at=AT,
        updated_at=AT,
        daemon_instance="7@an-earlier-boot",
    )
    operations.append_record(op_ctx.state_path, stranded)
    before = _store_bytes(tmp_path)
    answer = asyncio.run(_follow(op_ctx, tmp_path, operations.operation_reference(stranded)))
    assert _states(answer) == ["running", "unknown"]
    assert (answer["terminal"], answer["cursor"]) == (True, 3)
    assert _store_bytes(tmp_path) == before


@pytest.mark.parametrize(
    ("reference", "code"),
    [
        (f"operation://{uuid.UUID(int=1)}", "operation_not_found"),
        ("operation://not-an-id", "operation_ref_invalid"),
    ],
)
def test_surf_084_following_an_unknown_or_malformed_reference_is_refused(
    tmp_path: Path, op_ctx: MethodContext, reference: str, code: str
) -> None:
    with pytest.raises(DaemonValidationError, match=code):
        asyncio.run(_follow(op_ctx, tmp_path, reference))
    assert not operations.operation_store_path(op_ctx.state_path).exists()


def test_surf_084_a_release_publication_reference_is_followed_from_its_ledger(
    tmp_path: Path, op_ctx: MethodContext
) -> None:
    operation = PublicationOperation(
        operation_id=uuid.UUID(int=5),
        release_ref="REL-0.7.0",
        kind=PublicationOperationKind.PUBLISH,
        proof_digest="sha256:" + "b" * 64,
        idempotency_key="publish-1",
        opened_at=AT,
    )
    record_operation(
        op_ctx.state_path,
        operation,
        idempotency_key="publish-1",
        fingerprint="sha256:" + "c" * 64,
        recorded_at=AT,
        summary="publish REL-0.7.0",
    )
    answer = asyncio.run(_follow(op_ctx, tmp_path, f"operation://REL-0.7.0/{uuid.UUID(int=5)}"))
    assert _states(answer) == ["running"]
    assert answer["operation"]["verb"] == "release.publish"
    assert answer["terminal"] is False
