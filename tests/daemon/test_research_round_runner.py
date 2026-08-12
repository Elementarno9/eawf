"""Tests for the production round-runner binding.

Covers the seam that converts each staged researcher dispatch into a spawned
researcher session and parses each ``agent_end`` body into typed findings rows:

* :func:`~eawf.kernel.spec.campaign_driver.build_round_runner` spawns a session
  per :class:`StagedDispatch` and folds the parsed bodies into a
  :class:`RoundFindings` per round, recorded for persistence.
* :func:`~eawf.kernel.spec.campaign_driver.parse_researcher_findings` validates
  a researcher ``agent_end`` body and raises a typed
  :class:`ResearcherDispatchError` on an unparseable body (not a silent empty
  round).
* :func:`~eawf.runtime.daemon.methods.research.build_bound_round_runner` binds
  the runner over the (stubbed-in-test) ``agent.dispatch`` spawn path so a
  whole live round drives end to end without spawning a real subprocess.

The live spawn is stubbed with a recording fake + fixture ``agent_end`` bodies,
mirroring how :mod:`tests.daemon.test_fleet_drive` injects a spawner; no live
campaign is run here.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from eawf.kernel.spec.campaign_driver import (
    ResearcherDispatchError,
    RoundFindings,
    build_round_runner,
    drive_campaign,
    parse_researcher_findings,
)
from eawf.kernel.spec.live_rounds import CockpitLevel
from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    StagedDispatch,
    stage_campaign,
)
from eawf.kernel.spec.round_loop import CheckpointPolicy, CheckpointTier, RoundHaltReason
from eawf.kernel.spec.saturation import SaturationReport
from eawf.kernel.store.kinds.agent_report import ResearcherReportBody
from eawf.runtime.daemon.methods.research import (
    build_bound_round_runner,
    build_live_dispatch_spawner,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# fixtures: a staged campaign + a recording fake spawner
# --------------------------------------------------------------------------


def _block() -> ResearchProfileBlock:
    return ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "market-structure": ResearchDomainConfig(focus="venues + flow"),
            "pricing-models": ResearchDomainConfig(depth=ResearchDepth.DEEP),
        },
    )


def _agent_end_body(domain: str, *, findings: list[str]) -> dict[str, object]:
    """A valid researcher ``agent_end`` body fixture for *domain*."""
    return {
        "role": "researcher",
        "verdict": "pass",
        "confidence": "medium",
        "summary": f"surveyed {domain}",
        "question": f"what does {domain} reveal",
        "findings": findings,
        "alternatives": [],
        "recommendation": f"pursue {domain}",
        "evidence_refs": [{"kind": "store_record", "ref": "src/x.py:1"}],
    }


class _RecordingSpawner:
    """Records each spawned dispatch and returns its fixture ``agent_end`` body."""

    def __init__(self, bodies: dict[str, dict[str, object]]) -> None:
        self._bodies = bodies
        self.spawned: list[str] = []

    def __call__(self, dispatch: StagedDispatch) -> Mapping[str, object]:
        self.spawned.append(dispatch.domain)
        return self._bodies[dispatch.domain]


def _never_saturate(findings: RoundFindings) -> SaturationReport:
    """A reducer that never declares the campaign dry (forces budget halt)."""
    return SaturationReport(saturated=False, gates=(), live_claim_count=0, empty_ledger=True)


def _saturate(findings: RoundFindings) -> SaturationReport:
    """A reducer that declares the campaign dry immediately."""
    return SaturationReport(saturated=True, gates=(), live_claim_count=1, empty_ledger=False)


# --------------------------------------------------------------------------
# parse_researcher_findings -- happy path + typed error path
# --------------------------------------------------------------------------


def test_parse_researcher_findings_validates_body() -> None:
    """A valid researcher body parses into a typed ResearcherReportBody."""
    body = parse_researcher_findings(
        "market-structure", _agent_end_body("market-structure", findings=["claim a", "claim b"])
    )
    assert isinstance(body, ResearcherReportBody)
    assert body.findings == ["claim a", "claim b"]
    assert body.role == "researcher"


def test_parse_researcher_findings_rejects_non_researcher_body() -> None:
    """A non-researcher body (executor) raises the typed dispatch error."""
    executor_body = {
        "role": "executor",
        "verdict": "pass",
        "confidence": "high",
        "summary": "did the wave",
        "wave_id": "W01",
        "outcome": "done",
    }
    with pytest.raises(ResearcherDispatchError, match="market-structure"):
        parse_researcher_findings("market-structure", executor_body)


def test_parse_researcher_findings_rejects_missing_required_field() -> None:
    """A researcher body missing the required recommendation raises typed."""
    partial = {
        "role": "researcher",
        "verdict": "pass",
        "confidence": "low",
        "summary": "partial",
        "question": "q",
        "findings": [],
    }
    with pytest.raises(ResearcherDispatchError, match="unparseable agent_end body"):
        parse_researcher_findings("pricing-models", partial)


# --------------------------------------------------------------------------
# build_round_runner -- one live round spawns + parses every dispatch
# --------------------------------------------------------------------------


def test_round_runner_spawns_every_dispatch_and_records_findings() -> None:
    """A single round spawns a session per dispatch and folds the findings."""
    staged = stage_campaign("options-pricing landscape", _block())
    bodies = {
        "market-structure": _agent_end_body("market-structure", findings=["ms-1", "ms-2"]),
        "pricing-models": _agent_end_body("pricing-models", findings=["pm-1"]),
    }
    spawner = _RecordingSpawner(bodies)
    runner, rounds = build_round_runner(staged, spawner, _saturate)

    outcome = runner(1)

    # Every staged dispatch became a spawned session, in sorted-domain order.
    assert spawner.spawned == ["market-structure", "pricing-models"]
    assert outcome.saturation.saturated is True
    assert len(rounds) == 1
    findings = rounds[0]
    assert findings.round_number == 1
    assert findings.domains == ("market-structure", "pricing-models")
    assert findings.finding_lines == ("ms-1", "ms-2", "pm-1")


def test_round_runner_failed_parse_surfaces_typed_error_not_empty_round() -> None:
    """A dispatch returning a malformed body raises rather than an empty round."""
    staged = stage_campaign("options-pricing landscape", _block())
    bodies = {
        "market-structure": _agent_end_body("market-structure", findings=["ms-1"]),
        # pricing-models returns a body missing the required recommendation.
        "pricing-models": {
            "role": "researcher",
            "verdict": "pass",
            "confidence": "low",
            "summary": "partial",
            "question": "q",
            "findings": [],
        },
    }
    spawner = _RecordingSpawner(bodies)
    runner, rounds = build_round_runner(staged, spawner, _saturate)

    with pytest.raises(ResearcherDispatchError, match="pricing-models"):
        runner(1)
    # The failed round never recorded a (partial) findings row.
    assert rounds == []


def test_round_runner_drives_bounded_loop_to_budget() -> None:
    """The bound runner drives drive_campaign's loop until the round budget."""
    staged = stage_campaign("liquidity regimes", _block())
    bodies = {
        "market-structure": _agent_end_body("market-structure", findings=["ms"]),
        "pricing-models": _agent_end_body("pricing-models", findings=["pm"]),
    }
    spawner = _RecordingSpawner(bodies)
    runner, rounds = build_round_runner(staged, spawner, _never_saturate)

    result = drive_campaign(
        "liquidity regimes",
        _block(),
        level=CockpitLevel.LIVE,
        round_runner=runner,
        round_budget=3,
        checkpoint_policy=CheckpointPolicy(tier=CheckpointTier.ON_HALT),
    )

    assert result.loop_result is not None
    assert result.loop_result.rounds_run == 3
    assert result.loop_result.halt_reason is RoundHaltReason.ROUND_BUDGET
    # Three rounds, each spawning two dispatches = six spawns recorded.
    assert len(rounds) == 3
    assert spawner.spawned.count("market-structure") == 3
    assert spawner.spawned.count("pricing-models") == 3


# --------------------------------------------------------------------------
# build_bound_round_runner -- the daemon-side live-spawn binding (stubbed)
# --------------------------------------------------------------------------


def test_build_bound_round_runner_parses_each_agent_end() -> None:
    """The daemon binding spawns + parses each dispatch behind a stub."""
    staged = stage_campaign("options-pricing landscape", _block())
    produced: list[str] = []

    def _produce(dispatch: StagedDispatch) -> Mapping[str, object]:
        produced.append(dispatch.domain)
        return _agent_end_body(dispatch.domain, findings=[f"{dispatch.domain}-finding"])

    runner, rounds = build_bound_round_runner(staged, _produce, _saturate)
    outcome = runner(1)

    assert outcome.saturation.saturated is True
    assert produced == ["market-structure", "pricing-models"]
    assert rounds[0].finding_lines == (
        "market-structure-finding",
        "pricing-models-finding",
    )


def test_live_dispatch_spawner_invokes_producer() -> None:
    """The live spawner forwards a StagedDispatch to the injected producer."""
    seen: list[StagedDispatch] = []

    def _produce(dispatch: StagedDispatch) -> Mapping[str, object]:
        seen.append(dispatch)
        return _agent_end_body(dispatch.domain, findings=[])

    spawner = build_live_dispatch_spawner(_produce)
    dispatch = StagedDispatch(
        domain="market-structure",
        agent_role="researcher",
        depth=ResearchDepth.MEDIUM,
        prompt="survey it",
    )
    body = spawner(dispatch)
    assert seen == [dispatch]
    assert body["role"] == "researcher"
