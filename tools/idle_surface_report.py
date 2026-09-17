"""Report public functions in ``src/`` that no production caller reaches.

Coverage cannot see this class of defect: a function with a thorough unit test
and no production caller reads as fully covered. The symptom is shipped
surface that never runs — a renderer nothing renders with, a producer that
writes no rows.

Production is not only ``src/``. A repo script under ``tools/`` is a real
consumer, so its call sites count: a reporter blind to them files a wired
function as idle and the ceiling then measures the scan, not the surface.
Caller roots contribute references only -- a function DEFINED in one is not
reported, because a script's own helpers are not shipped surface.

Usage::

    uv run python tools/idle_surface_report.py
    uv run python tools/idle_surface_report.py --ceiling 202
    uv run python tools/idle_surface_report.py --caller-root tools
"""

from __future__ import annotations

import argparse
import ast
import collections
import sys
from collections.abc import Sequence
from pathlib import Path

#: Repo directories that hold production callers but no shipped surface.
#: Used as the default caller roots so a ``tools/`` script counts as a caller.
_DEFAULT_CALLER_ROOTS: tuple[Path, ...] = (Path("tools"),)

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


def find_idle_functions(
    source_root: Path,
    *,
    caller_roots: Sequence[Path] = (),
) -> list[tuple[str, Path]]:
    """Return ``(name, defining_file)`` for public functions nothing calls.

    A name is idle when no call site names it anywhere under *source_root* or
    *caller_roots*, outside the body of its own definition. Counting *sites*
    rather than *files mentioning the name* is what makes a helper called only
    inside its own defining module non-idle; the file-level proxy reported
    every such helper, because its definition and its caller share one file.

    References from within the function's own body do not count: a recursive
    call is not evidence that anything reaches the function.

    Names defined more than once are skipped — the reference count cannot be
    attributed to one definition.

    Args:
        source_root: Tree whose public functions are the reported surface.
            Its modules supply both definitions and references.
        caller_roots: Extra trees scanned for references only, such as
            ``tools/``. A definition inside one is never reported.

    Returns:
        The idle rows, sorted by name.

    Raises:
        NotADirectoryError: A caller root does not exist. A missing root
            would scan as empty and silently report wired functions as idle.
        SyntaxError: A scanned module does not parse. Reading a broken file
            as empty would silently under-count references.
    """
    for caller_root in caller_roots:
        if not caller_root.is_dir():
            raise NotADirectoryError(f"caller root is not a directory: {caller_root}")

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

    for caller_root in caller_roots:
        for path in sorted(caller_root.rglob("*.py")):
            references.update(_reference_counts(ast.parse(path.read_text(encoding="utf-8"))))

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
    parser.add_argument("--caller-root", type=Path, action="append", dest="caller_roots")
    args = parser.parse_args(argv[1:])

    caller_roots = args.caller_roots if args.caller_roots else list(_DEFAULT_CALLER_ROOTS)
    idle = find_idle_functions(args.source_root, caller_roots=caller_roots)
    for name, path in idle:
        print(f"{path}:{name}")
    print(f"\n{len(idle)} public function(s) with no caller under {args.source_root}")

    if args.ceiling is not None and len(idle) > args.ceiling:
        print(f"ceiling {args.ceiling} exceeded", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
