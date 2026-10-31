"""The promotion write path and a production spike-report filer.

``_submit_evidence`` promotes into the tree's selected generation: one
line of its evidence ledger, appended through the leak-refusing native
ledger commit, and never a write of the v1 ``state.json``, which a marked
tree refuses to every writer. These tests drive the registered daemon
verbs (never a private test-only filer) to prove that, and that a spike
report filed by its own production verb
(``runtime.evidence.spike_report.file``) is resolved by
``submit_evidence`` end to end, with no test-only filer in the loop.

Driven through the same registered-verb dispatch
:mod:`tests.integration.runtime.daemon.test_semantic_tool_handlers` uses,
so what runs is the shipped gateway and the shipped handlers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusalCode, TransactionRefusedError
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.semantic_handlers import EVIDENCE_SPIKE_REPORT_FILE_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import root_context
from tests.integration.runtime.daemon.test_semantic_tool_handlers import (
    PROJECT_SCOPE,
    RUN_URN,
    bind,
    call_verb,
    evidence_payload,
    executor_capsule,
    invoke,
    make_canary,
    method_context,
    seal_call,
    seed_project,
    spike_report_payload,
    task_scope,
)

pytestmark = pytest.mark.integration

ACTOR: str = "OP-0001"
V1_STATE_FIXTURE: Final = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


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
    seed_project(task_canary)
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
    assert output["contract_refs"] == [f"urn:eawf:v1:artifact:{PROJECT_SCOPE}/MCT-99990101"]


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


def test_submit_evidence_files_its_promotion_through_one_ledger_append(
    task_canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promotion is one generation ledger line and touches no v1 file."""
    import eawf.runtime.daemon.semantic_handlers as handlers_mod

    capsule = executor_capsule(tool_grants=("budget_status", "submit_evidence"))
    bind(ctx, task_canary, capsule)
    seed_project(task_canary)
    ref = "artifact://spike/run-w17-04"
    filed = file_via_daemon(
        ctx,
        task_canary,
        ref=ref,
        report=spike_report_payload(report_id="RPT-W17-04", contract_id="MCT-99990104"),
    )

    appended: list[LedgerRecord] = []
    real_append = handlers_mod.commit_ledger_append

    def spy_append(session: object, record: LedgerRecord, **kwargs: object) -> object:
        appended.append(record)
        return real_append(session, record, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(handlers_mod, "commit_ledger_append", spy_append)

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

    assert answer["result"]["bounded_output"]["accepted"] is True
    assert [(item.collection, item.record_key) for item in appended] == [
        (Epoch2Collection.EVIDENCE, "RPT-W17-04")
    ]
    ea = Path(task_canary.root) / ".ea"
    assert not (ea / "state.json").exists()
    assert not (ea / "store" / "evidence.jsonl").exists()


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (
            TransactionRefusedError(
                code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
                detail="seeded failure: simulated leak refusal",
                entity_ref=RUN_URN,
                remediation="Remove the leak and retry.",
            ),
            "state_leak_refused",
        ),
        (OSError(28, "No space left on device"), "state_write_failed"),
        (PermissionError(13, "Permission denied"), "state_write_failed"),
    ],
    ids=["leak_refusal", "os_error", "os_error_subclass"],
)
def test_a_failed_ledger_append_is_a_typed_refusal_with_nothing_promoted(
    task_canary: CanaryProvision,
    ctx: MethodContext,
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    code: str,
) -> None:
    """A refused or failed append answers a finding and leaves the ledger as it was."""
    import eawf.runtime.daemon.semantic_handlers as handlers_mod

    capsule = executor_capsule(tool_grants=("budget_status", "submit_evidence"))
    bind(ctx, task_canary, capsule)
    seed_project(task_canary)
    ref = "artifact://spike/run-w17-05"
    filed = file_via_daemon(
        ctx,
        task_canary,
        ref=ref,
        report=spike_report_payload(report_id="RPT-W17-05", contract_id="MCT-99990105"),
    )

    def seeded_failure(session: object, record: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(handlers_mod, "commit_ledger_append", seeded_failure)

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
    context = root_context(task_canary, runtime_root)
    with context.session([RUN_URN]) as session:
        assert read_ledger_records(session.ledger_path(Epoch2Collection.EVIDENCE)) == ()
    assert not (Path(task_canary.root) / ".ea" / "state.json").exists()
