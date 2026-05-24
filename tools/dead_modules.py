"""Dead-module advisory: report ``src/eawf`` modules nothing imports.

Report-only — this script always exits ``0`` and never fails a build. It
walks the AST of every module under ``src/eawf``, builds the internal
import graph, and lists modules that no other in-tree module reaches.
Such a module is a *candidate* for deletion or for a missing wiring, not
a proven dead file; a human decides.

Two reachability sources feed the graph so the advisory's false-positive
rate stays low:

* **Static import edges** — ``import eawf.x`` and ``from eawf.x import y``
  (the latter records both ``eawf.x`` and a possible ``eawf.x.y``
  submodule).
* **String-literal module references** — any string literal matching a
  known ``eawf.*`` dotted module path. This is what catches the
  declarative CLI registry (``eawf.cli.registry`` resolves each command
  module via ``importlib.import_module(row.module)`` where ``row.module``
  is data, so there is no static edge) plus any other ``import_module``
  / plugin-discovery indirection.

Reaching a module also reaches its ancestor packages, so a package
``__init__`` is never flagged merely because callers import its
submodules rather than the package directly.

An allowlist covers the surfaces reached without any in-tree reference:
the console-script entry points and the package root.

Usage::

    uv run python tools/dead_modules.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
PKG_ROOT = SRC_ROOT / "eawf"

# Surfaces reached without an in-tree import edge: the pyproject console
# scripts (eawf/ea -> cli.app, eawfd -> daemon.main), the ``python -m
# eawf`` entry, the single-source version literal read by hatchling, and
# the package root imported as ``import eawf``.
ALLOWLIST: frozenset[str] = frozenset(
    {
        "eawf",
        "eawf.__main__",
        "eawf._version",
        "eawf.cli.app",
        "eawf.daemon.main",
    }
)

_DOTTED_EAWF = re.compile(r"^eawf(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+$")


def _module_name(path: Path) -> str:
    """Return the dotted module name for a ``.py`` file under ``src``."""
    rel = path.relative_to(SRC_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _all_modules() -> dict[str, Path]:
    """Map every in-tree dotted module name to its source path."""
    modules: dict[str, Path] = {}
    for path in sorted(PKG_ROOT.rglob("*.py")):
        modules[_module_name(path)] = path
    return modules


def _static_edges(tree: ast.AST) -> set[str]:
    """Collect ``eawf.*`` module names referenced by import statements."""
    edges: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "eawf" or alias.name.startswith("eawf."):
                    edges.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — resolved separately below
                continue
            mod = node.module or ""
            if mod == "eawf" or mod.startswith("eawf."):
                edges.add(mod)
                for alias in node.names:
                    edges.add(f"{mod}.{alias.name}")
    return edges


def _relative_edges(tree: ast.AST, module: str) -> set[str]:
    """Resolve ``from . import x`` / ``from .y import z`` to absolute names."""
    edges: set[str] = set()
    pkg_parts = module.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = pkg_parts[: len(pkg_parts) - node.level + 1]
            prefix = ".".join(base + ([node.module] if node.module else []))
            edges.add(prefix)
            for alias in node.names:
                edges.add(f"{prefix}.{alias.name}")
    return edges


def _string_edges(tree: ast.AST) -> set[str]:
    """Collect string literals that name a dotted ``eawf.*`` module."""
    edges: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _DOTTED_EAWF.match(node.value)
        ):
            edges.add(node.value)
    return edges


def _with_ancestors(names: set[str]) -> set[str]:
    """Expand each dotted name to include its ancestor packages."""
    out: set[str] = set()
    for name in names:
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            out.add(".".join(parts[:i]))
    return out


def find_dead_modules() -> list[str]:
    """Return sorted in-tree modules nothing reaches (minus the allowlist)."""
    modules = _all_modules()
    reached: set[str] = set()
    for name, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        reached |= _static_edges(tree)
        reached |= _relative_edges(tree, name)
        reached |= _string_edges(tree)
    reached = _with_ancestors(reached)
    dead = [name for name in modules if name not in reached and name not in ALLOWLIST]
    return sorted(dead)


def main() -> int:
    """Print the advisory report. Always returns ``0`` (report-only)."""
    dead = find_dead_modules()
    if not dead:
        print("dead-module advisory: no inbound-zero modules found")
        return 0
    print(f"dead-module advisory: {len(dead)} inbound-zero module(s)")
    print("(report-only — each is a candidate for deletion or missing wiring)")
    for name in dead:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
