"""Only the migration tests build a v1 ``State`` or seed epoch-1 rows.

A native tree holds a generation, not the v1 document, so a test of live
behaviour builds a native fixture. The v1 ``State`` model is kept for the
migration and upgrade path alone and lives in the migration package; the
tests that still build one are the tests of that path, which sit under a
directory whose path contains ``/migration``. The epoch-1 seed helpers that
lay phase, iter and wave rows into a ``State`` live there too, and only
tests there import them.

A module counts as building a v1 ``State`` when it calls the model, or its
``model_validate``, ``model_validate_json`` or ``model_construct``, through
a name bound to the model from either home: the old
:mod:`eawf.kernel.state.models` one or :mod:`eawf.kernel.migration.v1_state`.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]
TESTS: Final = REPO / "tests"
SRC: Final = REPO / "src"

#: The module the v1 model lives in, and the one it left.
V1_STATE_HOME: Final = "eawf.kernel.migration.v1_state"
OLD_STATE_HOME: Final = "eawf.kernel.state.models"
_HOMES: Final = frozenset({V1_STATE_HOME, OLD_STATE_HOME})

#: The model's class methods that build an instance.
_BUILDERS: Final = frozenset({"model_validate", "model_validate_json", "model_construct"})

#: The marker of a migration test path, matched against the repo-relative path.
_MIGRATION: Final = "/migration"

#: The seed helper this sweep moved under the migration tests.
OLD_SEED_HELPER: Final = TESTS / "_epoch1_seed.py"


def _bindings(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """Return the names bound to the v1 model and to a module that holds it."""
    models: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                if node.module in _HOMES and alias.name == "State":
                    models.add(alias.asname or alias.name)
                elif f"{node.module}.{alias.name}" in _HOMES:
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _HOMES and alias.asname is not None:
                    modules.add(alias.asname)
    return frozenset(models), frozenset(modules)


def _is_model(node: ast.expr, models: frozenset[str], modules: frozenset[str]) -> bool:
    """Return whether *node* names the v1 model."""
    if isinstance(node, ast.Name):
        return node.id in models
    if isinstance(node, ast.Attribute) and node.attr == "State":
        if isinstance(node.value, ast.Name) and node.value.id in modules:
            return True
        return ast.unparse(node.value) in _HOMES
    return False


def v1_state_builds(source: str) -> int:
    """Return how many times *source* builds a v1 ``State``.

    Args:
        source: A module's text.

    Returns:
        The number of calls to the model or one of its builders; ``0`` for a
        module that binds no name to the model.

    Raises:
        SyntaxError: *source* is not Python.
    """
    tree = ast.parse(source)
    models, modules = _bindings(tree)
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if _is_model(func, models, modules) or (
            isinstance(func, ast.Attribute)
            and func.attr in _BUILDERS
            and _is_model(func.value, models, modules)
        ):
            count += 1
    return count


def is_migration_test(relative: str) -> bool:
    """Return whether the repo-relative posix *relative* path is a migration test path."""
    return _MIGRATION in f"/{relative}"


def v1_state_builders(root: Path) -> list[str]:
    """Return every module under *root* that builds a v1 ``State``, repo-relative, sorted."""
    return sorted(
        path.relative_to(root.parent).as_posix()
        for path in root.rglob("*.py")
        if v1_state_builds(path.read_text(encoding="utf-8"))
    )


def is_seed_helper(dotted: str) -> bool:
    """Return whether the dotted test module *dotted* is an epoch-1 seed helper.

    A seed helper is a module under ``tests`` named for epoch 1 that is not
    itself a test module.
    """
    parts = dotted.split(".")
    return parts[0] == "tests" and "epoch1" in parts[-1] and not parts[-1].startswith("test_")


def seed_helper_imports(source: str) -> frozenset[str]:
    """Return the epoch-1 seed helpers *source* imports, as dotted names.

    Raises:
        SyntaxError: *source* is not Python.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            candidates = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
        elif isinstance(node, ast.Import):
            candidates = [alias.name for alias in node.names]
        else:
            continue
        found.update(name for name in candidates if is_seed_helper(name))
    return frozenset(found)


def _dotted(root: Path, path: Path) -> str:
    return ".".join(path.relative_to(root.parent).with_suffix("").parts)


# ---- the real tree ----------------------------------------------------------


def test_no_v1_state_outside_migration_tests() -> None:
    stray = [path for path in v1_state_builders(TESTS) if not is_migration_test(path)]
    assert stray == [], (
        f"{len(stray)} test modules outside the migration tests build a v1 State: {stray}; "
        "build a native fixture instead, or move a test of the migration path under it"
    )


def test_v1_state_lives_in_the_migration_package() -> None:
    home = importlib.import_module(V1_STATE_HOME)
    old = importlib.import_module(OLD_STATE_HOME)

    assert home.State.__module__ == V1_STATE_HOME
    assert "1.20" in home.State.model_fields["schema_version"].annotation.__args__
    assert "State" not in vars(old)
    importers = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module == OLD_STATE_HOME
            and any(alias.name == "State" for alias in node.names)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )
    assert importers == []


def test_epoch1_seed_lives_with_migration_tests() -> None:
    assert not OLD_SEED_HELPER.exists()
    paths = list(TESTS.rglob("*.py"))
    helpers = [p for p in paths if is_seed_helper(_dotted(TESTS, p))]
    importers = [p for p in paths if seed_helper_imports(p.read_text(encoding="utf-8"))]
    stray = sorted(
        p.relative_to(REPO).as_posix()
        for p in [*helpers, *importers]
        if not is_migration_test(p.relative_to(REPO).as_posix())
    )
    assert stray == []


# ---- red: a regrowth --------------------------------------------------------


def _plant(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_a_live_test_building_a_v1_state_is_found(tmp_path: Path) -> None:
    root = tmp_path / "tests"
    _plant(
        root,
        "unit/test_live.py",
        "from eawf.kernel.migration.v1_state import State\nState.model_validate({})\n",
    )
    _plant(
        root,
        "unit/kernel/migration/test_chain.py",
        "from eawf.kernel.migration.v1_state import State\nState({})\n",
    )

    found = v1_state_builders(root)

    assert found == ["tests/unit/kernel/migration/test_chain.py", "tests/unit/test_live.py"]
    assert [p for p in found if not is_migration_test(p)] == ["tests/unit/test_live.py"]


def test_a_live_test_importing_a_seed_helper_is_found() -> None:
    source = "from tests.fixtures.migration.epoch1_seed import seed_wave\n"
    assert seed_helper_imports(source) == {"tests.fixtures.migration.epoch1_seed"}
    assert seed_helper_imports("from tests import _epoch1_seed\n") == {"tests._epoch1_seed"}


# ---- boundaries -------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", 0),
        ("State({})\n", 0),
        ("from eawf.kernel.state.models import State\nref = State\n", 0),
        ("from eawf.kernel.state.models import State\nState({})\n", 1),
        ("from eawf.kernel.state.models import State as S\nS.model_validate({})\n", 1),
        ("from eawf.kernel.migration.v1_state import State\nState.model_validate_json(b)\n", 1),
        ("from eawf.kernel.migration.v1_state import State\nState.model_construct()\n", 1),
        ("from eawf.kernel.state import models\nmodels.State({})\nmodels.Wave({})\n", 1),
        ("import eawf.kernel.state.models as m\nm.State.model_validate({})\n", 1),
        ("import eawf.kernel.migration.v1_state\neawf.kernel.migration.v1_state.State()\n", 1),
        ("from textual.app import State\nState({})\n", 0),
        ("from eawf.kernel.state.models import State\nState()\nState.model_validate(x)\n", 2),
    ],
    ids=[
        "empty",
        "unbound-name",
        "bare-reference",
        "call",
        "aliased-validate",
        "new-home-json",
        "construct",
        "module-attribute",
        "module-alias",
        "dotted",
        "other-state",
        "two",
    ],
)
def test_v1_state_builds_boundaries(source: str, expected: int) -> None:
    assert v1_state_builds(source) == expected


def test_v1_state_builds_refuses_a_module_that_is_not_python() -> None:
    with pytest.raises(SyntaxError):
        v1_state_builds("def broken(:\n")


def test_seed_helper_imports_refuses_a_module_that_is_not_python() -> None:
    with pytest.raises(SyntaxError):
        seed_helper_imports("import (\n")


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("tests/unit/test_x.py", False),
        ("tests/fixtures/migration/epoch1_seed.py", True),
        ("tests/unit/kernel/migrations/test_v1_7.py", True),
        ("tests/unit/test_migration_census.py", False),
        ("migration/test_x.py", True),
    ],
    ids=["live", "fixtures", "chain-dir", "name-only", "top-level"],
)
def test_is_migration_test_boundaries(relative: str, expected: bool) -> None:
    assert is_migration_test(relative) is expected


@pytest.mark.parametrize(
    ("dotted", "expected"),
    [
        ("tests._epoch1_seed", True),
        ("tests._memory_epoch1", True),
        ("tests.contract.test_epoch1_noun_census", False),
        ("tests._epoch2_helpers", False),
        ("eawf.kernel.epoch1", False),
        ("tests", False),
    ],
    ids=["seed", "memory", "test-module", "epoch2", "not-tests", "bare"],
)
def test_is_seed_helper_boundaries(dotted: str, expected: bool) -> None:
    assert is_seed_helper(dotted) is expected
