"""The 0.6.8 upgrade guide's commands work on the tree eawf writes.

A ``git add`` that names an ignored path refuses the whole command, so a
path the managed ``.gitignore`` block ignores would stop the guide's commit.
The tests render that block into a real repository and ask git itself, so
the check follows gitignore semantics rather than a reimplementation.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from eawf.platform.install.gitignore_writer import write_gitignore

pytestmark = pytest.mark.integration

_GUIDE = Path(__file__).resolve().parents[4] / "docs" / "help" / "upgrade-from-0.6.md"
_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_ROLLBACK_HEADING = "## Roll back to 0.6.8"


def _git_add_paths(markdown: str) -> list[str]:
    """Return every path a ``git add`` line in a fenced block names, in order."""
    paths: list[str] = []
    for block in _FENCE.findall(markdown):
        for line in block.splitlines():
            if not line.startswith("git add "):
                continue
            command = line.removeprefix("git add ").split("&&", 1)[0]
            paths.extend(token for token in command.split() if not token.startswith("-"))
    return paths


def _section(markdown: str, heading: str) -> str:
    """Return the body under ``heading`` up to the next level-two heading.

    Raises:
        KeyError: When the guide has no such heading.
    """
    lines = markdown.splitlines()
    try:
        start = lines.index(heading) + 1
    except ValueError as exc:
        raise KeyError(heading) from exc
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


@pytest.fixture
def managed_repo(tmp_path: Path) -> Path:
    """A git repository whose only ignore rules are eawf's managed block."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    write_gitignore(tmp_path)
    subprocess.run([git, "init", "-q", str(tmp_path)], check=True, env=_isolated_env())
    return tmp_path


def _isolated_env() -> dict[str, str]:
    """Keep the operator's global excludes out of the ignore check."""
    return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _ignored(repo: Path, path: str) -> bool:
    """Return whether git ignores ``path`` in ``repo``."""
    result = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", "--", path],
        env=_isolated_env(),
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"git check-ignore failed on {path!r}: exit {result.returncode}")
    return result.returncode == 0


def test_git_add_paths_are_not_ignored_by_the_managed_block(managed_repo: Path) -> None:
    paths = _git_add_paths(_GUIDE.read_text(encoding="utf-8"))
    assert len(paths) >= 2, "the guide's git add lines were not found"
    ignored = [path for path in paths if _ignored(managed_repo, path)]
    assert ignored == []


def test_git_add_check_reports_an_ignored_runtime_path(managed_repo: Path) -> None:
    # The path the guide used to stage: the check must flag it, or the
    # test above passes vacuously.
    assert _ignored(managed_repo, ".claude/settings.json")
    assert not _ignored(managed_repo, "AGENTS.md")


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("", []),
        ("```text\ngit status\n```\n", []),
        ("```text\ngit add .ea\n```\n", [".ea"]),
        (
            "```text\ngit add -A .ea AGENTS.md && git commit -m 'git add x'\n```\n",
            [".ea", "AGENTS.md"],
        ),
        ("git add outside-a-fence\n", []),
    ],
)
def test_git_add_parser_boundaries(markdown: str, expected: list[str]) -> None:
    assert _git_add_paths(markdown) == expected


def test_rollback_section_names_each_step() -> None:
    section = _section(_GUIDE.read_text(encoding="utf-8"), _ROLLBACK_HEADING)
    assert "Work done after the upgrade is not kept." in section
    assert "eawf daemon stop" in section
    assert "git reset --hard" in section
    assert "Record the tree before the 0.7 upgrade" in section
    assert "diff -r .ea ../PROJ-ea-before-epoch2" in section
    assert '"eawf==0.6.8"' in section


def test_rollback_section_follows_the_cutover_steps() -> None:
    markdown = _GUIDE.read_text(encoding="utf-8")
    assert markdown.index("## 3. Apply, refresh and commit") < markdown.index(_ROLLBACK_HEADING)


def test_rollback_section_lookup_rejects_a_missing_heading() -> None:
    with pytest.raises(KeyError):
        _section("# Title\n\n## Other\n", _ROLLBACK_HEADING)


def test_rollback_section_stops_at_the_next_heading() -> None:
    markdown = f"{_ROLLBACK_HEADING}\nbody\n## Next\nlater\n"
    assert _section(markdown, _ROLLBACK_HEADING) == "body"
