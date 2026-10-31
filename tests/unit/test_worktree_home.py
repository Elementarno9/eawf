"""Tests for the KISS-007 cycle break in the worktree package.

Sibling modules in ``src/eawf/runtime/worktree/`` import
``eawf.runtime.worktree.git`` *directly* (``import eawf.runtime.worktree.git
as git``), never via the ``from eawf.runtime.worktree import git``
trampoline. The latter forms a re-export cycle through ``__init__.py`` that
future additions can deadlock on. The static AST sweep here is the
regression guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "eawf" / "runtime" / "worktree"


# ---- KISS-007 cycle break ---------------------------------------------------


def _collect_worktree_internal_imports(module_path: Path) -> list[str]:
    """Return every ``eawf.runtime.worktree.*`` module name *module_path* imports.

    Two import forms are recognised:

    - ``from eawf.runtime.worktree.X import ...``  — direct submodule form.
    - ``from eawf.runtime.worktree import X``       — trampoline form.
    - ``import eawf.runtime.worktree.X as ...``     — direct submodule form.
    - ``import eawf.runtime.worktree.X``            — direct submodule form.

    Trampoline-form imports are tagged with the ``"TRAMPOLINE:"`` prefix
    so the assertion can distinguish them.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "eawf.runtime.worktree":
                # ``from eawf.runtime.worktree import git`` — trampoline.
                for alias in node.names:
                    hits.append(f"TRAMPOLINE:{alias.name}")
            elif node.module and node.module.startswith("eawf.runtime.worktree."):
                hits.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("eawf.runtime.worktree."):
                    hits.append(alias.name)
    return hits


@pytest.mark.parametrize(
    "module_name",
    ["tree_change.py"],
)
def test_sibling_modules_avoid_trampoline_import(module_name: str) -> None:
    """No sibling submodule imports ``git`` via the ``__init__`` trampoline.

    KISS-007: ``from eawf.runtime.worktree import git`` is the trampoline form.
    Switching every sibling to ``import eawf.runtime.worktree.git as git``
    bypasses ``__init__.py`` and removes the cycle source.
    """
    module_path = _SRC_ROOT / module_name
    assert module_path.is_file(), f"missing sibling module: {module_path}"
    hits = _collect_worktree_internal_imports(module_path)
    trampolines = [h for h in hits if h.startswith("TRAMPOLINE:")]
    assert trampolines == [], (
        f"KISS-007 violation in {module_name}: trampoline imports detected "
        f"({trampolines}); use ``import eawf.runtime.worktree.X as X`` or "
        f"``from eawf.runtime.worktree.X import ...`` instead"
    )


def test_internal_cycle_detector_finds_no_cycles() -> None:
    """Static walk of ``src/eawf/runtime/worktree/`` reports zero internal cycles.

    Builds the directed graph (module -> imported sibling) from the
    AST and runs Tarjan's SCC. A "cycle" is any SCC of size >= 2 or a
    self-loop. Module ``__init__.py`` is excluded from the graph —
    package import is a normal entry-point, not an internal sibling
    edge.
    """
    modules = {p.stem: p for p in _SRC_ROOT.iterdir() if p.suffix == ".py" and p.stem != "__init__"}
    edges: dict[str, set[str]] = {name: set() for name in modules}
    for name, module_path in modules.items():
        for imp in _collect_worktree_internal_imports(module_path):
            # Strip trampolines: the prior test asserts they are absent.
            if imp.startswith("TRAMPOLINE:"):
                continue
            # ``eawf.runtime.worktree.X`` -> ``X``.
            target = imp.split(".")[-1]
            if target in modules and target != name:
                edges[name].add(target)

    cycles = _tarjan_cycles(edges)
    assert cycles == [], f"internal cycles detected in src/eawf/runtime/worktree/: {cycles}"


def _tarjan_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return every strongly-connected component of size >= 2.

    Self-loops (``x -> x``) are also reported. A graph with no cycles
    returns the empty list.
    """
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    result: list[list[str]] = []

    def strongconnect(node: str) -> None:
        indices[node] = index_counter[0]
        lowlinks[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for successor in graph.get(node, ()):
            if successor not in indices:
                strongconnect(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == node:
                    break
            if len(component) > 1 or (len(component) == 1 and node in graph.get(node, ())):
                result.append(sorted(component))

    for node in graph:
        if node not in indices:
            strongconnect(node)
    return result
