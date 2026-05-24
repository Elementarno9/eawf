"""EAWF010 — module-length rollup alarm (advisory coarse backstop).

A module-level rollup rule: flags any Python module whose physical line
count exceeds :data:`DEFAULT_MAX_LOC`. Oversized modules are a
maintainability smell — they outgrow a single reviewer's working set,
attract merge conflicts, and usually signal a missed split along a
responsibility seam (AGENTS rule 24, single-responsibility). The cap is
a rollup rather than a per-symbol rule: it does not care *what* makes the
module long, only that the file as a whole has crossed the budget.

**Advisory, not the master gate.** Lines-of-code is a coarse proxy:
research (and this codebase's own review) puts *complexity*, not length,
as the variable that predicts how hard code is to read, test, and change.
The precise per-function gates are therefore ruff ``C901`` (cyclomatic,
warns at 10) and :mod:`eawf.platform.lint.eawf011` (cognitive, warns at 15);
EAWF010 is demoted to an **advisory coarse alarm** that catches only
modules so large they are a structural emergency irrespective of their
internal complexity. Its budget is set well above the old precise cap so
the per-module grandfather list collapses to the genuine outliers, and a
module drops off that list as soon as a wave touches it (un-grandfather
on touch).

**Waiver.** A genuinely irreducible module (a generated table, a
cohesive command surface mid-split) may opt out with a waiver comment:

    # noqa: EAWF010 splitting deferred to P27-W06; see lifecycle seam

The waiver is accepted **only** when a non-empty rationale follows the
``EAWF010`` token on the same line. A bare ``# noqa: EAWF010`` with no
trailing rationale is rejected with its own diagnostic, so a waiver can
never be a silent mute — the reader always learns *why* the file is
oversized. The rationale is free prose explaining the WHY (a planned
split, a generated-file note); it is not a decision-provenance reference
(AGENTS rule 25). The rule scans only the first :data:`_WAIVER_SCAN_LINES`
lines for the waiver so it reads as a file-level annotation near the top,
not buried at the bottom.

The threshold and a per-path exclusion list are configurable through the
``[tool.eawf.lint]`` table in ``pyproject.toml`` (see :mod:`eawf.platform.lint`),
which is how the rule is wired to run under pre-commit without redding
the existing tree on modules already slated for a split.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RULE_CODE = "EAWF010"

# Default per-module physical line-count budget. Overridable via the
# ``[tool.eawf.lint] max-loc`` key in pyproject.
DEFAULT_MAX_LOC = 700

# A waiver must appear within the first N lines so it reads as a
# file-level annotation, not a hidden mute at the bottom of the module.
_WAIVER_SCAN_LINES = 40

# Matches ``# noqa: EAWF010`` followed by optional rationale prose. The
# rationale group is whitespace-trimmed by the caller; an empty group
# means the waiver carries no rationale and is itself a violation.
_WAIVER_PATTERN = re.compile(r"#\s*noqa:\s*EAWF010(?P<rationale>.*)$")


@dataclass(frozen=True)
class ModuleLengthViolation:
    """One EAWF010 finding.

    Attributes:
        loc: the module's physical line count.
        max_loc: the budget that was exceeded.
        reason: short human-readable cause (over budget, or a waiver
            present but missing its required rationale).
    """

    loc: int
    max_loc: int
    reason: str

    @property
    def code(self) -> str:
        """Return the rule code (``EAWF010``)."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``CODE reason`` style one-liner body."""
        return f"{RULE_CODE} {self.reason}"


def count_loc(source: str) -> int:
    """Return the physical line count of ``source``.

    Counts physical lines (the unit the cap is expressed in), not logical
    statements: a trailing newline does not add a phantom final line, and
    an empty source counts as zero.

    Args:
        source: Module source text.

    Returns:
        The number of physical lines.
    """
    if not source:
        return 0
    return source.count("\n") + (0 if source.endswith("\n") else 1)


def find_waiver(source: str) -> str | None:
    """Return the waiver rationale if a valid waiver is present, else ``None``.

    Scans the first :data:`_WAIVER_SCAN_LINES` lines for a
    ``# noqa: EAWF010 <rationale>`` comment.

    Args:
        source: Module source text.

    Returns:
        The trimmed rationale string when a waiver with non-empty
        rationale is present; an empty string when a bare waiver (no
        rationale) is present; ``None`` when no waiver token is found.
    """
    for line in source.splitlines()[:_WAIVER_SCAN_LINES]:
        match = _WAIVER_PATTERN.search(line)
        if match is not None:
            return match.group("rationale").strip()
    return None


def check_source(source: str, *, max_loc: int = DEFAULT_MAX_LOC) -> list[ModuleLengthViolation]:
    """Return EAWF010 violations for ``source``.

    A module under ``max_loc`` lines is always clean. An oversized module
    is clean only when it carries a ``# noqa: EAWF010 <rationale>`` waiver
    with a non-empty rationale; a bare waiver (no rationale) is itself a
    violation, and no waiver at all is the plain over-budget violation.

    Args:
        source: Module source text.
        max_loc: Per-module line budget (defaults to
            :data:`DEFAULT_MAX_LOC`).

    Returns:
        A list with at most one violation (the rule is a single per-module
        rollup), empty when the module is within budget or validly waived.
    """
    loc = count_loc(source)
    if loc <= max_loc:
        return []
    waiver = find_waiver(source)
    if waiver is None:
        return [
            ModuleLengthViolation(
                loc=loc,
                max_loc=max_loc,
                reason=f"module is {loc} lines (cap {max_loc}); split it or add a waiver "
                f"'# noqa: EAWF010 <rationale>'",
            )
        ]
    if not waiver:
        return [
            ModuleLengthViolation(
                loc=loc,
                max_loc=max_loc,
                reason="EAWF010 waiver present but missing rationale; "
                "add prose after the code: '# noqa: EAWF010 <rationale>'",
            )
        ]
    return []
