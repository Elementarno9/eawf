"""Pin the L0 argv-policy + cwd-guard wrap around ``ship._run_gate_command``.

Wave P28-I01-W05 success criterion 3 — every gauntlet gate command must
pass through :func:`eawf.runtime.sandbox.argv_policy.validate_gate_argv`
BEFORE :func:`subprocess.run`. Wave P28-I01-W05 success criterion 4 —
the gate-runner cwd guard must refuse to run outside the repo root.

The single subprocess seam in :mod:`eawf.workflow.skills.ship` is
:func:`_run_gate_command`. These tests monkeypatch the validator + the
cwd guard to assert call order without spawning real subprocesses and
without relying on the canonical default-gate commands resolving on the
test host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.sandbox import argv_policy as _argv_policy_module
from eawf.runtime.sandbox import cwd_guard as _cwd_guard_module
from eawf.runtime.sandbox.argv_policy import ArgvPolicyError
from eawf.runtime.sandbox.cwd_guard import CwdGuardError
from eawf.workflow.skills import ship as ship_module


def test_run_gate_command_invokes_validate_before_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_run_gate_command`` calls ``validate_gate_argv`` before any spawn."""
    calls: list[str] = []

    def _fake_validate(argv: list[str], *, allowlist: list[str]) -> list[str]:
        calls.append("validate")
        return argv

    def _fake_run(*args: object, **kwargs: object) -> object:
        calls.append("subprocess")
        # Mimic a successful run so the gate is reported green.

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(ship_module, "validate_gate_argv", _fake_validate)
    monkeypatch.setattr(ship_module.subprocess, "run", _fake_run)
    result = ship_module._run_gate_command("tests", "uv run pytest", tmp_path)
    assert result.passed is True
    # The validator MUST run before subprocess.run; assert both ran and order.
    assert calls == ["validate", "subprocess"]


def test_run_gate_command_argv_policy_violation_collapses_to_failed_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ArgvPolicyError surfaces as a failed gate, NOT an exception."""
    seen: list[str] = []

    def _fake_validate(argv: list[str], *, allowlist: list[str]) -> list[str]:
        seen.append("validate")
        raise ArgvPolicyError("test injected reject")

    def _fake_run(*args: object, **kwargs: object) -> object:
        seen.append("subprocess")
        raise AssertionError("subprocess.run must NOT be reached on argv-policy reject")

    monkeypatch.setattr(ship_module, "validate_gate_argv", _fake_validate)
    monkeypatch.setattr(ship_module.subprocess, "run", _fake_run)
    result = ship_module._run_gate_command("tests", "uv run pytest", tmp_path)
    assert result.passed is False
    assert result.returncode is None
    assert "policy rejected" in result.output
    assert seen == ["validate"]


def test_run_gate_command_cwd_guard_violation_collapses_to_failed_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A CwdGuardError surfaces as a failed gate, NOT an exception."""
    seen: list[str] = []

    def _fake_validate(argv: list[str], *, allowlist: list[str]) -> list[str]:
        seen.append("validate")
        return argv

    def _fake_assert(cwd: Path, *, root: Path) -> None:
        seen.append("cwd-guard")
        raise CwdGuardError("test injected reject")

    def _fake_run(*args: object, **kwargs: object) -> object:
        seen.append("subprocess")
        raise AssertionError("subprocess.run must NOT be reached on cwd-guard reject")

    monkeypatch.setattr(ship_module, "validate_gate_argv", _fake_validate)
    monkeypatch.setattr(ship_module, "assert_cwd_inside", _fake_assert)
    monkeypatch.setattr(ship_module.subprocess, "run", _fake_run)
    result = ship_module._run_gate_command("tests", "uv run pytest", tmp_path)
    assert result.passed is False
    assert result.returncode is None
    assert "policy rejected" in result.output
    # Both pre-spawn guards ran; subprocess did NOT.
    assert seen == ["validate", "cwd-guard"]


def test_run_gate_command_cwd_guard_rejects_outside_repo_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``repo_root`` outside *cwd* triggers the real cwd guard.

    Exercises the live cwd-guard (no monkeypatch) so the wired path is
    proven end-to-end at the gate-runner boundary. The validator is
    stubbed to a pass so the only reject path under test is the cwd
    guard itself.
    """

    def _fake_validate(argv: list[str], *, allowlist: list[str]) -> list[str]:
        return argv

    def _fake_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("subprocess.run must NOT run when cwd is outside repo root")

    monkeypatch.setattr(ship_module, "validate_gate_argv", _fake_validate)
    monkeypatch.setattr(ship_module.subprocess, "run", _fake_run)
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    result = ship_module._run_gate_command("tests", "uv run pytest", outside, repo_root=root)
    assert result.passed is False
    assert result.returncode is None
    assert "outside repo root" in result.output


def test_run_gate_command_passes_default_allowlist_to_validator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ship gauntlet's canonical allowlist names ``uv`` and the inner gates.

    Asserts the call-time allowlist arg includes the expected heads so a
    future allowlist edit shows up as a test failure, not as a silent
    sandbox loosening.
    """
    captured: dict[str, list[str]] = {}

    def _fake_validate(argv: list[str], *, allowlist: list[str]) -> list[str]:
        captured["argv"] = argv
        captured["allowlist"] = allowlist
        return argv

    def _fake_run(*args: object, **kwargs: object) -> object:
        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(ship_module, "validate_gate_argv", _fake_validate)
    monkeypatch.setattr(ship_module.subprocess, "run", _fake_run)
    ship_module._run_gate_command("tests", "uv run pytest", tmp_path)
    assert captured["argv"] == ["uv", "run", "pytest"]
    # Sentinel heads: wrapper + sub-verb + inner.
    for head in ("uv", "run", "pre-commit", "ruff", "mypy", "pytest", "git"):
        assert head in captured["allowlist"], (
            f"ship gauntlet allowlist missing canonical head {head!r}; "
            f"got {captured['allowlist']!r}"
        )


def test_run_gate_command_live_validator_passes_canonical_uv_run_pytest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: the canonical ``uv run pytest`` argv passes the real validator.

    No stubs on :func:`validate_gate_argv` here — proves the live wiring
    accepts the canonical default-gate command without spawning the real
    pytest subprocess.
    """

    def _fake_run(*args: object, **kwargs: object) -> object:
        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    # Sanity: live modules are wired without test-only replacements.
    assert ship_module.validate_gate_argv is _argv_policy_module.validate_gate_argv
    assert ship_module.assert_cwd_inside is _cwd_guard_module.assert_cwd_inside

    monkeypatch.setattr(ship_module.subprocess, "run", _fake_run)
    result = ship_module._run_gate_command("tests", "uv run pytest", tmp_path)
    assert result.passed is True
