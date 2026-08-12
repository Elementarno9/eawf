"""Report public functions in ``src/`` that nothing in ``src/`` calls.

Coverage cannot see this class of defect: a function with a thorough unit test
and no production caller reads as fully covered. The symptom is shipped
surface that never runs — a renderer nothing renders with, a producer that
writes no rows.

Usage::

    uv run python tools/idle_surface_report.py
    uv run python tools/idle_surface_report.py --ceiling 525
"""

from __future__ import annotations

import argparse
import ast
import collections
import re
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

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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


def find_idle_functions(source_root: Path) -> list[tuple[str, Path]]:
    """Return ``(name, defining_file)`` for public functions no sibling names.

    A name mentioned in exactly one file under *source_root* is mentioned only
    where it is defined: no caller, no re-export, no type annotation elsewhere.
    Names defined more than once are skipped — the mention count cannot be
    attributed to one definition.
    """
    identifiers_by_file: dict[Path, set[str]] = {}
    definitions: dict[str, list[Path]] = collections.defaultdict(list)

    for path in sorted(source_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        identifiers_by_file[path] = set(_IDENTIFIER.findall(text))
        for node in ast.parse(text).body:
            is_function = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            if is_function and not node.name.startswith("_") and not _framework_owned(node):
                definitions[node.name].append(path)

    mentions: collections.Counter[str] = collections.Counter()
    for identifiers in identifiers_by_file.values():
        mentions.update(identifiers)

    return sorted(
        (name, paths[0])
        for name, paths in definitions.items()
        if len(paths) == 1 and mentions[name] == 1
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
