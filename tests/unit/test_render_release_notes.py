from __future__ import annotations

from typing import Any

from eawf.kernel.state.models import State
from eawf.platform.artifacts.references import Citation
from eawf.platform.artifacts.validation import validate_markdown_artifact
from eawf.surfaces.render.artifact_chassis import render_references
from eawf.surfaces.render.release_notes import build_release_notes, mine_unreleased_changelog


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
            "P16": {
                "id": "P16",
                "scope_id": "ZZ",
                "track_id": None,
                "title": "Previous phase",
                "status": "closed",
                "iter_ids": [],
                "outcome_ids": [],
                "opened_at": "2026-05-07T00:00:00Z",
                "closed_at": "2026-05-07T00:01:00Z",
                "audit_id": "A21-P16",
            },
            "P17": {
                "id": "P17",
                "scope_id": "ZZ",
                "track_id": None,
                "title": "PR hardening",
                "status": "closed",
                "iter_ids": [],
                "outcome_ids": [],
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": "2026-05-08T00:01:00Z",
                "audit_id": "A22-P17",
            },
        },
        "iters": {},
        "waves": {},
        "audits": {
            "A21-P16": {
                "id": "A21-P16",
                "scope_id": "P16",
                "kind": "ship-gate",
                "status": "complete",
                "report_artifact_id": "ART-A21-P16",
                "check_results": [],
                "integrity_results": [],
                "created_at": "2026-05-07T00:01:00Z",
                "verdict": "pass",
            },
            "A22-P17": {
                "id": "A22-P17",
                "scope_id": "P17",
                "kind": "ship-gate",
                "status": "complete",
                "report_artifact_id": "ART-A22-P17",
                "check_results": [],
                "integrity_results": [],
                "created_at": "2026-05-08T00:01:00Z",
                "verdict": "pass",
            },
        },
        "artifacts": {
            "ART-A21-P16": {
                "id": "ART-A21-P16",
                "kind": "audit_report",
                "uri": "repo:.ea/artifacts/A21-P16-ship-gate.md",
                "urn": "urn:eawf:v1:artifact:ZZ/ART-A21-P16",
                "sha256": None,
                "size_bytes": None,
                "created_at": "2026-05-07T00:01:00Z",
                "metadata": {},
            },
            "ART-A22-P17": {
                "id": "ART-A22-P17",
                "kind": "audit_report",
                "uri": "repo:.ea/artifacts/A22-P17-ship-gate.md",
                "urn": "urn:eawf:v1:artifact:ZZ/ART-A22-P17",
                "sha256": None,
                "size_bytes": None,
                "created_at": "2026-05-08T00:01:00Z",
                "metadata": {},
            },
        },
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
    assert "## What" in body
    assert "- `P17` PR hardening (closed)." in body
    assert "## Validation" in body
    assert "`ART-A22-P17`" in body
    assert "`ART-A21-P16`" not in body
    assert "## Changelog" in body
    assert "- PR hardening" in body
    assert validate_markdown_artifact(body).ok


def test_build_release_notes_generates_changelog_when_unreleased_is_empty() -> None:
    state = State.model_validate(_state_payload())
    body = build_release_notes(
        state,
        from_phase="P17",
        to_phase="P17",
        changelog_text="# Changelog\n\n## [Unreleased]\n\n",
    )

    assert "- PR hardening." in body
    assert "- Changelog entries generated from narrative bundles [1]." in body
    assert validate_markdown_artifact(body).ok


def test_build_release_notes_routes_references_through_render_references() -> None:
    state = State.model_validate(_state_payload())
    body = build_release_notes(
        state,
        from_phase="P17",
        to_phase="P17",
        changelog_text="# Changelog\n\n## [Unreleased]\n\n- PR hardening\n",
    )
    # The mined-changelog path cites both rows, so the references block is the
    # shared render_references shape for state.json + CHANGELOG.md -- no
    # hand-rolled divergence.
    expected = render_references(
        [Citation(n=1, ref=".ea/state.json"), Citation(n=2, ref="CHANGELOG.md")]
    )
    assert "\n".join(expected) in body


def test_build_release_notes_references_block_drops_changelog_row_when_unmined() -> None:
    state = State.model_validate(_state_payload())
    body = build_release_notes(
        state,
        from_phase="P17",
        to_phase="P17",
        changelog_text="# Changelog\n\n## [Unreleased]\n\n",
    )
    # No mined changelog -> only the state.json row is cited (dense 1..1).
    expected = render_references([Citation(n=1, ref=".ea/state.json")])
    assert "\n".join(expected) in body
    assert "CHANGELOG.md" not in body.split("## References", 1)[1]
