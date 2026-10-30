"""A successful .NET process must still prove its declared test population."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.workflow.audit_dsl import registry
from eawf.workflow.audit_dsl.models import CheckSpec, CommandExitZeroArgs, DotnetCounts

pytestmark = pytest.mark.unit

COUNTS = {"passed": 31, "failed": 0, "skipped": 0, "total": 31}
SUMMARY = (
    "Passed!  - Failed:     0, Passed:    31, Skipped:     0, Total:    31, "
    "Duration: 1 s - tests.dll (net10.0)"
)


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "No test matches",
        SUMMARY.replace("Passed:    31", "Passed:    30"),
        SUMMARY.replace("Skipped:     0", "Skipped:     1"),
        SUMMARY.replace("Total:    31", "Total:    32"),
        SUMMARY.replace("Failed:     0", "Failed:     1"),
        SUMMARY + "\n" + SUMMARY,
        SUMMARY + "\nPassed! - malformed second summary",
        SUMMARY.replace("Total:", "Total="),
        "Passed! - Failed: 0, Passed: 31, Skipped: 0, Total: 31",
    ],
)
def test_exit_zero_with_missing_malformed_or_wrong_population_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    monkeypatch.setattr(
        registry,
        "_run_in_group",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    )
    result = registry.execute_check(
        CheckSpec(
            kind="command_exit_zero",
            name="G-COUNTS",
            args={
                "argv": ["dotnet", "test", "tests/Example"],
                "scope": "all",
                "dotnet_counts": COUNTS,
            },
        ),
        tmp_path,
    )
    assert result.exit_status == 0
    assert not result.passed
    assert ".NET" in (result.details or "")


def test_exact_summary_passes_and_legacy_exit_zero_stays_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def command(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=SUMMARY, stderr="")

    monkeypatch.setattr(registry, "_run_in_group", command)
    for args in [{"dotnet_counts": COUNTS}, {}]:
        result = registry.execute_check(
            CheckSpec(
                kind="command_exit_zero",
                name="G-COUNTS",
                args={"argv": ["dotnet", "test", "tests/Example"], "scope": "all", **args},
            ),
            tmp_path,
        )
        assert result.passed


@pytest.mark.parametrize(
    "counts",
    [
        {**COUNTS, "total": 32},
        {**COUNTS, "passed": True},
        {**COUNTS, "skipped": -1},
        {**COUNTS, "extra": 1},
        {"passed": 1, "failed": 1, "skipped": 0, "total": 2},
        {"passed": 1, "failed": 0, "skipped": 1, "total": 2},
        {"passed": 0, "failed": 0, "skipped": 2, "total": 2},
        {"passed": 0, "failed": 2, "skipped": 0, "total": 2},
        {"passed": 0, "failed": 0, "skipped": 0, "total": 0},
    ],
)
def test_count_expectations_use_a_closed_consistent_model(counts: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DotnetCounts.model_validate(counts)


def test_counts_cannot_be_attached_to_another_command_family() -> None:
    with pytest.raises(ValidationError, match="dotnet-test"):
        CommandExitZeroArgs(argv=["pytest"], dotnet_counts=DotnetCounts(**COUNTS))
