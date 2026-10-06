"""``eawf release pipeline``: walk a merged phase's checkpoint to BAKED.

Split out of :mod:`eawf.surfaces.cli.commands.release`, whose
:data:`~eawf.surfaces.cli.commands.release.release_app` and daemon
dispatch it reuses. The walk itself is
:func:`~eawf.workflow.release.pipeline.run_post_merge_pipeline`; this
module wires the daemon and the real git and forge adapters into it and
renders how far it got.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.release import _dispatch, release_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.workflow.release.pipeline import StepOutcome

logger = logging.getLogger(__name__)


def _daemon_rpc(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Call one ``release.*`` method, translating a refusal for the pipeline.

    Args:
        method: The fully-qualified method.
        params: Its params.

    Returns:
        The handler's result.

    Raises:
        PipelineRpcError: When the daemon refuses or cannot be reached;
            the message is the daemon's own.
    """
    from eawf.workflow.release.pipeline import PipelineRpcError

    try:
        return _dispatch(method, dict(params))
    except cli_errors.CliError as exc:
        raise PipelineRpcError(str(exc)) from exc


def _outcome_lines(outcomes: tuple[StepOutcome, ...]) -> list[str]:
    """Return one line per step that stands completed."""
    return [
        f"  {outcome.disposition:<8} {outcome.step.value:<15} "
        + " ".join(f"{key}={value}" for key, value in sorted(outcome.facts.items()))
        for outcome in outcomes
    ]


@release_app.command("pipeline")
def release_pipeline(
    ctx: typer.Context,
    version: Annotated[
        str, typer.Argument(help="Checkpoint version the merged phase released, e.g. 0.7.0.dev4.")
    ],
    phase: Annotated[
        str | None,
        typer.Option("--phase", help="The merged phase whose wave pins are checked, e.g. P35."),
    ] = None,
    milestone: Annotated[
        str | None,
        typer.Option(
            "--milestone",
            help="The Milestone whose completed Batches are checked on main, e.g. MLS-0102.",
        ),
    ] = None,
    publish: Annotated[
        bool,
        typer.Option("--publish", help="Push the tag, which publishes to every registry."),
    ] = False,
    membership_ref: Annotated[
        list[str] | None,
        typer.Option(
            "--membership-ref", help="Acceptance bundle the record opens with; repeatable."
        ),
    ] = None,
    remote: Annotated[
        str, typer.Option("--remote", help="Remote main and the tag live on.")
    ] = "origin",
    redo: Annotated[
        str | None,
        typer.Option("--redo", help="Run this step and every later one again, e.g. receipts."),
    ] = None,
) -> None:
    """Take a merged checkpoint from tag to BAKED and the train advance.

    Run it from a clean checkout of the merge on ``main``, naming what
    was released with exactly one of ``--phase`` and ``--milestone``. It
    pins that commit, fetches the dry run's build receipts, sweeps
    readiness, tags and pushes, waits for the publish runs and downloads
    their receipts, then opens, pins, proves, approves, publishes,
    reconciles and observes the record until it bakes, advances the
    train, checks by trailer that ``main`` carries what was released, and
    lands the evidence locally with each file's sha256 journaled, leaving
    the release stores for one ``state:`` commit. With ``--milestone``
    each completed Batch must land as exactly one commit carrying its
    ``Batch:`` trailer, the very commit ``eawf publish`` bound.

    The tag push publishes, so it runs only with ``--publish``; without
    it every earlier step still runs and the verb refuses before the
    push. A tag already on the remote at the pinned commit is adopted.

    Each completed step is journaled under ``.ea/local/release-pipeline``
    and skipped on the next run, so a refused or interrupted walk resumes
    at the step that stopped it. The refusal names the step, a code and
    the next action. ``--redo <step>`` runs a journaled step again, e.g.
    ``--redo receipts`` once gate receipts have expired.
    """
    from eawf.workflow.release.pipeline import (
        PIPELINE_STEPS,
        PipelineOptions,
        PipelineRefusal,
        PipelineStep,
        run_post_merge_pipeline,
    )
    from eawf.workflow.release.pipeline_host import GitHubReleaseHost

    flags: GlobalFlags = ctx.obj
    try:
        redo_step = None if redo is None else PipelineStep(redo)
    except ValueError:
        steps = ", ".join(step.value for step in PIPELINE_STEPS)
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--redo {redo!r} is not a step; have {steps}", kind="InvalidInput"
            ),
            flags=flags,
        )
    state_path, _reason = resolve_with_reason(flags.workspace)
    repo_root = Path.cwd()
    try:
        options = PipelineOptions(
            version=version,
            phase_id=phase,
            milestone_key=milestone,
            publish=publish,
            membership_refs=tuple(membership_ref or ()),
            remote=remote,
        )
        result = run_post_merge_pipeline(
            options,
            host=GitHubReleaseHost(repo_root, state_path=state_path, remote=remote),
            rpc=_daemon_rpc,
            redo=redo_step,
        )
    except PipelineRefusal as refusal:
        done = "\n".join(_outcome_lines(refusal.outcomes)) or "  (no step completed)"
        cli_errors.emit_error(
            cli_errors.StateConflict(
                f"release pipeline refused at {refusal}\n{done}", kind="ReleasePipelineRefused"
            ),
            flags=flags,
            data={
                "step": refusal.step.value,
                "code": refusal.code.value,
                "completed": [outcome.step.value for outcome in refusal.outcomes],
            },
        )
    except ValueError as exc:
        cli_errors.emit_error(cli_errors.ValidationError(str(exc)), flags=flags)
    evidence = next(
        (o.facts.get("evidence_dir") for o in result.outcomes if o.step is PipelineStep.EVIDENCE),
        None,
    )
    payload = {
        "version": result.version,
        "steps": [
            {"step": o.step.value, "disposition": o.disposition, "facts": dict(o.facts)}
            for o in result.outcomes
        ],
        "advance": None if result.advance is None else result.advance.model_dump(mode="json"),
    }
    text = "\n".join(
        [
            f"release pipeline {result.version}: baked, train advanced",
            *_outcome_lines(result.outcomes),
            "next: commit .ea/store/release*.jsonl in one quiet-window `state:` commit; "
            f"the evidence stays local under {evidence}, cited by the digests above",
        ]
    )
    emit_json_or_text(payload, text, flags=flags)


__all__ = ["release_pipeline"]
