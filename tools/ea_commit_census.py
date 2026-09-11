#!/usr/bin/env python3
"""Census of the committed surface under ``.ea/``.

Dispatch only: parse the arguments, call
:func:`eawf.kernel.store.commit_census.run_census`, print what it found,
and pick the exit code. The declaration, the comparison and the git
collection all live in the library.

Exit codes: ``0`` when the tree agrees with the declaration, ``1`` when
it does not, ``2`` when git could not be consulted.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from eawf.kernel.store.commit_census import GitUnavailableError, run_census

GIT_UNAVAILABLE_EXIT = 2


def main(argv: list[str] | None = None) -> int:
    """Run the census and report.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=".ea/ commit-policy census")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="repository to census (default: the current directory)",
    )
    args = parser.parse_args(argv)
    try:
        findings = run_census(args.repo_root)
    except GitUnavailableError as exc:
        print(f"ea-commit-census: {exc}", file=sys.stderr)
        return GIT_UNAVAILABLE_EXIT
    if not findings:
        print("ea-commit-census: the tree agrees with the .ea/ commit declaration")
        return 0
    print(f"ea-commit-census: {len(findings)} finding(s)", file=sys.stderr)
    for finding in findings:
        print(f"  {finding.render()}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
