"""A Run's report reaches its candidate's seal and its Milestone's question.

Driven over the registered daemon verbs on a disposable canary, the way
the canary acceptance walk drives them, with every step a worker or a
skill takes rather than a direct verb call. The worker files its terminal
report through ``submit_report`` on the semantic gateway, so the accepted
report is on the Run before anyone seals. ``/integrate seal`` is then
presented only the Run and the candidate's resulting tree -- no report
schema, digest or verdict -- and the daemon binds the Run's accepted
report and seals the candidate, where a seal missing that report would
stop on ``candidate_report_unbound``. The Batch is integrated and
verified through the skills, and the ``/verify`` pass naming the
Milestone opens the acceptance approval itself.

Nothing here stands in for the report handler. With ``submit_report``
unregistered the gateway refuses the worker's call, the Run holds no
accepted report, and the seal cannot resolve the fields it was not
presented, so this suite reds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import eawf.runtime.daemon.methods.candidate
import eawf.runtime.daemon.methods.delivery
import eawf.runtime.daemon.methods.delivery_acceptance
import eawf.runtime.daemon.methods.domain
import eawf.runtime.daemon.methods.projection
import eawf.runtime.daemon.methods.run
import eawf.runtime.daemon.methods.semantic  # noqa: F401  (registers the verbs the walk reaches)
from eawf.kernel.delivery.receipts import canonical_digest
from eawf.kernel.runtime.candidate import candidate_identity
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.semantic import SemanticCall
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import native_dispatch
from eawf.runtime.daemon.methods.semantic import SEMANTIC_CALL_METHOD
from eawf.runtime.daemon.native_dispatch import compile_launchers
from eawf.runtime.runtimes.adapter import NativeLaunchOutcome, NativeLaunchRequest
from tests.integration.workflow.release._canary_acceptance_walk import (
    OPERATOR_PRINCIPAL,
    WORKED_PATH,
    WORKER,
    Planned,
    StandInLauncher,
    Walker,
    _integrate_and_verify,
    _plan,
    dispatch_request,
    worker_commit,
)

pytestmark = pytest.mark.integration


class CapsuleCapturingLauncher(StandInLauncher):
    """The walk's stand-in launcher, keeping the capsule the Run was sealed under.

    A semantic call must present the exact capsule its Run is bound to,
    and the daemon stores only that capsule's digest, so the one place a
    worker's copy exists is the launch request.

    Attributes:
        capsule: The capsule the last launch carried, or ``None``.
    """

    def __init__(self) -> None:
        """Start having launched nothing."""
        super().__init__()
        self.capsule: AuthorityCapsule | None = None

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Keep the capsule, then answer the handshake as the stand-in does."""
        self.capsule = request.capsule
        return await super().launch(request)


def run_ledger_kinds(walker: Walker, run_urn: str) -> list[str]:
    """Return the payload kind of every line the canary's run ledger holds."""
    with walker.context.session([run_urn]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    return [str(item.payload.get("payload_kind", "")) for item in records]


def dispatch(walker: Walker, planned: Planned) -> None:
    """Dispatch the Task through ``/dispatch`` with a grant to file its report."""
    request = dispatch_request(planned.run, planned.task)
    request["capsule"]["tool_grants"] = ["budget_status", "submit_candidate", "submit_report"]
    dispatched = walker.skill(
        "/dispatch", batch_ref=planned.batch, task=[planned.task], run=planned.run,
        run_request=request,
    )  # fmt: skip
    assert dispatched["dispatched"], dispatched
    walker.transition(planned.task, "CLAIMED", "the dispatched Run holds the Task's lease")
    walker.move(
        "domain.task.start",
        planned.task,
        "the Task runs under the dispatched Run",
        updates={"active_run_ref": planned.run},
    )
    # The stand-in answered the handshake but no worker takes the Run up,
    # and the gateway answers only a running Run.
    walker.transition(
        planned.run,
        "RUNNING",
        "the worker took the Run up",
        updates={"started_at": datetime.now(UTC).isoformat()},
    )


def submit_report(walker: Walker, run_urn: str, capsule: AuthorityCapsule) -> dict[str, Any]:
    """File the Run's terminal report through the semantic gateway, as its worker does."""
    call = SemanticCall.seal(
        {
            "call_id": f"call-{1:016x}",
            "run_ref": run_urn,
            "contract_digest": capsule.contract_digest,
            "idempotency_key": "report-01",
            "tool_id": "submit_report",
            "tool_schema_version": "1.0.0",
            "payload": {
                "tool_id": "submit_report",
                "report_schema_ref": capsule.report_schema_ref,
                "contract_digest": capsule.contract_digest,
                "body_ref": "artifact://report/executor/ar-0001",
                "body_digest": canonical_digest({"report": "canary"}),
                "verdict": "pass",
            },
            "requested_at": datetime.now(UTC),
        }
    )
    return walker.verb(
        SEMANTIC_CALL_METHOD,
        call=call.model_dump(mode="json"),
        capsule=capsule.model_dump(mode="json"),
    )


def test_a_filed_report_seals_through_integrate_and_verify_opens_the_approval(
    tmp_path: Path,
) -> None:
    walker = Walker(tmp_path / "repo", tmp_path / "runtime")
    launcher = CapsuleCapturingLauncher()
    with mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
    ):
        planned = _plan(walker)
        dispatch(walker, planned)
        assert launcher.capsule is not None, "the dispatch launched no Run"

        submission_ref, tree_digest = worker_commit(walker, planned.run)
        walker.verb(
            "runtime.candidate.submit",
            urn=planned.run,
            actor=WORKER,
            idempotency_key=uuid.uuid4().hex,
            task_ref=planned.task,
            submission_ref=submission_ref,
            changed_paths=[WORKED_PATH],
            resulting_tree_digest=tree_digest,
        )
        filed = submit_report(walker, planned.run, launcher.capsule)
        assert filed["result"]["status"] == "succeeded", filed.get("refused_check")
        assert filed["result"]["bounded_output"]["accepted"] is True, filed
        assert run_ledger_kinds(walker, planned.run).count("accepted_report") == 1

        candidate = candidate_identity(task_ref=planned.task, resulting_tree_digest=tree_digest)
        sealed = walker.skill(
            "/integrate",
            action="seal",
            subject_ref=candidate,
            run=planned.run,
            resulting_tree_digest=tree_digest,
            expected_revision=walker.revision(planned.run),
        )
        assert sealed["outcome"] == "sealed", sealed
        assert sealed["refusal_code"] != "candidate_report_unbound"
        kinds = run_ledger_kinds(walker, planned.run)
        assert kinds.count("candidate_report_binding") == 1
        assert kinds.count("candidate_bundle") == 1

        resealed = walker.skill(
            "/integrate",
            action="seal",
            subject_ref=candidate,
            run=planned.run,
            resulting_tree_digest=tree_digest,
            expected_revision=walker.revision(planned.run),
        )
        assert resealed["outcome"] == "sealed", resealed
        assert run_ledger_kinds(walker, planned.run) == kinds

        walker.transition(
            planned.task,
            "READY_TO_INTEGRATE",
            "the Run's report is bound, so the Task's work is ready to integrate",
            observations=["run_report_bound"],
        )
        binding = _integrate_and_verify(walker, planned)
        shown = walker.file_evidence(
            "EVD-0002", kind="artifact", summary="the delivered module assigns the new value"
        )
        asked = walker.skill(
            "/verify",
            subject_ref=planned.batch,
            mode="all",
            milestone=planned.milestone,
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
            expected_revision=walker.revision(planned.milestone),
        )

    assert asked["outcome"] == "passed", asked
    assert asked["approval_ref"], asked
    assert asked["bundle_digest"], asked
    assert asked["unresolved_request_fields"] == []
    assert walker.stored(asked["approval_ref"])["status"] == "WAITING"
