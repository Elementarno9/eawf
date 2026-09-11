"""A close may not complete while it cannot prove its required gates ran.

Five closes in one iteration reported ``status=closed`` against a non-empty
``required_gate_ids`` with zero receipts. The cause of those five is repaired
elsewhere; the floor pinned here is the terminal check that makes the *shape*
unreachable however it is produced. The close gate has several arms that return
an empty evidence list without refusing -- a gate that compiles to nothing, a
criterion a tier filter drops, a runner that dies before it observes -- and at
the close boundary none of them is distinguishable from a wave that owed no
deterministic proof at all. The floor makes them distinguishable by reading the
persisted receipt ledger rather than trusting the pass that produced it.

The headline test kills a real gate-runner child, for real, while it is running,
and then asks the production floor what it makes of the attempt that child left
behind. The answer has to name the receiptless gate, because an operator who is
told only "blocked" cannot tell which row to repair.

Every fixture tree is built under ``tmp_path``; nothing here reads or writes the
repository's own ``.ea/``. No test here starts, stops, or talks to the
operator's daemon.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eawf.kernel.state.enums import CloseAttemptStatus, GateReceiptResult
from eawf.kernel.state.models import CloseAttempt, State
from eawf.kernel.store.kinds.gate_receipt import GateReceipt, canonical_gate_digest
from eawf.runtime.daemon.gate_execution import GateChildCrashError
from eawf.runtime.daemon.gate_receipt_hygiene import append_gate_receipt
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.close import CloseWorkRejectedError
from eawf.runtime.daemon.methods.state_close import enforce_close_gate_receipt_floor
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.verify.gate_receipt_floor import (
    GateReceiptFloorError,
    enforce_gate_receipt_floor,
    obliged_gate_ids,
    receipted_gate_ids,
)
from tests.integration.workflow.verify._close_gate_helpers import (
    ATTEMPT_ID,
    FIXTURE_DIGEST,
    FIXTURE_SHA,
    close_attempt,
    criterion,
    enforce_verify_block,
    file_exists_gate,
    live_tree,
    score_close,
    wave,
)

pytestmark = pytest.mark.integration


def _one_gate_wave() -> Any:
    """Return a wave with exactly one required blocking deterministic gate."""
    return wave(
        criteria=[criterion("CR-01", gate_ids=["G-01"])],
        gates=[file_exists_gate("G-01", criterion_id="CR-01")],
        effort_bucket="M",
    )


def _kill_the_gate_child(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Kill the real gate-runner child mid-run and report its exit status.

    ``run_gate_out_of_process`` spawns a child interpreter and blocks on it.
    Only the blocking *wait* is replaced here: the child is a genuine process
    started from the same argv, cwd, and sandbox environment the production path
    builds, and it dies from a real ``SIGKILL`` while it is alive. Nothing about
    the child's own behaviour is simulated -- a mocked child would prove nothing
    about what a dying one leaves behind.

    The signal is sent as soon as the child is up rather than after waiting for
    some interior milestone, which is what makes the test deterministic: the
    gate under test is instantaneous, so any wait long enough to be meaningful
    is also long enough for the child to finish and win the race. Killing at
    once guarantees the one state the close path has to survive -- a child that
    ran and produced no terminal result.

    The substitution is installed on the ``gate_execution`` module's own
    ``subprocess`` reference rather than on the standard library, so no other
    process spawn in the test session is affected.

    Args:
        monkeypatch: Fixture used to install the substitution.

    Returns:
        A list that receives the killed child's exit status.
    """
    from eawf.runtime.daemon import gate_execution

    statuses: list[int] = []

    def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        for unsupported in ("capture_output", "check", "text"):
            kwargs.pop(unsupported, None)
        child = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **kwargs,
        )
        child.kill()
        stdout, stderr = child.communicate()
        statuses.append(child.returncode)
        return subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)

    monkeypatch.setattr(gate_execution, "subprocess", SimpleNamespace(run=_run))
    return statuses


def _bind_receipt(state_path: Path, *, wave_id: str, gate_id: str, freshness_key: str) -> str:
    """Persist one durable receipt for *gate_id* and bind it to the attempt.

    Stands in for the daemon's own receipt-commit pair (store append plus the
    attempt-row update). What the floor reads is the persisted result of that
    pair, so the double writes the same two places rather than re-deriving the
    digests the production path freezes.

    Args:
        state_path: The fixture's ``state.json``.
        wave_id: Wave the receipt scopes to.
        gate_id: Gate the receipt records.
        freshness_key: 64-hex freshness key the receipt id derives from.

    Returns:
        The receipt id now bound to the attempt.
    """
    started = datetime.now(UTC)
    receipt = GateReceipt(
        id=f"GR-{freshness_key[:32]}",
        scope_id=wave_id,
        criterion_id="CR-01",
        gate_id=gate_id,
        integration_id="WI-FLOOR-01",
        integrated_sha=FIXTURE_SHA,
        tree_sha=FIXTURE_SHA,
        contract_digest=canonical_gate_digest(FIXTURE_DIGEST),
        criteria_digest=canonical_gate_digest(FIXTURE_DIGEST),
        gate_manifest_digest=canonical_gate_digest(FIXTURE_DIGEST),
        policy_digest=canonical_gate_digest(FIXTURE_DIGEST),
        dependency_binding_digest=canonical_gate_digest(FIXTURE_DIGEST),
        runner_environment_digest=canonical_gate_digest(FIXTURE_DIGEST),
        runner_digest=canonical_gate_digest(FIXTURE_DIGEST),
        environment_digest=canonical_gate_digest(FIXTURE_DIGEST),
        freshness_key=freshness_key,
        started_at=started,
        ended_at=started + timedelta(milliseconds=5),
        duration_ms=5,
        result=GateReceiptResult.PASS,
        exit_status=0,
    )
    append_gate_receipt(state_path, receipt)
    state = State.model_validate_json(state_path.read_bytes())
    attempt = state.close_attempts[ATTEMPT_ID]
    payload = attempt.model_dump(mode="json")
    payload["gate_receipt_ids"] = [*attempt.gate_receipt_ids, receipt.id]
    state.close_attempts[ATTEMPT_ID] = CloseAttempt.model_validate(payload)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return receipt.id


def _bound_receipt_ids(state_path: Path) -> list[str]:
    """Return the receipt ids the attempt row currently binds."""
    state = State.model_validate_json(state_path.read_bytes())
    return list(state.close_attempts[ATTEMPT_ID].gate_receipt_ids)


# --- the headline: a killed child cannot produce a closed wave --------------


def test_a_close_whose_gate_child_is_killed_mid_run_is_refused_naming_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill the child for real, then hold the close to what it can prove.

    Two properties in one drive. The crash itself must not be scored as a gate
    verdict -- a child that died observed nothing -- so the scoring pass raises
    rather than returning a pass. And the attempt that child left behind binds
    no receipt, so the production floor refuses it and the refusal names the
    gate the operator has to repair.
    """
    closing = _one_gate_wave()
    state, state_path = live_tree(
        tmp_path,
        monkeypatch,
        closing=closing,
        attempt=close_attempt(wave_id=closing.id, required_gate_ids=["G-01"]),
    )
    enforce_verify_block(monkeypatch, uiux_bands=[])
    statuses = _kill_the_gate_child(monkeypatch)

    with pytest.raises(GateChildCrashError):
        score_close(
            state,
            state_path=state_path,
            repo_root=tmp_path,
            close_attempt_id=ATTEMPT_ID,
            before_gate_execute=lambda *_args: pytest.fail(
                "the parent must not claim: the gate child owns the claim"
            ),
        )

    assert statuses and statuses[0] != 0
    assert _bound_receipt_ids(state_path) == []

    with pytest.raises(GateReceiptFloorError) as refusal:
        enforce_close_gate_receipt_floor(
            closing,
            state_path=state_path,
            gate_specs=list(closing.gates),
            close_attempt_id=ATTEMPT_ID,
        )

    assert refusal.value.missing_gate_ids == ["G-01"]
    assert "G-01" in str(refusal.value)
    assert refusal.value.scope_id == closing.id


def test_a_receipt_floor_refusal_routes_the_attempt_to_blocked_not_closed() -> None:
    """The refusal's type is what makes the attempt blocked rather than closed.

    Each link is asserted rather than assumed: the mutation boundary catches
    :class:`LifecycleError`, the close worker catches the ``ValueError`` that
    boundary raises, and the fault it types from it is a BLOCKED outcome. A
    refusal that fell outside any one link would leave the close completing.
    """
    refusal = GateReceiptFloorError(
        scope_id="P32-I01-W43",
        missing_gate_ids=["G-01"],
        obliged_gate_ids=["G-01", "G-02"],
    )

    assert isinstance(refusal, LifecycleError)
    assert issubclass(DaemonValidationError, ValueError)
    assert CloseWorkRejectedError.attempt_status is CloseAttemptStatus.BLOCKED


def test_the_close_gate_refuses_a_passing_gate_whose_receipt_never_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass without a receipt is refused, because a pass is not the proof.

    The durable receipt-persisting hook declines on its own terms -- a freshness
    mismatch, an incomplete observation set, a proof log that did not survive --
    and each of those leaves the scorer's verdict intact while the ledger stays
    empty. The gate below genuinely runs and genuinely passes, and the close is
    still refused, because the close-time claim is what can be PROVEN later, not
    what one process happened to see.
    """
    closing = _one_gate_wave()
    state, state_path = live_tree(
        tmp_path,
        monkeypatch,
        closing=closing,
        attempt=close_attempt(wave_id=closing.id, required_gate_ids=["G-01"]),
    )
    enforce_verify_block(monkeypatch, uiux_bands=[])
    declined: list[str] = []

    def _decline_receipt(_criterion_id: str, gate_id: str, _result: Any) -> None:
        declined.append(gate_id)

    with pytest.raises(GateReceiptFloorError) as refusal:
        score_close(
            state,
            state_path=state_path,
            repo_root=tmp_path,
            close_attempt_id=ATTEMPT_ID,
            before_gate_execute=lambda *_args: None,
            on_gate_result=_decline_receipt,
        )

    assert declined == ["G-01"]
    assert _bound_receipt_ids(state_path) == []
    assert refusal.value.missing_gate_ids == ["G-01"]


def test_a_close_whose_gates_all_receipted_clears_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: proof present, close proceeds."""
    closing = _one_gate_wave()
    _state, state_path = live_tree(
        tmp_path,
        monkeypatch,
        closing=closing,
        attempt=close_attempt(wave_id=closing.id, required_gate_ids=["G-01"]),
    )
    receipt_id = _bind_receipt(
        state_path,
        wave_id=closing.id,
        gate_id="G-01",
        freshness_key="a" * 64,
    )

    assert _bound_receipt_ids(state_path) == [receipt_id]
    enforce_close_gate_receipt_floor(
        closing,
        state_path=state_path,
        gate_specs=list(closing.gates),
        close_attempt_id=ATTEMPT_ID,
    )


def test_an_advisory_close_binds_no_attempt_and_is_not_held_to_a_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close with no durable attempt has no receipt ledger to answer for."""
    closing = _one_gate_wave()
    _state, state_path = live_tree(tmp_path, monkeypatch, closing=closing)

    enforce_close_gate_receipt_floor(
        closing,
        state_path=state_path,
        gate_specs=list(closing.gates),
        close_attempt_id="",
    )


def test_an_unresolvable_attempt_id_is_skipped_rather_than_relabelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named attempt with no row has no ledger, so the floor declines to judge.

    The under-lock apply snapshot refuses that close as stale on its own terms.
    Refusing it here as a receipt gap would rename a known failure into a less
    accurate one and send the operator to repair the wrong thing.
    """
    closing = _one_gate_wave()
    _state, state_path = live_tree(tmp_path, monkeypatch, closing=closing)

    enforce_close_gate_receipt_floor(
        closing,
        state_path=state_path,
        gate_specs=list(closing.gates),
        close_attempt_id="CA-DOES-NOT-EXIST",
    )


# --- obliged-set boundaries ------------------------------------------------


def test_obliged_gate_ids_on_empty_inputs_is_empty() -> None:
    """The empty boundary: nothing declared, nothing owed."""
    assert obliged_gate_ids(criteria=[], gates=[]) == frozenset()


def test_obliged_gate_ids_on_a_single_required_blocking_gate_is_that_gate() -> None:
    """The single boundary."""
    assert obliged_gate_ids(
        criteria=[criterion("CR-01", gate_ids=["G-01"])],
        gates=[file_exists_gate("G-01", criterion_id="CR-01")],
    ) == frozenset({"G-01"})


@pytest.mark.parametrize(
    ("required", "policy"),
    [(False, "block"), (True, "warn")],
)
def test_obliged_gate_ids_excludes_a_gate_whose_verdict_may_be_ignored(
    required: bool, policy: str
) -> None:
    """A gate the scorer may drop is not proof the close must collect."""
    assert (
        obliged_gate_ids(
            criteria=[criterion("CR-01", gate_ids=["G-01"])],
            gates=[
                file_exists_gate(
                    "G-01",
                    criterion_id="CR-01",
                    required=required,
                    policy=policy,
                )
            ],
        )
        == frozenset()
    )


@pytest.mark.parametrize("evidence_kind", ["jury", "attested"])
def test_obliged_gate_ids_excludes_a_non_deterministic_criterion(evidence_kind: str) -> None:
    """No deterministic gate compiles for a jury or attested criterion."""
    assert (
        obliged_gate_ids(
            criteria=[criterion("CR-01", gate_ids=["G-01"], evidence_kind=evidence_kind)],
            gates=[file_exists_gate("G-01", criterion_id="CR-01")],
        )
        == frozenset()
    )


def test_obliged_gate_ids_excludes_an_optional_criterion() -> None:
    """A criterion the close need not score owes no proof."""
    assert (
        obliged_gate_ids(
            criteria=[criterion("CR-01", gate_ids=["G-01"], required=False)],
            gates=[file_exists_gate("G-01", criterion_id="CR-01")],
        )
        == frozenset()
    )


def test_obliged_gate_ids_excludes_a_gate_bound_to_no_known_criterion() -> None:
    """A gate naming a criterion the wave does not carry is not obliged."""
    assert (
        obliged_gate_ids(
            criteria=[criterion("CR-01", gate_ids=["G-01"])],
            gates=[file_exists_gate("G-02", criterion_id="CR-99")],
        )
        == frozenset()
    )


def test_obliged_gate_ids_rejects_a_non_criterion_row() -> None:
    """A caller feeding the wrong row type fails fast."""
    with pytest.raises(AttributeError):
        obliged_gate_ids(criteria=[object()], gates=[])  # type: ignore[list-item]


# --- receipt-lookup boundaries --------------------------------------------


def test_receipted_gate_ids_with_no_wanted_ids_is_empty(tmp_path: Path) -> None:
    """The empty boundary: an attempt binding nothing covers nothing."""
    assert receipted_gate_ids(tmp_path / ".ea" / "state.json", receipt_ids=[]) == frozenset()


def test_receipted_gate_ids_with_no_store_file_is_empty(tmp_path: Path) -> None:
    """A receipt id that names a row in a store that does not exist is not proof."""
    assert (
        receipted_gate_ids(tmp_path / ".ea" / "state.json", receipt_ids=["GR-missing"])
        == frozenset()
    )


def test_receipted_gate_ids_ignores_an_unbound_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored receipt the attempt never bound does not count toward the floor."""
    closing = _one_gate_wave()
    _state, state_path = live_tree(
        tmp_path,
        monkeypatch,
        closing=closing,
        attempt=close_attempt(wave_id=closing.id, required_gate_ids=["G-01"]),
    )
    _bind_receipt(state_path, wave_id=closing.id, gate_id="G-01", freshness_key="b" * 64)

    assert receipted_gate_ids(state_path, receipt_ids=["GR-" + "c" * 32]) == frozenset()


def test_receipted_gate_ids_skips_an_unparseable_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt row is treated as a missing one, never as proof."""
    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.store.paths import store_path

    closing = _one_gate_wave()
    _state, state_path = live_tree(
        tmp_path,
        monkeypatch,
        closing=closing,
        attempt=close_attempt(wave_id=closing.id, required_gate_ids=["G-01"]),
    )
    receipt_id = _bind_receipt(
        state_path, wave_id=closing.id, gate_id="G-01", freshness_key="d" * 64
    )
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    path.write_bytes(b"{not json}\n" + path.read_bytes())

    assert receipted_gate_ids(state_path, receipt_ids=[receipt_id]) == frozenset({"G-01"})


# --- the refusal itself ---------------------------------------------------


def test_enforce_gate_receipt_floor_names_every_missing_gate() -> None:
    """A multi-gate gap names all of them, sorted, so one pass repairs all."""
    with pytest.raises(GateReceiptFloorError) as refusal:
        enforce_gate_receipt_floor(
            scope_id="P32-I01-W43",
            criteria=[
                criterion("CR-01", gate_ids=["G-02"]),
                criterion("CR-02", gate_ids=["G-01"]),
            ],
            gates=[
                file_exists_gate("G-02", criterion_id="CR-01"),
                file_exists_gate("G-01", criterion_id="CR-02"),
            ],
            receipted=[],
        )

    assert refusal.value.missing_gate_ids == ["G-01", "G-02"]


def test_enforce_gate_receipt_floor_passes_on_partial_over_coverage() -> None:
    """Receipts for gates beyond the obliged set are surplus, never a gap."""
    assert enforce_gate_receipt_floor(
        scope_id="P32-I01-W43",
        criteria=[criterion("CR-01", gate_ids=["G-01"])],
        gates=[file_exists_gate("G-01", criterion_id="CR-01")],
        receipted=["G-01", "G-77"],
    ) == frozenset({"G-01"})


def test_enforce_gate_receipt_floor_reds_on_the_off_by_one_gap() -> None:
    """Two obliged, one receipted: the close is still refused."""
    with pytest.raises(GateReceiptFloorError) as refusal:
        enforce_gate_receipt_floor(
            scope_id="P32-I01-W43",
            criteria=[
                criterion("CR-01", gate_ids=["G-01"]),
                criterion("CR-02", gate_ids=["G-02"]),
            ],
            gates=[
                file_exists_gate("G-01", criterion_id="CR-01"),
                file_exists_gate("G-02", criterion_id="CR-02"),
            ],
            receipted=["G-01"],
        )

    assert refusal.value.missing_gate_ids == ["G-02"]
    assert refusal.value.obliged_gate_ids == ["G-01", "G-02"]
