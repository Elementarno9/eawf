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
  integrate request and submits it to ``runtime.delivery.integrate``;
- ``batch adopt-landed`` sends ``runtime.delivery.adopt_landed`` for work
  that already landed on the target branch outside the native loop;
- ``batch label`` sends ``runtime.delivery.label_audit``, the ground truth a
  principal pins on one criterion of the Batch for the jury's calibration;
- ``task prove`` submits ``runtime.delivery.prove_task`` and ``task
  assess`` sends ``runtime.delivery.task_assessment``; ``task assess
  --out`` writes the document ``task complete --assessment`` takes;
- ``batch reconcile`` sends ``runtime.delivery.reconcile_merge`` with a
  presented observation, or ``runtime.delivery.read_back_merge`` to have
  the daemon read the branch back itself;
- ``milestone open-approval`` and ``milestone seal-approval`` send the two
  acceptance-approval verbs; ``--bundle-out`` writes the bundle
  ``milestone accept --acceptance-bundle`` takes;
- ``record evidence`` sends ``runtime.delivery.record_evidence``.

``task prove`` and ``batch integrate`` run gates and git work, so they are
submitted through ``operation.submit`` rather than awaited: exit zero means
the work was accepted, and the envelope links the ``eawf follow`` command
that streams it. ``--wait`` blocks on the same submission until the work is
terminal and then prints exactly what the direct call would have.

The commands are dispatch and rendering only. Every mutating command
names the revision the caller read its subject at (``--expected-revision``)
and the daemon refuses a stale one with ``revision_conflict``. Each answer
is wrapped into the one machine envelope and printed through
:func:`~eawf.surfaces.cli.verb_contract.envelope_text`: a refusal carries the
daemon's own code and detail unchanged, and a proof, seal, integration or
assessment that answered but did not pass is an ``error`` envelope naming
the field that did not hold, so both exit with the typed status
:func:`~eawf.surfaces.cli.verb_contract.envelope_exit_code` gives them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Final

import orjson
import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.commands.domain import (
    CANDIDATE_SUBMIT,
    DELIVERY_SEAL_APPROVAL,
    _check_idempotency_key,
    _native_answer,
)
from eawf.surfaces.cli.commands.domain_legacy import record_app
from eawf.surfaces.cli.commands.lifecycle import batch_app, milestone_app, task_app
from eawf.surfaces.cli.commands.operation import OPERATION_SUBMIT, operation_answer
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.verb_contract import (
    answer_envelope,
    emit_envelope,
    read_spec_document,
    refusal_envelope,
)

#: The dotted JSON-RPC name each command forwards to, spelled here so the
#: Typer tree builds without the daemon method registry on the path.
CANDIDATE_REPORT_BIND: Final = "runtime.candidate.report.bind"
DELIVERY_ASSEMBLE: Final = "runtime.delivery.assemble"
DELIVERY_INTEGRATE: Final = "runtime.delivery.integrate"
DELIVERY_ADOPT_LANDED: Final = "runtime.delivery.adopt_landed"
DELIVERY_LABEL_AUDIT: Final = "runtime.delivery.label_audit"
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
    DELIVERY_LABEL_AUDIT,
    DELIVERY_PROVE,
    DELIVERY_TASK_ASSESSMENT,
    DELIVERY_RECONCILE_MERGE,
    DELIVERY_READ_BACK_MERGE,
    DELIVERY_OPEN_APPROVAL,
    DELIVERY_SEAL_APPROVAL,
    DELIVERY_RECORD_EVIDENCE,
)

_KEY_HELP: Final = "Caller's name for this request; a retry replays its receipt."
_REVISION_HELP: Final = "Revision the subject was read at (compare-and-swap token)."
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
_CRITERION_HELP: Final = "The criterion of the Batch the label is about."
_GOOD_HELP: Final = "The subject was actually good as the Batch delivered it."
_BAD_HELP: Final = "The subject was actually bad as the Batch delivered it."
_NOTE_HELP: Final = "Why the label is pinned (at least 20 characters)."
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
_Revision = Annotated[int, typer.Option("--expected-revision", help=_REVISION_HELP)]
_RunRevision = Annotated[
    int, typer.Option("--expected-revision", "--expected-run-revision", help=_REVISION_HELP)
]
_TaskRevision = Annotated[
    int, typer.Option("--expected-revision", "--expected-task-revision", help=_REVISION_HELP)
]
_BatchRevision = Annotated[
    int, typer.Option("--expected-revision", "--expected-batch-revision", help=_REVISION_HELP)
]
_MilestoneRevision = Annotated[
    int, typer.Option("--expected-revision", "--expected-milestone-revision", help=_REVISION_HELP)
]
_Actor = Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)]
_Wait = Annotated[
    bool,
    typer.Option("--wait", help="Block until the submitted work is terminal and print its answer."),
]


def _send(
    ctx: typer.Context,
    method: str,
    params: dict[str, Any],
    *,
    urn: str,
    verb_text: str,
    key: str | None,
    read: bool = False,
    wait: bool | None = None,
) -> dict[str, Any] | None:
    """Send one native RPC and return its answer, or print why there is none.

    Args:
        ctx: Typer context carrying the resolved global flags.
        method: The dotted JSON-RPC name.
        params: The wire parameters, less ``repo_root``.
        urn: The subject the request addresses, named by a refusal.
        verb_text: The command spelling an operator typed.
        key: The retry key to bound-check, or ``None`` for a request that
            carries none.
        read: Whether the verb only reads, so it never starts a daemon.
        wait: ``None`` to call *method* directly; otherwise submit it as an
            operation under *key*, blocking until it is terminal when true,
            with the wire held as long as a gated mutation may run.

    Returns:
        The answer, or ``None`` after a submission, a refusal envelope or an
        error was printed (a refusal and an error exit, so ``None`` after
        them is reached only under test doubles).
    """
    flags: GlobalFlags = ctx.obj
    try:
        if key is not None:
            _check_idempotency_key(key)
        if wait is None:
            return _native_answer(method, params, flags=flags, verb_text=verb_text, read=read)
        submitted = _native_answer(
            OPERATION_SUBMIT,
            {"method": method, "params": params, "idempotency_key": key, "wait": wait},
            flags=flags,
            verb_text=verb_text,
            gated=wait,
        )
        if wait:
            return operation_answer(submitted["operation"])
        emit_envelope(
            answer_envelope(
                submitted,
                operation=method,
                urn=urn,
                revision_before=params.get("expected_revision"),
                revision_after=None,
                links={"follow": f"eawf follow {submitted['operation_ref']}"},
            ),
            urn=urn,
            flags=flags,
        )
        return None
    except DaemonRpcError as exc:
        if exc.code != cli_errors.RPC_VALIDATION_FAILED:
            cli_errors.emit_error(cli_errors.cli_error_for_rpc(exc.code, exc.message), flags=flags)
            return None
        emit_envelope(
            refusal_envelope(exc.message, operation=method, urn=urn), urn=urn, flags=flags
        )
        return None
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return None


def _document(ctx: typer.Context, path: Path) -> dict[str, Any] | None:
    """Return the JSON object at *path*, or report why it cannot be read."""
    try:
        return read_spec_document(path)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=ctx.obj)
        return None


def _answer(
    ctx: typer.Context,
    answer: dict[str, Any],
    *,
    operation: str,
    urn: str,
    revision: int | None,
    failed_guard: str | None = None,
) -> None:
    """Print *answer* as its envelope and exit with the envelope's status.

    Args:
        ctx: Typer context carrying the resolved global flags.
        answer: The daemon's answer.
        operation: The dotted JSON-RPC name that earned it.
        urn: The subject the request addressed.
        revision: The anchor the request was sent under, or ``None`` for a read.
        failed_guard: The answer field that did not hold, or ``None``.
    """
    envelope = answer_envelope(
        answer,
        operation=operation,
        urn=urn,
        revision_before=revision,
        revision_after=None,
        failed_guard=failed_guard,
    )
    emit_envelope(envelope, urn=urn, flags=ctx.obj)


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
    expected_revision: _RunRevision,
    idempotency_key: _Key,
    actor: _Actor,
) -> None:
    """File one Run's claim that its leased workspace is ready to integrate.

    Nothing is checked against a report here and nothing is sealed: the
    claim is recorded as made, exactly as the daemon's own verb promises.
    """
    params: dict[str, Any] = {
        "urn": urn,
        "expected_revision": expected_revision,
        "actor": actor,
        "idempotency_key": idempotency_key,
        "task_ref": task_ref,
        "submission_ref": submission_ref,
        "changed_paths": list(changed_path),
        "resulting_tree_digest": resulting_tree_digest,
    }
    answer = _send(
        ctx, CANDIDATE_SUBMIT, params, urn=urn, verb_text="task submit", key=idempotency_key
    )
    if answer is not None:
        _answer(ctx, answer, operation=CANDIDATE_SUBMIT, urn=urn, revision=expected_revision)


@task_app.command("seal")
def task_seal_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_RUN_URN_HELP)],
    candidate_ref: Annotated[str, typer.Option("--candidate-ref", help=_CANDIDATE_HELP)],
    resulting_tree_digest: Annotated[
        str, typer.Option("--resulting-tree-digest", help=_TREE_DIGEST_HELP)
    ],
    expected_revision: _RunRevision,
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
        "expected_revision": expected_revision,
        "actor": actor,
        "idempotency_key": idempotency_key,
        "candidate_ref": candidate_ref,
        "resulting_tree_digest": resulting_tree_digest,
        "verdict": verdict,
        "report_digest": report_digest,
        "report_schema_ref": report_schema_ref,
    }
    answer = _send(
        ctx, CANDIDATE_REPORT_BIND, params, urn=urn, verb_text="task seal", key=idempotency_key
    )
    if answer is not None:
        _answer(
            ctx,
            answer,
            operation=CANDIDATE_REPORT_BIND,
            urn=urn,
            revision=expected_revision,
            failed_guard=None if answer["sealed"] else "sealed",
        )


@task_app.command("prove")
def task_prove_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_TASK_URN_HELP)],
    expected_revision: _TaskRevision,
    idempotency_key: _Key,
    actor: _Actor,
    gates: Annotated[Path | None, typer.Option("--gates", help=_GATES_HELP)] = None,
    wait: _Wait = False,
) -> None:
    """Run a Task's gates at the generation each leg binds and file the receipts.

    The proof is submitted as an operation and its reference printed at once;
    ``--wait`` blocks until it is terminal and prints the proof instead.
    """
    params: dict[str, Any] = {
        "urn": urn,
        "expected_revision": expected_revision,
        "actor": actor,
        "idempotency_key": idempotency_key,
    }
    if gates is not None:
        document = _document(ctx, gates)
        if document is None:
            return
        params["gates"] = document.get("gates", [])
    answer = _send(
        ctx,
        DELIVERY_PROVE,
        params,
        urn=urn,
        verb_text="task prove",
        key=idempotency_key,
        wait=wait,
    )
    if answer is not None:
        _answer(
            ctx,
            answer,
            operation=DELIVERY_PROVE,
            urn=urn,
            revision=expected_revision,
            failed_guard=None if answer["passed"] else "passed",
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
        urn=urn,
        verb_text="task assess",
        key=None,
        read=True,
    )
    if answer is None:
        return
    if out is not None:
        _write(out, answer["assessment"])
    _answer(
        ctx,
        answer,
        operation=DELIVERY_TASK_ASSESSMENT,
        urn=urn,
        revision=None,
        failed_guard=None if answer["answer"]["completable"] else "completable",
    )


# ---- Batch ------------------------------------------------------------------


@batch_app.command("integrate")
def batch_integrate_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_BATCH_URN_HELP)],
    expected_revision: _BatchRevision,
    actor: _Actor,
    from_spec: Annotated[Path, typer.Option("--from-spec", help=_REFS_HELP)],
    wait: _Wait = False,
) -> None:
    """Integrate a Batch's sealed candidates into its next generation.

    The daemon assembles the integrate request from the Batch's plan and
    the references the file names, then runs it under the anchor as a
    submitted operation; assembling the same plan again names the same
    idempotency key, so a retry replays. ``--wait`` blocks until the
    integration is terminal and prints it.
    """
    refs = _document(ctx, from_spec)
    if refs is None:
        return
    request = _send(
        ctx,
        DELIVERY_ASSEMBLE,
        {"urn": urn, "actor": actor, **refs},
        urn=urn,
        verb_text="batch integrate",
        key=None,
    )
    if request is None:
        return
    answer = _send(
        ctx,
        DELIVERY_INTEGRATE,
        {**request, "expected_revision": expected_revision},
        urn=urn,
        verb_text="batch integrate",
        key=request["idempotency_key"],
        wait=wait,
    )
    if answer is not None:
        _answer(
            ctx,
            answer,
            operation=DELIVERY_INTEGRATE,
            urn=urn,
            revision=expected_revision,
            failed_guard=None if answer["delivered"] else "delivered",
        )


@batch_app.command("adopt-landed")
def batch_adopt_landed_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_BATCH_URN_HELP)],
    head: Annotated[str, typer.Option("--head", help=_HEAD_HELP)],
    base: Annotated[str, typer.Option("--base", help=_BASE_HELP)],
    task: Annotated[list[str], typer.Option("--task", help=_ADOPT_TASK_HELP)],
    evidence: Annotated[list[str], typer.Option("--evidence", help=_EVIDENCE_REF_HELP)],
    expected_revision: _BatchRevision,
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
        "expected_revision": expected_revision,
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
        ctx,
        DELIVERY_ADOPT_LANDED,
        params,
        urn=urn,
        verb_text="batch adopt-landed",
        key=idempotency_key,
    )
    if answer is not None:
        _answer(ctx, answer, operation=DELIVERY_ADOPT_LANDED, urn=urn, revision=expected_revision)


@batch_app.command("label")
def batch_label_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_BATCH_URN_HELP)],
    criterion: Annotated[str, typer.Option("--criterion", help=_CRITERION_HELP)],
    note: Annotated[str, typer.Option("--note", help=_NOTE_HELP)],
    expected_revision: _BatchRevision,
    idempotency_key: _Key,
    actor: _Actor,
    good: Annotated[bool, typer.Option("--good", help=_GOOD_HELP)] = False,
    bad: Annotated[bool, typer.Option("--bad", help=_BAD_HELP)] = False,
) -> None:
    """Pin the ground truth of one Batch criterion for the jury's calibration.

    Exactly one of ``--good`` and ``--bad`` is required. Labels are
    append-only: a relabelled subject gets a fresh label and the newest
    wins, so a correction supersedes a mistake without rewriting it.
    """
    if good == bad:
        cli_errors.emit_error(
            cli_errors.UserError("pass exactly one of --good / --bad", kind="InvalidInput"),
            flags=ctx.obj,
        )
        return
    params: dict[str, Any] = {
        "urn": urn,
        "expected_revision": expected_revision,
        "actor": actor,
        "idempotency_key": idempotency_key,
        "criterion_id": criterion,
        "ground_truth": good,
        "note": note,
    }
    answer = _send(
        ctx, DELIVERY_LABEL_AUDIT, params, urn=urn, verb_text="batch label", key=idempotency_key
    )
    if answer is not None:
        _answer(ctx, answer, operation=DELIVERY_LABEL_AUDIT, urn=urn, revision=expected_revision)


@batch_app.command("reconcile")
def batch_reconcile_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_BATCH_URN_HELP)],
    expected_revision: _BatchRevision,
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
    params: dict[str, Any] = {
        "urn": urn,
        "expected_revision": expected_revision,
        "actor": actor,
        "idempotency_key": idempotency_key,
    }
    method = DELIVERY_READ_BACK_MERGE
    if observation is not None:
        document = _document(ctx, observation)
        if document is None:
            return
        params["observation"] = document
        method = DELIVERY_RECONCILE_MERGE
    answer = _send(ctx, method, params, urn=urn, verb_text="batch reconcile", key=idempotency_key)
    if answer is not None:
        _answer(ctx, answer, operation=method, urn=urn, revision=expected_revision)


# ---- Milestone --------------------------------------------------------------


@milestone_app.command("open-approval")
def milestone_open_approval_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_MILESTONE_URN_HELP)],
    expected_revision: _MilestoneRevision,
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
        {"urn": urn, "expected_revision": expected_revision, "actor": actor, **spec},
        urn=urn,
        verb_text="milestone open-approval",
        key=None,
    )
    if answer is None:
        return
    if bundle_out is not None and answer.get("acceptance_bundle") is not None:
        _write(bundle_out, answer["acceptance_bundle"])
    _answer(ctx, answer, operation=DELIVERY_OPEN_APPROVAL, urn=urn, revision=expected_revision)


@milestone_app.command("seal-approval")
def milestone_seal_approval_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_APPROVAL_URN_HELP)],
    expected_revision: Annotated[
        int,
        typer.Option(
            "--expected-revision", "--expected-approval-revision", help=_APPROVAL_REVISION_HELP
        ),
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
                f"--expected-revision must be a positive revision, got {expected_revision}",
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
        urn=urn,
        verb_text="milestone seal-approval",
        key=idempotency_key,
    )
    if answer is not None:
        _answer(ctx, answer, operation=DELIVERY_SEAL_APPROVAL, urn=urn, revision=expected_revision)


# ---- Evidence ---------------------------------------------------------------


@record_app.command("evidence")
def record_evidence_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_EVIDENCE_URN_HELP)],
    kind: Annotated[str, typer.Option("--kind", help=_KIND_HELP)],
    summary: Annotated[str, typer.Option("--summary", help=_SUMMARY_HELP)],
    expected_revision: _Revision,
    idempotency_key: _Key,
    actor: _Actor,
) -> None:
    """File one evidence row an acceptance step or answer may cite."""
    params: dict[str, Any] = {
        "urn": urn,
        "expected_revision": expected_revision,
        "actor": actor,
        "idempotency_key": idempotency_key,
        "kind": kind,
        "summary": summary,
    }
    answer = _send(
        ctx,
        DELIVERY_RECORD_EVIDENCE,
        params,
        urn=urn,
        verb_text="record evidence",
        key=idempotency_key,
    )
    if answer is not None:
        _answer(
            ctx, answer, operation=DELIVERY_RECORD_EVIDENCE, urn=urn, revision=expected_revision
        )


__all__ = [
    "INTEGRATION_CLI_METHODS",
    "batch_adopt_landed_cmd",
    "batch_integrate_cmd",
    "batch_label_cmd",
    "batch_reconcile_cmd",
    "milestone_open_approval_cmd",
    "milestone_seal_approval_cmd",
    "record_evidence_cmd",
    "task_assess_cmd",
    "task_prove_cmd",
    "task_seal_cmd",
    "task_submit_cmd",
]
