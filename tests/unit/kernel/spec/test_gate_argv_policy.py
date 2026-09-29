"""DEL-035: the gate argv allowlist admits ``eawf`` and ``just``, scoped like ``git``.

A criterion whose truth is this project's own command line needs an
admissible falsifier, so the default gate allowlist carries ``eawf`` and
``just``. The widening is scoped to read-only forms the way ``git`` is
scoped to read-only sub-verbs: a mutating verb, a mutating flag or an
undeclared recipe is refused. A wrapper still recurses to the inner
command, and a shell metacharacter is refused on its own ground whatever
the head. A refusal names the head and quotes the argv it came from.
"""

from __future__ import annotations

import pytest

from eawf.kernel.release.gate_binding import PROOF_ARGV_ALLOWLIST
from eawf.kernel.spec.common import GateSpec
from eawf.kernel.spec.promotion import DEFAULT_GATE_ARGV_ALLOWLIST
from eawf.platform.profiles.models import FloorCheck
from eawf.runtime.sandbox.argv_policy import (
    EAWF_READ_ONLY_SUBVERBS,
    JUST_READ_ONLY_RECIPES,
    ArgvPolicyError,
    validate_gate_argv,
)
from eawf.workflow.verify.compile import FloorPackCompileError, compile_floor_pack

ALLOWLIST = list(DEFAULT_GATE_ARGV_ALLOWLIST)


def _gate(argv: list[str]) -> GateSpec:
    return GateSpec(
        id="G-01",
        criterion_id="CR-01",
        kind="command_exit_zero",
        args={"argv": argv, "scope": "all", "timeout_class": "quick"},
        policy="block",
        cadence="every-wave",
    )


def test_del_035_default_allowlist_carries_both_new_heads() -> None:
    assert {"eawf", "just"} <= set(DEFAULT_GATE_ARGV_ALLOWLIST)


@pytest.mark.parametrize(
    "argv",
    [
        ["eawf", "status"],
        ["uv", "run", "eawf", "validate"],
        ["uv", "run", "eawf", "doctor", "--json"],
        ["uvx", "eawf", "--version"],
        ["uv", "run", "eawf", "bench", "turn-cost", "--fixture", "live"],
        ["uv", "run", "eawf", "release", "tag", "0.7.0.dev1", "--dry-run"],
    ],
)
def test_del_035_admits_a_read_only_eawf_form(argv: list[str]) -> None:
    assert validate_gate_argv(argv, allowlist=ALLOWLIST) is argv
    assert _gate(argv).args["argv"] == argv


@pytest.mark.parametrize("recipe", sorted(JUST_READ_ONLY_RECIPES))
def test_del_035_admits_each_declared_just_recipe(recipe: str) -> None:
    argv = ["just", recipe]
    assert validate_gate_argv(argv, allowlist=ALLOWLIST) is argv


def test_del_035_admits_a_just_recipe_with_its_arguments() -> None:
    argv = ["just", "test", "changed", "origin/main"]
    assert validate_gate_argv(argv, allowlist=ALLOWLIST) is argv


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "run", "eawf", "bench", "turn-cost", "--fixture", "live"],
        ["uvx", "eawf", "--version"],
    ],
)
def test_del_035_release_proof_commands_stay_admissible(argv: list[str]) -> None:
    assert validate_gate_argv(argv, allowlist=list(PROOF_ARGV_ALLOWLIST)) is argv


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["eawf", "task", "complete", "EAWF-0001"], "read-only"),
        (["uv", "run", "eawf", "schema", "dump"], "read-only"),
        (["eawf", "doctor", "--fix"], "mutating flag"),
        (["eawf", "doctor", "--yes"], "mutating flag"),
        (["eawf", "release", "tag", "0.7.0"], "--dry-run"),
        (["eawf", "release", "tag", "0.7.0", "--dry-run", "--push"], "mutating flag"),
        (["eawf", "release", "publish", "REL-0.7.0"], "read-only"),
        (["eawf", "bench", "record"], "read-only"),
        (["eawf"], "sub-verb"),
    ],
)
def test_del_035_refuses_a_mutating_eawf_form(argv: list[str], reason: str) -> None:
    with pytest.raises(ArgvPolicyError, match=reason) as caught:
        validate_gate_argv(argv, allowlist=ALLOWLIST)
    assert repr(argv[argv.index("eawf") :]) in str(caught.value)


@pytest.mark.parametrize("argv", [["just", "deploy"], ["just", "--list"], ["just"]])
def test_del_035_refuses_an_undeclared_just_recipe(argv: list[str]) -> None:
    with pytest.raises(ArgvPolicyError, match="just"):
        validate_gate_argv(argv, allowlist=ALLOWLIST)


def test_del_035_read_only_sets_are_disjoint_from_mutating_verbs() -> None:
    assert not {"task", "state", "schema", "config", "daemon"} & EAWF_READ_ONLY_SUBVERBS


def test_del_035_an_inadmissible_head_is_named_with_its_argv_quoted() -> None:
    argv = ["curl", "https://example.invalid"]
    with pytest.raises(ArgvPolicyError) as caught:
        validate_gate_argv(argv, allowlist=ALLOWLIST)
    message = str(caught.value)
    assert "'curl'" in message
    assert repr(argv) in message


def test_del_035_an_inadmissible_head_is_named_at_compilation() -> None:
    check = FloorCheck(
        name="fetch",
        cmd=["curl", "https://example.invalid"],
        scope="all",
        cadence="every-wave",
        policy="block",
    )
    with pytest.raises(FloorPackCompileError) as caught:
        compile_floor_pack([check], allowlist=[])
    assert "'fetch'" in str(caught.value)
    assert repr(check.cmd) in str(caught.value)


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "run", "curl", "https://example.invalid"],
        ["uv", "run", "eawf", "task", "complete", "EAWF-0001"],
        ["uv", "run", "just", "deploy"],
    ],
)
def test_del_035_an_inadmissible_inner_argv_behind_a_wrapper_is_refused(
    argv: list[str],
) -> None:
    with pytest.raises(ArgvPolicyError):
        validate_gate_argv(argv, allowlist=ALLOWLIST)
    with pytest.raises(ValueError, match="L0 policy"):
        _gate(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["eawf", "status", "|", "cat"],
        ["just", "test;", "true"],
        ["uv", "run", "eawf", "why", "$(id)"],
    ],
)
def test_del_035_a_metacharacter_is_refused_whatever_the_head(argv: list[str]) -> None:
    with pytest.raises(ArgvPolicyError, match="shell metacharacter"):
        validate_gate_argv(argv, allowlist=ALLOWLIST)
