"""Tests for the EAWF025 test-placement lint and its hook command.

Covers the two checks the rule makes -- a new test lives under
``tests/<kind>/<source-package-path>/``, and no subject is filed under
two kinds at once -- across boundary and error paths, that the exempt
subsystem directories stay clean, and that the ``eawf hook
eawf025-test-placement`` command exits non-zero on each violation and
zero on a conforming set, plus the whole-tree sweep that proves the
collapse landed: every file at a kind + mirror address, no subject filed
under two kinds, and the ``ls tests/`` inventory free of the retired
subsystem directories.

``TestKind`` is imported under the alias ``Kind`` because pytest treats a
module-level ``Test*`` name in a test module as a candidate test class.
"""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.platform.lint.eawf025_test_placement import (
    RULE_CODE,
    check_test_paths,
    source_package_paths,
)
from eawf.platform.lint.eawf025_test_placement import (
    TestPlacementViolation as PlacementViolation,
)
from eawf.platform.lint.kind_taxonomy import TestKind as Kind
from eawf.surfaces.cli.app import app

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGES = frozenset({"", "kernel", "kernel/state", "surfaces/cli"})

runner = CliRunner()


def _exit_code(*paths: str) -> tuple[int, dict[str, object]]:
    """Run the placement hook over ``paths`` and return its exit + payload."""
    result = runner.invoke(app, ["--json", "hook", "eawf025-test-placement", *paths])
    payload: dict[str, object] = orjson.loads(result.stdout)
    return result.exit_code, payload


def _check_one(path: str) -> PlacementViolation | None:
    """Return the single violation the rule reports for ``path``, or ``None``."""
    violations = check_test_paths([path], source_packages=_PACKAGES)
    return violations[0] if violations else None


# --- placement lint: conforming paths --------------------------------------


def test_placement_lint_accepts_a_mirrored_path() -> None:
    assert _check_one("tests/unit/kernel/state/test_models.py") is None


def test_placement_lint_accepts_a_file_at_the_kind_root() -> None:
    # Boundary: an empty mirror chain maps to the package root.
    assert _check_one("tests/golden/test_render.py") is None


def test_placement_lint_exempts_a_non_kind_subsystem_directory() -> None:
    assert _check_one("tests/kernel/state/test_models.py") is None


def test_placement_lint_ignores_a_file_directly_under_tests() -> None:
    assert _check_one("tests/conftest.py") is None


def test_placement_lint_ignores_a_non_python_file() -> None:
    assert _check_one("tests/unit/kernel/fixture.json") is None


def test_placement_lint_ignores_a_path_outside_the_suite() -> None:
    assert _check_one("src/eawf/kernel/state/models.py") is None


# --- placement lint: violations --------------------------------------------


def test_placement_lint_rejects_an_out_of_taxonomy_kind_directory() -> None:
    violation = _check_one("tests/smoke/kernel/test_x.py")
    assert violation is not None
    assert violation.code == RULE_CODE
    assert violation.path == "tests/smoke/kernel/test_x.py"
    assert "not a declared test kind" in violation.reason


def test_placement_lint_rejects_a_mirror_chain_with_no_source_package() -> None:
    violation = _check_one("tests/unit/nosuchpkg/test_x.py")
    assert violation is not None
    assert "'nosuchpkg'" in violation.reason
    assert "src/eawf/" in violation.reason


def test_placement_lint_rejects_a_subject_filed_under_two_kinds() -> None:
    violations = check_test_paths(
        [
            "tests/unit/kernel/state/test_models.py",
            "tests/integration/kernel/state/test_models.py",
        ],
        source_packages=_PACKAGES,
    )
    assert [v.path for v in violations] == [
        "tests/integration/kernel/state/test_models.py",
        "tests/unit/kernel/state/test_models.py",
    ]
    assert all("filed under two kinds" in v.reason for v in violations)
    assert "integration, unit" in violations[0].reason


def test_placement_lint_allows_scaffolding_under_every_kind() -> None:
    # ``__init__.py`` / ``conftest.py`` are per-directory scaffolding, not
    # subjects, so repeating them across kinds is not a two-kind filing.
    assert (
        check_test_paths(
            [
                "tests/unit/kernel/__init__.py",
                "tests/integration/kernel/__init__.py",
                "tests/unit/kernel/conftest.py",
                "tests/integration/kernel/conftest.py",
            ],
            source_packages=_PACKAGES,
        )
        == []
    )


def test_placement_lint_allows_the_same_subject_name_under_one_kind() -> None:
    assert (
        check_test_paths(
            ["tests/unit/kernel/test_x.py", "tests/unit/kernel/state/test_x.py"],
            source_packages=_PACKAGES,
        )
        == []
    )


def test_placement_lint_reports_a_misplaced_path_once() -> None:
    violations = check_test_paths(
        ["tests/smoke/test_x.py", "tests/unit/test_x.py"], source_packages=_PACKAGES
    )
    assert [v.path for v in violations] == ["tests/smoke/test_x.py"]


def test_placement_lint_on_an_empty_input_is_clean() -> None:
    # Boundary: a commit that adds no test file at all.
    assert check_test_paths([], source_packages=_PACKAGES) == []


def test_placement_lint_on_a_single_conforming_path_is_clean() -> None:
    # Boundary: exactly one path.
    assert check_test_paths(["tests/unit/test_x.py"], source_packages=_PACKAGES) == []


def test_placement_lint_render_prefixes_the_rule_code() -> None:
    violation = PlacementViolation(path="tests/smoke/test_x.py", reason="because")
    assert violation.render() == f"{RULE_CODE} because"


# --- source package discovery ----------------------------------------------


def test_source_package_paths_includes_the_root_and_a_nested_package() -> None:
    packages = source_package_paths(_REPO_ROOT)
    assert "" in packages
    assert "kernel/state" in packages
    assert "__pycache__" not in packages


def test_source_package_paths_on_a_treeless_root_is_just_the_root(tmp_path: Path) -> None:
    assert source_package_paths(tmp_path) == frozenset({""})


def test_source_package_paths_rejects_a_non_path() -> None:
    with pytest.raises(TypeError):
        source_package_paths("not-a-path")  # type: ignore[arg-type]


# --- hook command: cli exit codes ------------------------------------------


def test_placement_lint_cli_exits_zero_on_a_conforming_path() -> None:
    exit_code, payload = _exit_code("tests/unit/kernel/state/test_models.py")
    assert exit_code == 0
    assert payload["clean"] is True
    assert payload["scanned"] == 1


def test_placement_lint_cli_exits_nonzero_outside_the_taxonomy() -> None:
    exit_code, payload = _exit_code("tests/smoke/kernel/test_x.py")
    assert exit_code == 1
    assert payload["violations"] == 1


def test_placement_lint_cli_exits_nonzero_for_a_two_kind_subject() -> None:
    exit_code, payload = _exit_code(
        "tests/unit/kernel/state/test_models.py",
        "tests/integration/kernel/state/test_models.py",
    )
    assert exit_code == 1
    assert payload["violations"] == 2


def test_placement_lint_cli_exits_zero_for_the_exempt_subsystem_dirs() -> None:
    exit_code, payload = _exit_code("tests/lint/test_x.py", "tests/platform/lint/test_y.py")
    assert exit_code == 0
    assert payload["clean"] is True


# --- the whole tree, not a staged delta ------------------------------------

#: The ``tests/`` sub-directories that legitimately partition by something
#: other than kind, and so stay exempt after the tree collapse. ``fixtures``
#: and ``snapshots`` hold committed bytes rather than subjects, ``eval`` is
#: the opt-in lane the default marker expression deselects, and ``lint`` +
#: ``platform`` hold the repo's own lint suite -- the CI and wave gates that
#: prove the collapse point INTO them, so moving them under a kind would cost
#: those gates their own address.
SURVIVING_NON_KIND_DIRS: frozenset[str] = frozenset(
    {"eval", "fixtures", "lint", "platform", "snapshots"}
)

#: Directories the collapse retired for naming something other than a kind:
#: a reason a test was written (``acceptance``, ``regression``), an ambiguous
#: pair one letter apart (``runtime`` / ``runtimes``), and the shadow trees
#: named after a ``src/eawf`` package instead of after a kind.
RETIRED_TEST_DIRS: frozenset[str] = frozenset(
    {
        "acceptance",
        "cli",
        "config",
        "daemon",
        "kernel",
        "observability",
        "regression",
        "runtime",
        "runtimes",
        "state",
        "workflow",
    }
)


def _whole_tree_paths() -> list[str]:
    """Return every ``.py`` file under ``tests/``, repo-relative."""
    return sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for path in (_REPO_ROOT / "tests").rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _whole_tree_violations(extra: str | None = None) -> list[PlacementViolation]:
    """Run EAWF025 over the whole tree, optionally with one injected path."""
    paths = _whole_tree_paths() if extra is None else [*_whole_tree_paths(), extra]
    return check_test_paths(
        paths,
        source_packages=source_package_paths(_REPO_ROOT),
        non_kind_dirs=SURVIVING_NON_KIND_DIRS,
    )


def _suite_directory_names() -> set[str]:
    """Return the ``ls tests/`` directory inventory."""
    return {
        entry.name
        for entry in (_REPO_ROOT / "tests").iterdir()
        if entry.is_dir() and not entry.name.startswith((".", "__"))
    }


def test_placement_whole_tree_has_no_violations() -> None:
    """Every test file in the repo sits at tests/<kind>/<source-package-path>/.

    The hook is diff-scoped because the pre-taxonomy tree did not mirror
    the source layout. This runs the same rule over the WHOLE tree with
    only the five genuinely non-kind directories exempt, so a file re-filed
    into a subsystem shadow tree reds here instead of slipping past a scan
    that only ever sees one commit's delta.
    """
    assert [violation.render() for violation in _whole_tree_violations()] == []


def test_placement_whole_tree_cli_exits_zero() -> None:
    """``eawf hook eawf025-test-placement`` over the whole tree exits 0."""
    exit_code, payload = _exit_code(*_whole_tree_paths())
    assert exit_code == 0
    assert payload["clean"] is True


def test_placement_whole_tree_reds_on_a_re_added_shadow_tree() -> None:
    """The whole-tree scan fires on the defect it exists to catch.

    A subject re-filed under a top-level subsystem directory -- the shape
    the collapse removed -- is not a declared kind, so the scan rejects it.
    """
    violations = _whole_tree_violations("tests/daemon/test_reintroduced.py")
    assert [violation.path for violation in violations] == ["tests/daemon/test_reintroduced.py"]


def test_one_home_per_subject_admits_no_two_kind_filing() -> None:
    """No subject in the tree is filed under two kinds at once.

    A subject filed twice has no owning kind: neither copy is the one that
    must stay green, so both drift. The exclusivity half of EAWF025 runs
    here over the whole tree rather than over a commit's delta.
    """
    doubled = [
        violation.render()
        for violation in _whole_tree_violations()
        if "filed under two kinds" in violation.reason
    ]
    assert doubled == []


def test_one_home_per_subject_retired_directories_are_absent() -> None:
    """The ``ls tests/`` inventory names no retired subsystem tree.

    The collapse is only real if the old addresses are GONE: a surviving
    directory is a second home the next test can be filed into.
    """
    assert sorted(_suite_directory_names() & RETIRED_TEST_DIRS) == []


def test_one_home_per_subject_inventory_is_kinds_plus_the_exempt_five() -> None:
    """Every ``tests/`` sub-directory is a declared kind or a declared exemption."""
    unexplained = _suite_directory_names() - {kind.value for kind in Kind} - SURVIVING_NON_KIND_DIRS
    assert sorted(unexplained) == []
