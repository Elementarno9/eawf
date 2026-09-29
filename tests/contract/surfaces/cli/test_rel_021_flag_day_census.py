"""REL-021: every epoch-1 verb has a flag-day disposition, read from the live CLI tree.

The census walks the mounted command tree rather than a hand-kept list, so a
verb added to an epoch-1 command module without a disposition fails here: it
either names the epoch-2 verb that replaces it (or that it retired), or it is
a read that keeps working after the flag day.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Final

import click
import pytest
import typer

from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.flag_day import EPOCH1_REPLACEMENTS, replacement_guidance

pytestmark = pytest.mark.contract

_COMMANDS: Final = "eawf.surfaces.cli.commands."

#: The handler modules whose verbs act on epoch-1 state.
EPOCH1_MODULES: Final = frozenset(
    f"{_COMMANDS}{name}"
    for name in (
        "agent_report",
        "backfill",
        "close",
        "dispatch",
        "draft",
        "estimation",
        "evidence",
        "evidence_artifact",
        "evidence_backlog",
        "evidence_hypothesis",
        "evidence_incident",
        "flow",
        "lifecycle_iter",
        "lifecycle_phase",
        "lifecycle_wave",
        "lifecycle_wave_prune",
        "lifecycle_wave_read",
        "pr_review",
        "research",
        "roadmap",
        "session",
        "spec",
        "state",
        "wave_ci",
        "wave_policy",
        "worktree",
    )
)

#: Epoch-1 verbs that write nothing epoch-1, so they keep working after the flag day.
EPOCH1_READS: Final = frozenset(
    {
        "agent-report list",
        "agent-report show",
        "artifact show",
        "artifact validate",
        "artifact verify",
        "audit list",
        "audit show",
        "close follow",
        "close status",
        "decision graph",
        "decision list",
        "draft new",
        "draft validate",
        "flow status",
        "hypothesis list",
        "incident view",
        "operator rollup",
        "phase retro",
        "question list",
        "research show",
        "research status",
        "roadmap show",
        "spec show",
        "spec validate",
        "state resolve",
        "state show",
        "wave archive-refs",
        "wave budget show",
        "wave graph",
        "wave integration show",
        "wave next-ready",
        "wave policy show",
        "wave prune-branches",
        "wave show",
        "wave waivers",
        "worktree list",
    }
)

#: Research campaign verbs write their own append-only store, which the
#: epoch-2 surface carries forward rather than retiring.
EPOCH1_CARRIED: Final = frozenset({"campaign cancel", "campaign new", "campaign run"})


def _leaves() -> Iterator[tuple[str, click.Command]]:
    """Yield every mounted leaf command as ``(path after eawf, command)``."""
    root = typer.main.get_command(app)

    def walk(command: click.Command, path: tuple[str, ...]) -> Iterator[tuple[str, click.Command]]:
        if isinstance(command, click.Group):
            for name, child in sorted(command.commands.items()):
                yield from walk(child, (*path, name))
        else:
            yield " ".join(path), command

    yield from walk(root, ())


def _handler_module(command: click.Command) -> str:
    assert command.callback is not None
    return inspect.unwrap(command.callback).__module__


@pytest.fixture(scope="module")
def leaves() -> dict[str, click.Command]:
    return dict(_leaves())


def test_rel_021_every_epoch1_verb_has_exactly_one_disposition(
    leaves: dict[str, click.Command],
) -> None:
    epoch1 = {path for path, cmd in leaves.items() if _handler_module(cmd) in EPOCH1_MODULES}
    dispositions = (set(EPOCH1_REPLACEMENTS), EPOCH1_READS, EPOCH1_CARRIED)
    unclassified = sorted(p for p in epoch1 if not any(p in d for d in dispositions))
    doubled = sorted(p for p in epoch1 if sum(p in d for d in dispositions) > 1)
    assert unclassified == [], "epoch-1 verbs with no flag-day disposition"
    assert doubled == []


def test_rel_021_every_census_row_names_a_live_epoch1_verb(
    leaves: dict[str, click.Command],
) -> None:
    rows = set(EPOCH1_REPLACEMENTS) | EPOCH1_READS | EPOCH1_CARRIED
    assert sorted(rows - set(leaves)) == []
    assert sorted(p for p in rows if _handler_module(leaves[p]) not in EPOCH1_MODULES) == []


def test_rel_021_every_replacement_is_a_live_epoch2_verb(
    leaves: dict[str, click.Command],
) -> None:
    replacements = {r for r in EPOCH1_REPLACEMENTS.values() if r is not None}
    assert sorted(replacements - set(leaves)) == []
    assert sorted(r for r in replacements if _handler_module(leaves[r]) in EPOCH1_MODULES) == []


def test_rel_021_guidance_names_the_replacement_verb() -> None:
    assert replacement_guidance("wave claim") == (
        "run `eawf task claim` instead, the epoch-2 verb that replaces `eawf wave claim`"
    )


def test_rel_021_guidance_says_a_retired_verb_has_no_replacement() -> None:
    assert EPOCH1_REPLACEMENTS["wave release"] is None
    assert replacement_guidance("wave release") == (
        "`eawf wave release` retired at the flag day and no epoch-2 verb replaces it"
    )


@pytest.mark.parametrize("path", ["", "wave", "wave claim extra", "memory add"])
def test_rel_021_guidance_for_a_path_outside_the_census_points_at_the_native_nouns(
    path: str,
) -> None:
    assert replacement_guidance(path) == (
        "use the epoch-2 verbs instead (eawf milestone|batch|task|run --help)"
    )
