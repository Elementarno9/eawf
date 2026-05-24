"""Declarative command registry for the ``eawf`` root Typer app.

:mod:`eawf.cli.app` builds the root :class:`typer.Typer` and wires its
inline-defined surfaces (root callback, ``version`` / ``scope-debug`` /
``tui`` commands, logging, ``main``). Everything else — the ~50 sub-Typer
groups and the handful of direct commands that hang off the root — is
declared here as data and mounted by :func:`register_commands`.

Three row kinds drive the mount:

* :class:`GroupRow` — a sub-Typer group mounted via ``app.add_typer``.
* :class:`CommandRow` — a direct command registered via ``app.command``.
* :class:`SideEffectRow` — a module imported purely for its import-time
  side effect (it attaches a verb onto an already-mounted group's Typer).

Each row names its source module by dotted path; :func:`register_commands`
resolves it lazily via :func:`importlib.import_module`, so re-introducing a
module-level heavy import in any command module is the only way to breach
the import-budget gate — the registry itself imports nothing heavy.

Every group / command is mounted with ``rich_help_panel=panel_for(name)``,
exactly as the hand-written wall did, and rows are walked in declaration
order so the registration sequence is preserved.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass

import typer

from eawf.cli.help_panels import panel_for

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
    import time, so it carries no name / attribute — importing it is the
    whole effect.

    Attributes:
        module: Dotted path of the module to import.
    """

    module: str


#: Declarative registration table, walked in order by
#: :func:`register_commands`. Order is preserved from the historical
#: hand-written registration wall in :mod:`eawf.cli.app`.
COMMAND_REGISTRY: tuple[GroupRow | CommandRow | SideEffectRow, ...] = (
    # Strict state / envelope validator.
    CommandRow("validate", "eawf.cli.commands.validate", "validate", None),
    # Lifecycle nouns (project / subproject / phase / iter / wave).
    GroupRow("project", "eawf.cli.commands.lifecycle", "project_app"),
    GroupRow("subproject", "eawf.cli.commands.lifecycle", "subproject_app"),
    GroupRow("phase", "eawf.cli.commands.lifecycle", "phase_app"),
    GroupRow("iter", "eawf.cli.commands.lifecycle", "iter_app"),
    GroupRow("wave", "eawf.cli.commands.lifecycle", "wave_app"),
    # Evidence nouns.
    GroupRow("goal", "eawf.cli.commands.evidence", "goal_app"),
    GroupRow("outcome", "eawf.cli.commands.evidence", "outcome_app"),
    GroupRow("hypothesis", "eawf.cli.commands.evidence", "hypothesis_app"),
    GroupRow("audit", "eawf.cli.commands.evidence", "audit_app"),
    GroupRow("incident", "eawf.cli.commands.evidence", "incident_app"),
    GroupRow("decision", "eawf.cli.commands.evidence", "decision_app"),
    GroupRow("artifact", "eawf.cli.commands.evidence", "artifact_app"),
    GroupRow("backlog", "eawf.cli.commands.evidence", "backlog_app"),
    # Estimation nouns.
    GroupRow("estimate", "eawf.cli.commands.estimation", "estimate_app"),
    GroupRow("actual", "eawf.cli.commands.estimation", "actual_app"),
    # Memory + session.
    GroupRow("memory", "eawf.cli.commands.memory", "memory_app"),
    GroupRow("session", "eawf.cli.commands.session", "session_app"),
    # Status command + state / store groups.
    CommandRow(
        "status",
        "eawf.cli.commands.status",
        "status",
        "Show active pointers, blockers, and git head.",
    ),
    GroupRow("state", "eawf.cli.commands.state", "state_app"),
    GroupRow("store", "eawf.cli.commands.store", "store_app"),
    # Layered config.
    GroupRow("config", "eawf.cli.commands.config", "config_app"),
    # Co-author trailers.
    GroupRow("coauthor", "eawf.cli.commands.coauthor", "coauthor_app"),
    # Typed agent reports + operator surface.
    GroupRow("agent-report", "eawf.cli.commands.agent_report", "agent_report_app"),
    GroupRow("operator", "eawf.cli.commands.agent_report", "operator_app"),
    # Roadmap planner.
    GroupRow("roadmap", "eawf.cli.commands.roadmap", "roadmap_app"),
    # Doctor diagnostics + doc-drift linter.
    GroupRow("doctor", "eawf.cli.commands.doctor", "doctor_app"),
    GroupRow("doc", "eawf.cli.commands.doc", "doc_app"),
    # Schema dump + autogen reference.
    GroupRow("schema", "eawf.cli.commands.schema", "schema_app"),
    # PR body / release / wiki renderers.
    GroupRow("pr", "eawf.cli.commands.pr", "pr_app"),
    GroupRow("release", "eawf.cli.commands.release", "release_app"),
    GroupRow("wiki", "eawf.cli.commands.wiki", "wiki_app"),
    # Workspace init.
    CommandRow(
        "init",
        "eawf.cli.commands.init",
        "init_cmd",
        "Initialise a new Eä Workflow workspace.",
    ),
    # Workspace + repo groups, clone-repo command.
    GroupRow("workspace", "eawf.cli.commands.workspace", "workspace_app"),
    GroupRow("repo", "eawf.cli.commands.repo", "repo_app"),
    CommandRow("clone-repo", "eawf.cli.commands.clone_repo", "clone_repo_cmd", None),
    # Output envelope render bridge.
    CommandRow(
        "render-output",
        "eawf.cli.commands.render_output",
        "render_output_cmd",
        (
            "Convert between JSON and markdown forms of the output envelope "
            "(reads JSON or markdown from stdin). At a TTY with no piped data "
            "the command exits 2 with a hint instead of hanging."
        ),
    ),
    # Managed-asset re-render.
    CommandRow("sync", "eawf.cli.commands.sync", "sync_cmd", None),
    # Hook runner + plugins + harness adapters + skills.
    GroupRow("hook", "eawf.cli.commands.hook", "hook_app"),
    GroupRow("plugin", "eawf.cli.commands.plugin", "plugin_app"),
    GroupRow("cc", "eawf.cli.commands.cc", "cc_app"),
    GroupRow("skill", "eawf.cli.commands.skill", "skill_app"),
    # Worktree dispatch + flow loop.
    GroupRow("worktree", "eawf.cli.commands.worktree", "worktree_app"),
    GroupRow("flow", "eawf.cli.commands.flow", "flow_app"),
    # MCP servers, plan render, research, draft.
    GroupRow("mcp", "eawf.cli.commands.mcp", "mcp_app"),
    GroupRow("plan", "eawf.cli.commands.plan", "plan_app"),
    GroupRow("research", "eawf.cli.commands.research", "research_app"),
    GroupRow("draft", "eawf.cli.commands.draft", "draft_app"),
    # Wave-attached verbs (fix-ci, review, policy) — imported for side
    # effect; each attaches its verb onto the already-mounted wave group.
    SideEffectRow("eawf.cli.commands.wave_ci"),
    SideEffectRow("eawf.cli.commands.pr_review"),
    SideEffectRow("eawf.cli.commands.wave_policy"),
    # File-impact graph.
    CommandRow(
        "impact",
        "eawf.cli.commands.impact",
        "impact_cmd",
        "Render decision → wave → file-glob impact graph.",
    ),
    # Profile scaffolding + trust ledger.
    GroupRow("profile", "eawf.cli.commands.profile", "profile_app"),
    # Rolling workflow metrics.
    CommandRow(
        "metrics",
        "eawf.cli.commands.metrics",
        "metrics_cmd",
        (
            "Show rolling workflow metrics — EU variance, audit pass rate, "
            "wave elapsed, and planned vs reactive split."
        ),
    ),
    # Daemon + spec + bench + telemetry + snapshot + migrate + backup +
    # calibrate.
    GroupRow("daemon", "eawf.cli.commands.daemon", "daemon_app"),
    GroupRow("spec", "eawf.cli.commands.spec", "spec_app"),
    GroupRow("bench", "eawf.cli.commands.bench", "bench_app"),
    GroupRow("telemetry", "eawf.cli.commands.telemetry", "telemetry_app"),
    GroupRow("snapshot", "eawf.cli.commands.snapshot", "snapshot_app"),
    GroupRow("migrate", "eawf.cli.commands.migrate", "migrate_app"),
    GroupRow("backup", "eawf.cli.commands.backup", "backup_app"),
    GroupRow("calibrate", "eawf.cli.commands.calibrate", "calibrate_app"),
    # Completion + prose help topics.
    GroupRow("completion", "eawf.cli.commands.completion", "completion_app"),
    GroupRow("help", "eawf.cli.commands.help", "help_app"),
)


def register_commands(app: typer.Typer) -> None:
    """Mount every :data:`COMMAND_REGISTRY` row onto *app* in order.

    Groups are mounted via :meth:`typer.Typer.add_typer`, direct commands
    via :meth:`typer.Typer.command`, and side-effect rows by importing
    their module (which attaches a verb onto an already-mounted group).
    Each group / command uses ``rich_help_panel=panel_for(name)`` so the
    ``eawf --help`` panel grouping matches the historical wall.

    Args:
        app: The root ``eawf`` Typer app to mount commands onto.
    """
    for row in COMMAND_REGISTRY:
        if isinstance(row, SideEffectRow):
            importlib.import_module(row.module)
            continue
        module = importlib.import_module(row.module)
        target = getattr(module, row.attr)
        if isinstance(row, GroupRow):
            app.add_typer(target, name=row.name, rich_help_panel=panel_for(row.name))
        else:
            app.command(
                name=row.name,
                help=row.help_text,
                rich_help_panel=panel_for(row.name),
            )(target)


__all__ = [
    "COMMAND_REGISTRY",
    "CommandRow",
    "GroupRow",
    "SideEffectRow",
    "register_commands",
]
