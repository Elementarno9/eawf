"""Typer dispatcher for ``eawf`` with global flags and version banner.

The root callback parses the global flags (``--json``, ``--plain``,
``--no-input``, ``-w/--workspace``) and stores a
:class:`eawf.cli.flags.GlobalFlags` on ``ctx.obj``. Every subcommand pulls the
dataclass back out via :attr:`typer.Context.obj` to drive its emission and
output choices. ``--scope`` is intentionally *not* hoisted to the root — see
:mod:`eawf.cli.flags` for the rationale.

The bare invocation (``eawf`` with no subcommand) prints the version banner —
this matches the Phase 1 behaviour and avoids breaking the validate test
suite. ``--version`` short-circuits via the eager callback and exits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from eawf import __version__
from eawf.cli.commands.validate import validate as validate_cmd
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text

app = typer.Typer(
    name="eawf",
    help="Eä Workflow — agent-driven development framework (v0.1 in development).",
    no_args_is_help=False,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON output."),
    ] = False,
    plain_output: Annotated[
        bool,
        typer.Option("--plain", help="Disable colour and Rich markup."),
    ] = False,
    no_input: Annotated[
        bool,
        typer.Option("--no-input", help="Fail closed instead of prompting the user."),
    ] = False,
    workspace: Annotated[
        Path | None,
        typer.Option("-w", "--workspace", help="Workspace root used to locate .ea/state.json."),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the eawf version and exit.",
        ),
    ] = False,
) -> None:
    """Populate ``ctx.obj`` with resolved global flags and emit the banner."""
    ctx.obj = GlobalFlags(
        json_output=json_output,
        plain_output=plain_output,
        no_input=no_input,
        workspace=workspace,
    )
    if ctx.invoked_subcommand is None:
        # Bare ``eawf`` on a TTY routes to the TUI (config.ui.bare_command
        # default: "tui"); plain / no-input / non-TTY falls back to the
        # deterministic status emission per D15 + D23.
        from eawf.tui.app import run_tui

        rc = run_tui(workspace=workspace, no_input=no_input, plain=plain_output)
        raise typer.Exit(code=rc)


@app.command(name="version")
def version_cmd(ctx: typer.Context) -> None:
    """Show the eawf version (text or JSON envelope)."""
    flags: GlobalFlags = ctx.obj
    emit_json_or_text(
        {"version": __version__},
        f"eawf {__version__}",
        flags=flags,
    )


@app.command(name="scope-debug", hidden=True)
def scope_debug(ctx: typer.Context) -> None:
    """Print resolved global flags. Test/internal use only."""
    flags: GlobalFlags = ctx.obj
    text = (
        f"workspace={flags.workspace}"
        f"\njson={flags.json_output}"
        f"\nplain={flags.plain_output}"
        f"\nno_input={flags.no_input}"
    )
    typer.echo(text)


app.command(name="validate")(validate_cmd)


# --- P02 wave registrations (each wave appends here in wave order) ---
# Wave W01 - lifecycle nouns (project/subproject/phase/iter/wave).
from eawf.cli.commands.lifecycle import iter_app as _iter_app  # noqa: E402
from eawf.cli.commands.lifecycle import phase_app as _phase_app  # noqa: E402
from eawf.cli.commands.lifecycle import project_app as _project_app  # noqa: E402
from eawf.cli.commands.lifecycle import subproject_app as _subproject_app  # noqa: E402
from eawf.cli.commands.lifecycle import wave_app as _wave_app  # noqa: E402

app.add_typer(_project_app, name="project")
app.add_typer(_subproject_app, name="subproject")
app.add_typer(_phase_app, name="phase")
app.add_typer(_iter_app, name="iter")
app.add_typer(_wave_app, name="wave")

# --- W02 evidence registrations ---
from eawf.cli.commands.evidence import (  # noqa: E402
    artifact_app,
    audit_app,
    backlog_app,
    decision_app,
    goal_app,
    hypothesis_app,
    incident_app,
    outcome_app,
)

app.add_typer(goal_app, name="goal")
app.add_typer(outcome_app, name="outcome")
app.add_typer(hypothesis_app, name="hypothesis")
app.add_typer(audit_app, name="audit")
app.add_typer(incident_app, name="incident")
app.add_typer(decision_app, name="decision")
app.add_typer(artifact_app, name="artifact")
app.add_typer(backlog_app, name="backlog")
# --- end W02 ---

# --- W03 estimation registrations ---
from eawf.cli.commands.estimation import actual_app, estimate_app  # noqa: E402

app.add_typer(estimate_app, name="estimate")
app.add_typer(actual_app, name="actual")
# --- end W03 ---

# --- W04 memory+session registrations ---
from eawf.cli.commands.memory import memory_app  # noqa: E402
from eawf.cli.commands.session import session_app  # noqa: E402

app.add_typer(memory_app, name="memory")
app.add_typer(session_app, name="session")
# --- end W04 ---

# --- W05 status+state+store registrations ---
from eawf.cli.commands.state import state_app  # noqa: E402
from eawf.cli.commands.status import status as status_cmd  # noqa: E402
from eawf.cli.commands.store import store_app  # noqa: E402

app.command(name="status", help="Show active pointers, blockers, and git head.")(status_cmd)
app.add_typer(state_app, name="state")
app.add_typer(store_app, name="store")
# --- end W05 ---

# --- W06 config registrations ---
from eawf.cli.commands.config import config_app  # noqa: E402

app.add_typer(config_app, name="config")
# --- end W06 ---

# --- P17 W02 coauthor registration ---
from eawf.cli.commands.coauthor import coauthor_app  # noqa: E402

app.add_typer(coauthor_app, name="coauthor")
# --- end P17 W02 ---

# --- P18 W06 typed agent report registration ---
from eawf.cli.commands.agent_report import (  # noqa: E402
    agent_report_app as _agent_report_app,
)
from eawf.cli.commands.agent_report import operator_app as _operator_app  # noqa: E402

app.add_typer(_agent_report_app, name="agent-report")
app.add_typer(_operator_app, name="operator")
# --- end P18 W06 ---

# --- P19 W06 roadmap planner registration ---
from eawf.cli.commands.roadmap import roadmap_app  # noqa: E402

app.add_typer(roadmap_app, name="roadmap")
# --- end P19 W06 ---

# --- P03 W01 doctor registration ---
from eawf.cli.commands.doctor import doctor_app  # noqa: E402

app.add_typer(doctor_app, name="doctor")
# --- end P03 W01 ---

# --- P12 W03 doc-drift linter registration ---
from eawf.cli.commands.doc import doc_app  # noqa: E402

app.add_typer(doc_app, name="doc")
# --- end P12 W03 ---

# --- P12 W04 PR body + wiki render registration ---
from eawf.cli.commands.pr import pr_app  # noqa: E402
from eawf.cli.commands.release import release_app  # noqa: E402
from eawf.cli.commands.wiki import wiki_app  # noqa: E402

app.add_typer(pr_app, name="pr")
app.add_typer(release_app, name="release")
app.add_typer(wiki_app, name="wiki")
# --- end P12 W04 ---

# --- P03 W05 init registration ---
from eawf.cli.commands.init import init_cmd  # noqa: E402

app.command(name="init", help="Initialise a new Eä Workflow workspace.")(init_cmd)
# --- end P03 W05 ---

# --- P03 W06 workspace + repo + clone-repo registrations ---
from eawf.cli.commands.clone_repo import clone_repo_cmd  # noqa: E402
from eawf.cli.commands.repo import repo_app  # noqa: E402
from eawf.cli.commands.workspace import workspace_app  # noqa: E402

app.add_typer(workspace_app, name="workspace")
app.add_typer(repo_app, name="repo")
app.command(name="clone-repo")(clone_repo_cmd)
# --- end P03 W06 ---

# --- P03 W07 render-output registration ---
from eawf.cli.commands.render_output import render_output_cmd  # noqa: E402

app.command(
    name="render-output",
    help=(
        "Convert between JSON and markdown forms of the output envelope "
        "(reads JSON or markdown from stdin). At a TTY with no piped data "
        "the command exits 2 with a hint instead of hanging."
    ),
)(render_output_cmd)
# --- end W07 ---

# --- P03 W08 sync registration ---
from eawf.cli.commands.sync import sync_cmd  # noqa: E402

app.command(name="sync")(sync_cmd)
# --- end W08 ---

# --- P04 W04 hook registration ---
from eawf.cli.commands.hook import hook_app  # noqa: E402

app.add_typer(hook_app, name="hook")
# --- end P04 W04 ---

# --- P04 W05 plugin registration ---
from eawf.cli.commands.plugin import plugin_app  # noqa: E402

app.add_typer(plugin_app, name="plugin")
# --- end P04 W05 ---

# --- P04 W06 cc (Claude Code adapter) registration ---
from eawf.cli.commands.cc import cc_app  # noqa: E402

app.add_typer(cc_app, name="cc")
# --- end P04 W06 ---

# --- P04 W07 skill registration ---
from eawf.cli.commands.skill import skill_app  # noqa: E402

app.add_typer(skill_app, name="skill")
# --- end P04 W07 ---

# --- P05 W01 worktree registration ---
from eawf.cli.commands.worktree import worktree_app  # noqa: E402

app.add_typer(worktree_app, name="worktree")
# --- end P05 W01 ---

# --- P05 W02 flow registration ---
from eawf.cli.commands.flow import flow_app  # noqa: E402

app.add_typer(flow_app, name="flow")
# --- end P05 W02 ---

# --- P05 W04 mcp registration ---
from eawf.cli.commands.mcp import mcp_app  # noqa: E402

app.add_typer(mcp_app, name="mcp")
# --- end P05 W04 ---

# --- P05 W05 plan registration ---
from eawf.cli.commands.plan import plan_app  # noqa: E402

app.add_typer(plan_app, name="plan")
# --- end P05 W05 ---

# --- P16 W06 research show registration ---
from eawf.cli.commands.research import research_app  # noqa: E402

app.add_typer(research_app, name="research")
# --- end P16 W06 ---

# --- P16 W10 draft registration ---
from eawf.cli.commands.draft import draft_app  # noqa: E402

app.add_typer(draft_app, name="draft")
# --- end P16 W10 ---

# --- P08 W05 wave fix-ci registration (commands attach to wave_app on import) ---
import eawf.cli.commands.wave_ci  # noqa: E402, F401

# --- end P08 W05 ---
# --- P08 W06 pr-review registration (wave review verb hangs off wave_app) ---
from eawf.cli.commands import pr_review as _pr_review  # noqa: E402, F401

# --- end P08 W06 ---
# --- P12 W06 sandbox policy registration ---
from eawf.cli.commands import wave_policy as _wave_policy  # noqa: E402, F401

# --- end P12 W06 ---
# --- P12 W07 file impact graph registration ---
from eawf.cli.commands.impact import impact_cmd  # noqa: E402

app.command(name="impact", help="Render decision → wave → file-glob impact graph.")(impact_cmd)
# --- end P12 W07 ---

# --- P14 W05 profile registration (new + validate + TOFU trust ledger) ---
from eawf.cli.commands.profile import profile_app  # noqa: E402

app.add_typer(profile_app, name="profile")
# --- end P14 W05 ---

# --- P14 W10 TUI registration ---
from eawf.tui.app import run_tui as _run_tui  # noqa: E402


@app.command(name="tui", help="Open the Rich-backed Eä TUI (or text fallback off-TTY).")
def _tui_cmd(ctx: typer.Context) -> None:
    flags: GlobalFlags = ctx.obj
    rc = _run_tui(workspace=flags.workspace, no_input=flags.no_input, plain=flags.plain_output)
    raise typer.Exit(code=rc)


# --- end P14 W10 ---

# --- P20 W08 metrics registration ---
from eawf.cli.commands.metrics import metrics_cmd  # noqa: E402

app.command(
    name="metrics",
    help=(
        "Show rolling workflow metrics — EU variance, audit pass rate, "
        "wave elapsed, and planned vs reactive split."
    ),
)(metrics_cmd)
# --- end P20 W08 ---


def main() -> None:
    app()
