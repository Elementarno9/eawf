"""Tests for the registered-kind population helpers (P30-I10-W02).

The wired-on sweep keys on :func:`registered_audit_dsl_kinds` -- the union of
the file-set :data:`CHECK_REGISTRY` keys and the state-scoring
:data:`CLOSE_GATE_KINDS`. These tests pin that union so a kind cannot drop out
of the swept population (which would let it ship registered-but-idle
unnoticed).
"""

from __future__ import annotations

from eawf.workflow.audit_dsl.kinds.backlog_resolution import BACKLOG_RESOLUTION_KIND
from eawf.workflow.audit_dsl.registry import (
    CHECK_REGISTRY,
    CLOSE_GATE_KINDS,
    registered_audit_dsl_kinds,
)


def test_registered_kinds_is_union_of_check_and_close_gate() -> None:
    registered = registered_audit_dsl_kinds()
    assert registered == frozenset(CHECK_REGISTRY) | CLOSE_GATE_KINDS


def test_backlog_resolution_is_a_registered_close_gate_kind() -> None:
    # The new close-gate kind is registered (so the wired-on sweep counts it)
    # but does NOT take the file-set runner shape, so it stays out of
    # CHECK_REGISTRY.
    assert BACKLOG_RESOLUTION_KIND in CLOSE_GATE_KINDS
    assert BACKLOG_RESOLUTION_KIND in registered_audit_dsl_kinds()
    assert BACKLOG_RESOLUTION_KIND not in CHECK_REGISTRY


def test_check_registry_kinds_are_all_registered() -> None:
    # Every file-set check kind is part of the swept registered population.
    assert frozenset(CHECK_REGISTRY) <= registered_audit_dsl_kinds()


def test_registered_kinds_is_frozen() -> None:
    # The helper returns an immutable set so a caller cannot mutate the swept
    # population by side effect.
    assert isinstance(registered_audit_dsl_kinds(), frozenset)
