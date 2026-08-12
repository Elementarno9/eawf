"""Unit tests for the cross-vendor disjoint-family jury convener.

Exercises :mod:`eawf.observability.eval.cross_vendor_jury`:

- three disjoint-family jurors (claude / codex / opencode) convened
  independently, one per vendor;
- the binary minority-veto reduction via
  :func:`eawf.observability.eval.jury.aggregate_jury` (unanimous PASS -> PASS,
  one FAIL -> veto FAIL, split-no-veto -> NEEDS_USER);
- the split / sub-quorum NEEDS_USER outcome surfaced to the operator;
- independence by construction (each juror's spawn receives ONLY the diff base
  + the success criteria, never another juror's ballot);
- abstention + quorum (a juror whose runtime spawn raises abstains; below
  quorum the convener routes to NEEDS_USER);
- boundary cases (all abstain, single voter).

Every juror's spawn is a recording stub keyed by runtime -- no real
``claude`` / ``codex`` / ``opencode`` subprocess, no network, no cost. The
stubs replay canned auditor-body JSON.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    AgentSessionStatus,
)
from eawf.kernel.state.models import State, Wave
from eawf.observability.eval.cross_vendor_jury import (
    JURY_QUORUM,
    JURY_RUNTIME_FAMILIES,
    CrossVendorJuryResult,
    JurorOutcome,
    convene_cross_vendor_jury,
)
from eawf.observability.eval.jury import JuryAggregateOutcome
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.dispatch.llm_assist import SpawnFn
from tests._criteria_helpers import legacy_criteria

_WAVE_ID = "P29-I04-W08"
_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC)
_CRITERIA = ["convene the jury", "reduce the ballots"]


# --------------------------------------------------------------------------- #
# Fixtures: canned auditor bodies, per-runtime recording stubs, on-disk state.
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
    """Recording stand-in for one runtime's ``spawn_session`` (no real process).

    Replays one canned answer per call and records each prompt so a test can
    assert the juror prompt was fresh-context. Raises if called more times than
    answers queued.
    """

    def __init__(self, runtime: str, answers: list[str]) -> None:
        self.runtime = runtime
        self._answers = list(answers)
        self.prompts: list[str] = []
        self.calls = 0

    async def __call__(self, prompt: str) -> SpawnResult:
        self.prompts.append(prompt)
        if self.calls >= len(self._answers):
            raise AssertionError(
                f"spawn for {self.runtime!r} called {self.calls + 1} times but only "
                f"{len(self._answers)} answer(s) queued"
            )
        text = self._answers[self.calls]
        self.calls += 1
        return _spawn_result(text, runtime=self.runtime)


class _RaisingSpawn:
    """Stand-in for an unavailable runtime: every spawn raises.

    Models a vendor outage (CLI missing, auth failure) so the convener records
    the juror as an abstention rather than crashing. Records that it was called.
    """

    def __init__(self, runtime: str, *, message: str = "runtime unavailable") -> None:
        self.runtime = runtime
        self._message = message
        self.calls = 0

    async def __call__(self, prompt: str) -> SpawnResult:
        self.calls += 1
        raise RuntimeError(f"{self.runtime}: {self._message}")


class _RecordingFactory:
    """Per-runtime spawn factory backed by a dict of stubs.

    Mirrors the production seam (``select_adapter(runtime).spawn_session``)
    without a real adapter: it hands back the stub registered for each runtime
    and records the order runtimes were requested in, so a test can assert one
    juror was convened per family.
    """

    def __init__(self, stubs: dict[str, SpawnFn]) -> None:
        self._stubs = stubs
        self.requested: list[str] = []

    def __call__(self, runtime: str) -> SpawnFn:
        self.requested.append(runtime)
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
        "updated_at": "2026-06-01T00:00:00Z",
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
            "phase_id": "P29",
            "iter_id": "P29-I04",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P29": {
                "id": "P29",
                "scope_id": "EAWF",
                "title": "v0.5",
                "status": "active",
                "iter_ids": ["P29-I04"],
                "outcome_ids": [],
                "opened_at": "2026-06-01T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P29-I04": {
                "id": "P29-I04",
                "phase_id": "P29",
                "title": "Trust",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-01T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P29-I04",
                "title": "convene a cross-vendor jury",
                "status": "claimed",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/observability/eval/cross_vendor_jury.py"],
                "success_criteria": [
                    c.model_dump(mode="json") for c in legacy_criteria(*_CRITERIA)
                ],
                "agent_role": "executor",
                "effort_bucket": "L",
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-01T00:00:00Z",
                "claimed_at": "2026-06-01T00:00:00Z",
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
    ea.mkdir()
    state_path = ea / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    events_path = ea / "store" / "event.jsonl"
    return state, state_path, events_path


def _convene(
    tmp_path: Path,
    *,
    factory: _RecordingFactory,
    runtimes: tuple[str, ...] = JURY_RUNTIME_FAMILIES,
    quorum: int = JURY_QUORUM,
) -> tuple[CrossVendorJuryResult, State, Path]:
    """Drive the convener over an on-disk state + return the result + paths."""
    state, state_path, events_path = _write_state(tmp_path)
    wave: Wave = state.waves[_WAVE_ID]
    result = asyncio.run(
        convene_cross_vendor_jury(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn_factory=factory,
            runtimes=runtimes,
            quorum=quorum,
            repo_root=tmp_path,
        )
    )
    return result, state, state_path


def _unanimous_pass_factory() -> _RecordingFactory:
    """A factory whose three jurors all return PASS."""
    return _RecordingFactory(
        {
            "claude-code": _RecordingSpawn("claude-code", [_auditor_body_json(verdict="pass")]),
            "codex": _RecordingSpawn("codex", [_auditor_body_json(verdict="pass")]),
            "opencode": _RecordingSpawn("opencode", [_auditor_body_json(verdict="pass")]),
        }
    )


# --------------------------------------------------------------------------- #
# Criterion (a): three disjoint-family jurors convened independently.
# --------------------------------------------------------------------------- #


def test_convene_cross_vendor_jury_convenes_one_juror_per_family(tmp_path: Path) -> None:
    """The convener spawns exactly one juror per disjoint runtime family."""
    factory = _unanimous_pass_factory()

    result, _state, _path = _convene(tmp_path, factory=factory)

    # One spawn-factory request per family, in declared order.
    assert factory.requested == list(JURY_RUNTIME_FAMILIES)
    # One JurorOutcome per family, each bound to its own runtime.
    assert tuple(j.runtime for j in result.jurors) == JURY_RUNTIME_FAMILIES
    assert len(result.jurors) == 3
    assert result.voted_count == 3
    assert result.abstained_count == 0


def test_convene_cross_vendor_jury_jurors_get_distinct_fresh_sessions(tmp_path: Path) -> None:
    """Each juror authors its own fresh AUDITOR session (independence)."""
    factory = _unanimous_pass_factory()

    result, _state, _path = _convene(tmp_path, factory=factory)

    session_ids = [j.session_id for j in result.jurors if j.session_id is not None]
    assert len(session_ids) == 3
    # Distinct sessions: no juror reuses another's authoring session.
    assert len(set(session_ids)) == 3


def test_convene_cross_vendor_jury_terminalizes_every_real_session(tmp_path: Path) -> None:
    """Successful jurors inherit the producer's central terminalizer."""
    result, state, _path = _convene(tmp_path, factory=_unanimous_pass_factory())

    session_ids = {j.session_id for j in result.jurors if j.session_id is not None}
    auditors = [
        session
        for session in state.agent_sessions.values()
        if session.role is AgentSessionRole.AUDITOR
    ]
    assert {session.id for session in auditors} == session_ids
    assert all(session.status is AgentSessionStatus.CLOSED for session in auditors)
    assert all(session.ended_at is not None for session in auditors)
    assert state.current.active_session_ids == []


def test_convene_cross_vendor_jury_each_juror_sees_only_diff_and_criteria(tmp_path: Path) -> None:
    """Independence by construction: a juror prompt carries criteria, no ballot.

    Each juror's spawn must receive a fresh-context auditor prompt scoped to the
    diff base + the wave's success criteria -- and must NOT carry another
    juror's verdict / session id (there is no peer channel).
    """
    stubs = {
        "claude-code": _RecordingSpawn("claude-code", [_auditor_body_json(verdict="pass")]),
        "codex": _RecordingSpawn("codex", [_auditor_body_json(verdict="fail")]),
        "opencode": _RecordingSpawn("opencode", [_auditor_body_json(verdict="pass")]),
    }
    factory = _RecordingFactory(dict(stubs))

    _result, _state, _path = _convene(tmp_path, factory=factory)

    for runtime, stub in stubs.items():
        assert isinstance(stub, _RecordingSpawn)
        assert len(stub.prompts) == 1, runtime
        prompt = stub.prompts[0]
        # Carries the wave + every success criterion (fresh-context audit).
        assert _WAVE_ID in prompt
        for criterion in _CRITERIA:
            assert criterion in prompt
        # Does NOT carry another juror's ballot / authoring session id.
        for other_runtime, other in stubs.items():
            if other_runtime == runtime:
                continue
            assert isinstance(other, _RecordingSpawn)
            assert f"sess-{other.runtime}" not in prompt


# --------------------------------------------------------------------------- #
# Criterion (b): the aggregate_jury reduction.
# --------------------------------------------------------------------------- #


def test_convene_cross_vendor_jury_unanimous_pass_is_pass(tmp_path: Path) -> None:
    """Three PASS ballots reduce to a clean PASS with no reasons."""
    factory = _unanimous_pass_factory()

    result, _state, _path = _convene(tmp_path, factory=factory)

    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.needs_user is False
    assert result.aggregate is not None
    assert result.aggregate.veto_count == 0
    assert result.aggregate.acceptance_style == "binary"
    assert result.reasons == ()


def test_convene_cross_vendor_jury_one_fail_minority_vetoes(tmp_path: Path) -> None:
    """A single FAIL ballot vetoes the vote to FAIL (minority-veto)."""
    factory = _RecordingFactory(
        {
            "claude-code": _RecordingSpawn("claude-code", [_auditor_body_json(verdict="pass")]),
            "codex": _RecordingSpawn("codex", [_auditor_body_json(verdict="fail")]),
            "opencode": _RecordingSpawn("opencode", [_auditor_body_json(verdict="pass")]),
        }
    )

    result, _state, _path = _convene(tmp_path, factory=factory)

    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.aggregate is not None
    assert result.aggregate.veto_count == 1
    assert any("minority-veto" in reason for reason in result.reasons)
    # The dissenting juror is recorded with its FAIL verdict.
    codex = next(j for j in result.jurors if j.runtime == "codex")
    assert codex.verdict is AgentReportVerdict.FAIL


def test_convene_cross_vendor_jury_blocked_ballot_vetoes(tmp_path: Path) -> None:
    """A single BLOCKED ballot is a veto, not a mere non-pass."""
    factory = _RecordingFactory(
        {
            "claude-code": _RecordingSpawn("claude-code", [_auditor_body_json(verdict="pass")]),
            "codex": _RecordingSpawn("codex", [_auditor_body_json(verdict="pass")]),
            "opencode": _RecordingSpawn("opencode", [_auditor_body_json(verdict="blocked")]),
        }
    )

    result, _state, _path = _convene(tmp_path, factory=factory)

    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.aggregate is not None
    assert result.aggregate.veto_count == 1


# --------------------------------------------------------------------------- #
# Criterion (c): a split -> NEEDS_USER surfaced to the operator.
# --------------------------------------------------------------------------- #


def test_convene_cross_vendor_jury_split_no_veto_needs_user(tmp_path: Path) -> None:
    """A pass / pass-with-followups split with no veto surfaces NEEDS_USER."""
    factory = _RecordingFactory(
        {
            "claude-code": _RecordingSpawn("claude-code", [_auditor_body_json(verdict="pass")]),
            "codex": _RecordingSpawn("codex", [_auditor_body_json(verdict="pass-with-followups")]),
            "opencode": _RecordingSpawn("opencode", [_auditor_body_json(verdict="pass")]),
        }
    )

    result, _state, _path = _convene(tmp_path, factory=factory)

    assert result.outcome is JuryAggregateOutcome.NEEDS_USER
    assert result.needs_user is True
    assert result.aggregate is not None
    assert result.aggregate.veto_count == 0
    assert any("no clean consensus" in reason for reason in result.reasons)


# --------------------------------------------------------------------------- #
# Abstention + quorum: an unavailable runtime abstains; below quorum -> NEEDS_USER.
# --------------------------------------------------------------------------- #


def test_convene_cross_vendor_jury_unavailable_runtime_abstains(tmp_path: Path) -> None:
    """A juror whose runtime spawn raises abstains; the rest still reduce."""
    raising = _RaisingSpawn("opencode")
    factory = _RecordingFactory(
        {
            "claude-code": _RecordingSpawn("claude-code", [_auditor_body_json(verdict="pass")]),
            "codex": _RecordingSpawn("codex", [_auditor_body_json(verdict="pass")]),
            "opencode": raising,
        }
    )

    result, _state, _path = _convene(tmp_path, factory=factory)

    # The unavailable runtime was attempted, then recorded as an abstention.
    assert raising.calls >= 1
    assert result.voted_count == 2
    assert result.abstained_count == 1
    opencode = next(j for j in result.jurors if j.runtime == "opencode")
    assert opencode.voted is False
    assert opencode.verdict is None
    assert opencode.error is not None
    assert "opencode" in opencode.error
    # Two PASS votes at quorum 2 still reduce to PASS.
    assert result.outcome is JuryAggregateOutcome.PASS
    assert any("abstained" in reason for reason in result.reasons)


def test_convene_cross_vendor_jury_abstention_does_not_mask_veto(tmp_path: Path) -> None:
    """An abstention plus a FAIL among the voters still vetoes to FAIL."""
    factory = _RecordingFactory(
        {
            "claude-code": _RecordingSpawn("claude-code", [_auditor_body_json(verdict="pass")]),
            "codex": _RecordingSpawn("codex", [_auditor_body_json(verdict="fail")]),
            "opencode": _RaisingSpawn("opencode"),
        }
    )

    result, _state, _path = _convene(tmp_path, factory=factory)

    assert result.voted_count == 2
    assert result.abstained_count == 1
    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.aggregate is not None
    assert result.aggregate.veto_count == 1


def test_convene_cross_vendor_jury_sub_quorum_needs_user(tmp_path: Path) -> None:
    """Fewer than quorum jurors voting routes to NEEDS_USER with no reduction."""
    factory = _RecordingFactory(
        {
            "claude-code": _RecordingSpawn("claude-code", [_auditor_body_json(verdict="pass")]),
            "codex": _RaisingSpawn("codex"),
            "opencode": _RaisingSpawn("opencode"),
        }
    )

    result, _state, _path = _convene(tmp_path, factory=factory)

    assert result.voted_count == 1
    assert result.abstained_count == 2
    assert result.outcome is JuryAggregateOutcome.NEEDS_USER
    assert result.needs_user is True
    # No reduction ran below quorum.
    assert result.aggregate is None
    assert any("sub-quorum" in reason for reason in result.reasons)


def test_convene_cross_vendor_jury_all_abstain_needs_user(tmp_path: Path) -> None:
    """Boundary: every juror abstaining routes to NEEDS_USER, no reduction."""
    factory = _RecordingFactory(
        {
            "claude-code": _RaisingSpawn("claude-code"),
            "codex": _RaisingSpawn("codex"),
            "opencode": _RaisingSpawn("opencode"),
        }
    )

    result, _state, _path = _convene(tmp_path, factory=factory)

    assert result.voted_count == 0
    assert result.abstained_count == 3
    assert result.outcome is JuryAggregateOutcome.NEEDS_USER
    assert result.aggregate is None
    assert all(j.voted is False for j in result.jurors)


def test_convene_cross_vendor_jury_single_voter_at_quorum_one(tmp_path: Path) -> None:
    """Boundary: a single juror at ``quorum=1`` resolves on its lone ballot."""
    factory = _RecordingFactory(
        {"claude-code": _RecordingSpawn("claude-code", [_auditor_body_json(verdict="pass")])}
    )

    result, _state, _path = _convene(
        tmp_path,
        factory=factory,
        runtimes=("claude-code",),
        quorum=1,
    )

    assert result.voted_count == 1
    assert len(result.jurors) == 1
    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.aggregate is not None
    assert result.aggregate.ballot_count == 1


# --------------------------------------------------------------------------- #
# Error path + model surface.
# --------------------------------------------------------------------------- #


def test_convene_cross_vendor_jury_empty_runtimes_raises(tmp_path: Path) -> None:
    """Error path: convening over no runtime families raises ValueError."""
    state, state_path, events_path = _write_state(tmp_path)
    wave = state.waves[_WAVE_ID]
    factory = _RecordingFactory({})

    with pytest.raises(ValueError, match="no runtime families"):
        asyncio.run(
            convene_cross_vendor_jury(
                state=state,
                state_path=state_path,
                events_path=events_path,
                wave=wave,
                spawn_factory=factory,
                runtimes=(),
                repo_root=tmp_path,
            )
        )


def test_juror_outcome_voted_property() -> None:
    """A juror with a verdict is ``voted``; one with only an error is not."""
    voter = JurorOutcome(
        runtime="claude-code",
        verdict=AgentReportVerdict.PASS,
        session_id="sess-x",
        attempts_used=1,
    )
    abstainer = JurorOutcome(runtime="codex", error="RuntimeError: down")

    assert voter.voted is True
    assert abstainer.voted is False


def test_cross_vendor_jury_result_rejects_extra_keys() -> None:
    """Error path: an unknown result key fails the extra='forbid' guard."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CrossVendorJuryResult(
            wave_id=_WAVE_ID,
            outcome=JuryAggregateOutcome.PASS,
            jurors=(),
            voted_count=0,
            abstained_count=0,
            surprise=True,  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
# P30-I23-W08: jurors convene concurrently; spawns carry a finite ceiling.
# --------------------------------------------------------------------------- #


class _SleepingSpawn:
    """Spawn stub that sleeps *delay* seconds before answering PASS."""

    def __init__(self, runtime: str, delay: float) -> None:
        self.runtime = runtime
        self.delay = delay
        self.started_at: float | None = None
        self.ended_at: float | None = None

    async def __call__(self, prompt: str) -> SpawnResult:
        self.started_at = time.monotonic()
        await asyncio.sleep(self.delay)
        self.ended_at = time.monotonic()
        return _spawn_result(_auditor_body_json(verdict="pass"), runtime=self.runtime)

    def interval(self) -> tuple[float, float]:
        if self.started_at is None or self.ended_at is None:
            raise AssertionError(f"spawn for {self.runtime!r} was not called")
        return self.started_at, self.ended_at


def test_convene_cross_vendor_jury_runs_jurors_concurrently(tmp_path: Path) -> None:
    """CR-02: three jurors sleeping t overlap instead of running sequentially.

    The juror loop gathers concurrently, so jury wall time is max(juror),
    not sum(juror). The assertion uses recorded spawn intervals rather than
    process wall time, so suite load cannot make a concurrent run look
    sequential.
    """
    delay = 0.4
    stubs = {
        "claude-code": _SleepingSpawn("claude-code", delay),
        "codex": _SleepingSpawn("codex", delay),
        "opencode": _SleepingSpawn("opencode", delay),
    }
    factory = _RecordingFactory(dict(stubs))  # type: ignore[arg-type]
    result, _state, _path = _convene(tmp_path, factory=factory)

    assert result.outcome.value == "pass"

    intervals = [stub.interval() for stub in stubs.values()]
    latest_start = max(start for start, _end in intervals)
    earliest_end = min(end for _start, end in intervals)
    assert latest_start < earliest_end, "juror spawn intervals did not overlap"


def test_daemon_jury_spawn_factory_passes_finite_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-01: every close-time juror/auditor spawn carries a finite ceiling.

    The daemon's ``_jury_spawn_factory`` threads an explicit wall-clock
    ``timeout`` into ``adapter.spawn_session`` (default 600s; the close
    gate resolves it from ``verify.juror_wall_clock_seconds``), so a hung
    vendor CLI errors out instead of holding the state lock forever.
    """
    from eawf.runtime.daemon.methods import state as daemon_state

    seen: dict[str, Any] = {}

    class _Adapter:
        async def spawn_session(self, prompt: str, **kwargs: Any) -> SpawnResult:
            seen.update(kwargs)
            return _spawn_result(_auditor_body_json(verdict="pass"), runtime="claude-code")

    monkeypatch.setattr("eawf.runtime.runtimes.selector.select_adapter", lambda runtime: _Adapter())
    state, _state_path, _events_path = _write_state(tmp_path)
    wave: Wave = state.waves[_WAVE_ID]

    factory = daemon_state._jury_spawn_factory(
        state, wave, repo_root=tmp_path, timeout_seconds=123.0
    )
    asyncio.run(factory("claude-code")("prompt"))

    assert seen.get("timeout") == 123.0

    # The default ceiling is finite too -- never a wait-forever spawn.
    seen.clear()
    default_factory = daemon_state._jury_spawn_factory(state, wave, repo_root=tmp_path)
    asyncio.run(default_factory("claude-code")("prompt"))
    assert seen.get("timeout") == 600.0


def _persisted_chunk_scopes(event_path: Path) -> list[str]:
    """Return the envelope ``scope_id`` of every ``agent.output.chunk`` row on disk."""
    if not event_path.exists():
        return []
    scopes: list[str] = []
    for line in event_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        env = json.loads(line)
        if env.get("payload", {}).get("event_type") == "agent.output.chunk":
            scopes.append(env["scope_id"])
    return scopes


def test_daemon_jury_spawn_factory_streams_chunks_to_auditor_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W21: with events_path threaded, a juror spawn streams stdout to the auditor scope.

    The daemon's ``_jury_spawn_factory`` binds an ``on_chunk`` callback that
    persists each stdout batch to the auditor session scope
    (``{wave}::audit``), so the Watch store-poll tail renders the juror's own
    output live -- the fix for auditor/juror spawns that previously wired no
    live output.
    """
    from eawf.runtime.daemon.methods import state as daemon_state
    from eawf.workflow.dispatch.verdict import _auditor_scope_id

    class _StreamingAdapter:
        async def spawn_session(self, prompt: str, **kwargs: Any) -> SpawnResult:
            on_chunk = kwargs.get("on_chunk")
            assert on_chunk is not None, "expected on_chunk bound when events_path is threaded"
            await on_chunk("juror scoring criterion one\n")
            await on_chunk("verdict: pass\n")
            return _spawn_result(_auditor_body_json(verdict="pass"), runtime="claude-code")

    monkeypatch.setattr(
        "eawf.runtime.runtimes.selector.select_adapter", lambda runtime: _StreamingAdapter()
    )
    state, _state_path, events_path = _write_state(tmp_path)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    wave: Wave = state.waves[_WAVE_ID]

    factory = daemon_state._jury_spawn_factory(
        state, wave, repo_root=tmp_path, events_path=events_path
    )
    asyncio.run(factory("claude-code")("prompt"))

    scopes = _persisted_chunk_scopes(events_path)
    expected = _auditor_scope_id(_WAVE_ID)
    assert scopes, "expected at least one agent.output.chunk persisted"
    assert set(scopes) == {expected}


def test_daemon_jury_spawn_factory_no_events_path_binds_no_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W21: without events_path, the spawn binds no on_chunk and persists no chunk."""
    from eawf.runtime.daemon.methods import state as daemon_state

    seen: dict[str, Any] = {}

    class _Adapter:
        async def spawn_session(self, prompt: str, **kwargs: Any) -> SpawnResult:
            seen.update(kwargs)
            return _spawn_result(_auditor_body_json(verdict="pass"), runtime="claude-code")

    monkeypatch.setattr("eawf.runtime.runtimes.selector.select_adapter", lambda runtime: _Adapter())
    state, _state_path, events_path = _write_state(tmp_path)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    wave: Wave = state.waves[_WAVE_ID]

    factory = daemon_state._jury_spawn_factory(state, wave, repo_root=tmp_path)
    asyncio.run(factory("claude-code")("prompt"))

    assert "on_chunk" not in seen
    assert _persisted_chunk_scopes(events_path) == []
