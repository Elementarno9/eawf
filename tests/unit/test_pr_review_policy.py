"""Unit tests for :mod:`eawf.workflow.pr_review.policy` (B041)."""

from __future__ import annotations

from eawf.workflow.pr_review.parser import Finding
from eawf.workflow.pr_review.policy import summary_line, verdict_for


def _f(severity: str) -> Finding:
    """Helper: minimal :class:`Finding` with the supplied severity."""
    return Finding(path="src/x.py", line=1, severity=severity, message="msg")  # type: ignore[arg-type]


def test_verdict_for_empty_returns_approve() -> None:
    """An empty findings list maps to ``approve``."""
    assert verdict_for([]) == "approve"


def test_verdict_for_blocker_returns_request_changes() -> None:
    """A single blocker maps to ``request-changes``."""
    assert verdict_for([_f("blocker")]) == "request-changes"


def test_verdict_for_must_fix_returns_request_changes() -> None:
    """A single must-fix also maps to ``request-changes``."""
    assert verdict_for([_f("must-fix")]) == "request-changes"


def test_verdict_for_only_nit_returns_comment_only() -> None:
    """A nit alone maps to ``comment-only``."""
    assert verdict_for([_f("nit")]) == "comment-only"


def test_verdict_for_only_should_fix_returns_comment_only() -> None:
    """A should-fix alone maps to ``comment-only``."""
    assert verdict_for([_f("should-fix")]) == "comment-only"


def test_summary_line_emits_counts() -> None:
    """The summary line tallies counts in canonical order."""
    findings = [
        _f("blocker"),
        _f("must-fix"),
        _f("must-fix"),
        _f("should-fix"),
        _f("nit"),
        _f("nit"),
        _f("nit"),
    ]
    assert summary_line(findings) == "blocker:1 must-fix:2 should-fix:1 nit:3"


def test_summary_line_empty_is_all_zeros() -> None:
    """An empty list still emits the full per-severity row."""
    assert summary_line([]) == "blocker:0 must-fix:0 should-fix:0 nit:0"
