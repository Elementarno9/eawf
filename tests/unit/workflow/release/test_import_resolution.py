"""Resolving source imports to the distributions that provide them.

The module-to-distribution map is read from ``pyproject.toml`` rather
than from the running interpreter. That is the property these cases
pin: the same source must resolve identically on a Linux runner, on a
Windows runner and on a developer laptop, none of which install the
same set of packages. Deriving the map from
``packages_distributions()`` alone cannot do that, because it can only
speak for what happens to be installed.

Every case builds its own checkout under ``tmp_path``. Reading the
repo's real ``pyproject.toml`` would make these tests restate the
current dependency list instead of testing the resolver.
"""

from __future__ import annotations

from pathlib import Path

from eawf.workflow.release.produce import SOURCE_PACKAGE, imported_distributions


def _checkout(root: Path, *, pyproject: str, source: str) -> Path:
    """Write a minimal checkout and return its root.

    Args:
        root: Directory to populate.
        pyproject: Contents of ``pyproject.toml``.
        source: Contents of the single source module that is scanned.

    Returns:
        The populated root, ready to hand to the resolver.
    """
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    package = root / SOURCE_PACKAGE
    package.mkdir(parents=True)
    (package / "module.py").write_text(source, encoding="utf-8")
    return root


def test_authored_map_resolves_a_module_absent_from_this_host(tmp_path: Path) -> None:
    """A declared mapping is honoured without the distribution installed.

    ``pywin32`` provides ``win32api`` and installs on Windows only, so
    no lookup against the running environment can resolve it here. The
    authored map is what makes the answer the same everywhere.
    """
    root = _checkout(
        tmp_path,
        pyproject='[tool.deptry.package_module_name_map]\npywin32 = ["win32api", "win32con"]\n',
        source="import win32api\nimport win32con\n",
    )
    assert imported_distributions(root) == ("pywin32",)


def test_optional_import_is_not_a_missing_dependency(tmp_path: Path) -> None:
    """A ``DEP001`` entry is guarded at its call site, so it is dropped."""
    root = _checkout(
        tmp_path,
        pyproject='[tool.deptry.per_rule_ignores]\nDEP001 = ["duckdb"]\n',
        source="import duckdb\n",
    )
    assert imported_distributions(root) == ()


def test_unmapped_import_keeps_its_own_name(tmp_path: Path) -> None:
    """An import nobody declares survives, so the lock check can red it.

    Falling back to the module's own name is what stops a typo'd or
    genuinely missing dependency from being silently resolved away.
    """
    root = _checkout(
        tmp_path,
        pyproject="[project]\nname = 'x'\n",
        source="import nowhere_to_be_found\n",
    )
    assert imported_distributions(root) == ("nowhere-to-be-found",)


def test_first_party_and_stdlib_imports_are_not_dependencies(tmp_path: Path) -> None:
    """The project's own package and the standard library are excluded."""
    root = _checkout(
        tmp_path,
        pyproject="[project]\nname = 'x'\n",
        source="from __future__ import annotations\nimport json\nimport eawf\n",
    )
    assert imported_distributions(root) == ()


def test_missing_pyproject_resolves_without_a_map(tmp_path: Path) -> None:
    """A checkout without the file still resolves, mapping nothing."""
    package = tmp_path / SOURCE_PACKAGE
    package.mkdir(parents=True)
    (package / "module.py").write_text("import win32api\n", encoding="utf-8")
    assert imported_distributions(tmp_path) == ("win32api",)
