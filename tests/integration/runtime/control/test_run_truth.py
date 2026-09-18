"""Run truth comes from confirmed effects, and from nothing else.

Every sequence below is driven through the live ``runtime.run.*`` verbs
against a provisioned epoch-2 canary, so what is asserted is what a
client would get: the daemon appends the control fact, the reducer folds
the ledger, and the canonical record moves only where the fold says it
should. Nothing here constructs a fact by hand, because a reducer proved
against hand-built input proves nothing about the producer.

The ack-sequence table is a fixture rather than parametrised code. Each
row is one request/acknowledgement/effect walk with the Run status and
the control cursor it must end at, and the two rows that matter most are
the ones with no terminal at all: an accepted acknowledgement that was
never effected, and an effect that came back undetermined. Both leave the
Run running, because "we asked and never found out" is a thing the
vocabulary can say and rounding it to cancelled would be a lie.

The lease is driven with a real barrier rather than a sleep. Two threads
request first, meet at a :class:`threading.Barrier`, and then race for
the root's entity lock; the winner takes the Run's one control lease and
the loser is superseded, carrying no receipt.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
from pathlib import Path
from typing import Any, Final, Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.runtime.control import (
    ControlDisposition,
    ControlFact,
    ControlPhase,
    RunBinding,
)
from eawf.kernel.state.epoch2.run import Run, RunStatus
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.control.reducer import (
    decide_control_lease,
    project_disposition,
    reconstruct_run_contract,
    reduce_run_control,
)
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.run import (
    RUN_CONTROL_ACKNOWLEDGE_METHOD,
    RUN_CONTROL_EFFECT_METHOD,
    RUN_CONTROL_REQUEST_METHOD,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"
EFFECT_REF: Final = "EFF-0000000b"
RECEIPT_REF: Final = "REC-0000000c"

#: The ack-sequence table, four levels up lands on ``tests/``.
SEQUENCES: Final = (
    Path(__file__).resolve().parents[3] / "fixtures" / "runtime" / "control_ack_sequences.jsonl"
)


class AckSequence(BaseModel):
    """One request / acknowledgement / effect walk and where it must end.

    Attributes:
        scenario: The row's name, used as the test id.
        control: Which control the request asks for.
        acknowledge: The acknowledgement decision, or ``None`` for a
            request nobody answered.
        effect: The effected disposition, or ``None`` for a control whose
            effect was never observed.
        expected_disposition: The rendered outcome of the last fact.
        expected_status: The Run status the walk must leave behind.
        expected_cursor: The control cursor the walk must reach.
        note: Why the row exists, in one sentence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: str
    control: str
    acknowledge: Literal["accepted", "rejected", "invalidated"] | None
    effect: Literal["confirmed", "unknown", "recovery"] | None
    expected_disposition: str
    expected_status: str
    expected_cursor: int
    note: str


def load_sequences() -> tuple[AckSequence, ...]:
    """Return every ack sequence the fixture declares, in file order.

    Raises:
        ValidationError: A row carries an unknown key or a value outside
            the closed vocabularies, which would silently weaken the
            table it is the contract for.
    """
    lines = SEQUENCES.read_text(encoding="utf-8").splitlines()
    return tuple(AckSequence.model_validate(json.loads(line)) for line in lines if line.strip())


ACK_SEQUENCES: Final = load_sequences()


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one running Run."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(tmp_path / "runtime")


def call(method: str, ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb the way the server does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


def request_control(
    ctx: MethodContext, canary: CanaryProvision, *, ref: str, control: str = "cancel"
) -> dict[str, Any]:
    """Ask for one control on the canary's Run."""
    return call(
        RUN_CONTROL_REQUEST_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        control_request_ref=ref,
        control=control,
        actor=ACTOR,
    )


def acknowledge(
    ctx: MethodContext, canary: CanaryProvision, *, ref: str, decision: str = "accepted"
) -> dict[str, Any]:
    """Acknowledge one control request, deciding the Run's control lease."""
    return call(
        RUN_CONTROL_ACKNOWLEDGE_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        control_request_ref=ref,
        actor=ACTOR,
        decision=decision,
    )


def effect(
    ctx: MethodContext, canary: CanaryProvision, *, ref: str, disposition: str, key: str
) -> dict[str, Any]:
    """Record what was observed of one acknowledged control."""
    proof: dict[str, Any] = {}
    if disposition == "confirmed":
        proof["effect_ref"] = EFFECT_REF
    else:
        proof["receipt_ref"] = RECEIPT_REF
    return call(
        RUN_CONTROL_EFFECT_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        control_request_ref=ref,
        actor=ACTOR,
        disposition=disposition,
        idempotency_key=key,
        **proof,
    )


def stored_row(canary: CanaryProvision) -> dict[str, Any]:
    """Return the canonical Run record, from whichever tier holds it.

    A Run that reaches a terminal state is compacted out of the document
    and into the run ledger, beside the control lines the same ledger
    carries. The Run's own line is the one with no payload
    discriminator, which is how the daemon's own reader tells it apart.
    """
    rows = read_document(document_path(canary)).get("run", {})
    if RUN_KEY in rows:
        row: dict[str, Any] = rows[RUN_KEY]
        return row
    path = ledger_path(document_path(canary), Epoch2Collection.RUN)
    for item in effective_records(read_ledger_records(path)):
        if item.record_key == RUN_KEY and "payload_kind" not in item.payload:
            return item.payload
    raise AssertionError(f"neither tier holds a run record keyed {RUN_KEY!r}")


def stored_status(canary: CanaryProvision) -> str:
    """Return the status the canonical Run record carries right now."""
    return str(stored_row(canary)["status"])


def control_lines(canary: CanaryProvision) -> tuple[dict[str, Any], ...]:
    """Return every control fact the canary's run ledger holds, in order."""
    path = ledger_path(document_path(canary), Epoch2Collection.RUN)
    return tuple(
        item.payload
        for item in read_ledger_records(path)
        if item.payload.get("payload_kind") == "control"
    )


def walk(
    ctx: MethodContext, canary: CanaryProvision, sequence: AckSequence, *, ref: str
) -> dict[str, Any]:
    """Drive one ack sequence end to end and return its last answer."""
    answer = request_control(ctx, canary, ref=ref, control=sequence.control)
    if sequence.acknowledge is not None:
        answer = acknowledge(ctx, canary, ref=ref, decision=sequence.acknowledge)
    if sequence.effect is not None:
        answer = effect(
            ctx, canary, ref=ref, disposition=sequence.effect, key=f"idem-{sequence.scenario}"
        )
    return answer


# ---------------------------------------------------------------------------
# DOM-013 / RUN-004: request, acknowledgement and effect report separately
# ---------------------------------------------------------------------------


def test_the_ack_sequence_table_covers_every_terminal_and_non_terminal_ending() -> None:
    """The fixture is the contract, so a thinned table fails here first."""
    endings = {row.expected_status for row in ACK_SEQUENCES}
    dispositions = {row.expected_disposition for row in ACK_SEQUENCES}

    assert endings == {"RUNNING", "CANCELLED"}
    assert dispositions == {
        "requesting",
        "accepted",
        "confirmed",
        "rejected",
        "invalidated",
        "unknown",
        "recovery",
    }


@pytest.mark.parametrize("sequence", ACK_SEQUENCES, ids=[row.scenario for row in ACK_SEQUENCES])
def test_an_ack_sequence_ends_where_the_fixture_says(
    sequence: AckSequence, ctx: MethodContext, canary: CanaryProvision
) -> None:
    answer = walk(ctx, canary, sequence, ref="CTL-0000000a")

    assert answer["disposition"] == sequence.expected_disposition
    assert answer["run_status"] == sequence.expected_status
    assert answer["control_cursor"] == sequence.expected_cursor
    assert stored_status(canary) == sequence.expected_status
    assert len(control_lines(canary)) == sequence.expected_cursor


def test_a_cancel_request_alone_leaves_the_record_untouched(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """The request is an ask. Nothing about the Run has been decided."""
    before = read_document(document_path(canary))["run"][RUN_KEY]

    request_control(ctx, canary, ref="CTL-0000000a")

    assert read_document(document_path(canary))["run"][RUN_KEY] == before


def test_a_lost_acknowledgement_never_fabricates_a_terminal_state(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """Accepted and never effected is a running Run, not a cancelled one."""
    request_control(ctx, canary, ref="CTL-0000000a")
    answer = acknowledge(ctx, canary, ref="CTL-0000000a")

    assert answer["disposition"] == "accepted"
    assert answer["run_status"] == "RUNNING"
    assert stored_status(canary) == "RUNNING"
    assert answer["receipt"] is None


def test_an_undetermined_effect_never_fabricates_a_terminal_state(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """``unknown`` is neither success nor failure, so it moves nothing."""
    request_control(ctx, canary, ref="CTL-0000000a")
    acknowledge(ctx, canary, ref="CTL-0000000a")
    answer = effect(ctx, canary, ref="CTL-0000000a", disposition="unknown", key="idem-unknown")

    assert answer["fact"]["receipt_ref"] == RECEIPT_REF
    assert answer["fact"]["effect_ref"] is None
    assert stored_status(canary) == "RUNNING"


def test_a_confirmed_effect_terminalizes_and_carries_the_mutation_receipt(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    request_control(ctx, canary, ref="CTL-0000000a")
    acknowledge(ctx, canary, ref="CTL-0000000a")
    answer = effect(ctx, canary, ref="CTL-0000000a", disposition="confirmed", key="idem-confirm")

    assert answer["run_status"] == "CANCELLED"
    assert answer["receipt"]["revision_before"] == 1
    assert answer["receipt"]["revision_after"] == 2
    assert stored_status(canary) == "CANCELLED"


def test_replaying_a_confirmed_effect_writes_no_second_fact(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """A retry is answered from the ledger it already wrote, not appended to."""
    request_control(ctx, canary, ref="CTL-0000000a")
    acknowledge(ctx, canary, ref="CTL-0000000a")
    first = effect(ctx, canary, ref="CTL-0000000a", disposition="confirmed", key="idem-confirm")
    second = effect(ctx, canary, ref="CTL-0000000a", disposition="confirmed", key="idem-confirm")

    assert second["fact"] == first["fact"]
    assert second["control_cursor"] == 3
    assert len(control_lines(canary)) == 3
    assert stored_status(canary) == "CANCELLED"


# ---------------------------------------------------------------------------
# The control lease: one holder, driven through a real barrier
# ---------------------------------------------------------------------------


def test_two_principals_racing_one_run_produce_one_holder_and_one_superseded(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """Both threads are provably in flight before either takes the lock."""
    refs = ("CTL-0000000a", "CTL-0000000b")
    for ref in refs:
        request_control(ctx, canary, ref=ref)

    barrier = threading.Barrier(len(refs))
    answers: dict[str, dict[str, Any]] = {}

    def contend(ref: str) -> None:
        barrier.wait()
        answers[ref] = acknowledge(ctx, canary, ref=ref)

    threads = [threading.Thread(target=contend, args=(ref,)) for ref in refs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)

    assert sorted(answer["disposition"] for answer in answers.values()) == [
        "accepted",
        "superseded",
    ]
    losing = next(a for a in answers.values() if a["disposition"] == "superseded")
    assert losing["fact"]["receipt_ref"] is None
    assert losing["fact"]["effect_ref"] is None
    assert stored_status(canary) == "RUNNING"


def test_a_superseded_request_cannot_record_an_effect(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """The loser holds nothing, so no effect is attributed to it."""
    request_control(ctx, canary, ref="CTL-0000000a")
    request_control(ctx, canary, ref="CTL-0000000b")
    acknowledge(ctx, canary, ref="CTL-0000000a")
    loser = acknowledge(ctx, canary, ref="CTL-0000000b")

    assert loser["disposition"] == "superseded"
    with pytest.raises(DaemonValidationError, match="does not hold this Run's control lease"):
        effect(ctx, canary, ref="CTL-0000000b", disposition="confirmed", key="idem-loser")


def test_the_lease_frees_once_the_holder_is_rejected(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """A refused request holds nothing, so the next principal may take it."""
    request_control(ctx, canary, ref="CTL-0000000a")
    request_control(ctx, canary, ref="CTL-0000000b")
    acknowledge(ctx, canary, ref="CTL-0000000a", decision="rejected")

    assert acknowledge(ctx, canary, ref="CTL-0000000b")["disposition"] == "accepted"


def test_acknowledging_twice_replays_the_first_decision(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    request_control(ctx, canary, ref="CTL-0000000a")
    first = acknowledge(ctx, canary, ref="CTL-0000000a")
    second = acknowledge(ctx, canary, ref="CTL-0000000a")

    assert second["fact"] == first["fact"]
    assert len(control_lines(canary)) == 2


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_acknowledging_a_request_nobody_made_is_refused(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        acknowledge(ctx, canary, ref="CTL-0000dead")


def test_effecting_a_request_that_was_never_acknowledged_is_refused(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    request_control(ctx, canary, ref="CTL-0000000a")

    with pytest.raises(DaemonValidationError, match="does not hold this Run's control lease"):
        effect(ctx, canary, ref="CTL-0000000a", disposition="confirmed", key="idem-early")


def test_a_confirmed_effect_with_no_effect_reference_is_refused(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    request_control(ctx, canary, ref="CTL-0000000a")
    acknowledge(ctx, canary, ref="CTL-0000000a")

    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call(
            RUN_CONTROL_EFFECT_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            control_request_ref="CTL-0000000a",
            actor=ACTOR,
            disposition="confirmed",
            idempotency_key="idem-bare",
        )


def test_an_unknown_parameter_is_refused_before_anything_is_written(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call(
            RUN_CONTROL_REQUEST_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            control_request_ref="CTL-0000000a",
            control="cancel",
            actor=ACTOR,
            disposition="confirmed",
        )
    assert control_lines(canary) == ()


def test_a_request_naming_a_run_the_document_does_not_hold_is_refused(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        call(
            RUN_CONTROL_REQUEST_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00009999",
            control_request_ref="CTL-0000000a",
            control="cancel",
            actor=ACTOR,
        )


def test_a_row_with_an_unknown_key_fails_the_ack_sequence_loader() -> None:
    """The table is strict, so a silently widened fixture never loads."""
    with pytest.raises(ValidationError, match="Extra inputs"):
        AckSequence.model_validate({**ACK_SEQUENCES[0].model_dump(), "expected_receipt": "x"})


@pytest.mark.parametrize("seeded", ["QUEUED", "SUSPENDED"])
def test_a_confirmed_cancel_terminalizes_a_run_in_any_live_status(
    seeded: str, tmp_path: Path, ctx: MethodContext
) -> None:
    """Each live status carries its own required stamps on the way out."""
    provisioned = provision(tmp_path / f"repo-{seeded.lower()}")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", seeded)}})

    request_control(ctx, provisioned, ref="CTL-0000000a")
    acknowledge(ctx, provisioned, ref="CTL-0000000a")
    answer = effect(
        ctx, provisioned, ref="CTL-0000000a", disposition="confirmed", key=f"idem-{seeded}"
    )

    assert answer["run_status"] == "CANCELLED"
    row = stored_row(provisioned)
    assert row["status"] == "CANCELLED"
    assert row["started_at"] is not None
    assert row["ended_at"] is not None
    assert row["suspension_reason"] is None


# ---------------------------------------------------------------------------
# The pure reducer refuses input no ledger of ours would produce
# ---------------------------------------------------------------------------


def control_fact(**overrides: Any) -> ControlFact:
    """Build one requested fact directly, with *overrides* applied."""
    payload: dict[str, Any] = {
        "control_request_ref": "CTL-0000000a",
        "run_ref": RUN_URN,
        "control": "cancel",
        "phase": ControlPhase.REQUESTED,
        "disposition": ControlDisposition.REQUESTING,
        "actor": ACTOR,
        "recorded_at": "2026-09-18T12:00:00Z",
        "sequence": 1,
    }
    payload.update(overrides)
    return ControlFact.model_validate(payload)


def accepted_fact(**overrides: Any) -> ControlFact:
    """Build one accepted acknowledgement, with *overrides* applied."""
    return control_fact(
        phase=ControlPhase.ACKNOWLEDGED,
        disposition=ControlDisposition.ACCEPTED,
        sequence=2,
        **overrides,
    )


def confirmed_fact(**overrides: Any) -> ControlFact:
    """Build one confirmed effect, with *overrides* applied."""
    return control_fact(
        phase=ControlPhase.EFFECTED,
        disposition=ControlDisposition.CONFIRMED,
        effect_ref=EFFECT_REF,
        sequence=3,
        **overrides,
    )


def test_an_empty_ledger_reduces_to_the_stored_status() -> None:
    state = reduce_run_control(status=RunStatus.RUNNING, facts=())

    assert state.status is RunStatus.RUNNING
    assert state.control_cursor == 0
    assert state.rows == ()


def test_one_fact_reduces_to_a_cursor_of_one() -> None:
    state = reduce_run_control(status=RunStatus.RUNNING, facts=(control_fact(),))

    assert state.control_cursor == 1
    assert state.rows[0].disposition is ControlDisposition.REQUESTING


def test_a_ledger_with_a_gap_is_refused_rather_than_reduced() -> None:
    """A lost line would report a cursor that skips the missing fact."""
    with pytest.raises(ValueError, match="not contiguous"):
        reduce_run_control(status=RunStatus.RUNNING, facts=(control_fact(), confirmed_fact()))


def test_a_late_confirmed_effect_cannot_reopen_a_terminal_run() -> None:
    facts = (control_fact(), accepted_fact(), confirmed_fact())

    state = reduce_run_control(status=RunStatus.COMPLETED, facts=facts)

    assert state.status is RunStatus.COMPLETED


def test_projecting_over_two_requests_at_once_is_refused() -> None:
    facts = (control_fact(), control_fact(control_request_ref="CTL-0000000b", sequence=2))

    with pytest.raises(ValueError, match="a disposition projects over one"):
        project_disposition(facts)


def test_a_lease_decision_for_a_request_nobody_made_is_refused() -> None:
    with pytest.raises(ValueError, match="no requested fact to acknowledge"):
        decide_control_lease(control_request_ref="CTL-0000dead", facts=())


def test_a_lease_decision_taken_twice_for_one_request_is_refused() -> None:
    with pytest.raises(ValueError, match="already been acknowledged"):
        decide_control_lease(
            control_request_ref="CTL-0000000a", facts=(control_fact(), accepted_fact())
        )


def test_a_binding_naming_another_run_cannot_build_this_contract() -> None:
    run = Run.model_validate(seed_row("run", "RUNNING"))
    binding = RunBinding(
        run_ref="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011",
        compiled_spec_digest=f"sha256:{'d' * 64}",
        authority_capsule_digest=f"sha256:{'e' * 64}",
        route_policy_revision=1,
        bound_at="2026-09-18T12:00:00Z",
    )

    with pytest.raises(ValueError, match="but the record is"):
        reconstruct_run_contract(run=run, binding=binding, facts=())
