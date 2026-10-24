"""Every test path a bundled profile's floor check runs exists in the repository.

A floor check whose ``cmd`` names a moved or deleted test file runs pytest
on nothing and reports an error instead of the smoke it was declared for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from eawf.platform.profiles.loader import list_profiles, load_profile
from eawf.platform.profiles.trust import is_bundled

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]


def missing_test_paths(cmd: list[str], root: Path) -> list[str]:
    """Return each ``tests/`` argument of *cmd* that names no file or directory under *root*.

    Args:
        cmd: One floor check's argv.
        root: The repository root the paths are relative to.

    Returns:
        The missing paths, in argv order; a ``::`` node suffix is ignored.
    """
    paths = [arg.split("::", 1)[0] for arg in cmd if arg.startswith("tests/")]
    return [path for path in paths if not (root / path).exists()]


@pytest.mark.parametrize("profile_id", [p for p in list_profiles() if is_bundled(p)])
def test_every_floor_check_test_path_exists(profile_id: str) -> None:
    body = load_profile(profile_id)
    checks = [] if body.verify is None else body.verify.floor_checks
    assert {check.name: missing_test_paths(check.cmd, REPO) for check in checks} == {
        check.name: [] for check in checks
    }


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ([], []),
        (["uv", "run", "pytest", "-q"], []),
        (["uv", "run", "pytest", "tests/lint"], []),
        (["uv", "run", "pytest", "tests/lint/test_profile_floor_check_paths.py::x"], []),
        (
            ["uv", "run", "pytest", "tests/unit/test_profile_cli.py"],
            ["tests/unit/test_profile_cli.py"],
        ),
    ],
    ids=["empty", "no-path", "directory", "node-id", "moved-file"],
)
def test_missing_test_paths_boundaries(cmd: list[str], expected: list[str]) -> None:
    assert missing_test_paths(cmd, REPO) == expected
