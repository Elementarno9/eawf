"""Native dotnet test gate resolution and restricted argument forms."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import GateSpec
from eawf.platform.profiles.models import FloorCheck
from eawf.runtime.sandbox.argv_policy import (
    ArgvPolicyError,
    resolve_command_family,
    validate_family_argv,
    validate_gate_argv,
)
from eawf.workflow.verify.compile import compile_floor_pack


@pytest.mark.parametrize(
    "target",
    [
        "tests/CloudPirates.ShardServer.Tests",
        "tests/CloudPirates.ShardServer.Tests/CloudPirates.ShardServer.Tests.csproj",
    ],
)
def test_resolve_command_family_accepts_dotnet_test_gate(target: str) -> None:
    argv = [
        "dotnet",
        "test",
        target,
        "--filter",
        "FullyQualifiedName~ShellHeat",
        "--no-restore",
        "-m:1",
    ]
    assert resolve_command_family(argv).family_id == "dotnet-test"
    assert validate_family_argv(argv, family_ref="dotnet-test") is argv
    check = FloorCheck(
        name="shell-heat", cmd=argv, scope="all", cadence="every-task", policy="block"
    )
    compiled = compile_floor_pack([check], allowlist=[])[0]
    gate = GateSpec.model_validate(
        {
            "id": "G-01",
            "criterion_id": "CR-01",
            "kind": "command_exit_zero",
            "args": {"argv": argv, "scope": "all", "timeout_class": "quick"},
            "policy": "block",
            "cadence": "every-task",
        }
    )
    assert gate.command_family_ref == "dotnet-test"
    assert GateSpec.model_validate(gate.model_dump()).args["argv"] == argv
    assert compiled.args["argv"] == argv
    with pytest.raises(ArgvPolicyError, match="family"):
        validate_family_argv(argv, family_ref="pytest")


@pytest.mark.parametrize("argv", [["dotnet", "test", "tests"], ["dotnet", "test", "Tests.csproj"]])
def test_validate_gate_argv_accepts_minimal_dotnet_test(argv: list[str]) -> None:
    assert validate_gate_argv(argv, allowlist=["dotnet"]) is argv


@pytest.mark.parametrize(
    "verb", ["run", "new", "tool", "install", "publish", "build", "restore", "msbuild", "vstest"]
)
def test_resolve_command_family_rejects_dotnet_other_verbs(verb: str) -> None:
    with pytest.raises(ArgvPolicyError, match="read-only allow set"):
        resolve_command_family(["dotnet", verb, "tests"])


@pytest.mark.parametrize(
    "tail",
    [
        [],
        ["../tests"],
        ["/tmp/tests"],
        ["tests/../other"],
        ["@args.rsp"],
        ["--help"],
        ["tests", "run"],
        ["tests", "--"],
        ["tests", "-t:Publish"],
        ["tests", "/t:Install"],
        ["tests", "--property:Target=Publish"],
        ["tests", "-p:CustomAfterMicrosoftCommonTargets=evil.targets"],
        ["tests", "--artifacts-path", "other"],
        ["tests", "--settings", "custom.runsettings"],
        ["tests", "--logger", "custom"],
        ["tests", "--filter"],
        ["tests", "--filter", ""],
        ["tests", "--filter", "--no-restore"],
        ["tests", "--filter", "@args.rsp"],
        ["tests", "--no-restore=false"],
        ["tests", "-m:4"],
        ["tests", "--filter=FullyQualifiedName~ShellHeat"],
        ["tests", "--no-restore", "--filter", "FullyQualifiedName~ShellHeat", "--blame-crash"],
    ],
)
def test_validate_gate_argv_rejects_dotnet_unsafe_arguments(tail: list[str]) -> None:
    argv = ["dotnet", "test", *tail]
    with pytest.raises(ArgvPolicyError, match="dotnet test"):
        validate_gate_argv(argv, allowlist=["dotnet"])
    with pytest.raises(ValidationError, match="L0 policy"):
        GateSpec.model_validate(
            {
                "id": "G-01",
                "criterion_id": "CR-01",
                "kind": "command_exit_zero",
                "args": {"argv": argv, "scope": "all", "timeout_class": "quick"},
                "policy": "block",
                "cadence": "every-task",
                "command_family_ref": "dotnet-test",
            }
        )


def test_resolve_command_family_rejects_dotnet_global_options() -> None:
    with pytest.raises(ArgvPolicyError, match="read-only allow set"):
        resolve_command_family(["dotnet", "--additionalprobingpath", "other", "test", "tests"])
