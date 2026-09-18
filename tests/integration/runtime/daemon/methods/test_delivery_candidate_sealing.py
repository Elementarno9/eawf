"""RUN-003 and DEL-003: a candidate survives the provider that made it.

Every case runs against a provisioned canary whose Run has been
dispatched for real -- compiled, leased, spawned and announced -- so the
lease the seal checks read is a lease the daemon issued over a real
worktree, and the records left behind are the ones a caller on the socket
would produce.

``executor-success.json`` drives the forward path: one immutable
submission with its report binding pending, then an accepted report, then
a bundle. ``submission-before-loss.json`` drives the loss: the submission
is filed, the provider is gone, and the retry cannot prove continuity, so
W74's resume guards link a recovery Run to the predecessor. The recovery
Run then presents the same work and the ledger still holds one candidate.

Nothing here sleeps and nothing polls. The provider loss is a retry
offered no continuity proof rather than a wait, and every stamp comes
from the ``now`` each call is handed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

import eawf.runtime.daemon.methods.candidate  # noqa: F401  -- registers runtime.candidate.*
from eawf.kernel.runtime.candidate import (
    CandidateRefusal,
    CandidateSubmission,
    SealCheck,
    candidate_identity,
)
from eawf.kernel.store.ledger import LedgerRecord
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.candidate.seal import binding_of, bundle_of, submission_of
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.candidate import (
    CANDIDATE_REPORT_BIND_METHOD,
    CANDIDATE_SUBMIT_METHOD,
)
from eawf.runtime.daemon.native_dispatch import DispatchStage, active_lease_of, standing_attempt
from eawf.runtime.daemon.native_retry import (
    ResumeGuard,
    RetryDisposition,
    RetryParams,
    lineage_of,
    retry_run,
)
from tests.integration.runtime.daemon.test_native_dispatch import (
    ACTOR,
    RUN_URN,
    SUCCESSOR_KEY,
    TASK_URN,
    LedgerReadingLauncher,
    call_verb,
    dispatch,
    ledger_records,
    make_canary,
    make_repo,
    method_ctx,
    root_ctx,
    run_row,
    run_urn,
)

pytestmark = pytest.mark.integration


FIXTURE_ROOT: Final = Path(__file__).resolve().parents[4] / "fixtures" / "runtime_contract" / "v1"


def fixture(name: str) -> dict[str, Any]:
    """Return one runtime-delivery fixture body."""
    return json.loads((FIXTURE_ROOT / f"{name}.json").read_text(encoding="utf-8"))


SUCCESS: Final = fixture("executor-success")
LOSS: Final = fixture("submission-before-loss")

#: The unsealing cases the two verbs can actually reach. The rest move a
#: lease field the daemon derives itself, so they are driven against the
#: decision directly in the unit suite rather than faked here.
REACHABLE: Final = [case for case in SUCCESS["unsealing"] if case["reachable_through_verbs"]]


def seeded_rows() -> dict[str, dict[str, Any]]:
    """Return the dispatched Run and the Run a linked retry runs as."""
    return {run_urn().entity_key: run_row(), SUCCESSOR_KEY: run_row(key=SUCCESSOR_KEY)}


def dispatched(tmp_path: Path) -> tuple[CanaryProvision, Path, MethodContext]:
    """Return a canary whose Run has been dispatched all the way through."""
    canary = make_canary(tmp_path / "repo", rows=seeded_rows())
    runtime = tmp_path / "runtime"
    answer = dispatch(
        method_ctx(runtime),
        canary,
        LedgerReadingLauncher(canary, runtime),
        now=datetime.now(UTC),
    )
    assert answer["stage"] == DispatchStage.ANNOUNCED.value
    return canary, runtime, method_ctx(runtime)


def submit_params(
    canary: CanaryProvision, *, claim: dict[str, Any], **overrides: Any
) -> dict[str, Any]:
    """Return the wire params of one submission of *claim*."""
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": str(RUN_URN),
        "actor": ACTOR,
        "idempotency_key": "candidate-01",
        "task_ref": TASK_URN,
        "submission_ref": claim["submission_ref"],
        "changed_paths": list(claim["changed_paths"]),
        "resulting_tree_digest": claim["resulting_tree_digest"],
    }
    params.update(overrides)
    return params


def report_params(
    canary: CanaryProvision, *, claim: dict[str, Any], report: dict[str, Any], **overrides: Any
) -> dict[str, Any]:
    """Return the wire params of one report acceptance for *claim*."""
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": str(RUN_URN),
        "actor": ACTOR,
        "idempotency_key": "report-01",
        "candidate_ref": candidate_identity(
            task_ref=TASK_URN, resulting_tree_digest=claim["resulting_tree_digest"]
        ),
        "report_schema_ref": report["report_schema_ref"],
        "report_digest": report["report_digest"],
        "verdict": report["verdict"],
        "resulting_tree_digest": report["resulting_tree_digest"],
    }
    params.update(overrides)
    return params


def submissions_on(canary: CanaryProvision, runtime: Path) -> tuple[CandidateSubmission, ...]:
    """Return every candidate submission line the canary holds, in order."""
    return tuple(
        CandidateSubmission.model_validate(item.payload)
        for item in ledger_records(canary, runtime)
        if item.payload.get("payload_kind") == "candidate_submission"
    )


def records_of(canary: CanaryProvision, runtime: Path) -> tuple[LedgerRecord, ...]:
    """Return every line the canary's run ledger holds."""
    return ledger_records(canary, runtime)


def submitted(
    tmp_path: Path, *, claim: dict[str, Any]
) -> tuple[CanaryProvision, Path, MethodContext, dict[str, Any]]:
    """Dispatch a Run, submit *claim* from it, and return what was recorded."""
    canary, runtime, ctx = dispatched(tmp_path)
    answer = call_verb(CANDIDATE_SUBMIT_METHOD, ctx, submit_params(canary, claim=claim))
    return canary, runtime, ctx, answer


# ---- the verbs a caller on the socket reaches -------------------------------


def test_both_candidate_verbs_are_registered_behind_the_native_fence() -> None:
    """The submission and the acceptance are reachable as daemon verbs."""
    registered = methods.registered_methods()

    assert CANDIDATE_SUBMIT_METHOD in registered
    assert CANDIDATE_REPORT_BIND_METHOD in registered


def test_candidate_submit_refuses_an_epoch_one_tree(tmp_path: Path) -> None:
    """A tree with no epoch-2 authority never records a claim."""
    repo = tmp_path / "plain"
    make_repo(repo)
    (repo / ".ea").mkdir(exist_ok=True)

    with pytest.raises(DaemonValidationError, match="native_authority_required"):
        call_verb(
            CANDIDATE_SUBMIT_METHOD,
            method_ctx(tmp_path / "runtime"),
            {"repo_root": str(repo), "urn": str(RUN_URN), "actor": ACTOR, "idempotency_key": "k"},
        )


def test_candidate_submit_refuses_params_it_does_not_declare(tmp_path: Path) -> None:
    """An unknown field is refused by the strict parameter model."""
    canary, _runtime, ctx = dispatched(tmp_path)
    params = submit_params(canary, claim=SUCCESS["submission"])
    params["unexpected"] = True

    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call_verb(CANDIDATE_SUBMIT_METHOD, ctx, params)


def test_candidate_submit_refuses_an_empty_changed_path_list(tmp_path: Path) -> None:
    """A claim that touched nothing is not a candidate for anything."""
    canary, _runtime, ctx = dispatched(tmp_path)

    with pytest.raises(DaemonValidationError, match="changed_paths"):
        call_verb(
            CANDIDATE_SUBMIT_METHOD,
            ctx,
            submit_params(canary, claim=SUCCESS["submission"], changed_paths=[]),
        )


# ---- the submission is immutable and binds no report ------------------------


def test_submit_records_one_immutable_claim_with_its_binding_pending(tmp_path: Path) -> None:
    """The claim is filed exactly as made, and it names no report."""
    canary, runtime, _ctx, answer = submitted(tmp_path, claim=SUCCESS["submission"])

    assert answer["report_binding"] == SUCCESS["expected"]["report_binding"]
    assert answer["replayed"] is False
    standing = submissions_on(canary, runtime)
    assert len(standing) == 1
    assert standing[0].report_binding == "pending"
    assert standing[0].changed_paths == tuple(SUCCESS["submission"]["changed_paths"])
    assert standing[0].resulting_tree_digest == SUCCESS["submission"]["resulting_tree_digest"]


def test_the_daemon_fills_the_workspace_facts_the_worker_never_names(tmp_path: Path) -> None:
    """The lease, its generation and its base commit come off the lease."""
    canary, runtime, _ctx, _answer = submitted(tmp_path, claim=SUCCESS["submission"])
    lease = active_lease_of(root_ctx(canary, runtime), run_ref=str(RUN_URN), now=datetime.now(UTC))
    standing = submissions_on(canary, runtime)[0]

    assert lease is not None
    assert standing.lease_id == lease.lease_id
    assert standing.workspace_handle == lease.workspace_handle
    assert standing.workspace_generation == lease.workspace_generation
    assert standing.base_commit == lease.base_commit


def test_no_bundle_is_sealed_before_a_report_is_bound(tmp_path: Path) -> None:
    """A submission alone seals nothing, whatever it claims."""
    canary, runtime, _ctx, answer = submitted(tmp_path, claim=SUCCESS["submission"])
    records = records_of(canary, runtime)

    assert bundle_of(records, answer["candidate_ref"]) is None
    assert binding_of(records, answer["candidate_ref"]) is None


def test_a_submission_from_a_run_holding_no_lease_is_refused(tmp_path: Path) -> None:
    """A Run with no workspace has nothing whose contents it could claim."""
    canary, _runtime, ctx = dispatched(tmp_path)

    with pytest.raises(DaemonValidationError, match=CandidateRefusal.LEASE_ABSENT.value):
        call_verb(
            CANDIDATE_SUBMIT_METHOD,
            ctx,
            submit_params(canary, claim=SUCCESS["submission"], urn=str(run_urn(SUCCESSOR_KEY))),
        )


def test_a_submission_naming_another_task_than_the_lease_is_refused(tmp_path: Path) -> None:
    """A claim cannot file work under a Task the workspace is not for."""
    canary, _runtime, ctx = dispatched(tmp_path)

    with pytest.raises(DaemonValidationError, match=CandidateRefusal.PAYLOAD_CONFLICT.value):
        call_verb(
            CANDIDATE_SUBMIT_METHOD,
            ctx,
            submit_params(
                canary,
                claim=SUCCESS["submission"],
                task_ref=TASK_URN.replace("EAWF-0042", "EAWF-0043"),
            ),
        )


def test_one_identity_naming_other_content_is_refused(tmp_path: Path) -> None:
    """Two different claims under one identity is not a replay."""
    canary, _runtime, ctx, _answer = submitted(tmp_path, claim=SUCCESS["submission"])

    with pytest.raises(DaemonValidationError, match=CandidateRefusal.PAYLOAD_CONFLICT.value):
        call_verb(
            CANDIDATE_SUBMIT_METHOD,
            ctx,
            submit_params(
                canary,
                claim=SUCCESS["submission"],
                changed_paths=["src/other.py"],
                idempotency_key="candidate-02",
            ),
        )


# ---- executor-success.json reaches a sealed bundle --------------------------


def test_executor_success_reaches_a_sealed_bundle(tmp_path: Path) -> None:
    """The fixture's claim and report seal, and the bundle is durable."""
    canary, runtime, ctx, _answer = submitted(tmp_path, claim=SUCCESS["submission"])

    sealed = call_verb(
        CANDIDATE_REPORT_BIND_METHOD,
        ctx,
        report_params(canary, claim=SUCCESS["submission"], report=SUCCESS["report"]),
    )

    assert sealed["sealed"] is SUCCESS["expected"]["sealed"]
    assert sealed["failed_checks"] == SUCCESS["expected"]["failed_checks"]
    bundle = bundle_of(records_of(canary, runtime), sealed["candidate_ref"])
    assert bundle is not None
    assert bundle.checks_passed == tuple(SealCheck)
    assert bundle.verdict.value == SUCCESS["report"]["verdict"]


def test_the_seal_leaves_the_submission_exactly_as_it_was(tmp_path: Path) -> None:
    """Sealing writes a third record; it never edits the worker's claim."""
    canary, runtime, ctx, _answer = submitted(tmp_path, claim=SUCCESS["submission"])
    before = submissions_on(canary, runtime)[0]

    call_verb(
        CANDIDATE_REPORT_BIND_METHOD,
        ctx,
        report_params(canary, claim=SUCCESS["submission"], report=SUCCESS["report"]),
    )

    after = submissions_on(canary, runtime)
    assert len(after) == 1
    assert after[0] == before
    assert after[0].report_binding == "pending"


def test_accepting_one_report_twice_replays_the_seal(tmp_path: Path) -> None:
    """A second acceptance answers from the bundle and seals nothing twice."""
    canary, runtime, ctx, _answer = submitted(tmp_path, claim=SUCCESS["submission"])
    params = report_params(canary, claim=SUCCESS["submission"], report=SUCCESS["report"])
    first = call_verb(CANDIDATE_REPORT_BIND_METHOD, ctx, params)

    second = call_verb(CANDIDATE_REPORT_BIND_METHOD, ctx, params)

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["bundle"] == first["bundle"]
    bundles = [
        item
        for item in records_of(canary, runtime)
        if item.payload.get("payload_kind") == "candidate_bundle"
    ]
    assert len(bundles) == 1


def test_a_report_for_a_candidate_nobody_submitted_is_refused(tmp_path: Path) -> None:
    """There is nothing for an acceptance with no claim to be about."""
    canary, _runtime, ctx = dispatched(tmp_path)

    with pytest.raises(DaemonValidationError, match=CandidateRefusal.SUBMISSION_ABSENT.value):
        call_verb(
            CANDIDATE_REPORT_BIND_METHOD,
            ctx,
            report_params(canary, claim=SUCCESS["submission"], report=SUCCESS["report"]),
        )


def test_a_second_different_report_for_one_candidate_is_refused(tmp_path: Path) -> None:
    """Replacing bound evidence would move a seal decision after the fact."""
    canary, _runtime, ctx, _answer = submitted(tmp_path, claim=SUCCESS["submission"])
    refusing = {**SUCCESS["report"], "verdict": "blocked"}
    call_verb(
        CANDIDATE_REPORT_BIND_METHOD,
        ctx,
        report_params(canary, claim=SUCCESS["submission"], report=refusing),
    )

    with pytest.raises(DaemonValidationError, match=CandidateRefusal.BINDING_CONFLICT.value):
        call_verb(
            CANDIDATE_REPORT_BIND_METHOD,
            ctx,
            report_params(canary, claim=SUCCESS["submission"], report=SUCCESS["report"]),
        )


@pytest.mark.parametrize("case", REACHABLE, ids=lambda case: str(case["case_id"]))
def test_each_failed_check_leaves_the_submission_unsealed(
    tmp_path: Path, case: dict[str, Any]
) -> None:
    """One moved fact refuses the seal and names exactly what did not hold."""
    claim = dict(SUCCESS["submission"])
    report = dict(SUCCESS["report"])
    if case["mutation"] == "changed_paths":
        claim["changed_paths"] = case["value"]
    elif case["mutation"] == "verdict":
        report["verdict"] = case["value"]
    elif case["mutation"] == "report_resulting_tree_digest":
        report["resulting_tree_digest"] = case["value"]

    canary, runtime, ctx, _answer = submitted(tmp_path, claim=claim)
    if case["mutation"] == "omit_report":
        records = records_of(canary, runtime)
        candidate_ref = candidate_identity(
            task_ref=TASK_URN, resulting_tree_digest=claim["resulting_tree_digest"]
        )
        assert bundle_of(records, candidate_ref) is None
        return
    answer = call_verb(
        CANDIDATE_REPORT_BIND_METHOD, ctx, report_params(canary, claim=claim, report=report)
    )

    assert answer["sealed"] is False
    assert answer["failed_checks"] == case["failed_checks"]
    assert answer["bundle"] is None
    assert bundle_of(records_of(canary, runtime), answer["candidate_ref"]) is None
    assert submission_of(records_of(canary, runtime), answer["candidate_ref"]) is not None


# ---- submission-before-loss.json: the candidate outlives the provider -------


def retry(canary: CanaryProvision, runtime: Path, **fields: Any) -> Any:
    """Drive one retry decision against the dispatched canary."""
    params = RetryParams.model_validate(
        {"urn": str(RUN_URN), "actor": ACTOR, "idempotency_key": "retry-01", **fields}
    )
    return retry_run(root_ctx(canary, runtime), params, now=datetime.now(UTC))


def test_a_submission_made_before_provider_loss_survives_the_retry(tmp_path: Path) -> None:
    """The claim is durable, so losing the provider does not lose the work."""
    canary, runtime, _ctx, answer = submitted(tmp_path, claim=LOSS["submission"])
    before = submissions_on(canary, runtime)

    retry(canary, runtime, successor_urn=str(run_urn(SUCCESSOR_KEY)))

    after = submissions_on(canary, runtime)
    assert LOSS["expected"]["submission_survives"] is True
    assert after == before
    assert after[0].candidate_ref == answer["candidate_ref"]
    assert after[0].report_binding == LOSS["expected"]["report_binding"]


def test_the_loss_creates_a_recovery_run_linked_to_its_predecessor(
    tmp_path: Path,
) -> None:
    """The recovery Run is W74's linked successor, not a second mechanism."""
    canary, runtime, _ctx, _answer = submitted(tmp_path, claim=LOSS["submission"])

    decided = retry(canary, runtime, successor_urn=str(run_urn(SUCCESSOR_KEY)))

    assert decided.disposition.value == LOSS["loss"]["disposition"]
    assert [guard.value for guard in decided.unmet_guards] == LOSS["loss"]["unmet_guards"]
    lineage = lineage_of(records_of(canary, runtime), run_urn(SUCCESSOR_KEY))
    assert lineage is not None
    assert lineage.retry_of_run_ref == run_urn()
    assert lineage.run_ref == run_urn(SUCCESSOR_KEY)


def test_the_recovery_run_replays_the_candidate_rather_than_duplicating_it(
    tmp_path: Path,
) -> None:
    """One Task and one resulting tree is one candidate, however often retried."""
    canary, runtime, ctx, first = submitted(tmp_path, claim=LOSS["submission"])
    retry(canary, runtime, successor_urn=str(run_urn(SUCCESSOR_KEY)))

    replayed = call_verb(
        CANDIDATE_SUBMIT_METHOD,
        ctx,
        submit_params(
            canary,
            claim=LOSS["submission"],
            urn=str(run_urn(SUCCESSOR_KEY)),
            idempotency_key="candidate-recovery",
        ),
    )

    assert replayed["replayed"] is LOSS["recovery"]["replayed"]
    assert replayed["candidate_ref"] == first["candidate_ref"]
    assert replayed["run_ref"] == str(RUN_URN)
    assert len(submissions_on(canary, runtime)) == LOSS["recovery"]["submission_lines"]


def test_the_recovery_run_replays_even_though_it_holds_no_lease(tmp_path: Path) -> None:
    """A replay writes nothing, so it needs no workspace of its own."""
    canary, runtime, ctx, _first = submitted(tmp_path, claim=LOSS["submission"])
    successor_lease = active_lease_of(
        root_ctx(canary, runtime), run_ref=str(run_urn(SUCCESSOR_KEY)), now=datetime.now(UTC)
    )

    replayed = call_verb(
        CANDIDATE_SUBMIT_METHOD,
        ctx,
        submit_params(
            canary,
            claim=LOSS["submission"],
            urn=str(run_urn(SUCCESSOR_KEY)),
            idempotency_key="candidate-recovery",
        ),
    )

    assert successor_lease is None
    assert replayed["replayed"] is True


def test_the_candidate_stays_unsealed_across_the_loss(tmp_path: Path) -> None:
    """No report was ever accepted, so the three binding checks still fail."""
    canary, runtime, _ctx, answer = submitted(tmp_path, claim=LOSS["submission"])

    retry(canary, runtime, successor_urn=str(run_urn(SUCCESSOR_KEY)))

    records = records_of(canary, runtime)
    assert LOSS["expected"]["sealed"] is False
    assert bundle_of(records, answer["candidate_ref"]) is None
    assert submission_of(records, answer["candidate_ref"]) is not None


def test_a_resume_that_proves_continuity_writes_no_second_candidate(
    tmp_path: Path,
) -> None:
    """The other retry disposition also leaves exactly one candidate."""
    canary, runtime, ctx, first = submitted(tmp_path, claim=LOSS["submission"])
    attempt = standing_attempt(records_of(canary, runtime), run_urn())
    assert attempt is not None

    decided = retry(
        canary,
        runtime,
        continuity={
            "provider_session_ref": attempt.provider_session_ref,
            "compiled_spec_digest": attempt.compiled_spec_digest,
        },
    )
    replayed = call_verb(
        CANDIDATE_SUBMIT_METHOD,
        ctx,
        submit_params(canary, claim=LOSS["submission"], idempotency_key="candidate-resume"),
    )

    assert decided.disposition is RetryDisposition.RESUME_SAME_RUN
    assert ResumeGuard.PROVIDER_SESSION_CONTINUITY not in decided.unmet_guards
    assert replayed["replayed"] is True
    assert len(submissions_on(canary, runtime)) == 1
    assert replayed["candidate_ref"] == first["candidate_ref"]
