"""Tests for the registered-kind population helpers.

The wired-on sweep keys on :func:`registered_audit_dsl_kinds` -- the union of
the file-set :data:`CHECK_REGISTRY` keys and the state-scoring
:data:`CLOSE_GATE_KINDS`. These tests pin that union so a kind cannot drop out
of the swept population (which would let it ship registered-but-idle
unnoticed).
"""

from __future__ import annotations

from typing import get_args

from eawf.kernel.spec.common import _GATE_KIND_RESPONSE, _GATE_KIND_TIER
from eawf.workflow.audit_dsl.models import CheckKind
from eawf.workflow.audit_dsl.registry import (
    CHECK_REGISTRY,
    CLOSE_GATE_KINDS,
    registered_audit_dsl_kinds,
)


def test_registered_kinds_is_union_of_check_and_close_gate() -> None:
    registered = registered_audit_dsl_kinds()
    assert registered == frozenset(CHECK_REGISTRY) | CLOSE_GATE_KINDS


def test_check_registry_kinds_are_all_registered() -> None:
    # Every file-set check kind is part of the swept registered population.
    assert frozenset(CHECK_REGISTRY) <= registered_audit_dsl_kinds()


def test_registered_kinds_is_frozen() -> None:
    # The helper returns an immutable set so a caller cannot mutate the swept
    # population by side effect.
    assert isinstance(registered_audit_dsl_kinds(), frozenset)


def test_wave_spec_walker_kind_is_gone() -> None:
    # The kind that walked wave spec files is retired everywhere a kind name
    # is declared: the CheckKind literal, the runner registry and both oracle
    # tables, so no gate can still name it.
    retired = "verify_implements"
    assert retired not in get_args(CheckKind)
    assert retired not in CHECK_REGISTRY
    assert retired not in _GATE_KIND_TIER
    assert retired not in _GATE_KIND_RESPONSE
