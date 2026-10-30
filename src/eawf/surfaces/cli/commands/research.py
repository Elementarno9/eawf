"""The research, campaign and question groups.

``research``, ``campaign`` and ``question`` are mounted at the CLI root by the
command registry. ``research check`` judges one local research brief without a
daemon. The ``question`` group files operator decisions, the pending actions
an agent raises when it reaches a choice it may not take.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import orjson
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

research_app = typer.Typer(
    name="research",
    help="Check the local briefs the research skill writes.",
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


def _questions_file(path: Path) -> list[dict[str, Any]]:
    """Return the questions a ``--questions-file`` lists, each validated.

    The file is a YAML list whose items each hold ``question``, an optional
    ``context`` and an optional ``brief`` path relative to the repository root.

    Raises:
        UserError: The file cannot be read, is not YAML, is not a list, or an
            item does not hold the question shape, an unknown key included.
    """
    import yaml
    from pydantic import TypeAdapter
    from pydantic import ValidationError as PydanticValidationError

    from eawf.kernel.state.epoch2.campaign import CampaignQuestionSpec

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise errors.UserError(
            f"--questions-file {path} cannot be read as YAML: {exc}", kind="InvalidInput"
        ) from exc
    if not isinstance(raw, list) or not raw:
        raise errors.UserError(
            f"--questions-file {path} must hold a non-empty YAML list of questions",
            kind="InvalidInput",
        )
    try:
        items = TypeAdapter(list[CampaignQuestionSpec]).validate_python(raw)
    except PydanticValidationError as exc:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in exc.errors()})
        raise errors.UserError(
            f"--questions-file {path} is not a valid questions list; check {', '.join(fields)}",
            kind="InvalidInput",
        ) from exc
    return [item.model_dump(mode="json") for item in items]


@research_app.command("check")
def research_check(
    ctx: typer.Context,
    brief: Annotated[Path, typer.Argument(help="The brief's markdown file.")],
) -> None:
    """Check that a research brief holds every section, in order, with sound References.

    The brief needs an H1 title and exactly the sections Question, Findings,
    Alternatives, Recommendation, Open gaps and References. References is a
    numbered table of repo-relative paths, URLs or Eawf URNs, each cited in
    the prose as ``[N]``. Exits non-zero naming every defect.
    """
    from eawf.workflow.skills.research_brief import check_research_brief

    flags: GlobalFlags = ctx.obj
    try:
        text = brief.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.emit_error(
            errors.UserError(f"brief {brief} cannot be read: {exc}", kind="InvalidInput"),
            flags=flags,
        )
    verdict = check_research_brief(text)
    if not verdict.ok:
        errors.emit_error(
            errors.ValidationError(
                f"brief {brief} is refused: {'; '.join(verdict.errors)}",
                kind="ResearchBriefRefused",
            ),
            flags=flags,
            data={"violations": list(verdict.errors)},
        )
    emit_json_or_text(verdict.model_dump(mode="json"), f"brief {brief} passes", flags=flags)


@campaign_app.command("new")
def campaign_new(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="What the Campaign researches, in one line.")],
    actor: _Actor,
    track: Annotated[str, typer.Option("--track", help="URN of the Track that owns it.")],
    question: Annotated[
        list[str] | None,
        typer.Option("--question", help="A question to work; repeat it, the Campaign's own first."),
    ] = None,
    questions_file: Annotated[
        Path | None,
        typer.Option(
            "--questions-file",
            help="YAML list of {question, context, brief} items, after any --question.",
        ),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option(
            "--idempotency-key",
            help="Caller's name for this request; a retry returns the first Campaign. "
            "Derived from the request when omitted, so the same request retried is one Campaign.",
        ),
    ] = None,
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
    A question from ``--questions-file`` may carry context and a brief file,
    which the daemon reads once, now, and every round of the question sees.
    """
    asked: list[str | dict[str, Any]] = list(question or [])
    try:
        if questions_file is not None:
            asked += _questions_file(questions_file)
        if not asked:
            raise errors.UserError(
                "state at least one --question or a --questions-file", kind="InvalidInput"
            )
    except errors.CliError as exc:
        errors.emit_error(exc, flags=ctx.obj)
    axes = [
        {"axis_kind": kind, "limit": limit, "unit": kind}
        for kind, limit in (("rounds", budget_rounds), ("tokens", budget_tokens))
        if limit is not None
    ]
    params: dict[str, Any] = {
        "actor": actor,
        "track_ref": track,
        "title": title,
        "questions": asked,
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
    request = hashlib.sha256(orjson.dumps(params, option=orjson.OPT_SORT_KEYS)).hexdigest()
    params["idempotency_key"] = idempotency_key or f"campaign-new-{request[:32]}"
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
    expected_revision: Annotated[
        int,
        typer.Option(
            "--expected-revision", min=1, help="The Campaign revision the drive was decided at."
        ),
    ],
    agents: _Agents = None,
    runtime: _Runtime = "claude-code",
) -> None:
    """Drive an approved Campaign round by round in the daemon.

    Each ready step runs on a Run of its own; each round checkpoints its
    report as an artifact revision, charges the budget and finishes its step.
    The drive runs behind the answer: follow it in the console's Campaign
    screen. A hard budget axis at its limit stops it and says why. A
    Campaign that moved past the expected revision is refused, not driven.
    """
    params: dict[str, Any] = {
        "actor": actor,
        "campaign_key": campaign_key,
        "runtime": runtime,
        "expected_revision": expected_revision,
    }
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
