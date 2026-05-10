"""PR review automation (B041).

Public API:

- :func:`parse_findings` — parse a caveman-reviewer / claude ``/review``
  Markdown output into a list of structured :class:`Finding` records.
- :func:`verdict_for` — derive a review verdict
  (``approve`` / ``request-changes`` / ``comment-only``) from a list of
  findings.
- :func:`summary_line` — render a one-line per-severity tally.
- :class:`Finding` — the structured-finding dataclass.
- :data:`ReviewVerdict` — the review-verdict literal type alias.

The package is pure: no I/O, no logging side-effects beyond the
module-level loggers. CLI handlers in
:mod:`eawf.cli.commands.pr_review` own file reads and state mutation.
"""

from __future__ import annotations

from eawf.pr_review.parser import Finding, parse_findings
from eawf.pr_review.policy import ReviewVerdict, summary_line, verdict_for

__all__ = [
    "Finding",
    "ReviewVerdict",
    "parse_findings",
    "summary_line",
    "verdict_for",
]
