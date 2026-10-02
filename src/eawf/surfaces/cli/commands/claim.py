"""The operator verbs that file a claim and work its evidence ladder.

- ``claim file`` sends ``runtime.evidence.claim.file``: it files a claim about a Task,
  Batch or Milestone under the next ``CLM-####`` key, citing evidence and receipt keys and
  anchoring spans given as ``[EVD-####:]path:start-end``, and scores its ladder at once;
- ``claim check`` sends ``runtime.evidence.claim.check``: it re-runs the check behind
  the rung its URN's ``#rung-<n>`` fragment names, and every rung above it, and files
  what they found as new revisions of those rungs;
- ``claim attest`` sends ``runtime.evidence.claim.attest``: it records what an outside
  party decided about rung 4, against an evidence record that files the decision. An
  attested rung 4 attests and never certifies.

The commands are dispatch and rendering only; the key a claim is filed under, each span's
digest, which rungs may run, what each check found and the lifecycle the claim lands at are
decided daemon-side.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Final

import typer

from eawf.surfaces.cli.commands.domain_consequence import DryRun, Yes, preview
from eawf.surfaces.cli.commands.domain_integration import _answer, _send

#: The dotted JSON-RPC names the commands forward to, spelled here so the Typer tree
#: builds without the daemon method registry on the path.
CLAIM_FILE: Final = "runtime.evidence.claim.file"
CLAIM_CHECK: Final = "runtime.evidence.claim.check"
CLAIM_ATTEST: Final = "runtime.evidence.claim.attest"

claim_app = typer.Typer(
    name="claim",
    help="File a claim, and re-run and attest the checks behind its evidence rungs.",
    no_args_is_help=True,
)

_SUBJECT_HELP: Final = "URN of the Task, Batch or Milestone the claim is about."
_TITLE_HELP: Final = "The claim in one line."
_DESCRIPTION_HELP: Final = "The claim restated plainly."
_IMPLICATION_HELP: Final = "What the claim buys if it stands."
_FALSIFIER_HELP: Final = "What observation would take it away."
_CITE_HELP: Final = "Key the claim cites: an EVD-#### evidence record or one RCP-#### receipt."
_ANCHOR_HELP: Final = (
    "Span a cited evidence record points into, as [EVD-####:]path:start-end; the "
    "evidence key may be left out when the claim cites exactly one."
)
_TREE_REVISION_HELP: Final = "The tree's canonical sequence the claim was decided against."
_CLAIM_URN_HELP: Final = (
    "URN of the claim; a #rung-<n> fragment names the lowest rung to re-run, "
    "and without one the whole ladder runs."
)
_ATTEST_URN_HELP: Final = "URN of the claim's rung 4, ending in #rung-4."
_REVISION_HELP: Final = "Revision the claim was read at (compare-and-swap token)."
_KEY_HELP: Final = "Caller's name for this request; a retry replays its answer."
_ACTOR_HELP: Final = "Principal key the request is attributed to."
_EVIDENCE_HELP: Final = "URN of the evidence record filing the outside party's decision."
_OUTCOME_HELP: Final = "What the outside party decided: passed or failed."
_FINDING_HELP: Final = "What they found, in words."

_Revision = Annotated[int, typer.Option("--expected-revision", help=_REVISION_HELP)]
_TreeRevision = Annotated[
    int, typer.Option("--expected-revision", "--expected-tree-revision", help=_TREE_REVISION_HELP)
]
_Key = Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)]
_Actor = Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)]


#: One span as an operator types it: an optional evidence key, a path, and a line range.
_SPAN: Final = re.compile(
    r"(?:(?P<evidence>EVD-\d{4,}):)?(?P<path>[^:]+):(?P<start>\d+)-(?P<end>\d+)"
)


def _spans(values: list[str]) -> list[dict[str, Any]]:
    """Return each ``--anchor`` as the span fields the daemon digests.

    Raises:
        typer.BadParameter: A value is not ``[EVD-####:]path:start-end``.
    """
    spans: list[dict[str, Any]] = []
    for value in values:
        match = _SPAN.fullmatch(value)
        if match is None:
            raise typer.BadParameter(f"{value!r} is not [EVD-####:]path:start-end")
        span: dict[str, Any] = {} if match["evidence"] is None else {"evidence": match["evidence"]}
        span |= {"path": match["path"], "start_line": int(match["start"])}
        spans.append(span | {"end_line": int(match["end"])})
    return spans


@claim_app.command("file")
def claim_file_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_SUBJECT_HELP)],
    title: Annotated[str, typer.Option("--title", help=_TITLE_HELP)],
    expected_revision: _TreeRevision,
    idempotency_key: _Key,
    actor: _Actor,
    description: Annotated[
        str | None, typer.Option("--description", help=_DESCRIPTION_HELP)
    ] = None,
    implication: Annotated[
        str | None, typer.Option("--implication", help=_IMPLICATION_HELP)
    ] = None,
    falsifier: Annotated[str | None, typer.Option("--falsifier", help=_FALSIFIER_HELP)] = None,
    evidence: Annotated[list[str] | None, typer.Option("--evidence", help=_CITE_HELP)] = None,
    anchors: Annotated[list[str] | None, typer.Option("--anchor", help=_ANCHOR_HELP)] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """File a claim about a Task, Batch or Milestone and score its evidence ladder."""
    params: dict[str, Any] = {
        "urn": urn,
        "expected_revision": expected_revision,
        "actor": actor,
        "idempotency_key": idempotency_key,
        "title": title,
        "description": description,
        "implication": implication,
        "falsifier": falsifier,
        "evidence": evidence or [],
        "anchors": _spans(anchors or []),
    }
    if not preview(CLAIM_FILE, urn, expected_revision, flags=ctx.obj, dry_run=dry_run, yes=yes):
        return
    answer = _send(ctx, CLAIM_FILE, params, urn=urn, verb_text="claim file", key=idempotency_key)
    if answer is not None:
        _answer(ctx, answer, operation=CLAIM_FILE, urn=urn, revision=expected_revision)


@claim_app.command("check")
def claim_check_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_CLAIM_URN_HELP)],
    expected_revision: _Revision,
    idempotency_key: _Key,
    actor: _Actor,
) -> None:
    """Re-run the check behind a rung of a claim, and every rung above it."""
    params = {
        "urn": urn,
        "expected_revision": expected_revision,
        "actor": actor,
        "idempotency_key": idempotency_key,
    }
    answer = _send(ctx, CLAIM_CHECK, params, urn=urn, verb_text="claim check", key=idempotency_key)
    if answer is not None:
        _answer(ctx, answer, operation=CLAIM_CHECK, urn=urn, revision=expected_revision)


@claim_app.command("attest")
def claim_attest_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_ATTEST_URN_HELP)],
    evidence: Annotated[str, typer.Option("--evidence", help=_EVIDENCE_HELP)],
    outcome: Annotated[str, typer.Option("--outcome", help=_OUTCOME_HELP)],
    finding: Annotated[str, typer.Option("--finding", help=_FINDING_HELP)],
    expected_revision: _Revision,
    idempotency_key: _Key,
    actor: _Actor,
) -> None:
    """Record what an outside party decided about a claim's rung 4."""
    params = {
        "urn": urn,
        "expected_revision": expected_revision,
        "actor": actor,
        "idempotency_key": idempotency_key,
        "evidence_ref": evidence,
        "outcome": outcome,
        "finding": finding,
    }
    answer = _send(
        ctx, CLAIM_ATTEST, params, urn=urn, verb_text="claim attest", key=idempotency_key
    )
    if answer is not None:
        _answer(ctx, answer, operation=CLAIM_ATTEST, urn=urn, revision=expected_revision)


__all__ = [
    "CLAIM_ATTEST",
    "CLAIM_CHECK",
    "CLAIM_FILE",
    "claim_app",
    "claim_attest_cmd",
    "claim_check_cmd",
    "claim_file_cmd",
]
