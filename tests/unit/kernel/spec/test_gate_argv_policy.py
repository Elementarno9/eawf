"""DEL-035: ``eawf`` and ``just`` are registered command families, not argv heads.

The interim gate argv allowlist that admitted the project's own CLI and task
runner as bare heads is superseded by the ``command_family_ref`` registry: a
gate names the family its argv runs, the family declares its argv shape and
that the shape is read-only, and the allowlist entry is gone. A wrapper
still recurses to the inner command, so an inadmissible inner argv is
refused as before, and a shell metacharacter is refused on its own ground
whatever the head. A refusal at compilation names the head and quotes the
argv it came from.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.spec import promotion
from eawf.kernel.spec.common import GateSpec
from eawf.platform.profiles.models import FloorCheck
from eawf.runtime.sandbox.argv_policy import (
    ArgvPolicyError,
    resolve_command_family,
    validate_family_argv,
    validate_gate_argv,
)
from eawf.runtime.sandbox.command_families import (
    COMMAND_FAMILIES,
    FAMILIES_BY_HEAD,
    REGISTERED_GATE_HEADS,
    CommandFamily,
)
from eawf.workflow.verify.compile import FloorPackCompileError, compile_floor_pack


def _gate(
    argv: list[str], *, family: str | None = None, kind: str = "command_exit_zero"
) -> GateSpec:
    payload: dict[str, object] = {
        "id": "G-01",
        "criterion_id": "CR-01",
        "kind": kind,
        "args": {"argv": argv, "scope": "all", "timeout_class": "quick"},
        "policy": "block",
        "cadence": "every-wave",
    }
    if family is not None:
        payload["command_family_ref"] = family
    return GateSpec.model_validate(payload)


def test_del_035_the_interim_head_allowlist_is_removed() -> None:
    assert not hasattr(promotion, "DEFAULT_GATE_ARGV_ALLOWLIST")


@pytest.mark.parametrize("head", ["eawf", "just"])
def test_del_035_a_project_head_is_a_registered_family(head: str) -> None:
    family = COMMAND_FAMILIES[head]
    assert family.head == head
    assert family.read_only is True
    assert family.verbs


@pytest.mark.parametrize("argv", [["eawf", "status"], ["just", "test"]])
def test_del_035_a_bare_project_head_is_no_longer_admitted_by_head_alone(
    argv: list[str],
) -> None:
    with pytest.raises(ArgvPolicyError, match="not in the caller-supplied allowlist"):
        validate_gate_argv(argv, allowlist=["uv", "pytest", "git"])


@pytest.mark.parametrize(
    ("argv", "family"),
    [
        (["eawf", "status"], "eawf"),
        (["uv", "run", "eawf", "validate"], "eawf"),
        (["uv", "run", "eawf", "doctor", "--json"], "eawf"),
        (["uvx", "eawf", "--version"], "eawf"),
        (["uv", "run", "eawf", "bench", "turn-cost", "--fixture", "live"], "eawf"),
        (["uv", "run", "eawf", "release", "tag", "0.7.0.dev1", "--dry-run"], "eawf"),
        (["just", "test"], "just"),
        (["just", "test-all"], "just"),
        (["just", "test-tui"], "just"),
        (["just", "test", "changed", "origin/main"], "just"),
        (["uv", "run", "pytest", "tests/unit", "-q"], "pytest"),
        (["git", "diff", "--quiet"], "git"),
    ],
)
def test_del_035_a_gate_names_the_family_its_argv_runs(argv: list[str], family: str) -> None:
    assert resolve_command_family(argv).family_id == family
    assert _gate(argv).command_family_ref == family
    assert _gate(argv, family=family).args["argv"] == argv


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
        (["eawf", "release"], "read-only"),
        (["eawf", "bench", "record"], "read-only"),
        (["eawf"], "sub-verb"),
        (["just", "deploy"], "just"),
        (["just", "--list"], "just"),
        (["just"], "just"),
    ],
)
def test_del_035_a_form_outside_the_family_shape_is_refused(argv: list[str], reason: str) -> None:
    with pytest.raises(ArgvPolicyError, match=reason) as caught:
        resolve_command_family(argv)
    head = next(index for index, token in enumerate(argv) if token in {"eawf", "just"})
    assert repr(argv[head:]) in str(caught.value)


def test_del_035_the_eawf_family_writes_no_state() -> None:
    eawf = COMMAND_FAMILIES["eawf"]
    assert eawf.verbs is not None
    assert not {"task", "state", "schema", "config", "daemon"} & eawf.verbs


def test_del_035_an_inadmissible_head_is_named_with_its_argv_quoted() -> None:
    argv = ["curl", "https://example.invalid"]
    with pytest.raises(ArgvPolicyError) as caught:
        resolve_command_family(argv)
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
    assert "'curl'" in str(caught.value)
    assert repr(check.cmd) in str(caught.value)


def test_del_035_compilation_admits_a_registered_family_in_its_shape_only() -> None:
    def check(cmd: list[str]) -> FloorCheck:
        return FloorCheck(name="cli", cmd=cmd, scope="all", cadence="every-wave", policy="block")

    compiled = compile_floor_pack([check(["uv", "run", "eawf", "status"])], allowlist=[])
    assert compiled[0].args["argv"] == ["uv", "run", "eawf", "status"]
    with pytest.raises(FloorPackCompileError, match="read-only"):
        compile_floor_pack([check(["uv", "run", "eawf", "state", "rpc"])], allowlist=[])


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
        resolve_command_family(argv)
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
        resolve_command_family(argv)


@pytest.mark.parametrize("argv", [["uv"], ["uvx"]])
def test_del_035_a_bare_wrapper_runs_no_family(argv: list[str]) -> None:
    with pytest.raises(ArgvPolicyError, match="no registered command family"):
        resolve_command_family(argv)


def test_del_035_a_gate_naming_another_family_is_refused() -> None:
    with pytest.raises(ValidationError, match="names command family 'pytest'"):
        _gate(["uv", "run", "eawf", "status"], family="pytest")


def test_del_035_a_gate_naming_an_unregistered_family_is_refused() -> None:
    with pytest.raises(ValidationError, match="'shell' is not registered"):
        _gate(["uv", "run", "eawf", "status"], family="shell")


def test_del_035_a_gate_naming_a_malformed_family_id_is_refused() -> None:
    with pytest.raises(ValidationError, match="command_family_ref"):
        _gate(["eawf", "status"], family="Not A Family")


def test_del_035_a_gate_without_argv_names_no_family() -> None:
    with pytest.raises(ValidationError, match="names no command family"):
        _gate(["eawf", "status"], family="eawf", kind="regex_match")
    assert _gate(["eawf", "status"], kind="regex_match").command_family_ref is None


def test_del_035_validate_family_argv_refuses_a_missing_ref() -> None:
    with pytest.raises(ArgvPolicyError, match="None is not registered"):
        validate_family_argv(["eawf", "status"], family_ref=None)
    argv = ["eawf", "status"]
    assert validate_family_argv(argv, family_ref="eawf") is argv


@pytest.mark.parametrize("argv", ["eawf status", [], ["eawf", 1]])
def test_del_035_a_mis_typed_argv_resolves_to_no_family(argv: object) -> None:
    with pytest.raises(ArgvPolicyError, match="argv"):
        resolve_command_family(argv)  # type: ignore[arg-type]


def test_del_035_the_registry_is_one_family_per_id_and_head() -> None:
    assert len(FAMILIES_BY_HEAD) == len(COMMAND_FAMILIES)
    assert all(family.family_id == key for key, family in COMMAND_FAMILIES.items())
    assert {"uv", "uvx", *FAMILIES_BY_HEAD} == REGISTERED_GATE_HEADS


def test_del_035_a_family_that_writes_is_not_registrable() -> None:
    with pytest.raises(ValidationError, match="read_only"):
        CommandFamily.model_validate({"family_id": "deploy", "head": "deploy", "read_only": False})
    with pytest.raises(ValidationError, match="extra"):
        CommandFamily.model_validate(
            {"family_id": "deploy", "head": "deploy", "read_only": True, "shell": True}
        )


def test_del_035_a_family_without_verbs_admits_any_arguments() -> None:
    assert COMMAND_FAMILIES["pytest"].shape_violation(["pytest"]) is None
    assert COMMAND_FAMILIES["pytest"].shape_violation(["pytest", "-k", "x"]) is None


def test_del_035_a_preview_verb_is_admitted_only_with_its_flag() -> None:
    ruff = COMMAND_FAMILIES["ruff"]
    assert ruff.shape_violation(["ruff", "format", "--check", "src"]) is None
    assert ruff.shape_violation(["ruff", "format", "src"]) == (
        "ruff format acts unless run as --check"
    )
    assert ruff.shape_violation(["ruff", "check", "--fix"]) is not None


@pytest.mark.parametrize(
    "argv",
    [["pre-commit"], ["pre-commit", "run", "--all-files"], ["ruff"], ["ruff", "--version"]],
)
def test_del_035_a_tool_family_admits_its_reporting_forms(argv: list[str]) -> None:
    assert resolve_command_family(argv).head == argv[0]


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["pre-commit", "install"], "writes state"),
        (["uv", "run", "pre-commit", "autoupdate"], "writes state"),
        (["ruff", "check", "--unsafe-fixes"], "mutating flag"),
    ],
)
def test_del_035_a_tool_family_refuses_its_writing_forms(argv: list[str], reason: str) -> None:
    with pytest.raises(ArgvPolicyError, match=reason):
        resolve_command_family(argv)
