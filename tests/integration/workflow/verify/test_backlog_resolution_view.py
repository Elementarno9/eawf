"""Every registered audit-DSL kind is wired to a readiness consumer.

:func:`wired_audit_dsl_kinds` covers every registered audit-DSL kind, so the
BIND-1 wired-on sweep finds no kind registered but idle, including
``tui_flow`` and ``backlog_resolution``.
"""

from __future__ import annotations

from eawf.workflow.audit_dsl.registry import registered_audit_dsl_kinds
from eawf.workflow.verify.readiness import wired_audit_dsl_kinds


def test_wired_kinds_cover_every_registered_kind() -> None:
    # The post-W02 tree: every registered kind is wired (the wired-on sweep is
    # clean). No kind ships registered-but-idle.
    registered = registered_audit_dsl_kinds()
    wired = wired_audit_dsl_kinds()
    assert registered - wired == frozenset()


def test_previously_idle_tui_flow_is_now_wired() -> None:
    # tui_flow was registered-but-idle (no _GATE_KIND_TIER entry); the
    # supplemental tier binding un-idles it.
    assert "tui_flow" in wired_audit_dsl_kinds()


def test_backlog_resolution_is_wired() -> None:
    # The new close-gate kind is wired via CLOSE_GATE_KINDS, not a tier map.
    assert "backlog_resolution" in wired_audit_dsl_kinds()


def test_wired_kinds_is_frozen() -> None:
    assert isinstance(wired_audit_dsl_kinds(), frozenset)
