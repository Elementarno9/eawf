"""Unit tests for the spec-jury close-gate producer (P29-I08-W05).

Covers :mod:`eawf.workflow.dispatch.spec_jury`:

- the pure band predicate :func:`wave_in_uiux_band` (empty / None bands band
  nothing; a token matches the wave id or title case-insensitively);
- the idle-contract producer :func:`produce_spec_jury_verdict` -- idle when
  no ballot fn is injected (typed skipped result, nothing written), safe-skip
  when the wave has no spec / no jury-scorable behaviour, and the scored path
  (collects per-item ballots, reduces them, writes ONE per-item
  ``AuditorReportBody`` at ``base_id=wave_id`` with per-item verdicts).

The ballot fn is ALWAYS a canned stub -- no real model, no spawn, no cost.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.wave import QualityDimension, WaveBehavior, WaveSpec
from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole, AgentSessionStatus
from eawf.kernel.state.models import State, Wave
from eawf.observability.eval.cross_vendor_jury import PerItemJurorBallot, RubricItemVote
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.agent_report.rollup import iter_agent_reports
from eawf.workflow.dispatch.llm_assist import SpawnFn
from eawf.workflow.dispatch.spec_jury import (
    SpecJuryResult,
    live_per_item_ballot_fn,
    produce_spec_jury_verdict,
    wave_in_uiux_band,
)
from eawf.workflow.lifecycle._errors import LifecycleError
from tests._criteria_helpers import legacy_criteria

_WAVE_ID = "P29-I08-W05"
_T0 = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
_AUDITOR_SESSION = "S-2026-auditor-spec-jury"


# --------------------------------------------------------------------------- #
# Builders: WaveSpec with jury-scorable behaviours, Wave, AUDITOR-session State.
# --------------------------------------------------------------------------- #


def _make_wave_spec(*, jury_scorable: bool = True) -> WaveSpec:
    """Build a WaveSpec whose two B-behaviours are (optionally) jury-scorable."""
    behaviors = [
        WaveBehavior(
            id="B1",
            text="the close gate routes a banded wave through the spec jury",
            jury_scorable=jury_scorable,
            quality_dimension=QualityDimension.OPERABILITY if jury_scorable else None,
        ),
        WaveBehavior(
            id="B2",
            text="the producer is idle until a per-item ballot fn is injected",
            jury_scorable=jury_scorable,
            quality_dimension=(QualityDimension.INTERACTION_CAPABILITY if jury_scorable else None),
        ),
    ]
    return WaveSpec(
        id=_WAVE_ID,
        iter_id="P29-I08",
        phase_id="P29",
        title="route band waves through the spec-jury producer",
        agent_role=AgentSessionRole.EXECUTOR,
        effort_bucket="L",
        file_scopes=["src/eawf/workflow/dispatch/spec_jury.py"],
        implements=[
            {"verdict_id": "D17", "brief": ".ea/local/research/2026-06-03-p29-drift-audit.md"}
        ],
        behaviors=behaviors,
        failure_modes=["idle producer silently passes a banded close"],
    )


def _make_wave(*, wave_id: str = _WAVE_ID, title: str = "spec-jury producer") -> Wave:
    """Build a standalone claimed :class:`Wave`."""
    return Wave.model_validate(
        {
            "id": wave_id,
            "iter_id": "P29-I08",
            "title": title,
            "status": "claimed",
            "deps": [],
            "blocks": [],
            "file_scopes": ["src/eawf/workflow/dispatch/spec_jury.py"],
            "success_criteria": [
                c.model_dump(mode="json")
                for c in legacy_criteria("route banded waves through the spec jury")
            ],
            "agent_role": "executor",
            "effort_bucket": "L",
            "opened_at": "2026-06-03T00:00:00Z",
            "claimed_at": "2026-06-03T00:00:00Z",
        }
    )


def _write_state_with_auditor(tmp_path: Path) -> tuple[State, Path]:
    """Serialise a State carrying one ACTIVE AUDITOR session + return paths.

    The canonical agent-report writer authenticates the author against the
    session role, so the producer's verdict write needs a real AUDITOR
    session present in state.
    """
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-03T00:00:00Z",
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
        "current": {"project_code": "EAWF"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {
            _AUDITOR_SESSION: {
                "id": _AUDITOR_SESSION,
                "role": AgentSessionRole.AUDITOR.value,
                "runtime": "claude-code",
                "scope_id": f"{_WAVE_ID}::audit",
                "status": AgentSessionStatus.ACTIVE.value,
                "claimed_wave_ids": [],
                "worktree_ids": [],
                "artifact_ids": [],
                "started_at": "2026-06-03T00:00:00Z",
                "ended_at": None,
                "summary": None,
            }
        },
        "plugins": {},
        "indexes": {},
    }
    state = State.model_validate(payload)
    ea = tmp_path / ".ea"
    ea.mkdir()
    state_path = ea / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state, state_path


def _ballot_fn_factory(votes_by_juror: dict[str, dict[str, tuple[bool, str | None]]]) -> Any:
    """Return a canned per-item ballot fn replaying *votes_by_juror*.

    *votes_by_juror* maps juror id -> {item_id: (passed, refutation)} so a
    test fully controls each juror's per-item vote without a model. Records
    the prompt it was handed so a test can assert the rubric reached it.
    """
    seen_prompts: list[str] = []

    async def _fn(prompt: str) -> tuple[PerItemJurorBallot, ...]:
        seen_prompts.append(prompt)
        ballots: list[PerItemJurorBallot] = []
        for juror, item_votes in votes_by_juror.items():
            votes = tuple(
                RubricItemVote(item_id=item_id, passed=passed, refutation=refutation)
                for item_id, (passed, refutation) in item_votes.items()
            )
            ballots.append(PerItemJurorBallot(juror=juror, votes=votes))
        return tuple(ballots)

    _fn.seen_prompts = seen_prompts  # type: ignore[attr-defined]
    return _fn


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# wave_in_uiux_band -- pure predicate boundary cases.
# --------------------------------------------------------------------------- #


def test_wave_in_uiux_band_empty_bands_bands_nothing() -> None:
    """An empty / None band list bands no wave (the v0.5 default)."""
    wave = _make_wave(title="tui richer views")
    assert wave_in_uiux_band(wave, bands=[]) is False
    assert wave_in_uiux_band(wave, bands=None) is False


def test_wave_in_uiux_band_matches_id_or_title_case_insensitive() -> None:
    """A band token matches the wave id or title, case-insensitively."""
    by_title = _make_wave(title="Native TUI modes chassis")
    assert wave_in_uiux_band(by_title, bands=["tui"]) is True
    # A token that matches neither id nor title does not band.
    assert wave_in_uiux_band(by_title, bands=["telemetry"]) is False
    # Match against the wave id segment.
    by_id = _make_wave(wave_id="P29-I05-W11", title="poll backstop")
    assert wave_in_uiux_band(by_id, bands=["p29-i05"]) is True


def _wave_with_scopes(file_scopes: list[str]) -> Wave:
    """Build a Wave whose title / id carry no band token, only file_scopes vary."""
    return Wave.model_validate(
        {
            "id": "P29-I08-W06",
            "iter_id": "P29-I08",
            "title": "band-scoped verify resolution",
            "status": "claimed",
            "file_scopes": file_scopes,
            "success_criteria": [c.model_dump(mode="json") for c in legacy_criteria("c1")],
            "opened_at": "2026-06-03T00:00:00Z",
            "claimed_at": "2026-06-03T00:00:00Z",
        }
    )


def test_wave_in_uiux_band_ui_file_scopes_band_even_with_empty_bands() -> None:
    """A UI-surface file_scope bands the wave via the structural arm (no token needed)."""
    tui = _wave_with_scopes(["src/eawf/surfaces/tui/widgets/footer.py"])
    assert wave_in_uiux_band(tui, bands=[]) is True
    assert wave_in_uiux_band(tui, bands=None) is True
    render = _wave_with_scopes(["src/eawf/surfaces/render/envelope.py"])
    assert wave_in_uiux_band(render, bands=[]) is True


def test_wave_in_uiux_band_non_ui_file_scopes_not_banded() -> None:
    """A non-UI file_scope with no matching token bands nothing."""
    backend = _wave_with_scopes(["src/eawf/kernel/spec/wave.py"])
    assert wave_in_uiux_band(backend, bands=[]) is False
    assert wave_in_uiux_band(backend, bands=["tui"]) is False


# --------------------------------------------------------------------------- #
# produce_spec_jury_verdict -- idle contract (no ballot fn).
# --------------------------------------------------------------------------- #


def test_producer_idle_when_no_ballot_fn(tmp_path: Path) -> None:
    """No injected ballot fn -> typed skipped result, nothing written."""
    state, state_path = _write_state_with_auditor(tmp_path)
    result = _run(
        produce_spec_jury_verdict(
            state=state,
            state_path=state_path,
            wave=_make_wave(),
            spec=_make_wave_spec(),
            auditor_session_id=_AUDITOR_SESSION,
            per_item_ballot_fn=None,
        )
    )
    assert isinstance(result, SpecJuryResult)
    assert result.status == "skipped"
    assert result.scored is False
    assert result.verdict is None
    assert result.append_result is None
    # No auditor report was written for the wave.
    assert iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID) == []


# --------------------------------------------------------------------------- #
# produce_spec_jury_verdict -- safe-skip on missing / empty rubric.
# --------------------------------------------------------------------------- #


def test_producer_skips_when_spec_is_none(tmp_path: Path) -> None:
    """A banded wave with no WaveSpec safe-skips (nothing to score)."""
    state, state_path = _write_state_with_auditor(tmp_path)
    fn = _ballot_fn_factory({"claude-code": {"B1": (True, None)}})
    result = _run(
        produce_spec_jury_verdict(
            state=state,
            state_path=state_path,
            wave=_make_wave(),
            spec=None,
            auditor_session_id=_AUDITOR_SESSION,
            per_item_ballot_fn=fn,
        )
    )
    assert result.status == "skipped"
    assert result.reason == "no wave spec"
    # The ballot fn was never invoked -- the skip short-circuits before convening.
    assert fn.seen_prompts == []
    assert iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID) == []


def test_producer_skips_when_no_jury_scorable_behaviour(tmp_path: Path) -> None:
    """A spec with no jury-scorable behaviour safe-skips with a typed reason."""
    state, state_path = _write_state_with_auditor(tmp_path)
    fn = _ballot_fn_factory({"claude-code": {"B1": (True, None)}})
    result = _run(
        produce_spec_jury_verdict(
            state=state,
            state_path=state_path,
            wave=_make_wave(),
            spec=_make_wave_spec(jury_scorable=False),
            auditor_session_id=_AUDITOR_SESSION,
            per_item_ballot_fn=fn,
        )
    )
    assert result.status == "skipped"
    assert result.reason == "no jury-scorable behaviour in spec"
    assert fn.seen_prompts == []
    assert iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID) == []


# --------------------------------------------------------------------------- #
# produce_spec_jury_verdict -- scored path writes a per-item auditor report.
# --------------------------------------------------------------------------- #


def test_producer_scores_pass_and_writes_per_item_report(tmp_path: Path) -> None:
    """Unanimous pass on every rubric item -> PASS verdict + per-item report."""
    state, state_path = _write_state_with_auditor(tmp_path)
    fn = _ballot_fn_factory(
        {
            "claude-code": {"B1": (True, None), "B2": (True, None)},
            "codex": {"B1": (True, None), "B2": (True, None)},
            "opencode": {"B1": (True, None), "B2": (True, None)},
        }
    )
    result = _run(
        produce_spec_jury_verdict(
            state=state,
            state_path=state_path,
            wave=_make_wave(),
            spec=_make_wave_spec(),
            auditor_session_id=_AUDITOR_SESSION,
            per_item_ballot_fn=fn,
            evidence_block="evidence: the gate routes banded waves",
            now=_T0,
        )
    )
    assert result.status == "scored"
    assert result.scored is True
    assert result.verdict is AgentReportVerdict.PASS
    assert result.append_result is not None
    assert result.append_result.attempt == 1

    # Exactly one per-item auditor report at base_id=wave_id, carrying one
    # CriterionVerdict per rubric item.
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)
    assert len(rows) == 1
    body = rows[-1].payload.body
    assert body.verdict is AgentReportVerdict.PASS
    assert body.target_id == _WAVE_ID
    assert {c.criterion for c in body.criteria} == {"B1", "B2"}
    assert all(c.passed for c in body.criteria)
    # include_diff=False means the prompt carries no diff section but does carry
    # the rubric + the evidence block.
    prompt = fn.seen_prompts[0]
    assert "## Diff under audit" not in prompt
    assert "## Rubric (refute-first)" in prompt
    assert "## Evidence" in prompt


def test_producer_veto_fails_named_item_and_blocks(tmp_path: Path) -> None:
    """A single refuted item minority-vetoes that item -> FAIL verdict.

    The written report names which rubric item failed (passed=False) and
    carries the refutation text so the operator sees why.
    """
    state, state_path = _write_state_with_auditor(tmp_path)
    fn = _ballot_fn_factory(
        {
            "claude-code": {"B1": (True, None), "B2": (True, None)},
            "codex": {
                "B1": (False, "B1 is not actually wired into the close gate"),
                "B2": (True, None),
            },
            "opencode": {"B1": (True, None), "B2": (True, None)},
        }
    )
    result = _run(
        produce_spec_jury_verdict(
            state=state,
            state_path=state_path,
            wave=_make_wave(),
            spec=_make_wave_spec(),
            auditor_session_id=_AUDITOR_SESSION,
            per_item_ballot_fn=fn,
            now=_T0,
        )
    )
    assert result.status == "scored"
    assert result.verdict is AgentReportVerdict.FAIL
    assert result.result is not None
    assert result.result.failed_item_ids == ("B1",)

    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)
    body = rows[-1].payload.body
    assert body.verdict is AgentReportVerdict.FAIL
    by_item = {c.criterion: c.passed for c in body.criteria}
    assert by_item == {"B1": False, "B2": True}
    assert "B1 is not actually wired into the close gate" in body.refutations


def test_producer_split_no_veto_blocks_as_blocked(tmp_path: Path) -> None:
    """A non-veto non-pass split routes the item to NEEDS_USER -> BLOCKED verdict."""
    state, state_path = _write_state_with_auditor(tmp_path)
    # B1: one juror votes fail WITHOUT a refutation (non-veto), two pass ->
    # split -> NEEDS_USER for that item -> wave-level BLOCKED.
    fn = _ballot_fn_factory(
        {
            "claude-code": {"B1": (True, None), "B2": (True, None)},
            "codex": {"B1": (False, None), "B2": (True, None)},
            "opencode": {"B1": (True, None), "B2": (True, None)},
        }
    )
    result = _run(
        produce_spec_jury_verdict(
            state=state,
            state_path=state_path,
            wave=_make_wave(),
            spec=_make_wave_spec(),
            auditor_session_id=_AUDITOR_SESSION,
            per_item_ballot_fn=fn,
            now=_T0,
        )
    )
    assert result.status == "scored"
    assert result.verdict is AgentReportVerdict.BLOCKED
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)
    assert rows[-1].payload.body.verdict is AgentReportVerdict.BLOCKED


def test_producer_rejects_off_rubric_ballot(tmp_path: Path) -> None:
    """A ballot voting on an unknown rubric item id raises (malformed ballot)."""
    state, state_path = _write_state_with_auditor(tmp_path)
    fn = _ballot_fn_factory({"claude-code": {"B1": (True, None), "B99": (True, None)}})
    with pytest.raises(ValueError, match="unknown rubric item"):
        _run(
            produce_spec_jury_verdict(
                state=state,
                state_path=state_path,
                wave=_make_wave(),
                spec=_make_wave_spec(),
                auditor_session_id=_AUDITOR_SESSION,
                per_item_ballot_fn=fn,
            )
        )


# --------------------------------------------------------------------------- #
# produce_spec_jury_verdict -- advisory-until-blocking gate (TRUST-5, C2).
# --------------------------------------------------------------------------- #


def test_producer_veto_does_not_raise_under_advisory(tmp_path: Path) -> None:
    """A per-item FAIL is held advisory by default: the producer returns, never raises.

    C2 (advisory arm): under the default ADVISORY authority a refuted item
    minority-vetoes that item to FAIL, but the producer writes the FAIL verdict
    for the operator and RETURNS a scored result rather than raising -- an
    uncalibrated spec jury never blocks a close.
    """
    state, state_path = _write_state_with_auditor(tmp_path)
    fn = _ballot_fn_factory(
        {
            "claude-code": {"B1": (True, None), "B2": (True, None)},
            "codex": {"B1": (False, "B1 is not wired"), "B2": (True, None)},
            "opencode": {"B1": (True, None), "B2": (True, None)},
        }
    )
    result = _run(
        produce_spec_jury_verdict(
            state=state,
            state_path=state_path,
            wave=_make_wave(),
            spec=_make_wave_spec(),
            auditor_session_id=_AUDITOR_SESSION,
            per_item_ballot_fn=fn,
            block_authority=BlockAuthority.ADVISORY,
            now=_T0,
        )
    )
    # The FAIL was scored + written but did NOT raise (advisory hold).
    assert result.status == "scored"
    assert result.verdict is AgentReportVerdict.FAIL
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)
    assert rows[-1].payload.body.verdict is AgentReportVerdict.FAIL


def test_producer_veto_raises_under_blocking_authority(tmp_path: Path) -> None:
    """A per-item FAIL raises LifecycleError once the jury earns blocking authority.

    C2 (blocking arm): the SAME refuted item that is held advisory above raises
    :class:`LifecycleError` under BLOCKING authority -- a calibrated jury that
    has cleared its trust floors blocks the close. The verdict is still written
    before the raise so the operator sees the (now-blocking) veto.
    """
    state, state_path = _write_state_with_auditor(tmp_path)
    fn = _ballot_fn_factory(
        {
            "claude-code": {"B1": (True, None), "B2": (True, None)},
            "codex": {"B1": (False, "B1 is not wired"), "B2": (True, None)},
            "opencode": {"B1": (True, None), "B2": (True, None)},
        }
    )
    with pytest.raises(LifecycleError, match="spec jury vetoed close"):
        _run(
            produce_spec_jury_verdict(
                state=state,
                state_path=state_path,
                wave=_make_wave(),
                spec=_make_wave_spec(),
                auditor_session_id=_AUDITOR_SESSION,
                per_item_ballot_fn=fn,
                block_authority=BlockAuthority.BLOCKING,
                now=_T0,
            )
        )
    # The verdict was persisted before the raise -- the veto is recorded.
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)
    assert rows[-1].payload.body.verdict is AgentReportVerdict.FAIL


def test_producer_pass_does_not_raise_under_blocking_authority(tmp_path: Path) -> None:
    """A close-ready verdict never raises, even under blocking authority."""
    state, state_path = _write_state_with_auditor(tmp_path)
    fn = _ballot_fn_factory(
        {
            "claude-code": {"B1": (True, None), "B2": (True, None)},
            "codex": {"B1": (True, None), "B2": (True, None)},
            "opencode": {"B1": (True, None), "B2": (True, None)},
        }
    )
    result = _run(
        produce_spec_jury_verdict(
            state=state,
            state_path=state_path,
            wave=_make_wave(),
            spec=_make_wave_spec(),
            auditor_session_id=_AUDITOR_SESSION,
            per_item_ballot_fn=fn,
            block_authority=BlockAuthority.BLOCKING,
            now=_T0,
        )
    )
    assert result.status == "scored"
    assert result.verdict is AgentReportVerdict.PASS


# --------------------------------------------------------------------------- #
# live_per_item_ballot_fn -- drives disjoint juror runtimes through the loop.
# --------------------------------------------------------------------------- #


def _ballot_json(juror: str, item_votes: dict[str, tuple[bool, str | None]]) -> str:
    """Render one juror's per-item ballot as the JSON a spawn would emit."""
    votes = [
        {"item_id": item_id, "passed": passed, "refutation": refutation}
        for item_id, (passed, refutation) in item_votes.items()
    ]
    return json.dumps({"juror": juror, "votes": votes})


class _RecordingSpawn:
    """Replays one canned ballot body per call for a single juror runtime.

    *answers* is an ordered list of spawn texts: the first call replays the
    first answer, the second the second, etc. A short list lets a test force the
    bounded re-ask loop (a malformed answer followed by a valid one).
    """

    def __init__(self, runtime: str, answers: list[str]) -> None:
        self.runtime = runtime
        self._answers = answers
        self.calls = 0

    async def __call__(self, prompt: str) -> SpawnResult:
        idx = min(self.calls, len(self._answers) - 1)
        self.calls += 1
        return SpawnResult(
            session_id=f"sess-{self.runtime}-{self.calls}",
            runtime=self.runtime,
            model="model-x",
            subprocess_pid=4242,
            exit_status=0,
            text=self._answers[idx],
            started_at=_T0,
            ended_at=_T0,
        )


def _spawn_factory(stubs: dict[str, _RecordingSpawn]) -> Any:
    """Return a per-runtime SpawnFactory binding each runtime to its stub."""

    def _factory(runtime: str) -> SpawnFn:
        return stubs[runtime]  # type: ignore[return-value]

    return _factory


_RUNTIMES = ("claude-code", "codex", "opencode")


def test_live_ballot_fn_parses_one_ballot_per_juror() -> None:
    """The live fn drives each disjoint runtime to a parsed per-item ballot.

    Three runtimes each emit one valid per-item ballot; the live fn returns one
    PerItemJurorBallot per juror with the votes the spawn emitted. No real
    subprocess runs -- the spawn factory is a recording stub.
    """
    rubric = (
        WaveBehavior(
            id="B1",
            text="the close gate routes a banded wave through the spec jury",
            jury_scorable=True,
            quality_dimension="operability",
        ),
        WaveBehavior(
            id="B2",
            text="the producer is idle until a per-item ballot fn is injected",
            jury_scorable=True,
            quality_dimension="interaction_capability",
        ),
    )
    stubs = {
        rt: _RecordingSpawn(rt, [_ballot_json(rt, {"B1": (True, None), "B2": (True, None)})])
        for rt in _RUNTIMES
    }
    fn = live_per_item_ballot_fn(
        spawn_factory=_spawn_factory(stubs), rubric=rubric, runtimes=_RUNTIMES
    )
    ballots = _run(fn("# per-item prompt"))
    assert len(ballots) == 3
    assert {b.juror for b in ballots} == set(_RUNTIMES)
    for ballot in ballots:
        assert {v.item_id for v in ballot.votes} == {"B1", "B2"}
    # Each disjoint vendor lane spawned exactly once.
    for rt in _RUNTIMES:
        assert stubs[rt].calls == 1


def test_live_ballot_fn_reasks_on_malformed_then_parses() -> None:
    """A malformed first answer forces a bounded re-ask; the second answer parses."""
    rubric = (
        WaveBehavior(
            id="B1",
            text="the close gate routes a banded wave through the spec jury",
            jury_scorable=True,
            quality_dimension="operability",
        ),
    )
    # claude-code emits junk first, then a valid ballot on the re-ask.
    stubs = {
        "claude-code": _RecordingSpawn(
            "claude-code", ["not json at all", _ballot_json("claude-code", {"B1": (True, None)})]
        )
    }
    fn = live_per_item_ballot_fn(
        spawn_factory=_spawn_factory(stubs), rubric=rubric, runtimes=("claude-code",)
    )
    ballots = _run(fn("# per-item prompt"))
    assert len(ballots) == 1
    assert ballots[0].votes[0].item_id == "B1"
    # The loop re-asked once: two spawns total.
    assert stubs["claude-code"].calls == 2


def test_live_ballot_fn_abstains_when_loop_exhausts() -> None:
    """A juror whose every spawn is malformed abstains (no ballot), never crashes.

    The other two jurors still vote, so the live fn returns two ballots -- the
    abstention is orthogonal, mirroring the cross-vendor convener's contract.
    """
    rubric = (
        WaveBehavior(
            id="B1",
            text="the close gate routes a banded wave through the spec jury",
            jury_scorable=True,
            quality_dimension="operability",
        ),
    )
    stubs = {
        "claude-code": _RecordingSpawn("claude-code", ["junk"]),  # always malformed -> abstains
        "codex": _RecordingSpawn("codex", [_ballot_json("codex", {"B1": (True, None)})]),
        "opencode": _RecordingSpawn("opencode", [_ballot_json("opencode", {"B1": (True, None)})]),
    }
    fn = live_per_item_ballot_fn(
        spawn_factory=_spawn_factory(stubs), rubric=rubric, runtimes=_RUNTIMES, max_attempts=2
    )
    ballots = _run(fn("# per-item prompt"))
    # claude-code abstained (exhausted its 2 spawns); the other two voted.
    assert {b.juror for b in ballots} == {"codex", "opencode"}
    assert stubs["claude-code"].calls == 2


def test_live_ballot_fn_abstains_when_spawn_raises() -> None:
    """A juror whose bound spawn raises abstains rather than crashing the fn."""
    rubric = (
        WaveBehavior(
            id="B1",
            text="the close gate routes a banded wave through the spec jury",
            jury_scorable=True,
            quality_dimension="operability",
        ),
    )

    async def _raising_spawn(prompt: str) -> SpawnResult:
        raise RuntimeError("vendor CLI unavailable")

    def _factory(runtime: str) -> SpawnFn:
        if runtime == "claude-code":
            return _raising_spawn
        stub = _RecordingSpawn(runtime, [_ballot_json(runtime, {"B1": (True, None)})])
        return stub  # type: ignore[return-value]

    fn = live_per_item_ballot_fn(spawn_factory=_factory, rubric=rubric, runtimes=_RUNTIMES)
    ballots = _run(fn("# per-item prompt"))
    # The raising lane abstained; the other two voted.
    assert {b.juror for b in ballots} == {"codex", "opencode"}


def test_live_ballot_fn_drives_producer_to_per_item_report(tmp_path: Path) -> None:
    """End-to-end: the LIVE ballot fn drives the producer to a per-item report (C1).

    Wires the live fn (over recording stubs) into produce_spec_jury_verdict: the
    three jurors vote unanimous PASS, the producer reduces them and writes ONE
    per-item AuditorReportBody with a CriterionVerdict row per rubric item.
    """
    state, state_path = _write_state_with_auditor(tmp_path)
    rubric = (
        WaveBehavior(
            id="B1",
            text="the close gate routes a banded wave through the spec jury",
            jury_scorable=True,
            quality_dimension="operability",
        ),
        WaveBehavior(
            id="B2",
            text="the producer is idle until a per-item ballot fn is injected",
            jury_scorable=True,
            quality_dimension="interaction_capability",
        ),
    )
    stubs = {
        rt: _RecordingSpawn(rt, [_ballot_json(rt, {"B1": (True, None), "B2": (True, None)})])
        for rt in _RUNTIMES
    }
    fn = live_per_item_ballot_fn(
        spawn_factory=_spawn_factory(stubs), rubric=rubric, runtimes=_RUNTIMES
    )
    result = _run(
        produce_spec_jury_verdict(
            state=state,
            state_path=state_path,
            wave=_make_wave(),
            spec=_make_wave_spec(),
            auditor_session_id=_AUDITOR_SESSION,
            per_item_ballot_fn=fn,
            now=_T0,
        )
    )
    assert result.status == "scored"
    assert result.verdict is AgentReportVerdict.PASS
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)
    assert len(rows) == 1
    assert {c.criterion for c in rows[-1].payload.body.criteria} == {"B1", "B2"}
