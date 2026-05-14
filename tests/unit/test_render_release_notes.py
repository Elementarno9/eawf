from __future__ import annotations

from typing import Any

from eawf.artifacts.validation import validate_markdown_artifact
from eawf.render.release_notes import build_release_notes, mine_unreleased_changelog
from eawf.state.models import State


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
            "P17": {
                "id": "P17",
                "scope_id": "ZZ",
                "subproject_id": None,
                "title": "PR hardening",
                "status": "closed",
                "iter_ids": [],
                "outcome_ids": [],
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": "2026-05-08T00:01:00Z",
                "audit_id": None,
            }
        },
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def test_mine_unreleased_changelog_extracts_section() -> None:
    text = "# Changelog\n\n## [Unreleased]\n\n- One\n\n## [0.1.0]\n"
    assert mine_unreleased_changelog(text) == ["- One"]


def test_build_release_notes_validates_as_artifact() -> None:
    state = State.model_validate(_state_payload())
    body = build_release_notes(
        state,
        from_phase="P17",
        to_phase="P17",
        changelog_text="# Changelog\n\n## [Unreleased]\n\n- PR hardening\n",
    )
    assert "`P17` PR hardening" in body
    assert validate_markdown_artifact(body).ok
