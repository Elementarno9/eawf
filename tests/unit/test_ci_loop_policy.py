"""Unit tests for :mod:`eawf.ci_loop.policy`.

The policy helpers turn parsed failures into the inputs needed by the
follow-up ``plan_wave`` call — a sorted-unique file-scope list and a
short ``kind:count`` signature.
"""

from __future__ import annotations

from eawf.ci_loop.parser import MypyFailure, PytestFailure, RuffFailure
from eawf.ci_loop.policy import failure_to_file_scope, summarise_failures


def test_failure_to_file_scope_dedups_and_sorts() -> None:
    """Mixed-kind input → sorted-unique path list."""
    failures: list[PytestFailure | RuffFailure | MypyFailure] = [
        PytestFailure(
            nodeid="tests/foo.py::test_a",
            test_path="tests/foo.py",
            message="",
        ),
        RuffFailure(path="src/eawf/zzz.py", line=1, col=1, code="E501", message="x"),
        MypyFailure(path="src/eawf/aaa.py", line=2, message="bad"),
        # Duplicate ruff entry for the same path — must dedupe.
        RuffFailure(path="src/eawf/zzz.py", line=99, col=1, code="F401", message="y"),
        # Duplicate pytest failure for the same test_path — must dedupe.
        PytestFailure(
            nodeid="tests/foo.py::test_b",
            test_path="tests/foo.py",
            message="",
        ),
    ]
    scope = failure_to_file_scope(failures)
    assert scope == [
        "src/eawf/aaa.py",
        "src/eawf/zzz.py",
        "tests/foo.py",
    ]


def test_summarise_failures_emits_kind_counts() -> None:
    """Signature follows ``pytest:N ruff:N mypy:N`` order regardless of input."""
    failures: list[PytestFailure | RuffFailure | MypyFailure] = [
        RuffFailure(path="a.py", line=1, col=1, code="E501", message="m"),
        MypyFailure(path="b.py", line=2, message="m"),
        PytestFailure(nodeid="t.py::t", test_path="t.py", message=""),
        PytestFailure(nodeid="t.py::t2", test_path="t.py", message=""),
        PytestFailure(nodeid="t.py::t3", test_path="t.py", message=""),
    ]
    assert summarise_failures(failures) == "pytest:3 ruff:1 mypy:1"
    # Empty input → all-zero signature.
    assert summarise_failures([]) == "pytest:0 ruff:0 mypy:0"
