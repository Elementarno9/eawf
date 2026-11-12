"""The 0.7.3 cut: the package version and the changelog section naming its six areas."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import eawf
from eawf._version import __version__
from eawf.surfaces.cli._version_display import compose_display_version

_CHANGELOG = Path(__file__).resolve().parents[4] / "CHANGELOG.md"

#: The six areas of the 0.7.3 patch, each a third-level heading of its section.
_AREAS = (
    "Daemon",
    "Data-loss guard",
    "Host tool observations",
    "Approval seals",
    "Test suite",
    "Release",
)

#: What the upgrade notes must tell a user before the areas begin.
_UPGRADE_PHRASES = (
    "`eawf daemon stop`",
    "only once the daemon process has exited",
    "`daemon stop failed`",
    "`unlocked_state_commit`",
    "`git commit`",
    "`git cherry-pick`",
    "`eawf git commit -- <git args>`",
    "`eawf git cherry-pick -- <git args>`",
    "No state migration is required from 0.7.2",
    "replaced by one for 0.7.3",
    "plugin_install goldens are regenerated",
)


def _section(text: str, version: str) -> list[str]:
    """Return the lines of the ``## [<version>]`` section, heading excluded.

    Raises:
        LookupError: The changelog has no section for *version*.
    """
    lines = text.splitlines()
    heading = re.compile(rf"^## \[{re.escape(version)}\]( - \d{{4}}-\d{{2}}-\d{{2}})?$")
    starts = [index for index, line in enumerate(lines) if heading.match(line)]
    if not starts:
        raise LookupError(version)
    body = lines[starts[0] + 1 :]
    end = next((index for index, line in enumerate(body) if line.startswith("## ")), len(body))
    return body[:end]


def test_version_and_changelog() -> None:
    assert __version__ == "0.7.3"
    assert eawf.__version__ == "0.7.3"
    assert compose_display_version().split("+")[0] == "0.7.3"

    text = _CHANGELOG.read_text(encoding="utf-8")
    first = next(line for line in text.splitlines() if line.startswith("## "))
    assert first == "## [0.7.3] - 2026-11-12"
    section = _section(text, "0.7.3")
    headings = [line.removeprefix("### ") for line in section if line.startswith("### ")]
    assert headings == ["Upgrade notes", *_AREAS]
    body = "\n".join(section)
    # The release probe needs one line naming the migration outcome.
    assert "No state migration is required from 0.7.2" in body
    upgrade = "\n".join(section[: section.index(f"### {_AREAS[0]}")])
    for phrase in _UPGRADE_PHRASES:
        assert phrase in upgrade, phrase
    # A changelog speaks to users: no lifecycle keys, no machine paths.
    assert not re.search(r"\b(?:EAWF|BAT|MLS|PRV)-\d{4}\b", body)
    # An absolute path opens with a slash that follows no path character.
    assert not re.search(r"(?<![\w.$>-])/[\w.-]+/", body)
    # Every area has at least one bullet under its heading.
    for area in ("Upgrade notes", *_AREAS):
        start = section.index(f"### {area}")
        following = section[start + 1 :]
        stop = next(
            (index for index, line in enumerate(following) if line.startswith("### ")),
            len(following),
        )
        assert any(line.startswith("- **") for line in following[:stop]), area


def test_section_lookup_boundaries() -> None:
    sample = "# Changelog\n\n## [9.9.9] - 2026-01-01\n\n- one\n\n## [9.9.8]\n\n- two\n"
    assert _section(sample, "9.9.9") == ["", "- one", ""]
    assert _section(sample, "9.9.8") == ["", "- two"]
    with pytest.raises(LookupError):
        _section(sample, "9.9")
    with pytest.raises(LookupError):
        _section("", "0.7.3")
