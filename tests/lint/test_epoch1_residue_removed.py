"""The unclaimed epoch-1 residue stays deleted.

Eight modules under ``src/eawf`` were reached only by their own tests or a
package re-export, the wave-keyed ``eawf jury label`` verb wrote gold labels
the native ``batch label`` verb now files on the Batch ledger, and three
tools had no caller at all. These checks red when one of them returns, when
a module under ``src``, ``tools`` or ``tests`` imports one again, or when the
snapshot inventory or the root help names a surface that no longer exists.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Final

import click
import pytest
import typer
from typer.testing import CliRunner

from eawf.runtime.daemon.methods import _METHOD_MODULES, registered_methods
from eawf.runtime.daemon.methods.delivery_label import DELIVERY_LABEL_AUDIT_METHOD
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.snapshot import SNAPSHOT_SURFACES
from eawf.surfaces.cli.registry import COMMAND_REGISTRY

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]

#: The deleted residue modules, by dotted module name.
RESIDUE_MODULES: Final = (
    "eawf.workflow.dispatch.cost_ab",
    "eawf.workflow.dispatch.clarity_judge",
    "eawf.kernel.spec.auq_bridge",
    "eawf.kernel.spec.wave_body",
    "eawf.runtime.daemon.close_workspace",
    "eawf.observability.telemetry.join",
    "eawf.surfaces.cli.streaming",
    "eawf.workflow.verify.hosted_close",
)

#: The deleted ``eawf jury`` command and daemon modules.
JURY_FILES: Final = (
    "src/eawf/surfaces/cli/commands/jury.py",
    "src/eawf/runtime/daemon/methods/jury.py",
)

#: The deleted tools nothing ran.
DELETED_TOOLS: Final = (
    "tools/migrate_drop_wave_commit.py",
    "tools/rasterize_svg.py",
    "tools/validate_drive_recording.py",
)

#: The snapshot surfaces whose golden trees and regen targets are gone.
RETIRED_SNAPSHOT_KINDS: Final = frozenset({"state", "spec", "telemetry"})


def _module_path(name: str) -> str:
    """Return the repo-relative source path of dotted module *name*."""
    return f"src/{name.replace('.', '/')}.py"


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
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _residue_imports(path: Path) -> list[str]:
    """Return the residue modules *path* imports, sorted."""
    return sorted(set(RESIDUE_MODULES) & _imported_modules(path))


def _tool_mentions(text: str) -> list[str]:
    """Return the deleted tools *text* names by file name, in table order."""
    return [tool for tool in DELETED_TOOLS if Path(tool).name in text]


def _tracked_files() -> list[str]:
    """Return the tracked repo-relative paths outside ``.ea``, minus this file.

    ``.ea`` holds the plan records that name the deleted tools as the work
    to do, and this file names them as the work done.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True, text=True
    ).stdout
    own = str(Path(__file__).resolve().relative_to(REPO))
    return [p for p in listed.split("\0") if p and not p.startswith(".ea/") and p != own]


def _workspace_help() -> str:
    """Return the help text of the root ``--workspace`` option."""
    root = typer.main.get_command(app)
    option = next(p for p in root.params if "--workspace" in p.opts)
    assert isinstance(option, click.Option)
    return option.help or ""


def test_residue_modules_are_gone() -> None:
    present = [name for name in RESIDUE_MODULES if (REPO / _module_path(name)).exists()]
    assert present == []
    own = Path(__file__).resolve()
    importers = {
        str(path.relative_to(REPO)): hits
        for top in ("src", "tools", "tests")
        for path in sorted((REPO / top).rglob("*.py"))
        if path != own and (hits := _residue_imports(path))
    }
    assert importers == {}


def test_jury_label_verb_is_gone(tmp_path: Path) -> None:
    runner = CliRunner()
    group = runner.invoke(app, ["--workspace", str(tmp_path), "jury"])
    assert group.exit_code == 2
    assert "No such command 'jury'" in group.output
    label = runner.invoke(app, ["--workspace", str(tmp_path), "jury", "label", "W-X", "--good"])
    assert label.exit_code == 2
    assert "`eawf jury label` is retired" in label.output
    assert "`eawf batch label`" in label.output
    assert [row for row in COMMAND_REGISTRY if getattr(row, "name", None) == "jury"] == []
    assert "jury" not in _METHOD_MODULES
    assert [path for path in JURY_FILES if (REPO / path).exists()] == []
    methods = registered_methods()
    assert DELIVERY_LABEL_AUDIT_METHOD in methods
    assert "jury.label" not in methods


def test_unused_tools_and_stale_rows_are_gone() -> None:
    assert [tool for tool in DELETED_TOOLS if (REPO / tool).exists()] == []
    named: dict[str, list[str]] = {}
    for rel in _tracked_files():
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if hits := _tool_mentions(text):
            named[rel] = hits
    assert named == {}
    assert sorted(RETIRED_SNAPSHOT_KINDS & set(SNAPSHOT_SURFACES)) == []
    assert ".ea/state.json" not in _workspace_help()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", []),
        ("import eawf.kernel.spec.wave_body\n", ["eawf.kernel.spec.wave_body"]),
        ("from eawf.surfaces.cli import streaming\n", ["eawf.surfaces.cli.streaming"]),
        (
            "def f():\n    from eawf.kernel.spec.auq_bridge import AUQRequest\n",
            ["eawf.kernel.spec.auq_bridge"],
        ),
        (
            "from eawf.workflow.dispatch import clarity_judge, cost_ab\n",
            ["eawf.workflow.dispatch.clarity_judge", "eawf.workflow.dispatch.cost_ab"],
        ),
        ("from eawf.workflow.dispatch import routing\n", []),
        ("from . import join\n", []),
    ],
)
def test_residue_imports_flags_each_import_shape(
    tmp_path: Path, source: str, expected: list[str]
) -> None:
    path = tmp_path / "probe.py"
    path.write_text(source, encoding="utf-8")
    assert _residue_imports(path) == expected


def test_residue_imports_raises_on_invalid_python(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("def (:\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        _residue_imports(path)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("uv run python tools/rasterize_svg.py\n", ["tools/rasterize_svg.py"]),
        (
            "validate_drive_recording.py and migrate_drop_wave_commit.py\n",
            ["tools/migrate_drop_wave_commit.py", "tools/validate_drive_recording.py"],
        ),
        ("tools/rasterize_svg_to_png\n", []),
    ],
)
def test_tool_mentions_matches_each_tool_file_name(text: str, expected: list[str]) -> None:
    assert _tool_mentions(text) == expected
