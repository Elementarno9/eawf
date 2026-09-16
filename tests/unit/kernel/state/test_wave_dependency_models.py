"""Wave dependency bindings and the State sparse maps that key them.

A binding that adopts its own Wave's integration generation would let a
Wave satisfy its own dependency, so it is refused. The State stores
barriers and bindings in sparse maps keyed by the directed edge; a key
that disagrees with the record's own edge would make a lookup by edge
return the wrong record, so the State refuses the mismatch at load.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.models import State, WaveDependencyBinding, wave_dependency_key

pytestmark = pytest.mark.unit

_TS = "2026-09-08T00:00:00Z"
WAVE = "P01-I01-W02"
DEP_WAVE = "P01-I01-W01"


def _binding_fields(**overrides: Any) -> dict[str, Any]:
    """Build the field mapping of a valid binding of WAVE onto DEP_WAVE."""
    fields: dict[str, Any] = {
        "wave_id": WAVE,
        "dep_wave_id": DEP_WAVE,
        "integration_id": "INT-0001",
        "generation": 1,
        "integrated_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "bound_at": _TS,
    }
    fields.update(overrides)
    return fields


def _barrier_fields() -> dict[str, Any]:
    """Build the field mapping of a valid barrier on WAVE's edge to DEP_WAVE."""
    return {
        "wave_id": WAVE,
        "dep_wave_id": DEP_WAVE,
        "start_after": "integrated",
        "land_after": "closed",
        "reason": "the downstream wave may start once the upstream integrates",
    }


def _state_document(**overrides: Any) -> dict[str, Any]:
    """Build a State document with no phases, iters or waves."""
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": _TS,
        "project": {
            "code": "QR",
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {"project_code": "QR"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    document.update(overrides)
    return document


def test_wave_dependency_binding_validates_across_two_waves() -> None:
    binding = WaveDependencyBinding.model_validate(_binding_fields())
    assert (binding.wave_id, binding.dep_wave_id) == (WAVE, DEP_WAVE)


def test_wave_dependency_binding_rejects_a_self_binding() -> None:
    with pytest.raises(ValidationError, match="dependency binding cannot reference itself"):
        WaveDependencyBinding.model_validate(_binding_fields(dep_wave_id=WAVE))


def test_state_validates_with_empty_dependency_maps() -> None:
    state = State.model_validate(_state_document())
    assert state.wave_dependency_barriers == {}
    assert state.wave_dependency_bindings == {}


# The reversed edge is the realistic mis-key: both ids are real, only the
# direction is wrong, and the key check must fire before any wave lookup.
def test_state_rejects_a_barrier_keyed_by_another_edge() -> None:
    reversed_key = wave_dependency_key(DEP_WAVE, WAVE)
    document = _state_document(wave_dependency_barriers={reversed_key: _barrier_fields()})
    with pytest.raises(
        ValidationError, match=r"wave dependency barrier key .* does not match edge"
    ):
        State.model_validate(document)


def test_state_rejects_a_binding_keyed_by_another_edge() -> None:
    reversed_key = wave_dependency_key(DEP_WAVE, WAVE)
    document = _state_document(wave_dependency_bindings={reversed_key: _binding_fields()})
    with pytest.raises(
        ValidationError, match=r"wave dependency binding key .* does not match edge"
    ):
        State.model_validate(document)
