"""The Vale prose linter stays removed from the tree, the hooks, CI and the CLI.

Vale reported nothing for months: CI scanned no files, the binary was absent
locally so the hook skipped, and its error-level threshold silenced the
custom rules. These checks red when its configuration, a whole-word mention
in the places that wired it, its command, or its fold-in parameter returns.
"""

from __future__ import annotations

import inspect
import re
import subprocess
from pathlib import Path
from typing import Final

import pytest
from typer.testing import CliRunner

from eawf.platform.lint.validate_prose import validate_prose
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]

#: The files and trees that wired Vale in, repo-relative.
SCANNED: Final = (".pre-commit-config.yaml", ".github/workflows/ci.yaml", ".gitignore", "src/eawf")

#: The identifiers and CI step names the removal deleted.
GONE: Final = (
    "_collect_vale_rows",
    "vale_rows",
    "Install Vale + sync styles",
    "Validate prose (strict)",
)

_VALE_WORD: Final = re.compile(r"\bvale\b", re.IGNORECASE)


def _vale_mentions(text: str) -> list[int]:
    """Return the 1-based lines of *text* that name Vale as a whole word, any case."""
    return [number for number, line in enumerate(text.splitlines(), 1) if _VALE_WORD.search(line)]


def _scanned_files() -> list[Path]:
    """Return every file under :data:`SCANNED`, a directory expanded to its text files."""
    files: list[Path] = []
    for rel in SCANNED:
        path = REPO / rel
        if path.is_dir():
            files.extend(p for p in sorted(path.rglob("*")) if p.suffix in {".py", ".md"})
        else:
            files.append(path)
    return files


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("", []),
        ("vale", [1]),
        ("ok\nInstall Vale + sync styles\n", [2]),
        ("hook vale-prose", [1]),
        ("VALE", [1]),
        ("prevalent equivalent valence", []),
    ],
)
def test_vale_mentions_matches_whole_words_only(text: str, lines: list[int]) -> None:
    """Boundary: the scanner finds the word in any case and ignores words containing it."""
    assert _vale_mentions(text) == lines


def test_no_vale_left() -> None:
    """No tracked Vale file, whole-word mention, removed name or CI step remains."""
    tracked = subprocess.run(
        ["git", "ls-files", ".vale.ini", ".vale"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert tracked == ""

    offenders: dict[str, list[int]] = {}
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8")
        found = _vale_mentions(text)
        found.extend(
            number
            for number, line in enumerate(text.splitlines(), 1)
            if any(name in line for name in GONE)
        )
        if found:
            offenders[path.relative_to(REPO).as_posix()] = sorted(set(found))
    assert offenders == {}

    assert "vale_rows" not in inspect.signature(validate_prose).parameters


def test_vale_prose_command_gone() -> None:
    """``eawf hook vale-prose`` is a usage error naming no such command."""
    result = CliRunner().invoke(app, ["hook", "vale-prose"])

    assert result.exit_code == 2
    assert "No such command" in result.output
