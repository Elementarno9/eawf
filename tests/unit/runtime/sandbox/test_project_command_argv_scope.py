"""AUTH-039: the project's own command line is a registered, read-only family.

The interim epoch-1 widening that admitted ``eawf`` and ``just`` as bare
argv heads is superseded by the ``command_family_ref`` registry: ``eawf``
is admissible as a registered family scoped to its read-only forms, and
every gate boundary -- authoring, promote, the close runner, compilation
and the release proof binding -- resolves an argv through that registry.
A gate row is stored without its family and given one as it loads, so the
closed wave's ``release tag --dry-run`` gate in the committed state still
loads without a hand edit and no stored row is rewritten. The plan-time
floor and the close boundary resolve ``gate_ids`` against the closing
scope's own gates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.release.gate_binding import (
    GateBindingError,
    GateBindingRejection,
    parse_gate_bindings,
)
from eawf.kernel.spec.common import CriterionSpec, GateSpec, validate_criterion_gate_refs
from eawf.kernel.spec.promotion import SpecPromoteValidationError, validate_argv_gates
from eawf.kernel.state.models import State
from eawf.runtime.sandbox.argv_policy import ArgvPolicyError, resolve_command_family
from eawf.workflow.audit_dsl.models import CheckSpec
from eawf.workflow.audit_dsl.registry import CHECK_REGISTRY

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
_STATE_PATH: Final[Path] = _REPO_ROOT / ".ea" / "state.json"
_DRY_RUN_ARGV: Final[list[str]] = [
    "uv",
    "run",
    "eawf",
    "release",
    "tag",
    "0.7.0.dev1",
    "--dry-run",
]


def _gate(argv: list[str]) -> GateSpec:
    return GateSpec(
        id="G-01",
        criterion_id="CR-01",
        kind="command_exit_zero",
        args={"argv": argv},
        policy="block",
        cadence="every-wave",
    )


def _stored_row(argv: list[str]) -> dict[str, Any]:
    """A gate row in the shape stored before gates named a family."""
    return {
        "args": {"argv": argv, "scope": "all", "timeout_class": "quick"},
        "cadence": "every-wave",
        "criterion_id": "CR-03",
        "id": "G-03",
        "kind": "command_exit_zero",
        "policy": "block",
        "required": True,
        "timeout_s": None,
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["eawf", "status"],
        ["uv", "run", "eawf", "doctor"],
        _DRY_RUN_ARGV,
        ["just", "test-all"],
    ],
)
def test_auth_039_a_read_only_project_form_resolves_to_its_family(argv: list[str]) -> None:
    assert resolve_command_family(argv).family_id == argv[2 if argv[0] == "uv" else 0]


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "run", "eawf", "release", "tag", "0.7.0rc1"],
        ["eawf", "config", "set", "vcs.task_reference", "subject"],
        ["uv", "run", "eawf", "spec", "sync"],
        ["just", "release"],
    ],
)
def test_auth_039_a_mutating_project_form_is_refused(argv: list[str]) -> None:
    with pytest.raises(ArgvPolicyError):
        resolve_command_family(argv)


def test_auth_039_a_stored_row_without_a_family_is_migrated_on_load() -> None:
    row = _stored_row(_DRY_RUN_ARGV)
    gate = GateSpec.model_validate(row)
    assert gate.command_family_ref == "eawf"
    assert "command_family_ref" not in row
    # The family is derived on every load and never written back, so the
    # stored row and every digest over it stay byte-identical.
    assert gate.model_dump(mode="json") == row
    assert GateSpec.model_validate(gate.model_dump(mode="json")) == gate


def test_auth_039_a_stored_row_with_a_null_family_is_migrated_on_load() -> None:
    gate = GateSpec.model_validate(
        {**_stored_row(["git", "log", "--stat"]), "command_family_ref": None}
    )
    assert gate.command_family_ref == "git"


def test_auth_039_the_committed_closed_wave_dry_run_gate_still_loads() -> None:
    raw = json.loads(_STATE_PATH.read_bytes())
    stored = next(g for g in raw["waves"]["P31-I01-W26"]["gates"] if g["id"] == "G-03")
    assert stored["args"]["argv"] == _DRY_RUN_ARGV

    state = State.model_validate(raw)

    wave = state.waves["P31-I01-W26"]
    gate = next(g for g in wave.gates if g.id == "G-03")
    assert gate.args["argv"] == _DRY_RUN_ARGV
    assert gate.command_family_ref == "eawf"
    argv_gates = [g for w in state.waves.values() for g in w.gates if g.kind == "command_exit_zero"]
    assert argv_gates
    assert all(g.command_family_ref is not None for g in argv_gates)


def test_auth_039_a_gate_row_on_a_mutating_form_does_not_construct() -> None:
    with pytest.raises(ValidationError, match="L0 policy"):
        _gate(["uv", "run", "eawf", "release", "tag", "0.7.0rc1"])


def test_auth_039_promote_resolves_through_the_registry() -> None:
    gate = _gate(["eawf", "status"])
    validate_argv_gates([gate])
    forged = gate.model_copy(update={"command_family_ref": "pytest"})
    with pytest.raises(SpecPromoteValidationError, match="names command family 'pytest'"):
        validate_argv_gates([forged])


def test_auth_039_the_close_runner_resolves_through_the_registry(tmp_path: Path) -> None:
    spec = CheckSpec(
        kind="command_exit_zero",
        name="G-01",
        args={"argv": ["uv", "run", "eawf", "task", "complete", "EAWF-0001"]},
    )
    with pytest.raises(ArgvPolicyError, match="read-only"):
        CHECK_REGISTRY["command_exit_zero"](spec, tmp_path)


def _proof_source(argv: list[str]) -> dict[str, Any]:
    return {
        "bindings": [
            {
                "gate": "front_door_journey",
                "kind": "proof_command",
                "proof": {"command_id": "front_door", "argv": argv, "timeout_seconds": 60},
            }
        ]
    }


def test_auth_039_a_release_proof_resolves_through_the_registry() -> None:
    (binding,) = parse_gate_bindings(_proof_source(["uvx", "eawf", "--version"]))
    assert binding.proof is not None
    with pytest.raises(GateBindingError) as caught:
        parse_gate_bindings(_proof_source(["uv", "run", "eawf", "release", "publish", "X"]))
    assert caught.value.code is GateBindingRejection.ARGV_REJECTED


def _criterion(gate_ids: list[str]) -> CriterionSpec:
    return CriterionSpec(
        id="CR-01",
        text="the status verb exits zero on a healthy tree",
        kind="behavioral",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension="functional_suitability",
        measurable_signal="eawf status exits zero",
        gate_ids=gate_ids,
    )


def test_auth_039_a_gate_id_resolving_against_the_scope_gates_passes() -> None:
    validate_criterion_gate_refs([_criterion(["G-01"])], [_gate(["eawf", "status"])])


def test_auth_039_a_dangling_gate_id_is_refused_by_resolution_not_presence() -> None:
    with pytest.raises(ValueError, match="unknown gate id: 'G-02'"):
        validate_criterion_gate_refs([_criterion(["G-02"])], [_gate(["eawf", "status"])])
