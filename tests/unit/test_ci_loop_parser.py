"""Unit tests for :mod:`eawf.ci_loop.parser`.

The parsers must be tolerant: malformed lines silently skipped, every
matching line surfaced as a dataclass, and the pytest short-summary →
traceback association resolved by bare test name.
"""

from __future__ import annotations

from eawf.ci_loop.parser import (
    parse_mypy_failures,
    parse_pytest_failures,
    parse_ruff_failures,
)

# ---- pytest -----------------------------------------------------------------


def test_parse_pytest_failures_single_failed_line() -> None:
    """A bare ``FAILED ... - reason`` line surfaces nodeid + path + message."""
    log = "FAILED tests/foo.py::test_bar - AssertionError: 1 != 2\n"
    failures = parse_pytest_failures(log)
    assert len(failures) == 1
    f = failures[0]
    assert f.nodeid == "tests/foo.py::test_bar"
    assert f.test_path == "tests/foo.py"
    assert f.message == "AssertionError: 1 != 2"


def test_parse_pytest_failures_collects_traceback_into_message() -> None:
    """``___ test_name ___`` header + traceback block folds into message."""
    log = (
        "=========== FAILURES ===========\n"
        "___________________ test_bar ___________________\n"
        "    def test_bar():\n"
        ">       assert 1 == 2\n"
        "E       assert 1 == 2\n"
        "\n"
        "tests/foo.py:5: AssertionError\n"
        "=========== short test summary info ===========\n"
        "FAILED tests/foo.py::test_bar - AssertionError: 1 != 2\n"
    )
    failures = parse_pytest_failures(log)
    assert len(failures) == 1
    f = failures[0]
    assert f.nodeid == "tests/foo.py::test_bar"
    assert f.test_path == "tests/foo.py"
    # Short-summary tail is joined with the traceback block by a single newline.
    assert f.message.startswith("AssertionError: 1 != 2\n")
    assert "assert 1 == 2" in f.message
    assert "tests/foo.py:5: AssertionError" in f.message


def test_parse_pytest_failures_error_kind_recognised() -> None:
    """``ERROR ... ::`` lines are recognised the same way as FAILED ones."""
    log = "ERROR tests/baz.py::test_qux - ImportError: no module\n"
    failures = parse_pytest_failures(log)
    assert len(failures) == 1
    assert failures[0].nodeid == "tests/baz.py::test_qux"
    assert failures[0].test_path == "tests/baz.py"
    assert failures[0].message == "ImportError: no module"


def test_parse_pytest_failures_empty_log_returns_empty_list() -> None:
    """Boundary: empty input string yields an empty list, not an error."""
    assert parse_pytest_failures("") == []
    assert parse_pytest_failures("\n\n\n") == []
    # Pure banner / noise without any FAILED / ERROR lines.
    assert parse_pytest_failures("============== passed ==============\n") == []


# ---- ruff -------------------------------------------------------------------


def test_parse_ruff_failures_basic_pattern() -> None:
    """``path:line:col: CODE message`` parses to a :class:`RuffFailure`."""
    log = (
        "src/eawf/foo.py:10:1: E501 line too long (120 > 100)\n"
        "src/eawf/bar.py:25:5: F401 'os' imported but unused\n"
    )
    failures = parse_ruff_failures(log)
    assert len(failures) == 2
    assert failures[0].path == "src/eawf/foo.py"
    assert failures[0].line == 10
    assert failures[0].col == 1
    assert failures[0].code == "E501"
    assert "line too long" in failures[0].message
    assert failures[1].path == "src/eawf/bar.py"
    assert failures[1].code == "F401"


def test_parse_ruff_failures_ignores_unrelated_lines() -> None:
    """Banner / summary / blank lines are silently skipped."""
    log = (
        "Found 2 errors.\n"
        "src/eawf/foo.py:10:1: E501 line too long\n"
        "\n"
        "[*] 0 fixable with the --fix option.\n"
        "Some unrelated banner without colons\n"
        "src/eawf/bar.py:1:1: F401 unused import\n"
    )
    failures = parse_ruff_failures(log)
    assert [f.code for f in failures] == ["E501", "F401"]


# ---- mypy -------------------------------------------------------------------


def test_parse_mypy_failures_excludes_note_lines() -> None:
    """``: note:`` lines describe context, not failures — they must be skipped."""
    log = (
        "src/eawf/foo.py:5: error: Incompatible types in assignment\n"
        "src/eawf/foo.py:5: note: Revealed type is 'int'\n"
        "src/eawf/bar.py:12: error: Name 'undefined' is not defined  [name-defined]\n"
        "src/eawf/bar.py:12: note: See PEP 526\n"
        "Found 2 errors in 2 files (checked 7 source files)\n"
    )
    failures = parse_mypy_failures(log)
    assert len(failures) == 2
    assert failures[0].path == "src/eawf/foo.py"
    assert failures[0].line == 5
    assert "Incompatible types" in failures[0].message
    assert failures[1].path == "src/eawf/bar.py"
    assert failures[1].line == 12
    # The error-code tail is preserved verbatim in the message.
    assert "[name-defined]" in failures[1].message
