"""Diff a pytest JUnit report against the committed duration baseline.

``check JUNIT BASELINE`` prints every regression and fast-tier ceiling
breach and exits ``1`` when there is any; ``record JUNIT BASELINE`` writes
the baseline a measured run stands for, stamped with the commit it
measured. The rules live in :mod:`eawf.platform.lint.duration_budget`.

Exit codes: ``0`` within budget (or recorded), ``1`` over budget.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from eawf.platform.lint.duration_budget import (
    budget_findings,
    junit_durations,
    load_baseline,
    record_baseline,
)


def main(argv: list[str]) -> int:
    """Run one ``check`` or ``record`` invocation and return its exit code."""
    parser = argparse.ArgumentParser(prog="duration_budget_gate.py")
    parser.add_argument("mode", choices=("check", "record"))
    parser.add_argument("junit", type=Path)
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args(argv)
    durations = junit_durations(args.junit.read_text(encoding="utf-8"))
    if args.mode == "record":
        revision = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        baseline = record_baseline(durations, revision=revision)
        args.baseline.write_text(baseline.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(f"recorded {len(baseline.durations)} of {len(durations)} tests at {revision}")
        return 0
    findings = budget_findings(durations, load_baseline(args.baseline))
    for finding in findings:
        print(finding.render())
    print(f"{len(findings)} of {len(durations)} tests over budget")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
