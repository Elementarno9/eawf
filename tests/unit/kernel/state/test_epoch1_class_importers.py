"""No module imports an epoch-1 state class.

The Phase, Iter and Wave row models, and the sub-models only they carry,
left with the epoch-1 migration stack. The lint walks every module under
``src/eawf`` with :mod:`ast` and fails on any importer of them, so they
cannot regrow. An import counts whether it is top-level, lazy inside a
function, or under ``TYPE_CHECKING``; and it counts from the stack's
``v1_models`` module or from :mod:`eawf.kernel.state.models`, the home the
classes left before it, so neither can carry them back unnoticed.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[4]
SRC: Final = REPO / "src"

#: The module the epoch-1 state classes lived in until the stack left.
EPOCH1_HOME: Final = "eawf.kernel.migration.v1_models"

#: The module the epoch-1 state classes left.
OLD_HOME: Final = "eawf.kernel.state.models"

#: The epoch-1 state classes.
EPOCH1_STATE_CLASSES: Final = frozenset(
    {
        "CloseAttempt",
        "CriteriaFloorWaiver",
        "Iter",
        "Phase",
        "RuntimeBaseline",
        "RuntimeCarry",
        "RuntimeLatest",
        "Wave",
        "WaveDependencyBarrier",
        "WaveDependencyBinding",
        "WaveIntegration",
    }
)


def module_name(root: Path, path: Path) -> str:
    """Return the dotted name of the module at *path* under *root*."""
    parts = path.relative_to(root).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _absolute(module: str | None, level: int, importer: str, is_package: bool) -> str:
    """Resolve a possibly relative ``from`` target to its absolute name."""
    if level == 0:
        return module or ""
    package = importer.split(".") if is_package else importer.split(".")[:-1]
    base = package[: len(package) - (level - 1)]
    return ".".join([*base, module] if module else base)


def _names_epoch1(dotted: str) -> bool:
    """Return whether an imported dotted name is the epoch-1 home or one of its classes."""
    module, _, name = dotted.rpartition(".")
    return (
        dotted == EPOCH1_HOME
        or module == EPOCH1_HOME
        or (module == OLD_HOME and name in EPOCH1_STATE_CLASSES)
    )


def epoch1_imports(source: str, importer: str, *, is_package: bool = False) -> list[str]:
    """Return each epoch-1 state class or home module *source* imports.

    Args:
        source: The module's text.
        importer: The module's dotted name, used to resolve relative imports.
        is_package: Whether the module is a package ``__init__``.

    Returns:
        One ``<module>.<name>`` entry per offending import, in source order.

    Raises:
        SyntaxError: When *source* is not valid Python.
    """
    tree = ast.parse(source)
    found: list[str] = []
    old_home_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [(alias.name, alias.asname) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            target = _absolute(node.module, node.level, importer, is_package)
            names = [(f"{target}.{alias.name}", alias.asname) for alias in node.names]
        else:
            continue
        for dotted, asname in names:
            if _names_epoch1(dotted):
                found.append(dotted)
            elif dotted == OLD_HOME:
                old_home_aliases.add(asname or dotted.rpartition(".")[2])
    found.extend(
        f"{OLD_HOME}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in EPOCH1_STATE_CLASSES
        and ast.unparse(node.value) in old_home_aliases
    )
    return found


def find_violations(root: Path) -> list[tuple[str, str]]:
    """Return ``(importer, imported)`` for every live epoch-1 import under *root*.

    Args:
        root: A source root holding the ``eawf`` package tree.

    Returns:
        The violations, sorted by importer then imported name; empty when
        no module imports an epoch-1 state class.

    Raises:
        SyntaxError: When a module under *root* is not valid Python.
    """
    violations: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        importer = module_name(root, path)
        source = path.read_text(encoding="utf-8")
        for imported in epoch1_imports(source, importer, is_package=path.name == "__init__.py"):
            violations.append((importer, imported))
    return sorted(set(violations))


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """An empty source root a test plants modules in."""
    return tmp_path / "src"


def _plant(root: Path, dotted: str, source: str, *, package: bool = False) -> None:
    path = root.joinpath(*dotted.split("."))
    path = path / "__init__.py" if package else path.with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


# ---- the real tree ----------------------------------------------------------


def test_no_live_module_imports_an_epoch1_state_class() -> None:
    assert find_violations(SRC) == []


def test_the_epoch1_home_is_gone() -> None:
    assert not SRC.joinpath(*EPOCH1_HOME.split(".")).with_suffix(".py").exists()


def test_the_old_home_defines_no_epoch1_state_class() -> None:
    old = SRC.joinpath(*OLD_HOME.split(".")).with_suffix(".py")
    tree = ast.parse(old.read_text(encoding="utf-8"))
    bound = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    bound |= {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert bound & EPOCH1_STATE_CLASSES == set()


# ---- red: a live importer ---------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        f"from {EPOCH1_HOME} import Wave\n",
        f"import {EPOCH1_HOME}\n",
        "from eawf.kernel.migration import v1_models\n",
        f"def late():\n    from {EPOCH1_HOME} import Iter\n",
        f"if TYPE_CHECKING:\n    from {EPOCH1_HOME} import Phase\n",
        f"from {OLD_HOME} import Wave\n",
        "from eawf.kernel.state import models\nmodels.Wave.model_rebuild()\n",
        f"import {OLD_HOME} as m\nm.CloseAttempt\n",
    ],
)
def test_a_live_importer_fails(tree: Path, source: str) -> None:
    _plant(tree, "eawf.workflow.regrown", source)

    violations = find_violations(tree)

    assert len(violations) == 1
    assert violations[0][0] == "eawf.workflow.regrown"


def test_a_relative_import_from_the_old_home_fails(tree: Path) -> None:
    _plant(tree, "eawf.kernel.state.regrown", "from .models import Iter\n")

    assert find_violations(tree) == [("eawf.kernel.state.regrown", f"{OLD_HOME}.Iter")]


def test_a_relative_import_in_a_package_init_fails(tree: Path) -> None:
    _plant(tree, "eawf.kernel.state", "from .models import Phase\n", package=True)

    assert find_violations(tree) == [("eawf.kernel.state", f"{OLD_HOME}.Phase")]


def test_two_imports_in_one_module_report_each(tree: Path) -> None:
    _plant(tree, "eawf.workflow.regrown", f"from {EPOCH1_HOME} import Iter, Wave\n")

    assert find_violations(tree) == [
        ("eawf.workflow.regrown", f"{EPOCH1_HOME}.Iter"),
        ("eawf.workflow.regrown", f"{EPOCH1_HOME}.Wave"),
    ]


# ---- green: what the lint must not flag -------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "",
        f"from {OLD_HOME} import SessionAttempt, IdStr\n",
        "from eawf.kernel.state import models\nmodels.SessionAttempt\n",
        "Wave = 1\nclass Iter: ...\n",
    ],
)
def test_a_live_model_or_a_local_name_passes(tree: Path, source: str) -> None:
    _plant(tree, "eawf.workflow.live", source)

    assert find_violations(tree) == []


def test_an_empty_tree_has_no_violations(tree: Path) -> None:
    tree.mkdir(parents=True)

    assert find_violations(tree) == []


def test_a_module_that_is_not_python_raises(tree: Path) -> None:
    _plant(tree, "eawf.workflow.broken", "def oops(:\n")

    with pytest.raises(SyntaxError):
        find_violations(tree)


def test_a_copy_of_the_real_tree_reds_on_a_planted_importer(tmp_path: Path) -> None:
    root = tmp_path / "src"
    shutil.copytree(SRC / "eawf", root / "eawf", ignore=shutil.ignore_patterns("__pycache__"))
    _plant(root, "eawf.surfaces.cli.commands.regrown", f"from {EPOCH1_HOME} import Wave\n")

    assert find_violations(root) == [
        ("eawf.surfaces.cli.commands.regrown", f"{EPOCH1_HOME}.Wave"),
    ]
