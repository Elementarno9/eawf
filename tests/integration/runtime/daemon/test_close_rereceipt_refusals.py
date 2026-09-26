"""What ``close.rereceipt`` refuses, and what it records instead.

A re-receipt writes evidence about a wave nobody may reopen, so every way
the request can be wrong has to end in a refusal with no binding row --
otherwise the store would accumulate rows describing a revision, a wave
revision or a history that never existed. This suite pins each refusal and
the one case that is deliberately NOT a refusal: a gate that fails is
recorded as a ``fail`` receipt and the wave stays CLOSED.

Coverage:

* refusals with no binding row: a wave that is not CLOSED, an unknown
  wave, a wave with no gates, a wave with no ``Wave.commit``, a pin that
  does not resolve, a pin that is not an ancestor of ``HEAD``, and a wave
  row that moved between the gate run and the bind;
* ``at`` refusals: a re-bind commit that does not resolve, does not
  descend from the landed commit, or is off ``HEAD``;
* params: an unknown key is an invalid-params ``ValueError``, not a
  validation refusal;
* recorded failure: a failing gate binds a ``fail`` receipt and leaves the
  wave CLOSED;
* the binding row's own invariants (receipt list agreement, no repeated
  gate, at least one gate).

The refusal paths stub the gate runner or never reach it, so only the one
recorded-failure case pays for a real child process.

The helpers come from the happy-path suite next door rather than a second
copy: both drive the same fixture repository and the same ledger shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.state.models import State
from eawf.kernel.store.kinds.gate_rereceipt import (
    GateRereceiptBinding,
    GateRereceiptOutcome,
)
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods import close_rereceipt as rereceipt_method
from eawf.runtime.daemon.methods.close_rereceipt import rereceipt
from tests.integration.runtime.daemon.test_close_rereceipt import (
    _FAIL_MODULE,
    _PASS_MODULE,
    _T0,
    _WAVE_ID,
    build_ctx,
    build_repo,
    build_state_payload,
    criterion,
    gate,
    git,
    load_wave,
    read_bindings,
    read_receipts,
    run,
    write_state,
)

pytestmark = pytest.mark.integration


def _expect_refusal(
    *,
    tmp_path: Path,
    repo: Path,
    state_path: Path,
    match: str,
    at: str | None = None,
) -> None:
    """Assert the re-receipt is refused and leaves the store empty."""
    ctx = build_ctx(tmp_path, state_path)
    params: dict[str, Any] = {"wave_id": _WAVE_ID, "repo_root": str(repo)}
    if at is not None:
        params["at"] = at

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match=match):
            await rereceipt(ctx, params)

    run(body)
    assert read_bindings(state_path) == []


def test_rereceipt_refuses_a_wave_that_is_not_closed(tmp_path: Path) -> None:
    """CR-02: an open wave is refused and binds nothing."""
    repo, landed = build_repo(tmp_path)
    state_path = write_state(
        repo,
        build_state_payload(
            commit=landed,
            criteria=[criterion(1)],
            gates=[gate(1, _PASS_MODULE)],
            status="in_progress",
        ),
    )
    _expect_refusal(
        tmp_path=tmp_path,
        repo=repo,
        state_path=state_path,
        match="is not closed",
    )


def test_rereceipt_refuses_a_wave_with_no_landed_commit(tmp_path: Path) -> None:
    """CR-02: an absent ``Wave.commit`` is refused and binds nothing."""
    repo, _landed = build_repo(tmp_path)
    state_path = write_state(
        repo,
        build_state_payload(
            commit=None,
            criteria=[criterion(1)],
            gates=[gate(1, _PASS_MODULE)],
        ),
    )
    _expect_refusal(
        tmp_path=tmp_path,
        repo=repo,
        state_path=state_path,
        match="has no landed commit",
    )


def test_rereceipt_refuses_a_commit_that_is_not_an_ancestor_of_head(tmp_path: Path) -> None:
    """CR-02: a pin off the current history is refused and binds nothing."""
    repo, landed = build_repo(tmp_path)
    git(repo, "checkout", "-b", "side", landed)
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    git(repo, "add", "side.txt")
    git(repo, "commit", "-m", "test: land work that never merged")
    off_history = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    state_path = write_state(
        repo,
        build_state_payload(
            commit=off_history,
            criteria=[criterion(1)],
            gates=[gate(1, _PASS_MODULE)],
        ),
    )
    _expect_refusal(
        tmp_path=tmp_path,
        repo=repo,
        state_path=state_path,
        match="is not an ancestor of HEAD",
    )


def test_rereceipt_refuses_a_commit_that_does_not_resolve(tmp_path: Path) -> None:
    """CR-02 boundary: a pin absent from the repository is refused."""
    repo, _landed = build_repo(tmp_path)
    state_path = write_state(
        repo,
        build_state_payload(
            commit="0" * 40,
            criteria=[criterion(1)],
            gates=[gate(1, _PASS_MODULE)],
        ),
    )
    _expect_refusal(
        tmp_path=tmp_path,
        repo=repo,
        state_path=state_path,
        match="does not resolve",
    )


def test_rereceipt_refuses_a_wave_with_no_gates(tmp_path: Path) -> None:
    """CR-02 boundary: an empty gate manifest has nothing to re-run."""
    repo, landed = build_repo(tmp_path)
    state_path = write_state(
        repo,
        build_state_payload(commit=landed, criteria=[], gates=[]),
    )
    _expect_refusal(
        tmp_path=tmp_path,
        repo=repo,
        state_path=state_path,
        match="records no gates",
    )


def test_rereceipt_refuses_an_unknown_wave(tmp_path: Path) -> None:
    """CR-02 boundary: an id no wave carries is refused before any git call."""
    repo, landed = build_repo(tmp_path)
    state_path = write_state(
        repo,
        build_state_payload(
            commit=landed,
            criteria=[criterion(1)],
            gates=[gate(1, _PASS_MODULE)],
        ),
    )
    ctx = build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="unknown wave"):
            await rereceipt(ctx, {"wave_id": "P32-I01-W99", "repo_root": str(repo)})

    run(body)
    assert read_bindings(state_path) == []


def test_rereceipt_rejects_unknown_params(tmp_path: Path) -> None:
    """CR-02 error path: a stray key is invalid params, not a refusal."""
    repo, landed = build_repo(tmp_path)
    state_path = write_state(
        repo,
        build_state_payload(
            commit=landed,
            criteria=[criterion(1)],
            gates=[gate(1, _PASS_MODULE)],
        ),
    )
    ctx = build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await rereceipt(
                ctx,
                {"wave_id": _WAVE_ID, "repo_root": str(repo), "force": True},
            )

    run(body)
    assert read_bindings(state_path) == []


def test_rereceipt_refuses_when_the_wave_row_moves_mid_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-02: a wave edited while its gates ran gets no binding row."""
    repo, landed = build_repo(tmp_path)
    state_path = write_state(
        repo,
        build_state_payload(
            commit=landed,
            criteria=[criterion(1)],
            gates=[gate(1, _PASS_MODULE)],
        ),
    )

    def _mutate_then_report(**kwargs: Any) -> list[GateRereceiptOutcome]:
        """Rewrite the wave's outcome the way a concurrent writer would."""
        payload = State.model_validate_json(state_path.read_text(encoding="utf-8")).model_dump(
            mode="json"
        )
        payload["waves"][_WAVE_ID]["outcome"] = "rewritten while the gates ran"
        state_path.write_text(
            State.model_validate(payload).model_dump_json(),
            encoding="utf-8",
        )
        return [
            GateRereceiptOutcome(
                gate_id="GATE-01",
                criterion_id="CR-01",
                result=GateReceiptResult.PASS,
                receipt_id="GR-" + "a" * 32,
            )
        ]

    monkeypatch.setattr(rereceipt_method, "_run_wave_gates", _mutate_then_report)
    _expect_refusal(
        tmp_path=tmp_path,
        repo=repo,
        state_path=state_path,
        match="changed while its gates re-ran",
    )


def test_rereceipt_records_a_failing_gate_and_leaves_the_wave_closed(tmp_path: Path) -> None:
    """CR-02: a gate that fails becomes a fail receipt, not a refusal."""
    repo, landed = build_repo(tmp_path)
    state_path = write_state(
        repo,
        build_state_payload(
            commit=landed,
            criteria=[criterion(1)],
            gates=[gate(1, _FAIL_MODULE)],
        ),
    )
    ctx = build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await rereceipt(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo)})
        assert result["passed_count"] == 0
        assert result["failed_count"] == 1

    run(body)

    bindings = read_bindings(state_path)
    assert len(bindings) == 1
    binding = bindings[0]
    assert [row.result for row in binding.gates] == [GateReceiptResult.FAIL]
    assert len(binding.receipt_ids) == 1
    receipt = read_receipts(state_path)[binding.receipt_ids[0]]
    assert receipt.result == GateReceiptResult.FAIL
    assert receipt.integrated_sha == landed

    wave = load_wave(state_path)
    assert wave.status.value == "closed"
    assert wave.outcome == "ok"


def _single_gate_state(repo: Path, commit: str) -> Path:
    """Write a one-passing-gate wave landed at *commit*; return its ledger."""
    return write_state(
        repo,
        build_state_payload(
            commit=commit,
            criteria=[criterion(1)],
            gates=[gate(1, _PASS_MODULE)],
        ),
    )


def test_rereceipt_at_refuses_a_commit_that_does_not_descend_from_the_landed_one(
    tmp_path: Path,
) -> None:
    """Error path: re-binding to history older than the landed commit is refused."""
    repo, landed = build_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    state_path = _single_gate_state(repo, head)
    _expect_refusal(
        tmp_path=tmp_path,
        repo=repo,
        state_path=state_path,
        match="does not descend from its landed commit",
        at=landed,
    )


def test_rereceipt_at_refuses_a_commit_that_is_not_on_the_branch(tmp_path: Path) -> None:
    """Error path: a descendant that never reached HEAD is refused."""
    repo, landed = build_repo(tmp_path)
    git(repo, "checkout", "-b", "side", landed)
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    git(repo, "add", "side.txt")
    git(repo, "commit", "-m", "test: fix that never merged")
    off_branch = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    state_path = _single_gate_state(repo, landed)
    _expect_refusal(
        tmp_path=tmp_path,
        repo=repo,
        state_path=state_path,
        match="re-bind commit .* is not an ancestor of HEAD",
        at=off_branch,
    )


@pytest.mark.parametrize("at", ["0" * 40, "no-such-ref", "--help"])
def test_rereceipt_at_refuses_an_unresolvable_commit(tmp_path: Path, at: str) -> None:
    """Error path: an ``at`` that names no commit is refused, options included."""
    repo, landed = build_repo(tmp_path)
    state_path = _single_gate_state(repo, landed)
    _expect_refusal(
        tmp_path=tmp_path,
        repo=repo,
        state_path=state_path,
        match="re-bind commit .* does not resolve",
        at=at,
    )


def test_rereceipt_at_rejects_an_empty_commit(tmp_path: Path) -> None:
    """Boundary: an empty ``at`` is invalid params, not a landed run."""
    repo, landed = build_repo(tmp_path)
    state_path = _single_gate_state(repo, landed)
    ctx = build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await rereceipt(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo), "at": ""})

    run(body)
    assert read_bindings(state_path) == []


def _outcome(gate_id: str, receipt_id: str | None) -> GateRereceiptOutcome:
    """One passing outcome row for the binding-model invariants."""
    return GateRereceiptOutcome(
        gate_id=gate_id,
        criterion_id="CR-01",
        result=GateReceiptResult.PASS,
        receipt_id=receipt_id,
    )


def _binding_kwargs(gates: list[GateRereceiptOutcome], receipt_ids: list[str]) -> dict[str, Any]:
    """Binding-row constructor kwargs with only the gate view varying."""
    return {
        "id": "GRR-0123456789ab",
        "wave_id": _WAVE_ID,
        "landed_sha": "a" * 40,
        "landed_tree_sha": "b" * 40,
        "criteria_digest": "sha256:" + "c" * 64,
        "gate_manifest_digest": "sha256:" + "d" * 64,
        "wave_fingerprint_digest": "sha256:" + "e" * 64,
        "receipt_ids": receipt_ids,
        "gates": gates,
        "ran_at": _T0,
    }


def test_binding_rejects_receipt_ids_that_no_gate_produced() -> None:
    """CR-02 error path: the flat receipt list must come from the gate rows."""
    with pytest.raises(ValidationError, match="receipt_ids must list exactly"):
        GateRereceiptBinding.model_validate(
            _binding_kwargs([_outcome("GATE-01", None)], ["GR-" + "a" * 32]),
        )


def test_binding_rejects_a_repeated_gate_id() -> None:
    """CR-02 error path: one gate cannot appear twice in one run."""
    gates = [_outcome("GATE-01", "GR-" + "a" * 32), _outcome("GATE-01", "GR-" + "b" * 32)]
    with pytest.raises(ValidationError, match="gates must not repeat a gate id"):
        GateRereceiptBinding.model_validate(
            _binding_kwargs(gates, ["GR-" + "a" * 32, "GR-" + "b" * 32]),
        )


def test_binding_rejects_an_empty_gate_list() -> None:
    """CR-02 boundary: a run that touched no gate is not a binding."""
    with pytest.raises(ValidationError):
        GateRereceiptBinding.model_validate(_binding_kwargs([], []))


def test_binding_parses_a_row_written_before_rebinding() -> None:
    """Boundary: a pre-rebind row with no bound fields still parses."""
    binding = GateRereceiptBinding.model_validate(
        _binding_kwargs([_outcome("GATE-01", "GR-" + "a" * 32)], ["GR-" + "a" * 32]),
    )
    assert binding.bound_sha is None
    assert binding.bound_tree_sha is None


def test_binding_rejects_a_bound_sha_without_its_tree() -> None:
    """Error path: a re-bound row must name the tree its gates ran on."""
    kwargs = _binding_kwargs([_outcome("GATE-01", "GR-" + "a" * 32)], ["GR-" + "a" * 32])
    with pytest.raises(ValidationError, match="must be set together"):
        GateRereceiptBinding.model_validate({**kwargs, "bound_sha": "f" * 40})


def test_binding_rejects_a_bound_sha_equal_to_the_landed_one() -> None:
    """Error path: re-binding to the landed commit is not a re-bind."""
    kwargs = _binding_kwargs([_outcome("GATE-01", "GR-" + "a" * 32)], ["GR-" + "a" * 32])
    with pytest.raises(ValidationError, match="must differ from landed_sha"):
        GateRereceiptBinding.model_validate(
            {**kwargs, "bound_sha": "a" * 40, "bound_tree_sha": "b" * 40},
        )
