"""Live-convener reliability-wiring tests (P30-I09-W06, TRUST-6).

The reliability-weighting seam built in P29-I05-W04
(:func:`~eawf.observability.eval.jury.aggregate_jury` /
:func:`~eawf.observability.eval.jury.juror_weight`) shipped idle -- nothing in
production threaded a reliability map into it. This wave is the FIRST production
caller: the cross-vendor jury convener
(:func:`~eawf.observability.eval.cross_vendor_jury.convene_cross_vendor_jury`),
its reducer (``_reduce_jury``), and the per-item reducer
(:func:`~eawf.observability.eval.cross_vendor_jury.reduce_per_item_ballots`) now
accept a reliability map and forward it into the graded mean, while
:func:`~eawf.observability.eval.reputation.build_jury_reliability_map` builds
the live ``(agent_role, runtime)`` map from the on-disk verdict store.

These tests pin the two success criteria:

- C1: the live convene path passes a non-None reliability map into
  ``aggregate_jury``, and a SCORED high-reliability juror measurably shifts the
  graded mean toward its score.
- C2: with only INSUFFICIENT reliability rows the weighted mean EQUALS the
  unweighted mean (data-starved path behavior-preserving), and a low-reliability
  juror's VETO still blocks the vote.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

import pytest

from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole
from eawf.kernel.state.models import State, Wave
from eawf.observability.eval import cross_vendor_jury as cvj
from eawf.observability.eval.cross_vendor_jury import (
    JURY_QUORUM,
    JURY_RUNTIME_FAMILIES,
    CrossVendorJuryResult,
    PerItemJurorBallot,
    RubricItemVote,
    convene_cross_vendor_jury,
    reduce_per_item_ballots,
)
from eawf.observability.eval.jury import (
    JurorBallot,
    JuryAggregate,
    JuryAggregateOutcome,
    aggregate_jury,
)
from eawf.observability.eval.reputation import (
    ReliabilityStatus,
    ReputationConfig,
    RoleReliability,
    build_jury_reliability_map,
)
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.dispatch.llm_assist import SpawnFn
from tests._criteria_helpers import legacy_criteria

_ROLE = AgentSessionRole.AUDITOR
_WAVE_ID = "P30-I09-W06"
_T0 = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 11, 12, 0, 5, tzinfo=UTC)
_CRITERIA = ["thread the reliability map", "keep the veto un-weighted"]


# --------------------------------------------------------------------------- #
# Reliability-row + ballot builders.
# --------------------------------------------------------------------------- #


def _scored(runtime: str, lower_bound: float) -> RoleReliability:
    """Build a SCORED reliability row whose display LB is *lower_bound*."""
    return RoleReliability(
        agent_role=_ROLE,
        runtime=runtime,
        n=42,
        status=ReliabilityStatus.SCORED,
        posterior_lower_bound=lower_bound,
    )


def _insufficient(runtime: str) -> RoleReliability:
    """Build an INSUFFICIENT reliability row (every numeric field ``None``)."""
    return RoleReliability(
        agent_role=_ROLE,
        runtime=runtime,
        n=3,
        status=ReliabilityStatus.INSUFFICIENT,
    )


def _graded(juror_id: str, score: float, runtime: str) -> JurorBallot:
    """Build a graded ballot tagged with the ``(AUDITOR, runtime)`` key."""
    return JurorBallot(
        juror_id=juror_id,
        acceptance_style="graded",
        score=score,
        agent_role=_ROLE,
        runtime=runtime,
    )


# --------------------------------------------------------------------------- #
# Convener fixtures (no real subprocess; canned auditor bodies).
# --------------------------------------------------------------------------- #


def _auditor_body_json(*, verdict: str = "pass") -> str:
    """Serialise a minimal schema-valid auditor ``agent_end`` body to JSON."""
    return json.dumps(
        {
            "role": "auditor",
            "verdict": verdict,
            "confidence": "high",
            "summary": "re-read the diff against the criteria",
            "target_id": _WAVE_ID,
            "criteria": [
                {"criterion": c, "passed": verdict in {"pass", "pass-with-followups"}}
                for c in _CRITERIA
            ],
            "refutations": [],
        }
    )


def _spawn_result(text: str, *, runtime: str) -> SpawnResult:
    """Wrap *text* in an otherwise-valid :class:`SpawnResult` for *runtime*."""
    return SpawnResult(
        session_id=f"sess-{runtime}",
        runtime=runtime,
        model="model-x",
        subprocess_pid=4242,
        exit_status=0,
        text=text,
        started_at=_T0,
        ended_at=_T1,
    )


class _RecordingSpawn:
    """Recording stand-in for one runtime's ``spawn_session`` (no real process)."""

    def __init__(self, runtime: str, answers: list[str]) -> None:
        self.runtime = runtime
        self._answers = list(answers)
        self.calls = 0

    async def __call__(self, prompt: str) -> SpawnResult:
        if self.calls >= len(self._answers):
            raise AssertionError(
                f"spawn for {self.runtime!r} called {self.calls + 1} times but only "
                f"{len(self._answers)} answer(s) queued"
            )
        text = self._answers[self.calls]
        self.calls += 1
        return _spawn_result(text, runtime=self.runtime)


class _RecordingFactory:
    """Per-runtime spawn factory backed by a dict of stubs."""

    def __init__(self, stubs: dict[str, SpawnFn]) -> None:
        self._stubs = stubs

    def __call__(self, runtime: str) -> SpawnFn:
        try:
            return self._stubs[runtime]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AssertionError(f"no stub registered for runtime {runtime!r}") from exc


def _state_payload() -> dict[str, Any]:
    """A minimal valid State with the phase -> iter -> wave chain for the wave."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-11T00:00:00Z",
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "Eawf",
            "description": "",
            "domains": ["workflow"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {
            "project_code": "EAWF",
            "track_id": None,
            "phase_id": "P30",
            "iter_id": "P30-I09",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "EAWF",
                "title": "v0.6",
                "status": "active",
                "iter_ids": ["P30-I09"],
                "outcome_ids": [],
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I09": {
                "id": "P30-I09",
                "phase_id": "P30",
                "title": "Trust wiring",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P30-I09",
                "title": "wire reliability into live jury aggregation",
                "status": "claimed",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/observability/eval/cross_vendor_jury.py"],
                "success_criteria": [
                    c.model_dump(mode="json") for c in legacy_criteria(*_CRITERIA)
                ],
                "agent_role": "executor",
                "effort_bucket": "M",
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-11T00:00:00Z",
                "claimed_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
                "runtime_preference": ["claude-code"],
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path) -> tuple[State, Path, Path]:
    """Serialise a valid State to ``<tmp>/.ea/state.json`` + return paths."""
    state = State.model_validate(_state_payload())
    ea = tmp_path / ".ea"
    ea.mkdir(exist_ok=True)
    state_path = ea / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    events_path = ea / "store" / "event.jsonl"
    return state, state_path, events_path


def _convene(
    tmp_path: Path,
    *,
    factory: _RecordingFactory,
    reliability: dict[tuple[AgentSessionRole, str], RoleReliability] | None,
    runtimes: tuple[str, ...] = JURY_RUNTIME_FAMILIES,
    quorum: int = JURY_QUORUM,
) -> CrossVendorJuryResult:
    """Drive the convener over an on-disk state + return the reduced result."""
    state, state_path, events_path = _write_state(tmp_path)
    wave: Wave = state.waves[_WAVE_ID]
    return asyncio.run(
        convene_cross_vendor_jury(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn_factory=factory,
            runtimes=runtimes,
            quorum=quorum,
            reliability=reliability,
            repo_root=tmp_path,
        )
    )


def _unanimous_pass_factory() -> _RecordingFactory:
    """A factory whose three jurors all return PASS."""
    return _RecordingFactory(
        {r: _RecordingSpawn(r, [_auditor_body_json(verdict="pass")]) for r in JURY_RUNTIME_FAMILIES}
    )


# --------------------------------------------------------------------------- #
# C1: the live convene path threads a non-None map into aggregate_jury.
# --------------------------------------------------------------------------- #


def test_convene_threads_reliability_map_into_aggregate_jury(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: the convener forwards the supplied map into ``aggregate_jury``.

    The map is the first production wiring of the reliability seam. A spy over
    the reducer captures the ``reliability=`` kwarg the convener passes, proving
    the live path threads a non-None map (not the default ``None``).
    """
    captured: list[Any] = []
    real = aggregate_jury

    def _spy(ballots: tuple[JurorBallot, ...], reliability: Any = None) -> JuryAggregate:
        captured.append(reliability)
        return real(ballots, reliability=reliability)

    monkeypatch.setattr(cvj, "aggregate_jury", _spy)

    reliability = {(_ROLE, r): _scored(r, 0.8) for r in JURY_RUNTIME_FAMILIES}
    result = _convene(tmp_path, factory=_unanimous_pass_factory(), reliability=reliability)

    assert result.outcome is JuryAggregateOutcome.PASS
    # The reducer was called with the non-None map the convener forwarded.
    assert len(captured) == 1
    assert captured[0] is reliability


def test_convene_tags_ballots_with_auditor_role_and_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: each cast ballot carries its ``(AUDITOR, runtime)`` reliability key.

    Without the join key on the ballot, ``juror_weight`` cannot match the map
    row, so the wiring would be inert. The spy inspects the ballots the convener
    built to confirm each is keyed for reliability lookup.
    """
    captured_ballots: list[tuple[JurorBallot, ...]] = []
    real = aggregate_jury

    def _spy(ballots: tuple[JurorBallot, ...], reliability: Any = None) -> JuryAggregate:
        captured_ballots.append(ballots)
        return real(ballots, reliability=reliability)

    monkeypatch.setattr(cvj, "aggregate_jury", _spy)

    reliability = {(_ROLE, r): _scored(r, 0.8) for r in JURY_RUNTIME_FAMILIES}
    _convene(tmp_path, factory=_unanimous_pass_factory(), reliability=reliability)

    assert len(captured_ballots) == 1
    ballots = captured_ballots[0]
    assert {b.runtime for b in ballots} == set(JURY_RUNTIME_FAMILIES)
    assert all(b.agent_role is _ROLE for b in ballots)


def test_high_reliability_graded_juror_shifts_mean_toward_its_score() -> None:
    """C1: a SCORED high-reliability juror pulls the graded mean toward its score.

    The convener-threaded reliability map flows into the SAME
    :func:`~eawf.observability.eval.jury.aggregate_jury` the live path uses. Over
    graded ballots, a high-LB juror voting high and a low-LB juror voting low
    push the weighted mean above the plain mean, toward the trusted juror's
    score -- the measurable shift the wiring exists to produce.
    """
    ballots = (
        _graded("trusted", 0.95, runtime="claude-code"),
        _graded("shaky", 0.45, runtime="codex"),
    )
    reliability = {
        (_ROLE, "claude-code"): _scored("claude-code", 0.9),
        (_ROLE, "codex"): _scored("codex", 0.1),
    }

    weighted = aggregate_jury(ballots, reliability=reliability)
    plain = aggregate_jury(ballots)

    expected = (0.9 * 0.95 + 0.1 * 0.45) / (0.9 + 0.1)
    assert weighted.mean_score == pytest.approx(expected)
    assert plain.mean_score is not None
    # The weighted mean leans toward the trusted juror's high score.
    assert weighted.mean_score > plain.mean_score
    assert weighted.mean_score > 0.85


def test_per_item_reducer_forwards_reliability_into_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1: ``reduce_per_item_ballots`` forwards the map into each item reduction.

    The per-item path is the live spec-jury reducer; threading the map there
    keeps the per-item graded mean reliability-weighted the same way the holistic
    path is.
    """
    captured: list[Any] = []
    real = aggregate_jury

    def _spy(ballots: tuple[JurorBallot, ...], reliability: Any = None) -> JuryAggregate:
        captured.append(reliability)
        return real(ballots, reliability=reliability)

    monkeypatch.setattr(cvj, "aggregate_jury", _spy)

    ballots = (
        PerItemJurorBallot(
            juror="claude-code",
            votes=(RubricItemVote(item_id="B1", passed=True),),
        ),
        PerItemJurorBallot(
            juror="codex",
            votes=(RubricItemVote(item_id="B1", passed=True),),
        ),
    )
    reliability = {(_ROLE, "claude-code"): _scored("claude-code", 0.8)}

    result = reduce_per_item_ballots(ballots, ("B1",), reliability=reliability)

    assert result.outcome is JuryAggregateOutcome.PASS
    assert captured == [reliability]


def test_per_item_vote_to_ballot_carries_reliability_key() -> None:
    """C1: a per-item vote maps onto a ballot keyed for reliability lookup."""
    ballot = cvj._vote_to_ballot("codex", RubricItemVote(item_id="B1", passed=True))

    assert ballot.agent_role is _ROLE
    assert ballot.runtime == "codex"
    assert ballot.verdict is AgentReportVerdict.PASS


# --------------------------------------------------------------------------- #
# C2: data-starved (INSUFFICIENT) path is behavior-preserving.
# --------------------------------------------------------------------------- #


def test_all_insufficient_weighted_graded_mean_equals_unweighted() -> None:
    """C2: an all-INSUFFICIENT map weights every juror neutrally -> plain mean.

    The honest-negative path that ships today: the reputation engine scores no
    role, so every reliability row is INSUFFICIENT and the weighted graded mean
    is identical to the unweighted mean -- the wiring is behavior-preserving.
    """
    scores = (0.9, 0.85, 0.8)
    ballots = tuple(
        _graded(f"j{i}", s, runtime=r)
        for i, (s, r) in enumerate(zip(scores, JURY_RUNTIME_FAMILIES, strict=True))
    )
    reliability = {(_ROLE, r): _insufficient(r) for r in JURY_RUNTIME_FAMILIES}

    weighted = aggregate_jury(ballots, reliability=reliability)
    baseline = aggregate_jury(ballots)

    assert weighted.outcome is baseline.outcome
    assert weighted.mean_score == pytest.approx(fmean(scores))
    assert weighted.mean_score == pytest.approx(baseline.mean_score)


def test_convene_with_insufficient_map_matches_unweighted_outcome(tmp_path: Path) -> None:
    """C2: the live convener over an all-INSUFFICIENT map matches the plain run.

    Driving the convener with the data-starved map and again with no map yields
    the same outcome + aggregate -- the binary holistic path is unchanged because
    the binary side never weights and the INSUFFICIENT map weights neutrally.
    """
    reliability = {(_ROLE, r): _insufficient(r) for r in JURY_RUNTIME_FAMILIES}

    weighted = _convene(tmp_path, factory=_unanimous_pass_factory(), reliability=reliability)
    plain = _convene(tmp_path, factory=_unanimous_pass_factory(), reliability=None)

    assert weighted.outcome is plain.outcome is JuryAggregateOutcome.PASS
    assert weighted.aggregate is not None
    assert plain.aggregate is not None
    assert weighted.aggregate.veto_count == plain.aggregate.veto_count == 0


def test_live_reliability_map_is_empty_on_empty_verdict_store(tmp_path: Path) -> None:
    """C2: ``build_jury_reliability_map`` is honest-empty on an empty store.

    The end-to-end production wiring returns an empty map today (no verdict rows
    on disk), and an empty map weights every juror neutrally -- so threading it
    is behavior-preserving until real verdict rows accrue.
    """
    state, state_path, _events = _write_state(tmp_path)

    weighting_map = build_jury_reliability_map(state, state_path, ReputationConfig(), {})

    assert weighting_map == {}


# --------------------------------------------------------------------------- #
# C2: the veto is never down-weighted, regardless of reliability.
# --------------------------------------------------------------------------- #


def test_low_reliability_juror_veto_still_blocks_via_convener(tmp_path: Path) -> None:
    """C2: a low-reliability juror's FAIL still vetoes the live convened vote.

    Minority-veto is the conservative close-gate signal; one credible refutation
    sinks the vote regardless of that juror's reliability weight. The convener
    threads a map giving the FAIL juror the lowest possible weight, yet the vote
    still blocks to FAIL.
    """
    factory = _RecordingFactory(
        {
            "claude-code": _RecordingSpawn("claude-code", [_auditor_body_json(verdict="pass")]),
            "codex": _RecordingSpawn("codex", [_auditor_body_json(verdict="fail")]),
            "opencode": _RecordingSpawn("opencode", [_auditor_body_json(verdict="pass")]),
        }
    )
    reliability = {
        (_ROLE, "codex"): _scored("codex", 0.0),
        (_ROLE, "claude-code"): _scored("claude-code", 0.99),
        (_ROLE, "opencode"): _scored("opencode", 0.99),
    }

    result = _convene(tmp_path, factory=factory, reliability=reliability)

    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.aggregate is not None
    assert result.aggregate.veto_count == 1
    codex = next(j for j in result.jurors if j.runtime == "codex")
    assert codex.verdict is AgentReportVerdict.FAIL


def test_low_weight_veto_dominates_weighted_graded_mean() -> None:
    """C2: a low-weight binary veto still sinks a mixed weighted-graded vote.

    Even when a trusted juror's high graded score would clear, a single credible
    ``fail`` from a low-reliability juror vetoes -- the veto is never weighted.
    """
    ballots = (
        JurorBallot(
            juror_id="vetoer",
            acceptance_style="binary",
            verdict=AgentReportVerdict.FAIL,
            agent_role=_ROLE,
            runtime="codex",
        ),
        _graded("grader", 0.98, runtime="claude-code"),
    )
    reliability = {
        (_ROLE, "codex"): _scored("codex", 0.05),
        (_ROLE, "claude-code"): _scored("claude-code", 0.95),
    }

    weighted = aggregate_jury(ballots, reliability=reliability)

    assert weighted.outcome is JuryAggregateOutcome.FAIL
    assert weighted.veto_count == 1


def test_per_item_low_reliability_veto_still_fails_item() -> None:
    """C2: a low-reliability juror's per-item refutation still fails the item.

    The per-item reducer maps a refuted vote onto a binary FAIL ballot; the
    binary minority-veto is never down-weighted, so a threaded low-LB map cannot
    rescue the item.
    """
    ballots = (
        PerItemJurorBallot(
            juror="claude-code",
            votes=(RubricItemVote(item_id="B1", passed=True),),
        ),
        PerItemJurorBallot(
            juror="codex",
            votes=(RubricItemVote(item_id="B1", passed=False, refutation="B1 is broken"),),
        ),
    )
    reliability = {
        (_ROLE, "claude-code"): _scored("claude-code", 0.99),
        (_ROLE, "codex"): _scored("codex", 0.0),
    }

    result = reduce_per_item_ballots(ballots, ("B1",), reliability=reliability)

    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.failed_item_ids == ("B1",)
    assert result.items[0].veto_count == 1


# --------------------------------------------------------------------------- #
# build_jury_reliability_map: index + honest-negative boundary.
# --------------------------------------------------------------------------- #


def test_reliability_weighting_map_indexes_by_role_runtime() -> None:
    """Boundary: the weighting-map adapter keys each row by ``(role, runtime)``."""
    from eawf.observability.eval.reputation import reliability_weighting_map

    rows = [_scored("claude-code", 0.8), _insufficient("codex")]

    weighting_map = reliability_weighting_map(rows)

    assert set(weighting_map) == {(_ROLE, "claude-code"), (_ROLE, "codex")}
    assert weighting_map[(_ROLE, "claude-code")].posterior_lower_bound == pytest.approx(0.8)


def test_reliability_weighting_map_empty_for_empty_input() -> None:
    """Boundary: no reliabilities -> the empty weighting map."""
    from eawf.observability.eval.reputation import reliability_weighting_map

    assert reliability_weighting_map([]) == {}


def test_convene_default_reliability_is_none_for_existing_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backward-compat: a caller that omits ``reliability`` passes ``None`` through.

    Existing convener callers must stay behavior-preserving: the new optional
    param defaults to ``None``, so the reducer receives ``None`` and weights every
    juror neutrally exactly as before this wave.
    """
    captured: list[Any] = []
    real = aggregate_jury

    def _spy(ballots: tuple[JurorBallot, ...], reliability: Any = None) -> JuryAggregate:
        captured.append(reliability)
        return real(ballots, reliability=reliability)

    monkeypatch.setattr(cvj, "aggregate_jury", _spy)

    state, state_path, events_path = _write_state(tmp_path)
    wave = state.waves[_WAVE_ID]
    result = asyncio.run(
        convene_cross_vendor_jury(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn_factory=_unanimous_pass_factory(),
            repo_root=tmp_path,
        )
    )

    assert result.outcome is JuryAggregateOutcome.PASS
    assert captured == [None]
