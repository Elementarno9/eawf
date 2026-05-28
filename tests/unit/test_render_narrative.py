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
            "subproject_id": None,
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
                "subproject_id": None,
                "title": "Render narrative",
                "description": "Explain release surfaces.",
                "status": "closed",
                "iter_ids": [],
                "outcome_ids": [],
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": "2026-05-08T00:01:00Z",
                "audit_id": "A28",
                "intent": {
                    "goal": "share release prose",
                    "motivation": "Keep PR and release output aligned.",
                    "success_signal": "Both surfaces render the same validation facts.",
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
    assert bundle.why[0] == "Keep PR and release output aligned."
    assert bundle.validation == [
        "Audit `A28` verdict: pass.",
        "1/1 wave(s) closed.",
        "1 wave commit(s) pinned in state.",
    ]
    assert bundle.risks == ["No open risks recorded."]

    rendered = render_narrative_bundle(bundle)
    for heading in ("## What", "## Why", "## Validation", "## Risks"):
        assert heading in rendered


def test_build_narrative_unknown_phase_raises() -> None:
    state = State.model_validate(_state_payload())
    with pytest.raises(NarrativeNotFoundError, match="phase not found"):
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


def test_build_narrative_prefers_w24_audited_intent_fields() -> None:
    """W60: ``priority_rationale`` + ``problem`` + ``desired_outcome`` lead the why list.

    The legacy ``motivation`` / ``goal`` / ``success_signal`` triad
    still flows in as fallback content so pre-W59 briefs are unchanged,
    but the audited fields rank first when both are present.
    """
    payload = _state_payload()
    payload["phases"]["P28"]["intent"] = {
        "goal": "share release prose",
        "motivation": "Keep PR and release output aligned.",
        "success_signal": "Both surfaces render the same validation facts.",
        "problem": "Release narratives drift from PR text.",
        "desired_outcome": "PR and release ship identical validation prose.",
        "priority_rationale": "W24 audit ranked unified narrative above polish.",
    }
    state = State.model_validate(payload)
    bundle = build_narrative(state, "P28")

    # The first 3 entries come from the audited triad in priority,
    # problem, desired_outcome order; legacy entries follow as
    # fallbacks within the 6-line bound.
    assert bundle.why[0] == "W24 audit ranked unified narrative above polish."
    assert bundle.why[1] == "Release narratives drift from PR text."
    assert bundle.why[2] == "PR and release ship identical validation prose."
    # Legacy entries are still present as fallback within the cap.
    assert "Keep PR and release output aligned." in bundle.why


def test_build_narrative_legacy_only_intent_unchanged() -> None:
    """Pre-W59 briefs (audited fields unset) keep their original why ordering."""
    payload = _state_payload()
    # Default payload only has legacy fields.
    state = State.model_validate(payload)
    bundle = build_narrative(state, "P28")
    # First non-fallback entry is the legacy motivation, matching the
    # pre-W60 behaviour pinned by ``test_build_narrative_emits_required_sections``.
    assert bundle.why[0] == "Keep PR and release output aligned."
