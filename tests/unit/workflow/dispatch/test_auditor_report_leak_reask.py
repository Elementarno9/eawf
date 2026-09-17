"""Tests for re-asking the auditor when its report body fails the leak scrub.

The report store refuses a body that quotes a home path or a private IP. The
producer runs that same check inside the bounded re-ask loop, so a leaking
auditor body is re-asked with the finding kinds named rather than rejected
after the loop accepted it, and the auditor prompt asks for generic
descriptions up front.

The spawn is always a recording stub (no subprocess, no network). Leak-shaped
values are assembled from parts at runtime so the source carries no literal
home path or IP address.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus
from eawf.kernel.state.models import State, Wave
from eawf.platform.scrub.scan import scan_text
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.agent_report.rollup import iter_agent_reports
from eawf.workflow.agent_report.store import (
    AgentReportScrubError,
    append_agent_report,
    scrub_finding_kinds,
)
from eawf.workflow.dispatch.llm_assist import LLMAssistError
from eawf.workflow.dispatch.verdict import (
    SENSITIVE_VALUE_RULE,
    DurableAuditContext,
    DurableAuditCriterion,
    _reject_scrub_findings,
    build_auditor_prompt,
    parse_auditor_report_body,
    produce_wave_verdict,
)
from tests._criteria_helpers import legacy_criteria
from tests._session_helpers import seed_active_session

_WAVE_ID = "P40-I02-W05"
_T0 = datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 7, 1, 9, 0, 4, tzinfo=UTC)
_RECEIPT_URN = f"urn:eawf:v1:store:{_WAVE_ID}/gate_receipt/GR-leak"
_CRITERIA = ("scrub the report writer", "prove the scrub")
_RULE_SENTENCE = (
    "Describe home-directory paths, IP addresses, email addresses and tokens "
    "generically instead of quoting them"
)


def _home_path() -> str:
    """Return a tilde home path built from parts (no literal in source)."""
    return "".join(("~", "/", "checkout", "/", "fixture.txt"))


def _private_ip() -> str:
    """Return a private IPv4 address built from parts (no literal in source)."""
    return ".".join(("10", "0", "0", "7"))


def _flat(text: str) -> str:
    """Collapse the prompt's hard wraps so a sentence matches as one line."""
    return " ".join(text.split())


def _body(*, summary: str = "re-read the diff against the criteria") -> dict[str, Any]:
    """Return a schema-valid passing auditor body carrying *summary*."""
    return {
        "role": "auditor",
        "verdict": "pass",
        "confidence": "high",
        "summary": summary,
        "target_id": _WAVE_ID,
        "criteria": [{"criterion": text, "passed": True} for text in _CRITERIA],
        "refutations": [],
    }


def _durable_context() -> DurableAuditContext:
    """Exact close context with one deterministic and one judged criterion."""
    return DurableAuditContext(
        wave_id=_WAVE_ID,
        close_attempt_id="CA-07",
        integration_id="WI-07",
        integrated_sha="a" * 40,
        tree_sha="b" * 40,
        spec_digest="c" * 64,
        criteria_digest="d" * 64,
        gate_manifest_digest="e" * 64,
        policy_digest="f" * 64,
        runner_digest="1" * 64,
        dependency_binding_digest="2" * 64,
        criteria=(
            DurableAuditCriterion(
                criterion_id="CR-01",
                text=_CRITERIA[0],
                deterministic=True,
                gate_receipt_urns=(_RECEIPT_URN,),
            ),
            DurableAuditCriterion(
                criterion_id="CR-02",
                text=_CRITERIA[1],
                deterministic=False,
            ),
        ),
    )


def _durable_body(*, note: str | None = None) -> dict[str, Any]:
    """Return a context-valid durable body; *note* lands on the judged row."""
    body = _body()
    body["criteria"][0]["evidence_refs"] = [{"kind": "store_record", "ref": _RECEIPT_URN}]
    judged_ref: dict[str, Any] = {
        "kind": "artifact",
        "ref": "tests/unit/workflow/dispatch/test_auditor_report_leak_reask.py",
    }
    if note is not None:
        judged_ref["note"] = note
    body["criteria"][1]["evidence_refs"] = [judged_ref]
    return body


class _RecordingSpawn:
    """Replay canned answers and record each prompt (never a real process)."""

    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self._answers = [json.dumps(answer) for answer in answers]
        self.prompts: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.prompts)

    async def __call__(self, prompt: str) -> SpawnResult:
        if self.calls >= len(self._answers):
            raise AssertionError(f"spawn called more than {len(self._answers)} time(s)")
        text = self._answers[self.calls]
        self.prompts.append(prompt)
        return SpawnResult(
            session_id="sess-auditor-leak",
            runtime="claude-code",
            model="opus",
            subprocess_pid=4242,
            exit_status=0,
            text=text,
            started_at=_T0,
            ended_at=_T1,
        )


def _state_payload() -> dict[str, Any]:
    """A minimal valid State with the phase -> iter -> wave chain."""
    criteria = [c.model_dump(mode="json") for c in legacy_criteria(*_CRITERIA)]
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-07-01T00:00:00Z",
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
            "phase_id": "P40",
            "iter_id": "P40-I02",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P40": {
                "id": "P40",
                "scope_id": "EAWF",
                "title": "leak scrub",
                "status": "active",
                "iter_ids": ["P40-I02"],
                "outcome_ids": [],
                "opened_at": "2026-07-01T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P40-I02": {
                "id": "P40-I02",
                "phase_id": "P40",
                "title": "Report scrub",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-07-01T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P40-I02",
                "title": "scrub the report writer",
                "status": "claimed",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/workflow/agent_report/store.py"],
                "success_criteria": criteria,
                "agent_role": "executor",
                "effort_bucket": "S",
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-07-01T00:00:00Z",
                "claimed_at": "2026-07-01T00:00:00Z",
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
    """Serialise a valid State under *tmp_path* and return it with its paths."""
    state = State.model_validate(_state_payload())
    ea = tmp_path / ".ea"
    ea.mkdir()
    state_path = ea / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state, state_path, ea / "store" / "event.jsonl"


def _wave() -> Wave:
    return Wave.model_validate(_state_payload()["waves"][_WAVE_ID])


def _produce(
    tmp_path: Path,
    spawn: _RecordingSpawn,
    *,
    max_attempts: int = 3,
    durable_context: DurableAuditContext | None = None,
) -> tuple[State, Path, Any]:
    """Run the producer over a fresh state; return the state, path and result."""
    state, state_path, events_path = _write_state(tmp_path)
    result = asyncio.run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=state.waves[_WAVE_ID],
            spawn=spawn,
            repo_root=tmp_path,
            max_attempts=max_attempts,
            durable_context=durable_context,
        )
    )
    return state, state_path, result


def _auditor_status(state: State) -> AgentSessionStatus:
    sessions = [s for s in state.agent_sessions.values() if s.role is AgentSessionRole.AUDITOR]
    assert len(sessions) == 1
    return sessions[0].status


def _notice_head(prompt: str) -> str:
    """Return the re-ask notice without the echoed previous response."""
    head, separator, _echo = prompt.partition("Previous response:")
    assert separator, "re-ask prompt lost its previous-response section"
    return head


@pytest.fixture(autouse=True)
def _fixed_diff_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the diff base so the producer never shells out to git."""
    monkeypatch.setattr(
        "eawf.workflow.dispatch.verdict.derive_diff_base",
        lambda _wave_id, *, repo_root=None: "abc123~1",
    )


# --------------------------------------------------------------------------- #
# The generic-description rule in the auditor prompts.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("durable", [False, True], ids=["plain", "durable"])
def test_build_auditor_prompt_renders_sensitive_value_rule(durable: bool) -> None:
    """Both the plain and the durable-close prompt carry the rule once."""
    context = _durable_context() if durable else None

    prompt = build_auditor_prompt(_wave(), diff_base="abc123~1", durable_context=context)

    assert prompt.count("## Sensitive-value rule") == 1
    assert _RULE_SENTENCE in _flat(prompt)
    assert ("## Durable evidence contract" in prompt) is durable
    assert prompt.index("## Sensitive-value rule") < prompt.index("## Output contract")


def test_build_auditor_prompt_rule_survives_minimal_prompt() -> None:
    """The rule renders even with no diff section and no criteria."""
    wave = _wave().model_copy(update={"success_criteria": []})

    prompt = build_auditor_prompt(wave, diff_base="abc123~1", include_diff=False)

    assert "## Diff under audit" not in prompt
    assert _RULE_SENTENCE in _flat(prompt)


def test_sensitive_value_rule_prompt_text_is_scrub_clean() -> None:
    """An auditor that echoes the rule must not trip the scrub it describes."""
    assert scan_text(SENSITIVE_VALUE_RULE) == []


def test_produce_wave_verdict_first_prompt_carries_sensitive_value_rule(tmp_path: Path) -> None:
    """The prompt the live producer spawns with carries the rule."""
    spawn = _RecordingSpawn([_durable_body()])

    _produce(tmp_path, spawn, durable_context=_durable_context())

    assert _RULE_SENTENCE in _flat(spawn.prompts[0])


# --------------------------------------------------------------------------- #
# The scrub check inside the bounded re-ask loop.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("leak_parts", "kinds"),
    [
        pytest.param(("home",), ("home_path",), id="home-path"),
        pytest.param(("ip",), ("private_ip",), id="private-ip"),
        pytest.param(("home", "ip"), ("home_path", "private_ip"), id="both"),
    ],
)
def test_produce_wave_verdict_reasks_leaking_body_then_appends_clean_body(
    tmp_path: Path,
    leak_parts: tuple[str, ...],
    kinds: tuple[str, ...],
) -> None:
    """A leaking first body is re-asked by kind; the clean second one lands."""
    values = {"home": _home_path(), "ip": _private_ip()}
    leaked = [values[part] for part in leak_parts]
    clean = _body(summary="all criteria hold on re-read")
    spawn = _RecordingSpawn([_body(summary=f"fixture quotes {' and '.join(leaked)}"), clean])

    state, state_path, result = _produce(tmp_path, spawn)

    assert spawn.calls == 2
    notice = _notice_head(spawn.prompts[1])
    failure = result.assist_result.prior_failures[0]
    assert result.assist_result.attempts_used == 2
    assert failure.reason == "schema_mismatch"
    for kind in kinds:
        assert kind in notice
        assert kind in failure.detail
    for value in leaked:
        assert value not in notice
        assert value not in failure.detail
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)
    assert len(rows) == 1
    assert rows[0].payload.body.summary == clean["summary"]
    assert _auditor_status(state) is AgentSessionStatus.CLOSED


def test_produce_wave_verdict_reasks_leaking_durable_body(tmp_path: Path) -> None:
    """A durable close body leaking in an evidence note is re-asked, then lands."""
    spawn = _RecordingSpawn(
        [
            _durable_body(note=f"fixture reads {_home_path()} from {_private_ip()}"),
            _durable_body(note="fixture reads a home-directory path from a private IP"),
        ]
    )

    state, state_path, result = _produce(tmp_path, spawn, durable_context=_durable_context())

    assert spawn.calls == 2
    assert "home_path, private_ip" in _notice_head(spawn.prompts[1])
    assert result.verdict.value == "pass"
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)
    assert [row.payload.header.report_id for row in rows] == [result.append_result.envelope.id]
    assert _auditor_status(state) is AgentSessionStatus.CLOSED


def test_produce_wave_verdict_reask_not_needed_for_clean_body(tmp_path: Path) -> None:
    """A clean first body is accepted without a re-ask."""
    spawn = _RecordingSpawn([_body()])

    _state, _path, result = _produce(tmp_path, spawn)

    assert spawn.calls == 1
    assert result.assist_result.attempts_used == 1
    assert result.assist_result.prior_failures == []


@pytest.mark.parametrize("max_attempts", [1, 3])
def test_produce_wave_verdict_reask_exhausted_raises_and_appends_nothing(
    tmp_path: Path,
    max_attempts: int,
) -> None:
    """A body still leaking at max_attempts raises typed and stores no report."""
    leaking = _body(summary=f"fixture quotes {_home_path()} and {_private_ip()}")
    spawn = _RecordingSpawn([leaking] * max_attempts)
    state, state_path, events_path = _write_state(tmp_path)

    with pytest.raises(LLMAssistError) as excinfo:
        asyncio.run(
            produce_wave_verdict(
                state=state,
                state_path=state_path,
                events_path=events_path,
                wave=state.waves[_WAVE_ID],
                spawn=spawn,
                repo_root=tmp_path,
                max_attempts=max_attempts,
            )
        )

    error = excinfo.value
    assert spawn.calls == max_attempts
    assert error.attempts == max_attempts
    assert [failure.reason for failure in error.failures] == ["schema_mismatch"] * max_attempts
    assert "home_path, private_ip" in str(error)
    assert _home_path() not in str(error)
    assert _private_ip() not in str(error)
    assert iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID) == []
    assert _auditor_status(state) is AgentSessionStatus.FAILED


def test_reject_scrub_findings_reask_error_hides_the_leaked_value() -> None:
    """The raised error carries one hidden-input error naming only the kinds."""
    body = parse_auditor_report_body(_body(summary=f"quoted {_private_ip()}"))

    with pytest.raises(ValidationError) as excinfo:
        _reject_scrub_findings(body)

    errors = excinfo.value.errors()
    assert len(errors) == 1
    assert errors[0]["type"] == "agent_report_scrub"
    assert "private_ip" in str(excinfo.value)
    assert _private_ip() not in str(excinfo.value)
    assert _private_ip() not in repr(errors)


def test_reject_scrub_findings_reask_passes_clean_body_through() -> None:
    body = parse_auditor_report_body(_body())

    assert _reject_scrub_findings(body) is body


# --------------------------------------------------------------------------- #
# The store-side check shared by the validator and the final append guard.
# --------------------------------------------------------------------------- #


def test_scrub_finding_kinds_clean_body_is_empty() -> None:
    assert scrub_finding_kinds(parse_auditor_report_body(_body())) == ()


def test_scrub_finding_kinds_dedupes_and_sorts_nested_findings() -> None:
    """Repeated and nested findings collapse to sorted distinct kinds."""
    raw = _body(summary=f"{_private_ip()} then {_private_ip()}")
    raw["refutations"] = [f"see {_home_path()}"]
    raw["verdict"] = "fail"

    kinds = scrub_finding_kinds(parse_auditor_report_body(raw))

    assert kinds == ("home_path", "private_ip")


def test_append_agent_report_reask_bypass_still_refused(tmp_path: Path) -> None:
    """A leaking body that skips the validator is still refused at append."""
    state, state_path, _events_path = _write_state(tmp_path)
    session = seed_active_session(
        state,
        session_id="SES-AUD-1",
        scope_id=f"{_WAVE_ID}::audit",
        role=AgentSessionRole.AUDITOR,
    )
    body = parse_auditor_report_body(_body(summary=f"{_home_path()} {_private_ip()}"))

    with pytest.raises(AgentReportScrubError, match=r"failed scrub: home_path, private_ip$"):
        append_agent_report(
            state=state,
            state_path=state_path,
            session_id=session.id,
            base_id=_WAVE_ID,
            body=body,
        )
    assert iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID) == []
