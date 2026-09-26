"""A Run control's request and acknowledgement are ordinals a console can read back.

Each control line is committed at the workspace-global ``canonical_sequence`` its
append allocates, and the firehose row states the Run it speaks for, so the ordinal
produces a keyed patch of that Run marked with the request and the disposition it
reached. That is what lets a console whose answer was lost settle the operation from
the reconnect replay rather than send the request a second time.

The defect this guards is a control fact appended as a bare ledger line: its ordinal
patches nothing, retention cannot supply it, and a reconnect across it negotiates to
``snapshot_required`` and falls back to re-sending. The last test plants exactly that
producer and shows the reconnect property reds under it.

Every call runs through the live daemon verbs against a provisioned epoch-2 canary; the
console end is the real seam over an in-process client, so nothing here is a stand-in
for the producer or the reader.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import orjson
import pytest

from eawf.kernel.projection.compute import patches_for_event
from eawf.kernel.projection.connection import ReconnectDisposition
from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.store.envelope import Envelope
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods import run as run_methods
from eawf.runtime.daemon.methods.run import (
    RUN_CONTROL_ACKNOWLEDGE_METHOD,
    RUN_CONTROL_REQUEST_METHOD,
)
from eawf.surfaces.tui.console.operations import (
    CONTROL_METHOD,
    ControlRequest,
    OperationResult,
    OperationStatus,
    Operator,
)
from eawf.surfaces.tui.console.seam import ProjectionSeam
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    firehose_path,
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"

#: The route that renders Runs, so a control line's patch reaches it.
ROUTE: Final = "activity"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one running Run."""
    provisioned = provision(tmp_path / "repo", code="CTLS")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(tmp_path / "runtime")


def call(ctx: MethodContext, method: str, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb the way the server does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


def firehose(canary: CanaryProvision) -> list[Envelope]:
    """Return every row the canary's firehose holds, in order."""
    lines = firehose_path(canary).read_bytes().splitlines()
    return [Envelope.model_validate(orjson.loads(line)) for line in lines if line.strip()]


class _InProcessClient:
    """A daemon client that dispatches straight into the method table.

    The seam's binding runs each call off the event loop, so each call gets a loop of
    its own here. An armed loss drops the answer to the next control request after the
    daemon has committed it, which is the case a reconnect exists to reconcile.
    """

    def __init__(self, ctx: MethodContext, harness: _Harness) -> None:
        self._ctx = ctx
        self._harness = harness

    def __enter__(self) -> _InProcessClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch *method*, then drop the answer if a loss is armed.

        Raises:
            ConnectionError: The armed control request's answer was lost.
        """
        self._harness.calls.append(method)
        answer: dict[str, Any] = asyncio.run(
            methods.dispatch(method, self._ctx, dict(params or {}))
        )
        if method == CONTROL_METHOD and self._harness.lose_next:
            self._harness.lose_next = False
            raise ConnectionError("the answer to the control request was lost")
        return answer


@dataclass
class _Harness:
    """What the in-process client records, and whether it loses the next answer."""

    calls: list[str]
    lose_next: bool = False


@dataclass(frozen=True)
class _Reconnected:
    """What one lost-answer reconnect did, for the assertions both tests share."""

    lost: OperationResult
    disposition: ReconnectDisposition
    reconciled: tuple[OperationResult, ...]
    outstanding_after: int
    requests_sent: int


def _lose_then_reconnect(canary: CanaryProvision, ctx: MethodContext) -> _Reconnected:
    """Send a control whose answer is lost, acknowledge it elsewhere, then reconnect."""
    harness = _Harness(calls=[])

    async def body() -> _Reconnected:
        seam = ProjectionSeam(
            route=ROUTE,
            scope_id="EAWF",
            state_path=None,
            repo_root=canary.root,
            daemon_client_factory=lambda: _InProcessClient(ctx, harness),
            operator=Operator(principal=ACTOR),
        )
        await seam.load()
        harness.lose_next = True
        lost = await seam.request(ControlRequest(target=RUN_KEY, control=ControlKind.CANCEL))
        assert lost.operation_id is not None
        await asyncio.to_thread(
            call,
            ctx,
            RUN_CONTROL_ACKNOWLEDGE_METHOD,
            repo_root=str(canary.root),
            urn=RUN_URN,
            control_request_ref=lost.operation_id,
            actor=ACTOR,
        )
        outcome = await seam.reconnect()
        return _Reconnected(
            lost=lost,
            disposition=outcome.negotiation.disposition,
            reconciled=outcome.reconciled,
            outstanding_after=len(seam.outstanding),
            requests_sent=harness.calls.count(CONTROL_METHOD),
        )

    return asyncio.run(body())


def _read_back_from_the_patch(result: _Reconnected) -> bool:
    """Return whether the reconnect settled the lost control from the replay alone."""
    return (
        result.lost.status is OperationStatus.OUTSTANDING
        and result.disposition is ReconnectDisposition.REPLAY
        and [(r.operation_id, r.status) for r in result.reconciled]
        == [(result.lost.operation_id, OperationStatus.APPLIED)]
        and "accepted" in result.reconciled[0].detail
        and result.outstanding_after == 0
        and result.requests_sent == 1
    )


def test_request_and_acknowledge_each_commit_at_their_own_canonical_sequence(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    requested = call(
        ctx,
        RUN_CONTROL_REQUEST_METHOD,
        repo_root=str(canary.root),
        urn=RUN_URN,
        control_request_ref="CTL-000000000001",
        control="cancel",
        actor=ACTOR,
    )
    acknowledged = call(
        ctx,
        RUN_CONTROL_ACKNOWLEDGE_METHOD,
        repo_root=str(canary.root),
        urn=RUN_URN,
        control_request_ref="CTL-000000000001",
        actor=ACTOR,
    )
    assert requested["canonical_sequence"] >= 1
    assert acknowledged["canonical_sequence"] == requested["canonical_sequence"] + 1
    rows = {row.payload.get("canonical_sequence"): row for row in firehose(canary)}
    marks = []
    for answer in (requested, acknowledged):
        row = rows[answer["canonical_sequence"]]
        assert row.payload["entity_ref"] == RUN_URN
        (patch,) = [p for p in patches_for_event(row) if ROUTE in p.routes]
        assert patch.canonical_sequence == answer["canonical_sequence"]
        (entry,) = patch.entries
        assert entry.key == RUN_KEY
        assert entry.status == "RUNNING"
        assert entry.control is not None
        marks.append((entry.control.control_request_ref, entry.control.disposition))
    assert marks == [("CTL-000000000001", "requesting"), ("CTL-000000000001", "accepted")]


def test_a_retried_request_commits_nothing_and_states_no_sequence(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    params = {
        "repo_root": str(canary.root),
        "urn": RUN_URN,
        "control_request_ref": "CTL-000000000002",
        "control": "interrupt",
        "actor": ACTOR,
    }
    first = call(ctx, RUN_CONTROL_REQUEST_METHOD, **params)
    before = len(firehose(canary))
    again = call(ctx, RUN_CONTROL_REQUEST_METHOD, **params)
    assert first["canonical_sequence"] is not None
    assert again["canonical_sequence"] is None
    assert again["fact"] == first["fact"]
    assert len(firehose(canary)) == before


def test_reconnecting_console_reads_the_acknowledgement_from_the_patch(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """Gate-fire proof, green half: the lost answer is read back and nothing is re-sent."""
    result = _lose_then_reconnect(canary, ctx)
    assert _read_back_from_the_patch(result), result


def test_a_bare_ledger_fact_leaves_the_reconnect_nothing_to_read(
    canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate-fire proof, red half: a control appended with no patchable row fails the check.

    The planted producer drops the fields that make the row a patch of the Run, which is
    what the control verbs wrote before. Its ordinal is then one retention cannot supply,
    so the reconnect refuses to replay and the console sends the request again.
    """
    append = run_methods.commit_ledger_append

    def bare(session: Any, record: Any, **_patch: Any) -> Envelope:
        return append(session, record)

    monkeypatch.setattr(run_methods, "commit_ledger_append", bare)
    result = _lose_then_reconnect(canary, ctx)
    assert not _read_back_from_the_patch(result)
    assert result.disposition is ReconnectDisposition.SNAPSHOT_REQUIRED
    assert result.requests_sent == 2
