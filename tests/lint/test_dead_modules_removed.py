"""The test-only modules outside epoch 1 stay deleted.

Ten modules under ``src/eawf`` had no production caller: only their own
unit tests, and one package re-export, reached them. These checks red when
one of them returns, when a module under ``src``, ``tools`` or ``tests``
imports one again, or when the repository configuration or the
dead-module advisory names one.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

from tools.dead_modules import find_dead_modules

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]

#: The deleted modules, by dotted module name.
DELETED_MODULES: Final = (
    "eawf.kernel.spec.pruning",
    "eawf.observability.eval.experiments.rule_adherence",
    "eawf.observability.telemetry.metrics_projection",
    "eawf.observability.telemetry.otel",
    "eawf.runtime.runtimes.dispatch",
    "eawf.platform.lint.tools.eawf020_laconic_bullet",
    "eawf.surfaces.render.eu",
    "eawf.surfaces.render.spinner",
    "eawf.surfaces.render.changelog",
    "eawf.surfaces.render.agent_report",
)

#: The configuration files that may name a module path.
CONFIG_FILES: Final = ("pyproject.toml", ".pre-commit-config.yaml")


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


def _deleted_imports(path: Path) -> list[str]:
    """Return the deleted modules *path* imports, sorted."""
    return sorted(set(DELETED_MODULES) & _imported_modules(path))


def _config_mentions(text: str) -> list[str]:
    """Return the deleted modules *text* names by source path or dotted name."""
    return [name for name in DELETED_MODULES if _module_path(name) in text or name in text]


def test_test_only_modules_are_gone() -> None:
    present = [name for name in DELETED_MODULES if (REPO / _module_path(name)).exists()]
    assert present == []
    importers = {
        str(path.relative_to(REPO)): hits
        for top in ("src", "tools", "tests")
        for path in sorted((REPO / top).rglob("*.py"))
        if (hits := _deleted_imports(path))
    }
    assert importers == {}


def test_no_config_names_a_deleted_module() -> None:
    named = {
        config: hits
        for config in CONFIG_FILES
        if (hits := _config_mentions((REPO / config).read_text(encoding="utf-8")))
    }
    assert named == {}
    assert sorted(set(DELETED_MODULES) & set(find_dead_modules())) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", []),
        ("import eawf.surfaces.render.eu\n", ["eawf.surfaces.render.eu"]),
        ("from eawf.surfaces.render import spinner\n", ["eawf.surfaces.render.spinner"]),
        (
            "def f():\n    from eawf.kernel.spec.pruning import prune\n",
            ["eawf.kernel.spec.pruning"],
        ),
        (
            "from eawf.observability.telemetry import otel, metrics_projection\n",
            [
                "eawf.observability.telemetry.metrics_projection",
                "eawf.observability.telemetry.otel",
            ],
        ),
        ("from eawf.surfaces.render import regions\n", []),
        ("from . import dispatch\n", []),
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        (
            '"src/eawf/surfaces/render/agent_report.py" = ["C901"]\n',
            ["eawf.surfaces.render.agent_report"],
        ),
        (
            "entry: uv run python -m eawf.platform.lint.tools.eawf020_laconic_bullet\n",
            ["eawf.platform.lint.tools.eawf020_laconic_bullet"],
        ),
        ('"src/eawf/surfaces/render/regions.py" = ["C901"]\n', []),
    ],
)
def test_config_mentions_matches_path_and_dotted_name(text: str, expected: list[str]) -> None:
    assert _config_mentions(text) == expected
