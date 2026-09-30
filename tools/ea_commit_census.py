#!/usr/bin/env python3
"""Census of the committed surface under ``.ea/``.

Dispatch only: parse the arguments, call
:func:`eawf.kernel.store.commit_census.run_census` -- or, with
``--ancestry``, :func:`~eawf.kernel.store.commit_census.run_ancestry_census`
over a product's default branch and its review checkpoint -- print what it
found, and pick the exit code. The declaration, the comparison and the git
collection all live in the library.

Exit codes: ``0`` when the tree (or history) agrees with the declaration, ``1`` when
it does not, ``2`` when git could not be consulted.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from eawf.kernel.store.commit_census import (
    GitUnavailableError,
    run_ancestry_census,
    run_census,
)
from eawf.kernel.store.commit_policy import AncestryFinding, CensusFinding

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
    parser.add_argument(
        "--ancestry",
        metavar="BRANCH",
        help="judge BRANCH's history against the permanent-commit policy instead",
    )
    parser.add_argument(
        "--checkpoint",
        metavar="REV",
        help="the disposable review checkpoint --ancestry judges the branch against",
    )
    args = parser.parse_args(argv)
    if (args.ancestry is None) != (args.checkpoint is None):
        parser.error("--ancestry and --checkpoint are given together")
    findings: tuple[CensusFinding, ...] | tuple[AncestryFinding, ...]
    try:
        if args.ancestry is None:
            findings = run_census(args.repo_root)
        else:
            findings = run_ancestry_census(
                args.repo_root, branch=args.ancestry, checkpoint=args.checkpoint
            )
    except GitUnavailableError as exc:
        print(f"ea-commit-census: {exc}", file=sys.stderr)
        return GIT_UNAVAILABLE_EXIT
    if not findings:
        subject = ".ea/ commit declaration" if args.ancestry is None else "permanent-commit policy"
        print(f"ea-commit-census: the tree agrees with the {subject}")
        return 0
    print(f"ea-commit-census: {len(findings)} finding(s)", file=sys.stderr)
    for finding in findings:
        print(f"  {finding.render()}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
