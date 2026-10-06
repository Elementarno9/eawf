"""The epoch-1 keep-closure lint holds on the tree and reds on a regrowth.

The red cases import modules this sweep deleted: the ``commit_repin`` store
kind and the epoch-1 ``state_events`` daemon method. Each is planted in a
copy of the real tree, outside the migration keep closure, and must fail
the lint; the same import from inside the closure must not.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from eawf.platform.lint.epoch1_keep_closure import (
    DEFAULT_CLOSURE_PATH,
    ClosureViolation,
    KeepClosure,
    find_violations,
    imported_names,
    keep_closure_modules,
    load_keep_closure,
    main,
    module_names,
)

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]
SRC: Final = REPO / "src"
CLOSURE_FILE: Final = REPO / DEFAULT_CLOSURE_PATH

#: Modules this sweep deleted, each a v1 store or store writer.
DELETED_STORE_KIND: Final = "eawf.kernel.store.kinds.commit_repin"
DELETED_STORE_METHOD: Final = "eawf.runtime.daemon.methods.state_events"


@pytest.fixture(scope="module")
def closure() -> KeepClosure:
    return load_keep_closure(CLOSURE_FILE)


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A copy of the real ``src`` tree a test may plant a module in."""
    root = tmp_path / "src"
    shutil.copytree(SRC / "eawf", root / "eawf", ignore=shutil.ignore_patterns("__pycache__"))
    return root


def _plant(root: Path, dotted: str, source: str) -> None:
    path = root.joinpath(*dotted.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _write_closure(path: Path, **body: object) -> Path:
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


# ---- the real tree ----------------------------------------------------------


def test_the_current_tree_passes(closure: KeepClosure) -> None:
    assert find_violations(SRC, closure) == []


def test_main_exits_zero_on_the_current_tree(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--src-root", str(SRC), "--closure", str(CLOSURE_FILE)]) == 0
    assert "clean" in capsys.readouterr().out


@pytest.mark.parametrize("deleted", [DELETED_STORE_KIND, DELETED_STORE_METHOD])
def test_the_red_cases_name_modules_this_sweep_deleted(closure: KeepClosure, deleted: str) -> None:
    assert deleted not in module_names(SRC)
    assert deleted in closure.v1_only


def test_every_root_exists(closure: KeepClosure) -> None:
    names = module_names(SRC)
    missing = [
        root
        for root in closure.roots
        if not any(n == root or n.startswith(f"{root}.") for n in names)
    ]
    assert missing == []


def test_every_present_v1_only_module_is_inside_the_closure(closure: KeepClosure) -> None:
    """A v1-only module that left the closure would be unimportable by anyone."""
    names = module_names(SRC)
    inside = keep_closure_modules(closure.roots, names)
    stray = sorted(name for name in closure.v1_only if name in names and name not in inside)
    assert stray == []


# ---- red: a regrowth outside the closure ------------------------------------


@pytest.mark.parametrize("deleted", [DELETED_STORE_KIND, DELETED_STORE_METHOD])
def test_importing_a_deleted_v1_store_from_outside_the_closure_fails(
    tree: Path, closure: KeepClosure, deleted: str
) -> None:
    _plant(tree, "eawf.surfaces.cli.commands.regrown", f"from {deleted} import Row\n")

    assert find_violations(tree, closure) == [
        ClosureViolation(importer="eawf.surfaces.cli.commands.regrown", imported=deleted)
    ]


def test_a_lazy_import_of_a_live_v1_only_module_fails(tree: Path, closure: KeepClosure) -> None:
    _plant(
        tree,
        "eawf.workflow.regrown",
        "def chain():\n    from eawf.kernel.migrations import _base\n    return _base\n",
    )

    assert find_violations(tree, closure) == [
        ClosureViolation(importer="eawf.workflow.regrown", imported="eawf.kernel.migrations._base")
    ]


def test_main_exits_one_and_names_the_import(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _plant(tree, "eawf.workflow.regrown", f"import {DELETED_STORE_METHOD}\n")

    assert main(["--src-root", str(tree), "--closure", str(CLOSURE_FILE)]) == 1
    err = capsys.readouterr().err
    assert f"eawf.workflow.regrown imports {DELETED_STORE_METHOD}" in err


def test_the_same_import_inside_the_closure_passes(tree: Path, closure: KeepClosure) -> None:
    _plant(tree, "eawf.kernel.migrations.regrown", f"from {DELETED_STORE_KIND} import Row\n")

    assert find_violations(tree, closure) == []


# ---- the closure walk, on a small tree --------------------------------------


def test_a_package_and_its_submodule_report_once(tmp_path: Path) -> None:
    closure = KeepClosure(roots=("pkg.mig",), v1_only=("pkg.old",))
    _plant(tmp_path, "pkg.mig", "")
    _plant(tmp_path, "pkg.live", "import pkg.old\nfrom pkg.old import store\n")

    assert find_violations(tmp_path, closure) == [
        ClosureViolation(importer="pkg.live", imported="pkg.old")
    ]


def test_a_lazy_import_in_the_closure_does_not_widen_it(tmp_path: Path) -> None:
    _plant(tmp_path, "pkg.mig", "import pkg.helper\ndef late():\n    import pkg.live\n")
    _plant(tmp_path, "pkg.helper", "")
    _plant(tmp_path, "pkg.live", "")

    inside = keep_closure_modules(("pkg.mig",), module_names(tmp_path))

    assert inside == {"pkg.mig", "pkg.helper"}


def test_an_empty_tree_has_no_violations(tmp_path: Path) -> None:
    closure = KeepClosure(roots=("pkg.mig",), v1_only=("pkg.old",))
    assert find_violations(tmp_path, closure) == []


def test_a_module_that_is_not_python_raises(tmp_path: Path) -> None:
    _plant(tmp_path, "pkg.broken", "def oops(:\n")
    with pytest.raises(SyntaxError):
        find_violations(tmp_path, KeepClosure(roots=("pkg.mig",), v1_only=("pkg.old",)))


# ---- imported_names boundaries ----------------------------------------------


@pytest.mark.parametrize(
    ("source", "module", "is_package", "level_only", "expected"),
    [
        ("", "a.b", False, False, frozenset()),
        ("import a.c\n", "a.b", False, False, frozenset({"a.c"})),
        ("from a import c\n", "a.b", False, False, frozenset({"a", "a.c"})),
        ("from . import c\n", "a.b", False, False, frozenset({"a", "a.c"})),
        ("from .c import d\n", "a.b", True, False, frozenset({"a.b.c", "a.b.c.d"})),
        ("from ..c import d\n", "a.b.e", False, False, frozenset({"a.c", "a.c.d"})),
        ("def f():\n    import a.c\n", "a.b", False, True, frozenset()),
        ("def f():\n    import a.c\n", "a.b", False, False, frozenset({"a.c"})),
        (
            "try:\n    import a.c\nexcept ImportError:\n    pass\n",
            "a.b",
            False,
            True,
            frozenset({"a.c"}),
        ),
        ("if X:\n    import a.c\n", "a.b", False, True, frozenset({"a.c"})),
    ],
    ids=[
        "empty",
        "import",
        "from-import",
        "relative-module",
        "relative-package",
        "relative-two-levels",
        "lazy-skipped",
        "lazy-counted",
        "try-block",
        "if-block",
    ],
)
def test_imported_names_boundaries(
    source: str, module: str, is_package: bool, level_only: bool, expected: frozenset[str]
) -> None:
    assert (
        imported_names(source, module=module, is_package=is_package, module_level_only=level_only)
        == expected
    )


def test_imported_names_refuses_a_module_that_is_not_python() -> None:
    with pytest.raises(SyntaxError):
        imported_names("import (\n", module="a", is_package=False, module_level_only=False)


# ---- the closure file -------------------------------------------------------


def test_load_keep_closure_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_keep_closure(tmp_path / "absent.json")


@pytest.mark.parametrize(
    "body",
    [
        {"roots": ["a"]},
        {"roots": [], "v1_only": ["b"]},
        {"roots": ["a"], "v1_only": []},
        {"roots": ["a"], "v1_only": ["b"], "extra": 1},
        {"roots": "a", "v1_only": ["b"]},
        {"roots": [1], "v1_only": ["b"]},
    ],
    ids=["missing-key", "empty-roots", "empty-v1-only", "extra-key", "not-a-list", "not-a-name"],
)
def test_load_keep_closure_refuses_a_malformed_file(
    tmp_path: Path, body: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        load_keep_closure(_write_closure(tmp_path / "closure.json", **body))


def test_load_keep_closure_reads_a_single_entry_file(tmp_path: Path) -> None:
    loaded = load_keep_closure(
        _write_closure(tmp_path / "closure.json", roots=["a"], v1_only=["b"])
    )
    assert loaded == KeepClosure(roots=("a",), v1_only=("b",))
