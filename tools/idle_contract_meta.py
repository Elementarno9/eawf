"""Idle-contract meta-gate: flag a newly-defined contract that ships idle in a diff."""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from idle_contract_common import (
    _REPO_ROOT,
)

# =========================================================================== #
# Meta-gate: detect a newly-defined contract that ships idle in a diff.
# =========================================================================== #


#: Contract-family detectors over a single added source line. The meta-gate is
#: scoped tightly to these families so a plain internal helper (e.g.
#: ``def _coerce_row(...)``) is never flagged. Each pattern captures the
#: contract symbol name in group ``sym`` so the finding can name the orphan.
#:
#: - ``check_*`` / ``*_gate`` / ``*_lint`` function defs are the gate / lint /
#:   validator family.
#: - a ``@register(...)`` / ``@register_check(...)`` decorator whose argument is
#:   a ``CheckKind`` string token registers a check runner; the decorated name
#:   is the contract.
#: - a new ``OracleTier.T<n>_*`` dispatch arm is an oracle-tier branch.
_CONTRACT_DEF_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*def\s+(?P<sym>check_[A-Za-z0-9_]+)\s*\("),
    re.compile(r"^\s*def\s+(?P<sym>[A-Za-z0-9_]+_gate)\s*\("),
    re.compile(r"^\s*def\s+(?P<sym>[A-Za-z0-9_]+_lint)\s*\("),
)

#: A ``CheckKind``-runner registration: a ``@register(...)`` /
#: ``@register_check(...)`` decorator line that names a ``CheckKind`` token. The
#: decorated def on the following added line carries the contract symbol.
_CHECKKIND_DECORATOR_RE = re.compile(r"^\s*@register(?:_check)?\s*\(.*CheckKind.*\)\s*$")

#: A decorated def line (used to recover the symbol a ``CheckKind`` decorator
#: registers).
_DECORATED_DEF_RE = re.compile(r"^\s*def\s+(?P<sym>[A-Za-z0-9_]+)\s*\(")

#: A new oracle-tier dispatch arm: a reference to an ``OracleTier.T<n>_<NAME>``
#: member in an added line. The member token is the contract symbol.
_ORACLE_TIER_RE = re.compile(r"OracleTier\.(?P<sym>T\d+_[A-Z0-9_]+)")

#: A new ``eawf0##_*.py`` lint rule module under the lint package. The file's
#: rule id (``eawf0##``) is the contract symbol.
_LINT_MODULE_RE = re.compile(r"^src/eawf/platform/lint/(?P<sym>eawf0\d\d)_[A-Za-z0-9_]+\.py$")

#: A unified-diff hunk-header carrying the file path of the *added* side.
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(?P<path>.+)$")

#: The unified-diff ``--- /dev/null`` old-side marker, present only when the
#: file is genuinely added (a modified file's old side is ``--- a/<path>``).
#: The lint-module path detector keys on this so a mere edit of an existing
#: ``eawf0##_*.py`` module is not mistaken for a new lint contract -- only a
#: brand-new module file registers the rule-id contract. ``git diff`` also
#: emits a ``new file mode`` line for an addition, but ``--- /dev/null`` is the
#: portable signal both real git and the test fixtures carry.
_NEW_FILE_OLD_SIDE_RE = re.compile(r"^--- /dev/null$")

#: Repo-relative path prefixes a contract may legitimately be DEFINED under.
#: A contract is defined in shipped source (``src/``) or a gate script
#: (``tools/``); a ``tests/`` file that constructs a contract-shaped token is a
#: test fixture (or the discharge itself), never a new contract, so the parser
#: ignores added lines in test files to avoid flagging its own fixtures.
_SOURCE_PREFIXES: tuple[str, ...] = ("src/", "tools/")

#: An added line in a unified diff (``+`` prefix, but not the ``+++`` header).
_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+ )(?P<body>.*)$")


class MissingDischarge(StrEnum):
    """Which idle-contract discharge a finding reports as missing.

    A contract is discharged only when it is both *called* (a call-site outside
    its defining module proves it runs in production) AND *asserted* (a test
    references it so a regression is caught). The order is informational only.

    :attr:`NO_RUNTIME_OUTPUT` is a distinct, *dynamic* discharge asserted by the
    phase-close leg :func:`check_contract_exercised`: a contract may be statically
    called + tested yet still capture nothing at runtime (the exact blind spot
    behind EU-capture passing while writing zero rows). A bound contract that
    produced no row in its store during the phase reports this discharge missing.
    """

    NO_CALL_SITE = "no_call_site"
    NO_ASSERTING_TEST = "no_asserting_test"
    BOTH = "both"
    NO_RUNTIME_OUTPUT = "no_runtime_output"


@dataclass(frozen=True, slots=True)
class IdleContractFinding:
    """A newly-defined contract that ships idle (an orphan).

    Attributes:
        symbol: The contract symbol name (function / tier member / rule id).
        module: The repo-relative path of the file defining the contract.
        missing: Which discharge is absent -- no call-site, no asserting test,
            or both.
    """

    symbol: str
    module: str
    missing: MissingDischarge


@dataclass(frozen=True, slots=True)
class _ContractDef:
    """An added contract definition parsed out of a diff (internal).

    Attributes:
        symbol: The contract symbol name to chase for discharges.
        module: The repo-relative path of the defining file.
    """

    symbol: str
    module: str


#: A diff source: returns the unified-diff text for a rev range. Injected so
#: tests feed a synthetic diff without a git repo (mirrors how
#: :func:`check_idle_contract` injects ``profiles`` / ``resolve_fn``).
type DiffFn = Callable[[str], str]

#: A tree-scan source: returns the repo-relative paths of every source / test
#: file in the working tree. Injected alongside :data:`ReadFn` so tests feed a
#: synthetic tree without touching the real one.
type TreeFn = Callable[[], Sequence[str]]

#: A file-read source: returns the text of a repo-relative path. Injected so
#: the discharge scan reads the synthetic tree the test built.
type ReadFn = Callable[[str], str]


def _default_diff(diff_range: str) -> str:
    """Return the unified diff for *diff_range* via git.

    Args:
        diff_range: A git rev range (``HEAD~1..HEAD``) or a flag the diff
            subcommand accepts (``--cached``).

    Returns:
        The unified-diff text. Empty when the range has no changes.
    """
    proc = subprocess.run(
        ["git", "diff", "--unified=0", diff_range],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _default_tree() -> list[str]:
    """Return the repo-relative paths of tracked Python sources and tests.

    Returns:
        Every ``.py`` path under ``src/`` and ``tests/`` plus the ``tools/``
        gate scripts, repo-relative and sorted.
    """
    paths: list[str] = []
    for top in ("src", "tests", "tools"):
        root = _REPO_ROOT / top
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            paths.append(str(path.relative_to(_REPO_ROOT)))
    return sorted(paths)


def _default_read(path: str) -> str:
    """Return the text of repo-relative *path*, or empty if it is unreadable.

    Args:
        path: A repo-relative file path.

    Returns:
        The file text, or ``""`` when the file is absent (e.g. a path that was
        renamed away after the diff was taken).
    """
    target = _REPO_ROOT / path
    if not target.is_file():
        return ""
    return target.read_text(encoding="utf-8", errors="replace")


def _family_symbols_on_line(body: str) -> list[str]:
    """Return the contract symbols a single added source line defines.

    Recognizes a :data:`_CONTRACT_DEF_PATTERNS` ``def`` family (``check_*`` /
    ``*_gate`` / ``*_lint``) and an :data:`_ORACLE_TIER_RE` tier arm. A plain
    helper line matches nothing, so it yields an empty list.

    Args:
        body: The added line's text (the ``+`` prefix already stripped).

    Returns:
        The contract symbol names found on the line, in detection order.
    """
    symbols: list[str] = []
    for pattern in _CONTRACT_DEF_PATTERNS:
        family = pattern.match(body)
        if family is not None:
            symbols.append(family.group("sym"))
            break
    tier = _ORACLE_TIER_RE.search(body)
    if tier is not None:
        symbols.append(tier.group("sym"))
    return symbols


def _parse_added_contract_defs(diff_text: str) -> list[_ContractDef]:
    """Parse the contract-family symbols newly defined in *diff_text*.

    Only added lines (``+`` prefixed) under a :data:`_SOURCE_PREFIXES` path are
    considered, and only those that match a :data:`_CONTRACT_DEF_PATTERNS`
    family, a ``CheckKind``-runner decorator, an :data:`_ORACLE_TIER_RE` arm, or
    a new :data:`_LINT_MODULE_RE` module. Plain helpers never match, and added
    lines under ``tests/`` are skipped (a contract-shaped token in a test is a
    fixture, not a new contract), so neither becomes a finding.

    Args:
        diff_text: A unified diff (``git diff`` output).

    Returns:
        The de-duplicated contract definitions, in first-seen order.
    """
    parser = _DiffContractParser()
    for raw in diff_text.splitlines():
        parser.feed(raw)
    return parser.defs


@dataclass(slots=True)
class _DiffContractParser:
    """A line-at-a-time state machine that collects added contract defs.

    The diff walk is a small state machine: a ``--- /dev/null`` old-side marker
    flags the next file as a genuine addition, a ``+++ b/<path>`` header sets
    the file scope (and, when the file is newly added, may itself name a
    lint-module contract), then each added line in a source file is matched
    against the contract families. The ``CheckKind`` decorator/def pairing
    spans two lines, so the pending flag carries that one bit of state between
    :meth:`feed` calls.

    Attributes:
        defs: The de-duplicated contract definitions collected so far.
    """

    defs: list[_ContractDef] = field(default_factory=list)
    _seen: set[tuple[str, str]] = field(default_factory=set)
    _current_file: str = ""
    _pending_checkkind: bool = False
    _next_file_is_new: bool = False

    def feed(self, raw: str) -> None:
        """Advance the state machine by one raw diff line.

        Args:
            raw: A single line of unified-diff text.
        """
        if _NEW_FILE_OLD_SIDE_RE.match(raw) is not None:
            self._next_file_is_new = True
            return
        file_match = _DIFF_FILE_RE.match(raw)
        if file_match is not None:
            self._enter_file(file_match.group("path"))
            return

        # Only added lines in shipped source / gate scripts define a contract;
        # a contract-shaped token added under tests/ is a fixture, never a new
        # contract (this is what keeps the meta-gate from flagging itself).
        if not self._current_file.startswith(_SOURCE_PREFIXES):
            return
        added = _ADDED_LINE_RE.match(raw)
        if added is not None:
            self._feed_added(added.group("body"))

    def _enter_file(self, path: str) -> None:
        self._current_file = path
        self._pending_checkkind = False
        # A lint-module contract is keyed on the file path, so it is "new" only
        # when the module file itself is newly added -- editing an existing
        # eawf0##_*.py module adds no new rule-id contract.
        is_new = self._next_file_is_new
        self._next_file_is_new = False
        if not is_new:
            return
        lint_match = _LINT_MODULE_RE.match(path)
        if lint_match is not None:
            self._record(lint_match.group("sym"))

    def _feed_added(self, body: str) -> None:
        # A def on the line right after a CheckKind decorator is the registered
        # runner; a non-def line breaks the adjacency and falls through.
        if self._pending_checkkind:
            self._pending_checkkind = False
            decorated = _DECORATED_DEF_RE.match(body)
            if decorated is not None:
                self._record(decorated.group("sym"))
                return
        if _CHECKKIND_DECORATOR_RE.match(body) is not None:
            self._pending_checkkind = True
            return
        for symbol in _family_symbols_on_line(body):
            self._record(symbol)

    def _record(self, symbol: str) -> None:
        key = (symbol, self._current_file)
        if key not in self._seen:
            self._seen.add(key)
            self.defs.append(_ContractDef(symbol=symbol, module=self._current_file))


def _wired_through_own_main(symbol: str, defining_module: str, read_fn: ReadFn) -> bool:
    """Return whether a ``tools/`` gate script wires *symbol* through its own ``main``.

    A ``src/`` contract proves it runs via a production caller in another module,
    but a ``tools/`` gate script's production caller IS its own ``main`` (the
    pre-commit hook invokes ``python tools/<gate>.py``, which runs ``main``).
    A gate-check function referenced inside that module's ``main`` body is
    therefore genuinely wired on, not idle -- so this same-module wiring counts
    as a call-site for a ``tools/`` script (and only there; a ``src/`` module's
    ``main`` does not, since shipped contracts must run from production code).

    Args:
        symbol: The contract symbol to chase.
        defining_module: The repo-relative path of the file that defines it.
        read_fn: Reader for a repo-relative path.

    Returns:
        ``True`` when *defining_module* is a ``tools/`` script whose ``main``
        function body references *symbol*.
    """
    if not defining_module.startswith("tools/"):
        return False
    text = read_fn(defining_module)
    main_match = re.search(r"^def main\(", text, flags=re.MULTILINE)
    if main_match is None:
        return False
    # The main body runs to the next top-level def/class or the module guard.
    tail = text[main_match.start() :]
    end_match = re.search(r"\n(?:def |class |if __name__)", tail[1:])
    main_body = tail if end_match is None else tail[: end_match.start() + 1]
    needle = re.compile(rf"\b{re.escape(symbol)}\b")
    # The def line itself is not a call; only a reference in the body counts.
    body_after_signature = main_body.split("\n", 1)[1] if "\n" in main_body else ""
    return needle.search(body_after_signature) is not None


def _node_references_symbol(node: ast.AST, symbol: str) -> bool:
    """Return whether a single AST *node* references *symbol* in code.

    A ``Name`` load, an attribute access, or an import (``from x import symbol``,
    ``import x as symbol``, ``import symbol...``) is a real code reference; every
    other node is not. Split out of :func:`_ast_references_symbol` so the walk
    stays a flat ``any(...)`` and the per-node branching stays readable.

    Args:
        node: One node from an :func:`ast.walk` traversal.
        symbol: The symbol name to chase.

    Returns:
        ``True`` when *node* references *symbol* by name in code.
    """
    if isinstance(node, ast.Name):
        return node.id == symbol
    if isinstance(node, ast.Attribute):
        return node.attr == symbol
    if isinstance(node, ast.ImportFrom):
        return any(symbol in (alias.name, alias.asname) for alias in node.names)
    if isinstance(node, ast.Import):
        # ``import x as symbol`` binds *symbol*; ``import a.b.c`` binds the
        # top-level ``a``, so match the first dotted segment too.
        return any(
            alias.asname == symbol or alias.name.split(".", 1)[0] == symbol for alias in node.names
        )
    return False


def _ast_references_symbol(source: str, symbol: str) -> bool:
    """Return whether *symbol* appears as a real code reference in *source*.

    Parses *source* to an AST and reports a hit when *symbol* appears as an
    imported name, a ``Name`` load, or an attribute access -- references that
    prove the symbol is USED in code. A mention inside a docstring or a comment
    is a string literal (or stripped entirely by the tokenizer), so it never
    becomes an AST name node and is correctly ignored. This is the whole point
    of the AST probe over the prior word-boundary regex: a ``:func:`symbol```
    cross-reference in a docstring used to match the regex and falsely discharge
    the call-site contract, so a symbol whose only non-test reference was
    documentation looked "called" when nothing ran it.

    Args:
        source: Python source text.
        symbol: The symbol name to chase.

    Returns:
        ``True`` when *symbol* is imported, called, or otherwise referenced by
        name in code; ``False`` when it is absent, appears only in a string /
        comment, or *source* does not parse.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # An unparseable file cannot be shown to reference the symbol; treat it
        # as no reference rather than crashing the gate on one bad file.
        return False
    return any(_node_references_symbol(node, symbol) for node in ast.walk(tree))


def _has_call_site(symbol: str, defining_module: str, tree: Iterable[str], read_fn: ReadFn) -> bool:
    """Return whether *symbol* is referenced in a non-test file other than its module.

    A call-site outside the defining module proves the contract runs in
    production (a same-module self-reference does not discharge a ``src/``
    contract, and a test reference is counted separately as the asserting-test
    discharge). The one same-module exception is a ``tools/`` gate script that
    wires the check through its own ``main`` -- that IS the production caller for
    a gate script (see :func:`_wired_through_own_main`).

    Detection is AST-based (:func:`_ast_references_symbol`): only a call, import,
    or name reference in code counts, so a comment or a docstring mention no
    longer discharges the contract the way the prior word-boundary regex did.

    Args:
        symbol: The contract symbol to chase.
        defining_module: The repo-relative path of the file that defines it.
        tree: The repo-relative paths to scan.
        read_fn: Reader for a repo-relative path.

    Returns:
        ``True`` when some non-test, non-defining file references *symbol* in
        code, or when a ``tools/`` gate script wires it through its own ``main``.
    """
    if _wired_through_own_main(symbol, defining_module, read_fn):
        return True
    for path in tree:
        if path == defining_module:
            continue
        if path.startswith("tests/") or "/tests/" in path:
            continue
        if _ast_references_symbol(read_fn(path), symbol):
            return True
    return False


def _test_function_references_symbol(source: str, symbol: str) -> bool:
    """Return whether a ``test_*`` function in *source* references *symbol* in code.

    Args:
        source: Python source text of a test module.
        symbol: The symbol name to chase.

    Returns:
        ``True`` when some ``test_*`` function (its body or decorators) names
        *symbol*; ``False`` when *source* does not parse or only a comment,
        string, or non-test helper mentions it.
    """
    try:
        module = ast.parse(source)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
        and any(_node_references_symbol(inner, symbol) for inner in ast.walk(node))
        for node in ast.walk(module)
    )


def _has_asserting_test(symbol: str, tree: Iterable[str], read_fn: ReadFn) -> bool:
    """Return whether a test function under ``tests/`` exercises *symbol*.

    Only a code reference inside a ``test_*`` function discharges the contract:
    a comment, a docstring, or a helper that no test calls gives a regression
    nowhere to fail, so none of those count.

    Args:
        symbol: The contract symbol to chase.
        tree: The repo-relative paths to scan.
        read_fn: Reader for a repo-relative path.

    Returns:
        ``True`` when some ``tests/`` file has a ``test_*`` function that
        references *symbol* in code.
    """
    needle = re.compile(rf"\b{re.escape(symbol)}\b")
    for path in tree:
        if not (path.startswith("tests/") or "/tests/" in path):
            continue
        source = read_fn(path)
        # The text pre-filter keeps the AST parse to the few files that could match.
        if needle.search(source) and _test_function_references_symbol(source, symbol):
            return True
    return False


def detect_idle_contracts(
    diff_range: str = "--cached",
    *,
    diff_fn: DiffFn = _default_diff,
    tree_fn: TreeFn = _default_tree,
    read_fn: ReadFn = _default_read,
) -> list[IdleContractFinding]:
    """Flag every newly-defined contract in *diff_range* that ships idle.

    The meta-gate parses the contract-family symbols a diff adds (see
    :func:`_parse_added_contract_defs`), then for each chases two discharges in
    the resulting working tree: a call-site outside the defining module
    (proves it runs) and an asserting test that references it (proves a
    regression is caught). A contract missing either discharge is an orphan and
    yields one :class:`IdleContractFinding`; a contract with BOTH yields none.

    The diff, tree, and read sources are injectable so tests feed a synthetic
    diff + tree without a git repo, mirroring how :func:`check_idle_contract`
    injects its ``profiles`` / ``resolve_fn``. The function mutates nothing --
    it only reads.

    Args:
        diff_range: A git rev range (``HEAD~1..HEAD``) or the staged-diff flag
            (``--cached``, the default so the pre-commit hook needs no args).
        diff_fn: Diff source; defaults to ``git diff --unified=0 <range>``.
        tree_fn: Tree-scan source; defaults to the tracked ``src`` / ``tests``
            / ``tools`` Python files.
        read_fn: File reader; defaults to reading the working-tree file.

    Returns:
        One :class:`IdleContractFinding` per orphan contract, in the order the
        defs appear in the diff. Empty when every added contract is discharged
        (or the diff adds no contract).
    """
    contract_defs = _parse_added_contract_defs(diff_fn(diff_range))
    if not contract_defs:
        return []

    tree = list(tree_fn())
    findings: list[IdleContractFinding] = []
    for contract in contract_defs:
        has_call = _has_call_site(contract.symbol, contract.module, tree, read_fn)
        has_test = _has_asserting_test(contract.symbol, tree, read_fn)
        if has_call and has_test:
            continue
        if not has_call and not has_test:
            missing = MissingDischarge.BOTH
        elif not has_call:
            missing = MissingDischarge.NO_CALL_SITE
        else:
            missing = MissingDischarge.NO_ASSERTING_TEST
        findings.append(
            IdleContractFinding(
                symbol=contract.symbol,
                module=contract.module,
                missing=missing,
            )
        )
    return findings
