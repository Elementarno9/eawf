"""Diff a pytest JUnit report against the committed duration baseline.

``check JUNIT BASELINE`` prints every regression and fast-tier ceiling
breach and exits ``1`` when there is any; ``record JUNIT BASELINE`` writes
the baseline a measured run stands for, stamped with the commit it
measured; ``split BASELINE OUT`` writes the baseline as the node-id keyed
durations file ``pytest-split`` reads, so the CI shards balance by
measured time. The rules live in :mod:`eawf.platform.lint.duration_budget`.

Refresh the baseline from a CI run by downloading the ``pytest-junit-*``
artifact of the unsharded leg and running, from the repository root::

    uv run python tools/duration_budget_gate.py record pytest-junit.xml tests/duration-baseline.json

Exit codes: ``0`` within budget (or recorded, or split), ``1`` over budget.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from eawf.observability.logging.state_leak import HOME_PATH_PATTERNS
from eawf.platform.lint.duration_budget import (
    budget_findings,
    junit_durations,
    load_baseline,
    record_baseline,
)


def node_ids(durations: Mapping[str, float], root: Path) -> dict[str, float]:
    """Return *durations* re-keyed from JUnit keys to pytest node ids.

    A JUnit classname joins the module path and any test classes with dots,
    so the module is the longest dotted prefix naming a ``.py`` file under
    *root*; the rest are classes. A key whose module no longer exists names
    a deleted test and is dropped, since no shard can collect it.

    Args:
        durations: Seconds per ``<classname>::<name>`` key.
        root: The pytest rootdir node ids are relative to.

    Returns:
        Seconds per ``<module path>::[<class>::]<name>`` node id.

    Raises:
        ValueError: A key carries no ``::`` separator.
    """
    modules: dict[str, tuple[str, list[str]] | None] = {}
    split: dict[str, float] = {}
    for key, seconds in durations.items():
        classname, separator, name = key.partition("::")
        if not separator:
            raise ValueError(f"not a JUnit test key: {key!r}")
        if classname not in modules:
            modules[classname] = _module_of(classname, root)
        found = modules[classname]
        if found is not None:
            module, classes = found
            split["::".join([module, *classes, name])] = seconds
    return split


def committable(durations: Mapping[str, float]) -> dict[str, float]:
    """Return *durations* without the tests whose key names a home path.

    A leak-scrubber fixture is parametrized with sample home paths, so its
    JUnit key carries one, and the path-leak gate refuses the committed
    baseline line that repeats it. Dropping the key costs nothing: the gate
    judges an unrecorded test against the floor and the shard splitter
    weighs it at the average.

    Args:
        durations: Seconds per JUnit test key.

    Returns:
        The same mapping minus every key a home-path pattern matches.
    """
    return {
        key: seconds
        for key, seconds in durations.items()
        if not any(pattern.search(key) for pattern in HOME_PATH_PATTERNS)
    }


def _module_of(classname: str, root: Path) -> tuple[str, list[str]] | None:
    parts = classname.split(".")
    for cut in range(len(parts), 0, -1):
        module = f"{'/'.join(parts[:cut])}.py"
        if (root / module).is_file():
            return module, parts[cut:]
    return None


def main(argv: list[str]) -> int:
    """Run one ``check``, ``record`` or ``split`` invocation and return its exit code."""
    parser = argparse.ArgumentParser(prog="duration_budget_gate.py")
    modes = parser.add_subparsers(dest="mode", required=True)
    for mode in ("check", "record"):
        measured = modes.add_parser(mode)
        measured.add_argument("junit", type=Path)
        measured.add_argument("baseline", type=Path)
    splitter = modes.add_parser("split")
    splitter.add_argument("baseline", type=Path)
    splitter.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "split":
        split = node_ids(load_baseline(args.baseline).durations, Path.cwd())
        args.out.write_text(json.dumps(split, indent=0, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {len(split)} node-id durations to {args.out}")
        return 0
    durations = junit_durations(args.junit.read_text(encoding="utf-8"))
    if args.mode == "record":
        revision = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        baseline = record_baseline(committable(durations), revision=revision)
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
