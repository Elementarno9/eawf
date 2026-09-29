"""AUTH-039: the project's own command line is a gate head only for reading.

The epoch-1 gate argv floor admits ``eawf`` and ``just`` beside its
existing heads, scoped to read-only sub-verbs the way ``git`` is; the
scoping itself lives in :mod:`eawf.runtime.sandbox.argv_policy`. A gate
recorded under a preview form -- the closed wave's ``release tag
--dry-run`` gate -- still loads. The plan-time floor and the close
boundary resolve ``gate_ids`` against the closing scope's own gates.
The epoch-2 ``command_family_ref`` registry that supersedes this widening
is not built here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import CriterionSpec, GateSpec, validate_criterion_gate_refs
from eawf.kernel.spec.promotion import DEFAULT_GATE_ARGV_ALLOWLIST
from eawf.runtime.sandbox.argv_policy import ArgvPolicyError, validate_gate_argv

_FLOOR = list(DEFAULT_GATE_ARGV_ALLOWLIST)


def _gate(argv: list[str]) -> GateSpec:
    return GateSpec(
        id="G-01",
        criterion_id="CR-01",
        kind="command_exit_zero",
        args={"argv": argv},
        policy="block",
        cadence="every-wave",
    )


def test_auth_039_the_floor_admits_the_project_heads() -> None:
    assert {"eawf", "just"} <= set(DEFAULT_GATE_ARGV_ALLOWLIST)


@pytest.mark.parametrize(
    "argv",
    [
        ["eawf", "status"],
        ["uv", "run", "eawf", "doctor"],
        ["uv", "run", "eawf", "release", "tag", "0.7.0.dev1", "--dry-run"],
        ["just", "test-all"],
    ],
)
def test_auth_039_a_read_only_project_form_is_admitted(argv: list[str]) -> None:
    assert validate_gate_argv(argv, allowlist=_FLOOR) == argv


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
        validate_gate_argv(argv, allowlist=_FLOOR)


def test_auth_039_the_closed_wave_dry_run_gate_still_loads() -> None:
    argv = ["uv", "run", "eawf", "release", "tag", "0.7.0.dev1", "--dry-run"]
    assert _gate(argv).args["argv"] == argv


def test_auth_039_a_gate_row_on_a_mutating_form_does_not_construct() -> None:
    with pytest.raises(ValidationError):
        _gate(["uv", "run", "eawf", "release", "tag", "0.7.0rc1"])


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
