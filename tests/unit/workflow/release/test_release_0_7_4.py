"""The 0.7.4 cut: the package version and the changelog section naming its five areas."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import eawf
from eawf._version import __version__
from eawf.surfaces.cli._version_display import compose_display_version

_CHANGELOG = Path(__file__).resolve().parents[4] / "CHANGELOG.md"
_LOCK = Path(__file__).resolve().parents[4] / "uv.lock"

#: The first opentelemetry-api release without the advisory that blocked the 0.7.4 release.
_OTEL_API_FIXED = (1, 45, 2)

#: The five areas of the 0.7.4 patch, each a third-level heading of its section.
_AREAS = (
    "0.8 layout",
    "History rewrite",
    "Commit regime",
    "Package metadata",
    "Release",
)

#: What the upgrade notes must tell a user before the areas begin.
_UPGRADE_PHRASES = (
    "`eawf: this repository uses the eawf 0.8 layout; install eawf 0.8 or later`",
    "once at session start",
    "Every verb except hook, status line, doctor, help and version is refused",
    "`eawf batch rebind-rewritten --map-file <git filter-repo commit map> [--dry-run]`",
    "`.ea/store/rewrite_map.jsonl`",
    "run `eawf sync` first",
    "Do not run `eawf doctor --fix` or a pruning `git gc` before that",
    "already unreachable before the rewrite stay reported",
    "one `state: seal` commit when the pull request opens",
    "Review fixes are code-only",
    "The author email is removed from the package metadata",
    "the leak scanners no longer allow that address anywhere",
    "rows already committed do not block a commit",
    "reports the address in old lines too",
    "No state migration is required from 0.7.3",
    "replaced by one for 0.7.4",
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
    assert __version__ == "0.7.4"
    assert eawf.__version__ == "0.7.4"
    assert compose_display_version().split("+")[0] == "0.7.4"

    text = _CHANGELOG.read_text(encoding="utf-8")
    first = next(line for line in text.splitlines() if line.startswith("## "))
    assert first == "## [0.7.4] - 2026-11-17"
    section = _section(text, "0.7.4")
    headings = [line.removeprefix("### ") for line in section if line.startswith("### ")]
    assert headings == ["Upgrade notes", *_AREAS]
    body = "\n".join(section)
    # The release probe needs one line naming the migration outcome.
    assert "No state migration is required from 0.7.3" in body
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
    release = "\n".join(section[section.index("### Release") :])
    assert "same targets and claims as 0.7.3" in release
    # The section must not spell out an address the release stopped publishing.
    assert "@" not in body


def test_section_lookup_boundaries() -> None:
    sample = "# Changelog\n\n## [9.9.9] - 2026-01-01\n\n- one\n\n## [9.9.8]\n\n- two\n"
    assert _section(sample, "9.9.9") == ["", "- one", ""]
    assert _section(sample, "9.9.8") == ["", "- two"]
    with pytest.raises(LookupError):
        _section(sample, "9.9")
    with pytest.raises(LookupError):
        _section("", "0.7.4")


def test_advisory_fixed_lock_pins_opentelemetry_api_at_or_past_the_fix() -> None:
    packages = tomllib.loads(_LOCK.read_text(encoding="utf-8"))["package"]
    versions = [row["version"] for row in packages if row["name"] == "opentelemetry-api"]

    assert len(versions) == 1
    assert tuple(int(part) for part in versions[0].split(".")[:3]) >= _OTEL_API_FIXED
