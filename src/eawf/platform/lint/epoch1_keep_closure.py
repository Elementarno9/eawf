"""Keep the epoch-1 sweep from regrowing.

The migration keep closure is every module the migration path from an
epoch-1 tree reaches through its module-level imports, starting from the
roots a closure file names: the v1 chain to schema 1.20, the epoch-2
cutover, the ``migrate`` verb, and the pre-cutover ``session`` and
``worktree`` verbs with the flag-day tables. The same file lists the v1-only
modules: those in the closure that nothing outside it imports, plus the
epoch-1 modules the sweep deleted.

A module outside the closure that imports a v1-only module fails the lint.
Live code that leans on a migration-only module would keep the closure from
ever being deleted, and an import of a swept module is the sweep regrowing.
Lazy imports inside functions count, because they are dependencies all the
same; the closure itself follows only module-level imports, so a lazy import
in a closure module does not drag a live module into the closure.

Usage::

    uv run python -m eawf.platform.lint.epoch1_keep_closure
    uv run python -m eawf.platform.lint.epoch1_keep_closure --closure PATH --src-root src
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

#: The closure file the pre-commit hook checks the tree against.
DEFAULT_CLOSURE_PATH: Final = Path("tests/fixtures/epoch1-keep-closure.json")


class KeepClosure(BaseModel):
    """The migration keep closure's roots and the modules only it may import.

    Attributes:
        roots: Dotted names of the modules or packages the migration path
            starts from; a package name roots every module under it.
        v1_only: Dotted names of the modules or packages only the closure may
            import, including epoch-1 modules that no longer exist.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    roots: tuple[str, ...] = Field(min_length=1)
    v1_only: tuple[str, ...] = Field(min_length=1)


@dataclass(frozen=True)
class ClosureViolation:
    """One import of a v1-only module from outside the keep closure.

    Attributes:
        importer: Dotted name of the module outside the closure.
        imported: Dotted name of the v1-only module it imports.
    """

    importer: str
    imported: str

    def render(self) -> str:
        """Return the one-line finding the hook prints."""
        return (
            f"{self.importer} imports {self.imported}, which only the epoch-1 "
            "migration closure may import"
        )


def load_keep_closure(path: Path) -> KeepClosure:
    """Read and validate the closure file at *path*.

    Raises:
        FileNotFoundError: *path* does not exist.
        pydantic.ValidationError: The file is not a valid closure document.
    """
    return KeepClosure.model_validate_json(path.read_bytes())


def module_names(src_root: Path) -> dict[str, Path]:
    """Map every module under *src_root* to its file, by dotted name.

    A package is named by its ``__init__.py``.
    """
    names: dict[str, Path] = {}
    for path in sorted(src_root.rglob("*.py")):
        parts = list(path.relative_to(src_root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            names[".".join(parts)] = path
    return names


def imported_names(
    source: str, *, module: str, is_package: bool, module_level_only: bool
) -> frozenset[str]:
    """Return the dotted names *source* imports, relative imports resolved.

    ``from a import b`` yields both ``a`` and ``a.b``, since ``b`` may be a
    submodule; a caller keeps whichever names it recognises.

    Args:
        source: The module's text.
        module: The module's dotted name, which anchors relative imports.
        is_package: Whether the module is a package ``__init__``.
        module_level_only: Skip imports nested in a function or class body.

    Raises:
        SyntaxError: *source* is not Python.
    """
    tree = ast.parse(source)
    nodes: Iterable[ast.AST] = (
        _module_level_statements(tree.body) if module_level_only else ast.walk(tree)
    )
    names: set[str] = set()
    package = module.split(".") if is_package else module.split(".")[:-1]
    for node in nodes:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join([*anchor, node.module] if node.module else anchor)
            else:
                base = node.module or ""
            if base:
                names.add(base)
                names.update(f"{base}.{alias.name}" for alias in node.names)
    return frozenset(names)


def _module_level_statements(body: Sequence[ast.stmt]) -> list[ast.stmt]:
    """Return *body*'s statements, descending into ``if`` and ``try`` blocks only."""
    out: list[ast.stmt] = []
    pending = list(body)
    while pending:
        node = pending.pop()
        out.append(node)
        if isinstance(node, ast.If):
            pending.extend([*node.body, *node.orelse])
        elif isinstance(node, ast.Try):
            pending.extend([*node.body, *node.orelse, *node.finalbody])
            for handler in node.handlers:
                pending.extend(handler.body)
    return out


def _is_within(name: str, prefixes: Iterable[str]) -> str | None:
    """Return the first of *prefixes* that *name* equals or sits under."""
    for prefix in prefixes:
        if name == prefix or name.startswith(f"{prefix}."):
            return prefix
    return None


def _existing_modules(names: Iterable[str], modules: Mapping[str, Path]) -> set[str]:
    """Return the modules *names* load: each name and the packages above it."""
    found: set[str] = set()
    for name in names:
        parts = name.split(".")
        found.update(
            candidate
            for candidate in (".".join(parts[:end]) for end in range(1, len(parts) + 1))
            if candidate in modules
        )
    return found


def keep_closure_modules(roots: Sequence[str], modules: Mapping[str, Path]) -> frozenset[str]:
    """Return every module reachable from *roots* through module-level imports.

    Raises:
        SyntaxError: A module in the closure is not Python.
    """
    pending = [name for name in modules if _is_within(name, roots)]
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        path = modules[name]
        imported = imported_names(
            path.read_text(encoding="utf-8"),
            module=name,
            is_package=path.name == "__init__.py",
            module_level_only=True,
        )
        pending.extend(_existing_modules(imported, modules) - seen)
    return frozenset(seen)


def find_violations(src_root: Path, closure: KeepClosure) -> list[ClosureViolation]:
    """Return each import of a v1-only module from outside the keep closure.

    A module importing both a v1-only package and a module under it is
    reported once, against the package.

    Raises:
        SyntaxError: A module under *src_root* is not Python.
    """
    modules = module_names(src_root)
    inside = keep_closure_modules(closure.roots, modules)
    violations: list[ClosureViolation] = []
    for name, path in sorted(modules.items()):
        if name in inside:
            continue
        imported = imported_names(
            path.read_text(encoding="utf-8"),
            module=name,
            is_package=path.name == "__init__.py",
            module_level_only=False,
        )
        hits = {target for target in imported if _is_within(target, closure.v1_only)}
        violations.extend(
            ClosureViolation(importer=name, imported=target)
            for target in sorted(hits)
            if not _is_within(target, hits - {target})
        )
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    """Check the tree against the closure file; exit 1 on any violation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src-root", type=Path, default=Path("src"))
    parser.add_argument("--closure", type=Path, default=DEFAULT_CLOSURE_PATH)
    args = parser.parse_args(argv)
    violations = find_violations(args.src_root, load_keep_closure(args.closure))
    if not violations:
        print("epoch1-keep-closure: clean")
        return 0
    for violation in violations:
        print(f"epoch1-keep-closure: {violation.render()}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_CLOSURE_PATH",
    "ClosureViolation",
    "KeepClosure",
    "find_violations",
    "imported_names",
    "keep_closure_modules",
    "load_keep_closure",
    "main",
    "module_names",
]
