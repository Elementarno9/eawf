"""Unit tests for the live per-wave fresh-context auditor verdict producer.

Covers the three layers of :mod:`eawf.workflow.dispatch.verdict` (P29-I04-W07):

- the pure risk-weighted policy :func:`verdict_requirement` (boundary cases
  per effort bucket, judgment role, security keyword, sampler);
- the pure close gate :func:`verify_wave_verdict_gate` (blocks only the
  required subset; a skip wave never blocks; a FAIL verdict blocks);
- the live producer :func:`produce_wave_verdict` (registers a FRESH AUDITOR
  session distinct from the executor, appends an ``AuditorReportBody`` at
  ``base_id=wave_id``, fresh-context prompt carries only diff-base +
  success_criteria, append-only retry, self-report rejection).

The spawn is ALWAYS a recording stub -- no real ``claude`` subprocess, no
network, no auth, no cost. The stub replays canned auditor-body JSON.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.wave import QualityDimension, WaveBehavior
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    AgentSessionStatus,
    Confidence,
)
from eawf.kernel.state.models import State, Wave
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.agent_report.rollup import iter_agent_reports
from eawf.workflow.agent_report.store import AgentReportRoleMismatchError
from eawf.workflow.dispatch.llm_assist import LLMAssistError
from eawf.workflow.dispatch.verdict import (
    ExecutorSelfReportError,
    WaveVerdictGate,
    _is_security_scoped,
    assert_not_executor_self_report,
    build_auditor_prompt,
    parse_auditor_report_body,
    produce_wave_verdict,
    verdict_requirement,
    verify_wave_verdict_gate,
)
from tests._criteria_helpers import legacy_criteria

_WAVE_ID = "P29-I04-W07"
_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures: a canned auditor SpawnResult + recording stub + on-disk state.
# --------------------------------------------------------------------------- #


def _auditor_body_json(
    *,
    verdict: str = "pass",
    target_id: str = _WAVE_ID,
    role: str = "auditor",
) -> str:
    """Serialise a minimal schema-valid auditor ``agent_end`` body to JSON."""
    return json.dumps(
        {
            "role": role,
            "verdict": verdict,
            "confidence": "high",
            "summary": "re-read the diff against the criteria",
            "target_id": target_id,
            "criteria": [
                {"criterion": "ship the producer", "passed": True},
                {
                    "criterion": "test the producer",
                    "passed": verdict in {"pass", "pass-with-followups"},
                },
            ],
            "refutations": [],
        }
    )


def _spawn_result(text: str) -> SpawnResult:
    """Wrap *text* in an otherwise-valid :class:`SpawnResult` envelope."""
    return SpawnResult(
        session_id="sess-auditor-xyz",
        runtime="claude-code",
        model="opus",
        subprocess_pid=7777,
        exit_status=0,
        text=text,
        started_at=_T0,
        ended_at=_T1,
    )


class _RecordingSpawn:
    """Recording stand-in for a live ``spawn_session`` (NEVER a real process).

    Replays one canned answer per call and records each prompt so a test can
    assert the auditor prompt was fresh-context (carried the criteria, not
    the executor's report). Raises if called more times than answers queued.
    """

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []
        self.calls = 0

    async def __call__(self, prompt: str) -> SpawnResult:
        self.prompts.append(prompt)
        if self.calls >= len(self._answers):
            raise AssertionError(
                f"spawn called {self.calls + 1} times but only "
                f"{len(self._answers)} answer(s) queued"
            )
        text = self._answers[self.calls]
        self.calls += 1
        return _spawn_result(text)


def _state_payload(
    *,
    agent_role: str = "executor",
    effort_bucket: str = "L",
    success_criteria: list[str] | None = None,
    title: str = "live verdict producer",
    extra_sessions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A minimal valid State with the full phase -> iter -> wave chain.

    The chain is required because the producer projects the auditor role
    contract (which reads ``state.sandbox_policies``) and the gate resolves
    the wave from ``state.waves``. ``agent_sessions`` starts empty unless
    *extra_sessions* seeds an executor session (for the self-report path).
    """
    criteria_texts = (
        success_criteria
        if success_criteria is not None
        else [
            "ship the producer",
            "test the producer",
        ]
    )
    criteria = [c.model_dump(mode="json") for c in legacy_criteria(*criteria_texts)]
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
                "title": "Spawn spine",
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
                "title": title,
                "status": "claimed",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/workflow/dispatch/verdict.py"],
                "success_criteria": criteria,
                "agent_role": agent_role,
                "effort_bucket": effort_bucket,
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
        "agent_sessions": extra_sessions or {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, **kwargs: Any) -> tuple[State, Path, Path]:
    """Serialise a valid State to ``<tmp>/.ea/state.json`` + return paths."""
    state = State.model_validate(_state_payload(**kwargs))
    ea = tmp_path / ".ea"
    ea.mkdir()
    state_path = ea / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    events_path = ea / "store" / "event.jsonl"
    return state, state_path, events_path


def _make_wave(
    *,
    wave_id: str = _WAVE_ID,
    agent_role: str | None = "executor",
    effort_bucket: str | None = "S",
    title: str = "mechanical edit",
    success_criteria: list[str] | None = None,
) -> Wave:
    """Build a standalone :class:`Wave` for the pure-policy / gate tests."""
    payload = _state_payload(
        agent_role=agent_role or "executor",
        effort_bucket=effort_bucket or "S",
        title=title,
        success_criteria=success_criteria,
    )
    wave_payload = payload["waves"][_WAVE_ID]
    wave_payload["id"] = wave_id
    wave_payload["agent_role"] = agent_role
    wave_payload["effort_bucket"] = effort_bucket
    return Wave.model_validate(wave_payload)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Criterion 5: risk-weighted policy (pure-function tests).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bucket", ["L", "XL"])
def test_verdict_requirement_large_effort_is_always(bucket: str) -> None:
    """A large effort bucket (L / XL) forces an ``always`` requirement."""
    wave = _make_wave(agent_role="executor", effort_bucket=bucket)
    assert verdict_requirement(wave) == "always"


@pytest.mark.parametrize(
    "role",
    ["auditor", "reviewer", "planner", "researcher", "domain-specialist", "operator"],
)
def test_verdict_requirement_judgment_role_is_always(role: str) -> None:
    """A judgment-heavy agent_role forces ``always`` even at small effort."""
    wave = _make_wave(agent_role=role, effort_bucket="XS")
    assert verdict_requirement(wave) == "always"


@pytest.mark.parametrize("keyword", ["security", "sandbox", "auth", "egress", "jail", "scrub"])
def test_verdict_requirement_security_keyword_is_always(keyword: str) -> None:
    """A security-scoped wave (keyword in title) forces ``always``."""
    wave = _make_wave(agent_role="executor", effort_bucket="XS", title=f"harden the {keyword} path")
    assert verdict_requirement(wave) == "always"


def test_verdict_requirement_security_keyword_in_criteria_is_always() -> None:
    """The security signal scans success_criteria too, not only the title."""
    wave = _make_wave(
        agent_role="executor",
        effort_bucket="S",
        title="mechanical edit",
        success_criteria=["enforce the sandbox deny-list at dispatch"],
    )
    assert verdict_requirement(wave) == "always"


def test_is_security_scoped_auth_word_boundary_drops_code_keeps_word() -> None:
    """Whole-word scan: an ``AUTH-3`` code is not scoped; a real word is.

    CR-01: the bare-substring scan armed ``"auth"`` on the wave code
    ``AUTH-3``; the word-boundary scan drops that false positive while a
    standalone ``"auth"`` in prose still classifies security-scoped.
    """
    code_wave = _make_wave(agent_role="executor", effort_bucket="XS", title="AUTH-3")
    word_wave = _make_wave(agent_role="executor", effort_bucket="XS", title="harden the auth flow")
    assert _is_security_scoped(code_wave) is False
    assert _is_security_scoped(word_wave) is True


def test_is_security_scoped_ignores_substring_embeddings() -> None:
    """A keyword embedded in a larger word is not a whole-word match.

    ``"egress"`` inside ``"regression"`` and ``"auth"`` inside
    ``"authority"`` are the dominant P30 false positives the word-boundary
    scan removes.
    """
    regression_wave = _make_wave(
        agent_role="executor", effort_bucket="XS", title="fix a regression in the authority map"
    )
    assert _is_security_scoped(regression_wave) is False


def test_verdict_requirement_auth_code_title_is_mechanical() -> None:
    """A small executor wave titled with an ``AUTH-N`` code stays mechanical.

    CR-01 at the policy layer: the code no longer inflates the wave to an
    ``"always"`` requirement, so it is sampled or skipped like any other
    mechanical wave.
    """
    wave = _make_wave(agent_role="executor", effort_bucket="XS", title="AUTH-3")
    assert verdict_requirement(wave) in {"sampled", "skip"}


def test_verdict_requirement_auth_word_title_is_always() -> None:
    """A standalone ``auth`` word still forces an ``always`` requirement."""
    wave = _make_wave(agent_role="executor", effort_bucket="XS", title="harden the auth flow")
    assert verdict_requirement(wave) == "always"


def test_verdict_requirement_mechanical_executor_is_sampled_or_skip() -> None:
    """A small mechanical executor wave is sampled or skipped, never always."""
    wave = _make_wave(agent_role="executor", effort_bucket="S", title="rename a helper")
    assert verdict_requirement(wave) in {"sampled", "skip"}


def test_verdict_requirement_sampler_is_deterministic() -> None:
    """The sampler is a stable hash -- the same wave always lands the same way."""
    wave = _make_wave(agent_role="executor", effort_bucket="XS", title="tidy imports")
    first = verdict_requirement(wave)
    for _ in range(5):
        assert verdict_requirement(wave) == first


def test_verdict_requirement_sample_every_one_forces_sampled() -> None:
    """``sample_every=1`` selects every mechanical wave (boundary case)."""
    wave = _make_wave(agent_role="executor", effort_bucket="XS", title="tidy imports")
    assert verdict_requirement(wave, sample_every=1) == "sampled"


def test_verdict_requirement_unspecified_role_and_effort_is_mechanical() -> None:
    """A wave with no role + no effort is mechanical (sampled / skip)."""
    wave = _make_wave(agent_role=None, effort_bucket=None, title="touch a comment")
    assert verdict_requirement(wave) in {"sampled", "skip"}


def test_verdict_requirement_medium_executor_is_mechanical() -> None:
    """Medium effort is below the high-risk L/XL floor -- mechanical."""
    wave = _make_wave(agent_role="executor", effort_bucket="M", title="add a flag")
    assert verdict_requirement(wave) in {"sampled", "skip"}


# --------------------------------------------------------------------------- #
# Criterion 5 (gate half): the close gate blocks only the required subset.
# --------------------------------------------------------------------------- #


def test_verify_gate_high_risk_no_verdict_blocks(tmp_path: Path) -> None:
    """An always-wave with no auditor verdict blocks close."""
    _state, state_path, _events = _write_state(tmp_path, effort_bucket="L")
    wave = _state.waves[_WAVE_ID]
    gate = verify_wave_verdict_gate(wave, state_path=state_path)
    assert gate.requirement == "always"
    assert gate.passed is False
    assert gate.verdict is None
    assert any("no fresh auditor verdict" in reason for reason in gate.reasons)


def test_verify_gate_high_risk_fail_verdict_blocks(tmp_path: Path) -> None:
    """An always-wave whose fresh auditor verdict is FAIL blocks close."""
    state, state_path, events_path = _write_state(tmp_path, effort_bucket="L")
    wave = state.waves[_WAVE_ID]
    _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn([_auditor_body_json(verdict="fail")]),
            repo_root=tmp_path,
        )
    )
    gate = verify_wave_verdict_gate(wave, state_path=state_path)
    assert gate.passed is False
    assert gate.verdict is AgentReportVerdict.FAIL
    assert any("not in close-ready set" in reason for reason in gate.reasons)


def test_verify_gate_high_risk_pass_verdict_does_not_block(tmp_path: Path) -> None:
    """An always-wave with a clean PASS auditor verdict permits close."""
    state, state_path, events_path = _write_state(tmp_path, effort_bucket="L")
    wave = state.waves[_WAVE_ID]
    _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn([_auditor_body_json(verdict="pass")]),
            repo_root=tmp_path,
        )
    )
    gate = verify_wave_verdict_gate(wave, state_path=state_path)
    assert gate.passed is True
    assert gate.verdict is AgentReportVerdict.PASS


def test_verify_gate_pass_with_followups_does_not_block(tmp_path: Path) -> None:
    """``pass-with-followups`` is close-ready -- it does not block."""
    state, state_path, events_path = _write_state(tmp_path, effort_bucket="XL")
    wave = state.waves[_WAVE_ID]
    _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn([_auditor_body_json(verdict="pass-with-followups")]),
            repo_root=tmp_path,
        )
    )
    gate = verify_wave_verdict_gate(wave, state_path=state_path)
    assert gate.passed is True


def test_verify_gate_skip_wave_never_blocks(tmp_path: Path) -> None:
    """A mechanical wave the sampler skipped does not block on a missing verdict.

    Chooses a wave id whose deterministic sampler bucket is non-zero so the
    requirement is ``skip``; the gate then passes despite zero auditor rows.
    """
    # Find a mechanical wave id that the sampler skips.
    skip_wave: Wave | None = None
    for suffix in range(1, 40):
        candidate = _make_wave(
            wave_id=f"P29-I04-W{suffix:02d}",
            agent_role="executor",
            effort_bucket="S",
            title="mechanical edit",
        )
        if verdict_requirement(candidate) == "skip":
            skip_wave = candidate
            break
    assert skip_wave is not None, "expected at least one skipped mechanical wave id"

    _state, state_path, _events = _write_state(tmp_path, effort_bucket="S")
    gate = verify_wave_verdict_gate(skip_wave, state_path=state_path)
    assert gate.requirement == "skip"
    assert gate.passed is True
    assert gate.verdict is None


def test_verify_gate_returns_typed_outcome(tmp_path: Path) -> None:
    """The gate returns a frozen :class:`WaveVerdictGate` (closed schema)."""
    _state, state_path, _events = _write_state(tmp_path, effort_bucket="L")
    wave = _state.waves[_WAVE_ID]
    gate = verify_wave_verdict_gate(wave, state_path=state_path)
    assert isinstance(gate, WaveVerdictGate)
    assert gate.wave_id == _WAVE_ID


# --------------------------------------------------------------------------- #
# Criteria 1-4: the live producer registers a fresh AUDITOR session and
# appends an AuditorReportBody at base_id=wave_id.
# --------------------------------------------------------------------------- #


def test_produce_registers_fresh_auditor_session(tmp_path: Path) -> None:
    """A successful producer closes its fresh AUDITOR session."""
    state, state_path, events_path = _write_state(tmp_path, effort_bucket="L")
    wave = state.waves[_WAVE_ID]
    result = _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn([_auditor_body_json()]),
            repo_root=tmp_path,
        )
    )
    sessions = [s for s in state.agent_sessions.values() if s.role is AgentSessionRole.AUDITOR]
    assert len(sessions) == 1
    auditor = sessions[0]
    # The auditor session scopes to the verdict-qualified wave scope so it
    # coexists with the executor's wave-scoped session; the verdict's join
    # key (report base_id) is the bare wave id (asserted separately).
    assert auditor.scope_id == f"{_WAVE_ID}::audit"
    assert auditor.status is AgentSessionStatus.CLOSED
    assert auditor.ended_at is not None
    assert auditor.id not in state.current.active_session_ids
    assert result.auditor_session_id == auditor.id


def test_produce_auditor_session_distinct_from_executor(tmp_path: Path) -> None:
    """The verdict author is a fresh AUDITOR session, NOT the executor's.

    Seeds an ACTIVE executor session for the same wave first, then produces:
    the auditor session id must differ and the role must be AUDITOR.
    """
    executor_session: dict[str, Any] = {
        "SES-EXEC": {
            "id": "SES-EXEC",
            "role": "executor",
            "runtime": "claude-code",
            "scope_id": _WAVE_ID,
            "status": "active",
            "claimed_wave_ids": [],
            "worktree_ids": [],
            "artifact_ids": [],
            "started_at": "2026-06-01T00:00:00Z",
            "ended_at": None,
            "summary": None,
        }
    }
    state, state_path, events_path = _write_state(
        tmp_path, effort_bucket="L", extra_sessions=executor_session
    )
    wave = state.waves[_WAVE_ID]
    result = _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn([_auditor_body_json()]),
            repo_root=tmp_path,
        )
    )
    assert result.auditor_session_id != "SES-EXEC"
    author = state.agent_sessions[result.auditor_session_id]
    assert author.role is AgentSessionRole.AUDITOR


def test_produce_appends_auditor_report_at_base_id_wave_id(tmp_path: Path) -> None:
    """An AuditorReportBody lands in the auditor store at base_id=wave_id."""
    state, state_path, events_path = _write_state(tmp_path, effort_bucket="L")
    wave = state.waves[_WAVE_ID]
    _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn([_auditor_body_json(verdict="pass")]),
            repo_root=tmp_path,
        )
    )
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload.header.role is AgentSessionRole.AUDITOR
    assert payload.header.base_id == _WAVE_ID
    assert payload.body.role == "auditor"
    assert payload.body.verdict is AgentReportVerdict.PASS
    # The auditor body carries one CriterionVerdict per success criterion.
    assert payload.body.target_id == _WAVE_ID
    assert len(payload.body.criteria) == 2


def test_produce_report_session_id_is_the_auditor_session(tmp_path: Path) -> None:
    """The persisted report's header session id is the fresh auditor session."""
    state, state_path, events_path = _write_state(tmp_path, effort_bucket="L")
    wave = state.waves[_WAVE_ID]
    result = _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn([_auditor_body_json()]),
            repo_root=tmp_path,
        )
    )
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)
    assert rows[0].payload.header.session_id == result.auditor_session_id


# --------------------------------------------------------------------------- #
# Criterion 4: append-only retry -- a second produce appends attempt 2.
# --------------------------------------------------------------------------- #


def test_produce_retry_appends_attempt_two(tmp_path: Path) -> None:
    """A second produce appends attempt 2 (does not overwrite attempt 1)."""
    state, state_path, events_path = _write_state(tmp_path, effort_bucket="L")
    wave = state.waves[_WAVE_ID]
    first = _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn([_auditor_body_json(verdict="fail")]),
            repo_root=tmp_path,
        )
    )
    second = _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn([_auditor_body_json(verdict="pass")]),
            repo_root=tmp_path,
        )
    )
    assert first.append_result.attempt == 1
    assert second.append_result.attempt == 2
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)
    assert [r.payload.header.attempt for r in rows] == [1, 2]
    # Attempt 1 (the FAIL) is still on disk -- not overwritten.
    assert rows[0].payload.body.verdict is AgentReportVerdict.FAIL
    assert rows[1].payload.body.verdict is AgentReportVerdict.PASS


def test_produce_retry_creates_distinct_terminal_auditor_session(tmp_path: Path) -> None:
    """Each report retry mints a distinct terminal auditor session."""
    state, state_path, events_path = _write_state(tmp_path, effort_bucket="L")
    wave = state.waves[_WAVE_ID]
    result_ids: list[str] = []
    for verdict in ("fail", "pass"):
        result = _run(
            produce_wave_verdict(
                state=state,
                state_path=state_path,
                events_path=events_path,
                wave=wave,
                spawn=_RecordingSpawn([_auditor_body_json(verdict=verdict)]),
                repo_root=tmp_path,
            )
        )
        result_ids.append(result.auditor_session_id)
    auditors = [s for s in state.agent_sessions.values() if s.role is AgentSessionRole.AUDITOR]
    assert len(auditors) == 2
    assert len(set(result_ids)) == 2
    assert all(session.status is AgentSessionStatus.CLOSED for session in auditors)
    assert state.current.active_session_ids == []


# --------------------------------------------------------------------------- #
# Criterion 1: the fresh-context prompt carries ONLY diff-base + criteria.
# --------------------------------------------------------------------------- #


def test_produce_prompt_is_fresh_context(tmp_path: Path) -> None:
    """The auditor prompt carries the success criteria + a diff range only.

    It must NOT embed an executor report or working narrative -- the author
    re-reads the diff cold.
    """
    state, state_path, events_path = _write_state(
        tmp_path,
        effort_bucket="L",
        success_criteria=["criterion-alpha-ship", "criterion-beta-test"],
    )
    wave = state.waves[_WAVE_ID]
    spawn = _RecordingSpawn([_auditor_body_json()])
    _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=spawn,
            repo_root=tmp_path,
        )
    )
    prompt = spawn.prompts[0]
    assert "criterion-alpha-ship" in prompt
    assert "criterion-beta-test" in prompt
    assert "git diff" in prompt
    assert _WAVE_ID in prompt
    # No executor report body leaked into the auditor prompt.
    assert "ExecutorReportBody" not in prompt
    assert "executor answer" not in prompt.lower()


def test_build_auditor_prompt_no_criteria_renders_placeholder() -> None:
    """A wave with no success criteria still renders a fresh-context prompt."""
    wave = _make_wave(agent_role="executor", effort_bucket="L", success_criteria=[])
    prompt = build_auditor_prompt(wave, diff_base="abc123~1")
    assert "no explicit success criteria" in prompt
    assert "git diff abc123~1...HEAD" in prompt


# --------------------------------------------------------------------------- #
# K2 (P29-I08-W02): rubric + provenance-pinned evidence, refute-first stance.
# --------------------------------------------------------------------------- #


def _rubric_pair() -> list[WaveBehavior]:
    """Two jury-scorable behaviours on distinct quality dimensions."""
    return [
        WaveBehavior(
            id="B1",
            text="renders the scope breadcrumb on the header line every frame",
            quality_dimension=QualityDimension.INTERACTION_CAPABILITY,
            jury_scorable=True,
        ),
        WaveBehavior(
            id="B2",
            text="refuses an egress call the sandbox deny-list forbids",
            quality_dimension=QualityDimension.SECURITY,
            jury_scorable=True,
        ),
    ]


def test_build_auditor_prompt_rubric_emits_refute_line_per_item() -> None:
    """Each rubric item gets a refute instruction keyed by id + dimension."""
    wave = _make_wave(agent_role="executor", effort_bucket="L", success_criteria=["c1"])
    prompt = build_auditor_prompt(wave, diff_base="abc123~1", rubric=_rubric_pair())
    # Every behaviour id appears so a downstream ballot can match per item.
    assert "B1" in prompt
    assert "B2" in prompt
    # Each behaviour names its own quality dimension.
    assert "interaction_capability" in prompt
    assert "security" in prompt
    # The refute-first stance is explicit, per item.
    assert prompt.count("DISPROVE") >= 2
    assert "refute-first" in prompt.lower()


def test_build_auditor_prompt_refute_first_stance_wording_present() -> None:
    """The disprove-first / default-to-fail stance wording is present."""
    wave = _make_wave(agent_role="executor", effort_bucket="L", success_criteria=["c1"])
    prompt = build_auditor_prompt(wave, diff_base="abc123~1", rubric=_rubric_pair())
    lowered = prompt.lower()
    assert "disprove" in lowered
    assert "refute" in lowered
    assert "default to fail" in lowered


def test_build_auditor_prompt_no_diff_omits_diff_keeps_evidence() -> None:
    """include_diff=False drops the diff section but keeps the evidence block."""
    wave = _make_wave(agent_role="executor", effort_bucket="L", success_criteria=["c1"])
    evidence = "EVIDENCE-PIN-7f3a provenance-pinned ballot grounding line"
    prompt = build_auditor_prompt(
        wave,
        diff_base="abc123~1",
        rubric=_rubric_pair(),
        evidence_block=evidence,
        include_diff=False,
    )
    # The diff section (the git-diff instruction) is absent entirely.
    assert "git diff" not in prompt
    assert "## Diff under audit" not in prompt
    # The provenance-pinned evidence is present under its own heading.
    assert "## Evidence" in prompt
    assert evidence in prompt


def test_build_auditor_prompt_evidence_present_with_diff_default() -> None:
    """An evidence block coexists with the diff section when include_diff stays True."""
    wave = _make_wave(agent_role="executor", effort_bucket="L", success_criteria=["c1"])
    evidence = "EVIDENCE-PIN-aa01 grounding text"
    prompt = build_auditor_prompt(wave, diff_base="abc123~1", evidence_block=evidence)
    assert "git diff abc123~1...HEAD" in prompt
    assert "## Evidence" in prompt
    assert evidence in prompt


def test_build_auditor_prompt_back_compat_shape_unchanged() -> None:
    """Calling with neither rubric nor evidence matches the legacy section shape.

    The base-case prompt must still carry the four legacy sections in
    order and omit the new rubric / evidence headings, so existing callers
    and their string-level expectations stay green.
    """
    wave = _make_wave(
        agent_role="executor",
        effort_bucket="L",
        success_criteria=["criterion-alpha", "criterion-beta"],
    )
    prompt = build_auditor_prompt(wave, diff_base="abc123~1")
    assert "## Diff under audit" in prompt
    assert "## Success criteria" in prompt
    assert "## Output contract" in prompt
    # New optional sections are absent in the back-compat call.
    assert "## Evidence" not in prompt
    assert "## Rubric" not in prompt
    assert "DISPROVE" not in prompt
    # Legacy section order is preserved.
    assert prompt.index("## Diff under audit") < prompt.index("## Success criteria")
    assert prompt.index("## Success criteria") < prompt.index("## Output contract")


# --------------------------------------------------------------------------- #
# Criterion 3: an executor self-report MUST be rejected (fail-fast typed).
# --------------------------------------------------------------------------- #


def test_assert_not_executor_self_report_rejects_executor(tmp_path: Path) -> None:
    """Feeding the executor's own session as the author raises typed."""
    executor_session: dict[str, Any] = {
        "SES-EXEC": {
            "id": "SES-EXEC",
            "role": "executor",
            "runtime": "claude-code",
            "scope_id": _WAVE_ID,
            "status": "active",
            "claimed_wave_ids": [],
            "worktree_ids": [],
            "artifact_ids": [],
            "started_at": "2026-06-01T00:00:00Z",
            "ended_at": None,
            "summary": None,
        }
    }
    state, _state_path, _events = _write_state(
        tmp_path, effort_bucket="L", extra_sessions=executor_session
    )
    with pytest.raises(ExecutorSelfReportError, match="cannot author its own"):
        assert_not_executor_self_report(state, wave_id=_WAVE_ID, author_session_id="SES-EXEC")


def test_assert_not_executor_self_report_allows_auditor(tmp_path: Path) -> None:
    """An auditor author passes the self-report guard unscathed."""
    auditor_session: dict[str, Any] = {
        "SES-AUD": {
            "id": "SES-AUD",
            "role": "auditor",
            "runtime": "claude-code",
            "scope_id": _WAVE_ID,
            "status": "active",
            "claimed_wave_ids": [],
            "worktree_ids": [],
            "artifact_ids": [],
            "started_at": "2026-06-01T00:00:00Z",
            "ended_at": None,
            "summary": None,
        }
    }
    state, _state_path, _events = _write_state(
        tmp_path, effort_bucket="L", extra_sessions=auditor_session
    )
    # Does not raise.
    assert_not_executor_self_report(state, wave_id=_WAVE_ID, author_session_id="SES-AUD")


def test_assert_not_executor_self_report_unknown_session_raises(tmp_path: Path) -> None:
    """An unknown author session id is a KeyError (fail-fast)."""
    state, _state_path, _events = _write_state(tmp_path, effort_bucket="L")
    with pytest.raises(KeyError, match="unknown agent session"):
        assert_not_executor_self_report(state, wave_id=_WAVE_ID, author_session_id="SES-NOPE")


def test_produce_rejects_when_executor_body_returned(tmp_path: Path) -> None:
    """A spawn returning an executor-shaped body is rejected, not stored.

    The narrowing validator forces an auditor body; an executor body fails
    the schema every attempt, so the bounded loop exhausts typed and NO
    verdict row is written (the producer never reaches the append).
    """
    state, state_path, events_path = _write_state(tmp_path, effort_bucket="L")
    wave = state.waves[_WAVE_ID]
    executor_body = json.dumps(
        {
            "role": "executor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "did the thing",
            "wave_id": _WAVE_ID,
            "outcome": "shipped",
        }
    )
    with pytest.raises(LLMAssistError):
        _run(
            produce_wave_verdict(
                state=state,
                state_path=state_path,
                events_path=events_path,
                wave=wave,
                spawn=_RecordingSpawn([executor_body, executor_body, executor_body]),
                repo_root=tmp_path,
                max_attempts=3,
            )
        )
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)
    assert rows == []
    auditor = next(
        session
        for session in state.agent_sessions.values()
        if session.role is AgentSessionRole.AUDITOR
    )
    assert auditor.status is AgentSessionStatus.FAILED
    assert auditor.ended_at is not None
    assert auditor.id not in state.current.active_session_ids


def test_produce_report_store_failure_terminalizes_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report append failure preserves its error and leaves no ACTIVE session."""
    state, state_path, events_path = _write_state(tmp_path, effort_bucket="L")
    wave = state.waves[_WAVE_ID]

    def _fail_append(**_kwargs: object) -> object:
        raise OSError("report store unavailable")

    monkeypatch.setattr(
        "eawf.workflow.dispatch.verdict.append_agent_report",
        _fail_append,
    )
    with pytest.raises(OSError, match="report store unavailable"):
        _run(
            produce_wave_verdict(
                state=state,
                state_path=state_path,
                events_path=events_path,
                wave=wave,
                spawn=_RecordingSpawn([_auditor_body_json()]),
                repo_root=tmp_path,
            )
        )
    auditor = next(
        session
        for session in state.agent_sessions.values()
        if session.role is AgentSessionRole.AUDITOR
    )
    assert auditor.status is AgentSessionStatus.FAILED
    assert auditor.ended_at is not None
    assert auditor.id not in state.current.active_session_ids


def test_produce_close_event_failure_does_not_mask_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Close-event loss is secondary; validated report still returns CLOSED."""
    from eawf.runtime.session import store as session_store

    state, state_path, events_path = _write_state(tmp_path, effort_bucket="L")
    wave = state.waves[_WAVE_ID]
    original = session_store.commit_event
    calls = 0

    def _fail_second_event(path: Path, envelope: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("close event unavailable")
        return original(path, envelope)  # type: ignore[arg-type]

    monkeypatch.setattr(session_store, "commit_event", _fail_second_event)
    result = _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn([_auditor_body_json()]),
            repo_root=tmp_path,
        )
    )
    auditor = state.agent_sessions[result.auditor_session_id]
    assert auditor.status is AgentSessionStatus.CLOSED
    assert auditor.ended_at is not None
    assert auditor.id not in state.current.active_session_ids
    assert "event_status=failed" in caplog.text


# --------------------------------------------------------------------------- #
# The narrowing validator: parse_auditor_report_body.
# --------------------------------------------------------------------------- #


def test_parse_auditor_report_body_accepts_auditor() -> None:
    """The narrowing validator accepts a valid auditor body."""
    body = parse_auditor_report_body(json.loads(_auditor_body_json()))
    assert body.role == "auditor"
    assert body.target_id == _WAVE_ID


def test_parse_auditor_report_body_rejects_non_auditor_role() -> None:
    """A reviewer body fails the forced auditor schema (wrong discriminator)."""
    reviewer_body = {
        "role": "reviewer",
        "verdict": "pass",
        "confidence": "high",
        "summary": "reviewed",
        "target_id": _WAVE_ID,
        "findings": [],
    }
    with pytest.raises(ValidationError):
        parse_auditor_report_body(reviewer_body)


def test_parse_auditor_report_body_rejects_malformed() -> None:
    """A body missing required auditor fields raises ValidationError."""
    with pytest.raises(ValidationError):
        parse_auditor_report_body({"role": "auditor", "verdict": "pass"})


# --------------------------------------------------------------------------- #
# Coexistence + defense-in-depth: the fresh auditor never IS the executor.
# --------------------------------------------------------------------------- #


def test_produce_coexists_with_active_executor_session(tmp_path: Path) -> None:
    """The producer registers a fresh auditor even with an ACTIVE executor.

    This is the realistic close-time scenario: the executor session for the
    wave is still ACTIVE on ``(wave_id, runtime)``. The auditor scopes to the
    verdict-qualified ``"{wave_id}::audit"`` so it coexists with the executor
    lane rather than colliding -- and the verdict author is the fresh AUDITOR,
    never the executor's session.
    """
    executor_session: dict[str, Any] = {
        "SES-EXEC": {
            "id": "SES-EXEC",
            "role": "executor",
            "runtime": "claude-code",
            "scope_id": _WAVE_ID,
            "status": "active",
            "claimed_wave_ids": [],
            "worktree_ids": [],
            "artifact_ids": [],
            "started_at": "2026-06-01T00:00:00Z",
            "ended_at": None,
            "summary": None,
        }
    }
    state, state_path, events_path = _write_state(
        tmp_path, effort_bucket="L", extra_sessions=executor_session
    )
    wave = state.waves[_WAVE_ID]
    result = _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn([_auditor_body_json()]),
            repo_root=tmp_path,
        )
    )
    author = state.agent_sessions[result.auditor_session_id]
    assert author.role is AgentSessionRole.AUDITOR
    assert result.auditor_session_id != "SES-EXEC"
    # The executor lane remains ACTIVE; the completed auditor lane is CLOSED.
    assert state.agent_sessions["SES-EXEC"].status is AgentSessionStatus.ACTIVE
    assert author.scope_id == f"{_WAVE_ID}::audit"
    assert author.status is AgentSessionStatus.CLOSED


def test_append_agent_report_role_mismatch_is_typed() -> None:
    """Sanity: the canonical writer raises typed on a role mismatch.

    Pins the defense-in-depth contract the producer relies on -- an auditor
    body cannot be appended through an executor session.
    """
    from eawf.workflow.agent_report.store import append_agent_report

    executor_session: dict[str, Any] = {
        "SES-EXEC": {
            "id": "SES-EXEC",
            "role": "executor",
            "runtime": "claude-code",
            "scope_id": _WAVE_ID,
            "status": "active",
            "claimed_wave_ids": [],
            "worktree_ids": [],
            "artifact_ids": [],
            "started_at": "2026-06-01T00:00:00Z",
            "ended_at": None,
            "summary": None,
        }
    }
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ea = root / ".ea"
        ea.mkdir()
        state_path = ea / "state.json"
        state = State.model_validate(
            _state_payload(effort_bucket="L", extra_sessions=executor_session)
        )
        state_path.write_text(state.model_dump_json(), encoding="utf-8")
        body = parse_auditor_report_body(json.loads(_auditor_body_json()))
        with pytest.raises(AgentReportRoleMismatchError):
            append_agent_report(
                state=state,
                state_path=state_path,
                session_id="SES-EXEC",
                base_id=_WAVE_ID,
                body=body,
            )


def test_auditor_prompt_forbids_mutating_the_working_tree() -> None:
    """The auditor audits the operator's LIVE tree, so it must never mutate it.

    `pre-commit run --all-files` stashes uncommitted changes and rolls them back
    when a hook auto-fix conflicts with the stash -- during P30-I25 a close-time
    auditor did exactly that and discarded a set of uncommitted test edits. A
    verdict never needs to re-run a gate: the executor's run is recorded.
    """
    wave = _make_wave(agent_role="executor", effort_bucket="L")

    prompt = build_auditor_prompt(wave, diff_base="abc123~1")

    assert "Working-tree rule" in prompt
    assert "pre-commit" in prompt
    assert "never mutate" in prompt
    # The auditor is pointed at recorded evidence instead of re-running gates.
    assert "RECORDED evidence" in prompt


# --- P30-I25-W33: a numeric confidence must not kill the close --------------


def test_parse_auditor_body_coerces_numeric_confidence() -> None:
    """A float confidence is read as its enum bucket rather than failing the schema.

    A model asked for a "confidence" reaches for a probability far more readily
    than for one of three words. Because the re-ask loop is bounded, three floats
    in a row exhausted it and the close failed with NO verdict written -- which
    the gate then reads as a refusal, so the wave could never close (the live
    P30-I25-W29 failure: `confidence=0.78`).
    """
    raw = json.loads(_auditor_body_json(verdict="pass"))
    raw["confidence"] = 0.78

    body = parse_auditor_report_body(raw)

    assert body.confidence is Confidence.HIGH
    assert body.verdict is AgentReportVerdict.PASS


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, Confidence.HIGH),
        (0.75, Confidence.HIGH),
        (0.6, Confidence.MEDIUM),
        (0.4, Confidence.MEDIUM),
        (0.2, Confidence.LOW),
        (0, Confidence.LOW),
    ],
)
def test_numeric_confidence_buckets(value: float, expected: Confidence) -> None:
    raw = json.loads(_auditor_body_json(verdict="pass"))
    raw["confidence"] = value

    assert parse_auditor_report_body(raw).confidence is expected


def test_unparseable_confidence_still_raises() -> None:
    """Coercion is not a licence to accept anything: a bad body must still re-ask."""
    raw = json.loads(_auditor_body_json(verdict="pass"))
    raw["confidence"] = "very sure"

    with pytest.raises(ValidationError):
        parse_auditor_report_body(raw)

    # Out of the [0, 1] probability range: not a confidence, so not coerced.
    out_of_range = json.loads(_auditor_body_json(verdict="pass"))
    out_of_range["confidence"] = 42.0
    with pytest.raises(ValidationError):
        parse_auditor_report_body(out_of_range)


def test_auditor_prompt_names_the_allowed_confidence_values() -> None:
    wave = _make_wave(agent_role="executor", effort_bucket="L")

    prompt = build_auditor_prompt(wave, diff_base="abc123~1")

    assert "`high`, `medium`, or `low`" in prompt
    assert "never a number" in prompt
