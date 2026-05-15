"""Unit tests for the audit-running overlay (P20-I01-W07).

Covers the per-panel builders, the remediation-hints derivation, the
action-menu keymap, the :class:`AuditAttachment` model, and the single
:func:`open_audit_overlay` dispatch entry. Integration-level golden
snapshots live in ``tests/integration/test_tui_audit_overlay.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    ProjectStatus,
    ScopeKind,
)
from eawf.state.models import (
    Audit,
    CurrentPointers,
    Project,
    State,
)
from eawf.tui.audit_overlay import (
    ACTION_DEFERRED_MARKER,
    ACTION_DEFERRED_TOAST,
    ACTION_KEYMAP,
    HINT_FAILED,
    HINT_PENDING,
    HINT_RUNNING,
    HINT_VERDICT,
    KNOWN_AUDIT_ACTIONS,
    KNOWN_HARNESS_ADAPTERS,
    AuditAttachment,
    build_actions_panel,
    build_attachment_panel,
    build_audit_summary_panel,
    build_remediation_panel,
    handle_action_key,
    open_audit_overlay,
    remediation_hints,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _utc() -> datetime:
    """Deterministic UTC timestamp for fixtures."""
    return datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)


def _make_audit(
    aid: str = "A21-P16",
    *,
    kind: AuditKind = AuditKind.SHIP_GATE,
    status: AuditStatus = AuditStatus.RUNNING,
    verdict: AuditVerdict | None = None,
    report_artifact_id: str | None = None,
) -> Audit:
    return Audit(
        id=aid,
        scope_id="P20",
        kind=kind,
        status=status,
        report_artifact_id=report_artifact_id,
        check_results=[],
        integrity_results=[],
        created_at=_utc(),
        verdict=verdict,
    )


def _make_state(*, audits: list[Audit] | None = None) -> State:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.REPO.value,
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": _utc(),
        "project": Project(
            code="EAWF",
            slug="eawf",
            title="EAWF",
            description=None,
            domains=["dev"],
            default_branch="main",
            status=ProjectStatus.ACTIVE,
            repo_urn="urn:eawf:v1:repo:EAWF",
        ).model_dump(mode="json"),
        "current": CurrentPointers(
            project_code="EAWF",
            phase_id="P20",
            iter_id="P20-I01",
        ).model_dump(mode="json"),
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "audits": {a.id: a.model_dump(mode="json") for a in (audits or [])} or None,
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def _render(renderable: Any) -> str:
    """Render a Rich object into a string buffer for inspection."""
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=100, record=False).print(renderable)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Known-set constants — keep the action and harness enumerations stable
# ---------------------------------------------------------------------------


def test_known_audit_actions_is_three_tuple() -> None:
    """Action menu surfaces exactly three deferred verbs."""
    assert KNOWN_AUDIT_ACTIONS == ("retry", "mark-blocked", "escalate")


def test_known_harness_adapters_matches_d12_scope() -> None:
    """v0.3 harness scope: claude-code / codex / opencode + unknown."""
    assert KNOWN_HARNESS_ADAPTERS == ("claude-code", "codex", "opencode", "unknown")


def test_action_keymap_uses_single_letter_shortcuts() -> None:
    """Each action shortcut is exactly one character."""
    for key in ACTION_KEYMAP:
        assert len(key) == 1, f"shortcut {key!r} is not single-letter"


def test_action_keymap_covers_every_known_action() -> None:
    """ACTION_KEYMAP is exhaustive over KNOWN_AUDIT_ACTIONS."""
    assert set(ACTION_KEYMAP.values()) == set(KNOWN_AUDIT_ACTIONS)


def test_action_keymap_has_no_duplicate_shortcuts() -> None:
    """Two actions cannot share a shortcut."""
    shortcuts = list(ACTION_KEYMAP)
    assert len(shortcuts) == len(set(shortcuts))


def test_action_deferred_marker_is_v04() -> None:
    """The marker MUST read ``(v0.4)`` per the dispatch spec."""
    assert ACTION_DEFERRED_MARKER == "(v0.4)"


def test_action_deferred_toast_text() -> None:
    """The footer toast MUST read ``action deferred to v0.4``."""
    assert ACTION_DEFERRED_TOAST == "action deferred to v0.4"


# ---------------------------------------------------------------------------
# AuditAttachment — Pydantic v2 strict
# ---------------------------------------------------------------------------


def test_audit_attachment_rejects_extra_fields() -> None:
    """``extra="forbid"`` per AGENTS.md rule 2."""
    with pytest.raises(ValidationError, match="Extra inputs"):
        AuditAttachment(
            pid=12345,
            harness_adapter="claude-code",
            extra="nope",  # type: ignore[call-arg]
        )


def test_audit_attachment_rejects_zero_pid() -> None:
    """PID must be a positive integer."""
    with pytest.raises(ValidationError):
        AuditAttachment(pid=0, harness_adapter="claude-code")


def test_audit_attachment_rejects_negative_pid() -> None:
    with pytest.raises(ValidationError):
        AuditAttachment(pid=-1, harness_adapter="claude-code")


def test_audit_attachment_rejects_unknown_adapter() -> None:
    with pytest.raises(ValidationError):
        AuditAttachment(pid=1, harness_adapter="not-a-known-adapter")  # type: ignore[arg-type]


def test_audit_attachment_accepts_known_adapters() -> None:
    """Every adapter in KNOWN_HARNESS_ADAPTERS must validate."""
    for adapter in KNOWN_HARNESS_ADAPTERS:
        attachment = AuditAttachment(pid=1, harness_adapter=adapter)
        assert attachment.harness_adapter == adapter


def test_audit_attachment_optional_agent_session_id() -> None:
    """``agent_session_id`` is None by default; setting it is permitted."""
    bare = AuditAttachment(pid=42, harness_adapter="claude-code")
    assert bare.agent_session_id is None
    with_session = AuditAttachment(
        pid=42, harness_adapter="claude-code", agent_session_id="SES-001"
    )
    assert with_session.agent_session_id == "SES-001"


# ---------------------------------------------------------------------------
# remediation_hints — status + verdict derivation
# ---------------------------------------------------------------------------


def test_remediation_hints_running_audit_surfaces_running_hint() -> None:
    audit = _make_audit(status=AuditStatus.RUNNING, verdict=None)
    hints = remediation_hints(audit)
    assert HINT_RUNNING in hints


def test_remediation_hints_failed_audit_surfaces_failed_hint() -> None:
    audit = _make_audit(status=AuditStatus.FAILED, verdict=None)
    hints = remediation_hints(audit)
    assert HINT_FAILED in hints


def test_remediation_hints_pending_audit_surfaces_pending_hint() -> None:
    audit = _make_audit(status=AuditStatus.PENDING, verdict=None)
    hints = remediation_hints(audit)
    assert HINT_PENDING in hints


def test_remediation_hints_complete_pass_has_no_hints() -> None:
    """``complete`` + ``pass`` is the green-path; no hints surface."""
    audit = _make_audit(status=AuditStatus.COMPLETE, verdict=AuditVerdict.PASS)
    assert remediation_hints(audit) == []


def test_remediation_hints_major_verdict_surfaces_block_hint() -> None:
    audit = _make_audit(status=AuditStatus.COMPLETE, verdict=AuditVerdict.MAJOR)
    hints = remediation_hints(audit)
    assert HINT_VERDICT[AuditVerdict.MAJOR.value] in hints


def test_remediation_hints_minor_verdict_surfaces_triage_hint() -> None:
    audit = _make_audit(status=AuditStatus.COMPLETE, verdict=AuditVerdict.MINOR)
    hints = remediation_hints(audit)
    assert HINT_VERDICT[AuditVerdict.MINOR.value] in hints


def test_remediation_hints_failed_plus_major_yields_both_hints() -> None:
    """Status-hint + verdict-hint compose — order: status first, verdict second."""
    audit = _make_audit(status=AuditStatus.FAILED, verdict=AuditVerdict.MAJOR)
    hints = remediation_hints(audit)
    assert hints == [HINT_FAILED, HINT_VERDICT[AuditVerdict.MAJOR.value]]


# ---------------------------------------------------------------------------
# build_audit_summary_panel
# ---------------------------------------------------------------------------


def test_build_audit_summary_panel_renders_id_kind_status() -> None:
    audit = _make_audit(aid="A21-P16", kind=AuditKind.SHIP_GATE, status=AuditStatus.RUNNING)
    rendered = _render(build_audit_summary_panel(audit))
    assert "A21-P16" in rendered
    assert "ship-gate" in rendered
    assert "running" in rendered
    # The placeholder dash appears in the verdict cell when verdict is None.
    lines = rendered.splitlines()
    verdict_line = next(line for line in lines if "verdict:" in line)
    assert "-" in verdict_line


def test_build_audit_summary_panel_renders_verdict_when_set() -> None:
    audit = _make_audit(verdict=AuditVerdict.MAJOR)
    rendered = _render(build_audit_summary_panel(audit))
    assert "major" in rendered


def test_build_audit_summary_panel_renders_report_link() -> None:
    audit = _make_audit(report_artifact_id="ART-REP-42")
    rendered = _render(build_audit_summary_panel(audit))
    assert "ART-REP-42" in rendered


def test_build_audit_summary_panel_uses_dash_for_missing_report() -> None:
    audit = _make_audit(report_artifact_id=None)
    rendered = _render(build_audit_summary_panel(audit))
    lines = rendered.splitlines()
    report_line = next(line for line in lines if "report:" in line)
    assert "-" in report_line


# ---------------------------------------------------------------------------
# build_attachment_panel
# ---------------------------------------------------------------------------


def test_build_attachment_panel_with_attachment_renders_pid_and_adapter() -> None:
    attachment = AuditAttachment(
        pid=12345, harness_adapter="claude-code", agent_session_id="SES-001"
    )
    rendered = _render(build_attachment_panel(attachment))
    assert "12345" in rendered
    assert "claude-code" in rendered
    assert "SES-001" in rendered


def test_build_attachment_panel_none_shows_placeholder() -> None:
    rendered = _render(build_attachment_panel(None))
    assert "no runtime attached" in rendered


def test_build_attachment_panel_session_dash_when_unset() -> None:
    attachment = AuditAttachment(pid=1, harness_adapter="codex")
    rendered = _render(build_attachment_panel(attachment))
    lines = rendered.splitlines()
    session_line = next(line for line in lines if "session:" in line)
    assert "-" in session_line


# ---------------------------------------------------------------------------
# build_remediation_panel
# ---------------------------------------------------------------------------


def test_build_remediation_panel_renders_each_hint() -> None:
    hints = [HINT_FAILED, HINT_VERDICT[AuditVerdict.MAJOR.value]]
    rendered = _render(build_remediation_panel(hints))
    assert HINT_FAILED in rendered
    assert HINT_VERDICT[AuditVerdict.MAJOR.value] in rendered


def test_build_remediation_panel_empty_collapses_to_dash() -> None:
    rendered = _render(build_remediation_panel([]))
    lines = rendered.splitlines()
    # The placeholder is the first non-border body row.
    body_rows = [
        line for line in lines if "│" in line and "remediation" not in line and "─" not in line
    ]
    assert any("-" in row for row in body_rows)


# ---------------------------------------------------------------------------
# build_actions_panel
# ---------------------------------------------------------------------------


def test_build_actions_panel_renders_every_action() -> None:
    rendered = _render(build_actions_panel())
    for action in KNOWN_AUDIT_ACTIONS:
        assert action in rendered, f"action {action!r} missing from actions panel"


def test_build_actions_panel_renders_v04_marker_per_row() -> None:
    """Each action row carries the ``(v0.4)`` deferred marker."""
    rendered = _render(build_actions_panel())
    # One marker per action row + one in the panel title.
    marker_count = rendered.count(ACTION_DEFERRED_MARKER)
    assert marker_count >= len(KNOWN_AUDIT_ACTIONS) + 1


def test_build_actions_panel_renders_each_shortcut() -> None:
    rendered = _render(build_actions_panel())
    for key in ACTION_KEYMAP:
        assert key in rendered, f"shortcut {key!r} missing"


def test_build_actions_panel_title_carries_v04_marker() -> None:
    """The panel title row repeats the marker so it cannot be missed."""
    rendered = _render(build_actions_panel())
    # First line containing 'actions' should also contain the marker.
    lines = rendered.splitlines()
    title_line = next(line for line in lines if "actions" in line)
    assert ACTION_DEFERRED_MARKER in title_line


# ---------------------------------------------------------------------------
# handle_action_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key,_action", list(ACTION_KEYMAP.items()))
def test_handle_action_key_known_returns_deferred_toast(key: str, _action: str) -> None:
    """Every known shortcut returns the deferred-toast string."""
    assert handle_action_key(key) == ACTION_DEFERRED_TOAST


def test_handle_action_key_unknown_returns_none() -> None:
    """Unknown keystrokes propagate to the caller's dispatch table."""
    assert handle_action_key("z") is None
    assert handle_action_key("") is None


def test_handle_action_key_non_string_raises_typeerror() -> None:
    with pytest.raises(TypeError, match="key must be str"):
        handle_action_key(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# open_audit_overlay — single dispatch entry
# ---------------------------------------------------------------------------


def test_open_audit_overlay_returns_layout_with_header_and_body() -> None:
    audit = _make_audit()
    state = _make_state(audits=[audit])
    layout = open_audit_overlay(state, audit.id)
    rendered = _render(layout)
    assert "Eä" in rendered
    assert "EAWF" in rendered
    assert "audit A21-P16" in rendered  # overlay title
    assert "ship-gate" in rendered


def test_open_audit_overlay_with_attachment_renders_pid_block() -> None:
    audit = _make_audit()
    state = _make_state(audits=[audit])
    attachment = AuditAttachment(
        pid=99999, harness_adapter="opencode", agent_session_id="SES-AUDIT"
    )
    rendered = _render(open_audit_overlay(state, audit.id, attachment=attachment))
    assert "99999" in rendered
    assert "opencode" in rendered
    assert "SES-AUDIT" in rendered


def test_open_audit_overlay_no_attachment_shows_placeholder() -> None:
    audit = _make_audit()
    state = _make_state(audits=[audit])
    rendered = _render(open_audit_overlay(state, audit.id))
    assert "no runtime attached" in rendered


def test_open_audit_overlay_renders_remediation_when_running() -> None:
    audit = _make_audit(status=AuditStatus.RUNNING)
    state = _make_state(audits=[audit])
    rendered = _render(open_audit_overlay(state, audit.id))
    assert HINT_RUNNING in rendered


def test_open_audit_overlay_renders_remediation_when_failed_major() -> None:
    audit = _make_audit(status=AuditStatus.FAILED, verdict=AuditVerdict.MAJOR)
    state = _make_state(audits=[audit])
    rendered = _render(open_audit_overlay(state, audit.id))
    assert HINT_FAILED in rendered
    assert HINT_VERDICT[AuditVerdict.MAJOR.value] in rendered


def test_open_audit_overlay_renders_action_menu_with_v04_markers() -> None:
    audit = _make_audit()
    state = _make_state(audits=[audit])
    rendered = _render(open_audit_overlay(state, audit.id))
    for action in KNOWN_AUDIT_ACTIONS:
        assert action in rendered
    # At least one marker per action plus title.
    assert rendered.count(ACTION_DEFERRED_MARKER) >= len(KNOWN_AUDIT_ACTIONS) + 1


# ---------------------------------------------------------------------------
# open_audit_overlay — error paths
# ---------------------------------------------------------------------------


def test_open_audit_overlay_unknown_audit_raises_keyerror() -> None:
    state = _make_state(audits=[_make_audit("A21-P16")])
    with pytest.raises(KeyError, match="unknown audit"):
        open_audit_overlay(state, "A99-P99")


def test_open_audit_overlay_empty_audit_bucket_raises_keyerror() -> None:
    """Missing optional ``audits`` bucket (``None``) MUST propagate KeyError."""
    state = _make_state(audits=None)
    with pytest.raises(KeyError, match="unknown audit"):
        open_audit_overlay(state, "A21-P16")


def test_open_audit_overlay_non_string_target_id_raises_typeerror() -> None:
    state = _make_state()
    with pytest.raises(TypeError, match="target_id must be str"):
        open_audit_overlay(state, 123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Brand chassis lives outside-left of breadcrumb (rule consistency)
# ---------------------------------------------------------------------------


def test_open_audit_overlay_brand_left_of_breadcrumb() -> None:
    audit = _make_audit()
    state = _make_state(audits=[audit])
    rendered = _render(open_audit_overlay(state, audit.id))
    brand_idx = rendered.find("Eä")
    project_idx = rendered.find("EAWF")
    assert brand_idx >= 0
    assert project_idx > brand_idx, "brand must sit outside-left of breadcrumb"


# ---------------------------------------------------------------------------
# Read-only contract — no mutation paths
# ---------------------------------------------------------------------------


def test_open_audit_overlay_does_not_mutate_state() -> None:
    """The overlay is strictly read-only; state must round-trip unchanged."""
    audit = _make_audit(status=AuditStatus.FAILED, verdict=AuditVerdict.MAJOR)
    state = _make_state(audits=[audit])
    before = state.model_dump(mode="json")
    open_audit_overlay(
        state,
        audit.id,
        attachment=AuditAttachment(pid=12345, harness_adapter="claude-code"),
    )
    after = state.model_dump(mode="json")
    assert before == after


def test_handle_action_key_does_not_execute_actions() -> None:
    """Pressing an action key returns the toast — never executes anything."""
    # Sanity: the toast text is the only side effect.
    for key in ACTION_KEYMAP:
        assert handle_action_key(key) == ACTION_DEFERRED_TOAST
