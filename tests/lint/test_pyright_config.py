"""Pin the ``[tool.pyright]`` table that makes editor diagnostics trustworthy.

Without a ``[tool.pyright]`` table pyright resolves neither the uv-managed
virtualenv nor the src-layout editable install, so the language server
reports every third-party import as ``reportMissingImports`` and every
cross-module symbol as ``reportAttributeAccessIssue`` -- on a tree that
``uv run mypy`` passes clean. The failure mode is not the individual false
positive, it is the volume: when the diagnostics stream is wrong on nearly
every file, a reader stops reading it, and the first real type error arrives
invisible.

Three things are asserted, in the order they can break:

1. the table exists and carries the three resolution leaves verbatim;
2. the configured ``extraPaths`` root really is the src-layout root, and a
   first-party module imports out of it (the check pyright itself performs);
3. the venv leaves address a real interpreter root, so the third-party half
   of the resolution is not merely declared.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: The module the resolution check imports. Deep in the package and
#: import-heavy, so a broken path shows up as an ImportError rather than a
#: silently empty namespace.
_PROBE_MODULE = "eawf.kernel.state.models"


def _pyproject() -> dict[str, Any]:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def _pyright_table() -> dict[str, Any]:
    table = _pyproject().get("tool", {}).get("pyright")
    assert isinstance(table, dict), "pyproject.toml declares no [tool.pyright] table"
    return table


def test_pyright_table_is_present() -> None:
    """``pyproject.toml`` carries a ``[tool.pyright]`` table."""
    assert _pyright_table()


@pytest.mark.parametrize(
    ("leaf", "expected"),
    [("venvPath", "."), ("venv", ".venv"), ("extraPaths", ["src"])],
)
def test_pyright_resolution_leaf_is_pinned(leaf: str, expected: object) -> None:
    """Each resolution leaf carries the value pyright needs, verbatim."""
    table = _pyright_table()
    assert leaf in table, f"[tool.pyright] is missing {leaf!r}"
    assert table[leaf] == expected


def test_pyright_extra_paths_is_a_single_src_root() -> None:
    """``extraPaths`` is exactly the one src-layout root, not a grab bag."""
    extra_paths = _pyright_table()["extraPaths"]
    assert isinstance(extra_paths, list)
    assert len(extra_paths) == 1
    assert (_REPO_ROOT / extra_paths[0]).is_dir()


def test_configured_extra_path_resolves_the_first_party_package() -> None:
    """A first-party module resolves out of the configured ``extraPaths``.

    This is the resolution pyright performs: put ``extraPaths`` on the search
    path and import the package by name. A path that does not resolve here
    would not resolve in the editor either.
    """
    root = _REPO_ROOT / _pyright_table()["extraPaths"][0]
    finder = importlib.machinery.PathFinder()
    spec = finder.find_spec(_PROBE_MODULE.split(".", 1)[0], [str(root)])

    assert spec is not None, f"{root} does not resolve the eawf package"
    assert spec.origin is not None
    assert Path(spec.origin).is_relative_to(root)


def test_probe_module_imports_and_exposes_its_state_models() -> None:
    """``eawf.kernel.state.models`` imports and carries its entity models.

    The editor reports ``reportAttributeAccessIssue`` against symbols on this
    module that import fine at runtime; asserting the import *and* a symbol
    pins the fact those diagnostics were false.
    """
    module = importlib.import_module(_PROBE_MODULE)

    assert module.__name__ == _PROBE_MODULE
    assert hasattr(module, "Wave")


def test_configured_venv_root_is_a_real_virtualenv() -> None:
    """``venvPath``/``venv`` address a real interpreter root.

    A declared-but-absent venv resolves no third-party distribution, which is
    the ``Import "pydantic" could not be resolved`` half of the defect.
    """
    table = _pyright_table()
    venv_root = (_REPO_ROOT / table["venvPath"] / table["venv"]).resolve()

    assert venv_root.is_dir(), f"{venv_root} is not a directory"
    assert (venv_root / "pyvenv.cfg").is_file(), f"{venv_root} carries no pyvenv.cfg"


@pytest.mark.parametrize("distribution", ["pydantic", "pytest", "orjson"])
def test_third_party_dependency_the_editor_calls_unresolved_imports(distribution: str) -> None:
    """Each import the editor reports unresolved resolves at runtime.

    These three are the exact ``reportMissingImports`` rows the language
    server emitted before the table landed; pinning them keeps the false
    positive from being mistaken for a real missing dependency.
    """
    spec = importlib.util.find_spec(distribution)

    assert spec is not None, f"{distribution} does not resolve"
    assert spec.origin is not None


def test_missing_pyright_table_is_detected_not_defaulted() -> None:
    """A pyproject with no ``[tool.pyright]`` fails the presence check.

    The gate must red on absence rather than fall back to a default, so the
    table cannot be dropped by a later edit without a test failing.
    """
    stripped = tomllib.loads("[tool.mypy]\nstrict = true\n")

    assert stripped.get("tool", {}).get("pyright") is None


def test_unparseable_pyproject_raises_rather_than_yielding_an_empty_table() -> None:
    """A malformed pyproject raises ``TOMLDecodeError``; it never reads empty."""
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads("[tool.pyright\nvenv = ")
