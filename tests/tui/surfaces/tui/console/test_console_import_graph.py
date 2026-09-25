"""The console imports nothing from the epoch-1 TUI tree.

Before this wave the console's projection seam reached into
``eawf.surfaces.tui.chassis.state_binding`` -- a module of the epoch-1 tree -- for its
daemon transport. That edge meant the epoch-1 tree could never be deleted
(the P35 flag day) without breaking the console it was meant to retire in
favour of. ``state_binding`` (and five siblings it shares no console
dependant with today: ``theme``, ``sigils``, ``progress``, ``pilot_harness``
and ``offline``) moved to :mod:`eawf.surfaces.tui.chassis`, neutral ground
neither app owns and the epoch-1 tree's deletion cannot reach.

This is the regression gate over that edge staying cut: an AST walk of every
console module's imports, asserting none of them name a module of the
epoch-1 tree (anything under ``eawf.surfaces.tui`` except ``console`` itself
and the shared ``chassis`` package both apps may depend on).
"""

from __future__ import annotations

import ast
from pathlib import Path

from eawf.surfaces.tui import console as console_package

CONSOLE_ROOT = Path(console_package.__file__).resolve().parent

#: The two subtrees under ``eawf.surfaces.tui`` a console import may name.
_ALLOWED_TUI_CHILDREN = frozenset({"console", "chassis"})

_TUI_PREFIX = "eawf.surfaces.tui"


def _imported_modules(source: str) -> list[str]:
    """Return every dotted module name a Python source file's imports name."""
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _is_epoch1_edge(module_name: str) -> bool:
    """Return whether *module_name* names a module of the epoch-1 TUI tree.

    Args:
        module_name: A dotted module name as an import statement named it.

    Returns:
        ``True`` for anything under ``eawf.surfaces.tui`` other than the
        console package itself or the shared chassis; ``False`` for a bare
        ``eawf.surfaces.tui`` package import (names no epoch-1 module) and
        for anything outside the ``tui`` surface entirely.
    """
    if module_name == _TUI_PREFIX:
        return False
    if not module_name.startswith(_TUI_PREFIX + "."):
        return False
    rest = module_name[len(_TUI_PREFIX) + 1 :]
    head = rest.split(".", 1)[0]
    return head not in _ALLOWED_TUI_CHILDREN


def _epoch1_edges(source_files: list[Path]) -> dict[str, list[str]]:
    """Return each file's import edges into the epoch-1 tree, keyed by path.

    Args:
        source_files: Python files to walk.

    Returns:
        A mapping from a file's path (as a string) to the epoch-1 module
        names it imports; files with no such edge are omitted, so an empty
        mapping means the walk found nothing to red.
    """
    edges: dict[str, list[str]] = {}
    for path in source_files:
        offenders = [
            name
            for name in _imported_modules(path.read_text(encoding="utf-8"))
            if _is_epoch1_edge(name)
        ]
        if offenders:
            edges[str(path)] = offenders
    return edges


def test_console_imports_nothing_from_epoch1_tree() -> None:
    modules = sorted(CONSOLE_ROOT.rglob("*.py"))
    assert modules, "the console package holds no module"
    edges = _epoch1_edges(modules)
    assert edges == {}


def test_epoch1_edge_detector_reds_on_a_seeded_import(tmp_path: Path) -> None:
    """Gate-fire proof: the detector the prior test relies on catches the exact
    defect it exists to prevent, so a real regression cannot slip past a
    detector that silently stopped working.
    """
    offender = tmp_path / "bad_console_module.py"
    offender.write_text(
        "from eawf.surfaces.tui.app import EaApp\n",
        encoding="utf-8",
    )
    edges = _epoch1_edges([offender])
    assert edges, "seeding an epoch-1 import must be caught, not pass silently"
    assert edges[str(offender)] == ["eawf.surfaces.tui.app"]


def test_edge_detector_allows_console_and_chassis_imports(tmp_path: Path) -> None:
    """A console module may still import its own package and the shared chassis."""
    clean = tmp_path / "clean_console_module.py"
    clean.write_text(
        "from eawf.surfaces.tui.console.registry import REGISTRY\n"
        "from eawf.surfaces.tui.chassis.state_binding import StateBinding\n"
        "from eawf.surfaces.tui import console\n",
        encoding="utf-8",
    )
    assert _epoch1_edges([clean]) == {}
