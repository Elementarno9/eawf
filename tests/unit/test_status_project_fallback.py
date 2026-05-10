"""Regression tests for the ``eawf status`` project-code fallback.

The v0.1 ``eawf init`` contract leaves ``state.project`` as ``None`` (the
full ``Project`` record requires ``domains`` which the wizard does not
collect; that record is materialised later by ``eawf project init``). The
status renderer must surface ``state.current.project_code`` in that
window — otherwise a successful init mis-renders as ``project: <none>``
and looks like a failure.

The existing :func:`tests.unit.test_status_render` module asserts the
"both project and project_code unset" → ``None`` branch; this module
covers the new "project_code set, project unset" branch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from eawf.cli.commands import status as status_mod
from eawf.state.enums import ScopeKind
from eawf.state.models import CurrentPointers, State

_DT = datetime(2026, 5, 10, tzinfo=UTC)


def _uninitialised_state(
    *,
    project_code: str | None,
    project_title: str | None = None,
) -> State:
    """Build a :class:`State` mirroring the post-``eawf init`` shape.

    ``project`` is ``None``; ``current.project_code`` is the stamped code;
    ``indexes`` carries the title (or omits it when *project_title* is
    ``None``) so callers can exercise both rendering paths.
    """
    indexes: dict[str, Any] = {}
    if project_title is not None:
        indexes["project_title"] = project_title
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:" + (project_code or "X"),
            "updated_at": _DT.isoformat(),
            "project": None,
            "current": CurrentPointers(project_code=project_code).model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": indexes,
        }
    )


def test_project_summary_none_when_project_and_code_missing() -> None:
    s = _uninitialised_state(project_code=None)
    assert status_mod._project_summary(s) is None


def test_project_summary_falls_back_to_current_project_code() -> None:
    s = _uninitialised_state(project_code="REPRO", project_title="Repro Project")
    out = status_mod._project_summary(s)
    assert out == {
        "code": "REPRO",
        "title": "Repro Project",
        "status": "uninitialised",
    }


def test_project_summary_fallback_with_missing_title_returns_empty_string() -> None:
    s = _uninitialised_state(project_code="REPRO")
    out = status_mod._project_summary(s)
    assert out == {"code": "REPRO", "title": "", "status": "uninitialised"}


def test_format_text_renders_uninitialised_status_when_title_empty() -> None:
    payload: dict[str, Any] = {
        "project": {"code": "REPRO", "title": "", "status": "uninitialised"},
        "current": {
            "project_code": "REPRO",
            "subproject_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "git": {"head": None, "branch": None, "dirty": None},
        "blockers": [],
    }
    text = status_mod._format_text(payload)
    assert "project: REPRO (uninitialised)" in text


def test_format_text_prefers_title_when_present() -> None:
    payload: dict[str, Any] = {
        "project": {"code": "REPRO", "title": "Repro Project", "status": "uninitialised"},
        "current": {
            "project_code": "REPRO",
            "subproject_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "git": {"head": None, "branch": None, "dirty": None},
        "blockers": [],
    }
    text = status_mod._format_text(payload)
    assert "project: REPRO (Repro Project)" in text
