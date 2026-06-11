"""Declarative command registry for the ``eawf`` root Typer app.

:mod:`eawf.surfaces.cli.app` builds the root :class:`typer.Typer` and wires its
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

from eawf.surfaces.cli.help_panels import panel_for

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
#: hand-written registration wall in :mod:`eawf.surfaces.cli.app`.
COMMAND_REGISTRY: tuple[GroupRow | CommandRow | SideEffectRow, ...] = (
    # Strict state / envelope validator.
    CommandRow("validate", "eawf.surfaces.cli.commands.validate", "validate", None),
    # Lifecycle nouns (project / track / phase / iter / wave).
    GroupRow("project", "eawf.surfaces.cli.commands.lifecycle", "project_app"),
    GroupRow("track", "eawf.surfaces.cli.commands.lifecycle", "track_app"),
    GroupRow("phase", "eawf.surfaces.cli.commands.lifecycle", "phase_app"),
    GroupRow("iter", "eawf.surfaces.cli.commands.lifecycle", "iter_app"),
    GroupRow("wave", "eawf.surfaces.cli.commands.lifecycle", "wave_app"),
    # Evidence nouns.
    GroupRow("goal", "eawf.surfaces.cli.commands.evidence", "goal_app"),
    GroupRow("outcome", "eawf.surfaces.cli.commands.evidence", "outcome_app"),
    GroupRow("hypothesis", "eawf.surfaces.cli.commands.evidence", "hypothesis_app"),
    GroupRow("audit", "eawf.surfaces.cli.commands.evidence", "audit_app"),
    GroupRow("incident", "eawf.surfaces.cli.commands.evidence", "incident_app"),
    GroupRow("decision", "eawf.surfaces.cli.commands.evidence", "decision_app"),
    GroupRow("artifact", "eawf.surfaces.cli.commands.evidence", "artifact_app"),
    GroupRow("backlog", "eawf.surfaces.cli.commands.evidence", "backlog_app"),
    GroupRow("evidence", "eawf.surfaces.cli.commands.evidence", "evidence_app"),
    # Estimation nouns.
    GroupRow("estimate", "eawf.surfaces.cli.commands.estimation", "estimate_app"),
    GroupRow("actual", "eawf.surfaces.cli.commands.estimation", "actual_app"),
    # Memory + session.
    GroupRow("memory", "eawf.surfaces.cli.commands.memory", "memory_app"),
    GroupRow("session", "eawf.surfaces.cli.commands.session", "session_app"),
    # Status command + state / store groups.
    CommandRow(
        "status",
        "eawf.surfaces.cli.commands.status",
        "status",
        "Show active pointers, blockers, and git head.",
    ),
    GroupRow("state", "eawf.surfaces.cli.commands.state", "state_app"),
    GroupRow("store", "eawf.surfaces.cli.commands.store", "store_app"),
    # Read-only daemon WAL inspection.
    GroupRow("wal", "eawf.surfaces.cli.commands.wal", "wal_app"),
    # Layered config.
    GroupRow("config", "eawf.surfaces.cli.commands.config", "config_app"),
    # Co-author trailers.
    GroupRow("coauthor", "eawf.surfaces.cli.commands.coauthor", "coauthor_app"),
    # Typed agent reports + operator surface.
    GroupRow("agent-report", "eawf.surfaces.cli.commands.agent_report", "agent_report_app"),
    GroupRow("operator", "eawf.surfaces.cli.commands.agent_report", "operator_app"),
    # Roadmap planner.
    GroupRow("roadmap", "eawf.surfaces.cli.commands.roadmap", "roadmap_app"),
    # Doctor diagnostics + doc-drift linter.
    GroupRow("doctor", "eawf.surfaces.cli.commands.doctor", "doctor_app"),
    GroupRow("doc", "eawf.surfaces.cli.commands.doc", "doc_app"),
    # Schema dump + autogen reference.
    GroupRow("schema", "eawf.surfaces.cli.commands.schema", "schema_app"),
    # PR body / release / wiki renderers.
    GroupRow("pr", "eawf.surfaces.cli.commands.pr", "pr_app"),
    GroupRow("release", "eawf.surfaces.cli.commands.release", "release_app"),
    GroupRow("wiki", "eawf.surfaces.cli.commands.wiki", "wiki_app"),
    # Workspace init.
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
    # Hook runner + plugins + harness adapters + skills.
    GroupRow("hook", "eawf.surfaces.cli.commands.hook", "hook_app"),
    GroupRow("plugin", "eawf.surfaces.cli.commands.plugin", "plugin_app"),
    GroupRow("cc", "eawf.surfaces.cli.commands.cc", "cc_app"),
    GroupRow("skill", "eawf.surfaces.cli.commands.skill", "skill_app"),
    # Worktree dispatch + flow loop + headless dispatch pause/resume.
    GroupRow("worktree", "eawf.surfaces.cli.commands.worktree", "worktree_app"),
    GroupRow("flow", "eawf.surfaces.cli.commands.flow", "flow_app"),
    GroupRow("dispatch", "eawf.surfaces.cli.commands.dispatch", "dispatch_app"),
    # MCP servers, plan render, research, draft.
    GroupRow("mcp", "eawf.surfaces.cli.commands.mcp", "mcp_app"),
    GroupRow("plan", "eawf.surfaces.cli.commands.plan", "plan_app"),
    GroupRow("research", "eawf.surfaces.cli.commands.research", "research_app"),
    GroupRow("draft", "eawf.surfaces.cli.commands.draft", "draft_app"),
    # Wave-attached verbs (fix-ci, review, policy) — imported for side
    # effect; each attaches its verb onto the already-mounted wave group.
    SideEffectRow("eawf.surfaces.cli.commands.wave_ci"),
    SideEffectRow("eawf.surfaces.cli.commands.pr_review"),
    SideEffectRow("eawf.surfaces.cli.commands.wave_policy"),
    # File-impact graph.
    CommandRow(
        "impact",
        "eawf.surfaces.cli.commands.impact",
        "impact_cmd",
        "Render decision → wave → file-glob impact graph.",
    ),
    # Profile scaffolding + trust ledger.
    GroupRow("profile", "eawf.surfaces.cli.commands.profile", "profile_app"),
    # Rolling workflow metrics.
    CommandRow(
        "metrics",
        "eawf.surfaces.cli.commands.metrics",
        "metrics_cmd",
        (
            "Show rolling workflow metrics — EU variance, audit pass rate, "
            "wave elapsed, and planned vs reactive split."
        ),
    ),
    CommandRow(
        "why",
        "eawf.surfaces.cli.commands.why",
        "why_cmd",
        "Explain why an EAWF entity has its current trust tier.",
    ),
    # Daemon + spec + bench + telemetry + snapshot + migrate + backup +
    # calibrate.
    GroupRow("daemon", "eawf.surfaces.cli.commands.daemon", "daemon_app"),
    GroupRow("spec", "eawf.surfaces.cli.commands.spec", "spec_app"),
    GroupRow("bench", "eawf.surfaces.cli.commands.bench", "bench_app"),
    GroupRow("telemetry", "eawf.surfaces.cli.commands.telemetry", "telemetry_app"),
    GroupRow("snapshot", "eawf.surfaces.cli.commands.snapshot", "snapshot_app"),
    GroupRow("vfl", "eawf.surfaces.cli.commands.vfl", "vfl_app"),
    GroupRow("migrate", "eawf.surfaces.cli.commands.migrate", "migrate_app"),
    # Generalized entity-title backfill (all five lifecycle / decision kinds).
    GroupRow("backfill", "eawf.surfaces.cli.commands.backfill", "backfill_app"),
    GroupRow("backup", "eawf.surfaces.cli.commands.backup", "backup_app"),
    GroupRow("calibrate", "eawf.surfaces.cli.commands.calibrate", "calibrate_app"),
    # Completion + prose help topics.
    GroupRow("completion", "eawf.surfaces.cli.commands.completion", "completion_app"),
    GroupRow("help", "eawf.surfaces.cli.commands.help", "help_app"),
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
