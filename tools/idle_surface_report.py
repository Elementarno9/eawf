"""Report public functions in ``src/`` that nothing in ``src/`` calls.

Coverage cannot see this class of defect: a function with a thorough unit test
and no production caller reads as fully covered. The symptom is shipped
surface that never runs — a renderer nothing renders with, a producer that
writes no rows.

Usage::

    uv run python tools/idle_surface_report.py
    uv run python tools/idle_surface_report.py --ceiling 202
"""

from __future__ import annotations

import argparse
import ast
import collections
import sys
from pathlib import Path

#: Decorator name fragments that mean "the caller is a framework, not our code".
#: A Typer handler or a registry entry is referenced only by its decorator, so
#: reference-counting alone would report every one of them as idle.
_FRAMEWORK_DECORATORS: tuple[str, ...] = (
    "command",
    "callback",
    "register",
    "hookimpl",
    "validator",
    "property",
)


def _decorator_path(node: ast.expr) -> str:
    """Return a decorator's dotted name (``app.command`` for ``@app.command()``)."""
    while isinstance(node, ast.Call):
        node = node.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _framework_owned(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when a framework decorator supplies the call site."""
    return any(
        fragment in _decorator_path(decorator)
        for decorator in node.decorator_list
        for fragment in _FRAMEWORK_DECORATORS
    )


def _reference_counts(tree: ast.AST) -> collections.Counter[str]:
    """Count every load-context use of a name anywhere under *tree*.

    Attribute access is counted under the attribute name, so a qualified
    ``module.render()`` reaches the ``render`` definition the same way a bare
    ``render()`` does. Import aliases and ``__all__`` strings are deliberately
    not references: re-exporting a function is not calling it, and treating a
    re-export as a caller is what would let dead surface hide behind a package
    ``__init__``.
    """
    counts: collections.Counter[str] = collections.Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            counts[node.id] += 1
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            counts[node.attr] += 1
    return counts


def find_idle_functions(source_root: Path) -> list[tuple[str, Path]]:
    """Return ``(name, defining_file)`` for public functions nothing calls.

    A name is idle when no call site names it anywhere under *source_root*
    outside the body of its own definition. Counting *sites* rather than
    *files mentioning the name* is what makes a helper called only inside its
    own defining module non-idle; the file-level proxy reported every such
    helper, because its definition and its caller share one file.

    References from within the function's own body do not count: a recursive
    call is not evidence that anything reaches the function.

    Names defined more than once are skipped — the reference count cannot be
    attributed to one definition.
    """
    definitions: dict[str, list[Path]] = collections.defaultdict(list)
    references: collections.Counter[str] = collections.Counter()
    self_references: collections.Counter[str] = collections.Counter()

    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        references.update(_reference_counts(tree))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name.startswith("_") or _framework_owned(node):
                continue
            definitions[node.name].append(path)
            self_references[node.name] += _reference_counts(node)[node.name]

    return sorted(
        (name, paths[0])
        for name, paths in definitions.items()
        if len(paths) == 1 and references[name] - self_references[name] == 0
    )


def main(argv: list[str]) -> int:
    """Print the idle set; exit 1 when a ``--ceiling`` is given and exceeded."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("src/eawf"))
    parser.add_argument("--ceiling", type=int, default=None)
    args = parser.parse_args(argv[1:])

    idle = find_idle_functions(args.source_root)
    for name, path in idle:
        print(f"{path}:{name}")
    print(f"\n{len(idle)} public function(s) with no caller under {args.source_root}")

    if args.ceiling is not None and len(idle) > args.ceiling:
        print(f"ceiling {args.ceiling} exceeded", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
