"""``/verify`` skill — judge one Delivery Batch at one exact revision.

The pass verifies and never repairs. It binds the head the Batch
actually delivers, walks the Batch's verification cycle there, and
returns per-criterion rows the aggregate verdict is derived from. It
resolves none of its own findings and edits no candidate.

Absence of evidence is not a pass. A criterion the pass cannot settle
reads as ``unverified``, which blocks, and a head that moved under the
pass reads as ``stale`` rather than as a result. The audit and review
modes complete from this grammar: walking a cycle needs the Batch
reference and the judgment criteria the caller names, and nothing else.

Verify is also where a Milestone's acceptance is asked about. When the
pass names a Milestone and the Batch clears, it opens the protected
approval the acceptance is taken on, presenting what the acceptance
journey showed and the exact tree it was shown on. The daemon files that
journey as the Milestone's acceptance bundle and binds the question to
its digest; the pass reports both and never answers the question, which
is a person's to seal.
The gate mode does not: judging one Task's completion needs the exact
base binding, the Run's report verdict, the gate specifications its
criteria reference and the runtime facts its proofs ran under, and no
surface this invocation reaches resolves them, so the pass says
``unverified`` and names the fields instead of presenting invented ones.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.runtime.runtimes.plugin_manifest import SkillManifest
from eawf.surfaces.render.envelope import SkillName
from eawf.workflow.skills._common import probe_skill_instruments
from eawf.workflow.skills.bodies.verify import (
    CriterionRow,
    VerifyBody,
    VerifyMode,
    VerifyOutcome,
)
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.workflow.skills.lifecycle_rpc import (
    OutputRendering,
    RpcCaller,
    RpcRefusedError,
    RpcScope,
    daemon_rpc_caller,
    row_keys,
    status_for,
)
from eawf.workflow.skills.registry import register

logger = logging.getLogger(__name__)


#: The Batch read model the pass binds its subject at.
BATCH_READ_METHOD: Final = "projection.batch.detail.read"

#: The read model the claims and evidence a finding cites are read from.
EVIDENCE_READ_METHOD: Final = "projection.evidence.read"

#: The verb that walks one Batch's verification cycle on its exact head.
DELIVERY_VERIFY_BATCH_METHOD: Final = "runtime.delivery.verify_batch"

#: The verb that judges whether one Task is finished on the Batch head.
DELIVERY_ASSESS_COMPLETION_METHOD: Final = "runtime.delivery.assess_completion"

#: The verb that asks the operator to accept a verified Milestone.
DELIVERY_OPEN_APPROVAL_METHOD: Final = "runtime.delivery.open_acceptance_approval"

#: Every JSON-RPC method this skill may address. A call outside the set
#: is refused before the transport is touched.
RPC_SCOPE: Final = RpcScope(
    skill="/verify",
    methods=(
        BATCH_READ_METHOD,
        EVIDENCE_READ_METHOD,
        DELIVERY_VERIFY_BATCH_METHOD,
        DELIVERY_ASSESS_COMPLETION_METHOD,
        DELIVERY_OPEN_APPROVAL_METHOD,
    ),
)

#: The complete accepted invocation, brackets optional and ``...`` repeatable.
INVOCATION_GRAMMAR: Final = (
    "/verify <batch-or-revision-ref> [--mode <gates|audit|review|security|all>] "
    "[--gate <id>...] [--severity-floor <P0|P1|P2|P3>] [--agents <1..8>] [--budget <spec>] "
    "[--milestone <ref>] [--journey <step>...] [--accepted-binding <binding>] "
    "[--requested-by <principal>] [--no-cache] [--idempotency-key <key>] "
    "[--output <human|json|markdown>]"
)

#: What this skill may cause, stated as the boundary it never crosses.
EFFECTS: Final = (
    "Batch and evidence read models plus the Batch verification and Task completion verbs, "
    "which file verification receipts, and the verb that opens a Milestone's acceptance "
    "question. The pass resolves no finding, edits no candidate, and answers no question."
)

#: The typed report every invocation produces.
OUTPUT_SCHEMA: Final = "verification_report"

#: The closed set of ways one invocation may end.
TERMINAL_OUTCOMES: Final[tuple[VerifyOutcome, ...]] = (
    "passed",
    "failed",
    "unverified",
    "stale",
    "blocked",
)

#: The request fields the completion verb names that no surface resolves.
_GATES_UNRESOLVED: Final[tuple[str, ...]] = (
    "base",
    "report_verdict",
    "gates",
    "proof_facts",
)

#: Why the gate mode cannot ask the question it exists to ask.
_GATES_STOP: Final = "proof_receipts_unpresented"

#: Why the security mode reaches no verb.
_SECURITY_STOP: Final = "security_verification_unbound"

#: The request fields opening an acceptance question needs beside the
#: Milestone, as the verb names them beside the args field presenting each.
_APPROVAL_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("steps", "journey"),
    ("accepted_binding", "accepted_binding"),
    ("requested_by", "requested_by"),
)

#: The refusal-code fragment that means the head moved under the pass.
_STALE_FRAGMENT: Final = "superseded"

#: Who a verification cycle the pass walks is attributed to. The skill's
#: own slash-prefixed name is not a valid principal key (the daemon's
#: ``PrincipalKey`` pattern is uppercase-anchored and admits no ``/``),
#: so the pass carries its own qualified key instead.
_ACTOR_PRINCIPAL: Final = "SKILL-VERIFY"

MANIFEST = SkillManifest(
    name="/verify",
    description="Verify one Delivery Batch at one exact revision, as auditor or as reviewer.",
    runtime=["claude-code", "codex", "opencode"],
    dispatch={"session_policy": "fresh"},
    output_envelope_kind=OUTPUT_SCHEMA,
)


class VerifyArgs(BaseModel):
    """The accepted invocation of ``/verify``, parsed and validated.

    Attributes:
        subject_ref: The Batch or revision the pass judges.
        mode: Which job the pass does.
        gate: The judgment criteria an independent row must cover.
        severity_floor: The lowest severity a review finding records.
        agents: How many arms the pass fans into.
        budget: The budget spec the pass charges against.
        milestone: The Milestone whose acceptance to ask about once the
            Batch clears.
        journey: What the Milestone's acceptance journey showed, one
            step outcome per journey step.
        accepted_binding: The exact tree the acceptance would be taken on.
        requested_by: The principal the question is recorded as asked by.
        no_cache: Re-derive every leg rather than reusing a receipt.
        idempotency_key: This request's name; minted when omitted.
        repo_root: The tree to address, when not the daemon's own.
        output: The rendering the caller wants.
    """

    model_config = ConfigDict(extra="forbid")

    subject_ref: str = Field(min_length=1)
    mode: VerifyMode = "all"
    gate: tuple[str, ...] = ()
    severity_floor: str | None = None
    agents: int = Field(default=1, ge=1, le=8)
    budget: str | None = None
    milestone: str | None = None
    journey: tuple[dict[str, Any], ...] = ()
    accepted_binding: dict[str, Any] | None = None
    requested_by: dict[str, Any] | None = None
    no_cache: bool = False
    idempotency_key: str | None = None
    repo_root: str | None = None
    output: OutputRendering = "human"


@register
class VerifySkill(Skill):
    """Concrete ``/verify`` skill.

    Attributes:
        name: The canonical skill name.
    """

    name: SkillName = "/verify"

    def __init__(self, *, caller: RpcCaller | None = None) -> None:
        """Bind the transport this pass makes its calls through.

        Args:
            caller: The JSON-RPC seam. ``None`` binds the daemon
                transport at first use, which is the production path.
        """
        self._caller = caller

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        """Probe the canonical instrument set."""
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        """Run one verification pass and return its report."""
        try:
            args = VerifyArgs.model_validate(dict(ctx.args))
        except ValidationError as error:
            return self._invalid(error)
        caller = self._caller if self._caller is not None else daemon_rpc_caller()
        params: dict[str, Any] = {}
        if args.repo_root is not None:
            params["repo_root"] = args.repo_root
        if args.mode == "gates":
            return self._unresolved(args, code=_GATES_STOP, unresolved=_GATES_UNRESOLVED)
        if args.mode == "security":
            return self._unresolved(args, code=_SECURITY_STOP, unresolved=())
        try:
            return self._cycle(caller, args, params)
        except RpcRefusedError as refused:
            return self._refused(args, refused)

    def _cycle(self, caller: RpcCaller, args: VerifyArgs, params: dict[str, Any]) -> SkillResult:
        """Walk the Batch's verification cycle and derive the aggregate.

        Raises:
            RpcRefusedError: The daemon refused a read or the cycle walk.
        """
        RPC_SCOPE.call(caller, BATCH_READ_METHOD, {**params, "key": args.subject_ref})
        evidence = RPC_SCOPE.call(caller, EVIDENCE_READ_METHOD, params)
        answer = RPC_SCOPE.call(
            caller,
            DELIVERY_VERIFY_BATCH_METHOD,
            {
                **params,
                "urn": args.subject_ref,
                "actor": _ACTOR_PRINCIPAL,
                "idempotency_key": args.idempotency_key or uuid.uuid4().hex,
                "judgment_criterion_ids": list(args.gate),
            },
        )
        blocking = [str(item) for item in answer.get("blocking_criterion_ids", ())]
        settled = [str(item) for item in answer.get("settled_criterion_ids", ())]
        rows = [
            CriterionRow(criterion_id=item, verdict="unverified", falsifier="") for item in blocking
        ] + [
            CriterionRow(
                criterion_id=item, verdict="passed", falsifier="independent audit row cleared it"
            )
            for item in settled
        ]
        merge_ready = bool(answer.get("merge_ready", False))
        outcome: VerifyOutcome = "passed" if merge_ready else "unverified"
        approval, unresolved = self._ask_acceptance(caller, args, params, merge_ready=merge_ready)
        body = VerifyBody(
            subject_ref=args.subject_ref,
            mode=args.mode,
            method=DELIVERY_VERIFY_BATCH_METHOD,
            head_generation=answer.get("head_generation"),
            stage=answer.get("stage"),
            blocking_criterion_ids=blocking,
            settled_criterion_ids=settled,
            rows=rows,
            merge_ready=merge_ready,
            approval_ref=approval.get("action_ref"),
            bundle_digest=approval.get("bundle_digest"),
            acceptance_bundle=approval.get("acceptance_bundle"),
            unresolved_request_fields=list(unresolved),
            outcome=outcome,
            reason=str(
                answer.get(
                    "reason",
                    f"batch {args.subject_ref} was walked against "
                    f"{len(row_keys(evidence))} evidence record(s)",
                )
            ),
        )
        return SkillResult(
            status=status_for(outcome),
            body=body.model_dump(mode="json"),
            repair_commands=None if merge_ready else [f"/integrate show {args.subject_ref}"],
            next_valid_actions=[f"/verify {args.subject_ref} --mode all --no-cache"],
        )

    def _ask_acceptance(
        self,
        caller: RpcCaller,
        args: VerifyArgs,
        params: dict[str, Any],
        *,
        merge_ready: bool,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Open the named Milestone's acceptance question once the Batch clears.

        Args:
            caller: The transport seam.
            args: The validated invocation.
            params: The tree-addressing params every call carries.
            merge_ready: Whether the walk just taken cleared the Batch.

        Returns:
            The question the daemon answered with, empty when none was
            asked, and the request fields the invocation left unpresented.

        Raises:
            RpcRefusedError: The daemon refused to open the question.
        """
        if args.milestone is None:
            return {}, ()
        unresolved = tuple(
            request_field
            for request_field, presented in _APPROVAL_FIELDS
            if not getattr(args, presented)
        )
        if unresolved or not merge_ready:
            return {}, unresolved
        opened = RPC_SCOPE.call(
            caller,
            DELIVERY_OPEN_APPROVAL_METHOD,
            {
                **params,
                "urn": args.milestone,
                "actor": _ACTOR_PRINCIPAL,
                "requested_by": args.requested_by,
                "steps": list(args.journey),
                "accepted_binding": args.accepted_binding,
            },
        )
        return opened, ()

    def _unresolved(
        self, args: VerifyArgs, *, code: str, unresolved: tuple[str, ...]
    ) -> SkillResult:
        """Report a mode that reaches no verb, naming what it would have invented."""
        reason = (
            "judging one Task's completion needs the exact base binding, the report verdict, the "
            "gate specifications its criteria reference and the runtime facts its proofs ran "
            "under, and no surface this invocation reaches resolves them"
            if unresolved
            else "no verb settles a security verdict, so this mode cannot be answered here"
        )
        body = VerifyBody(
            subject_ref=args.subject_ref,
            mode=args.mode,
            unresolved_request_fields=list(unresolved),
            refusal_code=code,
            outcome="unverified",
            reason=reason,
        )
        return SkillResult(
            status=status_for("unverified"),
            body=body.model_dump(mode="json"),
            repair_commands=[f"/verify {args.subject_ref} --mode all"],
        )

    def _refused(self, args: VerifyArgs, refused: RpcRefusedError) -> SkillResult:
        """Turn a daemon refusal into a stale or blocked report carrying its code."""
        outcome: VerifyOutcome = "stale" if _STALE_FRAGMENT in refused.code else "blocked"
        body = VerifyBody(
            subject_ref=args.subject_ref,
            mode=args.mode,
            method=refused.method,
            refusal_code=refused.code,
            outcome=outcome,
            reason=f"{refused.method} refused the pass: {refused.detail}",
        )
        return SkillResult(
            status=status_for(outcome),
            body=body.model_dump(mode="json"),
            repair_commands=[f"/integrate show {args.subject_ref}"],
        )

    def _invalid(self, error: ValidationError) -> SkillResult:
        """Refuse an invocation the grammar does not declare."""
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        body = VerifyBody(
            subject_ref="",
            mode="all",
            refusal_code="invocation_undeclared",
            outcome="blocked",
            reason=f"the invocation does not parse; check {', '.join(fields)}",
        )
        return SkillResult(
            status=status_for("blocked"),
            body=body.model_dump(mode="json"),
            repair_commands=[INVOCATION_GRAMMAR],
        )


__all__ = [
    "BATCH_READ_METHOD",
    "DELIVERY_ASSESS_COMPLETION_METHOD",
    "DELIVERY_OPEN_APPROVAL_METHOD",
    "DELIVERY_VERIFY_BATCH_METHOD",
    "EFFECTS",
    "EVIDENCE_READ_METHOD",
    "INVOCATION_GRAMMAR",
    "MANIFEST",
    "OUTPUT_SCHEMA",
    "RPC_SCOPE",
    "TERMINAL_OUTCOMES",
    "VerifyArgs",
    "VerifySkill",
]
