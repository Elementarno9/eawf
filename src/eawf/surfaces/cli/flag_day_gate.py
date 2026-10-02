"""The flag-day gate: a plain epoch-1 tree refuses every mutating verb until it migrates.

After the flag day a tree that has not been cut over to epoch 2 is read-only.
The console already says so for such a tree ("every normal command is refused
until this finishes"); this gate makes the CLI say it too, in one place: the
root group resolves the command path the operator typed before any handler
runs, and refuses it when the path mutates state and the tree it addresses is
a plain epoch-1 tree.

Which paths mutate is not restated here. The effect table classifies every
verb, and the flag-day replacement table lists every epoch-1 verb that
writes; the refused set is the writing verbs of the contract groups less
:data:`FLAG_DAY_EXEMPTIONS`, the verbs a tree needs in order to reach the
cutover at all. Each exemption states why, so widening the list is a
reviewable claim rather than a silent hole.
"""

from __future__ import annotations

import logging
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal

import click
from pydantic import BaseModel, ConfigDict, Field

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.error_codes import ErrorCode
from eawf.surfaces.cli.flag_day import EPOCH1_REPLACEMENTS, replacement_guidance, retired_verb
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.help_panels import RegistryOrderedTyperGroup
from eawf.surfaces.cli.verb_contract import CONTRACT_GROUPS
from eawf.surfaces.cli.verb_effects import CLI_VERB_EFFECTS

logger = logging.getLogger(__name__)

#: The ``data.kind`` a mutation refused on a plain epoch-1 tree carries.
MIGRATION_REQUIRED_KIND: Final = "MigrationRequired"

#: The verb that cuts a tree over to epoch 2.
MIGRATION_VERB: Final = "eawf migrate epoch2"

#: The verb that creates a new tree at epoch 2.
INIT_VERB: Final = "eawf init"

#: The help topic that walks an epoch-1 tree through the cutover.
UPGRADE_GUIDE: Final = "eawf help upgrade-from-0.6"

#: The groups whose verbs send the daemon the ``--workspace`` root, or the
#: working directory, as the tree to write -- never the ``EA_STATE`` document.
#: The gate asks about the tree the verb will actually write.
REPO_ROOTED_GROUPS: Final = frozenset({"track", "milestone", "batch", "task", "run", "campaign"})


#: Why a mutating verb is exempt: it is needed to reach the cutover, or it
#: writes only the machine-wide registry and never the tree the gate guards.
ExemptionKind = Literal["migration_support", "registry_only"]


class FlagDayExemption(BaseModel):
    """One mutating verb that keeps working on a plain epoch-1 tree.

    Attributes:
        verb: The command path after ``eawf``.
        kind: Which of the two reasons an exemption may rest on.
        reason: The specific reason, in one line.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verb: str = Field(min_length=1)
    kind: ExemptionKind = "migration_support"
    reason: str = Field(min_length=1)


#: The verbs a plain epoch-1 tree still takes: each one clears something the
#: cutover refuses to run past, is the cutover itself, or writes only the
#: registry the importer resolves the workspace from.
FLAG_DAY_EXEMPTIONS: Final[tuple[FlagDayExemption, ...]] = (
    FlagDayExemption(
        verb="session close",
        reason="the cutover's quiescence check refuses while a session is live",
    ),
    FlagDayExemption(
        verb="session recover",
        reason="a crashed session must be recovered before it can be closed",
    ),
    FlagDayExemption(
        verb="worktree reconcile",
        reason="retires stale worktree rows the quiescence check counts as live",
    ),
    FlagDayExemption(
        verb="worktree cleanup",
        reason="removes finished worktrees the quiescence check counts as live",
    ),
    FlagDayExemption(
        verb="worktree merge-back",
        reason="lands a worktree's commits so the worktree can be retired before the cutover",
    ),
    FlagDayExemption(
        verb="daemon replay-wal",
        reason="drains the fallback write-ahead log, which the quiescence check refuses on",
    ),
    FlagDayExemption(
        verb="daemon stop",
        reason="a running daemon holds a lock lease the quiescence check refuses on",
    ),
    FlagDayExemption(
        verb="workspace add",
        kind="registry_only",
        reason="the importer resolves the workspace in the registry before minting a URN",
    ),
    FlagDayExemption(
        verb="workspace member add",
        kind="registry_only",
        reason="the repository must be a member of the workspace the importer resolves",
    ),
    FlagDayExemption(
        verb="workspace member remove",
        kind="registry_only",
        reason="undoes a membership the importer would otherwise resolve",
    ),
    FlagDayExemption(
        verb="workspace select",
        kind="registry_only",
        reason="picks the session's workspace from the registry and writes nothing",
    ),
    FlagDayExemption(verb="migrate", reason="steps the epoch-1 document to the cutover's schema"),
    FlagDayExemption(verb="migrate epoch2", reason="the cutover itself"),
)


@cache
def exempt_verbs() -> frozenset[str]:
    """Return the command paths :data:`FLAG_DAY_EXEMPTIONS` names."""
    return frozenset(row.verb for row in FLAG_DAY_EXEMPTIONS)


@cache
def mutating_verbs() -> frozenset[str]:
    """Return every command path the effect tables classify as writing state."""
    classified = {verb for verb, eff in CLI_VERB_EFFECTS.items() if eff.effect_class != "read"}
    return frozenset(classified | set(EPOCH1_REPLACEMENTS))


@cache
def refused_verbs() -> frozenset[str]:
    """Return the command paths a plain epoch-1 tree refuses.

    Only a verb of a contract group is refused: a writing verb under a root
    entry the verb contract declares an exception for keeps the behaviour it
    had before the flag day until it is regrouped.
    """
    return frozenset(
        verb
        for verb in mutating_verbs() - exempt_verbs()
        if verb.split(" ", 1)[0] in CONTRACT_GROUPS
    )


def command_path(root: click.Group, ctx: click.Context, args: list[str]) -> str:
    """Return the command path ``args`` resolves to under ``root``, without running it.

    Group options are parsed resiliently, so a group that takes options
    before its subcommand still resolves; an unknown name stops the walk and
    leaves the error to click's own dispatch.

    Args:
        root: The root group.
        ctx: The root context, its own options already parsed.
        args: The tokens left after the root's options.

    Returns:
        The space-joined path after ``eawf``, empty when nothing resolves.
    """
    parts: list[str] = []
    command: click.Command = root
    parent = ctx
    rest = list(args)
    while isinstance(command, click.Group) and rest:
        name = rest[0]
        child = command.get_command(parent, name)
        if child is None:
            break
        parts.append(name)
        rest = rest[1:]
        if isinstance(child, click.Group):
            sub = child.make_context(name, list(rest), parent=parent, resilient_parsing=True)
            rest = [*sub._protected_args, *sub.args]
            parent = sub
        command = child
    return " ".join(parts)


def _addressed_state(verb: str, workspace: Path | None) -> Path:
    """Return the ``state.json`` of the tree ``verb`` will write."""
    if verb.split(" ", 1)[0] in REPO_ROOTED_GROUPS:
        return (workspace or Path.cwd()).resolve() / ".ea" / "state.json"
    from eawf.kernel.state.resolve import resolve_with_reason

    return resolve_with_reason(workspace)[0]


def _tree_epoch(state_path: Path) -> str:
    """Return one clause stating the epoch of the tree holding ``state_path``.

    A retired verb is answered before anything resolves it, so the clause
    says which tree it looked at; an epoch-1 tree is pointed at the upgrade
    guide, because no replacement verb writes there until it migrates.
    """
    if not state_path.is_file():
        return f"no eawf tree is here; create one with `{INIT_VERB}`"
    from eawf.kernel.state.epoch2.authority import resolve_authority

    if resolve_authority(state_path.parent).epoch == 2:
        return "this tree runs on epoch 2"
    return (
        f"this tree still runs on epoch 1, so migrate it first: `{UPGRADE_GUIDE}` walks the upgrade"
    )


def _refusal(verb: str, ea_dir: Path, *, tree_exists: bool) -> cli_errors.CliError:
    """Build the typed refusal for ``verb`` against the tree at ``ea_dir``."""
    if tree_exists:
        guidance = (
            f"{ea_dir.name} is an epoch-1 tree, so `eawf {verb}` was refused and nothing was "
            f"written; migrate it with `{MIGRATION_VERB} --plan` then `--apply`"
        )
    else:
        guidance = (
            f"`eawf {verb}` writes epoch-1 state, which no tree takes after the flag day; "
            f"create an epoch-2 tree with `{INIT_VERB}`"
        )
    return cli_errors.MigrationRequired(guidance, kind=MIGRATION_REQUIRED_KIND)


def enforce(root: click.Group, ctx: click.Context, args: list[str]) -> None:
    """Refuse a mutating verb aimed at a plain epoch-1 tree, else return.

    The tree is the one the verb writes: the ``--workspace`` root or the
    working directory for a :data:`REPO_ROOTED_GROUPS` verb, the resolved
    ``state.json`` for every other. A verb is refused on an existing
    ``state.json`` whose tree carries no
    epoch marker: a plain epoch-1 tree. A tree that carries the marker is
    epoch 2, or a cutover caught half-done, and the ``state.json`` write
    chokepoint already refuses epoch-1 writes there with the replacement
    verb. An epoch-1 verb is also refused where no tree exists, since the
    only tree it could create is an epoch-1 one; any other verb passes
    there, because it has no epoch-1 tree to write.

    Args:
        root: The root group.
        ctx: The root context, its own options already parsed.
        args: The tokens left after the root's options.

    Raises:
        typer.Exit: With the migration-required exit code, after the
            error envelope is emitted.
    """
    params: dict[str, Any] = ctx.params
    flags = GlobalFlags(
        json_output=bool(params.get("json_output")),
        plain_output=bool(params.get("plain_output")),
    )
    workspace = params.get("workspace")
    if (retired := retired_verb(args)) is not None:
        tree = _tree_epoch(_addressed_state(retired, Path(workspace) if workspace else None))
        cli_errors.emit_error(
            cli_errors.ValidationError(
                f"`eawf {retired}` retired at the flag day; {replacement_guidance(retired)}; "
                f"{tree}",
                kind=cli_errors.LEGACY_OPERATION_REMOVED_KIND,
            ),
            flags=flags,
        )
    if "--help" in args:
        return
    verb = command_path(root, ctx, args)
    if verb not in refused_verbs():
        return
    from eawf.kernel.state.io import epoch_marker_present

    state_path = _addressed_state(verb, Path(workspace) if workspace else None)
    tree_exists = state_path.is_file()
    if not tree_exists and verb not in EPOCH1_REPLACEMENTS:
        return
    if tree_exists and epoch_marker_present(state_path.parent):
        return
    logger.info(f"flag_day_gate refused verb={verb!r} tree_exists={tree_exists}")
    cli_errors.emit_error(
        _refusal(verb, state_path.parent, tree_exists=tree_exists),
        flags=flags,
        error_code=ErrorCode.MIGRATION_REQUIRED,
    )


class FlagDayTyperGroup(RegistryOrderedTyperGroup):
    """The root group: runs :func:`enforce` before any subcommand resolves."""

    def invoke(self, ctx: click.Context) -> Any:
        """Refuse a flag-day mutation, then dispatch as usual."""
        enforce(self, ctx, [*ctx._protected_args, *ctx.args])
        return super().invoke(ctx)


__all__ = [
    "FLAG_DAY_EXEMPTIONS",
    "INIT_VERB",
    "MIGRATION_REQUIRED_KIND",
    "MIGRATION_VERB",
    "REPO_ROOTED_GROUPS",
    "UPGRADE_GUIDE",
    "FlagDayExemption",
    "FlagDayTyperGroup",
    "command_path",
    "enforce",
    "exempt_verbs",
    "mutating_verbs",
    "refused_verbs",
]
