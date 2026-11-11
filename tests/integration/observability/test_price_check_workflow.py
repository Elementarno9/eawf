"""The weekly price-check workflow tells a finished check from a crash.

A crash of the check exits 1, the same status as a difference, so the
workflow's check step reads the report as well as the status. These tests run
that step's script under bash, with a stand-in ``uv`` that plays the check.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "price-check.yml"


def _check_step() -> dict[str, Any]:
    document = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = document["jobs"]["price-check"]["steps"]
    return next(step for step in steps if step.get("id") == "check")


def _run_check_step(tmp_path: Path, fake_uv: str) -> tuple[dict[str, str], str]:
    """Run the workflow's check step with *fake_uv* standing in for ``uv``."""
    bash = shutil.which("bash")
    assert bash is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(f"#!{bash}\n{fake_uv}\n", encoding="utf-8")
    uv.chmod(0o755)
    summary, output = tmp_path / "summary.md", tmp_path / "output.txt"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_OUTPUT": str(output),
    }
    # The runner's bash shell runs a step with errexit and pipefail.
    subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", _check_step()["run"]],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    pairs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    return pairs, summary.read_text(encoding="utf-8")


def test_price_check_repro_crash_reads_as_a_difference(tmp_path: Path) -> None:
    outputs, summary = _run_check_step(
        tmp_path, "echo 'Traceback (most recent call last):' >&2\necho 'KeyError: x' >&2\nexit 1"
    )

    assert outputs["outcome"] == "crashed"
    assert summary.startswith("Prices: the price check crashed (exit 1)")
    assert "KeyError: x" in summary
    assert (tmp_path / "price-check.md").read_text(encoding="utf-8") == summary


@pytest.mark.parametrize(
    ("status", "outcome"), [(0, "current"), (1, "differs"), (2, "unchecked"), (3, "crashed")]
)
def test_workflow_names_each_outcome(tmp_path: Path, status: int, outcome: str) -> None:
    outputs, summary = _run_check_step(tmp_path, f"echo 'Prices: report'\nexit {status}")

    assert outputs == {"status": str(status), "outcome": outcome}
    assert summary.startswith("Prices: ")


def test_workflow_calls_an_empty_report_a_crash(tmp_path: Path) -> None:
    outputs, _ = _run_check_step(tmp_path, "exit 0")

    assert outputs["outcome"] == "crashed"
