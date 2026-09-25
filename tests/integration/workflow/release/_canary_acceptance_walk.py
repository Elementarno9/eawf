"""One canary Milestone walked from creation to acceptance on native authority.

The walk is the evidence behind the dev3 membership reference, so it is
driven the way an operator drives it and records how each step was
reached. Records are admitted through the native create verbs and moved
through the lifecycle verbs. The Task is dispatched through ``/dispatch``,
the Batch is integrated through ``/integrate apply`` and verified through
``/verify``, and the second ``/verify`` pass opens the acceptance question.
The operator's answer is sealed through its verb and the Milestone is
accepted on exactly the bundle and the approval those verbs answered.

Three steps are not a skill's to take and the record says so: the worker
commits and submits its candidate from inside the lease, the Run's
terminal report is bound through the candidate verb because the
``/integrate seal`` branch still names fields it cannot resolve, and the
lifecycle moves and the operator's seal and acceptance are the
operator's. The evidence rows a journey step and a diagnostic cite have
no minting verb, so they are filed through the transaction's ledger
commit rather than written into a ledger file.

Every skill call and every verb runs in process against a disposable
canary repository, through the registered handlers a socket caller
reaches. The provider launcher is an in-process stand-in that answers
the handshake, so no provider process is ever started.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal
from unittest import mock

from eawf.kernel.delivery.acceptance import MilestoneAcceptanceBundle
from eawf.kernel.delivery.receipts import RevisionBinding, RevisionRefKind, canonical_digest
from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.candidate import candidate_identity
from eawf.kernel.spec.common import CriterionSpec, QualityDimension
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import LedgerRecord, effective_records, read_ledger_records
from eawf.kernel.store.tiers import ENTITY_COLLECTIONS, Epoch2Collection
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon import methods, native_dispatch
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY, commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery_acceptance import BUNDLE_KEY_PREFIX
from eawf.runtime.daemon.native_dispatch import active_lease_of, compile_launchers
from eawf.runtime.integration.git_workspace import COMMIT_ARTIFACT_PREFIX, delivery_pin_ref
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    compose_worker_hello,
)
from eawf.runtime.workspace.lease import workspace_path
from eawf.workflow.skills import dispatch as _dispatch_skill  # noqa: F401  -- registers /dispatch
from eawf.workflow.skills import (
    integrate as _integrate_skill,  # noqa: F401  -- registers /integrate
)
from eawf.workflow.skills import verify as _verify_skill  # noqa: F401  -- registers /verify
from eawf.workflow.skills.engine import SkillContext
from eawf.workflow.skills.lifecycle_rpc import RpcRefusedError, refusal_code
from eawf.workflow.skills.registry import lookup
from tests import _provider_helpers as fx

#: The canary's registry code. Short enough for the prefixed workspace,
#: project and repository keys to fit the symbol grammar.
PROJECT_CODE: Final = "W37CANARY"

#: The operator every lifecycle move, seal and acceptance is attributed to.
OPERATOR: Final = "OP-0001"
OPERATOR_PRINCIPAL: Final[dict[str, str]] = {"principal_kind": "human", "principal_id": OPERATOR}

#: The principal the worker inside the lease submits its candidate as.
WORKER: Final = "WORKER-0001"

#: The branch the Batch integrates into.
TARGET_BRANCH: Final = "canary/mls-0001"

#: The one path the worker changes, inside the Run's write set.
WORKED_PATH: Final = "src/module.py"

#: The provider the stand-in launcher answers for, which is the provider
#: the fixture profile compiles a dispatch to.
PROVIDER_KIND: Final = "codex"

#: The process id the stand-in reports. The outcome requires one, and no
#: process is started behind it.
STAND_IN_PID: Final = 4242

#: How a step was reached: through a shipped skill, through a daemon verb
#: called directly because no skill takes that step, or through the
#: transaction's own commit for a record no verb mints.
Through = Literal["skill", "verb", "transaction"]

#: The one commit path a record no verb mints is filed through.
LEDGER_COMMIT_SURFACE: Final = "epoch2_transaction.commit_ledger_append"

#: The record the walk is committed as, beside the canary evidence.
WALK_SCHEMA_VERSION: Final = "canary-acceptance-walk/v1"

#: How the canary's runtime directory is described, rather than located.
RUNTIME_DIR_NOTE: Final = "fresh, allocated by the provisioning and named nowhere in this record"

#: What stood in for the provider, stated so the record cannot be read as a live run.
LAUNCHER_NOTE: Final = (
    "an in-process stand-in answered the worker handshake; no provider process was started"
)


@dataclass(frozen=True)
class WalkStep:
    """One step of the walk, as the record carries it.

    Attributes:
        step: What the step did.
        surface: The skill invocation or daemon verb that took it.
        through: Whether a skill or a direct verb call took it.
        outcome: What the surface answered, in one word.
        note: Why this surface, in one sentence.
    """

    step: str
    surface: str
    through: Through
    outcome: str
    note: str

    def as_row(self) -> dict[str, str]:
        """Return the step as a record row."""
        return {
            "step": self.step,
            "surface": self.surface,
            "through": self.through,
            "outcome": self.outcome,
            "note": self.note,
        }


@dataclass
class CanaryWalk:
    """Everything the walk produced that the record is derived from.

    Attributes:
        canary: The provisioned canary.
        container: The URN prefix every record of the canary is spelled under.
        milestone_urn: The Milestone that was walked.
        milestone_status: Where the Milestone stands when the walk ends.
        bundle: The acceptance bundle the approval was sealed on.
        approval_ref: The sealed approval the acceptance was taken on.
        generation_ids: The integration generations the delivery selected.
        delivered_head: The commit the delivery pinned.
        accepted_at: When the acceptance committed.
        steps: Every step, in the order it was taken.
    """

    canary: CanaryProvision
    container: str
    milestone_urn: str = ""
    milestone_status: str = ""
    bundle: MilestoneAcceptanceBundle | None = None
    approval_ref: str = ""
    generation_ids: tuple[str, ...] = ()
    delivered_head: str = ""
    accepted_at: datetime | None = None
    steps: list[WalkStep] = field(default_factory=list)

    @property
    def bundle_reference(self) -> str:
        """Return the membership reference the accepted bundle is named by."""
        assert self.bundle is not None, "the walk sealed no bundle"
        key = (
            f"{BUNDLE_KEY_PREFIX}{self.bundle.revision:04d}-{self.bundle.milestone_ref.entity_key}"
        )
        return f"{self.milestone_urn}#{key}"


class StandInLauncher:
    """A provider launcher that answers the handshake and starts nothing.

    Attributes:
        provider_kind: Which provider this launcher stands in for.
        launches: How many launches it answered.
    """

    def __init__(self) -> None:
        """Start having answered nothing."""
        self.provider_kind = PROVIDER_KIND
        self.launches = 0

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Answer one launch with a worker announcement bound to its contract."""
        self.launches += 1
        session_ref = f"stand-in-session-{request.hello_sequence}"
        return NativeLaunchOutcome(
            provider_session_ref=session_ref,
            subprocess_pid=STAND_IN_PID,
            hello=compose_worker_hello(
                spec=request.spec,
                capsule=request.capsule,
                provider_session_ref=session_ref,
                sdk_version="1.0.0",
                worker_protocol_version="1.0.0",
                event_codec_version="1.0.0",
                hello_sequence=request.hello_sequence,
            ),
        )


def git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(root: Path) -> None:
    """Initialise a one-commit repository at *root*, with a committer set."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "canary@example.com")
    git(root, "config", "user.name", "canary")
    (root / "src").mkdir(exist_ok=True)
    (root / WORKED_PATH).write_text("x = 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial")


class Walker:
    """The daemon context, the skills and the verbs one walk is driven through."""

    def __init__(self, repo_root: Path, runtime_root: Path) -> None:
        """Provision the canary and bind a daemon context of its own.

        Args:
            repo_root: Where the disposable canary repository is created.
            runtime_root: Where the daemon context keeps its WAL.
        """
        make_repo(repo_root)
        ref = canary_ref(PROJECT_CODE)
        canary = provision_canary(repo_root=repo_root, ref=ref, provisioned_at=datetime.now(UTC))
        self.ctx = MethodContext(
            started_at=datetime.now(UTC).isoformat(),
            pid=1,
            protocol_version="1",
            version="canary",
            wal_dir=runtime_root / "wal",
        )
        self.root = canary.root
        self.walk = CanaryWalk(
            canary=canary, container=str(ref.repository).rsplit("/repository/", 1)[0]
        )
        self.repository = str(ref.repository)

    # ---- addressing -------------------------------------------------------

    def urn(self, kind: str, key: str) -> str:
        """Return the URN of the canary record *key* of *kind*."""
        return f"{self.walk.container}/{kind}/{key}"

    @property
    def context(self) -> Epoch2RootContext:
        """Return the native context of the canary tree."""
        return self.ctx.native_root_context(self.root / ".ea")

    def cursor(self) -> int:
        """Return the tree's committed canonical sequence, which a create compares."""
        with self.context.session([self.urn("track", "TRK-CANARY")]) as session:
            return int(session.read_document().get(CANONICAL_SEQUENCE_KEY, 0))

    def revision(self, urn: str) -> int:
        """Return the revision the document holds *urn* at."""
        parsed = parse_qualified_urn(urn)
        collection = ENTITY_COLLECTIONS[parsed.kind]
        with self.context.session([urn]) as session:
            row = document_rows(session.read_document(), collection)[parsed.entity_key]
        return int(row["revision"])

    def stored(self, urn: str) -> dict[str, Any]:
        """Return the stored row of *urn*, from the document or its ledger."""
        parsed = parse_qualified_urn(urn)
        collection, key = ENTITY_COLLECTIONS[parsed.kind], parsed.entity_key
        with self.context.session([urn]) as session:
            row = document_rows(session.read_document(), collection).get(key)
            if row is not None:
                return dict(row)
            lines = effective_records(read_ledger_records(session.ledger_path(collection)))
        matches = [
            item.payload
            for item in lines
            if item.record_key == key and "payload_kind" not in item.payload
        ]
        assert matches, f"no record holds {urn}"
        return dict(matches[-1])

    # ---- the three surfaces -----------------------------------------------

    def verb(self, method: str, **params: Any) -> dict[str, Any]:
        """Call one registered verb the way a socket caller does."""
        return asyncio.run(
            methods.dispatch(method, self.ctx, {"repo_root": str(self.root), **params})
        )

    def domain(self, method: str, **params: Any) -> dict[str, Any]:
        """Call one domain verb and require it to have committed."""
        answer = self.verb(method, actor=OPERATOR, idempotency_key=uuid.uuid4().hex, **params)
        assert answer["status"] == "ok", answer
        return answer

    def caller(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Be the transport a skill's calls reach the daemon through."""
        try:
            return asyncio.run(methods.dispatch(method, self.ctx, params))
        except DaemonValidationError as error:
            raise RpcRefusedError(
                method=method, code=refusal_code(str(error)), detail=str(error)
            ) from error

    def skill(self, name: str, **args: Any) -> dict[str, Any]:
        """Run one shipped skill against the canary and return its typed body."""
        skill_cls = lookup(name)
        assert skill_cls is not None, f"{name} is not registered"
        skill = skill_cls(caller=self.caller)  # type: ignore[call-arg]
        result = skill.action(
            SkillContext(
                scope="canary",
                session="canary",
                args={**args, "repo_root": str(self.root)},
            )
        )
        assert isinstance(result.body, dict), f"{name} answered no typed body"
        return result.body

    def record(self, step: str, surface: str, through: Through, outcome: str, note: str) -> None:
        """Append one step to the record."""
        self.walk.steps.append(WalkStep(step, surface, through, outcome, note))

    # ---- the steps --------------------------------------------------------

    def create(self, kind: str, key: str, spec: dict[str, Any]) -> str:
        """Admit one record through its native create verb and return its URN."""
        urn = self.urn(kind, key)
        method = f"domain.{kind}.create"
        self.domain(method, urn=urn, expected_revision=self.cursor(), spec={"key": key, **spec})
        self.record(f"create {kind} {key}", method, "verb", "created", "no skill admits records")
        return urn

    def move(self, method: str, urn: str, note: str, **params: Any) -> None:
        """Move one record through a lifecycle verb."""
        answer = self.domain(method, urn=urn, expected_revision=self.revision(urn), **params)
        self.record(
            f"{method.split('.', 1)[1]} {urn.rsplit('/', 1)[1]}", method, "verb", "moved", note
        )
        assert answer["revision_after"] > answer.get("revision_before", 0)

    def transition(self, urn: str, to_status: str, note: str, **params: Any) -> None:
        """Take a Task edge no per-entity verb names, through the transition verb."""
        self.domain(
            "domain.transition.apply",
            urn=urn,
            to_status=to_status,
            expected_revision=self.revision(urn),
            **params,
        )
        self.record(
            f"move {urn.rsplit('/', 1)[1]} to {to_status}",
            "domain.transition.apply",
            "verb",
            "moved",
            note,
        )

    def file_evidence(self, key: str, *, kind: str, summary: str) -> str:
        """Commit one evidence row through the transaction's ledger append."""
        urn = self.urn("evidence", key)
        now = datetime.now(UTC)
        payload = {"id": key, "kind": kind, "summary": summary, "recorded_at": now.isoformat()}
        with self.context.session([self.walk.milestone_urn]) as session:
            commit_ledger_append(
                session,
                LedgerRecord(
                    collection=Epoch2Collection.EVIDENCE,
                    record_key=key,
                    status="recorded",
                    recorded_at=now,
                    payload=payload,
                ),
            )
        self.record(
            f"file evidence {key}",
            LEDGER_COMMIT_SURFACE,
            "transaction",
            "filed",
            "no verb mints an evidence row, so it is committed through the transaction",
        )
        return urn


def criterion() -> CriterionSpec:
    """Return the one deterministic criterion the canary Task is proved by."""
    return CriterionSpec(
        id="CR-01",
        text="the module reports the delivered value",
        kind="behavioral",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["G-01"],
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="the delivered module assigns the new value",
    )


def worker_commit(walker: Walker, run_urn: str) -> tuple[str, str]:
    """Commit the Task's change inside the leased worktree, as the worker does.

    Returns:
        The submission reference of the commit and its resulting tree digest.
    """
    lease = active_lease_of(walker.context, run_ref=run_urn, now=datetime.now(UTC))
    assert lease is not None, "a dispatched Run holds a lease"
    workspace = workspace_path(walker.context, handle=lease.workspace_handle)
    (workspace / WORKED_PATH).write_text("x = 2\n", encoding="utf-8")
    git(workspace, "add", "--", WORKED_PATH)
    git(workspace, "commit", "-q", "-m", "canary task work")
    sha = git(workspace, "rev-parse", "HEAD")
    tree = git(workspace, "rev-parse", "HEAD^{tree}")
    return f"{COMMIT_ARTIFACT_PREFIX}{sha}", f"sha256:{hashlib.sha256(tree.encode()).hexdigest()}"


def base_binding(walker: Walker, batch_urn: str) -> dict[str, Any]:
    """Return the observed revision the Batch starts from: main at the lease base."""
    head = git(walker.root, "rev-parse", "main")
    return RevisionBinding.model_validate(
        {
            "repository_ref": walker.repository,
            "ref_kind": RevisionRefKind.INTEGRATION,
            "head_sha": head,
            "tree_sha": git(walker.root, "rev-parse", "main^{tree}"),
            "parent_sha": None,
            "batch_ref": batch_urn,
            "integration_generation": 1,
            "manifest_digest": canonical_digest({"canary": PROJECT_CODE, "head": head}),
            "criteria_digest": canonical_digest([]),
            "policy_digest": canonical_digest({"policy": "canary"}),
            "environment_digest": None,
            "bound_at": datetime.now(UTC),
        }
    ).model_dump(mode="json")


def dispatch_request(run_urn: str, task_urn: str) -> dict[str, Any]:
    """Return the compiled dispatch request the operator presents to ``/dispatch``."""
    request = fx.task_request(
        run_ref=run_urn,
        run_scope={
            "scope_kind": "task",
            "purpose": "implement",
            "task_ref": task_urn,
            "write_set": ["src"],
        },
    )
    return {
        "compile_request": request.model_dump(mode="json"),
        "provider_documents": fx.documents(),
        "provider_registry": fx.registry().model_dump(mode="json"),
        "bindings": [fx.binding().model_dump(mode="json")],
        "capsule": {
            "criteria_digest": canonical_digest([criterion().model_dump(mode="json")]),
            "report_schema_ref": "schema://executor-report/v1",
            "tool_grants": ["budget_status", "submit_candidate"],
            "stop_conditions": ["budget_exhausted"],
        },
        "base": "main",
        "lease_ttl_seconds": 600,
        "prompt": "change the module to assign the delivered value",
    }


def accepted_binding(walker: Walker, delivered: str, task_urn: str) -> dict[str, Any]:
    """Return the exact tree the acceptance is taken on: the delivered commit."""
    return {
        "head_sha": delivered,
        "tree_sha": git(walker.root, "rev-parse", f"{delivered}^{{tree}}"),
        "contract_digest": canonical_digest(walker.stored(task_urn)["criteria"]),
        "policy_revision": 1,
        "evidence_digest": canonical_digest({"delivered": delivered}),
    }


def walk_canary(repo_root: Path, runtime_root: Path) -> CanaryWalk:
    """Walk one canary Milestone from creation to acceptance.

    Args:
        repo_root: Where the disposable canary repository is created.
        runtime_root: Where the daemon context keeps its WAL.

    Returns:
        What the walk produced, with every step it took.
    """
    walker = Walker(repo_root, runtime_root)
    launcher = StandInLauncher()
    with mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
    ):
        _walk(walker)
    assert launcher.launches == 1, "exactly one Run was launched, by the stand-in"
    return walker.walk


@dataclass(frozen=True)
class Planned:
    """The records the walk admitted, by URN.

    Attributes:
        milestone: The canary Milestone.
        batch: Its one Batch.
        task: The Task the Batch delivers.
        exit_task: The backlog Task a conflict exit lands on.
        run: The Run the Task is dispatched under.
    """

    milestone: str
    batch: str
    task: str
    exit_task: str
    run: str


def _walk(walker: Walker) -> None:
    """Take every step of the walk, in order."""
    planned = _plan(walker)
    _dispatch_and_seal(walker, planned)
    binding = _integrate_and_verify(walker, planned)
    _accept(walker, planned, binding)


def _plan(walker: Walker) -> Planned:
    """Admit the Track, Milestone, Batch, Tasks and Run, and open them for work."""
    code = PROJECT_CODE
    track = walker.create(
        "track",
        "TRK-CANARY",
        {
            "title": "Rehearse native delivery on a disposable canary",
            "charter": "Carry one Milestone from creation to acceptance on native authority.",
            "scope": {"scope_kind": "repository", "repository_ref": walker.repository},
            "owner": {"principal_kind": "operator", "principal_id": OPERATOR},
            "policy": {
                "revision": 1,
                "wip": {"active_milestones": 1, "active_batches_per_repo": 1},
                "ownership_principal": {"principal_kind": "operator", "principal_id": OPERATOR},
                "permitted_milestone_kinds": ["product"],
                "campaign_templates": [],
                "outcome_metrics": [],
                "promotion_rules": [],
                "integration_priority": 50,
                "presentation": {"default_view": "roadmap", "color_token": "accent_blue"},
            },
        },
    )
    milestone = walker.create(
        "milestone",
        "MLS-0001",
        {
            "primary_track_ref": track,
            "title": "Deliver one change through the native path",
            "outcome": "The delivered module assigns the new value on the canary's main line.",
            "appetite": "S",
            "exclusions": ["any provider process"],
            "acceptance_journey": [
                {
                    "step_id": "AS-01",
                    "actor": "operator",
                    "action": "read the delivered module at the integrated head",
                    "expected_observation": "the module assigns the delivered value",
                    "evidence_kinds": ["artifact"],
                }
            ],
        },
    )
    walker.walk.milestone_urn = milestone
    task = walker.urn("task", f"{code}-0001")
    batch = walker.create(
        "batch",
        "BAT-0001",
        {"milestone_ref": milestone, "repository_ref": walker.repository, "task_refs": [task]},
    )
    walker.create("task", f"{code}-0001", {"priority": "P1", "intent": "Deliver the new value"})
    exit_task = walker.create(
        "task", f"{code}-0002", {"priority": "P2", "intent": "Repair a conflicting delivery"}
    )
    run = walker.create(
        "run",
        "RUN-00000001",
        {
            "scope": {
                "scope_kind": "task",
                "purpose": "implement",
                "task_ref": task,
                "write_set": ["src"],
            }
        },
    )
    walker.move("domain.milestone.activate", milestone, "the Milestone opens under its Track")
    walker.move(
        "domain.batch.activate",
        batch,
        "the Batch pins the branch it integrates into",
        updates={"target_branch": TARGET_BRANCH},
    )
    walker.move(
        "domain.task.promote",
        task,
        "the Task is placed in the Batch with its criterion",
        updates={
            "batch_ref": batch,
            "criteria": [criterion().model_dump(mode="json")],
            "due_scope": milestone,
        },
    )
    return Planned(milestone=milestone, batch=batch, task=task, exit_task=exit_task, run=run)


def _dispatch_and_seal(walker: Walker, planned: Planned) -> None:
    """Dispatch the Task through ``/dispatch`` and seal the worker's candidate."""
    task, run = planned.task, planned.run
    dispatched = walker.skill(
        "/dispatch",
        batch_ref=planned.batch,
        task=[task],
        run=run,
        run_request=dispatch_request(run, task),
    )
    assert dispatched["dispatched"], dispatched
    walker.record(
        "dispatch the Task's Run",
        "/dispatch --task --run --run-request",
        "skill",
        str(dispatched["outcome"]),
        "the skill read the Run and sent the operator-compiled request to runtime.run.dispatch",
    )
    walker.transition(task, "CLAIMED", "the dispatched Run holds the Task's lease")
    walker.move(
        "domain.task.start",
        task,
        "the Task runs under the dispatched Run",
        updates={"active_run_ref": run},
    )

    submission_ref, tree_digest = worker_commit(walker, run)
    walker.verb(
        "runtime.candidate.submit",
        urn=run,
        actor=WORKER,
        idempotency_key="canary-submit",
        task_ref=task,
        submission_ref=submission_ref,
        changed_paths=[WORKED_PATH],
        resulting_tree_digest=tree_digest,
    )
    walker.record(
        "submit the candidate",
        "runtime.candidate.submit",
        "verb",
        "submitted",
        "the worker submits from inside its lease; no skill acts as the worker",
    )
    sealed = walker.verb(
        "runtime.candidate.report.bind",
        urn=run,
        actor=OPERATOR,
        idempotency_key="canary-report",
        candidate_ref=candidate_identity(task_ref=task, resulting_tree_digest=tree_digest),
        report_schema_ref="schema://executor-report/v1",
        report_digest=canonical_digest({"report": "canary", "tree": tree_digest}),
        verdict="pass",
        resulting_tree_digest=tree_digest,
    )
    assert sealed["sealed"] is True, sealed
    walker.record(
        "bind the Run's report and seal the candidate",
        "runtime.candidate.report.bind",
        "verb",
        "sealed",
        "the /integrate seal branch still stops on candidate_report_unbound",
    )
    walker.transition(
        task,
        "READY_TO_INTEGRATE",
        "the Run's report is bound, so the Task's work is ready to integrate",
        observations=["run_report_bound"],
    )


def _integrate_and_verify(walker: Walker, planned: Planned) -> dict[str, Any]:
    """Integrate and verify the Batch through the skills, and ready it for review.

    Returns:
        The exact tree the acceptance will be taken on.
    """
    batch = planned.batch
    diagnostic = walker.file_evidence(
        "EVD-0001", kind="artifact", summary="where a delivery conflict would be diagnosed"
    )
    integrated = walker.skill(
        "/integrate",
        action="apply",
        subject_ref=batch,
        base=base_binding(walker, batch),
        exit={"repair_task": planned.exit_task, "rebase_task": planned.exit_task},
        diagnostic=diagnostic,
    )
    assert integrated["outcome"] == "integrated", integrated
    walker.walk.generation_ids = tuple(integrated["generation_ids"])
    walker.record(
        "integrate the Batch",
        "/integrate apply --base --exit --diagnostic",
        "skill",
        "integrated",
        "the skill asked runtime.delivery.assemble for the request and sent it to "
        "runtime.delivery.integrate",
    )

    verified = walker.skill("/verify", subject_ref=batch, mode="all")
    assert verified["merge_ready"] is True, verified
    walker.record(
        "verify the Batch on its delivered head",
        "/verify --mode all",
        "skill",
        str(verified["outcome"]),
        "the skill walked runtime.delivery.verify_batch to ready_to_merge",
    )

    delivered = _delivered_head(walker)
    walker.walk.delivered_head = delivered
    binding = accepted_binding(walker, delivered, planned.task)
    walker.move(
        "domain.batch.ready",
        batch,
        "the Batch records the verified head it is ready to merge at",
        updates={"current_head_binding": binding},
    )
    walker.move(
        "domain.milestone.open_review",
        planned.milestone,
        "every Batch of the Milestone stands verified",
        updates={"acceptance_bundle_revision": 1},
    )
    return binding


def _accept(walker: Walker, planned: Planned, binding: dict[str, Any]) -> None:
    """Ask through ``/verify``, seal the operator's answer, and accept the Milestone."""
    milestone = planned.milestone
    shown = walker.file_evidence(
        "EVD-0002", kind="artifact", summary="the delivered module assigns the new value"
    )

    asked = walker.skill(
        "/verify",
        subject_ref=planned.batch,
        mode="all",
        milestone=milestone,
        journey=[
            {
                "step_id": "AS-01",
                "passed": True,
                "observation": "the delivered module assigns the new value",
                "evidence_kinds": ["artifact"],
                "evidence_refs": [shown],
            }
        ],
        accepted_binding=binding,
        requested_by=OPERATOR_PRINCIPAL,
    )
    assert asked["approval_ref"], asked
    walker.record(
        "ask the operator to accept the Milestone",
        "/verify --mode all --milestone --journey --accepted-binding --requested-by",
        "skill",
        str(asked["outcome"]),
        "the skill opened runtime.delivery.open_acceptance_approval once the Batch cleared",
    )

    receipt = walker.file_evidence(
        "EVD-0003", kind="decision", summary="the operator approved the acceptance bundle"
    )
    approval_ref = str(asked["approval_ref"])
    sealed_action = walker.verb(
        "runtime.delivery.seal_acceptance_approval",
        urn=approval_ref,
        expected_revision=walker.revision(approval_ref),
        idempotency_key="canary-seal",
        actor=OPERATOR,
        resolver=OPERATOR_PRINCIPAL,
        option_id="approve",
        receipt_ref=receipt,
    )
    walker.record(
        "seal the operator's answer",
        "runtime.delivery.seal_acceptance_approval",
        "verb",
        str(sealed_action["status"]).lower(),
        "the answer is a person's, so no skill seals it",
    )
    accepted = walker.domain(
        "domain.milestone.accept",
        urn=milestone,
        expected_revision=walker.revision(milestone),
        updates={"accepted_binding": binding},
        approval_receipt_ref=approval_ref,
        acceptance_bundle=asked["acceptance_bundle"],
    )
    walker.record(
        "accept the Milestone on the sealed approval",
        "domain.milestone.accept",
        "verb",
        "accepted",
        "acceptance is the operator's move, taken on the bundle the approval covers",
    )
    walker.walk.accepted_at = datetime.now(UTC)
    walker.walk.approval_ref = approval_ref
    walker.walk.bundle = MilestoneAcceptanceBundle.model_validate(asked["acceptance_bundle"])
    walker.walk.milestone_status = str(walker.stored(milestone)["status"])
    assert accepted["revision_after"] > 0


def _delivered_head(walker: Walker) -> str:
    """Return the commit the Batch's one delivery pinned."""
    refs = git(walker.root, "for-each-ref", "--format=%(refname)", "refs/eawf/deliveries/")
    pins = [line for line in refs.splitlines() if line]
    assert len(pins) == 1, pins
    assert delivery_pin_ref(pins[0].rsplit("/", 1)[1]) == pins[0]
    return git(walker.root, "rev-parse", pins[0])


def walk_record(walk: CanaryWalk) -> dict[str, Any]:
    """Return the committed record of *walk*, derived from what it produced.

    Args:
        walk: A walk that reached acceptance.

    Returns:
        The record document: the canary, the accepted Milestone, the
        bundle and its digest, the delivery, and every step.
    """
    assert walk.bundle is not None and walk.accepted_at is not None, "the walk did not accept"
    ref = walk.canary.ref
    return {
        "schema_version": WALK_SCHEMA_VERSION,
        "walked_on": walk.accepted_at.date().isoformat(),
        "canary": {
            "project_code": ref.project_code,
            "repository": str(ref.repository),
            "generation_id": walk.canary.generation_id,
            "epoch": 2,
            "runtime_dir": RUNTIME_DIR_NOTE,
        },
        "milestone": {
            "urn": walk.milestone_urn,
            "key": walk.milestone_urn.rsplit("/", 1)[1],
            "status": walk.milestone_status,
            "reference": walk.bundle_reference,
            "approval_ref": walk.approval_ref,
        },
        "bundle_digest": walk.bundle.digest(),
        "acceptance_bundle": walk.bundle.model_dump(mode="json"),
        "delivery": {
            "generation_ids": list(walk.generation_ids),
            "delivered_head": walk.delivered_head,
        },
        "steps": [step.as_row() for step in walk.steps],
        "launcher": LAUNCHER_NOTE,
        "provider_processes_started": 0,
    }


def evidence_with(evidence: dict[str, Any], walk: CanaryWalk) -> dict[str, Any]:
    """Return the canary evidence export with *walk*'s canary and Milestone recorded.

    A Milestone is recorded only inside a canary the export declares, so
    the walk's canary is declared beside it. A Milestone the export
    already records under the same reference is replaced, never doubled.

    Args:
        evidence: The committed export, decoded.
        walk: A walk that reached acceptance.

    Returns:
        The updated export.
    """
    assert walk.bundle is not None and walk.accepted_at is not None, "the walk did not accept"
    ref = walk.canary.ref
    declared = {"project_code": ref.project_code, "repository": str(ref.repository)}
    canaries = [row for row in evidence["canaries"] if row["project_code"] != ref.project_code]
    milestones = [
        row for row in evidence["milestones"] if row["reference"] != walk.bundle_reference
    ]
    return {
        **evidence,
        "canaries": [*canaries, declared],
        "milestones": [
            *milestones,
            {
                "reference": walk.bundle_reference,
                "project_code": ref.project_code,
                "milestone_id": walk.milestone_urn.rsplit("/", 1)[1],
                "status": walk.milestone_status,
                "bundle_digest": walk.bundle.digest(),
                "recorded_at": walk.accepted_at.isoformat().replace("+00:00", "Z"),
            },
        ],
    }


__all__ = [
    "LAUNCHER_NOTE",
    "LEDGER_COMMIT_SURFACE",
    "PROJECT_CODE",
    "RUNTIME_DIR_NOTE",
    "WALK_SCHEMA_VERSION",
    "CanaryWalk",
    "WalkStep",
    "evidence_with",
    "walk_canary",
    "walk_record",
]
