"""EAWF016 — entity titles are scannable labels, not commit subjects or id soup.

Layer 1 of the doc-clarity enforcement stack (see
``.ea/local/research/2026-05-29-doc-clarity.md``). A lifecycle / decision
entity carries a bounded ``title`` (a label) and an optional long-form
``description`` (the prose "why"). The title is the surface a human scans
most, yet it is the worst-quality class: wave titles leak a
conventional-commit type prefix (``feat:`` / ``docs:``), phase and decision
titles chain transient internal cluster codes joined by ``+`` ("cluster-code
soup"), and some titles are a bare id with no description of the work.

This module is the deterministic backstop. It reuses the existing over-cap /
trailing-period check (:func:`eawf.surfaces.render.agents_md.lint_entity_title`)
and adds three rules:

- **conventional-commit prefix** — the title starts with one of the
  commit-subject type tokens (``feat`` / ``fix`` / ``docs`` / ``chore`` /
  ``refactor`` / ``test`` / ``build`` / ``perf`` / ``ci`` / ``revert`` /
  ``state``) followed by ``:``. Those prefixes are tooling-parsed *commit*
  metadata, not entity labels. The token set is sourced from
  :data:`eawf.platform.profiles.clarity.COMMIT_SUBJECT_PREFIX_EXEMPT` so the
  prose blocklist (which exempts the same tokens on a commit *subject*) and
  this title rule (which rejects them on an entity *title*) never drift.
- **cluster-code soup** — three or more ``+``-joined tokens (``A+B+C``), with
  a ``C++`` carve-out so a legitimate language name is not flagged.
- **bare-id-only** — the whole title is a single id (``W02`` / ``[D17]`` /
  ``P29a``) with no description of the work.

The leading-``[A-Z]\\d`` rule from an earlier draft is deliberately *dropped*:
a live-state measurement found it matched ~309 legitimate backlog refs
(``B0NN``), decision codes (``V1``), and cluster ids — a false-positive storm
that would train operators to ignore the lint.

Enforcement points (per the doc-clarity ownership matrix):

- The **mutation boundary** — :func:`assert_title_clarity` is called by the
  ``plan_wave`` / ``plan_iter`` / ``open_iter`` / ``plan_phase`` /
  ``open_phase`` lifecycle transitions and the ``add_decision`` evidence
  mutator, so a new title is rejected at author time. Existing persisted
  titles are never re-validated, so they are grandfathered for free.
- A **diff-scoped pre-commit backstop** — :func:`check_state_title_lines`
  parses only the *added* ``"title":`` lines of the staged ``state.json``
  delta, catching anything that bypasses the boundary without flooding on
  legacy titles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from eawf.platform.profiles.clarity import COMMIT_SUBJECT_PREFIX_EXEMPT
from eawf.surfaces.render.agents_md import lint_entity_title

RULE_CODE = "EAWF016"

# Conventional-commit type prefix at the head of a title (``feat: ...``).
# Built from the canonical exempt set so the two rules share one token list.
_CC_PREFIX = re.compile(
    rf"^(?:{'|'.join(sorted(COMMIT_SUBJECT_PREFIX_EXEMPT))}):\s",
)
# Three-or-more ``+``-joined tokens (``A+B+C``). A two-token ``A+B`` is allowed
# (a single conjunction reads fine); soup starts at three.
_CLUSTER_SOUP = re.compile(r"[A-Za-z0-9]+(?:\+[A-Za-z0-9/]+){2,}")
# Whole title is a single bare lifecycle / decision id: optional brackets
# around UPPER-letters then two-or-more digits then an optional lowercase
# suffix (``W02`` / ``[D17]`` / ``P29a``). The two-digit floor matches the
# project's zero-padded id grammar (``P<NN+>`` / ``I<NN+>`` / ``W<NN+>``, all
# ``\d{2,}``), so a real bare-id title is caught while a one-digit throwaway
# label (``P1`` / ``I1``) is not — a one-digit code is not a valid id and the
# false-positive lesson (F5 of the doc-clarity brief) is honored.
_BARE_ID_ONLY = re.compile(r"^\[?[A-Z]+\d{2,}[a-z]?\]?\s*$")


@dataclass(frozen=True)
class TitleClarityViolation:
    """One EAWF016 finding against an entity title.

    Attributes:
        reason: Human-readable, lowercase-led, period-free explanation of
            which rule the title tripped.
        snippet: The offending title, surfaced verbatim so an author can
            locate and rewrite it.
        lineno: 1-based line number when the finding came from a
            ``state.json`` delta scan; ``0`` for a direct title check that
            has no line context.
    """

    reason: str
    snippet: str
    lineno: int = 0

    @property
    def code(self) -> str:
        """Return the rule code."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``line:col: CODE reason`` style one-liner body.

        The column is always ``0`` (a title is a single value, not a span);
        the line is the ``state.json`` delta line when known, else ``0``.
        """
        return f"{self.lineno}:0: {RULE_CODE} {self.reason}: {self.snippet!r}"


def check_title(title: str, *, lineno: int = 0) -> list[TitleClarityViolation]:
    """Return EAWF016 violations for a single entity *title*.

    Runs the reused over-cap / trailing-period check first, then the three
    title-clarity rules, in declaration order. An empty list means the title
    is a clean, scannable label.

    Args:
        title: Candidate entity title to inspect.
        lineno: Optional 1-based line number to stamp on each finding (used
            by the ``state.json`` delta scan); defaults to ``0`` for a
            context-free check.

    Returns:
        Zero or more :class:`TitleClarityViolation`, in rule-declaration
        order. A title that trips several rules yields several findings.
    """
    out: list[TitleClarityViolation] = [
        TitleClarityViolation(reason=message, snippet=title, lineno=lineno)
        for message in lint_entity_title(title)
    ]
    if _CC_PREFIX.match(title):
        out.append(
            TitleClarityViolation(
                reason="title carries a conventional-commit type prefix; titles are labels",
                snippet=title,
                lineno=lineno,
            )
        )
    if "C++" not in title and _CLUSTER_SOUP.search(title):
        out.append(
            TitleClarityViolation(
                reason="title is cluster-code/+-join soup; name what it does in prose",
                snippet=title,
                lineno=lineno,
            )
        )
    if _BARE_ID_ONLY.match(title):
        out.append(
            TitleClarityViolation(
                reason="title is a bare id with no description of the work",
                snippet=title,
                lineno=lineno,
            )
        )
    return out


def assert_title_clarity(title: str, *, entity_kind: str, entity_id: str) -> None:
    """Raise when *title* violates the EAWF016 entity-title clarity rules.

    The mutation-boundary gate: the lifecycle plan / open transitions call
    this with the new title so a bad label is rejected at author time. A
    clean title is a no-op.

    Args:
        title: The candidate entity title.
        entity_kind: Human label for the entity kind (``"wave"`` / ``"iter"``
            / ``"phase"`` / ``"decision"``), interpolated into the error.
        entity_id: The entity id, interpolated into the error so the operator
            can locate the offending row.

    Raises:
        ValueError: when *title* trips one or more title-clarity rules. The
            message names the entity, the title, and every rule it failed.
    """
    violations = check_title(title)
    if not violations:
        return
    reasons = "; ".join(v.reason for v in violations)
    raise ValueError(f"{entity_kind} {entity_id!r} title {title!r} fails title-clarity: {reasons}")


# Matches a JSON ``"title": "..."`` member on one physical line, capturing the
# string value. Title values are bounded ≤72 chars and never contain an
# unescaped quote, so a single non-greedy capture is sufficient for the
# state.json delta scan (which only inspects added lines).
_TITLE_LINE = re.compile(r'^\s*"title"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _decode_json_string(raw: str) -> str:
    """Return the JSON string body *raw* with standard escapes resolved.

    The delta scan captures the raw text between the quotes; a title may
    carry ``\\"`` or ``\\\\`` escapes, so the common pairs are unescaped before
    the clarity rules run. Unicode ``\\uXXXX`` escapes are left untouched
    (they do not affect any title-clarity rule, which keys on ASCII prefixes,
    ``+``-joins, and bracketed ids).
    """
    return (
        raw.replace('\\"', '"')
        .replace("\\\\", "\\")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
    )


def check_state_title_lines(
    added_lines: list[tuple[int, str]],
) -> list[TitleClarityViolation]:
    """Return EAWF016 violations for added ``"title":`` lines of a state delta.

    The diff-scoped pre-commit backstop. The caller supplies the
    ``(lineno, text)`` pairs of the lines *added* by the staged ``state.json``
    diff; this scans each for a ``"title": "..."`` member and runs
    :func:`check_title` on the decoded value. Lines that are not a title
    member are ignored, so only freshly-authored titles are checked and the
    hundreds of unchanged legacy titles are never re-scanned.

    Args:
        added_lines: ``(1-based lineno, raw line text)`` pairs for the lines
            the staged diff added to ``state.json``.

    Returns:
        Zero or more :class:`TitleClarityViolation`, each stamped with the
        line number it was found on, in input order.
    """
    findings: list[TitleClarityViolation] = []
    for lineno, line in added_lines:
        match = _TITLE_LINE.match(line)
        if match is None:
            continue
        title = _decode_json_string(match.group(1))
        findings.extend(check_title(title, lineno=lineno))
    return findings


__all__ = [
    "RULE_CODE",
    "TitleClarityViolation",
    "assert_title_clarity",
    "check_state_title_lines",
    "check_title",
]
