"""Tests for the EAWF016 changelog bullet lint."""

from __future__ import annotations

from pathlib import Path

from eawf.platform.lint.tools.eawf016_laconic_bullet import check_source, main


def test_eawf016_accepts_specific_laconic_unreleased_bullets() -> None:
    source = (
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- Render release changelog from phase narratives.\n"
        "- `eawf016` rejects placeholder release bullets.\n"
    )

    assert check_source(source) == []


def test_eawf016_rejects_generic_unreleased_bullet() -> None:
    source = "# Changelog\n\n## [Unreleased]\n\n- Misc updates.\n"

    violations = check_source(source)

    assert len(violations) == 1
    assert violations[0].code == "EAWF016"
    assert "generic" in violations[0].reason


def test_eawf016_rejects_multiline_unreleased_bullet() -> None:
    source = (
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- Render release changelog from phase narratives\n"
        "  with extra implementation details that belong elsewhere.\n"
    )

    violations = check_source(source)

    assert len(violations) == 1
    assert "multiple physical lines" in violations[0].reason


def test_eawf016_ignores_prior_release_sections() -> None:
    source = "# Changelog\n\n## [0.2.0]\n\n- Misc updates.\n"

    assert check_source(source) == []


def test_eawf016_cli_returns_one_on_violation(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\n- Various fixes.\n", encoding="utf-8")

    assert main([str(changelog)]) == 1
