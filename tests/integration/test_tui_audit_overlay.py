"""Integration tests for the audit-running overlay (P20-I01-W07).

Covers byte-stable golden snapshots for the three audit states the
overlay surfaces (running + attached, failed + major, complete + pass)
plus structural assertions that hold independent of the snapshot
bytes (brand outside-left of breadcrumb, action menu deferred markers,
read-only contract).

When the renderer drifts intentionally, regenerate the snapshots:

    cd <repo>
    uv run python -c "
    import io, json
    from pathlib import Path
    from rich.console import Console
    from eawf.state.models import State
    from eawf.tui.audit_overlay import open_audit_overlay, AuditAttachment

    fixture = Path('tests/golden/tui')
    state = State.model_validate(
        json.loads((fixture / 'audit_overlay_state.json').read_text())
    )
    attachment = AuditAttachment(
        pid=12345, harness_adapter='claude-code', agent_session_id='SES-AUDIT-001',
    )
    plans = [
        ('A20-RUN', attachment, 'audit_overlay_running.txt'),
        ('A21-FAIL', None, 'audit_overlay_failed.txt'),
        ('A22-PASS', None, 'audit_overlay_pass.txt'),
    ]
    for target, att, name in plans:
        buf = io.StringIO()
        Console(file=buf, force_terminal=False, width=100, height=30, record=False).print(
            open_audit_overlay(state, target, attachment=att)
        )
        (fixture / name).write_text(buf.getvalue())
    "
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from eawf.state.models import State
from eawf.tui.audit_overlay import (
    ACTION_DEFERRED_MARKER,
    ACTION_DEFERRED_TOAST,
    ACTION_KEYMAP,
    KNOWN_AUDIT_ACTIONS,
    AuditAttachment,
    handle_action_key,
    open_audit_overlay,
)

_FIXTURE_DIR: Path = Path(__file__).parent.parent / "golden" / "tui"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture_state() -> State:
    payload = json.loads((_FIXTURE_DIR / "audit_overlay_state.json").read_text(encoding="utf-8"))
    return State.model_validate(payload)


def _fixture_attachment() -> AuditAttachment:
    return AuditAttachment(
        pid=12345,
        harness_adapter="claude-code",
        agent_session_id="SES-AUDIT-001",
    )


def _render(renderable: object) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100, height=30, record=False)
    console.print(renderable)
    return buf.getvalue()


def _normalise_trailing_newline(text: str) -> str:
    if text.endswith("\n"):
        return text[:-1]
    return text


# ---------------------------------------------------------------------------
# Golden snapshots — one per audit state (success criterion 1 + 2 + 3)
# ---------------------------------------------------------------------------


@pytest.mark.golden
def test_audit_overlay_running_matches_golden() -> None:
    """Running audit + attached runtime: pid/adapter/session + remediation hint."""
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(
        _render(open_audit_overlay(state, "A20-RUN", attachment=_fixture_attachment()))
    )
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "audit_overlay_running.txt").read_text(encoding="utf-8")
    )
    assert actual == expected, (
        "audit-overlay running snapshot drift — regenerate "
        "tests/golden/tui/audit_overlay_running.txt with the snippet at the "
        "top of test_tui_audit_overlay.py."
    )


@pytest.mark.golden
def test_audit_overlay_failed_matches_golden() -> None:
    """Failed audit + major verdict: dual remediation hints; no attachment."""
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(_render(open_audit_overlay(state, "A21-FAIL")))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "audit_overlay_failed.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


@pytest.mark.golden
def test_audit_overlay_pass_matches_golden() -> None:
    """Complete + pass: no remediation; action menu still rendered (deferred)."""
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(_render(open_audit_overlay(state, "A22-PASS")))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "audit_overlay_pass.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


# ---------------------------------------------------------------------------
# Structural assertions (independent of byte-equality)
# ---------------------------------------------------------------------------


def test_audit_overlay_brand_outside_left_of_breadcrumb() -> None:
    """Eä brand sits outside-left of the breadcrumb in every audit overlay."""
    state = _load_fixture_state()
    for target in ("A20-RUN", "A21-FAIL", "A22-PASS"):
        rendered = _render(open_audit_overlay(state, target))
        brand_idx = rendered.find("Eä")
        crumb_idx = rendered.find("EAWF")
        assert brand_idx >= 0, f"missing brand in audit {target}"
        assert crumb_idx > brand_idx, f"breadcrumb not after brand in audit {target}"


def test_audit_overlay_running_renders_attached_pid_and_adapter() -> None:
    """Success criterion 2 — PID + harness adapter visible for attached runtime."""
    state = _load_fixture_state()
    rendered = _render(open_audit_overlay(state, "A20-RUN", attachment=_fixture_attachment()))
    # Both the pid and the adapter id must appear in the rendered output.
    assert "12345" in rendered
    assert "claude-code" in rendered
    assert "SES-AUDIT-001" in rendered


def test_audit_overlay_failed_surfaces_remediation_hints() -> None:
    """Success criterion 1 — failed + major surfaces both hints."""
    state = _load_fixture_state()
    rendered = _render(open_audit_overlay(state, "A21-FAIL"))
    assert "rerun the failing checks" in rendered
    assert "block ship until downgraded" in rendered


def test_audit_overlay_running_surfaces_running_hint() -> None:
    """Success criterion 1 — running audit surfaces the running hint."""
    state = _load_fixture_state()
    rendered = _render(open_audit_overlay(state, "A20-RUN", attachment=_fixture_attachment()))
    assert "audit is still running" in rendered


def test_audit_overlay_pass_has_no_remediation_lines() -> None:
    """Success criterion 1 — pass audit collapses remediation to a single dash."""
    state = _load_fixture_state()
    rendered = _render(open_audit_overlay(state, "A22-PASS"))
    # No status/verdict-derived hints should appear.
    assert "rerun the failing checks" not in rendered
    assert "audit is still running" not in rendered
    assert "block ship" not in rendered
    assert "triage" not in rendered


def test_audit_overlay_actions_panel_marks_every_row_v04() -> None:
    """Success criterion 3 — every action row carries ``(v0.4)``."""
    state = _load_fixture_state()
    rendered = _render(open_audit_overlay(state, "A21-FAIL"))
    for action in KNOWN_AUDIT_ACTIONS:
        assert action in rendered, f"action {action!r} missing"
    # Marker appears once per action plus once in the title.
    assert rendered.count(ACTION_DEFERRED_MARKER) >= len(KNOWN_AUDIT_ACTIONS) + 1


def test_audit_overlay_two_renders_byte_stable() -> None:
    """Calling :func:`open_audit_overlay` twice yields identical bytes."""
    state = _load_fixture_state()
    first = _render(open_audit_overlay(state, "A20-RUN", attachment=_fixture_attachment()))
    second = _render(open_audit_overlay(state, "A20-RUN", attachment=_fixture_attachment()))
    assert first == second


def test_audit_overlay_action_key_returns_toast_for_each_shortcut() -> None:
    """Success criterion 3 — every shortcut returns the deferred toast."""
    for key in ACTION_KEYMAP:
        toast = handle_action_key(key)
        assert toast == ACTION_DEFERRED_TOAST


def test_audit_overlay_does_not_mutate_state_across_renders() -> None:
    """Read-only contract: state round-trips byte-for-byte across renders."""
    state = _load_fixture_state()
    before = state.model_dump(mode="json")
    open_audit_overlay(state, "A20-RUN", attachment=_fixture_attachment())
    open_audit_overlay(state, "A21-FAIL")
    open_audit_overlay(state, "A22-PASS")
    after = state.model_dump(mode="json")
    assert before == after


def test_audit_overlay_unknown_audit_raises_keyerror() -> None:
    """KeyError when id is not in ``state.audits``."""
    state = _load_fixture_state()
    with pytest.raises(KeyError, match="unknown audit"):
        open_audit_overlay(state, "A99-MISSING")
