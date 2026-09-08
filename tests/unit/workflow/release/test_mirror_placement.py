"""Release behaviour tests are filed under the package they exercise.

Eight modules exercising :mod:`eawf.workflow.release` sat under
``tests/unit/kernel/release/`` because that is where the first release
wave happened to start. A test's directory is its address: a reader
opening ``src/eawf/workflow/release/target_machine.py`` and looking for
its tests under the mirrored path found nothing, and the kernel mirror
grew coverage for a package it does not contain.

The line drawn here is not "no workflow import under the kernel mirror"
-- a kernel test legitimately reaches for the authored train to build a
fixture. It is that a module filed under the kernel mirror must have a
kernel subject at all: if its only release imports are workflow ones,
the workflow package is what it is about, and that is where it belongs.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from eawf.platform.lint.eawf025_test_placement import check_test_paths, source_package_paths

REPO_ROOT = Path(__file__).resolve().parents[4]

KERNEL_MIRROR = REPO_ROOT / "tests" / "unit" / "kernel" / "release"

WORKFLOW_MIRROR = REPO_ROOT / "tests" / "unit" / "workflow" / "release"

#: The modules refiled out of the kernel mirror. Named rather than
#: globbed so a module quietly moved back reds this test.
REFILED_MODULES: tuple[str, ...] = (
    "test_distinct_checkpoint.py",
    "test_import_resolution.py",
    "test_observe_adapters.py",
    "test_observer_only.py",
    "test_prerelease_channel.py",
    "test_prerequisite_receipts.py",
    "test_target_status_machine.py",
    "test_train_advance.py",
)


def _imported_packages(path: Path) -> frozenset[str]:
    """Return the ``eawf.*`` modules *path* imports from.

    Walks the module AST rather than matching text, so a package named
    only inside a docstring or a string literal is not counted as a
    subject.

    Args:
        path: Python module to inspect.

    Returns:
        Every dotted ``eawf`` module named by an ``import`` or
        ``from ... import`` statement.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names if alias.name.startswith("eawf"))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("eawf"):
            modules.add(node.module or "")
    return frozenset(modules)


def _release_test_paths() -> list[str]:
    """Return every release test path, repo-relative, in sorted order."""
    roots = (
        KERNEL_MIRROR,
        WORKFLOW_MIRROR,
        REPO_ROOT / "tests" / "integration" / "workflow" / "release",
    )
    return sorted(
        str(path.relative_to(REPO_ROOT))
        for root in roots
        if root.is_dir()
        for path in root.rglob("*.py")
    )


@pytest.mark.parametrize("module_name", REFILED_MODULES)
def test_release_behaviour_tests_are_mirrored(module_name: str) -> None:
    """Each refiled module lives under the workflow mirror, not the kernel one."""
    assert (WORKFLOW_MIRROR / module_name).is_file()
    assert not (KERNEL_MIRROR / module_name).exists()


def test_the_kernel_mirror_holds_no_workflow_only_subject() -> None:
    """A kernel-mirror module reaching into workflow also has a kernel subject."""
    misfiled = {}
    for path in sorted(KERNEL_MIRROR.glob("*.py")):
        imported = _imported_packages(path)
        workflow = {name for name in imported if name.startswith("eawf.workflow.release")}
        kernel = {name for name in imported if name.startswith("eawf.kernel.release")}
        if workflow and not kernel:
            misfiled[path.name] = sorted(workflow)
    assert misfiled == {}


def test_the_workflow_mirror_holds_release_workflow_subjects() -> None:
    """Every refiled module names the package its new address claims."""
    for module_name in REFILED_MODULES:
        imported = _imported_packages(WORKFLOW_MIRROR / module_name)
        assert any(name.startswith("eawf.workflow.release") for name in imported), module_name


def test_the_placement_lint_passes_over_the_release_subsystem() -> None:
    """EAWF025 reports no violation across every release test path."""
    paths = _release_test_paths()
    assert paths, "the release subsystem must own test files, or this proves nothing"
    violations = check_test_paths(paths, source_packages=source_package_paths(REPO_ROOT))
    assert [violation.render() for violation in violations] == []


def test_the_placement_lint_rejects_the_pre_refile_address() -> None:
    """The lint has teeth: an unmirrored chain is still a violation."""
    violations = check_test_paths(
        ["tests/unit/workflow/no_such_package/test_x.py"],
        source_packages=source_package_paths(REPO_ROOT),
    )
    assert len(violations) == 1
    assert "is not a package under src/eawf/" in violations[0].render()


def test_the_placement_lint_accepts_an_empty_path_set() -> None:
    """No candidate paths yields no violations (the empty boundary)."""
    assert check_test_paths([], source_packages=source_package_paths(REPO_ROOT)) == []
