"""REL-021: every epoch-1 verb has a flag-day disposition, read from the live CLI tree.

After the flag day the only epoch-1 verbs left on the tree are the
migration-support verbs a tree needs to reach the cutover, each naming its
epoch-2 replacement in :data:`EPOCH1_REPLACEMENTS`. Every other epoch-1 verb
was removed and sits in :data:`RETIRED_VERBS`, where the root group answers it
with the verb that replaces it or with ``eawf migrate epoch2 --plan``. The
census walks the mounted command tree rather than a hand-kept list, so a verb
added to an epoch-1 command module without a disposition fails here.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Final

import click
import pytest
import typer
from pydantic import ValidationError

from eawf.surfaces.cli import flag_day_gate
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.flag_day import (
    EPOCH1_REPLACEMENTS,
    RETIRED_VERBS,
    replacement_guidance,
    retired_verb,
)
from eawf.surfaces.cli.verb_contract import CONTRACT_GROUPS
from eawf.surfaces.cli.verb_effects import CLI_VERB_EFFECTS

pytestmark = pytest.mark.contract

_COMMANDS: Final = "eawf.surfaces.cli.commands."

#: The handler modules that still carry epoch-1 verbs.
EPOCH1_MODULES: Final = frozenset(
    f"{_COMMANDS}{name}" for name in ("research", "session", "worktree")
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
    dispositions = (set(EPOCH1_REPLACEMENTS), EPOCH1_CARRIED)
    unclassified = sorted(p for p in epoch1 if not any(p in d for d in dispositions))
    assert unclassified == [], "epoch-1 verbs with no flag-day disposition"
    assert set(EPOCH1_REPLACEMENTS) <= flag_day_gate.exempt_verbs()


def test_rel_021_every_live_row_names_a_live_epoch1_verb(
    leaves: dict[str, click.Command],
) -> None:
    rows = set(EPOCH1_REPLACEMENTS) | EPOCH1_CARRIED
    assert sorted(rows - set(leaves)) == []
    assert sorted(p for p in rows if _handler_module(leaves[p]) not in EPOCH1_MODULES) == []


def test_rel_021_no_retired_verb_is_on_the_tree(leaves: dict[str, click.Command]) -> None:
    assert sorted(set(RETIRED_VERBS) & set(leaves)) == []
    assert sorted(set(RETIRED_VERBS) & set(EPOCH1_REPLACEMENTS)) == []
    assert sorted(set(RETIRED_VERBS) & set(CLI_VERB_EFFECTS)) == []


def test_rel_021_every_replacement_is_a_live_verb(leaves: dict[str, click.Command]) -> None:
    replacements = {
        r for table in (EPOCH1_REPLACEMENTS, RETIRED_VERBS) for r in table.values() if r is not None
    }
    assert sorted(replacements - set(leaves)) == []
    assert sorted(r for r in replacements if _handler_module(leaves[r]) in EPOCH1_MODULES) == []


def test_rel_021_guidance_names_the_replacement_verb() -> None:
    assert replacement_guidance("wave claim") == (
        "run `eawf task claim` instead, the verb that replaces `eawf wave claim`"
    )


def test_rel_021_guidance_points_a_verb_with_no_replacement_at_the_migration_plan() -> None:
    assert RETIRED_VERBS["wave release"] is None
    assert replacement_guidance("wave release") == (
        "no epoch-2 verb replaces `eawf wave release`; inspect or migrate an epoch-1 tree "
        "with `eawf migrate epoch2 --plan`"
    )


@pytest.mark.parametrize("path", ["", "wave", "wave claim extra", "memory add"])
def test_rel_021_guidance_for_a_path_outside_the_census_points_at_the_native_nouns(
    path: str,
) -> None:
    assert replacement_guidance(path) == (
        "use the epoch-2 verbs instead (eawf milestone|batch|task|run --help)"
    )


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("wave", "budget", "show", "P01"), "wave budget show"),
        (("wave", "claim", "P01-I01-W01"), "wave claim"),
        (("state", "show"), "state show"),
        (("task", "claim"), None),
        ((), None),
    ],
)
def test_rel_021_retired_verb_matches_the_longest_retired_path(
    args: tuple[str, ...], expected: str | None
) -> None:
    assert retired_verb(args) == expected


# ---- REL-021: the plain-epoch-1-tree gate ------------------------------------


def test_rel_021_every_leaf_is_classified_for_the_epoch1_gate(
    leaves: dict[str, click.Command],
) -> None:
    """A new verb must be classified before it ships; no verb is left unclassified."""
    classified = set(CLI_VERB_EFFECTS) | set(EPOCH1_REPLACEMENTS)
    assert sorted(set(leaves) - classified) == [], "leaves the epoch-1 gate cannot classify"
    assert not hasattr(flag_day_gate, "UNCLASSIFIED_VERBS")


def test_rel_021_every_mutating_verb_is_refused_exempt_or_outside_the_contract() -> None:
    mutating = {v for v, eff in CLI_VERB_EFFECTS.items() if eff.effect_class != "read"}
    mutating |= set(EPOCH1_REPLACEMENTS)
    assert mutating == set(flag_day_gate.mutating_verbs())
    outside = {v for v in mutating if v.split(" ", 1)[0] not in CONTRACT_GROUPS}
    neither = mutating - flag_day_gate.refused_verbs() - flag_day_gate.exempt_verbs() - outside
    assert sorted(neither) == []
    assert flag_day_gate.refused_verbs() & flag_day_gate.exempt_verbs() == frozenset()
    assert sorted(flag_day_gate.refused_verbs() & outside) == []


@pytest.mark.parametrize("verb", ["plan apply", "hook run", "repo add"])
def test_rel_021_a_writing_verb_outside_the_contract_groups_is_not_refused(verb: str) -> None:
    """Boundary: classified as writing, yet under a declared root exception."""
    assert verb in flag_day_gate.mutating_verbs()
    assert verb not in flag_day_gate.refused_verbs()


def test_rel_021_every_exemption_is_a_live_path_with_a_reason() -> None:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    ctx = click.Context(root)
    for row in flag_day_gate.FLAG_DAY_EXEMPTIONS:
        assert row.reason.strip(), row.verb
        assert flag_day_gate.command_path(root, ctx, row.verb.split()) == row.verb
    verbs = [row.verb for row in flag_day_gate.FLAG_DAY_EXEMPTIONS]
    assert len(verbs) == len(set(verbs))


def test_rel_021_exemptions_are_the_migration_support_and_registry_verbs() -> None:
    assert flag_day_gate.exempt_verbs() == {
        "daemon replay-wal",
        "daemon stop",
        "migrate",
        "migrate epoch2",
        "session close",
        "session recover",
        "workspace add",
        "workspace member add",
        "workspace member remove",
        "workspace select",
        "worktree cleanup",
        "worktree merge-back",
        "worktree reconcile",
    }


def test_rel_021_only_workspace_registry_verbs_are_registry_only() -> None:
    registry_only = {
        row.verb for row in flag_day_gate.FLAG_DAY_EXEMPTIONS if row.kind == "registry_only"
    }
    assert registry_only == {
        "workspace add",
        "workspace member add",
        "workspace member remove",
        "workspace select",
    }


def test_rel_021_the_exemption_row_is_a_closed_model() -> None:
    with pytest.raises(ValidationError):
        flag_day_gate.FlagDayExemption.model_validate({"verb": "x", "reason": "y", "extra": 1})
    with pytest.raises(ValidationError):
        flag_day_gate.FlagDayExemption(verb="x", reason="")
    with pytest.raises(ValidationError):
        flag_day_gate.FlagDayExemption.model_validate({"verb": "x", "kind": "any", "reason": "y"})


def test_auth_049_the_workspace_pointer_verbs_are_gone(leaves: dict[str, click.Command]) -> None:
    retired = {
        "repo link",
        "repo link-workspace",
        "workspace add-repo",
        "workspace init",
        "workspace remove-repo",
        "workspace status",
        "workspace validate",
    }
    assert sorted(retired & set(leaves)) == []
    assert sorted(retired & set(CLI_VERB_EFFECTS)) == []
