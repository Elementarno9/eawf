"""Unit tests for :mod:`eawf.workflow.pr_review.parser` (B041)."""

from __future__ import annotations

import pytest

from eawf.workflow.pr_review.parser import Finding, parse_findings


def test_parse_findings_single_blocker_line() -> None:
    """A single canonical blocker line parses into one :class:`Finding`."""
    markdown = (
        "src/eawf/auth.py:42: \U0001f534 blocker: SQL injection in lookup. Use parameterised query."
    )
    findings = parse_findings(markdown)
    assert len(findings) == 1
    f = findings[0]
    assert isinstance(f, Finding)
    assert f.path == "src/eawf/auth.py"
    assert f.line == 42
    assert f.severity == "blocker"
    assert "SQL injection" in f.message


def test_parse_findings_all_four_severities() -> None:
    """One finding per canonical severity emoji round-trips intact."""
    markdown = "\n".join(
        [
            "src/a.py:10: \U0001f534 blocker: missing auth check. Add `require_login`.",
            "src/b.py:20: \U0001f7e0 must-fix: returns wrong type. Use `int`.",
            "src/c.py:30: \U0001f7e1 should-fix: name shadows builtin. Rename.",
            "src/d.py:40: \U0001f535 nit: trailing whitespace. Strip.",
        ]
    )
    findings = parse_findings(markdown)
    severities = [f.severity for f in findings]
    assert severities == ["blocker", "must-fix", "should-fix", "nit"]
    assert findings[0].path == "src/a.py"
    assert findings[3].line == 40


def test_parse_findings_unknown_emoji_raises_value_error() -> None:
    """A line with the leading shape but an unknown severity tag raises."""
    # Use a glyph that is neither in the canonical palette nor a known
    # textual severity ("foo" passes neither emoji-table nor text-set).
    markdown = "src/x.py:1: ! foo: something. Fix it."
    with pytest.raises(ValueError, match="unrecognised severity tag"):
        parse_findings(markdown)


def test_parse_findings_ignores_commentary_lines() -> None:
    """Lines without a ``path:line:`` prefix are silently dropped."""
    markdown = "\n".join(
        [
            "Summary of review:",
            "Reviewed 4 files; the auth path is mostly clean.",
            "",
            "src/eawf/auth.py:7: \U0001f7e0 must-fix: bad import order. Sort it.",
            "",
            "Overall the diff is acceptable.",
        ]
    )
    findings = parse_findings(markdown)
    assert len(findings) == 1
    assert findings[0].path == "src/eawf/auth.py"
    assert findings[0].line == 7
    assert findings[0].severity == "must-fix"


def test_parse_findings_missing_line_number_yields_none() -> None:
    """The ``path::`` form (empty line slot) gives ``line=None``."""
    markdown = (
        "src/eawf/missing.py:: \U0001f7e1 should-fix: file-level rename suggestion. Rename module."
    )
    findings = parse_findings(markdown)
    assert len(findings) == 1
    assert findings[0].path == "src/eawf/missing.py"
    assert findings[0].line is None
    assert findings[0].severity == "should-fix"
