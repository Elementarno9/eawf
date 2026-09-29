"""The operator verbs that carry a Task, a Run and a Batch to completion.

:mod:`eawf.surfaces.cli.commands.domain` exposes the moves that plan and
start work. This module exposes the ones that finish it, as one command
per daemon verb:

- ``task claim``, ``task release``, ``task ready`` and ``task complete``
  send ``domain.task.claim``, ``domain.task.release``, ``domain.task.ready``
  and ``domain.task.complete``;
- ``run create``, ``run start``, ``run finish`` and ``run fail`` send
  ``domain.run.create`` and the three Run moves;
- ``batch merge``, ``batch observe-merge`` and ``batch complete`` send the
  three Batch moves after readiness.

The verbs that produce the facts these moves are judged by -- the seal,
the integration or adoption, the proof, the assessment, the read-back --
answer outside the envelope shape and live in
:mod:`eawf.surfaces.cli.commands.domain_integration`.

The commands are dispatch and rendering only, exactly as in the sibling
module: the edge, its guards, the derived integrated binding and the
reconciliation are all decided daemon-side. ``task complete`` names the
commit the caller believes landed and the assessment inputs, never the
binding itself; the daemon derives the binding or refuses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.domain import (
    BATCH_COMPLETE,
    BATCH_MERGE,
    BATCH_OBSERVE_MERGE,
    RUN_CREATE,
    RUN_FAIL,
    RUN_FINISH,
    RUN_START,
    TASK_CLAIM,
    TASK_COMPLETE,
    TASK_READY,
    TASK_RELEASE,
    _run_create_verb,
    _run_verb,
)
from eawf.surfaces.cli.commands.domain_consequence import DryRun, Yes
from eawf.surfaces.cli.commands.lifecycle import batch_app, run_app, task_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.verb_contract import read_spec_document

_URN_HELP: Final = "URN of the record to move."
_REVISION_HELP: Final = "Revision the record was read at (compare-and-swap token)."
_KEY_HELP: Final = "Caller's name for this request; a retry replays its receipt."
_ACTOR_HELP: Final = "Principal key the move is attributed to."
_SPEC_HELP: Final = "JSON file carrying updates, observations, reason_code, binding_refs."
_CREATE_URN_HELP: Final = "URN of the Run to admit."
_TREE_REVISION_HELP: Final = (
    "The tree's committed canonical sequence the caller read; 0 for a tree "
    "nothing has committed to yet."
)
_CREATE_SPEC_HELP: Final = "JSON file carrying the Run's create document (key and scope)."
_CORRELATION_HELP: Final = "Caller's thread of related requests."
_COMMIT_HELP: Final = "Full sha of the commit the Batch head delivers the Task on."
_ASSESSMENT_HELP: Final = (
    "JSON file carrying the completion assessment: base, report_verdict, gates, "
    "receipts and proof_facts."
)

# Each alias spells one repeated option once, so the commands below read
# as the flags they take rather than as four copies of the same metadata.
_Urn = Annotated[str, typer.Argument(help=_URN_HELP)]
_Key = Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)]
_Actor = Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)]
_Spec = Annotated[Path | None, typer.Option("--from-spec", help=_SPEC_HELP)]
_TaskRevision = Annotated[
    int, typer.Option("--expected-revision", "--expected-task-revision", help=_REVISION_HELP)
]
_RunRevision = Annotated[
    int, typer.Option("--expected-revision", "--expected-run-revision", help=_REVISION_HELP)
]
_BatchRevision = Annotated[
    int, typer.Option("--expected-revision", "--expected-batch-revision", help=_REVISION_HELP)
]


def _move(
    ctx: typer.Context,
    method: str,
    urn: str,
    expected_revision: int,
    idempotency_key: str,
    actor: str,
    from_spec: Path | None,
    dry_run: bool,
    yes: bool,
) -> None:
    """Dispatch one plain lifecycle move through the sibling module's body."""
    _run_verb(
        ctx,
        dry_run=dry_run,
        yes=yes,
        method=method,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
    )


# ---- Task -------------------------------------------------------------------


@task_app.command("claim")
def task_claim_cmd(
    ctx: typer.Context,
    urn: _Urn,
    expected_revision: _TaskRevision,
    idempotency_key: _Key,
    actor: _Actor,
    from_spec: _Spec = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Claim a PLANNED Task for the actor that will run it."""
    _move(ctx, TASK_CLAIM, urn, expected_revision, idempotency_key, actor, from_spec, dry_run, yes)


@task_app.command("release")
def task_release_cmd(
    ctx: typer.Context,
    urn: _Urn,
    expected_revision: _TaskRevision,
    idempotency_key: _Key,
    actor: _Actor,
    from_spec: _Spec = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Release a CLAIMED Task's lease, handing it back to PLANNED.

    The payload names the cause in ``reason_code``. Only the principal
    holding the claim releases it, and never while a Run is open on the
    Task; the daemon files the release record naming the cause and actor.
    """
    _move(
        ctx, TASK_RELEASE, urn, expected_revision, idempotency_key, actor, from_spec, dry_run, yes
    )


@task_app.command("ready")
def task_ready_cmd(
    ctx: typer.Context,
    urn: _Urn,
    expected_revision: _TaskRevision,
    idempotency_key: _Key,
    actor: _Actor,
    from_spec: _Spec = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Declare a RUNNING Task ready to integrate on its bound report and evidence.

    The payload presents the ``run_report_bound`` observation and names
    the evidence in ``binding_refs``; a Run report alone does not qualify.
    """
    _move(ctx, TASK_READY, urn, expected_revision, idempotency_key, actor, from_spec, dry_run, yes)


@task_app.command("complete")
def task_complete_cmd(
    ctx: typer.Context,
    urn: _Urn,
    expected_revision: _TaskRevision,
    idempotency_key: _Key,
    actor: _Actor,
    integrated_commit: Annotated[str, typer.Option("--integrated-commit", help=_COMMIT_HELP)],
    assessment: Annotated[Path, typer.Option("--assessment", help=_ASSESSMENT_HELP)],
    from_spec: _Spec = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Complete a Task on the Batch head its passing assessment proves.

    The daemon runs the completion assessment, refuses unless every leg is
    proved and the named commit is the Batch head, and records the binding
    it derives rather than any the caller supplies.
    """
    flags: GlobalFlags = ctx.obj
    try:
        document = read_spec_document(assessment)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _run_verb(
        ctx,
        dry_run=dry_run,
        yes=yes,
        method=TASK_COMPLETE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
        extra_params={"integrated_commit": integrated_commit, "assessment": document},
    )


# ---- Run --------------------------------------------------------------------


@run_app.command("create")
def run_create_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_CREATE_URN_HELP)],
    expected_revision: Annotated[
        int,
        typer.Option("--expected-revision", "--expected-tree-revision", help=_TREE_REVISION_HELP),
    ],
    idempotency_key: _Key,
    actor: _Actor,
    from_spec: Annotated[Path, typer.Option("--from-spec", help=_CREATE_SPEC_HELP)],
    correlation_id: Annotated[
        str | None, typer.Option("--correlation-id", help=_CORRELATION_HELP)
    ] = None,
) -> None:
    """Admit a QUEUED Run against the scope its create document names."""
    _run_create_verb(
        ctx,
        method=RUN_CREATE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        from_spec=from_spec,
        correlation_id=correlation_id,
    )


@run_app.command("start")
def run_start_cmd(
    ctx: typer.Context,
    urn: _Urn,
    expected_revision: _RunRevision,
    idempotency_key: _Key,
    actor: _Actor,
    from_spec: _Spec = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Start a QUEUED Run; the payload's updates carry started_at."""
    _move(ctx, RUN_START, urn, expected_revision, idempotency_key, actor, from_spec, dry_run, yes)


@run_app.command("finish")
def run_finish_cmd(
    ctx: typer.Context,
    urn: _Urn,
    expected_revision: _RunRevision,
    idempotency_key: _Key,
    actor: _Actor,
    from_spec: _Spec = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Complete a RUNNING Run once its report is bound; updates carry ended_at."""
    _move(ctx, RUN_FINISH, urn, expected_revision, idempotency_key, actor, from_spec, dry_run, yes)


@run_app.command("fail")
def run_fail_cmd(
    ctx: typer.Context,
    urn: _Urn,
    expected_revision: _RunRevision,
    idempotency_key: _Key,
    actor: _Actor,
    from_spec: _Spec = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Fail a RUNNING Run; the payload carries the reason, ended_at and failure."""
    _move(ctx, RUN_FAIL, urn, expected_revision, idempotency_key, actor, from_spec, dry_run, yes)


# ---- Batch ------------------------------------------------------------------


@batch_app.command("merge")
def batch_merge_cmd(
    ctx: typer.Context,
    urn: _Urn,
    expected_revision: _BatchRevision,
    idempotency_key: _Key,
    actor: _Actor,
    from_spec: _Spec = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Authorise the merge of a READY_TO_MERGE Batch at the head it pinned."""
    _move(ctx, BATCH_MERGE, urn, expected_revision, idempotency_key, actor, from_spec, dry_run, yes)


@batch_app.command("observe-merge")
def batch_observe_merge_cmd(
    ctx: typer.Context,
    urn: _Urn,
    expected_revision: _BatchRevision,
    idempotency_key: _Key,
    actor: _Actor,
    from_spec: _Spec = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Record that the host landed a MERGING Batch, on a filed landed read-back."""
    _move(
        ctx,
        BATCH_OBSERVE_MERGE,
        urn,
        expected_revision,
        idempotency_key,
        actor,
        from_spec,
        dry_run,
        yes,
    )


@batch_app.command("complete")
def batch_complete_cmd(
    ctx: typer.Context,
    urn: _Urn,
    expected_revision: _BatchRevision,
    idempotency_key: _Key,
    actor: _Actor,
    from_spec: _Spec = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Complete a merged Batch whose landed commit matches the head it pinned."""
    _move(
        ctx, BATCH_COMPLETE, urn, expected_revision, idempotency_key, actor, from_spec, dry_run, yes
    )


__all__ = [
    "batch_complete_cmd",
    "batch_merge_cmd",
    "batch_observe_merge_cmd",
    "run_create_cmd",
    "run_fail_cmd",
    "run_finish_cmd",
    "run_start_cmd",
    "task_claim_cmd",
    "task_complete_cmd",
    "task_ready_cmd",
    "task_release_cmd",
]
