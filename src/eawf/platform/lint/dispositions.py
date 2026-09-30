"""The epoch-2 disposition of every shipped lint rule.

A rule survives the lifecycle cutover in one of three ways: it is
carried unchanged, rewritten against the epoch-2 identifiers (milestone,
batch, task, run), or retired. :data:`RULE_DISPOSITIONS` is the one table
that says which, and :func:`disposition_findings` holds it total over the
rules the tree actually ships.

The authority is the set of rule codes, not a file listing: a numbered
rule is whatever module under the lint package declares a module-level
``RULE_CODE``, wherever it sits -- one lives in the package's ``tools``
sub-directory, which is exactly how a top-level file count once missed
it. The unnumbered entries have no code to scan for, so
:data:`UNNUMBERED_RULE_MODULES` names each one's module.

A rewrite with no alias layer means a rule stops naming epoch-1 lifecycle
identifiers. :func:`epoch1_references` finds them in a rule's code (never
in its comments or docstrings, which may explain history), and a rule
that still names one must say why in its table row; the only standing
reason is a rule that reads history written in the epoch-1 form.
"""

from __future__ import annotations

import ast
import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

#: Repo-relative directory the lint package lives in.
LINT_PACKAGE: Final[str] = "src/eawf/platform/lint"


class Disposition(StrEnum):
    """What becomes of one shipped rule at the cutover."""

    CARRY = "carry"
    REWRITE = "rewrite"
    RETIRE = "retire"


@dataclass(frozen=True, slots=True)
class RuleDisposition:
    """One row of the disposition table.

    Attributes:
        rule: The rule code (``EAWF002``) or unnumbered entry name.
        enforces: What the rule holds, in one phrase.
        disposition: Whether it is carried, rewritten or retired.
        epoch1_reference: Why the rule's code still names an epoch-1
            lifecycle identifier, or ``None`` when it names none.
    """

    rule: str
    enforces: str
    disposition: Disposition
    epoch1_reference: str | None = None


#: The reason a rule may keep reading the epoch-1 commit grammar: history
#: is never rewritten, so commits written before the switch stay lintable.
_READS_EPOCH1_HISTORY: Final[str] = (
    "recognises the bracket and wave-trailer forms commits were written in before the switch"
)

#: The unnumbered entries, by the module each one lives in. The commit
#: grammar sits in the repository's own tools directory rather than the
#: package, so its path is spelled out rather than derived.
UNNUMBERED_RULE_MODULES: Final[Mapping[str, str]] = {
    "commit-prefix": "tools/commit_prefix_lint.py",
    "clarity-anchors": f"{LINT_PACKAGE}/clarity_anchors.py",
    "sigil-totality": f"{LINT_PACKAGE}/sigil_totality.py",
    "prose-chokepoint": f"{LINT_PACKAGE}/validate_prose.py",
    "conditional-harness": f"{LINT_PACKAGE}/_conditional.py",
}

RULE_DISPOSITIONS: Final[tuple[RuleDisposition, ...]] = (
    RuleDisposition("EAWF001", "structured log format at library call sites", Disposition.CARRY),
    RuleDisposition("EAWF002", "bare log keys for lifecycle identifiers", Disposition.REWRITE),
    RuleDisposition("EAWF003", "library logger acquired by module name", Disposition.CARRY),
    RuleDisposition("EAWF010", "module-length rollup alarm", Disposition.CARRY),
    RuleDisposition("EAWF011", "cognitive-complexity gate", Disposition.CARRY),
    RuleDisposition("EAWF012", "no provenance breadcrumbs in source comments", Disposition.CARRY),
    RuleDisposition("EAWF013", "citation brackets stay attached to their claim", Disposition.CARRY),
    RuleDisposition("EAWF014", "no manually wrapped rendered markdown", Disposition.CARRY),
    RuleDisposition("EAWF015", "advisory requirement-shape lint", Disposition.CARRY),
    RuleDisposition("EAWF016", "entity titles are scannable labels", Disposition.CARRY),
    RuleDisposition("EAWF017", "reference soup moves into a numbered table", Disposition.CARRY),
    RuleDisposition("EAWF018", "advisory structure-smell heuristics", Disposition.CARRY),
    RuleDisposition("EAWF019", "facets and citations in explainer docs", Disposition.CARRY),
    RuleDisposition("EAWF020", "laconic-bullet prose shape", Disposition.CARRY),
    RuleDisposition("EAWF021", "success criteria are measurable", Disposition.REWRITE),
    RuleDisposition(
        "EAWF022", "dropped brief detail surfaces at plan render time", Disposition.REWRITE
    ),
    RuleDisposition("EAWF023", "artifact placement and date stem", Disposition.REWRITE),
    RuleDisposition("EAWF024", "test-tier contract for the unit tier", Disposition.REWRITE),
    RuleDisposition("EAWF025", "test placement under the kind taxonomy", Disposition.CARRY),
    RuleDisposition(
        "EAWF026", "every settings section filed under one category", Disposition.CARRY
    ),
    RuleDisposition("EAWF027", "committed text quotes only quotable rows", Disposition.CARRY),
    RuleDisposition("EAWF028", "a committed research brief keeps its wording", Disposition.CARRY),
    RuleDisposition(
        "commit-prefix",
        "commit subject and trailer grammar",
        Disposition.REWRITE,
        epoch1_reference=_READS_EPOCH1_HISTORY,
    ),
    RuleDisposition("clarity-anchors", "clarity judge calibration anchors", Disposition.CARRY),
    RuleDisposition("sigil-totality", "no bare status value in a render", Disposition.CARRY),
    RuleDisposition("prose-chokepoint", "the single prose enforcement entry", Disposition.CARRY),
    RuleDisposition("conditional-harness", "shared conditional-rule plumbing", Disposition.CARRY),
)

#: Epoch-1 identifier shapes inside a string constant: a lifecycle id key,
#: a full wave id, the regex source that matches one, or the wave trailer.
#: A wave id glued to a preceding word or hyphen is part of a file name,
#: not an identifier the rule reads.
_EPOCH1_TEXT_RE: Final = re.compile(
    r"(?<![A-Za-z0-9])(?:wave|iter|phase)_ids?(?![A-Za-z0-9])"
    r"|(?<![\w-])P\d{2,}(?:-I\d{2,})?-W\d{2,}(?![\w-])"
    r"|\\\[P\\d"
    r"|\bEawf-Wave\b"
)
#: Epoch-1 identifier shapes as a Python name, argument or attribute.
_EPOCH1_NAME_RE: Final = re.compile(r"(?:^|_)(?:wave|iter|phase)_ids?(?:$|_)")
#: A string constant that is exactly an epoch-1 lifecycle key.
_EPOCH1_KEYS: Final[frozenset[str]] = frozenset(
    {"wave", "waves", "iter", "iters", "phase", "phases"}
)


@dataclass(frozen=True, slots=True)
class DispositionFinding:
    """One way the table and the shipped rules disagree.

    Attributes:
        rule: The rule the finding is about.
        reason: What is wrong, in one sentence.
    """

    rule: str
    reason: str

    def render(self) -> str:
        """Return a one-line ``rule: reason`` diagnostic."""
        return f"{self.rule}: {self.reason}"


def _rule_code(tree: ast.Module) -> str | None:
    """Return the module-level ``RULE_CODE`` string literal, if any."""
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "RULE_CODE" for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    return None


def shipped_rules(repo_root: Path) -> dict[str, Path]:
    """Return every rule the tree at *repo_root* ships, by rule id.

    Args:
        repo_root: The repository whose lint package is read.

    Returns:
        Each numbered rule code mapped to the module declaring it, plus
        each unnumbered entry whose module exists.

    Raises:
        SyntaxError: A module in the lint package does not parse.
    """
    rules: dict[str, Path] = {}
    for path in sorted((repo_root / LINT_PACKAGE).rglob("*.py")):
        code = _rule_code(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if code is not None:
            rules[code] = path
    for name, relative in UNNUMBERED_RULE_MODULES.items():
        if (repo_root / relative).is_file():
            rules[name] = repo_root / relative
    return rules


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Return the ids of every docstring constant in *tree*."""
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, owners)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _node_name(node: ast.AST) -> tuple[int, str] | None:
    """Return the line and identifier a name, attribute or argument node binds."""
    if isinstance(node, ast.Name):
        return node.lineno, node.id
    if isinstance(node, ast.Attribute):
        return node.lineno, node.attr
    if isinstance(node, ast.arg):
        return node.lineno, node.arg
    return None


def epoch1_references(source: str) -> list[str]:
    """Return the epoch-1 lifecycle identifiers *source*'s code names.

    Comments never reach the syntax tree and docstrings are skipped, so a
    rule may explain the vocabulary it replaced without naming it.

    Args:
        source: Python source of one rule module.

    Returns:
        ``line: token`` entries in line order, one per offending name,
        argument, attribute or string constant.

    Raises:
        SyntaxError: *source* is not parseable Python.
    """
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        named = _node_name(node)
        if named is not None:
            if _EPOCH1_NAME_RE.search(named[1]):
                hits.append(named)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and (node.value in _EPOCH1_KEYS or _EPOCH1_TEXT_RE.search(node.value))
        ):
            hits.append((node.lineno, node.value[:60]))
    return [f"{line}: {token}" for line, token in sorted(hits)]


def disposition_findings(
    repo_root: Path, table: Sequence[RuleDisposition] = RULE_DISPOSITIONS
) -> list[DispositionFinding]:
    """Return every disagreement between *table* and the shipped rules.

    Only shipped rules are judged, so a checkout that ships no lint
    package has nothing to disagree about.

    Args:
        repo_root: The repository whose rules are read.
        table: The disposition table to hold total.

    Returns:
        Findings in rule order: a rule with no disposition or more than
        one, a retired rule that still ships, a rule naming an epoch-1
        identifier its row gives no reason for, and a row giving a reason
        its rule no longer needs.

    Raises:
        SyntaxError: A rule module does not parse.
    """
    counts = Counter(row.rule for row in table)
    rows = {row.rule: row for row in table}
    findings: list[DispositionFinding] = []
    for rule, path in sorted(shipped_rules(repo_root).items()):
        if counts[rule] != 1:
            count = "no disposition" if counts[rule] == 0 else f"{counts[rule]} dispositions"
            findings.append(DispositionFinding(rule, f"ships with {count}; it needs exactly one"))
            continue
        row = rows[rule]
        if row.disposition is Disposition.RETIRE:
            findings.append(DispositionFinding(rule, "is retired but still ships"))
            continue
        references = epoch1_references(path.read_text(encoding="utf-8"))
        if references and row.epoch1_reference is None:
            findings.append(
                DispositionFinding(
                    rule,
                    f"names epoch-1 lifecycle identifiers ({', '.join(references[:3])}); "
                    "rewrite it against milestone, batch, task and run",
                )
            )
        elif not references and row.epoch1_reference is not None:
            findings.append(
                DispositionFinding(
                    rule, "no longer names an epoch-1 identifier; drop its stated reason"
                )
            )
    logger.debug(f"disposition_findings rules={len(rows)} findings={len(findings)}")
    return findings


__all__ = [
    "LINT_PACKAGE",
    "RULE_DISPOSITIONS",
    "UNNUMBERED_RULE_MODULES",
    "Disposition",
    "DispositionFinding",
    "RuleDisposition",
    "disposition_findings",
    "epoch1_references",
    "shipped_rules",
]
