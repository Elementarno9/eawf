"""Declarative command registry for the ``eawf`` root Typer app.

:mod:`eawf.surfaces.cli.app` builds the root :class:`typer.Typer` and wires its
inline-defined surfaces (root callback, ``version`` / ``scope-debug`` /
``ui`` commands, logging, ``main``). Everything else — the ~50 sub-Typer
groups and the handful of direct commands that hang off the root — is
declared here as data and mounted by :func:`register_commands`.

Three row kinds drive the mount:

* :class:`GroupRow` — a sub-Typer group mounted via ``app.add_typer``.
* :class:`CommandRow` — a direct command registered via ``app.command``.
* :class:`SideEffectRow` — a module imported purely for its import-time
  side effect (it attaches a verb onto an already-mounted group's Typer).

Each row names its source module by dotted path. The root group,
:class:`LazyCommandGroup`, imports a row's module only when something asks
for that command: running it, drawing its help, or walking the whole tree.
An ``eawf`` that runs one command, or none, imports one command module, or
none, rather than all of them.

Every group / command is mounted with ``rich_help_panel=panel_for(name)``,
exactly as the hand-written wall did, and rows are walked in declaration
order so the registration sequence is preserved.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import click
import typer

from eawf.surfaces.cli.help_panels import panel_for
from eawf.surfaces.cli.upgrade_refusal import UpgradeRefusalTyperGroup

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GroupRow:
    """A sub-Typer group mounted on the root app via ``add_typer``.

    Attributes:
        name: Top-level command name (``eawf <name> ...``).
        module: Dotted path of the module exporting the group Typer.
        attr: Attribute name of the :class:`typer.Typer` in *module*.
    """

    name: str
    module: str
    attr: str


@dataclass(frozen=True, slots=True)
class CommandRow:
    """A direct command registered on the root app via ``command``.

    Attributes:
        name: Top-level command name (``eawf <name>``).
        module: Dotted path of the module exporting the handler.
        attr: Attribute name of the handler callable in *module*.
        help_text: Override help string, or ``None`` to use the
            handler's own docstring.
    """

    name: str
    module: str
    attr: str
    help_text: str | None


@dataclass(frozen=True, slots=True)
class SideEffectRow:
    """A module imported solely for its import-time registration effect.

    The module attaches a verb onto an already-mounted group's Typer at
    import time, so it carries no attribute — importing it is the whole
    effect, and it must happen before that group is mounted.

    Attributes:
        module: Dotted path of the module to import.
        group: Name of the group row whose Typer the module attaches onto.
    """

    module: str
    group: str


#: Declarative registration table, walked in order by
#: :func:`register_commands`. Order is preserved from the historical
#: hand-written registration wall in :mod:`eawf.surfaces.cli.app`.
COMMAND_REGISTRY: tuple[GroupRow | CommandRow | SideEffectRow, ...] = (
    # Strict state / envelope validator.
    CommandRow("validate", "eawf.surfaces.cli.commands.validate", "validate", None),
    # The track noun.
    GroupRow("track", "eawf.surfaces.cli.commands.lifecycle", "track_app"),
    # Native epoch-2 lifecycle nouns (milestone / batch / task / run). Their
    # verbs are attached by the ``domain`` sibling that
    # ``commands.lifecycle`` imports, so mounting the apps is enough.
    GroupRow("milestone", "eawf.surfaces.cli.commands.lifecycle", "milestone_app"),
    GroupRow("batch", "eawf.surfaces.cli.commands.lifecycle", "batch_app"),
    GroupRow("task", "eawf.surfaces.cli.commands.lifecycle", "task_app"),
    GroupRow("run", "eawf.surfaces.cli.commands.lifecycle", "run_app"),
    GroupRow("repository", "eawf.surfaces.cli.commands.lifecycle", "repository_app"),
    # Post-cutover ledger records (audit / decision / artifact).
    GroupRow("record", "eawf.surfaces.cli.commands.record", "record_app"),
    # The checks behind a filed claim's evidence rungs.
    GroupRow("claim", "eawf.surfaces.cli.commands.claim", "claim_app"),
    # Memory.
    GroupRow("memory", "eawf.surfaces.cli.commands.memory", "memory_app"),
    # Status command + state / store groups.
    CommandRow(
        "status",
        "eawf.surfaces.cli.commands.status",
        "status",
        "Show active pointers, blockers, and git head.",
    ),
    GroupRow("store", "eawf.surfaces.cli.commands.store", "store_app"),
    # Read-only daemon WAL inspection.
    GroupRow("wal", "eawf.surfaces.cli.commands.wal", "wal_app"),
    # Layered config.
    GroupRow("config", "eawf.surfaces.cli.commands.config", "config_app"),
    # Co-author trailers.
    GroupRow("coauthor", "eawf.surfaces.cli.commands.coauthor", "coauthor_app"),
    # Doctor diagnostics + doc-drift linter.
    GroupRow("doctor", "eawf.surfaces.cli.commands.doctor", "doctor_app"),
    GroupRow("doc", "eawf.surfaces.cli.commands.doc", "doc_app"),
    # Schema dump + autogen reference.
    GroupRow("schema", "eawf.surfaces.cli.commands.schema", "schema_app"),
    # Release artifacts.
    GroupRow("release", "eawf.surfaces.cli.commands.release", "release_app"),
    # Repository init (born at epoch 2).
    CommandRow(
        "init",
        "eawf.surfaces.cli.commands.init",
        "init_cmd",
        "Initialise a new Eä Workflow workspace.",
    ),
    # Workspace + repo groups, clone-repo command.
    GroupRow("workspace", "eawf.surfaces.cli.commands.workspace", "workspace_app"),
    GroupRow("repo", "eawf.surfaces.cli.commands.repo", "repo_app"),
    CommandRow("clone-repo", "eawf.surfaces.cli.commands.clone_repo", "clone_repo_cmd", None),
    # Squash scratch commits into one commit per Batch before a push.
    CommandRow("publish", "eawf.surfaces.cli.commands.publish", "publish_cmd", None),
    # Output envelope render bridge.
    CommandRow(
        "render-output",
        "eawf.surfaces.cli.commands.render_output",
        "render_output_cmd",
        (
            "Convert between JSON and markdown forms of the output envelope "
            "(reads JSON or markdown from stdin). At a TTY with no piped data "
            "the command exits 2 with a hint instead of hanging."
        ),
    ),
    # Managed-asset re-render.
    CommandRow("sync", "eawf.surfaces.cli.commands.sync", "sync_cmd", None),
    # Rule views the sync render wrote.
    GroupRow("rules", "eawf.surfaces.cli.commands.rules", "rules_app"),
    # Hook runner + plugins + harness adapters + skills.
    GroupRow("hook", "eawf.surfaces.cli.commands.hook", "hook_app"),
    GroupRow("plugin", "eawf.surfaces.cli.commands.plugin", "plugin_app"),
    GroupRow("cc", "eawf.surfaces.cli.commands.cc", "cc_app"),
    GroupRow("runtime", "eawf.surfaces.cli.commands.runtime", "runtime_app"),
    GroupRow("skill", "eawf.surfaces.cli.commands.skill", "skill_app"),
    # MCP servers and plan revisions.
    GroupRow("mcp", "eawf.surfaces.cli.commands.mcp", "mcp_app"),
    GroupRow("plan", "eawf.surfaces.cli.commands.plan", "plan_app"),
    # The campaign and question entity groups, beside the research reads.
    GroupRow("research", "eawf.surfaces.cli.commands.research", "research_app"),
    GroupRow("campaign", "eawf.surfaces.cli.commands.research", "campaign_app"),
    GroupRow("question", "eawf.surfaces.cli.commands.research", "question_app"),
    # The action entity group: what the Attention register asks a principal about.
    GroupRow("action", "eawf.surfaces.cli.commands.action", "action_app"),
    # The decision entity group: the native decisions, read without a daemon.
    GroupRow("decision", "eawf.surfaces.cli.commands.entity_read", "decision_app"),
    # The list and show reads of the entity groups, attached onto each mounted group.
    SideEffectRow("eawf.surfaces.cli.commands.entity_read", "task"),
    SideEffectRow("eawf.surfaces.cli.commands.entity_read", "milestone"),
    SideEffectRow("eawf.surfaces.cli.commands.entity_read", "plan"),
    SideEffectRow("eawf.surfaces.cli.commands.entity_read", "action"),
    # The plain-text report of one Run, attached onto the mounted run group.
    SideEffectRow("eawf.surfaces.cli.commands.run_report", "run"),
    # The Run controls, attached onto the mounted run group.
    SideEffectRow("eawf.surfaces.cli.commands.run_control", "run"),
    # Dispatching one Task as a new Run, attached onto the mounted run group.
    SideEffectRow("eawf.surfaces.cli.commands.run_dispatch", "run"),
    # Profile scaffolding + trust ledger.
    GroupRow("profile", "eawf.surfaces.cli.commands.profile", "profile_app"),
    # Rolling workflow metrics.
    CommandRow(
        "metrics",
        "eawf.surfaces.cli.commands.metrics",
        "metrics_cmd",
        (
            "Show workflow metrics — the estimation roll-up, plus telemetry, "
            "variance, jury-validation and conduct sub-verbs."
        ),
    ),
    CommandRow(
        "why",
        "eawf.surfaces.cli.commands.why",
        "why_cmd",
        "Explain why an EAWF entity has its current trust tier.",
    ),
    CommandRow(
        "verbs",
        "eawf.surfaces.cli.commands.verbs",
        "verbs_cmd",
        "List every verb with its entity, parameters, typed errors and effect class.",
    ),
    # Daemon + bench + telemetry + snapshot + backup.
    GroupRow("daemon", "eawf.surfaces.cli.commands.daemon", "daemon_app"),
    # Streams any submitted operation, whichever entity verb submitted it.
    CommandRow("follow", "eawf.surfaces.cli.commands.operation", "follow_cmd", None),
    GroupRow("bench", "eawf.surfaces.cli.commands.bench", "bench_app"),
    GroupRow("telemetry", "eawf.surfaces.cli.commands.telemetry", "telemetry_app"),
    GroupRow("reflect", "eawf.surfaces.cli.commands.reflect", "reflect_app"),
    GroupRow("snapshot", "eawf.surfaces.cli.commands.snapshot", "snapshot_app"),
    GroupRow("vfl", "eawf.surfaces.cli.commands.vfl", "vfl_app"),
    GroupRow("backup", "eawf.surfaces.cli.commands.backup", "backup_app"),
    GroupRow("local", "eawf.surfaces.cli.commands.local", "local_app"),
    # Completion + prose help topics.
    GroupRow("completion", "eawf.surfaces.cli.commands.completion", "completion_app"),
    GroupRow("help", "eawf.surfaces.cli.commands.help", "help_app"),
)


def mount(rows: Iterable[GroupRow | CommandRow | SideEffectRow]) -> typer.Typer:
    """Return a Typer carrying *rows*, mounted in order as the root app mounts them.

    Groups are mounted via :meth:`typer.Typer.add_typer`, direct commands via
    :meth:`typer.Typer.command`, and side-effect rows by importing their module
    (which attaches a verb onto a group mounted beside it). Each group /
    command uses ``rich_help_panel=panel_for(name)`` so the ``eawf --help``
    panel grouping matches the historical wall.

    Args:
        rows: The registry rows to mount, in registration order.

    Returns:
        A bare Typer whose registered commands and groups are exactly *rows*.

    Raises:
        ModuleNotFoundError: A row names a module that does not exist.
        AttributeError: A row names an attribute its module does not export.
    """
    host = typer.Typer()
    for row in rows:
        module = importlib.import_module(row.module)
        if isinstance(row, SideEffectRow):
            continue
        target = getattr(module, row.attr)
        if isinstance(row, GroupRow):
            host.add_typer(target, name=row.name, rich_help_panel=panel_for(row.name))
        else:
            host.command(
                name=row.name,
                help=row.help_text,
                rich_help_panel=panel_for(row.name),
            )(target)
    return host


class LazyCommandGroup(UpgradeRefusalTyperGroup):
    """The root ``eawf`` group: mounts a registry row the first time it is asked for.

    Resolving one command name mounts that row alone, with the side-effect rows that
    attach onto it. Anything that reads :attr:`commands` (help, completion, a walk of
    the whole tree) mounts every row still unmounted, so the mapping it sees, and its
    order, are what mounting every row up front produced.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._unmounted: dict[str, GroupRow | CommandRow] = {
            row.name: row for row in COMMAND_REGISTRY if not isinstance(row, SideEffectRow)
        }
        self._mounted: dict[str, click.Command] = {}
        super().__init__(*args, **kwargs)

    @property
    def commands(self) -> dict[str, click.Command]:
        """Return every subcommand by name, mounting the rows not mounted yet."""
        if self._unmounted:
            self._mount_rest()
        return self._mounted

    @commands.setter
    def commands(self, value: Mapping[str, click.Command]) -> None:
        self._mounted = dict(value)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        """Return the subcommand *cmd_name*, mounting its row if it is not mounted yet."""
        mounted = self._mounted.get(cmd_name)
        if mounted is not None:
            return mounted
        row = self._unmounted.pop(cmd_name, None)
        if row is None:
            return None
        attached = (
            r for r in COMMAND_REGISTRY if isinstance(r, SideEffectRow) and r.group == row.name
        )
        command = typer.main.get_group(mount((row, *attached))).commands[cmd_name]
        self._mounted[cmd_name] = command
        logger.debug(f"LazyCommandGroup mounted name={cmd_name}")
        return command

    def _mount_rest(self) -> None:
        """Mount every unmounted row, keeping the order an up-front mount gives."""
        pending = tuple(
            row
            for row in COMMAND_REGISTRY
            if isinstance(row, SideEffectRow) or row.name in self._unmounted
        )
        built = typer.main.get_group(mount(pending)).commands
        self._unmounted.clear()
        # Typer lists a group's own commands first, then its sub-groups, each in
        # registration order; the root's inline commands precede every registry row.
        names = [
            *(row.name for row in COMMAND_REGISTRY if isinstance(row, CommandRow)),
            *(row.name for row in COMMAND_REGISTRY if isinstance(row, GroupRow)),
        ]
        inline = {name: cmd for name, cmd in self._mounted.items() if name not in names}
        self._mounted = inline | {name: self._mounted.get(name) or built[name] for name in names}


__all__ = [
    "COMMAND_REGISTRY",
    "CommandRow",
    "GroupRow",
    "LazyCommandGroup",
    "SideEffectRow",
    "mount",
]
