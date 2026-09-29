"""LINT-032: a test file resolves to one kind directory, whose marker is auto-applied.

A scratch suite laid out as ``tests/<kind>/...`` runs under the
repository's own collection hook, with no marker written in any file: a
``-m <kind>`` selection still picks exactly the files under that kind's
directory, and a file that declares a different kind's marker than its
directory fails collection.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from eawf.platform.lint.kind_taxonomy import TestKind, kind_directory, kind_for_test_path

_REPO = Path(__file__).resolve().parents[4]

_CONFTEST = "from tests.conftest import pytest_collection_modifyitems  # noqa: F401\n"
_PLAIN = "def test_it():\n    assert True\n"


def _suite(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "scratch"
    (root / "tests").mkdir(parents=True)
    (root / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    for relative, source in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(source, encoding="utf-8")
    return root


def _collect(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            *("-m", "pytest", "-p", "no:cacheprovider", "--rootdir", str(root)),
            *("--collect-only", "-q", *args),
        ],
        cwd=root,
        env=os.environ | {"PYTHONPATH": str(_REPO)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("kind", list(TestKind))
def test_lint_032_a_file_under_a_kind_directory_resolves_to_that_kind_only(
    kind: TestKind,
) -> None:
    path = f"{kind_directory(kind)}platform/lint/test_subject.py"

    assert kind_for_test_path(path) is kind
    assert [other for other in TestKind if path.startswith(kind_directory(other))] == [kind]


@pytest.mark.parametrize(
    "path",
    ["tests/test_smoke.py", "tests/lint/test_x.py", "tests/unit/fixture.json", "src/eawf/x.py"],
)
def test_lint_032_a_file_outside_every_kind_directory_resolves_to_none(path: str) -> None:
    assert kind_for_test_path(path) is None


def test_lint_032_every_kind_marker_is_auto_applied_from_the_directory(tmp_path: Path) -> None:
    """No file writes a marker, yet ``-m`` selects each kind's own files."""
    root = _suite(
        tmp_path,
        {
            "tests/unit/platform/lint/test_a.py": _PLAIN,
            "tests/contract/platform/lint/test_b.py": _PLAIN,
            "tests/integration/platform/lint/test_c.py": _PLAIN,
        },
    )

    unit = _collect(root, "-m", "unit")
    contract = _collect(root, "-m", "contract")

    assert unit.returncode == 0, unit.stdout
    assert "tests/unit/platform/lint/test_a.py::test_it" in unit.stdout
    assert "test_b.py" not in unit.stdout and "test_c.py" not in unit.stdout
    assert "tests/contract/platform/lint/test_b.py::test_it" in contract.stdout
    assert "test_a.py" not in contract.stdout


def test_lint_032_a_marker_contradicting_the_directory_fails_collection(tmp_path: Path) -> None:
    """Gate fire: a unit-directory file that declares itself e2e."""
    source = "import pytest\n\npytestmark = pytest.mark.e2e\n\n" + _PLAIN
    root = _suite(tmp_path, {"tests/unit/platform/lint/test_a.py": source})

    result = _collect(root)

    assert result.returncode != 0
    assert "test-kind marker conflict" in result.stdout + result.stderr
