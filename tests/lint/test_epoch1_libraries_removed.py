"""The epoch-1 daemon methods and libraries no entry point reaches stay deleted.

``wave autoland``, the v1 worktree create path, ``track sync``, the v1
open-question writers, the wave config overlay, the ``state.mutate`` payload
union, ``eawf impact`` and the EAWF022 coverage lint all served the v1
``.ea/state.json`` workflow and had no caller left in ``eawf`` or ``eawfd``.
These checks red when a deleted module, an import of one, a deleted RPC or
the ``impact`` verb returns; a call to ``eawf impact`` now gets the
retired-verb refusal.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import orjson
import pytest
import typer
from typer.testing import CliRunner

from eawf.runtime.daemon.methods import registered_methods
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.exit_codes import VALIDATION_ERROR
from tests.lint.test_v1_state_readers import V1_READERS

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]

#: The deleted libraries, by dotted module name.
DELETED_MODULES: Final = (
    "eawf.runtime.worktree.autoland",
    "eawf.runtime.worktree.create",
    "eawf.kernel.state.mutations",
    "eawf.surfaces.cli.commands.impact",
    "eawf.surfaces.render.impact",
    "eawf.platform.lint.eawf022_propose_coverage",
)

#: The deleted RPCs, each of which wrote the v1 document or daemon RAM only.
DELETED_RPCS: Final = (
    "state.wave_autoland",
    "track.sync",
    "research.add_question",
    "research.resolve_question",
    "config.set_wave_value",
    "config.get_wave_overlay",
    "config.clear_wave_overlay",
)

#: Kept modules that imported a deleted library, with the one they imported.
FORMER_IMPORTERS: Final = {
    "src/eawf/runtime/daemon/methods/state_context.py": "eawf.kernel.state.mutations",
    "tests/platform/lint/test_eawf021_022.py": "eawf.platform.lint.eawf022_propose_coverage",
}


def _imported_modules(path: Path) -> set[str]:
    """Return every absolute dotted name *path* imports, module or attribute.

    ``from a import b`` yields both ``a`` and ``a.b`` because ``b`` may be a
    submodule.

    Args:
        path: A Python source file.

    Returns:
        The dotted names the file imports at any nesting level.

    Raises:
        SyntaxError: *path* is not valid Python.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _deleted_imports(path: Path) -> list[str]:
    """Return the deleted modules *path* imports, sorted."""
    return sorted(set(DELETED_MODULES) & _imported_modules(path))


def test_epoch1_rpcs_are_unregistered() -> None:
    registered = set(registered_methods())
    assert "state.worktree_reconcile" in registered
    assert sorted(registered & set(DELETED_RPCS)) == []


def test_epoch1_library_modules_are_gone() -> None:
    present = [
        name for name in DELETED_MODULES if (REPO / "src" / f"{name.replace('.', '/')}.py").exists()
    ]
    assert present == []
    importers = {
        str(path.relative_to(REPO)): hits
        for top in ("src", "tools", "tests")
        for path in sorted((REPO / top).rglob("*.py"))
        if (hits := _deleted_imports(path))
    }
    assert importers == {}


def test_impact_command_is_gone(tmp_path: Path) -> None:
    assert "impact" not in typer.main.get_command(app).commands  # type: ignore[attr-defined]
    result = CliRunner().invoke(app, ["--json", "-w", str(tmp_path), "impact"])
    assert result.exit_code == VALIDATION_ERROR, result.output
    assert orjson.loads(result.stdout)["data"]["kind"] == "LegacyOperationRemoved"
    assert "eawf/surfaces/cli/commands/impact.py" not in V1_READERS


def test_kept_modules_drop_the_deleted_imports() -> None:
    still_importing = {
        path: module
        for path, module in FORMER_IMPORTERS.items()
        if module in _imported_modules(REPO / path)
    }
    assert still_importing == {}
    assert "research.resolve_question" not in registered_methods()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", []),
        ("import eawf.kernel.state.mutations\n", ["eawf.kernel.state.mutations"]),
        ("from eawf.surfaces.render import impact\n", ["eawf.surfaces.render.impact"]),
        (
            "def f():\n    from eawf.runtime.worktree.autoland import wave_autoland\n",
            ["eawf.runtime.worktree.autoland"],
        ),
        ("from eawf.runtime.worktree import reconcile\n", []),
    ],
)
def test_deleted_imports_flags_each_import_shape(
    tmp_path: Path, source: str, expected: list[str]
) -> None:
    path = tmp_path / "probe.py"
    path.write_text(source, encoding="utf-8")
    assert _deleted_imports(path) == expected


def test_deleted_imports_raises_on_invalid_python(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("def (:\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        _deleted_imports(path)
