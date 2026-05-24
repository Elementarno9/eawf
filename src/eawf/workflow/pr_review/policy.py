"""Review-policy derivation (B041).

Pure helpers that turn a list of :class:`~eawf.workflow.pr_review.parser.Finding`
records into a single review verdict and a one-line tally.
"""

from __future__ import annotations

import logging
from typing import Literal

from eawf.workflow.pr_review.parser import Finding

logger = logging.getLogger(__name__)


ReviewVerdict = Literal["approve", "request-changes", "comment-only"]


def verdict_for(findings: list[Finding]) -> ReviewVerdict:
    """Map a list of findings to the canonical review verdict.

    Rules:

    * Any ``blocker`` or ``must-fix`` → ``"request-changes"``.
    * Any ``should-fix`` or ``nit`` (and no blocker / must-fix) →
      ``"comment-only"``.
    * Empty list → ``"approve"``.

    The rule set is intentionally simple: the reviewer surface is
    deterministic and downstream consumers (e.g. ``wave review``)
    re-emit the verdict verbatim in their envelope.
    """
    severities = {f.severity for f in findings}
    if "blocker" in severities or "must-fix" in severities:
        return "request-changes"
    if severities:  # any should-fix / nit
        return "comment-only"
    return "approve"


def summary_line(findings: list[Finding]) -> str:
    """Return ``"blocker:N0 must-fix:N1 should-fix:N2 nit:N3"`` for *findings*."""
    counts = {"blocker": 0, "must-fix": 0, "should-fix": 0, "nit": 0}
    for f in findings:
        counts[f.severity] = counts[f.severity] + 1
    return (
        f"blocker:{counts['blocker']} must-fix:{counts['must-fix']} "
        f"should-fix:{counts['should-fix']} nit:{counts['nit']}"
    )


__all__ = ["ReviewVerdict", "summary_line", "verdict_for"]
