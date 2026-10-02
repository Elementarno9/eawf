"""The campaign and question entity groups.

``campaign`` and ``question`` are mounted at the CLI root by the command
registry. The ``question`` group files operator decisions, the pending
actions an agent raises when it reaches a choice it may not take.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any, Final

import typer

from eawf.surfaces.cli import errors
from eawf.surfaces.cli.commands.question_decision import (
    question_answer,
    question_open_decision,
    question_reply,
)
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

campaign_app = typer.Typer(
    name="campaign",
    help="Plan, drive and cancel research Campaigns.",
    no_args_is_help=True,
)

question_app = typer.Typer(
    name="question",
    help="File and answer operator decisions.",
    no_args_is_help=True,
)

# an epoch-2 verb of this group, kept in a module of its own so it is not an epoch-1 one
question_app.command("open-decision")(question_open_decision)
question_app.command("answer")(question_answer)
question_app.command("reply")(question_reply)


#: The daemon verbs the campaign group forwards to, spelled here so the Typer tree
#: builds without the daemon method registry on the path.
CAMPAIGN_START: Final = "runtime.campaign.start"
CAMPAIGN_RUN: Final = "runtime.campaign.run"
CAMPAIGN_CLOSE: Final = "runtime.campaign.close"
CAMPAIGN_BUDGET_SET: Final = "runtime.campaign.budget.set"
CAMPAIGN_VIEW: Final = "projection.campaign.view"

_CampaignKey = Annotated[str, typer.Argument(help="The Campaign's CAM-#### key.")]
_Actor = Annotated[str, typer.Option("--actor", help="Principal key the change is made as.")]
_Agents = Annotated[
    int | None,
    typer.Option(
        "--agents",
        min=1,
        max=12,
        help="Ready steps one round dispatches; the research.agent_count layer otherwise.",
    ),
]
_Runtime = Annotated[
    str, typer.Option("--runtime", help="Runtime whose headless session works each round.")
]


def _campaign_call(
    ctx: typer.Context, method: str, params: dict[str, Any], verb_text: str
) -> dict[str, Any] | None:
    """Send one campaign verb and return its answer, or emit the error and return ``None``."""
    from eawf.surfaces.cli._daemon_client import DaemonRpcError
    from eawf.surfaces.cli.commands.domain import _native_answer

    flags: GlobalFlags = ctx.obj
    try:
        return _native_answer(
            method,
            params,
            flags=flags,
            verb_text=verb_text,
            read=method == CAMPAIGN_VIEW,
        )
    except DaemonRpcError as exc:
        errors.emit_error(errors.cli_error_for_rpc(exc.code, exc.message), flags=flags)
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
    return None


@campaign_app.command("new")
def campaign_new(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="What the Campaign researches, in one line.")],
    actor: _Actor,
    track: Annotated[str, typer.Option("--track", help="URN of the Track that owns it.")],
    question: Annotated[
        list[str],
        typer.Option("--question", help="A question to work; repeat it, the Campaign's own first."),
    ],
    depth: Annotated[
        str | None,
        typer.Option(
            "--depth",
            help="shallow|medium|deep|exhaustive; the research.default_depth layer otherwise.",
        ),
    ] = None,
    agents: _Agents = None,
    budget_rounds: Annotated[
        int | None,
        typer.Option("--budget-rounds", min=1, help="Hard limit on the rounds it runs."),
    ] = None,
    budget_tokens: Annotated[
        int | None,
        typer.Option("--budget-tokens", min=1, help="Hard limit on the tokens its rounds spend."),
    ] = None,
    runtime: _Runtime = "claude-code",
    run: Annotated[
        bool, typer.Option("--run/--no-run", help="Start driving it once it is approved.")
    ] = False,
) -> None:
    """Plan a research Campaign from its brief and approve the plan.

    The daemon files each question, plans one step per question and method
    the depth names plus a closing synthesis, and approves the plan under the
    Track. With ``--run`` it also starts driving it; ``campaign run`` does so
    later. A budget left unset bounds the Campaign by one round per step.
    """
    axes = [
        {"axis_kind": kind, "limit": limit, "unit": kind}
        for kind, limit in (("rounds", budget_rounds), ("tokens", budget_tokens))
        if limit is not None
    ]
    params: dict[str, Any] = {
        "actor": actor,
        "track_ref": track,
        "title": title,
        "questions": question,
        "runtime": runtime,
        "drive": run,
    }
    params |= {
        key: value
        for key, value in (
            ("depth", depth),
            ("agents", agents),
            ("budget", {"axes": axes} if axes else None),
        )
        if value is not None
    }
    answer = _campaign_call(ctx, CAMPAIGN_START, params, "campaign new")
    if answer is None:
        return
    record = answer["record"]
    text = f"approved campaign {record['key']} ({len(record['plan_steps'])} steps)" + (
        "; driving" if answer["driving"] else ""
    )
    emit_json_or_text(answer, text, flags=ctx.obj)


@campaign_app.command("run")
def campaign_run(
    ctx: typer.Context,
    campaign_key: _CampaignKey,
    actor: _Actor,
    agents: _Agents = None,
    runtime: _Runtime = "claude-code",
) -> None:
    """Drive an approved Campaign round by round in the daemon.

    Each ready step runs on a Run of its own; each round checkpoints its
    report as an artifact revision, charges the budget and finishes its step.
    The drive runs behind the answer: follow it in the console's Campaign
    screen. A hard budget axis at its limit stops it and says why.
    """
    params: dict[str, Any] = {"actor": actor, "campaign_key": campaign_key, "runtime": runtime}
    if agents is not None:
        params["agents"] = agents
    answer = _campaign_call(ctx, CAMPAIGN_RUN, params, "campaign run")
    if answer is None:
        return
    text = (
        f"driving campaign {campaign_key}"
        if answer["driving"]
        else f"campaign {campaign_key} is already being driven"
    )
    emit_json_or_text(answer, text, flags=ctx.obj)


@campaign_app.command("budget")
def campaign_budget(
    ctx: typer.Context,
    campaign_key: _CampaignKey,
    actor: _Actor,
    expected_revision: Annotated[
        int,
        typer.Option(
            "--expected-revision", min=1, help="The Campaign revision the limits were decided at."
        ),
    ],
    rounds: Annotated[
        int | None, typer.Option("--rounds", min=1, help="Hard limit on the rounds it runs.")
    ] = None,
    tokens: Annotated[
        int | None,
        typer.Option("--tokens", min=1, help="Hard limit on the tokens its rounds spend."),
    ] = None,
) -> None:
    """Set an active Campaign's budget limits, keeping what it already spent.

    A limit raised past the spend lets a Campaign its budget stopped dispatch
    again; one lowered to the spend stops it. Setting the limits it already
    holds writes nothing.
    """
    limits = {kind: limit for kind, limit in (("rounds", rounds), ("tokens", tokens)) if limit}
    if not limits:
        raise typer.BadParameter("set at least one of --rounds or --tokens")
    view = _campaign_call(ctx, CAMPAIGN_VIEW, {"campaign_key": campaign_key}, "campaign budget")
    if view is None:
        return
    params = {
        "actor": actor,
        "urn": view["campaign_ref"],
        "expected_revision": expected_revision,
        "limits": limits,
    }
    answer = _campaign_call(ctx, CAMPAIGN_BUDGET_SET, params, "campaign budget")
    if answer is None:
        return
    text = (
        f"set the budget of campaign {campaign_key}"
        if answer["committed"]
        else f"campaign {campaign_key} already holds that budget"
    )
    emit_json_or_text(answer, text, flags=ctx.obj)


@campaign_app.command("cancel")
def campaign_cancel(
    ctx: typer.Context,
    campaign_key: _CampaignKey,
    actor: _Actor,
    reason: Annotated[str, typer.Option("--reason", help="Why it is abandoned, in one line.")],
    expected_revision: Annotated[
        int,
        typer.Option(
            "--expected-revision", min=1, help="The Campaign revision the cancel was decided at."
        ),
    ],
) -> None:
    """Cancel an active Campaign, recording why; its steps and artifacts stay.

    The Campaign's key is resolved to its reference; the revision is the
    caller's, so a Campaign that moved since it was read is refused rather
    than cancelled.
    """
    view = _campaign_call(ctx, CAMPAIGN_VIEW, {"campaign_key": campaign_key}, "campaign cancel")
    if view is None:
        return
    params = {
        "actor": actor,
        "urn": view["campaign_ref"],
        "expected_revision": expected_revision,
        "to_status": "cancelled",
        "reason": reason,
    }
    answer = _campaign_call(ctx, CAMPAIGN_CLOSE, params, "campaign cancel")
    if answer is None:
        return
    emit_json_or_text(answer, f"cancelled campaign {campaign_key}", flags=ctx.obj)
