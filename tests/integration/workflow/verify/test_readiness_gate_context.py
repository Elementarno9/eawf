"""Readiness reuses a gate the daemon close already claimed.

A daemon close binds its durable attempt around the whole mutation, runs the
enforcing oracle (which claims, executes and receipts every gate in a gate
child), and then computes readiness. Readiness used to ignore that binding:
every gate the oracle did not pre-validate ran AGAIN, in a second sandboxed
child that claimed nothing. :func:`eawf.workflow.verify.readiness.compute` now
defaults its gate context from the bound identity and compiles each gate with
the attempt's frozen facts, so it resolves the key the oracle claimed and the
gate child hands back the claimed result.

The tests drive the real oracle (:func:`_close_gate_helpers.score_close`)
inside the same binding the daemon uses, then flip the fixture tree so a
second run of either gate would answer differently than the claimed run did.
The readiness view therefore shows whether a gate was reused or re-run, and
spies on the two runners show which child each gate reached.

Every fixture tree is built under ``tmp_path``; nothing here reads or writes
the repository's own ``.ea/``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import CloseAttempt, State, Wave
from eawf.kernel.store.kinds.gate_receipt import GateReceipt, canonical_gate_digest
from eawf.kernel.store.paths import store_dir as _store_dir
from eawf.runtime.daemon import gate_execution
from eawf.runtime.daemon.gate_execution import GateExecutionContext, durable_gate_context
from eawf.runtime.daemon.gate_receipt_hygiene import append_gate_receipt
from eawf.runtime.daemon.methods.close_evidence import _gate_receipt_result
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec
from eawf.workflow.verify import readiness as readiness_mod
from eawf.workflow.verify.compile import compile_gate
from eawf.workflow.verify.models import CloseReadiness
from eawf.workflow.verify.sandboxed_checks import run_checks_out_of_process
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

#: The blocking gate asserts this file; it exists when the oracle runs and is
#: removed afterwards, so a re-run would fail where the claimed run passed.
_BLOCKING_TARGET = "payload.txt"

#: The advisory gate asserts this file; it is absent when the oracle runs and
#: created afterwards, so a re-run would pass where the claimed run failed.
_ADVISORY_TARGET = "late.txt"


def _closing_wave() -> Wave:
    """Return a mechanical wave with one blocking and one advisory gate."""
    return wave(
        criteria=[criterion("CR-01", gate_ids=["G-01", "G-02"])],
        gates=[
            file_exists_gate("G-01", criterion_id="CR-01", path=_BLOCKING_TARGET),
            file_exists_gate(
                "G-02",
                criterion_id="CR-01",
                path=_ADVISORY_TARGET,
                policy="warn",
            ),
        ],
        effort_bucket="M",
    )


def _bind_receipt_id(state_path: Path, receipt_id: str) -> None:
    """Add *receipt_id* to the fixture attempt row on disk."""
    state = State.model_validate_json(state_path.read_bytes())
    attempt = state.close_attempts[ATTEMPT_ID]
    payload = attempt.model_dump(mode="json")
    payload["gate_receipt_ids"] = [*attempt.gate_receipt_ids, receipt_id]
    state.close_attempts[ATTEMPT_ID] = CloseAttempt.model_validate(payload)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")


def _receipting_hook(
    state_path: Path,
    *,
    wave_id: str,
    prevalidated: set[str],
) -> Callable[[str, str, CheckResult], None]:
    """Return a stand-in for the daemon's receipt-commit hook.

    Writes the same three things the production hook does -- the receipt row,
    the attempt binding, and the completed claim -- and records a pass as
    pre-validated, so the readiness pass sees exactly what a daemon close
    leaves behind.

    Args:
        state_path: The fixture's ``state.json``.
        wave_id: Wave the receipts scope to.
        prevalidated: Accumulator for gate ids whose claimed run passed.

    Returns:
        The ``on_gate_result`` callback for :func:`score_close`.
    """

    def _on_gate_result(criterion_id: str, gate_id: str, result: CheckResult) -> None:
        assert result.freshness is not None, "the oracle compiled the gate without frozen facts"
        assert result.freshness_key is not None
        assert result.started_at is not None and result.ended_at is not None
        assert result.duration_ms is not None
        assert result.runner_fingerprint is not None
        assert result.environment_fingerprint is not None
        receipt = GateReceipt(
            id=gate_execution.gate_receipt_id(result.freshness_key),
            scope_id=wave_id,
            criterion_id=criterion_id,
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
            runner_digest=canonical_gate_digest(result.runner_fingerprint),
            environment_digest=canonical_gate_digest(result.environment_fingerprint),
            freshness_key=result.freshness_key,
            started_at=result.started_at,
            ended_at=result.ended_at,
            duration_ms=result.duration_ms,
            result=_gate_receipt_result(result),
        )
        append_gate_receipt(state_path, receipt)
        _bind_receipt_id(state_path, receipt.id)
        gate_execution.complete_gate_execution(
            state_path,
            attempt_id=ATTEMPT_ID,
            freshness_key=result.freshness_key,
            receipt_id=receipt.id,
            result=result,
        )
        if result.status == "pass":
            prevalidated.add(gate_id)

    return _on_gate_result


def _daemon_binding(state_path: Path, attempt_id: str = ATTEMPT_ID) -> GateExecutionContext:
    """Return the identity the daemon close worker binds for *attempt_id*."""
    return GateExecutionContext(state_path=state_path, attempt_id=attempt_id)


def _claim_through_the_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[State, Path, set[str]]:
    """Run the real enforcing oracle under the daemon binding, then flip the tree.

    Args:
        tmp_path: Scratch root the fixture repo and ledger live under.
        monkeypatch: Used to enforce the oracle and to keep readiness advisory.

    Returns:
        The loaded state, its ``state.json`` path, and the gate ids the
        claimed run pre-validated.
    """
    closing = _closing_wave()
    state, state_path = live_tree(
        tmp_path,
        monkeypatch,
        closing=closing,
        attempt=close_attempt(wave_id=closing.id, required_gate_ids=["G-01", "G-02"]),
    )
    (tmp_path / _BLOCKING_TARGET).write_text("gate target\n", encoding="utf-8")
    enforce_verify_block(monkeypatch, uiux_bands=[])
    # Readiness itself stays advisory so the test reads its view instead of
    # a refusal; the oracle run below is what enforces.
    monkeypatch.setattr(readiness_mod, "_load_active_verify_block", lambda *_a, **_k: None)
    prevalidated: set[str] = set()
    with durable_gate_context(_daemon_binding(state_path)):
        score_close(
            state,
            state_path=state_path,
            repo_root=tmp_path,
            close_attempt_id=ATTEMPT_ID,
            before_gate_execute=lambda *_args: pytest.fail(
                "the parent must not claim: the gate child owns the claim"
            ),
            on_gate_result=_receipting_hook(
                state_path, wave_id=closing.id, prevalidated=prevalidated
            ),
        )
    assert prevalidated == {"G-01"}
    (tmp_path / _BLOCKING_TARGET).unlink()
    (tmp_path / _ADVISORY_TARGET).write_text("arrived after the claimed run\n", encoding="utf-8")
    return state, state_path, prevalidated


class _RunnerSpy:
    """Records which child each readiness gate reached, then delegates."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.gate_children: list[tuple[str, GateExecutionContext]] = []
        self.advisory_children: list[list[str]] = []
        self.compiled_freshness: dict[str, bool] = {}
        real_gate = gate_execution.run_gate_out_of_process
        real_advisory = run_checks_out_of_process
        real_compile = compile_gate

        def _gate(spec: CheckSpec, **kwargs: Any) -> CheckResult:
            self.gate_children.append((kwargs["gate_id"], kwargs["context"]))
            return real_gate(spec, **kwargs)

        def _advisory(specs: list[CheckSpec], **kwargs: Any) -> list[CheckResult]:
            self.advisory_children.append([spec.name for spec in specs])
            return real_advisory(specs, **kwargs)

        def _compile(gate: Any, **kwargs: Any) -> CheckSpec | None:
            self.compiled_freshness[gate.id] = kwargs.get("freshness") is not None
            return real_compile(gate, **kwargs)

        monkeypatch.setattr(gate_execution, "run_gate_out_of_process", _gate)
        monkeypatch.setattr(readiness_mod, "run_checks_out_of_process", _advisory)
        monkeypatch.setattr(readiness_mod, "compile_gate", _compile)

    def gate_ids(self) -> list[str]:
        """Return the gate ids that reached the durable gate child, in order."""
        return [gate_id for gate_id, _context in self.gate_children]


@contextmanager
def _maybe_bound(context: GateExecutionContext | None) -> Iterator[None]:
    """Bind *context* like the daemon does, or bind nothing."""
    if context is None:
        yield
        return
    with durable_gate_context(context):
        yield


def _readiness(
    state: State,
    state_path: Path,
    repo_root: Path,
    **kwargs: Any,
) -> CloseReadiness:
    """Compute readiness for the fixture's single wave."""
    return readiness_mod.compute(
        next(iter(state.waves)),
        state=state,
        store_dir=_store_dir(state_path),
        repo_root=repo_root,
        **kwargs,
    )


def _gate_statuses(readiness: CloseReadiness) -> dict[str, str]:
    """Return ``gate_id -> status`` across every criterion view."""
    return {
        result.gate_id: result.status
        for view in readiness.criteria
        for result in view.gate_results or []
    }


# ---- the daemon close ----------------------------------------------------------


def test_compute_bound_close_reuses_claimed_gate_in_one_gate_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimed gate reaches exactly one gate child, which returns the claimed result.

    G-01 passed and is pre-validated, so readiness spawns nothing for it. G-02
    failed under a claim; its target now exists, so a second run would pass.
    Readiness reports the claimed ``fail`` and never enters the unclaimed
    sandboxed runner.
    """
    state, state_path, prevalidated = _claim_through_the_oracle(tmp_path, monkeypatch)
    spy = _RunnerSpy(monkeypatch)

    with durable_gate_context(_daemon_binding(state_path)):
        readiness = _readiness(state, state_path, tmp_path, prevalidated_gate_ids=prevalidated)

    assert _gate_statuses(readiness) == {"G-01": "pass", "G-02": "fail"}
    assert spy.gate_ids() == ["G-02"]
    assert spy.advisory_children == []
    assert spy.compiled_freshness == {"G-02": True}
    assert [context.attempt_id for _gate_id, context in spy.gate_children] == [ATTEMPT_ID]


def test_compute_bound_close_reuses_a_claimed_pass_when_not_prevalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recompute with no pre-validated ids still reuses each claimed result.

    G-01's target is gone, so a second run would fail; the reused claim keeps
    it ``pass``. One gate child per claimed gate, no unclaimed child.
    """
    state, state_path, _prevalidated = _claim_through_the_oracle(tmp_path, monkeypatch)
    spy = _RunnerSpy(monkeypatch)

    with durable_gate_context(_daemon_binding(state_path)):
        readiness = _readiness(state, state_path, tmp_path)

    assert _gate_statuses(readiness) == {"G-01": "pass", "G-02": "fail"}
    assert spy.gate_ids() == ["G-01", "G-02"]
    assert spy.advisory_children == []


def test_compute_without_bound_close_runs_the_unclaimed_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: with nothing bound, each gate runs again in the advisory child.

    This is the behaviour the default replaces inside a daemon close, and it is
    visible: both gates observe the flipped tree.
    """
    state, state_path, _prevalidated = _claim_through_the_oracle(tmp_path, monkeypatch)
    spy = _RunnerSpy(monkeypatch)

    readiness = _readiness(state, state_path, tmp_path)

    assert _gate_statuses(readiness) == {"G-01": "fail", "G-02": "pass"}
    assert spy.gate_ids() == []
    assert spy.advisory_children == [["G-01"], ["G-02"]]
    assert spy.compiled_freshness == {"G-01": False, "G-02": False}


# ---- boundaries and error paths ------------------------------------------------


def test_compute_explicit_gate_context_wins_over_the_bound_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a caller-passed identity is kept, and gets no frozen facts.

    The caller scores its own tree, which the bound attempt need not describe,
    so its gates compile unfrozen, claim fresh keys, and observe the tree as it
    is now.
    """
    state, state_path, _prevalidated = _claim_through_the_oracle(tmp_path, monkeypatch)
    spy = _RunnerSpy(monkeypatch)
    explicit = _daemon_binding(state_path, attempt_id="CA-EXPLICIT-01")

    with durable_gate_context(_daemon_binding(state_path)):
        readiness = _readiness(state, state_path, tmp_path, gate_context=explicit)

    assert _gate_statuses(readiness) == {"G-01": "fail", "G-02": "pass"}
    assert [context for _gate_id, context in spy.gate_children] == [explicit, explicit]
    assert spy.compiled_freshness == {"G-01": False, "G-02": False}
    assert spy.advisory_children == []


@pytest.mark.parametrize("bound", [True, False], ids=["bound", "unbound"])
def test_compute_bound_attempt_missing_from_state_compiles_unfrozen(
    bound: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a bound attempt with no row still routes through the gate child.

    With no frozen facts to compile, the gate claims its unfrozen key under the
    bound identity; nothing falls back to the unclaimed sandbox.
    """
    state, state_path, _prevalidated = _claim_through_the_oracle(tmp_path, monkeypatch)
    spy = _RunnerSpy(monkeypatch)
    orphan = _daemon_binding(state_path, attempt_id="CA-NO-ROW-01")

    with _maybe_bound(orphan if bound else None):
        readiness = _readiness(state, state_path, tmp_path)

    assert _gate_statuses(readiness) == {"G-01": "fail", "G-02": "pass"}
    assert spy.compiled_freshness == {"G-01": False, "G-02": False}
    if bound:
        assert [context for _gate_id, context in spy.gate_children] == [orphan, orphan]
        assert spy.advisory_children == []
    else:
        assert spy.gate_children == []
        assert spy.advisory_children == [["G-01"], ["G-02"]]


def test_compute_bound_close_unknown_wave_raises_before_any_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: an unknown wave raises ``KeyError`` and spawns nothing."""
    state, state_path, _prevalidated = _claim_through_the_oracle(tmp_path, monkeypatch)
    spy = _RunnerSpy(monkeypatch)

    with durable_gate_context(_daemon_binding(state_path)), pytest.raises(KeyError, match="W99"):
        readiness_mod.compute(
            "P32-I01-W99",
            state=state,
            store_dir=_store_dir(state_path),
            repo_root=tmp_path,
        )

    assert spy.gate_children == []
    assert spy.advisory_children == []
