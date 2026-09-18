"""RUN-011: after acceptance a retry keeps the Run only by proof.

Each case dispatches a Run for real -- compiled, leased, spawned and
announced against a provisioned canary -- and then retries it. Before the
provider accepted anything the queued Run is retried and nothing is
linked. Afterwards the decision belongs to the resume guards: every one
of them holding keeps the Run and spends a continuity attempt, and a
single unmet guard writes a lineage line naming the predecessor.

Every guard is driven both ways from the same dispatched Run, so a guard
that was wired to the wrong fact, or left out of the table, changes the
answer here rather than passing quietly. The clock is the ``now`` each
call is handed, which is how the resume window is crossed without waiting
for it.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.lease import LeaseStatus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_dispatch import DispatchRefusal, DispatchStage
from eawf.runtime.daemon.native_retry import (
    RESUME_GUARDS,
    ContinuityProof,
    ResumeGuard,
    ResumeInputs,
    RetryAnswer,
    RetryDisposition,
    RetryLineage,
    RetryParams,
    compile_resume_guards,
    lineage_of,
    retry_run,
    unmet_resume_guards,
)
from eawf.runtime.workspace.lease import revoke_lease, root_leases
from tests.integration.runtime.daemon.test_native_dispatch import (
    ACTOR,
    AT,
    RUN_KEY,
    RUN_URN,
    SUCCESSOR_KEY,
    DaemonDiedError,
    LedgerReadingLauncher,
    dispatch,
    ledger_records,
    make_canary,
    method_ctx,
    root_ctx,
    run_row,
    run_urn,
)

pytestmark = pytest.mark.integration


#: The provider session the dispatched launcher reports, which a resume
#: has to present back to prove continuity.
SESSION_REF: Final = "codex-session-1"


def seeded_rows() -> dict[str, dict[str, Any]]:
    """Return the predecessor and the successor a linked retry runs as."""
    return {
        RUN_KEY: run_row(),
        SUCCESSOR_KEY: run_row(key=SUCCESSOR_KEY),
    }


def dispatched(tmp_path: Path) -> tuple[Any, Path, dict[str, Any]]:
    """Return a canary whose Run has been dispatched all the way through."""
    canary = make_canary(tmp_path / "repo", rows=seeded_rows())
    runtime = tmp_path / "runtime"
    answer = dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))
    assert answer["stage"] == DispatchStage.ANNOUNCED.value
    return canary, runtime, answer


def retry(
    canary: Any,
    runtime: Path,
    *,
    now: Any = AT,
    ctx: MethodContext | None = None,
    **fields: Any,
) -> RetryAnswer:
    """Drive one retry decision against the dispatched canary."""
    params = RetryParams.model_validate(
        {"urn": str(RUN_URN), "actor": ACTOR, "idempotency_key": "retry-01", **fields}
    )
    context = (ctx or method_ctx(runtime)).native_root_context(canary.root / ".ea")
    return retry_run(context, params, now=now)


def full_proof(answer: dict[str, Any]) -> dict[str, Any]:
    """Return the continuity proof under which every resume guard holds."""
    return {
        "provider_session_ref": SESSION_REF,
        "compiled_spec_digest": answer["compiled_spec_digest"],
    }


# ---- before acceptance ------------------------------------------------------


def test_a_retry_before_acceptance_keeps_the_queued_run(tmp_path: Path) -> None:
    """Nothing was started, so there is no identity to preserve or link."""
    canary = make_canary(tmp_path / "repo", rows=seeded_rows())
    runtime = tmp_path / "runtime"
    with pytest.raises(DaemonDiedError):
        dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime, die=True))

    answer = retry(canary, runtime)

    assert answer.disposition is RetryDisposition.SAME_QUEUED_RUN
    assert answer.unmet_guards == ()
    assert answer.successor_run_ref is None
    assert lineage_of(ledger_records(canary, runtime), run_urn(SUCCESSOR_KEY)) is None


def test_a_pre_acceptance_retry_refuses_a_named_successor(tmp_path: Path) -> None:
    """A Run whose identity never became immutable links nothing to it."""
    canary = make_canary(tmp_path / "repo", rows=seeded_rows())
    runtime = tmp_path / "runtime"
    with pytest.raises(DaemonDiedError):
        dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime, die=True))

    with pytest.raises(DaemonValidationError, match=DispatchRefusal.SUCCESSOR_REFUSED.value):
        retry(canary, runtime, successor_urn=str(run_urn(SUCCESSOR_KEY)))


def test_a_retry_of_a_run_nobody_dispatched_is_refused(tmp_path: Path) -> None:
    """A Run with no attempt has nothing a retry could be a retry of."""
    canary = make_canary(tmp_path / "repo", rows=seeded_rows())
    runtime = tmp_path / "runtime"

    with pytest.raises(DaemonValidationError, match=DispatchRefusal.ATTEMPT_ABSENT.value):
        retry(canary, runtime)


# ---- after acceptance: every guard holds ------------------------------------


def test_full_continuity_keeps_the_run_and_spends_one_attempt(tmp_path: Path) -> None:
    """Proof of every guard preserves the identity rather than linking one."""
    canary, runtime, answer = dispatched(tmp_path)

    decided = retry(canary, runtime, continuity=full_proof(answer))

    assert decided.disposition is RetryDisposition.RESUME_SAME_RUN
    assert decided.unmet_guards == ()
    assert decided.successor_run_ref is None
    assert decided.continuity_attempts == 1
    assert decided.attempt_ref == answer["attempt"]["attempt_ref"]


def test_a_second_resume_exhausts_the_continuity_budget(tmp_path: Path) -> None:
    """The budget is one by policy, so the second resume links instead."""
    canary, runtime, answer = dispatched(tmp_path)
    retry(canary, runtime, continuity=full_proof(answer))

    decided = retry(
        canary,
        runtime,
        continuity=full_proof(answer),
        successor_urn=str(run_urn(SUCCESSOR_KEY)),
    )

    assert decided.disposition is RetryDisposition.LINKED_RUN
    assert decided.unmet_guards == (ResumeGuard.CONTINUITY_BUDGET_REMAINING,)


def test_a_proven_resume_refuses_a_named_successor(tmp_path: Path) -> None:
    """A Run that keeps its identity needs no second Run to run as."""
    canary, runtime, answer = dispatched(tmp_path)

    with pytest.raises(DaemonValidationError, match=DispatchRefusal.SUCCESSOR_REFUSED.value):
        retry(
            canary,
            runtime,
            continuity=full_proof(answer),
            successor_urn=str(run_urn(SUCCESSOR_KEY)),
        )


# ---- after acceptance: one unmet guard links a new Run ----------------------


def test_an_unproven_session_links_a_new_run_to_its_predecessor(
    tmp_path: Path,
) -> None:
    """With no session to reattach to, the retry is a different Run."""
    canary, runtime, answer = dispatched(tmp_path)

    decided = retry(
        canary,
        runtime,
        continuity={"compiled_spec_digest": answer["compiled_spec_digest"]},
        successor_urn=str(run_urn(SUCCESSOR_KEY)),
    )

    assert decided.disposition is RetryDisposition.LINKED_RUN
    assert decided.unmet_guards == (ResumeGuard.PROVIDER_SESSION_CONTINUITY,)
    assert str(decided.successor_run_ref) == str(run_urn(SUCCESSOR_KEY))
    assert decided.lineage is not None
    assert decided.lineage["retry_of_run_ref"] == str(RUN_URN)


def test_the_lineage_line_is_durable_on_the_run_ledger(tmp_path: Path) -> None:
    """The link a linked retry makes outlives the process that made it."""
    canary, runtime, answer = dispatched(tmp_path)
    retry(
        canary,
        runtime,
        continuity={"compiled_spec_digest": answer["compiled_spec_digest"]},
        successor_urn=str(run_urn(SUCCESSOR_KEY)),
    )

    lineage = lineage_of(ledger_records(canary, runtime), run_urn(SUCCESSOR_KEY))
    assert lineage is not None
    assert str(lineage.retry_of_run_ref) == str(RUN_URN)
    assert lineage.predecessor_attempt_ref == answer["attempt"]["attempt_ref"]
    assert lineage.unmet_guards == (ResumeGuard.PROVIDER_SESSION_CONTINUITY,)


def test_a_repeated_linked_retry_answers_from_the_standing_lineage(
    tmp_path: Path,
) -> None:
    """One successor carries one lineage, whatever a retry asks twice."""
    canary, runtime, answer = dispatched(tmp_path)
    fields = {
        "continuity": {"compiled_spec_digest": answer["compiled_spec_digest"]},
        "successor_urn": str(run_urn(SUCCESSOR_KEY)),
    }
    first = retry(canary, runtime, **fields)

    second = retry(canary, runtime, **fields)

    assert second.disposition is RetryDisposition.LINKED_RUN
    assert second.lineage == first.lineage
    lines = [
        item
        for item in ledger_records(canary, runtime)
        if item.payload.get("payload_kind") == "retry_lineage"
    ]
    assert len(lines) == 1


def test_a_drifted_contract_links_rather_than_resumes(tmp_path: Path) -> None:
    """A recompile that moved the contract is not the Run that was started."""
    canary, runtime, _ = dispatched(tmp_path)

    decided = retry(
        canary,
        runtime,
        continuity={
            "provider_session_ref": SESSION_REF,
            "compiled_spec_digest": f"sha256:{'e' * 64}",
        },
        successor_urn=str(run_urn(SUCCESSOR_KEY)),
    )

    assert decided.unmet_guards == (ResumeGuard.CONTRACT_UNCHANGED,)


def test_a_released_lease_links_rather_than_resumes(tmp_path: Path) -> None:
    """A Run whose workspace was taken back cannot carry on writing in it."""
    canary, runtime, answer = dispatched(tmp_path)
    context = root_ctx(canary, runtime)
    revoke_lease(context, lease_id=answer["lease"]["lease_id"], now=AT, hard=True)
    assert root_leases(context)[0].status is not LeaseStatus.ACTIVE

    decided = retry(
        canary,
        runtime,
        continuity=full_proof(answer),
        successor_urn=str(run_urn(SUCCESSOR_KEY)),
    )

    assert decided.unmet_guards == (ResumeGuard.LEASE_STILL_ACTIVE,)


def test_a_lapsed_resume_window_links_rather_than_resumes(tmp_path: Path) -> None:
    """A Run quiet longer than the window is not reattached to."""
    canary, runtime, answer = dispatched(tmp_path)

    decided = retry(
        canary,
        runtime,
        now=AT + timedelta(seconds=121),
        continuity=full_proof(answer),
        successor_urn=str(run_urn(SUCCESSOR_KEY)),
    )

    assert ResumeGuard.WITHIN_RESUME_WINDOW in decided.unmet_guards


def test_a_policy_that_forbids_reuse_links_rather_than_resumes(tmp_path: Path) -> None:
    """A compiled policy of never is not overridden by a continuity proof."""
    canary, runtime, answer = dispatched(tmp_path)

    decided = retry(
        canary,
        runtime,
        continuity=full_proof(answer),
        session_policy={"reuse": "never"},
        successor_urn=str(run_urn(SUCCESSOR_KEY)),
    )

    assert decided.unmet_guards == (ResumeGuard.SESSION_REUSE_PERMITTED,)


def test_a_linked_retry_names_every_guard_that_failed(tmp_path: Path) -> None:
    """The lineage states each reason, not merely the first one found."""
    canary, runtime, _ = dispatched(tmp_path)

    decided = retry(
        canary,
        runtime,
        session_policy={"reuse": "never"},
        successor_urn=str(run_urn(SUCCESSOR_KEY)),
    )

    assert set(decided.unmet_guards) >= {
        ResumeGuard.SESSION_REUSE_PERMITTED,
        ResumeGuard.PROVIDER_SESSION_CONTINUITY,
        ResumeGuard.CONTRACT_UNCHANGED,
    }


# ---- refusals a linked retry takes ------------------------------------------


def test_a_linked_retry_naming_no_successor_is_refused(tmp_path: Path) -> None:
    """A retry that cannot keep the Run must say which Run it runs as."""
    canary, runtime, _ = dispatched(tmp_path)

    with pytest.raises(DaemonValidationError, match=DispatchRefusal.SUCCESSOR_REQUIRED.value):
        retry(canary, runtime)


def test_a_successor_that_has_already_started_is_refused(tmp_path: Path) -> None:
    """A linked retry runs as a Run that has not begun."""
    canary = make_canary(
        tmp_path / "repo",
        rows={RUN_KEY: run_row(), SUCCESSOR_KEY: run_row("RUNNING", key=SUCCESSOR_KEY)},
    )
    runtime = tmp_path / "runtime"
    dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))

    with pytest.raises(DaemonValidationError, match=DispatchRefusal.SUCCESSOR_REFUSED.value):
        retry(canary, runtime, successor_urn=str(run_urn(SUCCESSOR_KEY)))


def test_a_successor_no_tier_holds_is_refused(tmp_path: Path) -> None:
    """A lineage never points at a Run nothing recorded."""
    canary = make_canary(tmp_path / "repo", rows={RUN_KEY: run_row()})
    runtime = tmp_path / "runtime"
    dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))

    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        retry(canary, runtime, successor_urn=str(run_urn(SUCCESSOR_KEY)))


def test_a_successor_that_is_the_predecessor_is_refused(tmp_path: Path) -> None:
    """A Run is never linked to itself."""
    canary, runtime, _ = dispatched(tmp_path)

    with pytest.raises(DaemonValidationError, match=DispatchRefusal.SUCCESSOR_REFUSED.value):
        retry(canary, runtime, successor_urn=str(RUN_URN))


def test_retry_params_refuse_a_field_they_do_not_declare() -> None:
    """The request model is strict, so a drifted caller is refused."""
    with pytest.raises(ValueError, match="Extra inputs"):
        RetryParams.model_validate(
            {
                "urn": str(RUN_URN),
                "actor": ACTOR,
                "idempotency_key": "retry-01",
                "surprise": True,
            }
        )


def test_retry_params_refuse_an_empty_idempotency_key() -> None:
    """A retry with no name of its own is not a request anything replays."""
    with pytest.raises(ValueError, match="idempotency_key"):
        RetryParams.model_validate({"urn": str(RUN_URN), "actor": ACTOR, "idempotency_key": ""})


# ---- the guard table itself -------------------------------------------------


def test_the_guard_table_answers_every_declared_guard() -> None:
    """Every member has an evaluator, so none defaults to satisfied."""
    assert set(RESUME_GUARDS) == set(ResumeGuard)


def test_compile_resume_guards_refuses_a_guard_with_no_evaluator() -> None:
    """A guard nobody evaluates would preserve an identity by omission."""
    partial = {
        guard: evaluator
        for guard, evaluator in RESUME_GUARDS.items()
        if guard is not ResumeGuard.RUN_NOT_TERMINAL
    }
    with pytest.raises(ValueError, match=ResumeGuard.RUN_NOT_TERMINAL.value):
        compile_resume_guards(partial)


def guard_inputs(**overrides: Any) -> ResumeInputs:
    """Return resume inputs in which no fact at all is available."""
    from eawf.kernel.runtime.provider import SessionPolicy
    from eawf.kernel.state.epoch2.run import RunStatus
    from eawf.runtime.daemon.native_dispatch import DispatchAttempt

    attempt = DispatchAttempt.model_validate(
        {
            "attempt_ref": f"ATT-{'a' * 32}",
            "run_ref": str(RUN_URN),
            "task_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042",
            "dispatch_key": "dispatch-01",
            "stage": DispatchStage.COMPILED.value,
            "compiled_spec_digest": f"sha256:{'1' * 64}",
            "authority_capsule_digest": f"sha256:{'2' * 64}",
            "route_policy_revision": 3,
            "provider_kind": "codex",
            "started_at": AT.isoformat(),
            "recorded_at": AT.isoformat(),
        }
    )
    fields: dict[str, Any] = {
        "attempt": attempt,
        "proof": ContinuityProof(),
        "policy": SessionPolicy(),
        "lease": None,
        "run_status": RunStatus.RUNNING,
        "derivation_stopped": False,
        "last_activity_at": None,
        "now": AT,
    }
    fields.update(overrides)
    return ResumeInputs(**fields)


def test_every_guard_that_reads_an_absent_fact_fails_closed() -> None:
    """A fact nobody recorded is a guard that did not hold."""
    unmet = unmet_resume_guards(guard_inputs())
    assert ResumeGuard.PROVIDER_SESSION_CONTINUITY in unmet
    assert ResumeGuard.CONTRACT_UNCHANGED in unmet
    assert ResumeGuard.LEASE_STILL_ACTIVE in unmet


def test_a_hole_in_the_event_stream_is_an_unmet_guard() -> None:
    """Ordering is half of what a resume preserves."""
    assert ResumeGuard.EVENT_ORDERING_INTACT in unmet_resume_guards(
        guard_inputs(derivation_stopped=True)
    )


def test_a_stopped_run_is_an_unmet_guard() -> None:
    """A Run that already ended is not resumed into a second episode."""
    from eawf.kernel.state.epoch2.run import RunStatus

    assert ResumeGuard.RUN_NOT_TERMINAL in unmet_resume_guards(
        guard_inputs(run_status=RunStatus.FAILED)
    )


def test_the_window_is_measured_from_the_last_recorded_activity() -> None:
    """An event stream, when there is one, is what the window measures."""
    late = guard_inputs(last_activity_at=AT - timedelta(seconds=600))
    assert ResumeGuard.WITHIN_RESUME_WINDOW in unmet_resume_guards(late)
    fresh = guard_inputs(last_activity_at=AT - timedelta(seconds=10))
    assert ResumeGuard.WITHIN_RESUME_WINDOW not in unmet_resume_guards(fresh)


def test_a_lineage_with_nothing_unmet_is_not_a_record() -> None:
    """A link the guards did not require is refused at the line."""
    with pytest.raises(ValueError, match="unmet_guards"):
        RetryLineage.model_validate(
            {
                "run_ref": str(run_urn(SUCCESSOR_KEY)),
                "retry_of_run_ref": str(RUN_URN),
                "predecessor_attempt_ref": f"ATT-{'a' * 32}",
                "unmet_guards": [],
                "actor": ACTOR,
                "recorded_at": AT.isoformat(),
            }
        )


def test_a_lineage_naming_one_run_twice_is_not_a_record() -> None:
    """A successor that is its own predecessor is a cycle, not a lineage."""
    with pytest.raises(ValueError, match="two different Runs"):
        RetryLineage.model_validate(
            {
                "run_ref": str(RUN_URN),
                "retry_of_run_ref": str(RUN_URN),
                "predecessor_attempt_ref": f"ATT-{'a' * 32}",
                "unmet_guards": [ResumeGuard.CONTRACT_UNCHANGED.value],
                "actor": ACTOR,
                "recorded_at": AT.isoformat(),
            }
        )


def test_the_retry_verb_is_registered_behind_the_native_fence() -> None:
    """The decision a caller on the socket reaches is this one."""
    from eawf.runtime.daemon import methods
    from eawf.runtime.daemon.native_dispatch import RUN_RETRY_METHOD

    assert RUN_RETRY_METHOD in methods.registered_methods()


def test_the_retry_verb_refuses_an_epoch_one_tree(tmp_path: Path) -> None:
    """A tree with no epoch-2 authority reaches no retry decision."""
    from eawf.runtime.daemon import methods
    from eawf.runtime.daemon.native_dispatch import RUN_RETRY_METHOD
    from tests.integration.runtime.daemon.test_native_dispatch import make_repo

    repo = tmp_path / "plain"
    make_repo(repo)
    (repo / ".ea").mkdir(exist_ok=True)

    with pytest.raises(DaemonValidationError, match="native_authority_required"):
        asyncio.run(
            methods.dispatch(
                RUN_RETRY_METHOD,
                method_ctx(tmp_path / "runtime"),
                {
                    "repo_root": str(repo),
                    "urn": str(RUN_URN),
                    "actor": ACTOR,
                    "idempotency_key": "retry-01",
                },
            )
        )
