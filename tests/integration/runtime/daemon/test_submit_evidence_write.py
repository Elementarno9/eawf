"""The locked write chokepoint and a production spike-report filer.

``_submit_evidence`` used to persist ``state.json`` with a plain
``write_text`` -- no lock, no atomic write, no leak check -- and appended
its event and evidence rows before that write landed. These tests drive
the registered daemon verbs (never a private test-only filer) to prove
two things: the write now goes through the locked, leak-refusing
chokepoint with events appended only after it lands, and a spike report
filed by its own production verb (``runtime.evidence.spike_report.file``)
is resolved by ``submit_evidence`` end to end, with no test-only filer in
the loop.

Driven through the same registered-verb dispatch
:mod:`tests.integration.runtime.daemon.test_semantic_tool_handlers` uses,
so what runs is the shipped gateway and the shipped handlers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.io import StateValidationError
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.semantic_handlers import EVIDENCE_SPIKE_REPORT_FILE_METHOD
from eawf.workflow.evidence._io import store_paths
from tests.integration.runtime.daemon.test_semantic_tool_handlers import (
    RUN_URN,
    V1_STATE_SCOPE,
    bind,
    call_verb,
    evidence_payload,
    executor_capsule,
    invoke,
    make_canary,
    method_context,
    seal_call,
    seed_v1_state,
    spike_report_payload,
    task_scope,
)

pytestmark = pytest.mark.integration

ACTOR: str = "OP-0001"


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(runtime_root)


@pytest.fixture
def task_canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one running, task-scoped Run."""
    return make_canary(tmp_path / "repo", scope=task_scope())


def file_via_daemon(
    ctx: MethodContext, canary: CanaryProvision, *, ref: str, report: dict[str, object]
) -> dict[str, object]:
    """File one spike report through the registered production verb."""
    return call_verb(
        EVIDENCE_SPIKE_REPORT_FILE_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        actor=ACTOR,
        artifact_ref=ref,
        report=report,
    )


def test_a_report_filed_by_its_production_verb_resolves_through_submit_evidence(
    task_canary: CanaryProvision, ctx: MethodContext
) -> None:
    """CR-02: the production filer, not a test-only one, is what submit_evidence reads."""
    capsule = executor_capsule(tool_grants=("budget_status", "submit_evidence"))
    bind(ctx, task_canary, capsule)
    seed_v1_state(task_canary)
    ref = "artifact://spike/run-w17-01"
    filed = file_via_daemon(
        ctx,
        task_canary,
        ref=ref,
        report=spike_report_payload(report_id="RPT-W17-01", contract_id="MCT-99990101"),
    )
    assert filed["artifact_ref"] == ref

    answer = invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_evidence",
            payload=evidence_payload(
                spike_report_ref=ref, spike_report_digest=filed["content_digest"]
            ),
        ),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert answer["result"]["status"] == "succeeded"
    assert output["accepted"] is True
    assert output["contract_refs"] == [f"urn:eawf:v1:artifact:{V1_STATE_SCOPE}/MCT-99990101"]


def test_refiling_the_same_ref_with_the_same_content_replays(
    task_canary: CanaryProvision, ctx: MethodContext
) -> None:
    """Boundary: a retried filing after a dropped response is a no-op, not a conflict."""
    ref = "artifact://spike/run-w17-02"
    report = spike_report_payload(report_id="RPT-W17-02")
    first = file_via_daemon(ctx, task_canary, ref=ref, report=report)
    second = file_via_daemon(ctx, task_canary, ref=ref, report=report)
    assert first == second


def test_refiling_the_same_ref_with_different_content_is_refused(
    task_canary: CanaryProvision, ctx: MethodContext
) -> None:
    """Error path: a filed ref is an immutable revision, not a repointable target."""
    ref = "artifact://spike/run-w17-03"
    file_via_daemon(ctx, task_canary, ref=ref, report=spike_report_payload(report_id="RPT-W17-03A"))
    with pytest.raises(DaemonValidationError, match="spike_report_payload_conflict"):
        file_via_daemon(
            ctx, task_canary, ref=ref, report=spike_report_payload(report_id="RPT-W17-03B")
        )


def test_submit_evidence_writes_state_before_appending_any_event(
    task_canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-01: the atomic state write lands before either event row is appended."""
    import eawf.runtime.daemon.semantic_handlers as handlers_mod

    capsule = executor_capsule(tool_grants=("budget_status", "submit_evidence"))
    bind(ctx, task_canary, capsule)
    seed_v1_state(task_canary)
    ref = "artifact://spike/run-w17-04"
    filed = file_via_daemon(
        ctx,
        task_canary,
        ref=ref,
        report=spike_report_payload(report_id="RPT-W17-04", contract_id="MCT-99990104"),
    )

    order: list[str] = []
    real_write = handlers_mod.atomic_write_state
    real_append = handlers_mod.append_jsonl

    def spy_write(path: Path, state: object, **kwargs: object) -> None:
        order.append("write")
        real_write(path, state, **kwargs)

    def spy_append(path: Path, envelope: object) -> None:
        order.append("append")
        real_append(path, envelope)

    monkeypatch.setattr(handlers_mod, "atomic_write_state", spy_write)
    monkeypatch.setattr(handlers_mod, "append_jsonl", spy_append)

    answer = invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_evidence",
            payload=evidence_payload(
                spike_report_ref=ref, spike_report_digest=filed["content_digest"]
            ),
        ),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert output["accepted"] is True
    assert order == ["write", "append", "append"]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (StateValidationError("seeded failure: simulated leak refusal"), "state_leak_refused"),
        (OSError(28, "No space left on device"), "state_write_failed"),
        (PermissionError(13, "Permission denied"), "state_write_failed"),
    ],
    ids=["leak_refusal", "os_error", "os_error_subclass"],
)
def test_a_failed_state_write_is_a_typed_refusal_with_state_unchanged(
    task_canary: CanaryProvision,
    ctx: MethodContext,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    code: str,
) -> None:
    """CR-02: a refused or failed write answers a finding and leaves no orphan row."""
    import eawf.runtime.daemon.semantic_handlers as handlers_mod

    capsule = executor_capsule(tool_grants=("budget_status", "submit_evidence"))
    bind(ctx, task_canary, capsule)
    seed_v1_state(task_canary)
    ref = "artifact://spike/run-w17-05"
    filed = file_via_daemon(
        ctx,
        task_canary,
        ref=ref,
        report=spike_report_payload(report_id="RPT-W17-05", contract_id="MCT-99990105"),
    )

    def seeded_failure(path: Path, state: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(handlers_mod, "atomic_write_state", seeded_failure)

    state_path = Path(task_canary.root) / ".ea" / "state.json"
    before = state_path.read_bytes()
    event_path = store_paths(state_path)[StoreKind.EVENT]
    evidence_path = store_paths(state_path)[StoreKind.EVIDENCE]
    events_before = event_path.read_bytes() if event_path.exists() else b""
    evidence_before = evidence_path.read_bytes() if evidence_path.exists() else b""

    answer = invoke(
        ctx,
        task_canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_evidence",
            payload=evidence_payload(
                spike_report_ref=ref, spike_report_digest=filed["content_digest"]
            ),
        ),
        capsule,
    )

    output = answer["result"]["bounded_output"]
    assert answer["result"]["status"] == "succeeded"
    assert output["accepted"] is False
    assert output["contract_refs"] == []
    assert [finding["code"] for finding in output["findings"]] == [code]
    # The leak text can quote the leaking value, so it must not reach the receipt.
    assert "seeded failure" not in output["findings"][0]["message"]
    assert state_path.read_bytes() == before
    # The gateway still logs its own receipt rows; what must be absent is any
    # event naming the contract the refused write would have promoted.
    events_after = event_path.read_bytes() if event_path.exists() else b""
    assert events_after.startswith(events_before)
    assert b"MCT-99990105" not in events_after[len(events_before) :]
    assert (evidence_path.read_bytes() if evidence_path.exists() else b"") == evidence_before
