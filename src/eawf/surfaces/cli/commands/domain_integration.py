"""The operator verbs that produce the facts the lifecycle moves are judged by.

A Task completes on a seal or an adoption, a selected generation and
receipts taken at its binding; a Batch completes on a filed read-back of
its target branch; a Milestone is accepted on a sealed approval that cites
recorded evidence. Each of those facts is filed by a daemon verb that
answers with its own typed shape rather than a :class:`DomainEnvelope`,
and raises its refusal as a JSON-RPC error. This module is one command per
such verb:

- ``task submit`` and ``task seal`` send ``runtime.candidate.submit`` and
  ``runtime.candidate.report.bind``;
- ``batch integrate`` asks ``runtime.delivery.assemble`` for the Batch's
  integrate request and sends it to ``runtime.delivery.integrate``;
- ``batch adopt-landed`` sends ``runtime.delivery.adopt_landed`` for work
  that already landed on the target branch outside the native loop;
- ``task prove`` and ``task assess`` send ``runtime.delivery.prove_task``
  and ``runtime.delivery.task_assessment``; ``task assess --out`` writes
  the document ``task complete --assessment`` takes;
- ``batch reconcile`` sends ``runtime.delivery.reconcile_merge`` with a
  presented observation, or ``runtime.delivery.read_back_merge`` to have
  the daemon read the branch back itself;
- ``milestone open-approval`` and ``milestone seal-approval`` send the two
  acceptance-approval verbs; ``--bundle-out`` writes the bundle
  ``milestone accept --acceptance-bundle`` takes;
- ``record evidence`` sends ``runtime.delivery.record_evidence``.

The commands are dispatch and rendering only. A refusal prints the
daemon's own code and detail unchanged and exits
:data:`~eawf.surfaces.cli.commands.domain.DOMAIN_REFUSAL_EXIT`, as does a
proof or assessment that answered but did not pass, so a script can gate
on the exit status.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Final

import orjson
import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.domain import (
    CANDIDATE_SUBMIT,
    DELIVERY_SEAL_APPROVAL,
    DOMAIN_REFUSAL_EXIT,
    _call_native_rpc,
    _check_idempotency_key,
    _load_create_document,
)
from eawf.surfaces.cli.commands.domain_legacy import record_app
from eawf.surfaces.cli.commands.lifecycle import batch_app, milestone_app, task_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

#: The dotted JSON-RPC name each command forwards to, spelled here so the
#: Typer tree builds without the daemon method registry on the path.
CANDIDATE_REPORT_BIND: Final = "runtime.candidate.report.bind"
DELIVERY_ASSEMBLE: Final = "runtime.delivery.assemble"
DELIVERY_INTEGRATE: Final = "runtime.delivery.integrate"
DELIVERY_ADOPT_LANDED: Final = "runtime.delivery.adopt_landed"
DELIVERY_PROVE: Final = "runtime.delivery.prove_task"
DELIVERY_TASK_ASSESSMENT: Final = "runtime.delivery.task_assessment"
DELIVERY_RECONCILE_MERGE: Final = "runtime.delivery.reconcile_merge"
DELIVERY_READ_BACK_MERGE: Final = "runtime.delivery.read_back_merge"
DELIVERY_OPEN_APPROVAL: Final = "runtime.delivery.open_acceptance_approval"
DELIVERY_RECORD_EVIDENCE: Final = "runtime.delivery.record_evidence"

#: Every daemon verb this module sends, in the order the loop uses them.
INTEGRATION_CLI_METHODS: Final[tuple[str, ...]] = (
    CANDIDATE_SUBMIT,
    CANDIDATE_REPORT_BIND,
    DELIVERY_ASSEMBLE,
    DELIVERY_INTEGRATE,
    DELIVERY_ADOPT_LANDED,
    DELIVERY_PROVE,
    DELIVERY_TASK_ASSESSMENT,
    DELIVERY_RECONCILE_MERGE,
    DELIVERY_READ_BACK_MERGE,
    DELIVERY_OPEN_APPROVAL,
    DELIVERY_SEAL_APPROVAL,
    DELIVERY_RECORD_EVIDENCE,
)

_KEY_HELP: Final = "Caller's name for this request; a retry replays its receipt."
_ACTOR_HELP: Final = "Principal key the request is attributed to."
_CORRELATION_HELP: Final = "Caller's thread of related requests."
_APPROVAL_URN_HELP: Final = "URN of the PendingAction acceptance question."
_APPROVAL_REVISION_HELP: Final = "Revision the pending action was read at (compare-and-swap token)."
_RESOLVER_HELP: Final = (
    "Human principal who resolved the question; defaults to --actor, which the "
    "daemon requires it to equal."
)
_OPTION_HELP: Final = "The answer option the resolver chose."
_RECEIPT_HELP: Final = "Evidence URN recording how the answer was reached."
_RUN_URN_HELP: Final = "URN of the Run submitting the work."
_TASK_REF_HELP: Final = "The Task the submitted work was done for."
_SUBMISSION_REF_HELP: Final = "Artifact URN of the commit carrying the work."
_CHANGED_PATH_HELP: Final = "Repository-relative path the work touched (repeatable)."
_TREE_DIGEST_HELP: Final = "Digest of the tree the submission produced."
_CANDIDATE_HELP: Final = "The candidate the accepted report is about."
_VERDICT_HELP: Final = "Report verdict; omit the report fields to use the Run's accepted report."
_REPORT_DIGEST_HELP: Final = "Digest of the accepted report body."
_REPORT_SCHEMA_HELP: Final = "Schema URN the accepted report satisfies."
_TASK_URN_HELP: Final = "URN of the Task."
_BATCH_URN_HELP: Final = "URN of the Batch."
_MILESTONE_URN_HELP: Final = "URN of the Milestone."
_GATES_HELP: Final = (
    'JSON file {"gates": [GateSpec, ...]} naming the gates the criteria reference; '
    "omit to rerun the gates of the Task's earlier proof."
)
_OUT_HELP: Final = "Write the assessment document here, for task complete --assessment."
_REFS_HELP: Final = "JSON file carrying base, exit_refs and diagnostic_ref."
_HEAD_HELP: Final = "Full sha of the landed commit to adopt."
_BASE_HELP: Final = "Full sha of the commit the landed change started from."
_ADOPT_TASK_HELP: Final = "URN of a Task the landed change carries (repeatable)."
_ADOPT_VERDICT_HELP: Final = "The verdict the adopted Runs' reports carried."
_EVIDENCE_REF_HELP: Final = (
    "Recorded audit, decision, artifact or EVD id the verdict rests on (repeatable)."
)
_AFFECTED_HELP: Final = "Criterion the change invalidates (repeatable); default every criterion."
_OBSERVATION_HELP: Final = (
    "JSON file carrying a host merge observation; omit to have the daemon read the "
    "target branch back from the repository."
)
_APPROVAL_SPEC_HELP: Final = "JSON file carrying requested_by, steps and accepted_binding."
_BUNDLE_OUT_HELP: Final = "Write the acceptance bundle here, for milestone accept."
_EVIDENCE_URN_HELP: Final = "URN of the record the evidence is about."
_KIND_HELP: Final = "Evidence kind: audit, artifact, decision, store_record or external_url."
_SUMMARY_HELP: Final = "What the evidence shows, naming the ids it points at."

_Key = Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)]
_Actor = Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)]


def _send(
    ctx: typer.Context,
    method: str,
    params: dict[str, Any],
    *,
    verb_text: str,
    key: str | None,
    gated: bool = False,
) -> dict[str, Any] | None:
    """Send one native RPC and return its answer, or report the failure.

    Args:
        ctx: Typer context carrying the resolved global flags.
        method: The dotted JSON-RPC name.
        params: The wire parameters, less ``repo_root``.
        verb_text: The command spelling an operator typed.
        key: The retry key to bound-check, or ``None`` for a read.
        gated: Whether the verb runs gates or git work inside the request.

    Returns:
        The answer, or ``None`` after the error was emitted.
    """
    flags: GlobalFlags = ctx.obj
    try:
        if key is not None:
            _check_idempotency_key(key)
        return _call_native_rpc(method, params, flags=flags, verb_text=verb_text, gated=gated)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return None


def _document(ctx: typer.Context, path: Path) -> dict[str, Any] | None:
    """Return the JSON object at *path*, or report why it cannot be read."""
    try:
        return _load_create_document(path)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=ctx.obj)
        return None


def _emit(
    ctx: typer.Context,
    answer: dict[str, Any],
    render: Callable[[dict[str, Any]], str],
    *,
    passed: bool = True,
) -> None:
    """Print *answer* and exit non-zero when it did not pass."""
    emit_json_or_text(answer, render(answer), flags=ctx.obj)
    if not passed:
        raise typer.Exit(DOMAIN_REFUSAL_EXIT)


def _write(path: Path, payload: dict[str, Any]) -> None:
    """Write *payload* to *path* as indented JSON."""
    path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))


# ---- Task -------------------------------------------------------------------


@task_app.command("submit")
def task_submit_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_RUN_URN_HELP)],
    task_ref: Annotated[str, typer.Option("--task-ref", help=_TASK_REF_HELP)],
    submission_ref: Annotated[str, typer.Option("--submission-ref", help=_SUBMISSION_REF_HELP)],
    changed_path: Annotated[list[str], typer.Option("--changed-path", help=_CHANGED_PATH_HELP)],
    resulting_tree_digest: Annotated[
        str, typer.Option("--resulting-tree-digest", help=_TREE_DIGEST_HELP)
    ],
    idempotency_key: _Key,
    actor: _Actor,
) -> None:
    """File one Run's claim that its leased workspace is ready to integrate.

    Nothing is checked against a report here and nothing is sealed: the
    claim is recorded as made, exactly as the daemon's own verb promises.
    """
    params: dict[str, Any] = {
        "urn": urn,
        "actor": actor,
        "idempotency_key": idempotency_key,
        "task_ref": task_ref,
        "submission_ref": submission_ref,
        "changed_paths": list(changed_path),
        "resulting_tree_digest": resulting_tree_digest,
    }
    answer = _send(ctx, CANDIDATE_SUBMIT, params, verb_text="task submit", key=idempotency_key)
    if answer is not None:
        _emit(
            ctx,
            answer,
            lambda a: (
                f"{CANDIDATE_SUBMIT} ok {a['candidate_ref']} run {a['run_ref']} "
                f"replayed={a['replayed']}\n  {a['reason']}"
            ),
        )


@task_app.command("seal")
def task_seal_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_RUN_URN_HELP)],
    candidate_ref: Annotated[str, typer.Option("--candidate-ref", help=_CANDIDATE_HELP)],
    resulting_tree_digest: Annotated[
        str, typer.Option("--resulting-tree-digest", help=_TREE_DIGEST_HELP)
    ],
    idempotency_key: _Key,
    actor: _Actor,
    verdict: Annotated[str | None, typer.Option("--verdict", help=_VERDICT_HELP)] = None,
    report_digest: Annotated[
        str | None, typer.Option("--report-digest", help=_REPORT_DIGEST_HELP)
    ] = None,
    report_schema_ref: Annotated[
        str | None, typer.Option("--report-schema-ref", help=_REPORT_SCHEMA_HELP)
    ] = None,
) -> None:
    """Bind a Run's accepted report to its candidate and attempt the seal."""
    params: dict[str, Any] = {
        "urn": urn,
        "actor": actor,
        "idempotency_key": idempotency_key,
        "candidate_ref": candidate_ref,
        "resulting_tree_digest": resulting_tree_digest,
        "verdict": verdict,
        "report_digest": report_digest,
        "report_schema_ref": report_schema_ref,
    }
    answer = _send(ctx, CANDIDATE_REPORT_BIND, params, verb_text="task seal", key=idempotency_key)
    if answer is not None:
        failed = ", ".join(answer.get("failed_checks") or ()) or "none"
        _emit(
            ctx,
            answer,
            lambda a: (
                f"{CANDIDATE_REPORT_BIND} sealed={a['sealed']} {a['candidate_ref']} "
                f"failed={failed}\n  {a['reason']}"
            ),
            passed=bool(answer["sealed"]),
        )


@task_app.command("prove")
def task_prove_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_TASK_URN_HELP)],
    idempotency_key: _Key,
    actor: _Actor,
    gates: Annotated[Path | None, typer.Option("--gates", help=_GATES_HELP)] = None,
) -> None:
    """Run a Task's gates at the generation each leg binds and file the receipts."""
    params: dict[str, Any] = {"urn": urn, "actor": actor, "idempotency_key": idempotency_key}
    if gates is not None:
        document = _document(ctx, gates)
        if document is None:
            return
        params["gates"] = document.get("gates", [])
    answer = _send(
        ctx, DELIVERY_PROVE, params, verb_text="task prove", key=idempotency_key, gated=True
    )
    if answer is not None:
        _emit(
            ctx,
            answer,
            lambda a: "\n".join(
                [f"{DELIVERY_PROVE} passed={a['passed']} {a['task_ref']}"]
                + [
                    f"  {leg['gate_id']} {leg['result']} {leg['receipt_id']} at {leg['head_sha']}"
                    for leg in a["legs"]
                ]
                + [f"  {a['reason']}"]
            ),
            passed=bool(answer["passed"]),
        )


@task_app.command("assess")
def task_assess_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_TASK_URN_HELP)],
    actor: _Actor,
    out: Annotated[Path | None, typer.Option("--out", help=_OUT_HELP)] = None,
) -> None:
    """Judge a Task for completion and print the document task complete needs."""
    answer = _send(
        ctx,
        DELIVERY_TASK_ASSESSMENT,
        {"urn": urn, "actor": actor},
        verb_text="task assess",
        key=None,
    )
    if answer is None:
        return
    if out is not None:
        _write(out, answer["assessment"])
    judged = answer["answer"]
    _emit(
        ctx,
        answer,
        lambda a: (
            f"{DELIVERY_TASK_ASSESSMENT} completable={judged['completable']} "
            f"{judged['task_ref']} integrated_commit={a['integrated_commit']}\n"
            f"  rerun: {', '.join(judged['rerun_gate_ids']) or 'none'}\n"
            f"  {judged['reason']}"
        ),
        passed=bool(judged["completable"]),
    )


# ---- Batch ------------------------------------------------------------------


@batch_app.command("integrate")
def batch_integrate_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_BATCH_URN_HELP)],
    actor: _Actor,
    from_spec: Annotated[Path, typer.Option("--from-spec", help=_REFS_HELP)],
) -> None:
    """Integrate a Batch's sealed candidates into its next generation.

    The daemon assembles the integrate request from the Batch's plan and
    the references the file names, then runs it; assembling the same plan
    again names the same idempotency key, so a retry replays.
    """
    refs = _document(ctx, from_spec)
    if refs is None:
        return
    request = _send(
        ctx,
        DELIVERY_ASSEMBLE,
        {"urn": urn, "actor": actor, **refs},
        verb_text="batch integrate",
        key=None,
    )
    if request is None:
        return
    answer = _send(
        ctx, DELIVERY_INTEGRATE, request, verb_text="batch integrate", key=None, gated=True
    )
    if answer is not None:
        _emit(
            ctx,
            answer,
            lambda a: (
                f"{DELIVERY_INTEGRATE} delivered={a['delivered']} {a['batch_ref']} "
                f"generations={', '.join(a['generation_ids']) or 'none'}\n  {a['reason']}"
            ),
            passed=bool(answer["delivered"]),
        )


@batch_app.command("adopt-landed")
def batch_adopt_landed_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_BATCH_URN_HELP)],
    head: Annotated[str, typer.Option("--head", help=_HEAD_HELP)],
    base: Annotated[str, typer.Option("--base", help=_BASE_HELP)],
    task: Annotated[list[str], typer.Option("--task", help=_ADOPT_TASK_HELP)],
    evidence: Annotated[list[str], typer.Option("--evidence", help=_EVIDENCE_REF_HELP)],
    idempotency_key: _Key,
    actor: _Actor,
    verdict: Annotated[str, typer.Option("--verdict", help=_ADOPT_VERDICT_HELP)] = "pass",
    affected: Annotated[
        list[str] | None, typer.Option("--affected-criterion", help=_AFFECTED_HELP)
    ] = None,
) -> None:
    """Adopt work that already landed on the Batch's target branch.

    The daemon reads the commits, their containment in the target branch
    and the changed paths back from the repository, resolves every
    evidence reference, and files the adoption and the generation it
    selects. The Tasks still complete only on receipts taken at that head.
    """
    params: dict[str, Any] = {
        "urn": urn,
        "actor": actor,
        "idempotency_key": idempotency_key,
        "task_refs": list(task),
        "base_commit": base,
        "head_sha": head,
        "report_verdict": verdict,
        "evidence_refs": list(evidence),
        "affected_criterion_ids": list(affected or ()),
    }
    answer = _send(
        ctx, DELIVERY_ADOPT_LANDED, params, verb_text="batch adopt-landed", key=idempotency_key
    )
    if answer is not None:
        _emit(
            ctx,
            answer,
            lambda a: (
                f"{DELIVERY_ADOPT_LANDED} ok {a['batch_ref']} {a['generation_id']} "
                f"head={a['head_sha']} replayed={a['replayed']}\n  {a['reason']}"
            ),
        )


@batch_app.command("reconcile")
def batch_reconcile_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_BATCH_URN_HELP)],
    idempotency_key: _Key,
    actor: _Actor,
    observation: Annotated[
        Path | None, typer.Option("--observation", help=_OBSERVATION_HELP)
    ] = None,
) -> None:
    """File what a read-back of a MERGING Batch's target branch found.

    The outcome is decided by the Batch's own pinned head being among the
    commits read back, not by the observation saying so. Without
    ``--observation`` the daemon reads the branch from the repository.
    """
    params: dict[str, Any] = {"urn": urn, "actor": actor, "idempotency_key": idempotency_key}
    method = DELIVERY_READ_BACK_MERGE
    if observation is not None:
        document = _document(ctx, observation)
        if document is None:
            return
        params["observation"] = document
        method = DELIVERY_RECONCILE_MERGE
    answer = _send(ctx, method, params, verb_text="batch reconcile", key=idempotency_key)
    if answer is not None:
        _emit(
            ctx,
            answer,
            lambda a: (
                f"{method} ok {a['batch_ref']} outcome {a['outcome']} "
                f"record {a['record_key']}\n  {a['reason']}"
            ),
        )


# ---- Milestone --------------------------------------------------------------


@milestone_app.command("open-approval")
def milestone_open_approval_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_MILESTONE_URN_HELP)],
    actor: _Actor,
    from_spec: Annotated[Path, typer.Option("--from-spec", help=_APPROVAL_SPEC_HELP)],
    bundle_out: Annotated[Path | None, typer.Option("--bundle-out", help=_BUNDLE_OUT_HELP)] = None,
) -> None:
    """Ask the operator to accept a Milestone in review on its acceptance bundle."""
    spec = _document(ctx, from_spec)
    if spec is None:
        return
    answer = _send(
        ctx,
        DELIVERY_OPEN_APPROVAL,
        {"urn": urn, "actor": actor, **spec},
        verb_text="milestone open-approval",
        key=None,
    )
    if answer is None:
        return
    if bundle_out is not None and answer.get("acceptance_bundle") is not None:
        _write(bundle_out, answer["acceptance_bundle"])
    _emit(
        ctx,
        answer,
        lambda a: (
            f"{DELIVERY_OPEN_APPROVAL} ok {a['action_ref']} status {a['status']} "
            f"revision {a['revision']}\n  {a['reason']}"
        ),
    )


@milestone_app.command("seal-approval")
def milestone_seal_approval_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_APPROVAL_URN_HELP)],
    expected_revision: Annotated[
        int, typer.Option("--expected-approval-revision", help=_APPROVAL_REVISION_HELP)
    ],
    idempotency_key: _Key,
    actor: _Actor,
    option_id: Annotated[str, typer.Option("--option-id", help=_OPTION_HELP)],
    receipt_ref: Annotated[str, typer.Option("--receipt-ref", help=_RECEIPT_HELP)],
    resolver: Annotated[str | None, typer.Option("--resolver", help=_RESOLVER_HELP)] = None,
    correlation_id: Annotated[
        str | None, typer.Option("--correlation-id", help=_CORRELATION_HELP)
    ] = None,
) -> None:
    """Seal the operator's answer onto a waiting acceptance question.

    ``domain.milestone.accept`` then requires the sealed question by
    reference. The resolver defaults to ``--actor``, which the daemon
    requires it to equal.
    """
    if expected_revision <= 0:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--expected-approval-revision must be a positive revision, "
                f"got {expected_revision}",
                kind="InvalidInput",
            ),
            flags=ctx.obj,
        )
        return
    params: dict[str, Any] = {
        "urn": urn,
        "expected_revision": expected_revision,
        "idempotency_key": idempotency_key,
        "actor": actor,
        "resolver": {
            "principal_kind": "human",
            "principal_id": resolver if resolver is not None else actor,
        },
        "option_id": option_id,
        "receipt_ref": receipt_ref,
        "correlation_id": correlation_id,
    }
    answer = _send(
        ctx,
        DELIVERY_SEAL_APPROVAL,
        params,
        verb_text="milestone seal-approval",
        key=idempotency_key,
    )
    if answer is not None:
        _emit(
            ctx,
            answer,
            lambda a: (
                f"{DELIVERY_SEAL_APPROVAL} ok {a['action_ref']} "
                f"status {a['status']} revision {a['revision']}\n  {a['reason']}"
            ),
        )


# ---- Evidence ---------------------------------------------------------------


@record_app.command("evidence")
def record_evidence_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_EVIDENCE_URN_HELP)],
    kind: Annotated[str, typer.Option("--kind", help=_KIND_HELP)],
    summary: Annotated[str, typer.Option("--summary", help=_SUMMARY_HELP)],
    idempotency_key: _Key,
    actor: _Actor,
) -> None:
    """File one evidence row an acceptance step or answer may cite."""
    params: dict[str, Any] = {
        "urn": urn,
        "actor": actor,
        "idempotency_key": idempotency_key,
        "kind": kind,
        "summary": summary,
    }
    answer = _send(
        ctx, DELIVERY_RECORD_EVIDENCE, params, verb_text="record evidence", key=idempotency_key
    )
    if answer is not None:
        _emit(
            ctx,
            answer,
            lambda a: f"{DELIVERY_RECORD_EVIDENCE} ok {a['evidence_ref']} created={a['created']}",
        )


__all__ = [
    "INTEGRATION_CLI_METHODS",
    "batch_adopt_landed_cmd",
    "batch_integrate_cmd",
    "batch_reconcile_cmd",
    "milestone_open_approval_cmd",
    "milestone_seal_approval_cmd",
    "record_evidence_cmd",
    "task_assess_cmd",
    "task_prove_cmd",
    "task_seal_cmd",
    "task_submit_cmd",
]
