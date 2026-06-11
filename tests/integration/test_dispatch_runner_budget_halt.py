"""Integration: the dispatch runner invokes the token-cap interlock.

Exercises the wiring added in P29-I03-W05:
:func:`eawf.runtime.daemon.dispatch_runner.accrue_tokens_consumed` runs the
safety-floor interlock
(:func:`eawf.runtime.daemon.budget_interlock.enforce_token_cap`) right after
it folds a dispatch's token tally into ``Wave.tokens_consumed``, against a
real ``state.json`` on a tmp filesystem.

The load-bearing assertion is the *wiring*: the interlock is invoked with
the post-increment cumulative consumption, the wave's configured budget, and
the call-site enforce mode / multiplier defaults. The HALT-vs-WARN branch of
the interlock itself is unit-tested separately (``test_budget_interlock``);
here the call site defaults enforce to ``soft`` (the documented config
default), so this test pins that the interlock *sees* the right numbers when
a small budget is crossed. ``enforce_token_cap`` is monkeypatched in the
dispatch_runner namespace to capture the kwargs without driving a real reap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.runtime.daemon import dispatch_runner
from eawf.runtime.daemon.budget_interlock import InterlockOutcome
from eawf.runtime.daemon.dispatch_runner import DispatchTokens, accrue_tokens_consumed
from eawf.runtime.daemon.methods import MethodContext
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_ID = "P29-I03-W05"
_SESSION_ID = "SES-executor"


def _state_payload(*, token_budget: int | None) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-02T00:00:00Z",
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
            "iter_id": "P29-I03",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [_SESSION_ID],
        },
        "workspace": None,
        "phases": {
            "P29": {
                "id": "P29",
                "scope_id": "EAWF",
                "title": "Safety floor",
                "status": "active",
                "iter_ids": ["P29-I03"],
                "outcome_ids": [],
                "opened_at": "2026-06-02T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P29-I03": {
                "id": "P29-I03",
                "phase_id": "P29",
                "title": "Floor",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-02T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P29-I03",
                "title": "Wire budget HALT to the kill ladder",
                "status": "in_progress",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/daemon/budget_interlock.py"],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "M",
                "claim_session_id": _SESSION_ID,
                "worktree_id": None,
                "token_budget": token_budget,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-02T00:00:00Z",
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {
            _SESSION_ID: {
                "id": _SESSION_ID,
                "role": "executor",
                "runtime": "claude",
                "scope_id": _WAVE_ID,
                "status": "active",
                "claimed_wave_ids": [_WAVE_ID],
                "worktree_ids": [],
                "artifact_ids": [],
                "started_at": "2026-06-02T00:00:00Z",
                "ended_at": None,
                "summary": None,
            }
        },
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, *, token_budget: int | None) -> Path:
    state = State.model_validate(_state_payload(token_budget=token_budget))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path) -> MethodContext:
    event_path = state_path.parent / "store" / "event.jsonl"
    return MethodContext(
        started_at="2026-06-02T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.5.0",
        bus=None,
        event_path=event_path,
        state_path=state_path,
    )


def _tokens(total: int) -> DispatchTokens:
    """A tally whose ``.total`` equals *total* (all of it billed input)."""
    return DispatchTokens(
        input_tokens=total,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def test_accrue_invokes_interlock_with_post_increment_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accrual drives the interlock with the post-increment burn + budget."""
    state_path = _write_state(tmp_path, token_budget=1000)
    ctx = _ctx(state_path)

    captured: dict[str, Any] = {}

    def _fake_enforce(
        *,
        consumed: int,
        base_budget: int | None,
        enforce: str,
        multiplier: float,
        pgid: int | None,
    ) -> InterlockOutcome:
        captured.update(
            consumed=consumed,
            base_budget=base_budget,
            enforce=enforce,
            multiplier=multiplier,
            pgid=pgid,
        )
        # The wiring test does not exercise the HALT branch (the unit test
        # owns it); return a benign CONTINUE-shaped outcome stand-in.
        from eawf.runtime.budget.policy import classify_enforcement

        decision = classify_enforcement(
            consumed, base_budget, enforce=enforce, multiplier=multiplier
        )
        return InterlockOutcome(decision=decision, terminated=False, termination=None)

    monkeypatch.setattr(dispatch_runner, "enforce_token_cap", _fake_enforce)

    # A 2000-token delta crosses the 1.5x cap of a 1000 budget (cap == 1500).
    outcome = accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=_tokens(2000))

    # The accrual now returns the interlock outcome (truthy), not a bool.
    assert outcome is not None
    assert outcome.terminated is False
    # The interlock saw the POST-increment cumulative consumption (0 + 2000).
    assert captured["consumed"] == 2000
    assert captured["base_budget"] == 1000
    # Call-site defaults: soft enforce, 1.5 multiplier, dark-dispatch pgid.
    assert captured["enforce"] == "soft"
    assert captured["multiplier"] == pytest.approx(1.5)
    assert captured["pgid"] is None
    # And the burn actually persisted to state.
    assert load_state(state_path).waves[_WAVE_ID].tokens_consumed == 2000


def test_accrue_invokes_interlock_even_without_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A budget-less wave still runs the interlock (it no-ops on a None cap)."""
    state_path = _write_state(tmp_path, token_budget=None)
    ctx = _ctx(state_path)

    captured: dict[str, Any] = {}

    def _fake_enforce(
        *,
        consumed: int,
        base_budget: int | None,
        enforce: str,
        multiplier: float,
        pgid: int | None,
    ) -> InterlockOutcome:
        captured.update(consumed=consumed, base_budget=base_budget)
        from eawf.runtime.budget.policy import classify_enforcement

        decision = classify_enforcement(
            consumed, base_budget, enforce=enforce, multiplier=multiplier
        )
        return InterlockOutcome(decision=decision, terminated=False, termination=None)

    monkeypatch.setattr(dispatch_runner, "enforce_token_cap", _fake_enforce)

    accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=_tokens(5000))

    assert captured["consumed"] == 5000
    assert captured["base_budget"] is None


def test_accrue_stateless_context_skips_interlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stateless context returns None and never reaches the interlock."""
    called = False

    def _fake_enforce(**_kwargs: Any) -> InterlockOutcome:
        nonlocal called
        called = True
        raise AssertionError("interlock must not run on a stateless context")

    monkeypatch.setattr(dispatch_runner, "enforce_token_cap", _fake_enforce)

    ctx = MethodContext(
        started_at="2026-06-02T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.5.0",
        bus=None,
        event_path=tmp_path / "store" / "event.jsonl",
        state_path=None,
    )

    assert accrue_tokens_consumed(ctx, wave_id=_WAVE_ID, tokens=_tokens(2000)) is None
    assert called is False
