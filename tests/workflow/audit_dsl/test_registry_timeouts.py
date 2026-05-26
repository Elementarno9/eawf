"""Unit tests for the W15 hardening of :mod:`eawf.workflow.audit_dsl.registry`.

Covers the three behaviours W15 layered onto ``_check_command_exit_zero``:

* ``timeout_class`` → concrete ``subprocess.run(..., timeout=...)`` budget;
  :class:`subprocess.TimeoutExpired` becomes
  ``CheckResult(status="blocked", passed=False)`` rather than an uncaught
  exception.
* ``scope`` → resolves to ``changed`` / ``touched`` / ``all`` and publishes
  the resolved file set through the ``EAWF_GATE_FILES`` env var (newline-
  separated — POSIX env vars cannot carry NUL bytes). The env var is set
  even when the resolved set is empty so the child can distinguish
  "scope evaluated to empty" from "scope=all".
* ``diff_base`` is derived from
  :func:`eawf.workflow.lifecycle.wave_sha.derive_diff_base` so wave-anchored
  gates compare against the wave's own delta, with a
  ``git merge-base HEAD main`` fallback when the wave SHA cannot be
  resolved.

Also pins the :class:`CommandExitZeroArgs` strict-args schema and the
:data:`CheckStatus` consistency invariant on :class:`CheckResult`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from eawf.workflow.audit_dsl import (
    CHECK_REGISTRY,
    CheckResult,
    CheckSpec,
    CommandExitZeroArgs,
)
from eawf.workflow.audit_dsl.registry import (
    _GATE_FILES_ENV,
    _TIMEOUT_CLASS_SECONDS,
    _resolve_scope_files,
)

# ---- helpers ---------------------------------------------------------------


def _run_command_check(args: dict[str, Any], cwd: Path, *, name: str = "x") -> CheckResult:
    spec = CheckSpec(kind="command_exit_zero", name=name, args=args)
    return CHECK_REGISTRY["command_exit_zero"](spec, cwd.resolve())


def _echo_env_argv(var: str) -> list[str]:
    """Argv that prints the value of *var* to stdout."""
    return [
        sys.executable,
        "-c",
        f"import os; print(os.environ.get({var!r}, ''), end='')",
    ]


# ---- timeout-class constant ------------------------------------------------


def test_timeout_class_seconds_has_four_canonical_keys() -> None:
    assert set(_TIMEOUT_CLASS_SECONDS) == {"quick", "standard", "slow", "very_slow"}
    assert _TIMEOUT_CLASS_SECONDS["quick"] == 60
    assert _TIMEOUT_CLASS_SECONDS["standard"] == 300
    assert _TIMEOUT_CLASS_SECONDS["slow"] == 900
    assert _TIMEOUT_CLASS_SECONDS["very_slow"] == 3600


# ---- CommandExitZeroArgs schema -------------------------------------------


def test_command_exit_zero_args_defaults() -> None:
    args = CommandExitZeroArgs(argv=["true"])
    assert args.timeout_class == "standard"
    assert args.scope == "changed"
    assert args.wave_id is None
    assert args.wave_file_scopes == []


def test_command_exit_zero_args_rejects_empty_argv() -> None:
    with pytest.raises(Exception, match="argv"):
        CommandExitZeroArgs(argv=[])


def test_command_exit_zero_args_rejects_non_str_argv() -> None:
    with pytest.raises(Exception, match="argv"):
        CommandExitZeroArgs(argv=["echo", 1])  # type: ignore[list-item]


def test_command_exit_zero_args_rejects_unknown_kwarg() -> None:
    with pytest.raises(Exception, match="extra"):
        CommandExitZeroArgs(argv=["true"], surprise=1)  # type: ignore[call-arg]


def test_command_exit_zero_args_rejects_bogus_timeout_class() -> None:
    with pytest.raises(Exception, match="timeout_class"):
        CommandExitZeroArgs(argv=["true"], timeout_class="forever")  # type: ignore[arg-type]


def test_command_exit_zero_args_rejects_bogus_scope() -> None:
    with pytest.raises(Exception, match="scope"):
        CommandExitZeroArgs(argv=["true"], scope="universe")  # type: ignore[arg-type]


# ---- CheckResult status invariant -----------------------------------------


def test_check_result_status_synthesised_from_passed_true() -> None:
    res = CheckResult(name="x", kind="command_exit_zero", passed=True)
    assert res.status == "pass"


def test_check_result_status_synthesised_from_passed_false() -> None:
    res = CheckResult(name="x", kind="command_exit_zero", passed=False)
    assert res.status == "fail"


def test_check_result_blocked_status_forces_passed_false() -> None:
    res = CheckResult(name="x", kind="command_exit_zero", passed=False, status="blocked")
    assert res.status == "blocked"
    assert res.passed is False


def test_check_result_pass_status_with_passed_false_rejected() -> None:
    with pytest.raises(Exception, match="inconsistent"):
        CheckResult(name="x", kind="command_exit_zero", passed=False, status="pass")


def test_check_result_blocked_with_passed_true_rejected() -> None:
    with pytest.raises(Exception, match="inconsistent"):
        CheckResult(name="x", kind="command_exit_zero", passed=True, status="blocked")


# ---- timeout-class happy path ---------------------------------------------


def test_command_exit_zero_happy_with_explicit_quick(tmp_path: Path) -> None:
    """A fast no-op under timeout_class='quick' passes and exits cleanly."""
    result = _run_command_check(
        {"argv": [sys.executable, "-c", "pass"], "timeout_class": "quick", "scope": "all"},
        tmp_path,
    )
    assert result.passed is True
    assert result.status == "pass"
    assert "returncode=0" in (result.details or "")


# ---- timeout fires → blocked GateResult -----------------------------------


def test_command_exit_zero_timeout_returns_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When subprocess.run raises TimeoutExpired, the result is blocked.

    The quick budget is normally 60s; monkeypatch it to a sub-second
    value so the deterministic ``sleep`` overshoots quickly.
    """
    monkeypatch.setitem(_TIMEOUT_CLASS_SECONDS, "quick", 1)
    result = _run_command_check(
        {
            "argv": [sys.executable, "-c", "import time; time.sleep(5)"],
            "timeout_class": "quick",
            "scope": "all",
        },
        tmp_path,
    )
    assert result.status == "blocked"
    assert result.passed is False
    assert "timeout" in (result.details or "")
    assert "class='quick'" in (result.details or "")


# ---- scope resolution ------------------------------------------------------


def test_resolve_scope_all_yields_empty(tmp_path: Path) -> None:
    assert (
        _resolve_scope_files(
            scope="all", wave_file_scopes=["a.py"], diff_base="origin/main", cwd=tmp_path
        )
        == []
    )


def test_resolve_scope_changed_uses_changed_files(tmp_path: Path) -> None:
    with patch(
        "eawf.workflow.audit_dsl.registry.changed_files",
        return_value=["src/a.py", "src/b.py"],
    ) as mocked:
        files = _resolve_scope_files(
            scope="changed", wave_file_scopes=[], diff_base="abc~1", cwd=tmp_path
        )
    mocked.assert_called_once_with("abc~1", cwd=tmp_path)
    assert files == ["src/a.py", "src/b.py"]


def test_resolve_scope_touched_unions_wave_scopes(tmp_path: Path) -> None:
    with patch(
        "eawf.workflow.audit_dsl.registry.changed_files",
        return_value=["src/a.py", "src/b.py"],
    ):
        files = _resolve_scope_files(
            scope="touched",
            wave_file_scopes=["src/b.py", "src/c.py"],
            diff_base="abc~1",
            cwd=tmp_path,
        )
    # union of {a, b} and {b, c} sorted
    assert files == ["src/a.py", "src/b.py", "src/c.py"]


def test_resolve_scope_changed_empty_yields_empty(tmp_path: Path) -> None:
    """changed=∅ — touched still includes wave_file_scopes."""
    with patch("eawf.workflow.audit_dsl.registry.changed_files", return_value=[]):
        assert (
            _resolve_scope_files(
                scope="changed", wave_file_scopes=["a.py"], diff_base="x", cwd=tmp_path
            )
            == []
        )
        assert _resolve_scope_files(
            scope="touched", wave_file_scopes=["a.py"], diff_base="x", cwd=tmp_path
        ) == ["a.py"]


# ---- EAWF_GATE_FILES env contract -----------------------------------------


def test_gate_files_env_carries_separated_list(tmp_path: Path) -> None:
    """The child sees the resolved file set via EAWF_GATE_FILES."""
    with patch(
        "eawf.workflow.audit_dsl.registry.changed_files",
        return_value=["alpha.py", "beta.py"],
    ):
        result = _run_command_check(
            {
                "argv": _echo_env_argv(_GATE_FILES_ENV),
                "timeout_class": "quick",
                "scope": "changed",
            },
            tmp_path,
        )
    # The child exits 0 (just prints the env var); the granular split
    # behaviour is exercised by test_gate_files_env_contains_separator
    # below — here we pin the smoke path that the env var is wired.
    assert result.passed is True


def test_gate_files_env_set_even_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EAWF_GATE_FILES is set on the child even when the resolved set is empty.

    Asserts: the var IS in the child env (returncode=0), even when no
    file changed. The child checks "env var present" via a Python
    exit code that distinguishes missing from empty.
    """
    monkeypatch.delenv(_GATE_FILES_ENV, raising=False)
    with patch("eawf.workflow.audit_dsl.registry.changed_files", return_value=[]):
        result = _run_command_check(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    f"import sys, os; sys.exit(0 if {_GATE_FILES_ENV!r} in os.environ else 7)",
                ],
                "timeout_class": "quick",
                "scope": "changed",
            },
            tmp_path,
        )
    assert result.passed is True, result.details


def test_gate_files_env_contains_separator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The child sees paths newline-joined inside the env var.

    The original W15 spec called for NUL-separation but POSIX
    ``execve(2)`` rejects NUL in env-value bytes, so the runner uses
    newline (the registry comment explains the constraint). This test
    pins the actual on-the-wire separator.
    """
    monkeypatch.delenv(_GATE_FILES_ENV, raising=False)
    with patch(
        "eawf.workflow.audit_dsl.registry.changed_files",
        return_value=["a.py", "b.py", "c.py"],
    ):
        result = _run_command_check(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    (
                        "import sys, os;"
                        f" raw = os.environ[{_GATE_FILES_ENV!r}];"
                        " parts = raw.split(chr(10));"
                        " sys.exit(0 if parts == ['a.py', 'b.py', 'c.py'] else 9)"
                    ),
                ],
                "timeout_class": "quick",
                "scope": "changed",
            },
            tmp_path,
        )
    assert result.passed is True, result.details


def test_gate_files_env_empty_string_when_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty resolved set => env var present and equal to the empty string."""
    monkeypatch.delenv(_GATE_FILES_ENV, raising=False)
    with patch("eawf.workflow.audit_dsl.registry.changed_files", return_value=[]):
        result = _run_command_check(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    (
                        "import sys, os;"
                        f" sys.exit(0 if os.environ.get({_GATE_FILES_ENV!r}) == '' else 11)"
                    ),
                ],
                "timeout_class": "quick",
                "scope": "changed",
            },
            tmp_path,
        )
    assert result.passed is True, result.details


# ---- diff-base derivation --------------------------------------------------


def test_diff_base_from_wave_id_uses_derive_wave_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When wave_id resolves, diff_base = derived_sha + '~1'."""
    fake_sha = (
        "deadbeef" + "cafebabe" + "12345678" + "90abcdef" + "12345678"
    )  # pragma: allowlist secret
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None: fake_sha if wid == "P28-I01-W15" else None,
    )
    captured: dict[str, str] = {}

    def _spy(base: str, *, cwd: Any = None) -> list[str]:
        captured["base"] = base
        return []

    with patch("eawf.workflow.audit_dsl.registry.changed_files", side_effect=_spy):
        _run_command_check(
            {
                "argv": [sys.executable, "-c", "pass"],
                "timeout_class": "quick",
                "scope": "changed",
                "wave_id": "P28-I01-W15",
            },
            tmp_path,
        )
    assert captured["base"] == f"{fake_sha}~1"


def test_diff_base_falls_back_to_merge_base_when_wave_sha_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown wave => diff_base falls back to merge-base helper."""
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha", lambda wid, repo_root=None: None
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._git_merge_base_head_main",
        lambda *, repo_root=None, fallback="origin/main": "MERGEBASESHA",
    )
    captured: dict[str, str] = {}

    def _spy(base: str, *, cwd: Any = None) -> list[str]:
        captured["base"] = base
        return []

    with patch("eawf.workflow.audit_dsl.registry.changed_files", side_effect=_spy):
        _run_command_check(
            {
                "argv": [sys.executable, "-c", "pass"],
                "timeout_class": "quick",
                "scope": "changed",
                "wave_id": "P99-I99-W99",  # legitimate-shape but unmatched
            },
            tmp_path,
        )
    assert captured["base"] == "MERGEBASESHA"


def test_diff_base_without_wave_id_uses_merge_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No wave_id => skip derive_wave_sha entirely; merge-base only."""
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._git_merge_base_head_main",
        lambda *, repo_root=None, fallback="origin/main": "JUSTMERGEBASE",
    )
    captured: dict[str, str] = {}

    def _spy(base: str, *, cwd: Any = None) -> list[str]:
        captured["base"] = base
        return []

    with patch("eawf.workflow.audit_dsl.registry.changed_files", side_effect=_spy):
        _run_command_check(
            {"argv": [sys.executable, "-c", "pass"], "timeout_class": "quick", "scope": "changed"},
            tmp_path,
        )
    assert captured["base"] == "JUSTMERGEBASE"


# ---- back-compat ----------------------------------------------------------


def test_command_exit_zero_back_compat_no_new_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-W15 YAML (only ``argv``) still works — defaults pick up the rest."""
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._git_merge_base_head_main",
        lambda *, repo_root=None, fallback="origin/main": "BASE",
    )
    with patch("eawf.workflow.audit_dsl.registry.changed_files", return_value=[]):
        result = _run_command_check(
            {"argv": [sys.executable, "-c", "import sys; sys.exit(0)"]},
            tmp_path,
        )
    assert result.passed is True
    assert result.status == "pass"


# ---- safety net: env propagation does not leak parent's value --------------


def test_gate_files_env_overrides_parent_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the parent already has EAWF_GATE_FILES set, the child sees ours."""
    monkeypatch.setenv(_GATE_FILES_ENV, "PARENTSHOULDNOTLEAK")
    with patch(
        "eawf.workflow.audit_dsl.registry.changed_files",
        return_value=["only_this.py"],
    ):
        result = _run_command_check(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    (
                        "import sys, os;"
                        f" raw = os.environ.get({_GATE_FILES_ENV!r}, '');"
                        " sys.exit(0 if raw == 'only_this.py' else 13)"
                    ),
                ],
                "timeout_class": "quick",
                "scope": "changed",
            },
            tmp_path,
        )
    assert result.passed is True, result.details
    # the parent's value MUST be untouched by the runner; we set it via
    # monkeypatch.setenv and the test isolation guarantees the cleanup.
    assert os.environ[_GATE_FILES_ENV] == "PARENTSHOULDNOTLEAK"
