"""The operator verbs over a filed claim's evidence ladder.

- ``claim check`` sends ``runtime.evidence.claim.check``: it re-runs the check behind
  the rung its URN's ``#rung-<n>`` fragment names, and every rung above it, and files
  what they found as new revisions of those rungs;
- ``claim attest`` sends ``runtime.evidence.claim.attest``: it records what an outside
  party decided about rung 4, against an evidence record that files the decision. An
  attested rung 4 attests and never certifies.

The commands are dispatch and rendering only; which rungs may run, what each check found
and the lifecycle the claim lands at are decided daemon-side.
"""

from __future__ import annotations

from typing import Annotated, Final

import typer

from eawf.surfaces.cli.commands.domain_integration import _answer, _send

#: The dotted JSON-RPC names the commands forward to, spelled here so the Typer tree
#: builds without the daemon method registry on the path.
CLAIM_CHECK: Final = "runtime.evidence.claim.check"
CLAIM_ATTEST: Final = "runtime.evidence.claim.attest"

claim_app = typer.Typer(
    name="claim",
    help="Re-run and attest the checks behind a claim's evidence rungs.",
    no_args_is_help=True,
)

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
_Key = Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)]
_Actor = Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)]


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


__all__ = ["CLAIM_ATTEST", "CLAIM_CHECK", "claim_app", "claim_attest_cmd", "claim_check_cmd"]
