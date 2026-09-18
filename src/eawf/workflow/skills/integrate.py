"""``/integrate`` skill — prepare or execute one daemon-owned integration action.

The skill never authors product changes and never chooses a candidate by
intuition. It resolves the Batch, its conflicts and its current
integration generation from the read models, then submits exactly one
candidate or integration request and returns the durable reference that
came back. A conflict is never resolved by editing a candidate here.

Two of the five branches complete from this grammar and three do not,
and the report says which. ``show`` binds the Batch and its conflict
frames and mutates nothing. ``apply`` and ``retry`` address the delivery
verb, whose request names a full revision binding, a commit subject per
sealed candidate, a typed exit per conflict kind and a diagnostic
reference -- fields the grammar carries no option for and no read model
resolves. ``seal`` addresses the candidate report verb, whose request
names the accepted report's schema, digest and verdict, which the same
argument applies to. Those branches stop with the unresolved fields
named rather than sending a request whose halves were invented.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.runtime.runtimes.plugin_manifest import SkillManifest
from eawf.surfaces.render.envelope import SkillName
from eawf.workflow.skills._common import probe_skill_instruments
from eawf.workflow.skills.bodies.integrate import (
    IntegrateAction,
    IntegrateBody,
    IntegrateOutcome,
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


#: The Batch read model an integration binds its subject at.
BATCH_READ_METHOD: Final = "projection.batch.detail.read"

#: The read model the conflict frames of a blocked attempt are read from.
CONFLICT_READ_METHOD: Final = "projection.merge.conflict.read"

#: The verb that binds a Run's terminal report and attempts the seal.
CANDIDATE_REPORT_BIND_METHOD: Final = "runtime.candidate.report.bind"

#: The verb that turns a Batch's sealed candidates into one delivery.
DELIVERY_INTEGRATE_METHOD: Final = "runtime.delivery.integrate"

#: Every JSON-RPC method this skill may address. A call outside the set
#: is refused before the transport is touched.
RPC_SCOPE: Final = RpcScope(
    skill="/integrate",
    methods=(
        BATCH_READ_METHOD,
        CONFLICT_READ_METHOD,
        CANDIDATE_REPORT_BIND_METHOD,
        DELIVERY_INTEGRATE_METHOD,
    ),
)

#: The complete accepted invocation, brackets optional and ``...`` repeatable.
INVOCATION_GRAMMAR: Final = (
    "/integrate <seal|select|apply|retry|show> <batch-or-candidate-ref> "
    "[--candidate <ref>...] [--strategy <declared-strategy>] [--expected-head <sha>] "
    "[--verify-after] [--reason <text>] [--dry-run] [--expected-revision <N>] "
    "[--idempotency-key <key>] [--output <human|json|markdown>]"
)

#: What this skill may cause, stated as the boundary it never crosses.
EFFECTS: Final = (
    "Batch and conflict read models plus the candidate-report and delivery-integration verbs. "
    "The skill authors no product change, edits no candidate, and resolves no conflict itself."
)

#: The typed report every invocation produces.
OUTPUT_SCHEMA: Final = "integration_skill_report"

#: The closed set of ways one invocation may end.
TERMINAL_OUTCOMES: Final[tuple[IntegrateOutcome, ...]] = (
    "shown",
    "sealed",
    "selected",
    "integrated",
    "conflicted",
    "stale",
    "blocked",
)

#: The request fields the delivery verb names that no surface resolves.
_INTEGRATE_UNRESOLVED: Final[tuple[str, ...]] = (
    "base",
    "branch",
    "subject",
    "subjects",
    "exit_refs",
    "diagnostic_ref",
)

#: The request fields the candidate-report verb names that no surface resolves.
_SEAL_UNRESOLVED: Final[tuple[str, ...]] = (
    "report_schema_ref",
    "report_digest",
    "verdict",
)

#: Why a selection cannot be computed from what a caller can read.
_SELECT_STOP: Final = "candidate_set_unreadable"

#: Why a delivery request cannot be assembled from this grammar.
_INTEGRATE_STOP: Final = "integration_request_unnamed"

#: Why a seal request cannot be assembled from this grammar.
_SEAL_STOP: Final = "candidate_report_unbound"

MANIFEST = SkillManifest(
    name="/integrate",
    description="Prepare or execute one daemon-owned integration action on a Delivery Batch.",
    runtime=["claude-code", "codex", "opencode"],
    dispatch={"session_policy": "fresh"},
    output_envelope_kind=OUTPUT_SCHEMA,
)


class IntegrateArgs(BaseModel):
    """The accepted invocation of ``/integrate``, parsed and validated.

    Attributes:
        action: The branch this invocation selects.
        subject_ref: The Batch or candidate the branch addresses.
        candidate: Candidates to restrict the branch to.
        strategy: The declared selection policy to apply.
        expected_head: The head the caller believes the Batch delivers.
        verify_after: Request the declared gates once an apply lands.
        reason: The operator's sentence for a retry or a rejection.
        dry_run: Resolve and validate, record no effect.
        expected_revision: The Batch revision the caller read.
        idempotency_key: This request's name.
        repo_root: The tree to address, when not the daemon's own.
        output: The rendering the caller wants.
    """

    model_config = ConfigDict(extra="forbid")

    action: IntegrateAction
    subject_ref: str = Field(min_length=1)
    candidate: tuple[str, ...] = ()
    strategy: str | None = None
    expected_head: str | None = None
    verify_after: bool = False
    reason: str | None = None
    dry_run: bool = False
    expected_revision: int | None = None
    idempotency_key: str | None = None
    repo_root: str | None = None
    output: OutputRendering = "human"


@register
class IntegrateSkill(Skill):
    """Concrete ``/integrate`` skill.

    Attributes:
        name: The canonical skill name.
    """

    name: SkillName = "/integrate"

    def __init__(self, *, caller: RpcCaller | None = None) -> None:
        """Bind the transport this action makes its calls through.

        Args:
            caller: The JSON-RPC seam. ``None`` binds the daemon
                transport at first use, which is the production path.
        """
        self._caller = caller

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        """Probe the canonical instrument set."""
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        """Run one integration branch and return its report."""
        try:
            args = IntegrateArgs.model_validate(dict(ctx.args))
        except ValidationError as error:
            return self._invalid(error)
        caller = self._caller if self._caller is not None else daemon_rpc_caller()
        params: dict[str, Any] = {}
        if args.repo_root is not None:
            params["repo_root"] = args.repo_root
        try:
            return self._branch(caller, args, params)
        except RpcRefusedError as refused:
            return self._refused(args, refused)

    def _branch(
        self, caller: RpcCaller, args: IntegrateArgs, params: dict[str, Any]
    ) -> SkillResult:
        """Route the selected branch to the one verb it addresses.

        Raises:
            RpcRefusedError: The daemon refused a call the branch made.
        """
        if args.action == "show":
            return self._show(caller, args, params)
        if args.action == "select":
            return self._stopped(
                args,
                code=_SELECT_STOP,
                unresolved=("candidates",),
                reason=(
                    "no read model renders a Batch's sealed candidate set, so the declared "
                    "selection policy has nothing to order"
                ),
            )
        if args.action == "seal":
            return self._stopped(
                args,
                code=_SEAL_STOP,
                unresolved=_SEAL_UNRESOLVED,
                reason=(
                    "sealing binds a Run's accepted report, whose schema, digest and verdict this "
                    "invocation carries no option for"
                ),
            )
        return self._stopped(
            args,
            code=_INTEGRATE_STOP,
            unresolved=_INTEGRATE_UNRESOLVED,
            reason=(
                "a delivery request names the exact base, the branch, one commit subject per "
                "sealed candidate, a typed exit per conflict kind and a diagnostic reference, and "
                "no surface this invocation reaches resolves them"
            ),
        )

    def _show(self, caller: RpcCaller, args: IntegrateArgs, params: dict[str, Any]) -> SkillResult:
        """Render Batch and conflict truth without mutating anything.

        Raises:
            RpcRefusedError: The daemon refused one of the two reads.
        """
        batch = RPC_SCOPE.call(caller, BATCH_READ_METHOD, {**params, "key": args.subject_ref})
        conflicts = RPC_SCOPE.call(caller, CONFLICT_READ_METHOD, params)
        generations = row_keys(batch)
        frames = row_keys(conflicts)
        outcome: IntegrateOutcome = "conflicted" if frames else "shown"
        body = IntegrateBody(
            action=args.action,
            subject_ref=args.subject_ref,
            method=CONFLICT_READ_METHOD,
            generation_ids=generations,
            conflict_refs=frames,
            outcome=outcome,
            reason=(
                f"batch {args.subject_ref} renders {len(generations)} record(s) and "
                f"{len(frames)} conflict frame(s) at the read cursor"
            ),
        )
        return SkillResult(
            status=status_for(outcome),
            body=body.model_dump(mode="json"),
            next_valid_actions=[f"/verify {args.subject_ref} --mode all"],
        )

    def _stopped(
        self,
        args: IntegrateArgs,
        *,
        code: str,
        unresolved: tuple[str, ...],
        reason: str,
    ) -> SkillResult:
        """Build the branch result that names what it would have had to invent."""
        body = IntegrateBody(
            action=args.action,
            subject_ref=args.subject_ref,
            unresolved_request_fields=list(unresolved),
            refusal_code=code,
            outcome="blocked",
            reason=reason,
        )
        return SkillResult(
            status=status_for("blocked"),
            body=body.model_dump(mode="json"),
            repair_commands=[f"/integrate show {args.subject_ref}"],
        )

    def _refused(self, args: IntegrateArgs, refused: RpcRefusedError) -> SkillResult:
        """Turn a daemon refusal into a blocked report carrying its code."""
        outcome: IntegrateOutcome = "stale" if "superseded" in refused.code else "blocked"
        body = IntegrateBody(
            action=args.action,
            subject_ref=args.subject_ref,
            method=refused.method,
            refusal_code=refused.code,
            outcome=outcome,
            reason=f"{refused.method} refused the action: {refused.detail}",
        )
        return SkillResult(
            status=status_for(outcome),
            body=body.model_dump(mode="json"),
            repair_commands=[f"/integrate show {args.subject_ref}"],
        )

    def _invalid(self, error: ValidationError) -> SkillResult:
        """Refuse an invocation the grammar does not declare."""
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        body = IntegrateBody(
            action="show",
            subject_ref="",
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
    "CANDIDATE_REPORT_BIND_METHOD",
    "CONFLICT_READ_METHOD",
    "DELIVERY_INTEGRATE_METHOD",
    "EFFECTS",
    "INVOCATION_GRAMMAR",
    "MANIFEST",
    "OUTPUT_SCHEMA",
    "RPC_SCOPE",
    "TERMINAL_OUTCOMES",
    "IntegrateArgs",
    "IntegrateSkill",
]
