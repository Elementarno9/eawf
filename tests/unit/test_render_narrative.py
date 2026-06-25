"""Unit tests for :mod:`eawf.surfaces.render.narrative`."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.models import State
from eawf.surfaces.render.narrative import (
    NarrativeBundle,
    NarrativeNotFoundError,
    build_narrative,
    generated_changelog_lines,
    render_narrative_bundle,
)


def _state_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ZZ",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "ZZ",
            "slug": "zz",
            "title": "ZZ",
            "description": "",
            "domains": [],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ZZ",
        },
        "current": {
            "project_code": "ZZ",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P28": {
                "id": "P28",
                "scope_id": "ZZ",
                "track_id": None,
                "title": "Render narrative",
                "description": "Explain release surfaces.",
                "status": "closed",
                "iter_ids": [],
                "outcome_ids": [],
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": "2026-05-08T00:01:00Z",
                "audit_id": "A28",
                "intent": {
                    "problem": "Release narratives drift from PR text.",
                    "desired_outcome": "PR and release ship identical validation prose.",
                    "priority_rationale": "Keep PR and release output aligned.",
                },
            }
        },
        "iters": {
            "P28-I03": {
                "id": "P28-I03",
                "phase_id": "P28",
                "title": "ship renderers",
                "status": "closed",
                "wave_ids": [],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": "2026-05-08T00:01:00Z",
            }
        },
        "waves": {
            "P28-I03-W01": {
                "id": "P28-I03-W01",
                "iter_id": "P28-I03",
                "title": "NarrativeBundle producer",
                "status": "closed",
                "deps": [],
                "blocks": [],
                "file_scopes": [],
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": "PR and release renderers consume NarrativeBundle",
                "commit": "a" * 40,
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": "2026-05-08T00:01:00Z",
            }
        },
        "audits": {
            "A28": {
                "id": "A28",
                "scope_id": "P28",
                "kind": "ship-gate",
                "status": "complete",
                "report_artifact_id": None,
                "check_results": [],
                "integrity_results": [],
                "created_at": "2026-05-08T00:01:00Z",
                "verdict": "pass",
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def test_build_narrative_emits_required_sections() -> None:
    state = State.model_validate(_state_payload())
    bundle = build_narrative(state, "P28")

    assert bundle.what[0] == "`P28` Render narrative (closed)."
    # The W24-audited triad leads the why list: priority_rationale,
    # then problem, then desired_outcome.
    assert bundle.why[0] == "Keep PR and release output aligned."
    assert bundle.why[1] == "Release narratives drift from PR text."
    assert bundle.why[2] == "PR and release ship identical validation prose."
    assert bundle.validation == [
        "Audit `A28` verdict: pass.",
        "1/1 wave(s) closed.",
        "1 wave commit(s) pinned in state.",
    ]
    assert bundle.risks == ["No open risks recorded."]

    rendered = render_narrative_bundle(bundle)
    for heading in ("## What", "## Why", "## Validation", "## Risks"):
        assert heading in rendered


def test_build_narrative_unknown_scope_raises() -> None:
    state = State.model_validate(_state_payload())
    with pytest.raises(NarrativeNotFoundError, match="scope not found"):
        build_narrative(state, "P99")


def test_narrative_bundle_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        NarrativeBundle.model_validate(
            {
                "scope_id": "P28",
                "title": "P28",
                "what": ["one"],
                "why": ["two"],
                "validation": ["three"],
                "risks": ["four"],
                "extra": True,
            }
        )


def test_generated_changelog_lines_prefixes_bullets() -> None:
    state = State.model_validate(_state_payload())
    bundle = build_narrative(state, "P28")

    assert generated_changelog_lines(bundle)[0] == "- Render narrative."


def test_build_narrative_w24_audited_intent_drives_why_list() -> None:
    """The W24-audited triad drives the why list in priority order.

    Post-W61 the brief only carries the audited fields; the why list
    leads with ``priority_rationale``, then ``problem``, then
    ``desired_outcome``.
    """
    payload = _state_payload()
    payload["phases"]["P28"]["intent"] = {
        "problem": "Release narratives drift from PR text.",
        "desired_outcome": "PR and release ship identical validation prose.",
        "priority_rationale": "W24 audit ranked unified narrative above polish.",
    }
    state = State.model_validate(payload)
    bundle = build_narrative(state, "P28")

    assert bundle.why[0] == "W24 audit ranked unified narrative above polish."
    assert bundle.why[1] == "Release narratives drift from PR text."
    assert bundle.why[2] == "PR and release ship identical validation prose."


def test_build_narrative_intent_without_priority_rationale() -> None:
    """When priority_rationale is unset the why list leads with the problem."""
    payload = _state_payload()
    payload["phases"]["P28"]["intent"] = {
        "problem": "Release narratives drift from PR text.",
        "desired_outcome": "PR and release ship identical validation prose.",
    }
    state = State.model_validate(payload)
    bundle = build_narrative(state, "P28")
    assert bundle.why[0] == "Release narratives drift from PR text."
    assert bundle.why[1] == "PR and release ship identical validation prose."


# ---------------------------------------------------------------------------
# Per-scope-kind dispatch (W55)
# ---------------------------------------------------------------------------


def _two_wave_payload() -> dict[str, Any]:
    """Return a state payload with two distinct waves under one iter."""
    payload = _state_payload()
    payload["waves"]["P28-I03-W02"] = {
        "id": "P28-I03-W02",
        "iter_id": "P28-I03",
        "title": "Wave card divergence",
        "status": "in_progress",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "claim_session_id": None,
        "worktree_id": None,
        "token_budget": None,
        "tokens_consumed": 0,
        "outcome": None,
        "commit": None,
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": None,
        "intent": {
            "problem": "Two waves share the phase rollup body.",
            "desired_outcome": "Each wave's d-tab quotes its own intent.",
            "planned_steps": ["Dispatch by scope kind"],
            "risks": ["Sibling waves still collide on rollup"],
            "priority_rationale": "Operator audit ranked divergence above polish.",
        },
    }
    # Decorate W01 with its own intent so the two waves diverge.
    payload["waves"]["P28-I03-W01"]["intent"] = {
        "problem": "Wave detail body re-runs the phase rollup.",
        "desired_outcome": "Wave detail body quotes the wave's own intent.",
        "planned_steps": ["Add wave-specific narrative builder"],
        "risks": ["NarrativeBundle shape change breaks PR/release tests"],
        "priority_rationale": "Pre-ship blocker for the v0.4 PR text.",
    }
    return payload


def test_build_narrative_wave_id_dispatches_to_wave_builder() -> None:
    """A wave id resolves to the wave-shaped bundle, not the phase rollup."""
    payload = _two_wave_payload()
    state = State.model_validate(payload)

    bundle = build_narrative(state, "P28-I03-W01")
    assert bundle.scope_id == "P28-I03-W01"
    assert bundle.title.startswith("P28-I03-W01:")
    # The wave bundle quotes the wave's IntentBrief problem/outcome.
    assert "Wave detail body re-runs the phase rollup." in bundle.what
    assert "Wave detail body quotes the wave's own intent." in bundle.what


def test_build_narrative_two_waves_under_one_phase_diverge() -> None:
    """Two sibling waves under one phase yield demonstrably different bodies."""
    payload = _two_wave_payload()
    state = State.model_validate(payload)

    bundle_one = build_narrative(state, "P28-I03-W01")
    bundle_two = build_narrative(state, "P28-I03-W02")

    assert bundle_one.scope_id != bundle_two.scope_id
    assert bundle_one.what != bundle_two.what
    assert bundle_one.why != bundle_two.why


def test_build_narrative_wave_validation_quotes_pinned_commit() -> None:
    """The wave bundle's validation list quotes the pinned ``commit`` SHA."""
    payload = _two_wave_payload()
    state = State.model_validate(payload)
    bundle = build_narrative(state, "P28-I03-W01")
    sha_prefix = ("a" * 40)[:12]
    assert any(sha_prefix in line for line in bundle.validation)


def test_build_narrative_wave_validation_marks_no_actuals() -> None:
    """When no ActualSummary is scoped to the wave the empty-state token surfaces."""
    payload = _two_wave_payload()
    state = State.model_validate(payload)
    bundle = build_narrative(state, "P28-I03-W01")
    assert "No rollup yet." in bundle.validation


def test_build_narrative_iter_id_dispatches_to_iter_builder() -> None:
    """An iter id resolves to an iter-shaped bundle (not the phase one)."""
    payload = _two_wave_payload()
    state = State.model_validate(payload)
    bundle = build_narrative(state, "P28-I03")
    assert bundle.scope_id == "P28-I03"
    assert bundle.title.startswith("P28-I03:")
    # The iter bundle lists each child wave by id.
    assert any("`P28-I03-W01`" in line for line in bundle.what)
    assert any("`P28-I03-W02`" in line for line in bundle.what)


def test_build_narrative_iter_and_phase_bundles_diverge() -> None:
    """The iter bundle differs from the parent phase rollup."""
    payload = _two_wave_payload()
    state = State.model_validate(payload)
    iter_bundle = build_narrative(state, "P28-I03")
    phase_bundle = build_narrative(state, "P28")
    assert iter_bundle.scope_id != phase_bundle.scope_id
    assert iter_bundle.title != phase_bundle.title


def test_build_narrative_backlog_id_dispatches_to_backlog_builder() -> None:
    """A backlog id resolves to a backlog-shaped bundle."""
    payload = _two_wave_payload()
    payload["backlog"] = {
        "B001": {
            "id": "B001",
            "scope_id": "P28",
            "title": "Drop legacy phase substitution",
            "description": "Audit-tracked follow-up to the rollup divergence.",
            "priority": "P2",
            "status": "open",
            "created_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
            "resolution": None,
            "commit": None,
        }
    }
    state = State.model_validate(payload)
    bundle = build_narrative(state, "B001")
    assert bundle.scope_id == "B001"
    assert bundle.title.startswith("B001:")
    # The backlog bundle leads with priority.
    assert any("Priority: P2." in line for line in bundle.why)
    # And the backlog bundle is distinct from the phase / iter bundles.
    iter_bundle = build_narrative(state, "P28-I03")
    phase_bundle = build_narrative(state, "P28")
    assert bundle.what != iter_bundle.what
    assert bundle.what != phase_bundle.what
