"""Version coupling: ``pyproject.toml`` and ``src/eawf/__init__.py`` must agree."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import eawf

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_INIT = _REPO_ROOT / "src" / "eawf" / "__init__.py"


def _pyproject_version() -> str:
    data = tomllib.loads(_PYPROJECT.read_text())
    return str(data["project"]["version"])


def _init_version() -> str:
    text = _INIT.read_text()
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    assert match is not None, "no __version__ assignment in src/eawf/__init__.py"
    return match.group(1)


def test_pyproject_matches_init() -> None:
    assert _pyproject_version() == _init_version()


def test_runtime_matches_pyproject() -> None:
    assert eawf.__version__ == _pyproject_version()
