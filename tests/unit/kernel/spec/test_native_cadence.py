"""The audit and gate cadences name the native scopes: Task, Batch and Milestone.

The epoch-1 values ``every-wave``, ``every-iter`` and ``every-phase`` are gone from
both Literals and from every shipped profile. A stored gate row (a filed proof in
the receipt ledger) still reads, mapped to its native value; an authored profile
check or audit document that names a retired value is refused with the value to
write instead.
"""

from __future__ import annotations

import typing
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.audit import AUDIT_CADENCE_VALUES, AuditCadence, AuditSpec
from eawf.kernel.spec.cadence import LEGACY_CADENCE_ALIASES
from eawf.kernel.spec.common import GateCadence, GateSpec
from eawf.platform.profiles import load_profile
from eawf.platform.profiles.loader import list_profiles
from eawf.platform.profiles.models import FloorCheck

_NATIVE = ("every-task", "every-batch", "every-milestone", "ship", "manual")
_PROFILE_DATA = Path(__file__).resolve().parents[4] / "src/eawf/platform/profiles/data"


def _gate(cadence: object) -> dict[str, object]:
    return {
        "id": "G-01",
        "criterion_id": "CR-01",
        "kind": "schema_validate",
        "args": {"model": "CloseReadiness"},
        "policy": "block",
        "cadence": cadence,
    }


def _floor_check(cadence: object) -> dict[str, object]:
    return {
        "name": "pytest",
        "cmd": ["uv", "run", "pytest", "-q"],
        "scope": "touched",
        "cadence": cadence,
        "policy": "warn",
    }


def test_cadence_values_are_native() -> None:
    assert typing.get_args(AuditCadence) == _NATIVE
    assert typing.get_args(GateCadence) == _NATIVE
    assert frozenset(_NATIVE) == AUDIT_CADENCE_VALUES
    profile_files = sorted(_PROFILE_DATA.glob("*.yaml"))
    assert profile_files
    for path in profile_files:
        text = path.read_text(encoding="utf-8")
        for legacy in ("every-wave", "every-iter", "every-phase"):
            assert legacy not in text, f"{path.name} still names {legacy}"
    for profile_id in list_profiles():
        assert load_profile(profile_id).name


def test_legacy_aliases_map_onto_native_values_only() -> None:
    assert LEGACY_CADENCE_ALIASES == {
        "every-wave": "every-task",
        "every-iter": "every-batch",
        "every-phase": "every-milestone",
    }
    assert set(LEGACY_CADENCE_ALIASES.values()) <= set(_NATIVE)
    assert not set(LEGACY_CADENCE_ALIASES) & set(_NATIVE)


@pytest.mark.parametrize(("legacy", "native"), sorted(LEGACY_CADENCE_ALIASES.items()))
def test_a_stored_gate_row_reads_its_native_cadence(legacy: str, native: str) -> None:
    gate = GateSpec.model_validate(_gate(legacy))

    assert gate.cadence == native
    # Written back as stored, so the digests a filed proof is keyed on do not move.
    assert gate.model_dump(mode="json")["cadence"] == legacy
    assert GateSpec.model_validate(gate.model_dump(mode="json")).cadence == native


@pytest.mark.parametrize("stored", ["every-wave", "bogus", ""])
def test_a_gate_refuses_a_stored_cadence_as_input(stored: str) -> None:
    """The stored spelling is read state: a row naming it is an unknown key."""
    with pytest.raises(ValidationError):
        GateSpec.model_validate({**_gate("every-task"), "stored_cadence": stored})


@pytest.mark.parametrize(("legacy", "native"), sorted(LEGACY_CADENCE_ALIASES.items()))
def test_a_stored_gate_row_copied_unchanged_writes_its_stored_spelling(
    legacy: str, native: str
) -> None:
    gate = GateSpec.model_validate(_gate(legacy))

    assert gate.model_copy().model_dump(mode="json")["cadence"] == legacy
    assert gate.model_copy(update={"cadence": native}).model_dump(mode="json")["cadence"] == legacy


@pytest.mark.parametrize("cadence", ["every-milestone", "ship", "manual"])
def test_a_stored_gate_row_copied_with_a_new_cadence_writes_the_new_value(cadence: str) -> None:
    gate = GateSpec.model_validate(_gate("every-wave"))

    moved = gate.model_copy(update={"cadence": cadence})

    assert moved.model_dump(mode="json")["cadence"] == cadence
    assert GateSpec.model_validate(moved.model_dump(mode="json")).cadence == cadence


@pytest.mark.parametrize("cadence", _NATIVE)
def test_a_native_gate_cadence_is_kept(cadence: str) -> None:
    assert GateSpec.model_validate(_gate(cadence)).cadence == cadence


@pytest.mark.parametrize("cadence", ["", "every-run", "Every-Task", None, 3])
def test_a_gate_refuses_an_unknown_cadence(cadence: object) -> None:
    with pytest.raises(ValidationError):
        GateSpec.model_validate(_gate(cadence))


@pytest.mark.parametrize(("legacy", "native"), sorted(LEGACY_CADENCE_ALIASES.items()))
def test_a_profile_check_refuses_a_retired_cadence_naming_its_successor(
    legacy: str, native: str
) -> None:
    with pytest.raises(ValidationError, match=f"{legacy!r} is retired; write {native!r}"):
        FloorCheck.model_validate(_floor_check(legacy))


@pytest.mark.parametrize("cadence", _NATIVE)
def test_a_profile_check_takes_each_native_cadence(cadence: str) -> None:
    assert FloorCheck.model_validate(_floor_check(cadence)).cadence == cadence


@pytest.mark.parametrize("cadence", ["", "every-run", None, ["every-task"]])
def test_a_profile_check_refuses_an_unknown_cadence(cadence: object) -> None:
    with pytest.raises(ValidationError):
        FloorCheck.model_validate(_floor_check(cadence))


def test_an_audit_document_refuses_a_retired_cadence() -> None:
    body = {
        "id": "audit-01",
        "scope_urn": "eawf://EAWF/task/EAWF-0001",
        "audit_kind": "review",
        "cadence": "every-phase",
        "checks": [{"name": "c1", "kind": "regex_in_file", "args": {}}],
    }

    with pytest.raises(ValidationError, match="'every-phase' is retired; write 'every-milestone'"):
        AuditSpec.model_validate(body)
