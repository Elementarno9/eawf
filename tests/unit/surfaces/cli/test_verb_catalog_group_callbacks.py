"""A group that runs its own callback is a verb the catalog lists.

``eawf doctor --fix`` and ``eawf migrate --to`` run without naming a subcommand, so a
catalog that lists only leaves hides them from ``eawf verbs`` and from the skill join.
"""

from __future__ import annotations

import click
import pytest
import typer

from eawf.runtime.daemon.methods import registered_methods
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.verb_catalog import VerbCatalogError, build_verb_catalog


def _root() -> click.Group:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    return root


@pytest.mark.parametrize(
    ("verb", "effect_class"),
    [
        ("doctor", "mutate"),
        ("migrate", "mutate"),
        ("migrate epoch2", "mutate"),
        ("cc statusline", "read"),
        ("cc statusline install", "mutate"),
        ("help", "read"),
    ],
)
def test_a_group_with_a_callback_is_catalogued(verb: str, effect_class: str) -> None:
    catalog = build_verb_catalog(_root(), registered_methods())
    entries = {entry.verb: entry for entry in catalog.entries}
    assert entries[verb].effect_class == effect_class


def test_a_callback_group_with_no_declared_effect_is_refused() -> None:
    @click.group("track", invoke_without_command=True)
    def track() -> None:
        """A callback group the effect table does not classify."""

    root = click.Group("eawf", commands={"track": track})
    with pytest.raises(VerbCatalogError, match="'track' declares no effect class"):
        build_verb_catalog(root, ())
