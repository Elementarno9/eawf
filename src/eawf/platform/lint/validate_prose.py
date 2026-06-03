"""``validate_prose`` — the single Layer-2 prose enforcement chokepoint.

Layer 2 of the doc-clarity enforcement stack (see
``.ea/local/research/2026-05-29-doc-clarity.md``) lands several independent
prose checks: the Vale wrapper (``eawf hook vale-prose``), the EAWF017
inline-reference tabulation lint, and the older EAWF013 citation-bracket and
EAWF014 no-manual-wrap lints. This module composes them into one entry point so
both the generation-time skill-render loop and a strict CI gate call a single
function rather than re-deriving the per-lint plumbing.

The chokepoint is a pure function over Markdown text. The three deterministic
checks (EAWF013 / EAWF014 / EAWF017) run in-process. Vale is a subprocess that
lives behind the ``eawf hook vale-prose`` CLI seam and fails open when its
binary is absent; to keep this function pure (and unit-testable without a Vale
install) the caller passes already-rendered Vale finding rows through
``vale_rows`` — the chokepoint folds them into the aggregate but never shells
out itself.

Stability model (the "Prose coverage DAGs" two-mode contract):

- **fail-open** (``strict=False``) — a local pre-commit / in-skill run. Findings
  are aggregated and surfaced as advisory, but :meth:`ProseReport.exit_code` is
  ``0`` and :meth:`ProseReport.ok` is ``True`` regardless. The operator is never
  blocked locally.
- **fail-closed** (``strict=True``) — the CI gate. Any aggregated finding makes
  :meth:`ProseReport.ok` ``False`` and :meth:`ProseReport.exit_code` non-zero,
  so the PR is blocked.

The Vale leg fails open at its own seam (an absent binary yields no
``vale_rows``); the deterministic EAWF013/014/017 legs always run, so strict
mode still rejects a known-bad artifact even on a machine without Vale.
"""

from __future__ import annotations

from dataclasses import dataclass

from eawf.platform.lint.eawf013_bracket_position import check_source as _check_eawf013
from eawf.platform.lint.eawf014_no_manual_wrap import check_source as _check_eawf014
from eawf.platform.lint.eawf017_inline_reference import check_source as _check_eawf017

# The deterministic Layer-2 lints the chokepoint composes, in source order. The
# Vale leg is intentionally not in this tuple: it is a subprocess fed in via
# ``vale_rows`` so this module stays a pure, import-only function.
COMPOSED_RULES: tuple[str, ...] = ("EAWF013", "EAWF014", "EAWF017")

# The rule label attached to a row that arrives pre-rendered from the Vale
# subprocess (``eawf hook vale-prose``). The individual ``Google.Weasel`` style
# check name is preserved inside the row text; this is the aggregate bucket.
_VALE_RULE = "VALE"


@dataclass(frozen=True)
class ProseFinding:
    """One normalized finding from any composed Layer-2 check.

    The three deterministic lints each expose their own violation dataclass
    (``BracketPositionViolation``, ``ManualWrapViolation``,
    ``InlineReferenceViolation``); :func:`validate_prose` normalizes each into
    this shared shape so the aggregate is one homogeneous list. A Vale row
    arrives pre-rendered (it is produced by the subprocess seam) and is wrapped
    with ``code="VALE"`` and ``lineno=0``.

    Attributes:
        code: The originating rule code (``"EAWF013"`` / ``"EAWF014"`` /
            ``"EAWF017"`` / ``"VALE"``).
        lineno: 1-based line the finding is anchored to; ``0`` for a Vale row
            whose position is already inside its rendered text.
        col_offset: 0-based column of the matched token (``0`` for Vale rows
            and block-level findings).
        reason: Lowercase-led, period-free explanation of the rule tripped.
        snippet: The offending token / descriptor surfaced so the author can
            locate it. Empty for a Vale row (its text is the ``reason``).
    """

    code: str
    lineno: int
    col_offset: int
    reason: str
    snippet: str = ""

    def render(self) -> str:
        """Return a ``CODE line:col reason: snippet`` one-liner body."""
        head = f"{self.code} {self.lineno}:{self.col_offset} {self.reason}"
        return f"{head}: {self.snippet!r}" if self.snippet else head


@dataclass(frozen=True)
class ProseReport:
    """The aggregate of every composed Layer-2 finding for one prose surface.

    Attributes:
        findings: Every normalized finding, in source then rule order.
        strict: The mode the chokepoint ran in. ``True`` is the CI gate
            (fail-closed); ``False`` is the local / in-skill run (fail-open).
    """

    findings: tuple[ProseFinding, ...]
    strict: bool = False

    @property
    def has_findings(self) -> bool:
        """Return ``True`` when at least one composed check reported a finding."""
        return bool(self.findings)

    @property
    def ok(self) -> bool:
        """Return whether the surface passes *in the configured mode*.

        Fail-open (``strict=False``) is always ``True`` — findings are advisory
        and never fail a local run. Fail-closed (``strict=True``) is ``True``
        only when there are no findings.
        """
        return True if not self.strict else not self.has_findings

    @property
    def exit_code(self) -> int:
        """Return the process exit code for the configured mode.

        ``0`` always in fail-open mode; ``1`` (``USER_ERROR``) on any finding in
        fail-closed mode, else ``0``.
        """
        return 0 if self.ok else 1

    def render(self) -> str:
        """Return a multi-line human summary (one row per finding, header first)."""
        mode = "strict" if self.strict else "advisory"
        if not self.findings:
            return f"validate_prose: clean ({mode})"
        head = f"validate_prose: {len(self.findings)} finding(s) ({mode})"
        return "\n".join([head, *(f"  {finding.render()}" for finding in self.findings)])

    def codes(self) -> set[str]:
        """Return the distinct rule codes that contributed a finding."""
        return {finding.code for finding in self.findings}


def _normalize(code: str, violation: object) -> ProseFinding:
    """Wrap one composed-lint violation in the shared :class:`ProseFinding`.

    The three deterministic violation dataclasses share the same attribute
    surface (``lineno`` / ``col_offset`` / ``reason`` / ``snippet``), so one
    duck-typed adapter covers all three.
    """
    return ProseFinding(
        code=code,
        lineno=getattr(violation, "lineno", 0),
        col_offset=getattr(violation, "col_offset", 0),
        reason=getattr(violation, "reason", ""),
        snippet=getattr(violation, "snippet", ""),
    )


def validate_prose(
    source: str,
    *,
    strict: bool = False,
    vale_rows: tuple[str, ...] = (),
) -> ProseReport:
    """Compose every Layer-2 prose check over one Markdown surface.

    Runs the three deterministic lints in-process (EAWF013 citation-bracket
    position, EAWF014 no-manual-wrap, EAWF017 inline-reference tabulation) and
    folds any pre-rendered Vale rows into one aggregate :class:`ProseReport`.
    The function never shells out: a caller that wants the Vale leg runs the
    ``eawf hook vale-prose`` subprocess (which fails open when the binary is
    absent) and passes its rendered rows here.

    Args:
        source: The Markdown text to inspect (a committed artifact, a rendered
            ``SKILL.md``, a commit/PR body, or a ``state.json`` description).
        strict: ``True`` selects the fail-closed CI contract (any finding makes
            the report not-``ok`` and the exit code non-zero); ``False`` (the
            default) selects the fail-open local/in-skill contract (findings are
            advisory, exit code stays ``0``).
        vale_rows: Pre-rendered Vale finding rows (each the
            ``  label:line:col: sev Check Message`` shape the wrapper emits),
            folded into the aggregate. Empty when Vale was unavailable or clean
            — the deterministic legs still run.

    Returns:
        A :class:`ProseReport` carrying every normalized finding plus the mode
        it ran in. Read :meth:`ProseReport.ok` / :meth:`ProseReport.exit_code`
        for the mode-aware verdict.
    """
    findings: list[ProseFinding] = []
    for code, check in (
        ("EAWF013", _check_eawf013),
        ("EAWF014", _check_eawf014),
        ("EAWF017", _check_eawf017),
    ):
        findings.extend(_normalize(code, violation) for violation in check(source))
    findings.sort(key=lambda f: (f.lineno, f.col_offset, f.code))
    # Vale rows arrive already rendered + position-bearing; append them after
    # the deterministic findings so the human summary keeps source order for the
    # in-process legs and groups the Vale advisory rows last.
    for row in vale_rows:
        findings.append(
            ProseFinding(code=_VALE_RULE, lineno=0, col_offset=0, reason=row.strip(), snippet="")
        )
    return ProseReport(findings=tuple(findings), strict=strict)


__all__ = [
    "COMPOSED_RULES",
    "ProseFinding",
    "ProseReport",
    "validate_prose",
]
