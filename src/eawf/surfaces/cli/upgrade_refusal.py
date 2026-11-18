"""The refusal a 0.6 tree gets: this release does not write it, and says how to upgrade.

A 0.6 tree keeps its whole state in ``.ea/state.json`` and carries no epoch
marker. This release reads and writes only the generation store, and the
cutover that builds one from a 0.6 tree shipped last in 0.7.0rc4. The root
group resolves the command path before any handler runs and refuses, on a
0.6 tree, every verb of a contract group the effect table classifies as
writing, and the console, whose views read only a generation. A read verb
still answers there and reports the tree as epoch 1.

A tree in the eawf 0.8 layout is refused outright, read verbs included, with
the line that names the newer client; only help, the version, the hooks, the
status lines and the read-only doctor still answer there; its repair is refused.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, NoReturn

import click

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.error_codes import ErrorCode
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.help_panels import RegistryOrderedTyperGroup
from eawf.surfaces.cli.verb_contract import CONTRACT_GROUPS
from eawf.surfaces.cli.verb_effects import CLI_VERB_EFFECTS

logger = logging.getLogger(__name__)

#: The ``data.kind`` every refusal of a tree that has to migrate first carries.
MIGRATION_REQUIRED_KIND: Final = "MigrationRequired"

#: The upgrade path for a 0.6 tree, stated the same way the upgrade guide states it.
UPGRADE_STEPS: Final = (
    "install eawf 0.7.0rc4, run its migrate epoch2 verb with --plan and then --apply, "
    "then upgrade to this release"
)

#: The command path that opens the console; a bare ``eawf`` opens it too.
CONSOLE_VERB: Final = "ui"

#: The groups whose verbs send the daemon the ``--workspace`` root, or the
#: working directory, as the tree to write -- never the ``EA_STATE`` document.
REPO_ROOTED_GROUPS: Final = frozenset({"track", "milestone", "batch", "task", "run", "campaign"})


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


#: Verbs that only stop a local process and touch no record: a user upgrading
#: a 0.6 tree must still stop the daemon an older eawf left running.
_STOP_VERBS: Final = frozenset({"daemon stop", "daemon restart"})


def refuses(verb: str, *, bare: bool) -> bool:
    """Return whether a 0.6 tree refuses the command path ``verb``.

    Args:
        verb: The space-joined path after ``eawf``.
        bare: Whether ``eawf`` was invoked with no arguments, which opens
            the console.

    Returns:
        ``True`` for the console and for every verb of a contract group the
        effect table classifies as writing. A writing verb under a root entry
        outside the contract groups, such as ``doctor`` or ``backup``,
        answers for the local install rather than the tree's records, and so
        do the daemon stop verbs, which a user still needs on a 0.6 tree.
    """
    if bare or verb == CONSOLE_VERB:
        return True
    effect = CLI_VERB_EFFECTS.get(verb)
    return (
        effect is not None
        and effect.effect_class != "read"
        and verb.split(" ", 1)[0] in CONTRACT_GROUPS
        and verb not in _STOP_VERBS
    )


#: Command paths a tree in the eawf 0.8 layout still answers: help and the
#: version, the hooks a host fires on every event, the status lines it polls,
#: and doctor, which names the layout as its finding. A path matches an entry
#: equal to it or nested under it.
_V08_EXEMPT_VERBS: Final = ("help", "version", "hook", "cc statusline", "statusline", "doctor")

#: The doctor flag that rewrites config and rendered files; the exemption
#: covers only the read-only doctor, so a 0.8 tree refuses the repair.
_DOCTOR_REPAIR_FLAG: Final = "--fix"

#: The next step a refused 0.8 tree is given in place of the 0.6 upgrade path.
_V08_NEXT_STEP: Final = "install eawf 0.8 or later, then run the command again"


def v08_exempt(verb: str) -> bool:
    """Return whether the command path ``verb`` still answers on a 0.8-layout tree.

    Args:
        verb: The space-joined path after ``eawf``; empty for a bare ``eawf``.

    Returns:
        ``True`` when ``verb`` is, or is nested under, an exempt path.
    """
    return any(verb == entry or verb.startswith(f"{entry} ") for entry in _V08_EXEMPT_VERBS)


def addressed_document(
    verb: str, workspace: Path | None, environ: Mapping[str, str]
) -> Path | None:
    """Return the ``state.json`` ``verb`` would address, or ``None`` when no tree is found.

    A verb of :data:`REPO_ROOTED_GROUPS` addresses the ``--workspace`` root or
    the working directory. Every other verb follows the state resolver's
    precedence: ``EA_STATE``, then the ``--workspace`` root, then the first
    ``.ea`` above the working directory that holds ``state.json``, the epoch
    marker or the eawf 0.8 layout. Only file existence is asked, so the check
    costs one walk of ``stat`` calls.

    Args:
        verb: The space-joined command path after ``eawf``.
        workspace: The ``--workspace`` root, when given.
        environ: The process environment.

    Returns:
        The document path, which need not exist, or ``None`` when the walk
        finds no tree.
    """
    from eawf.kernel.migration.epoch2.canary import uses_v08_layout
    from eawf.kernel.state.io import epoch_marker_present

    if verb.split(" ", 1)[0] in REPO_ROOTED_GROUPS:
        return (workspace or Path.cwd()).resolve() / ".ea" / "state.json"
    if raw := environ.get("EA_STATE"):
        return Path(raw)
    if workspace is not None:
        return workspace / ".ea" / "state.json"
    cwd = Path.cwd().resolve()
    for directory in (cwd, *cwd.parents):
        ea_dir = directory / ".ea"
        if (
            (ea_dir / "state.json").exists()
            or epoch_marker_present(ea_dir)
            or uses_v08_layout(ea_dir)
        ):
            return ea_dir / "state.json"
    return None


def is_unmigrated_tree(state_path: Path) -> bool:
    """Return whether ``state_path`` is the document of a 0.6 tree.

    Args:
        state_path: The ``.ea/state.json`` path a verb resolves to.

    Returns:
        ``True`` when the document exists and its tree carries no epoch
        marker; a tree with the marker is read from its generation.
    """
    from eawf.kernel.state.io import epoch_marker_present

    return state_path.is_file() and not epoch_marker_present(state_path.parent)


def _refuse_v08_tree(verb: str, flags: GlobalFlags) -> NoReturn:
    """Emit the 0.8-layout refusal and exit with the migration-required code.

    The envelope keeps the 0.6 refusal's kind, error code and exit code, but
    its hint names the newer client: the bucket's default hint describes the
    0.6 upgrade path, which would send a 0.8 user backwards.

    Args:
        verb: The refused command path, for the log line.
        flags: The output flags the envelope renders under.

    Raises:
        typer.Exit: Always, with the migration-required exit code.
    """
    import typer

    from eawf.kernel.migration.epoch2.canary import V08_LAYOUT_MESSAGE
    from eawf.surfaces.cli.output import emit_json_or_text

    logger.info(f"upgrade_refusal refused verb={verb!r} layout=v08")
    err = cli_errors.MigrationRequired(V08_LAYOUT_MESSAGE, kind=MIGRATION_REQUIRED_KIND)
    env = cli_errors.build_envelope(err, error_code=ErrorCode.MIGRATION_REQUIRED).model_copy(
        update={"suggested_next_step": _V08_NEXT_STEP}
    )
    emit_json_or_text(
        env.model_dump(mode="json", exclude_none=False), cli_errors._render_text(env), flags=flags
    )
    raise typer.Exit(err.exit_code)


def enforce(root: click.Group, ctx: click.Context, args: list[str]) -> None:
    """Refuse a verb a tree cannot serve, else return.

    A tree in the eawf 0.8 layout refuses every verb outside the exempt
    paths, read verbs included: this release cannot read it at all. The
    doctor repair is refused there too, though the read-only doctor answers.
    A 0.6
    tree refuses the console and every writing verb.

    Args:
        root: The root group.
        ctx: The root context, its own options already parsed.
        args: The tokens left after the root's options.

    Raises:
        typer.Exit: With the migration-required exit code, after the
            error envelope is emitted.
    """
    from eawf.kernel.migration.epoch2.canary import uses_v08_layout

    if "--help" in args:
        return
    verb = command_path(root, ctx, args)
    exempt = v08_exempt(verb) and not (verb == "doctor" and _DOCTOR_REPAIR_FLAG in args)
    refused_on_06 = refuses(verb, bare=not args)
    if exempt and not refused_on_06:
        return
    params: dict[str, Any] = ctx.params
    workspace = params.get("workspace")
    state_path = addressed_document(verb, Path(workspace) if workspace else None, os.environ)
    if state_path is None:
        return
    flags = GlobalFlags(
        json_output=bool(params.get("json_output")),
        plain_output=bool(params.get("plain_output")),
    )
    if not exempt and uses_v08_layout(state_path.parent):
        _refuse_v08_tree(verb or CONSOLE_VERB, flags)
    if not refused_on_06 or not is_unmigrated_tree(state_path):
        return
    shown = verb or CONSOLE_VERB
    logger.info(f"upgrade_refusal refused verb={shown!r} tree={state_path.parent.name}")
    cli_errors.emit_error(
        cli_errors.MigrationRequired(
            f"{state_path.parent.name} is a 0.6 tree, which this release does not write, so "
            f"`eawf {shown}` was refused and nothing was written; {UPGRADE_STEPS}",
            kind=MIGRATION_REQUIRED_KIND,
        ),
        flags=flags,
        error_code=ErrorCode.MIGRATION_REQUIRED,
    )


class UpgradeRefusalTyperGroup(RegistryOrderedTyperGroup):
    """The root group: runs :func:`enforce` before any subcommand resolves."""

    def invoke(self, ctx: click.Context) -> Any:
        """Refuse a verb the addressed tree cannot serve, then dispatch as usual."""
        enforce(self, ctx, [*ctx._protected_args, *ctx.args])
        return super().invoke(ctx)


__all__ = [
    "CONSOLE_VERB",
    "MIGRATION_REQUIRED_KIND",
    "REPO_ROOTED_GROUPS",
    "UPGRADE_STEPS",
    "UpgradeRefusalTyperGroup",
    "addressed_document",
    "command_path",
    "enforce",
    "is_unmigrated_tree",
    "refuses",
    "v08_exempt",
]
