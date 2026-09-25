"""Idle-contract rule: every TUI ``App`` subclass needs a reachable launcher.

A Textual :class:`App` subclass under ``src/eawf/surfaces/tui/`` does nothing
until some production code path actually constructs it. Before
:mod:`eawf.surfaces.tui.launch` landed, the console
(:class:`~eawf.surfaces.tui.console.app.ConsoleApp`) shipped importable, with
its own tests constructing it directly, but with no production caller at
all -- a whole phase's worth of "built but never opened", the exact B091
idle-verifier shape ``tools/idle_contract_gate.py`` exists to catch. That
gate tracked only hand-registered, one-off contracts, so a brand-new App
subclass with zero launcher wiring was invisible to it.

This module is generic where a one-off source-scan check is not: it
discovers App subclasses by AST scan rather than by a hardcoded class name,
so a future console screen is covered the moment it is defined, with no
matching edit here. What is fixed is the reachability chain: the
``pyproject.toml`` ``[project.scripts]`` console-script entry names a
module, that module's ``eawf.*`` imports (including the lazy,
function-local ones the launcher favors) are followed to a bounded frontier
inside the TUI surface package, and each discovered App subclass must have
an actual ``ClassName(`` construction call somewhere in that reachable set.

``tools/idle_contract_gate.py`` imports this module lazily (mirroring its
existing ``tools/coverage_gate.py`` sibling-loader) and wraps
:func:`find_unconstructed_console_apps` into its own typed ``GateResult``;
this module carries no dependency back on the gate module, so the two do not
form an import cycle.
"""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Iterable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Root of the TUI surface. Every Textual ``App`` subclass under here is a
#: candidate screen that must be reachable from a console-script entry point.
TUI_SURFACE_ROOT = "src/eawf/surfaces/tui"

#: The ``[project.scripts]`` key whose target module starts the reachability
#: walk -- the primary console-script entry point (``ea`` is a bare alias for
#: the same target, and ``eawfd`` is the daemon, not a TUI caller).
DEFAULT_CONSOLE_SCRIPT_NAME = "eawf"

#: Only ``eawf.*`` import edges inside this package are followed past the
#: entry module itself. Console construction sites live in the TUI stack,
#: not scattered across the whole codebase, and bounding the walk here keeps
#: it fast and keeps an unrelated import from ever counting as a launcher.
_BOUNDARY_PREFIX = "eawf.surfaces.tui"


def _is_textual_app_base(base: ast.expr) -> bool:
    """Return whether a class base expression names Textual's ``App``.

    Matches a bare ``App``, a subscripted ``App[None]``, and a
    dotted/qualified ``textual.app.App`` -- whichever form a subclass uses.
    """
    if isinstance(base, ast.Name):
        return base.id == "App"
    if isinstance(base, ast.Subscript):
        return _is_textual_app_base(base.value)
    if isinstance(base, ast.Attribute):
        return base.attr == "App"
    return False


def find_app_subclasses(tui_root: Path) -> dict[str, Path]:
    """Return ``{class_name: defining_file}`` for every Textual App subclass under ``tui_root``.

    Args:
        tui_root: Directory to scan recursively for ``*.py`` files.

    Returns:
        One entry per ``class Foo(App...)`` definition found. A file that
        fails to parse is skipped rather than raising, so a stray fixture or
        scratch file elsewhere under the surface never aborts the scan.
    """
    found: dict[str, Path] = {}
    for path in sorted(tui_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                _is_textual_app_base(base) for base in node.bases
            ):
                found[node.name] = path
    return found


def _dotted_imports(path: Path) -> set[str]:
    """Return every ``eawf.*`` module *path* imports, top-level or function-local."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("eawf"):
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names if alias.name.startswith("eawf"))
    return modules


def _module_path(root: Path, dotted: str) -> Path | None:
    """Resolve a dotted ``eawf.*`` module name to its source file under ``root/src``."""
    rel = Path(*dotted.split("."))
    module_file = root / "src" / rel.with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_init = root / "src" / rel / "__init__.py"
    if package_init.is_file():
        return package_init
    return None


def _entry_module(root: Path, *, script_name: str) -> str | None:
    """Return the dotted module the ``pyproject.toml`` console-script entry names.

    Args:
        root: Repo root, containing ``pyproject.toml``.
        script_name: The ``[project.scripts]`` key to resolve.

    Returns:
        The module half of ``module:attr``, or ``None`` when *script_name* is
        not declared -- an entry point removed outright has no chain left to
        walk, which is a louder, different failure the packaging tests cover.
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    target = data.get("project", {}).get("scripts", {}).get(script_name)
    if not target:
        return None
    module, _, _attr = target.partition(":")
    return module or None


def _reachable_modules(root: Path, *, entry_dotted: str, boundary_prefix: str) -> dict[str, Path]:
    """BFS the ``eawf.*`` import graph from ``entry_dotted``, bounded to ``boundary_prefix``.

    The entry module is always included even though it sits outside the
    boundary (the CLI dispatcher, not the TUI package) -- it is the known
    first hop of the console-script chain. Every module it imports that lies
    inside *boundary_prefix* is then walked transitively; an import outside
    the boundary is not followed, so the walk cannot wander into the rest of
    the codebase.
    """
    entry_path = _module_path(root, entry_dotted)
    if entry_path is None:
        return {}
    reachable: dict[str, Path] = {entry_dotted: entry_path}
    frontier = [entry_dotted]
    while frontier:
        current = frontier.pop()
        for dotted in _dotted_imports(reachable[current]):
            if dotted in reachable or not dotted.startswith(boundary_prefix):
                continue
            path = _module_path(root, dotted)
            if path is None:
                continue
            reachable[dotted] = path
            frontier.append(dotted)
    return reachable


def _constructed_names(sources: Iterable[str]) -> set[str]:
    """Return every symbol called as a constructor (an ``ast.Call``) across *sources*.

    AST-based rather than a text regex so a class's own ``class Foo(App):``
    definition line never self-satisfies as its own construction call --
    only an actual ``Foo(...)`` call site counts.
    """
    names: set[str] = set()
    for source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def find_unconstructed_console_apps(
    *,
    repo_root: Path | None = None,
    tui_surface_root: str = TUI_SURFACE_ROOT,
    console_script_name: str = DEFAULT_CONSOLE_SCRIPT_NAME,
) -> list[str]:
    """Return the App subclass names with no reachable production construction site.

    A class counts as reachable when the ``pyproject.toml``
    ``[project.scripts]`` entry for *console_script_name* resolves to a
    module, and following that module's ``eawf.*`` imports (top-level or
    function-local, bounded to :data:`_BOUNDARY_PREFIX`) reaches at least one
    module whose source calls the class as a constructor.

    Args:
        repo_root: Repo root to scan. Defaults to the real tree; injectable
            so a test can point the probe at a synthetic fixture directory.
        tui_surface_root: Repo-relative directory to scan for App subclasses.
        console_script_name: The ``[project.scripts]`` key naming the
            console-script entry point the reachability walk starts from.

    Returns:
        Sorted class names with no reachable construction site -- empty when
        every discovered App subclass is wired, or when the tree carries no
        App subclass at all (nothing to prove idle).
    """
    root = repo_root or _REPO_ROOT
    apps = find_app_subclasses(root / tui_surface_root)
    if not apps:
        return []
    entry_dotted = _entry_module(root, script_name=console_script_name)
    reachable = (
        _reachable_modules(root, entry_dotted=entry_dotted, boundary_prefix=_BOUNDARY_PREFIX)
        if entry_dotted is not None
        else {}
    )
    sources = [path.read_text(encoding="utf-8") for path in reachable.values()]
    constructed = _constructed_names(sources)
    return sorted(class_name for class_name in apps if class_name not in constructed)


__all__ = [
    "DEFAULT_CONSOLE_SCRIPT_NAME",
    "TUI_SURFACE_ROOT",
    "find_app_subclasses",
    "find_unconstructed_console_apps",
]
