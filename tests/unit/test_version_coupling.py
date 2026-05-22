"""Version coupling: ``_version.py`` is the single source.

Post-P27-W27 the package version is single-sourced in
``src/eawf/_version.py`` and consumed by Hatchling via
``dynamic = ["version"]`` + ``[tool.hatch.version] path``. The package
re-exports ``__version__`` from ``_version.py``, so ``eawf.__version__``,
the version module, and the build all agree on one literal. ``pyproject``
no longer carries a static ``[project] version`` field.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import eawf

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_VERSION_FILE = _REPO_ROOT / "src" / "eawf" / "_version.py"


def _version_module_literal() -> str:
    text = _VERSION_FILE.read_text()
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    assert match is not None, "no __version__ assignment in src/eawf/_version.py"
    return match.group(1)


def test_pyproject_version_is_dynamic() -> None:
    data = tomllib.loads(_PYPROJECT.read_text())
    assert "version" in data["project"]["dynamic"]
    assert "version" not in data["project"]
    assert data["tool"]["hatch"]["version"]["path"] == "src/eawf/_version.py"


def test_runtime_matches_version_module() -> None:
    assert eawf.__version__ == _version_module_literal()
