"""Pure parsers over raw CI log text.

Each parser scans the input line-by-line and surfaces structured
failure records. The parsers never raise on malformed lines — they
silently skip anything that does not match. The intent is "find what
you can; let the policy module decide what to do with it" rather than
"refuse to plan a follow-up because the log had stray banner text".

The three patterns covered by this wave:

- **pytest** — short-summary lines (``FAILED tests/foo.py::test_bar -
  ...``, ``ERROR tests/baz.py::test_qux ...``) plus the per-test
  ``___ test_name ___`` traceback block. The short-summary line is the
  primary record; the traceback block is folded into the record's
  ``message`` field when a matching header is found.
- **ruff** — the ``path:line:col: CODE message`` form emitted by both
  ``ruff check`` and the legacy ``ruff format --check`` output.
- **mypy** — the ``path:line: error: ...`` form. ``: note:`` lines are
  intentionally excluded since they describe context, not a failure.

All three return lightweight dataclasses so callers can read attributes
without juggling tuple positions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---- Dataclasses ------------------------------------------------------------


@dataclass(frozen=True)
class PytestFailure:
    """One pytest short-summary entry plus its (best-effort) traceback.

    Attributes:
        nodeid: Full pytest node id, e.g. ``tests/foo.py::test_bar``.
        test_path: Path portion of *nodeid* (everything before ``::``).
            Used by the policy layer as the file-scope entry.
        message: One-line short-summary tail (``AssertionError: ...``)
            joined with any matching ``___ test_name ___`` traceback
            block. May be the empty string when neither was captured.
    """

    nodeid: str
    test_path: str
    message: str


@dataclass(frozen=True)
class RuffFailure:
    """One ruff diagnostic.

    Attributes:
        path: File path that triggered the diagnostic.
        line: 1-based line number.
        col: 1-based column number.
        code: Ruff code (e.g. ``E501``, ``F401``).
        message: Diagnostic text.
    """

    path: str
    line: int
    col: int
    code: str
    message: str


@dataclass(frozen=True)
class MypyFailure:
    """One mypy ``error:`` diagnostic (``note:`` lines are excluded).

    Attributes:
        path: File path.
        line: 1-based line number.
        message: Error message text.
    """

    path: str
    line: int
    message: str


# ---- Pytest -----------------------------------------------------------------

# Short-summary patterns. Pytest emits one line per failed/errored test
# under the ``=== short test summary info ===`` section. We accept both
# the ``- <reason>`` long form and bare lines without a reason tail.
_RE_PYTEST_FAILED = re.compile(
    r"^(?P<kind>FAILED|ERROR)\s+(?P<nodeid>\S+::\S+)(?:\s+-\s+(?P<message>.+))?$"
)

# Per-test header inside the verbose section: ``___ test_name ___`` (3+
# underscores either side). The header gives us the test *name* (last
# segment of the nodeid); we match by suffix to attach the trailing
# traceback block as the failure message.
_RE_PYTEST_TEST_HEADER = re.compile(r"^_{3,}\s+(?P<name>\S+)\s+_{3,}\s*$")

# Section markers that signal the end of a traceback block. Anything
# matching one of these resets the "currently collecting" state.
_RE_PYTEST_SECTION_END = re.compile(
    r"^(?:=+|_+)\s*(short test summary info|FAILURES|ERRORS|warnings|passed|failed|error)"
)


def parse_pytest_failures(log_text: str) -> list[PytestFailure]:
    """Parse pytest ``FAILED``/``ERROR`` short-summary entries.

    Returns:
        One :class:`PytestFailure` per matching short-summary line, in
        encounter order. The ``message`` field is the short-summary
        tail joined with any matching ``___ test_name ___`` block found
        earlier in the log; if both are present they are joined with a
        single newline.
    """
    lines = log_text.splitlines()

    # First pass: collect traceback blocks keyed by the bare test name.
    # The header ``___ test_name ___`` precedes a block of traceback
    # text that ends at the next section marker (another header or a
    # ``=== ... ===`` boundary).
    traceback_blocks: dict[str, str] = {}
    current_name: str | None = None
    current_block: list[str] = []
    for raw in lines:
        header_match = _RE_PYTEST_TEST_HEADER.match(raw)
        if header_match is not None:
            # Flush previous block before starting the next.
            if current_name is not None and current_block:
                traceback_blocks[current_name] = "\n".join(current_block).rstrip()
            current_name = header_match.group("name")
            current_block = []
            continue
        if current_name is None:
            continue
        # End-of-block sentinel: any ``=== short summary ===``-style
        # line stops the current capture.
        if _RE_PYTEST_SECTION_END.match(raw):
            traceback_blocks[current_name] = "\n".join(current_block).rstrip()
            current_name = None
            current_block = []
            continue
        current_block.append(raw)
    # Tail flush.
    if current_name is not None and current_block:
        traceback_blocks[current_name] = "\n".join(current_block).rstrip()

    # Second pass: surface every FAILED / ERROR short-summary line.
    out: list[PytestFailure] = []
    for raw in lines:
        match = _RE_PYTEST_FAILED.match(raw)
        if match is None:
            continue
        nodeid = match.group("nodeid")
        tail = match.group("message") or ""
        test_path, _, test_name = nodeid.partition("::")
        # Bare test name = the last segment after the final ``::``. For
        # parametrised ids the name still matches the header (pytest
        # uses the same name in both places).
        bare_name = test_name.rsplit("::", 1)[-1]
        block = traceback_blocks.get(bare_name, "")
        message = f"{tail}\n{block}" if tail and block else tail or block
        out.append(
            PytestFailure(
                nodeid=nodeid,
                test_path=test_path,
                message=message,
            )
        )
    logger.debug(f"parse_pytest_failures lines={len(lines)} → {len(out)} failures")
    return out


# ---- Ruff -------------------------------------------------------------------

# ``path/to/file.py:LINE:COL: CODE message``. The code is letter+digits
# (``E501``, ``F401``, ``RUF012``) — the alpha prefix is 1-4 chars per
# the ruff plugin namespace convention.
_RE_RUFF = re.compile(
    r"^(?P<path>\S+\.\S+):(?P<line>\d+):(?P<col>\d+):\s+(?P<code>[A-Z]{1,4}\d+)\s+(?P<message>.+)$"
)


def parse_ruff_failures(log_text: str) -> list[RuffFailure]:
    """Parse ``path:line:col: CODE message`` ruff diagnostics.

    Returns:
        One :class:`RuffFailure` per matching line, in encounter order.
        Non-matching lines (banners, summaries, blank lines) are
        silently skipped.
    """
    out: list[RuffFailure] = []
    for raw in log_text.splitlines():
        match = _RE_RUFF.match(raw)
        if match is None:
            continue
        out.append(
            RuffFailure(
                path=match.group("path"),
                line=int(match.group("line")),
                col=int(match.group("col")),
                code=match.group("code"),
                message=match.group("message").strip(),
            )
        )
    logger.debug(f"parse_ruff_failures → {len(out)} failures")
    return out


# ---- Mypy -------------------------------------------------------------------

# ``path/to/file.py:LINE: error: message``. We deliberately *exclude*
# ``: note:`` lines: notes describe context (e.g. "Revealed type is X")
# rather than a fail-actionable diagnostic. The ``[error-code]`` tail
# that mypy emits is folded into the message verbatim.
_RE_MYPY = re.compile(r"^(?P<path>\S+\.\S+):(?P<line>\d+):\s+error:\s+(?P<message>.+)$")


def parse_mypy_failures(log_text: str) -> list[MypyFailure]:
    """Parse mypy ``path:line: error: ...`` diagnostics.

    Returns:
        One :class:`MypyFailure` per matching line, in encounter order.
        ``: note:`` lines are intentionally not surfaced.
    """
    out: list[MypyFailure] = []
    for raw in log_text.splitlines():
        match = _RE_MYPY.match(raw)
        if match is None:
            continue
        out.append(
            MypyFailure(
                path=match.group("path"),
                line=int(match.group("line")),
                message=match.group("message").strip(),
            )
        )
    logger.debug(f"parse_mypy_failures → {len(out)} failures")
    return out


__all__ = [
    "MypyFailure",
    "PytestFailure",
    "RuffFailure",
    "parse_mypy_failures",
    "parse_pytest_failures",
    "parse_ruff_failures",
]
