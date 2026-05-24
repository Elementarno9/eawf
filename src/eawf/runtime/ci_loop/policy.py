"""Policy helpers: parsed failures → follow-up wave plan inputs.

Two pure functions:

- :func:`failure_to_file_scope` produces the sorted-unique list of file
  globs to repair. Used as the ``--files`` arg for the planned
  follow-up wave. Mixing parser kinds is supported — the caller may
  concatenate pytest + ruff + mypy failure lists into one call.
- :func:`summarise_failures` emits a short ``kind:count`` summary like
  ``pytest:3 ruff:1 mypy:0`` used in the follow-up wave title and as
  the "signature" the loop CLI compares across iterations.

Both helpers accept the heterogeneous failure list (mixed kinds) and
dispatch internally by ``isinstance``. This keeps the CLI handler free
of repetitive per-kind plumbing.
"""

from __future__ import annotations

import logging

from eawf.runtime.ci_loop.parser import MypyFailure, PytestFailure, RuffFailure

logger = logging.getLogger(__name__)

# Type alias for the heterogeneous failure list. Declared as a plain
# tuple-union so callers can splat ``parse_pytest_failures(...) +
# parse_ruff_failures(...) + parse_mypy_failures(...)`` into one call.
Failure = PytestFailure | RuffFailure | MypyFailure


def failure_to_file_scope(failures: list[Failure]) -> list[str]:
    """Return the sorted-unique file globs to repair.

    Rules per kind:

    - Pytest failures contribute their ``test_path`` (the path portion
      of the nodeid). That is the test file the follow-up wave should
      touch — fixing a test failure typically lands in either the test
      file or the source it exercises; we surface the test path
      because that is the canonical reference the operator has.
    - Ruff / mypy failures contribute their ``path`` field directly.

    Returns:
        Sorted unique list of file paths (string form). Empty when
        *failures* is empty.
    """
    paths: set[str] = set()
    for f in failures:
        if isinstance(f, PytestFailure):
            paths.add(f.test_path)
        elif isinstance(f, (RuffFailure, MypyFailure)):
            paths.add(f.path)
        # No fallback branch: the parser dataclasses are exhaustive and
        # the type hint forces callers to pass only these three. Adding
        # a new kind would surface as a mypy error here.
    return sorted(paths)


def summarise_failures(failures: list[Failure]) -> str:
    """Return a short ``pytest:N ruff:N mypy:N`` summary.

    The summary string is used as both the follow-up wave title suffix
    and the "signature" the CI-fix loop compares across iterations to
    detect non-convergence (same failure shape on consecutive runs).

    Counts are stable and total over the input list (no dedup): each
    diagnostic counts once even if two ruff failures share a file.
    """
    pytest_count = sum(1 for f in failures if isinstance(f, PytestFailure))
    ruff_count = sum(1 for f in failures if isinstance(f, RuffFailure))
    mypy_count = sum(1 for f in failures if isinstance(f, MypyFailure))
    return f"pytest:{pytest_count} ruff:{ruff_count} mypy:{mypy_count}"


__all__ = [
    "Failure",
    "failure_to_file_scope",
    "summarise_failures",
]
