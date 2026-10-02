"""A research Campaign runs end to end on the native verbs, served by a live daemon.

A tmp canary holding one live Track is served over an isolated socket by a real daemon
connection handler, and a research brief is sent to ``runtime.campaign.start``. The daemon
plans the Campaign, approves it and drives it round by round with a stub research agent:
no model is called and no host binary starts. Everything is read back over the same
socket through ``projection.campaign.view``. Row ids name the packet requirement each test
proves: PLAN-049 (the scheduler moves each step pending, running, done on its own Run),
PLAN-050 (each round's checkpoint is an artifact revision its step produced), UI-056 (the
Campaign view carries the live steps and artifacts), DOM-034 (the synthesis promotes a
held finding) and PLAN-034 (a hard axis at its limit stops the Campaign and says why).
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import orjson
import pytest
import yaml

from eawf.kernel.projection.campaign import CampaignView
from eawf.kernel.runtime.events import MessageSummaryPayload
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import campaign_scheduler
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.campaign_scheduler import StepAssignment, StepReport
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, root_id_for
from eawf.runtime.daemon.epoch2_transaction import TransitionRequest, run_transaction
from eawf.runtime.daemon.methods import campaign_run
from eawf.runtime.daemon.methods.campaign_run import (
    CAMPAIGN_START_METHOD,
    parse_round_report,
    resolve_agent_count,
)
from eawf.runtime.daemon.methods.dispatch_queue import DISPATCH_CONTROL_REQUEST_METHOD
from eawf.runtime.daemon.run_events import run_events_of
from eawf.runtime.daemon.server import handle_connection
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
    tree_root,
)
from tests.integration.runtime.daemon.test_governor_admission import declare

pytestmark = pytest.mark.integration

TRACK: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-RUNTIME"
FINDING: Final = "replay preserves event order across restarts"
QUESTION: Final = "Does replay preserve event order?"

#: The real temp dir, read before a test redirects it: an AF_UNIX address has about 104
#: bytes on macOS, which a socket under the test's own tmp dir can overrun.
SYSTEM_TEMP_DIR: Final = tempfile.gettempdir()


class StubAgent:
    """Works every round from a script: 100 tokens and 3 sources a round."""

    def __init__(self) -> None:
        self.assignments: list[StepAssignment] = []

    async def work(self, assignment: StepAssignment) -> StepReport:
        self.assignments.append(assignment)
        synthesis = assignment.method.value == "synthesis"
        return StepReport(
            report=f"# {assignment.title}\n\n- round {assignment.round_number}\n",
            outcome="Order holds across restarts" if synthesis else "40 events, 0 inversions",
            tokens=100,
            sources=3,
            findings=(FINDING,) if synthesis else (),
        )


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep canary runtimes under tmp, and the operator's home out of reach."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> StubAgent:
    """Bind the stub as the research agent of every runtime."""
    stub = StubAgent()
    monkeypatch.setattr(campaign_run, "research_agent_for", lambda *_args: stub)
    return stub


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    provisioned = provision(tmp_path / "cam", code="CAM")
    seed(provisioned, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})
    return provisioned


class _Client:
    """One JSON-RPC connection to the served daemon."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader, self._writer = reader, writer

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        frame = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
        self._writer.write(orjson.dumps(frame) + b"\n")
        await self._writer.drain()
        reply = orjson.loads(await self._reader.readline())
        assert "error" not in reply, reply["error"]
        return dict(reply["result"])


@contextlib.asynccontextmanager
async def served(canary: CanaryProvision, tmp_path: Path) -> AsyncIterator[_Client]:
    """Serve *canary* from a live daemon connection handler on an isolated socket."""
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = EventBus()
    socket_dir = Path(SYSTEM_TEMP_DIR) / f"eawf-cr-{uuid.uuid4().hex[:8]}"
    socket_dir.mkdir()
    path = str(socket_dir / "d.sock")
    server = await asyncio.start_unix_server(lambda r, w: handle_connection(r, w, ctx), path=path)
    try:
        reader, writer = await asyncio.open_unix_connection(path)
        try:
            yield _Client(reader, writer)
        finally:
            writer.close()
    finally:
        server.close()
        await server.wait_closed()
        (socket_dir / "d.sock").unlink(missing_ok=True)
        socket_dir.rmdir()


async def _settled(client: _Client, root: Path, key: str) -> CampaignView:
    """Poll the Campaign view until the drive closed it."""
    for _ in range(200):
        answer = await client.call(
            "projection.campaign.view", repo_root=str(root), campaign_key=key
        )
        view = CampaignView.model_validate(answer)
        if view.status.value != "active":
            return view
        await asyncio.sleep(0.05)
    raise AssertionError(f"{key} is still active: {view.plan_line}")


def _start(root: Path, **brief: Any) -> dict[str, Any]:
    return {
        "repo_root": str(root),
        "actor": "OP-0001",
        "track_ref": TRACK,
        "title": "Establish whether replay preserves event order",
        "questions": [QUESTION],
        **brief,
    }


def _drive(canary: CanaryProvision, key: str) -> tuple[str, str]:
    """Return the in-flight key of *canary*'s drive of *key*."""
    return root_id_for(tree_root(canary)), key


def _runs(canary: CanaryProvision) -> dict[str, dict[str, Any]]:
    path = document_path(canary)
    rows = dict(document_rows(read_document(path), Epoch2Collection.RUN))
    for line in effective_records(read_ledger_records(ledger_path(path, Epoch2Collection.RUN))):
        rows[line.record_key] = dict(line.payload)
    return rows


def test_ui_056_a_campaign_runs_end_to_end_on_the_native_verbs(
    canary: CanaryProvision, tmp_path: Path, agent: StubAgent
) -> None:
    async def body() -> tuple[dict[str, Any], CampaignView]:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="medium", agents=1)
            )
            return started, await _settled(client, canary.root, started["record"]["key"])

    started, view = asyncio.run(body())

    assert (started["committed"], started["driving"]) == (True, True)
    # PLAN-049: two steps, each run once on its own Run, pending -> running -> done
    assert [(s.step.method.value, s.state) for s in view.steps] == [
        ("survey", "done"),
        ("synthesis", "done"),
    ]
    assert [len(s.step.run_refs) for s in view.steps] == [1, 1]
    assert view.plan_line == "2 of 2 steps done · 0 running · 0 blocked"
    assert agent.assignments[0].question == QUESTION
    assert agent.assignments[1].prior_outcomes == ("40 events, 0 inversions",)
    runs = _runs(canary)
    for step in view.steps:
        run = runs[step.step.run_refs[0].entity_key]
        assert (run["status"], run["scope"]["scope_kind"]) == ("COMPLETED", "campaign")
    # PLAN-050: each round's checkpoint is a revision its step produced, from text
    assert [card.file_name for card in view.artifacts] == [
        "CAM-0001/step-1/round-1/summary.md",
        "CAM-0001/step-2/round-1/summary.md",
    ]
    assert [s.step.produced for s in view.steps] == [
        (view.artifacts[0].artifact_ref,),
        (view.artifacts[1].artifact_ref,),
    ]
    assert view.artifacts[1].lines == ("# Synthesize the findings", "", "- round 1")
    assert not (canary.root / "CAM-0001").exists()
    # the accountant charged the Campaign one round per step
    row = read_document(document_path(canary))["campaign"]["CAM-0001"]
    assert row["evidence_budget"]["axes"][0] == {
        "axis_kind": "rounds",
        "limit": 2,
        "spent": 2,
        "unit": "rounds",
        "spent_quality": "measured",
        "hard": True,
    }
    # DOM-034: the synthesis promoted a held finding
    assert [(f.key, f.statement, f.disposition.value) for f in view.findings] == [
        ("CFN-0001", FINDING, "held")
    ]
    assert view.status.value == "converged"
    assert view.stop is not None
    assert view.stop.reason == "converged"


def test_plan_034_a_hard_budget_axis_stops_the_live_drive_and_records_why(
    canary: CanaryProvision, tmp_path: Path, agent: StubAgent
) -> None:
    config = canary.root / ".ea" / "config.yaml"
    layers = yaml.safe_load(config.read_text(encoding="utf-8")) if config.exists() else {}
    layers = {**(layers or {}), "research": {"default_depth": "deep", "agent_count": 2}}
    config.write_text(yaml.safe_dump(layers), encoding="utf-8")
    budget = {
        "axes": [
            {"axis_kind": "rounds", "limit": 3, "unit": "rounds"},
            {"axis_kind": "tokens", "limit": 100, "unit": "tokens"},
        ]
    }

    async def body() -> CampaignView:
        async with served(canary, tmp_path) as client:
            started = await client.call(CAMPAIGN_START_METHOD, **_start(canary.root, budget=budget))
            return await _settled(client, canary.root, started["record"]["key"])

    view = asyncio.run(body())

    # research.default_depth=deep: survey, adversarial, then synthesis
    assert [s.step.method.value for s in view.steps] == ["survey", "adversarial", "synthesis"]
    assert [s.step.state.value for s in view.steps] == ["done", "pending", "pending"]
    assert len(agent.assignments) == 1
    assert view.status.value == "converged"
    assert view.stop is not None
    assert (view.stop.reason, view.stop.axis_kind) == ("budget_exhausted", "tokens")
    assert view.stop.detail == "tokens reached 100 of its hard 100 tokens"
    assert view.findings == ()


def test_the_agent_count_layer_sets_the_fan_out_width_and_is_clamped() -> None:
    assert resolve_agent_count({}, None) == 4
    assert resolve_agent_count({"research": {"agent_count": 2}}, None) == 2
    assert resolve_agent_count({"research": {"agent_count": 40}}, None) == 12
    assert resolve_agent_count({"research": {"agent_count": 2}}, 5) == 5


def test_a_round_report_is_read_from_its_last_json_block() -> None:
    text = 'notes\n```json\n{"report": "# R", "outcome": "held", "sources": 2}\n```\n'
    report = parse_round_report(text, tokens=7)
    assert (report.report, report.outcome, report.tokens, report.sources) == ("# R", "held", 7, 2)
    with pytest.raises(ValueError, match="json report block"):
        parse_round_report("no block", tokens=0)


class FailingAgent(StubAgent):
    """Fails its first round, then works like the stub."""

    async def work(self, assignment: StepAssignment) -> StepReport:
        if not self.assignments:
            self.assignments.append(assignment)
            raise RuntimeError("provider lost")
        return await super().work(assignment)


def test_plan_049_a_failed_round_pauses_and_the_next_drive_resumes_on_a_new_run(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = FailingAgent()
    monkeypatch.setattr(campaign_run, "research_agent_for", lambda *_args: flaky)

    async def body() -> tuple[CampaignView, CampaignView]:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="shallow")
            )
            key = started["record"]["key"]
            while campaign_run.campaign_drive_in_flight(_drive(canary, key)):
                await asyncio.sleep(0.05)
            paused = CampaignView.model_validate(
                await client.call(
                    "projection.campaign.view", repo_root=str(canary.root), campaign_key=key
                )
            )
            resumed = await client.call(
                campaign_run.CAMPAIGN_RUN_METHOD,
                repo_root=str(canary.root),
                actor="OP-0001",
                campaign_key=key,
            )
            assert resumed["driving"] is True
            return paused, await _settled(client, canary.root, key)

    paused, view = asyncio.run(body())

    assert [s.step.state.value for s in paused.steps] == ["pending", "pending"]
    assert paused.status.value == "active"
    first_run = paused.steps[0].step.run_refs[0]
    assert _runs(canary)[first_run.entity_key]["status"] == "CANCELLED"
    assert [s.step.state.value for s in view.steps] == ["done", "done"]
    assert view.steps[0].step.run_refs[0] == first_run
    assert len(view.steps[0].step.run_refs) == 2
    assert view.status.value == "converged"


class CountingAgent(StubAgent):
    """Holds each round open briefly and records the most rounds open at once."""

    def __init__(self) -> None:
        super().__init__()
        self.open = 0
        self.peak = 0

    async def work(self, assignment: StepAssignment) -> StepReport:
        self.open += 1
        self.peak = max(self.peak, self.open)
        await asyncio.sleep(0.05)
        self.open -= 1
        return await super().work(assignment)


@pytest.mark.parametrize("agent_count", [1, 2])
def test_the_agent_count_layer_sets_how_many_steps_a_live_round_dispatches(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_count: int
) -> None:
    counting = CountingAgent()
    monkeypatch.setattr(campaign_run, "research_agent_for", lambda *_args: counting)
    config = canary.root / ".ea" / "config.yaml"
    layers = yaml.safe_load(config.read_text(encoding="utf-8")) if config.exists() else {}
    layers = {**(layers or {}), "research": {"agent_count": agent_count}}
    config.write_text(yaml.safe_dump(layers), encoding="utf-8")
    brief = _start(canary.root, depth="medium", questions=[QUESTION, "Which restarts reorder?"])

    async def body() -> CampaignView:
        async with served(canary, tmp_path) as client:
            started = await client.call(CAMPAIGN_START_METHOD, **brief)
            return await _settled(client, canary.root, started["record"]["key"])

    view = asyncio.run(body())

    # two independent surveys are ready together; the layer caps how many run at once
    assert [s.step.method.value for s in view.steps] == ["survey", "survey", "synthesis"]
    assert counting.peak == agent_count
    assert view.status.value == "converged"


def _control(root: Path, verb: str, ref: str) -> dict[str, Any]:
    return {"repo_root": str(root), "verb": verb, "actor": "OP-0001", "request_ref": ref}


def test_campaign_rounds_repro_dispatch_pause_bypassed(
    canary: CanaryProvision, tmp_path: Path, agent: StubAgent
) -> None:
    # dispatch control is filed under the tree's one project
    seed(canary, {"project": {"CAM": {"key": "CAM"}}})

    async def body() -> tuple[CampaignView, CampaignView]:
        async with served(canary, tmp_path) as client:
            await client.call(
                DISPATCH_CONTROL_REQUEST_METHOD, **_control(canary.root, "pause", "P1")
            )
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="shallow")
            )
            key = started["record"]["key"]
            while campaign_run.campaign_drive_in_flight(_drive(canary, key)):
                await asyncio.sleep(0.05)
            # a drive that ignored the hold would have worked every round by now
            await asyncio.sleep(0.5)
            held = CampaignView.model_validate(
                await client.call(
                    "projection.campaign.view", repo_root=str(canary.root), campaign_key=key
                )
            )
            await client.call(
                DISPATCH_CONTROL_REQUEST_METHOD, **_control(canary.root, "resume", "R1")
            )
            await client.call(
                campaign_run.CAMPAIGN_RUN_METHOD,
                repo_root=str(canary.root),
                actor="OP-0001",
                campaign_key=key,
            )
            return held, await _settled(client, canary.root, key)

    held, view = asyncio.run(body())

    # the pause held every round: no Run was created and no agent was asked
    assert [s.step.state.value for s in held.steps] == ["pending", "pending"]
    assert [s.step.run_refs for s in held.steps] == [(), ()]
    assert held.status.value == "active"
    assert len(agent.assignments) == 2
    assert view.status.value == "converged"


def test_the_governor_run_ceiling_bounds_how_many_rounds_run_at_once(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counting = CountingAgent()
    monkeypatch.setattr(campaign_run, "research_agent_for", lambda *_args: counting)
    declare(
        canary,
        governor={
            "max_concurrent_runs": 1,
            "max_in_flight_tokens": 1_000_000,
            "admission": "queue",
        },
    )
    brief = _start(
        canary.root, depth="medium", agents=2, questions=[QUESTION, "Which restarts reorder?"]
    )

    async def body() -> CampaignView:
        async with served(canary, tmp_path) as client:
            started = await client.call(CAMPAIGN_START_METHOD, **brief)
            return await _settled(client, canary.root, started["record"]["key"])

    view = asyncio.run(body())

    assert counting.peak == 1
    assert view.status.value == "converged"


class SlowAgent(StubAgent):
    """Holds each round open across several heartbeats."""

    async def work(self, assignment: StepAssignment) -> StepReport:
        await asyncio.sleep(0.3)
        return await super().work(assignment)


def test_campaign_runs_repro_no_events_false_stall(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(campaign_run, "research_agent_for", lambda *_args: SlowAgent())
    monkeypatch.setattr(campaign_scheduler, "_HEARTBEAT_SECONDS", 0.05)

    async def body() -> CampaignView:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="shallow")
            )
            return await _settled(client, canary.root, started["record"]["key"])

    view = asyncio.run(body())

    records = read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))
    for step in view.steps:
        lines = [
            event.payload.summary
            for event in run_events_of(records, step.step.run_refs[0])
            if isinstance(event.payload, MessageSummaryPayload)
        ]
        assert lines[0] == f"round 1 of step {step.step.ordinal} started"
        assert any(line.startswith("round still working after") for line in lines[1:])


class MovingAgent(StubAgent):
    """Suspends and resumes its first round's Run, moving its revision, then fails."""

    def __init__(self, context: Epoch2RootContext) -> None:
        super().__init__()
        self._context = context

    def _move(self, urn: str, to_status: str, revision: int, **fields: Any) -> None:
        run_transaction(
            context=self._context,
            request=TransitionRequest.model_validate(
                {
                    "urn": urn,
                    "to_status": to_status,
                    "expected_revision": revision,
                    "idempotency_key": f"moving-{to_status.lower()}",
                    "actor": "OP-0001",
                    **fields,
                }
            ),
            now=datetime.now(UTC),
        )

    async def work(self, assignment: StepAssignment) -> StepReport:
        if self.assignments:
            return await super().work(assignment)
        self.assignments.append(assignment)
        urn = str(assignment.run_ref)
        reason = {"suspension_reason": "AWAITING_PROVIDER_CAPACITY"}
        await asyncio.to_thread(self._move, urn, "SUSPENDED", 2, updates=reason)
        await asyncio.to_thread(
            self._move,
            urn,
            "RUNNING",
            3,
            observations=("run_clearing_fact_observed",),
            updates={"suspension_reason": None},
        )
        raise RuntimeError("provider lost")


def test_campaign_round_cancel_repro_hard_coded_revision(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moving = MovingAgent(root_context(canary, tmp_path / "runtime"))
    monkeypatch.setattr(campaign_run, "research_agent_for", lambda *_args: moving)

    async def body() -> CampaignView:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="shallow")
            )
            key = started["record"]["key"]
            while campaign_run.campaign_drive_in_flight(_drive(canary, key)):
                await asyncio.sleep(0.05)
            return CampaignView.model_validate(
                await client.call(
                    "projection.campaign.view", repo_root=str(canary.root), campaign_key=key
                )
            )

    view = asyncio.run(body())

    first = view.steps[0].step.run_refs[0]
    assert _runs(canary)[first.entity_key]["status"] == "CANCELLED"
    assert [s.step.state.value for s in view.steps] == ["pending", "pending"]


class GatedAgent(StubAgent):
    """Holds every round until the test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.gate: asyncio.Event | None = None

    async def work(self, assignment: StepAssignment) -> StepReport:
        assert self.gate is not None
        await self.gate.wait()
        return await super().work(assignment)


def test_campaign_drives_repro_same_key_in_two_trees_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gated = GatedAgent()
    monkeypatch.setattr(campaign_run, "research_agent_for", lambda *_args: gated)
    trees = [provision(tmp_path / name, code="CAM") for name in ("one", "two")]
    for tree in trees:
        seed(tree, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})

    async def body() -> list[CampaignView]:
        gated.gate = asyncio.Event()
        async with served(trees[0], tmp_path) as client:
            started = [
                await client.call(CAMPAIGN_START_METHOD, **_start(tree.root, depth="shallow"))
                for tree in trees
            ]
            gated.gate.set()
            # the second tree's CAM-0001 is driven beside the first's, not refused as its twin
            assert [answer["driving"] for answer in started] == [True, True]
            return [
                await _settled(client, tree.root, answer["record"]["key"])
                for tree, answer in zip(trees, started, strict=True)
            ]

    views = asyncio.run(body())

    assert [view.key for view in views] == ["CAM-0001", "CAM-0001"]
    assert [view.status.value for view in views] == ["converged", "converged"]
