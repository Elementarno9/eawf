"""Allowlist lint: only the sandbox child and the gate runner reach ``run_checks``.

:func:`eawf.workflow.audit_dsl.runner.run_checks` executes checks in the
calling process. A check is routinely a whole test suite that drives eawf's
own RPCs, so run in process it reaches whichever runtime directory and ledger
that process points at -- for the daemon or a CLI verb, the live pair. The two
allowlisted modules are the child-interpreter entry points that run it inside
a throwaway sandbox; every other caller goes through
:func:`eawf.workflow.verify.sandboxed_checks.run_checks_out_of_process`.

The sweep is static (AST over every module under ``src/eawf``), so a new
in-process caller reds here before it ships. It follows every way a module
can reach the function: the imported name, an import alias, and an attribute
on an imported module (``runner.run_checks``). Handing the function over
uncalled (``asyncio.to_thread(run_checks, ...)``) still runs it in process, so
a bare reference counts as a call.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

import eawf

pytestmark = pytest.mark.unit

_SRC_ROOT = Path(eawf.__file__).resolve().parent.parent
"""Absolute path of the ``src/`` tree holding the installed ``eawf`` package."""

TARGET = "run_checks"
"""The in-process runner the lint confines."""

ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        "eawf/workflow/verify/sandboxed_checks.py",
        "eawf/runtime/daemon/gate_execution.py",
    }
)
"""``src``-relative modules allowed to reach :data:`TARGET`: the two child entry points."""


@dataclass(frozen=True)
class RunChecksReference:
    """One place a module reaches the in-process runner.

    Attributes:
        path: ``src``-relative POSIX path of the module.
        lineno: 1-based line of the reference.
    """

    path: str
    lineno: int


def run_checks_linenos(source: str) -> list[int]:
    """Return the 1-based lines where *source* reaches :data:`TARGET`, sorted.

    An import that binds the function is not itself a reference, so a package
    that only re-exports it stays clean; every later use of the bound name
    (or of any alias it was bound under) is one.

    Args:
        source: Python module text.

    Returns:
        The sorted, de-duplicated line numbers of every reference.

    Raises:
        SyntaxError: *source* is not parseable. The lint fails closed rather
            than skipping a module it cannot read.
    """
    tree = ast.parse(source)
    names = {TARGET}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names if alias.name == TARGET)
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if (isinstance(node, ast.Name) and node.id in names and isinstance(node.ctx, ast.Load))
            or (isinstance(node, ast.Attribute) and node.attr == TARGET)
        }
    )


def scan_tree(src_root: Path) -> list[RunChecksReference]:
    """Return every reference to :data:`TARGET` outside :data:`ALLOWED_MODULES`.

    Args:
        src_root: Directory holding the ``eawf`` package to sweep.

    Returns:
        The offending references, ordered by path then line.
    """
    found: list[RunChecksReference] = []
    for module in sorted((src_root / "eawf").rglob("*.py")):
        relative = module.relative_to(src_root).as_posix()
        if relative in ALLOWED_MODULES:
            continue
        found.extend(
            RunChecksReference(path=relative, lineno=lineno)
            for lineno in run_checks_linenos(module.read_text(encoding="utf-8"))
        )
    return found


def assert_no_in_process_callers(src_root: Path) -> None:
    """Raise :class:`AssertionError` naming every reference outside the allowlist."""
    offenders = scan_tree(src_root)
    assert not offenders, (
        "in-process run_checks reached outside the sandbox child and the gate runner; "
        "call run_checks_out_of_process instead: "
        + ", ".join(f"{ref.path}:{ref.lineno}" for ref in offenders)
    )


def _plant(src_root: Path, relative: str, source: str) -> Path:
    """Write *source* at ``src_root / relative`` and return the file path."""
    target = src_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


_SEEDED_VIOLATION = """\
from pathlib import Path

from eawf.workflow.audit_dsl.runner import load_spec, run_checks


def audit(spec: Path) -> int:
    results = run_checks(load_spec(spec), cwd=spec.parent)
    return len(results)
"""


# ---- the source tree ---------------------------------------------------------


def test_scan_tree_source_tree_is_clean() -> None:
    """The shipped tree reaches the in-process runner only from the two child entry points."""
    assert_no_in_process_callers(_SRC_ROOT)


@pytest.mark.parametrize("relative", sorted(ALLOWED_MODULES))
def test_run_checks_linenos_allowlisted_module_still_calls_the_runner(relative: str) -> None:
    """Each allowlist entry exists and still reaches the runner.

    An entry that stopped matching (the child entry point moved or was renamed)
    would silently exempt nothing and hide that the allowlist drifted.
    """
    module = _SRC_ROOT / relative

    assert module.is_file(), f"{relative}: allowlisted module is gone; update ALLOWED_MODULES"
    assert run_checks_linenos(module.read_text(encoding="utf-8")), (
        f"{relative}: allowlisted module no longer reaches {TARGET}; update ALLOWED_MODULES"
    )


# ---- seeded violations -------------------------------------------------------


def test_scan_tree_flags_seeded_violation(tmp_path: Path) -> None:
    """A module outside the allowlist that calls the runner reds the lint at its line."""
    _plant(tmp_path, "eawf/workflow/skills/rogue.py", _SEEDED_VIOLATION)

    assert scan_tree(tmp_path) == [
        RunChecksReference(path="eawf/workflow/skills/rogue.py", lineno=7)
    ]
    with pytest.raises(AssertionError, match=r"eawf/workflow/skills/rogue\.py:7"):
        assert_no_in_process_callers(tmp_path)


def test_scan_tree_allowlisted_path_may_call_the_runner(tmp_path: Path) -> None:
    """The same call planted at an allowlisted path is exempt."""
    _plant(tmp_path, "eawf/workflow/verify/sandboxed_checks.py", _SEEDED_VIOLATION)
    _plant(tmp_path, "eawf/runtime/daemon/gate_execution.py", _SEEDED_VIOLATION)

    assert scan_tree(tmp_path) == []


def test_scan_tree_empty_tree_is_clean(tmp_path: Path) -> None:
    """Boundary: a package with no modules has nothing to flag."""
    (tmp_path / "eawf").mkdir()

    assert scan_tree(tmp_path) == []


def test_run_checks_linenos_flags_an_import_alias() -> None:
    """Renaming the import does not hide the call."""
    source = "from eawf.workflow.audit_dsl import run_checks as run_inline\n\nrun_inline([])\n"

    assert run_checks_linenos(source) == [3]


def test_run_checks_linenos_flags_a_module_attribute_call() -> None:
    """Reaching the runner through its module is still a call."""
    source = "from eawf.workflow.audit_dsl import runner\n\nrunner.run_checks([])\n"

    assert run_checks_linenos(source) == [3]


def test_run_checks_linenos_flags_an_uncalled_reference() -> None:
    """Handing the function to a thread runs it in process all the same."""
    source = (
        "import asyncio\n"
        "\n"
        "from eawf.workflow.audit_dsl.runner import run_checks\n"
        "\n"
        "\n"
        "async def go(specs):\n"
        "    return await asyncio.to_thread(run_checks, specs)\n"
    )

    assert run_checks_linenos(source) == [7]


def test_run_checks_linenos_ignores_a_bare_re_export() -> None:
    """Binding the name for re-export, as ``audit_dsl/__init__`` does, is not a use."""
    source = (
        "from eawf.workflow.audit_dsl.runner import load_spec, run_checks\n"
        "\n"
        '__all__ = ["load_spec", "run_checks"]\n'
    )

    assert run_checks_linenos(source) == []


def test_run_checks_linenos_ignores_the_sandboxed_runner() -> None:
    """The sanctioned entry point shares a prefix with the target but is not it."""
    source = (
        "from eawf.workflow.verify.sandboxed_checks import run_checks_out_of_process\n"
        "\n"
        "run_checks_out_of_process([], cwd=None)\n"
    )

    assert run_checks_linenos(source) == []


def test_run_checks_linenos_rejects_unparseable_source() -> None:
    """Error path: a module the lint cannot parse fails the sweep instead of passing it."""
    with pytest.raises(SyntaxError):
        run_checks_linenos("def broken(:\n")
