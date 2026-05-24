"""CI-fix loop subsystem (B040).

Pure parsers + planning policy that turn a CI log into a follow-up wave
plan. The live PR / CI fetch is *not* part of this module — callers
supply the log as text (typically read from a file path passed on the
CLI). The output is a description of the follow-up wave that would be
planned: id, deps, file-scope union, summary title.

The CLI verbs ``eawf wave fix-ci`` and ``eawf wave fix-ci-loop`` are the
operator-facing surface that exercises this module; see
:mod:`eawf.cli.commands.wave_ci`.

Public API:

- :class:`PytestFailure`, :class:`RuffFailure`, :class:`MypyFailure` —
  parsed-failure records.
- :func:`parse_pytest_failures`, :func:`parse_ruff_failures`,
  :func:`parse_mypy_failures` — pure parsers over the raw log text.
- :func:`failure_to_file_scope` — derive the sorted-unique file-glob
  list used as ``--files`` for the planned follow-up wave.
- :func:`summarise_failures` — emit a short ``kind:count`` summary used
  in the follow-up wave title.
"""

from __future__ import annotations

from eawf.runtime.ci_loop.parser import (
    MypyFailure,
    PytestFailure,
    RuffFailure,
    parse_mypy_failures,
    parse_pytest_failures,
    parse_ruff_failures,
)
from eawf.runtime.ci_loop.policy import failure_to_file_scope, summarise_failures

__all__ = [
    "MypyFailure",
    "PytestFailure",
    "RuffFailure",
    "failure_to_file_scope",
    "parse_mypy_failures",
    "parse_pytest_failures",
    "parse_ruff_failures",
    "summarise_failures",
]
