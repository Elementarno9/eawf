"""A Campaign round runs through the official provider driver, read-only, outside the repository.

The research agent compiles each round as a research Run against the installed drivers,
a fixture machine certification and the built-in provider layers, then starts it through
the launcher table -- here fake launchers, so no agent binary runs. The fake writes the
driver's real per-Run MCP files into the workspace it is handed, which is how the suite
proves those files land in the round's own checkout and never in the repository root.
The round's report is parsed from the turn's final answer, and the runtime tuple a
round's Run records is the one the driver observed, ``harness_version`` included.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.projection.campaign import CampaignView
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.runtime.provider import MUTATING_ROLES
from eawf.kernel.state.enums import AgentSessionRole, EffortBucket
from eawf.kernel.state.epoch2.measurement import VendorSessionRef
from eawf.kernel.state.epoch2.run import RunPurpose, RunRuntimeTuple
from eawf.kernel.store.ledger import read_ledger_records
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.campaign_scheduler import (
    StepAssignment,
    StepReport,
    UnreportedRoundError,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import campaign_run
from eawf.runtime.daemon.methods.campaign_run import CAMPAIGN_START_METHOD, HostResearchAgent
from eawf.runtime.daemon.native_dispatch import run_binding_of, run_ledger
from eawf.runtime.mcp.native_launch import MCP_ARTIFACT_DIRNAME, native_run_server_config
from eawf.runtime.runtimes import selector
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    TurnOutcome,
    compose_worker_hello,
)
from eawf.runtime.runtimes.metering import UsageSample
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.workflow.dispatch.routing import model_for_runtime
from eawf.workflow.evidence.machine_certification import append_machine_certification
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    root_context,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.methods.test_campaign_run import (
    _runs,
    _settled,
    _start,
    served,
)
from tests.integration.runtime.daemon.test_native_dispatch import make_canary
from tests.integration.runtime.daemon.test_task_dispatch import (
    CLAUDE_VERSION,
    CODEX_VERSION,
    certification,
)

pytestmark = pytest.mark.integration

CAMPAIGN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/campaign/CAM-0001"
RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000001"
ANSWER: Final = (
    "Read the module.\n\n```json\n"
    '{"report": "# Survey\\n\\n- one module", "outcome": "one module found", "sources": 2}'
    "\n```"
)

#: What each fake driver reports it observed of the runtime it started.
OBSERVED: Final = {
    "claude": RunRuntimeTuple(
        harness="claude-code",
        harness_version=CLAUDE_VERSION,
        provider="anthropic",
        model="claude-sonnet-4-6",
    ),
    "codex": RunRuntimeTuple(
        harness="codex", harness_version=CODEX_VERSION, provider="openai", model="gpt-5.5"
    ),
}

#: The lane each fake driver renders its per-Run MCP configuration for.
_LANES: Final = {"claude": "claude-code", "codex": "codex"}


@pytest.fixture(autouse=True)
def _isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_homes: Path
) -> Iterator[None]:
    """Keep the global layer, every host home and each round checkout under tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a Campaign round reached the headless spawn path")

    monkeypatch.setattr(selector, "select_adapter", refused)
    yield


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A git-backed canary with one live Track, certified for both shipped runtimes."""
    provisioned = make_canary(tmp_path / "repo", rows={})
    seed(provisioned, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})
    state = provisioned.root / ".ea" / "state.json"
    append_machine_certification(state, certification("claude-code", CLAUDE_VERSION))
    append_machine_certification(state, certification("codex", CODEX_VERSION))
    return provisioned


class FakeDriver:
    """A launcher that writes the driver's per-Run files, then answers from a script.

    Attributes:
        provider_kind: The provider the fake stands in for.
        requests: Every launch request it was handed.
        seen: What the workspace looked like while each turn ran.
    """

    def __init__(
        self,
        provider_kind: str,
        *,
        outcome: TurnOutcome | None = None,
        usage: tuple[UsageSample, ...] = (UsageSample(input_tokens=120, output_tokens=30),),
        hand_back_turn: bool = True,
        park: asyncio.Event | None = None,
    ) -> None:
        self.provider_kind = provider_kind
        self._outcome = outcome or TurnOutcome(
            exit_kind="completed", answer_text=ANSWER, runtime_tuple=OBSERVED[provider_kind]
        )
        self._usage = usage
        self._hand_back_turn = hand_back_turn
        self._park = park
        self.requests: list[NativeLaunchRequest] = []
        self.seen: list[dict[str, Any]] = []
        self.cancelled = False

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Write the per-Run MCP files into the workspace and announce the worker."""
        self.requests.append(request)
        native_run_server_config(request, runtime_id=_LANES[self.provider_kind])
        session = f"{self.provider_kind}-round-session"
        turn = (
            asyncio.get_running_loop().create_task(self._turn(request))
            if (self._hand_back_turn)
            else None
        )
        return NativeLaunchOutcome(
            provider_session_ref=session,
            subprocess_pid=4242,
            hello=compose_worker_hello(
                spec=request.spec,
                capsule=request.capsule,
                provider_session_ref=session,
                sdk_version="1.0.0",
                worker_protocol_version="1.0.0",
                event_codec_version="1.0.0",
                hello_sequence=request.hello_sequence,
            ),
            turn=turn,
            runtime_tuple=OBSERVED[self.provider_kind].model_copy(update={"model": None}),
        )

    async def _turn(self, request: NativeLaunchRequest) -> TurnOutcome:
        self.seen.append(
            {
                "workspace": request.workspace,
                "mcp_files": sorted(
                    p.name for p in (request.workspace / MCP_ARTIFACT_DIRNAME).iterdir()
                ),
                "checked_out": (request.workspace / "src" / "module.py").is_file(),
            }
        )
        for sample in self._usage:
            assert request.usage_sink is not None
            assert await request.usage_sink(sample, 4242) is False
        if self._park is not None:
            try:
                await self._park.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return self._outcome


def _assignment(ordinal: int = 1) -> StepAssignment:
    return StepAssignment.model_validate(
        {
            "campaign_ref": CAMPAIGN_URN,
            "campaign_title": "Establish what the repository holds",
            "ordinal": ordinal,
            "title": "Survey the sources",
            "method": "survey",
            "question": "What does src hold?",
            "run_ref": RUN_URN,
            "round_number": 1,
            "actor": "OP-0001",
        }
    )


def _context(canary: CanaryProvision) -> Epoch2RootContext:
    return root_context(canary, canary.root.parent / "runtime")


def _agent(
    canary: CanaryProvision, driver: FakeDriver, *, runtime: str = "claude-code"
) -> HostResearchAgent:
    return HostResearchAgent(
        runtime,
        _context(canary),
        EffortBucket.S,
        launchers={driver.provider_kind: driver},
    )


def _work(agent: HostResearchAgent, assignment: StepAssignment | None = None) -> StepReport:
    return asyncio.run(agent.work(assignment or _assignment()))


def _checkouts(canary: CanaryProvision) -> list[str]:
    """Return the worktrees git still registers for the canary repository."""
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=canary.root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in listing.splitlines() if line.startswith("worktree ")]


# ---- CR-1: one round, read-only, through the driver --------------------------------


def test_a_round_runs_read_only_through_the_driver_and_returns_its_report(
    canary: CanaryProvision,
) -> None:
    driver = FakeDriver("claude")

    report = _work(_agent(canary, driver))

    assert report.report == "# Survey\n\n- one module"
    assert (report.outcome, report.sources, report.tokens) == ("one module found", 2, 150)
    [request] = driver.requests
    spec: CompiledRunSpec = request.spec
    capsule: AuthorityCapsule = request.capsule
    assert spec.purpose is RunPurpose.RESEARCH
    assert spec.run_scope.scope_kind == "campaign"
    assert spec.agent_role is AgentSessionRole.RESEARCHER
    assert spec.agent_role not in MUTATING_ROLES
    assert spec.route_policy_ref == "route://campaign-research"
    assert spec.sandbox.mode == "read_only"
    assert spec.authority.workspace == "read_only"
    assert capsule.authority.workspace == "read_only"
    assert "workspace_apply_patch" not in capsule.tool_grants
    assert set(capsule.tool_grants) <= {"repo_read", "repo_search", "submit_report"}
    assert request.prompt.startswith("You are the researcher for step 1")


def _binding(canary: CanaryProvision) -> RunBinding | None:
    with _context(canary).session([RUN_URN]) as session:
        records = read_ledger_records(run_ledger(session))
    return run_binding_of(records, parse_qualified_urn(RUN_URN))


def test_a_round_binds_its_run_to_the_capsule_the_driver_is_handed(
    canary: CanaryProvision,
) -> None:
    driver = FakeDriver("claude")

    _work(_agent(canary, driver))

    [request] = driver.requests
    binding = _binding(canary)
    assert binding is not None
    assert binding.capsule == request.capsule
    assert binding.authority_capsule_digest == request.capsule.contract_digest
    assert binding.compiled_spec_digest == request.spec.contract_digest


def test_a_second_round_of_one_run_keeps_the_first_binding(canary: CanaryProvision) -> None:
    driver = FakeDriver("claude")
    agent = _agent(canary, driver)

    _work(agent)
    first = _binding(canary)
    _work(agent)

    assert len(driver.requests) == 2
    assert _binding(canary) == first


def test_a_round_refused_before_launch_records_no_binding(canary: CanaryProvision) -> None:
    agent = HostResearchAgent(
        "claude-code", _context(canary), EffortBucket.S, launchers={"codex": FakeDriver("codex")}
    )

    with pytest.raises(ValueError, match="no launcher serves provider 'claude'"):
        _work(agent)

    assert _binding(canary) is None


def test_the_driver_files_land_in_the_round_checkout_never_the_repository_root(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    driver = FakeDriver("claude")

    _work(_agent(canary, driver))

    [seen] = driver.seen
    workspace: Path = seen["workspace"]
    assert seen["checked_out"], "the round reads a checkout of the repository's HEAD"
    assert seen["mcp_files"], "the fake driver wrote its per-Run files into the workspace"
    assert not workspace.resolve().is_relative_to(canary.root.resolve())
    assert workspace.resolve().is_relative_to((tmp_path / "scratch").resolve())
    assert not (canary.root / MCP_ARTIFACT_DIRNAME).exists()
    assert not workspace.exists(), "the checkout is removed once the turn ends"
    assert len(_checkouts(canary)) == 1, "git no longer registers the round checkout"


def test_a_codex_round_is_started_through_the_codex_driver(canary: CanaryProvision) -> None:
    driver = FakeDriver("codex")

    report = _work(_agent(canary, driver, runtime="codex"))

    assert driver.requests[0].spec.provider_options.provider_kind == "codex"
    assert report.runtime == OBSERVED["codex"]
    assert report.session == VendorSessionRef(
        harness="codex", session_digest=hash_vendor_session_id("codex-round-session")
    )


@pytest.mark.parametrize("effort", [EffortBucket.XS, EffortBucket.M, EffortBucket.XL])
def test_the_round_prefers_the_model_its_effort_routes_to(
    canary: CanaryProvision, effort: EffortBucket
) -> None:
    driver = FakeDriver("claude")
    agent = HostResearchAgent("claude-code", _context(canary), effort, launchers={"claude": driver})

    _work(agent)

    routed = model_for_runtime(AgentSessionRole.RESEARCHER, effort, "claude")
    assert driver.requests[0].spec.model_id == routed


# ---- CR-2: the recorded runtime tuple is the driver's observed one -------------------


def test_the_report_carries_the_tuple_the_driver_observed(canary: CanaryProvision) -> None:
    report = _work(_agent(canary, FakeDriver("claude")))

    assert report.runtime == OBSERVED["claude"]
    assert report.runtime is not None
    assert report.runtime.harness_version == CLAUDE_VERSION
    assert report.session == VendorSessionRef(
        harness="claude-code", session_digest=hash_vendor_session_id("claude-round-session")
    )


def test_a_turn_with_no_tuple_falls_back_to_the_launch_tuple(canary: CanaryProvision) -> None:
    driver = FakeDriver("claude", outcome=TurnOutcome(exit_kind="completed", answer_text=ANSWER))

    report = _work(_agent(canary, driver))

    assert report.runtime == OBSERVED["claude"].model_copy(update={"model": None})


def test_a_round_run_records_the_driver_tuple_end_to_end(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drivers = {"claude": FakeDriver("claude")}

    def agent_for(runtime: str, context: Epoch2RootContext, _depth: Any) -> HostResearchAgent:
        return HostResearchAgent(runtime, context, EffortBucket.S, launchers=drivers)

    monkeypatch.setattr(campaign_run, "research_agent_for", agent_for)

    async def body() -> CampaignView:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="shallow", agents=1)
            )
            return await _settled(client, canary.root, started["record"]["key"])

    view = asyncio.run(body())

    runs = _runs(canary)
    assert view.steps
    for step in view.steps:
        run = runs[step.step.run_refs[0].entity_key]
        assert run["status"] == "COMPLETED"
        assert RunRuntimeTuple.model_validate(run["runtime_tuple"]) == OBSERVED["claude"]
        assert run["runtime_tuple"]["harness_version"] == CLAUDE_VERSION
    assert len(drivers["claude"].requests) == len(view.steps)
    assert not (canary.root / MCP_ARTIFACT_DIRNAME).exists()


# ---- boundaries ---------------------------------------------------------------------


def test_a_turn_with_no_usage_reading_leaves_the_tokens_unmetered(
    canary: CanaryProvision,
) -> None:
    report = _work(_agent(canary, FakeDriver("claude", usage=())))

    assert report.tokens is None


def test_the_last_cumulative_reading_is_the_rounds_charge(canary: CanaryProvision) -> None:
    usage = (
        UsageSample(input_tokens=10, output_tokens=1),
        UsageSample(input_tokens=400, output_tokens=99),
    )

    report = _work(_agent(canary, FakeDriver("claude", usage=usage)))

    assert report.tokens == 499


# ---- error paths --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "match"),
    [
        (TurnOutcome(exit_kind="failed", error_text="provider overloaded"), "ended failed"),
        (TurnOutcome(exit_kind="interrupted"), "ended interrupted"),
        (TurnOutcome(exit_kind="completed"), "ended completed with no answer"),
        (TurnOutcome(exit_kind="completed", answer_text="no block"), "no ```json report"),
        (
            TurnOutcome(exit_kind="completed", answer_text='```json\n{"report": "r"}\n```'),
            "outcome: Field required",
        ),
    ],
    ids=["failed", "interrupted", "no-answer", "no-block", "schema-mismatch"],
)
def test_a_turn_without_a_report_fails_the_round_and_still_removes_the_checkout(
    canary: CanaryProvision, outcome: TurnOutcome, match: str
) -> None:
    driver = FakeDriver("claude", outcome=outcome)

    with pytest.raises(UnreportedRoundError, match=match) as caught:
        _work(_agent(canary, driver))

    assert caught.value.tokens == 150, "a turn that ran is charged what it was metered for"

    assert not driver.seen[0]["workspace"].exists()
    assert not (canary.root / MCP_ARTIFACT_DIRNAME).exists()


def test_a_driver_that_hands_back_no_turn_fails_the_round(canary: CanaryProvision) -> None:
    driver = FakeDriver("claude", hand_back_turn=False)

    with pytest.raises(ValueError, match="no turn"):
        _work(_agent(canary, driver))

    assert not driver.requests[0].workspace.exists()


def test_a_runtime_no_shipped_driver_runs_is_refused_before_any_launch(
    canary: CanaryProvision,
) -> None:
    driver = FakeDriver("claude")

    with pytest.raises(ValueError, match="no shipped driver runs 'opencode'"):
        _work(_agent(canary, driver, runtime="opencode"))

    assert driver.requests == []


def test_an_uncertified_runtime_is_refused_before_any_launch(tmp_path: Path) -> None:
    bare = make_canary(tmp_path / "bare", rows={})
    driver = FakeDriver("claude")

    with pytest.raises(ValueError, match="no certified driver serves provider 'claude'"):
        _work(_agent(bare, driver))

    assert driver.requests == []


def test_a_compiled_provider_with_no_launcher_is_refused(canary: CanaryProvision) -> None:
    agent = HostResearchAgent(
        "claude-code", _context(canary), EffortBucket.S, launchers={"codex": FakeDriver("codex")}
    )

    with pytest.raises(ValueError, match="no launcher serves provider 'claude'"):
        _work(agent)


def test_a_cancelled_round_stops_its_turn_and_removes_the_checkout(
    canary: CanaryProvision,
) -> None:
    park = asyncio.Event()
    # no reading, so the cancel lands while the turn is parked, not while a reading is stated
    driver = FakeDriver("claude", park=park, usage=())

    async def body() -> None:
        working = asyncio.create_task(_agent(canary, driver).work(_assignment()))
        while not driver.seen:
            await asyncio.sleep(0.01)
        working.cancel()
        with pytest.raises(asyncio.CancelledError):
            await working

    asyncio.run(body())

    assert driver.cancelled
    assert not driver.seen[0]["workspace"].exists()


def test_a_repository_without_a_head_commit_fails_the_round(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    driver = FakeDriver("claude")
    agent = HostResearchAgent(
        "claude-code", _context(canary), EffortBucket.S, launchers={"claude": driver}
    )
    subprocess.run(["git", "update-ref", "-d", "HEAD"], cwd=canary.root, check=True)

    with pytest.raises(Exception, match="git worktree add"):
        _work(agent)

    assert driver.requests == []
    assert list((tmp_path / "scratch").iterdir()) == []


# ---- a refused report still charges the tokens its turn spent --------------------------

#: An answer a model writes when it reads the asked keys loosely: findings as objects.
LOOSE_ANSWER: Final = (
    "Read the module.\n\n```json\n"
    '{"report": "# Survey", "outcome": "one module found", "sources": ["src/module.py:1"], '
    '"findings": [{"statement": "one module", "evidence": "src/module.py:1"}]}'
    "\n```"
)


def test_a_round_whose_report_is_refused_charges_its_tokens_and_pauses_visibly(
    canary: CanaryProvision,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    driver = FakeDriver(
        "claude",
        outcome=TurnOutcome(exit_kind="completed", answer_text=LOOSE_ANSWER),
        usage=(
            UsageSample(input_tokens=100, output_tokens=10),
            UsageSample(input_tokens=400, output_tokens=40),
        ),
    )

    def agent_for(runtime: str, context: Epoch2RootContext, _depth: Any) -> HostResearchAgent:
        return HostResearchAgent(runtime, context, EffortBucket.S, launchers={"claude": driver})

    monkeypatch.setattr(campaign_run, "research_agent_for", agent_for)
    budget = {
        "axes": [
            {"axis_kind": "rounds", "limit": 5, "unit": "rounds"},
            {"axis_kind": "tokens", "limit": 100_000, "unit": "tokens"},
        ]
    }

    async def body() -> CampaignView:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD,
                **_start(canary.root, depth="shallow", agents=1, budget=budget),
            )
            key = started["record"]["key"]
            while campaign_run.campaign_drive_in_flight():
                await asyncio.sleep(0.05)
            return CampaignView.model_validate(
                await client.call(
                    "projection.campaign.view", repo_root=str(canary.root), campaign_key=key
                )
            )

    with caplog.at_level("INFO"):
        view = asyncio.run(body())

    first = view.steps[0].step
    assert first.state.value == "pending", "the refused round returns its step to pending"
    tokens = next(axis for axis in first.bound.axes if axis.axis_kind == "tokens")
    assert (tokens.spent, tokens.spent_quality) == (440, "measured")
    campaign_tokens = next(axis for axis in view.evidence_budget.axes if axis.axis_kind == "tokens")
    assert campaign_tokens.spent == 440
    assert view.status.value == "active"
    assert _runs(canary)[first.run_refs[0].entity_key]["status"] == "CANCELLED"
    failed = [r.getMessage() for r in caplog.records if "campaign round failed" in r.getMessage()]
    assert len(failed) == 1
    assert "findings.0: Input should be a valid string" in failed[0]
    assert "src/module.py" not in failed[0], "the log names the field, never the worker's text"
    ended = [r.getMessage() for r in caplog.records if "campaign drive ended" in r.getMessage()]
    assert ended
    assert "disposition=paused" in ended[0]
    assert "findings.0" in ended[0]
