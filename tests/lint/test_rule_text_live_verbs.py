"""Shipped rule and profile text names no command group the flag day retired.

An agent follows a rule literally, so a rule that routes mutations through a retired
group sends it to an unknown-command error at the moment it tries to comply.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import typer

from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.flag_day import RETIRED_VERBS

_DATA = Path(__file__).resolve().parents[2] / "src" / "eawf" / "platform"
_SOURCES = (
    *sorted((_DATA / "rules" / "data").rglob("*.yaml")),
    _DATA / "profiles" / "data" / "core.yaml",
)
_MENTION = re.compile(r"\beawf ([a-z][a-z0-9-]*)\b")


def _retired_groups() -> frozenset[str]:
    """Return the root words of retired verbs that no live root command carries."""
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    live = set(root.list_commands(click.Context(root)))
    return frozenset(path.split()[0] for path in RETIRED_VERBS) - live


def test_the_retired_state_group_is_detected() -> None:
    assert "state" in _retired_groups()
    assert "task" not in _retired_groups()


def test_rule_and_profile_text_names_no_retired_group() -> None:
    retired = _retired_groups()
    hits = [
        f"{source.relative_to(_DATA)}:{number}: eawf {word}"
        for source in _SOURCES
        for number, line in enumerate(source.read_text().splitlines(), start=1)
        for word in _MENTION.findall(line)
        if word in retired
    ]
    assert hits == []


def test_the_commit_rule_commits_task_definitions_and_keeps_live_status_local() -> None:
    text = (_DATA / "rules" / "data" / "modules" / "eawf.core.state.yaml").read_text()
    assert "ledgers and Task definitions" in text
    assert "keep in-flight Task status and Run rows (each generation's local/)" in text
