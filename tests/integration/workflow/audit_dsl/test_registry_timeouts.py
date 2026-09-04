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

import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from eawf.workflow.audit_dsl import (
    CHECK_REGISTRY,
    CheckResult,
    CheckSpec,
    CommandExitZeroArgs,
    GateFreshnessInput,
    run_checks,
)
from eawf.workflow.audit_dsl.registry import (
    _GATE_FILES_ENV,
    _OUTPUT_TAIL_CHARS,
    _TIMEOUT_CLASS_SECONDS,
    _resolve_scope_files,
)

# ---- helpers ---------------------------------------------------------------


def _run_command_check(
    args: dict[str, Any],
    cwd: Path,
    *,
    name: str = "x",
    freshness: GateFreshnessInput | None = None,
) -> CheckResult:
    spec = CheckSpec(kind="command_exit_zero", name=name, args=args, freshness=freshness)
    return CHECK_REGISTRY["command_exit_zero"](spec, cwd.resolve())


def _run_citation_check(args: dict[str, Any], cwd: Path, *, name: str = "cit") -> CheckResult:
    spec = CheckSpec(kind="citation_resolves", name=name, args=args)
    return CHECK_REGISTRY["citation_resolves"](spec, cwd.resolve())


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


# ---- citation_resolves -----------------------------------------------------


def test_citation_resolves_accepts_markdown_reference_rows(tmp_path: Path) -> None:
    artifact = tmp_path / "brief.md"
    artifact.write_text(
        "\n".join(
            [
                "# Brief",
                "",
                "## Summary",
                "",
                "Finding uses source [1].",
                "",
                "## References",
                "",
                "[1] docs/source.md",
                "",
            ]
        ),
        encoding="utf-8",
    )
    result = _run_citation_check({"path": "brief.md"}, tmp_path)
    assert result.passed is True
    assert result.status == "pass"
    assert "citations=1" in (result.details or "")


def test_citation_resolves_rejects_missing_reference_rows(tmp_path: Path) -> None:
    artifact = tmp_path / "brief.md"
    artifact.write_text("Finding uses source [1].\n", encoding="utf-8")
    result = _run_citation_check({"path": "brief.md"}, tmp_path)
    assert result.passed is False
    assert result.status == "fail"
    assert "citation references missing rows: [1]" in (result.details or "")


def test_citation_resolves_accepts_typed_reference_rows(tmp_path: Path) -> None:
    result = _run_citation_check(
        {
            "text": "Typed rows can be passed in args [1].",
            "references": [{"n": 1, "ref": "docs/source.md", "kind": "repo"}],
        },
        tmp_path,
    )
    assert result.passed is True
    assert result.status == "pass"


def test_citation_resolves_rejects_invalid_args(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one of path or text is required"):
        _run_citation_check({"path": "brief.md", "text": "duplicate"}, tmp_path)


# ---- CommandExitZeroArgs schema -------------------------------------------


def test_command_exit_zero_args_defaults() -> None:
    args = CommandExitZeroArgs(argv=["true"])
    assert args.timeout_class == "standard"
    assert args.timeout_s is None
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


@pytest.mark.parametrize("timeout_s", [-1, 0, "5", 1.5, True])
def test_command_exit_zero_args_rejects_malformed_timeout(timeout_s: object) -> None:
    with pytest.raises(Exception, match="timeout_s"):
        CommandExitZeroArgs.model_validate({"argv": ["true"], "timeout_s": timeout_s})


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


def test_check_result_rejects_partial_timing() -> None:
    with pytest.raises(Exception, match="started_at and ended_at"):
        CheckResult(
            name="x",
            kind="command_exit_zero",
            passed=True,
            started_at=datetime.now(UTC),
        )


def test_check_result_rejects_unbounded_output_tail() -> None:
    with pytest.raises(Exception, match="stdout_tail"):
        CheckResult(
            name="x",
            kind="command_exit_zero",
            passed=False,
            stdout_tail="x" * (_OUTPUT_TAIL_CHARS + 1),
        )


def test_gate_freshness_input_rejects_unknown_field() -> None:
    with pytest.raises(Exception, match="extra"):
        GateFreshnessInput.model_validate({"criterion_id": "CR-1", "surprise": "no"})


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


def test_command_exit_zero_claims_freshness_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable caller sees the full freshness key before execution starts."""
    claimed: list[str] = []

    def _claim(spec: CheckSpec, freshness_key: str) -> CheckResult | None:
        assert spec.name == "claimed-gate"
        claimed.append(freshness_key)
        return None

    def _run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert len(claimed) == 1
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    result = run_checks(
        [
            CheckSpec(
                kind="command_exit_zero",
                name="claimed-gate",
                args={"argv": ["gate"], "scope": "all"},
            )
        ],
        cwd=tmp_path,
        before_execute=_claim,
    )[0]

    assert result.status == "pass"
    assert result.freshness_key == claimed[0]


def test_command_exit_zero_reuses_exact_claim_result_without_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal same-key result suppresses execution without lossy mapping."""
    expected = CheckResult(
        name="receipt-hit",
        kind="command_exit_zero",
        passed=False,
        status="blocked",
        details="indeterminate prior execution",
        freshness_key="a" * 64,
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("subprocess must not run on receipt hit"),
    )

    actual = run_checks(
        [
            CheckSpec(
                kind="command_exit_zero",
                name="receipt-hit",
                args={"argv": ["gate"], "scope": "all"},
            )
        ],
        cwd=tmp_path,
        before_execute=lambda spec, freshness_key: expected,
    )[0]

    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")


def test_command_exit_zero_explicit_timeout_wins_over_class(
    tmp_path: Path,
) -> None:
    """Top-level timeout propagated into args overrides timeout-class budget."""
    completed = subprocess.CompletedProcess(
        args=["uv", "run", "pytest"],
        returncode=0,
        stdout="",
        stderr="",
    )
    with patch(
        "eawf.workflow.audit_dsl.registry.subprocess.run",
        return_value=completed,
    ) as mocked:
        result = _run_command_check(
            {
                "argv": ["uv", "run", "pytest"],
                "timeout_class": "very_slow",
                "timeout_s": 451,
                "scope": "all",
            },
            tmp_path,
            name="GATE-W51",
        )

    assert mocked.call_args.kwargs["timeout"] == 451
    assert result.timeout_class == "very_slow"
    assert result.resolved_timeout_seconds == 451


def test_command_exit_zero_marks_unavailable_fingerprints_as_none(
    tmp_path: Path,
) -> None:
    completed = subprocess.CompletedProcess(
        args=["uv", "run", "pytest"],
        returncode=0,
        stdout="",
        stderr="",
    )
    with (
        patch("eawf.workflow.audit_dsl.registry._runner_fingerprint", return_value=None),
        patch("eawf.workflow.audit_dsl.registry._environment_fingerprint", return_value=None),
        patch(
            "eawf.workflow.audit_dsl.registry.subprocess.run",
            return_value=completed,
        ),
    ):
        result = _run_command_check(
            {"argv": ["uv", "run", "pytest"], "scope": "all"},
            tmp_path,
        )

    assert result.runner_fingerprint is None
    assert result.environment_fingerprint is None


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


def test_command_exit_zero_timeout_preserves_captured_output(
    tmp_path: Path,
) -> None:
    """Timeout result retains bounded diagnostics and explicit timeout facts."""
    error = subprocess.TimeoutExpired(
        cmd=["uv", "run", "pytest"],
        timeout=2,
        output="collection reached sentinel-out",
        stderr="worker stalled sentinel-err",
    )
    freshness = GateFreshnessInput(full_log_ref="artifact:gate/GATE-timeout")
    with patch(
        "eawf.workflow.audit_dsl.registry.subprocess.run",
        side_effect=error,
    ):
        result = _run_command_check(
            {
                "argv": ["uv", "run", "pytest"],
                "timeout_class": "very_slow",
                "timeout_s": 2,
                "scope": "all",
            },
            tmp_path,
            name="GATE-timeout",
            freshness=freshness,
        )

    assert result.status == "blocked"
    assert result.exit_status is None
    assert result.resolved_timeout_seconds == 2
    assert result.stdout_tail == "collection reached sentinel-out"
    assert result.stderr_tail == "worker stalled sentinel-err"
    assert "sentinel-out" in (result.details or "")
    assert "sentinel-err" in (result.details or "")
    assert "artifact:gate/GATE-timeout" in (result.details or "")
    full_log = (tmp_path / "artifact:gate" / "GATE-timeout").read_text(encoding="utf-8")
    assert "collection reached sentinel-out" in full_log
    assert "worker stalled sentinel-err" in full_log


def test_command_exit_zero_w51_output_result_is_bounded_and_digest_complete(
    tmp_path: Path,
) -> None:
    """Long failing gate preserves tail, full-output digests, and receipt inputs."""
    stdout = f"dropped-prefix-stdout-{'o' * _OUTPUT_TAIL_CHARS}stdout-sentinel"
    stderr = f"dropped-prefix-stderr-{'e' * _OUTPUT_TAIL_CHARS}stderr-sentinel"
    completed = subprocess.CompletedProcess(
        args=["uv", "run", "pytest"],
        returncode=27,
        stdout=stdout,
        stderr=stderr,
    )
    freshness = GateFreshnessInput(
        scope_id="P02-I25-W51",
        criterion_id="CR-W51",
        integration_id="INT-W51-2",
        integrated_commit="a" * 40,
        tree_digest="tree-v2",
        contract_digest="contract-v2",
        policy_digest="policy-v2",
        runner_fingerprint="runner-v2",
        environment_fingerprint="environment-v2",
        collected_nodeid_digest="collection-v2",
        residual_manifest_digest="residual-v2",
        full_log_ref="artifact:gate/GATE-W51",
    )
    with patch(
        "eawf.workflow.audit_dsl.registry.subprocess.run",
        return_value=completed,
    ):
        result = _run_command_check(
            {
                "argv": ["uv", "run", "pytest"],
                "timeout_class": "quick",
                "timeout_s": 461,
                "scope": "all",
            },
            tmp_path,
            name="GATE-W51",
            freshness=freshness,
        )

    assert result.status == "fail"
    assert result.exit_status == 27
    assert result.started_at is not None
    assert result.ended_at is not None
    assert result.ended_at >= result.started_at
    assert result.duration_ms is not None and result.duration_ms >= 0
    assert len(result.stdout_tail or "") == _OUTPUT_TAIL_CHARS
    assert len(result.stderr_tail or "") == _OUTPUT_TAIL_CHARS
    assert (result.stdout_tail or "").endswith("stdout-sentinel")
    assert (result.stderr_tail or "").endswith("stderr-sentinel")
    assert "dropped-prefix-stdout" not in (result.stdout_tail or "")
    assert "dropped-prefix-stderr" not in (result.stderr_tail or "")
    assert result.stdout_digest == hashlib.sha256(stdout.encode()).hexdigest()
    assert result.stderr_digest == hashlib.sha256(stderr.encode()).hexdigest()
    assert result.argv == ["uv", "run", "pytest"]
    assert result.command == "uv run pytest"
    assert result.selected_file_digest is not None
    assert result.collected_nodeid_digest == "collection-v2"
    assert result.residual_manifest_digest == "residual-v2"
    assert result.runner_fingerprint == "runner-v2"
    assert result.environment_fingerprint == "environment-v2"
    assert result.full_log_ref == "artifact:gate/GATE-W51"
    assert result.freshness == freshness
    assert result.freshness_key is not None and len(result.freshness_key) == 64
    assert "stdout-sentinel" in (result.details or "")
    assert "stderr-sentinel" in (result.details or "")
    assert "artifact:gate/GATE-W51" in (result.details or "")
    full_log = (tmp_path / "artifact:gate" / "GATE-W51").read_text(encoding="utf-8")
    assert "dropped-prefix-stdout" in full_log
    assert "dropped-prefix-stderr" in full_log


def test_command_exit_zero_digests_declared_collection_and_residual_manifests(
    tmp_path: Path,
) -> None:
    """Declared gate artifacts become exact receipt digests."""
    collected = tmp_path / "artifacts" / "collected.txt"
    residual = tmp_path / "artifacts" / "residual.json"
    collected.parent.mkdir()
    collected.write_text("test_a\ntest_b\n", encoding="utf-8")
    residual.write_text('{"owner": "W37"}\n', encoding="utf-8")
    completed = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=0,
        stdout="2 passed",
        stderr="",
    )

    with patch(
        "eawf.workflow.audit_dsl.registry.subprocess.run",
        return_value=completed,
    ):
        result = _run_command_check(
            {
                "argv": ["pytest"],
                "scope": "all",
                "collected_nodeids_path": "artifacts/collected.txt",
                "collected_nodeids_expected_digest": hashlib.sha256(
                    collected.read_bytes()
                ).hexdigest(),
                "residual_manifest_path": "artifacts/residual.json",
                "residual_manifest_expected_digest": hashlib.sha256(
                    residual.read_bytes()
                ).hexdigest(),
            },
            tmp_path,
        )

    assert result.status == "pass"
    assert result.collected_nodeid_digest == hashlib.sha256(collected.read_bytes()).hexdigest()
    assert result.residual_manifest_digest == hashlib.sha256(residual.read_bytes()).hexdigest()


def test_command_exit_zero_fails_when_declared_manifest_is_missing(
    tmp_path: Path,
) -> None:
    """Deleted or unproduced declared manifests cannot yield a pass receipt."""
    completed = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=0,
        stdout="2 passed",
        stderr="",
    )

    with patch(
        "eawf.workflow.audit_dsl.registry.subprocess.run",
        return_value=completed,
    ):
        result = _run_command_check(
            {
                "argv": ["pytest"],
                "scope": "all",
                "collected_nodeids_path": "artifacts/missing.txt",
                "collected_nodeids_expected_digest": "0" * 64,
            },
            tmp_path,
        )

    assert result.status == "fail"
    assert "not found after gate execution" in (result.details or "")


def test_command_exit_zero_rejects_manifest_path_traversal() -> None:
    with pytest.raises(ValueError, match="repo-relative"):
        CommandExitZeroArgs(
            argv=["pytest"],
            residual_manifest_path="../outside.json",
            residual_manifest_expected_digest="0" * 64,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"collected_nodeids_path": "artifacts/collected.txt"},
        {"collected_nodeids_expected_digest": "0" * 64},
        {"residual_manifest_path": "artifacts/residual.json"},
        {"residual_manifest_expected_digest": "0" * 64},
    ],
)
def test_command_exit_zero_requires_manifest_path_digest_pairs(
    kwargs: dict[str, str],
) -> None:
    """Manifest comparisons cannot silently degrade to existence checks."""
    with pytest.raises(ValueError, match="must be provided together"):
        CommandExitZeroArgs(argv=["pytest"], **kwargs)


def test_command_exit_zero_fails_manifest_digest_mismatch(
    tmp_path: Path,
) -> None:
    """Passing command still fails when produced manifest differs from baseline."""
    manifest = tmp_path / "artifacts" / "residual.json"
    manifest.parent.mkdir()
    manifest.write_text('{"owner": "new"}\n', encoding="utf-8")
    completed = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=0,
        stdout="2 passed",
        stderr="",
    )

    with patch(
        "eawf.workflow.audit_dsl.registry.subprocess.run",
        return_value=completed,
    ):
        result = _run_command_check(
            {
                "argv": ["pytest"],
                "scope": "all",
                "residual_manifest_path": "artifacts/residual.json",
                "residual_manifest_expected_digest": "0" * 64,
            },
            tmp_path,
        )

    assert result.status == "fail"
    assert "digest mismatch" in (result.details or "")
    assert "expected=" in (result.details or "")
    assert "actual=" in (result.details or "")


def test_manifest_expected_digest_changes_pre_execution_freshness_key(
    tmp_path: Path,
) -> None:
    """Baseline changes cannot reuse a claim before the command starts."""
    keys: list[str] = []

    def _capture(spec: CheckSpec, freshness_key: str) -> CheckResult:
        keys.append(freshness_key)
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="blocked",
            details="captured before execution",
            freshness_key=freshness_key,
        )

    base = {
        "argv": ["pytest"],
        "scope": "all",
        "residual_manifest_path": "artifacts/residual.json",
    }
    for expected in ("0" * 64, "1" * 64):
        run_checks(
            [
                CheckSpec(
                    kind="command_exit_zero",
                    name="G-MANIFEST",
                    args={
                        **base,
                        "residual_manifest_expected_digest": expected,
                    },
                )
            ],
            cwd=tmp_path,
            before_execute=_capture,
        )

    assert len(keys) == 2
    assert keys[0] != keys[1]


@pytest.mark.parametrize(
    "field",
    ["dependency_binding_digest", "runner_environment_digest"],
)
def test_full_frozen_freshness_fact_changes_non_command_key(
    tmp_path: Path,
    field: str,
) -> None:
    """Dependency and runner-composite drift cannot reuse any gate kind."""
    keys: list[str] = []

    def _capture(spec: CheckSpec, freshness_key: str) -> CheckResult:
        keys.append(freshness_key)
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="blocked",
            details="captured before execution",
            freshness_key=freshness_key,
        )

    base = GateFreshnessInput(
        integration_id="INT-01",
        contract_digest="2" * 64,
        criteria_digest="3" * 64,
        gate_manifest_digest="4" * 64,
        policy_digest="5" * 64,
        dependency_binding_digest="6" * 64,
        runner_environment_digest="7" * 64,
    )
    for value in ("8" * 64, "9" * 64):
        freshness = base.model_copy(update={field: value})
        run_checks(
            [
                CheckSpec(
                    kind="file_exists",
                    name="G-FILE",
                    args={"path": "sentinel"},
                    freshness=freshness,
                )
            ],
            cwd=tmp_path,
            before_execute=_capture,
        )

    assert len(keys) == 2
    assert keys[0] != keys[1]


def test_run_checks_executes_duplicate_gate_once_per_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact execution identity is evaluated once and reused in output order."""
    calls = 0

    def _counted(spec: CheckSpec, cwd: Path) -> CheckResult:
        nonlocal calls
        calls += 1
        return CheckResult(name=spec.name, kind=spec.kind, passed=True)

    monkeypatch.setitem(CHECK_REGISTRY, "file_exists", _counted)
    spec = CheckSpec(
        kind="file_exists",
        name="GATE-once",
        args={"path": "sentinel"},
        freshness=GateFreshnessInput(
            scope_id="P02-I25-W51",
            criterion_id="CR-W51",
            integration_id="INT-W51-2",
        ),
    )

    results = run_checks([spec, spec], cwd=tmp_path)

    assert calls == 1
    assert len(results) == 2
    assert results[0] is results[1]


def test_run_checks_does_not_coalesce_distinct_criterion_freshness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same gate name/argv under distinct criterion bindings executes twice."""
    calls = 0

    def _counted(spec: CheckSpec, cwd: Path) -> CheckResult:
        nonlocal calls
        calls += 1
        return CheckResult(name=spec.name, kind=spec.kind, passed=True)

    monkeypatch.setitem(CHECK_REGISTRY, "file_exists", _counted)
    base = {
        "kind": "file_exists",
        "name": "GATE-shared-name",
        "args": {"path": "sentinel"},
    }
    first = CheckSpec(
        **base,
        freshness=GateFreshnessInput(criterion_id="CR-1", integration_id="INT-1"),
    )
    second = CheckSpec(
        **base,
        freshness=GateFreshnessInput(criterion_id="CR-2", integration_id="INT-1"),
    )

    run_checks([first, second], cwd=tmp_path)

    assert calls == 2


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
