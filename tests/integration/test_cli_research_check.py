"""``eawf research check`` judges one local research brief.

It exits 0 on a brief holding every section with a sound References table,
refuses one missing References with an error naming the section, and refuses a
path it cannot read as a user error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.exit_codes import OK, USER_ERROR, VALIDATION_ERROR

runner = CliRunner()

_BRIEF = """# Which store holds the tiers

## Question

Which module owns the store tiers [1]?

## Findings

The tiers module names every collection [1], as the guide says [2].

## Alternatives

A registry per collection; more files, no gain.

## Recommendation

Keep the tiers module.

## Open gaps

None.

## References

| # | Reference |
| --- | --- |
| 1 | `src/eawf/kernel/store/tiers.py` |
| 2 | https://example.org/guide |
"""


def _check(tmp_path: Path, text: str) -> Any:
    brief = tmp_path / "brief.md"
    brief.write_text(text, encoding="utf-8")
    return runner.invoke(app, ["research", "check", str(brief)])


def test_research_check_passes_a_brief_holding_every_section(tmp_path: Path) -> None:
    result = _check(tmp_path, _BRIEF)
    assert result.exit_code == OK, result.output
    assert "passes" in result.output


def test_research_check_refuses_a_brief_missing_references(tmp_path: Path) -> None:
    text = _BRIEF[: _BRIEF.index("## References")].replace(" [1]", "").replace(" [2]", "")
    result = _check(tmp_path, text)
    assert result.exit_code == VALIDATION_ERROR
    assert "missing section: References" in result.output
    assert "run `eawf research check <path>` again" in result.output
    assert "eawf validate" not in result.output


def test_research_check_refuses_an_unreadable_brief(tmp_path: Path) -> None:
    result = runner.invoke(app, ["research", "check", str(tmp_path / "absent.md")])
    assert result.exit_code == USER_ERROR
    assert "cannot be read" in result.output
