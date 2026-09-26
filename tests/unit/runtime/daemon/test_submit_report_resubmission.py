"""A Run's accepted report is never silently replaced or silently dropped.

``submit_report`` records one accepted report per Run, and ``/integrate
seal`` reads that record back when the caller omits the report fields. A
later submission naming another body or verdict must therefore be
refused -- answering it ``accepted`` while keeping the first row would
let the seal bind a report the worker has already replaced. A retry of
the same report stays an accepted, idempotent answer.

Driven through the registered semantic-call verb and the real
``/integrate`` skill, so what runs is the shipped gateway and handler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import MethodContext
from eawf.workflow.skills import integrate as integrate_skill
from eawf.workflow.skills.engine import SkillContext
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import root_context
from tests.integration.runtime.daemon.test_semantic_tool_handlers import (
    RUN_URN,
    bind,
    digest,
    executor_capsule,
    invoke,
    make_canary,
    method_context,
    seal_call,
    seal_fixture,
    skill_caller,
    task_scope,
)

REPORT_GRANTS: tuple[str, ...] = ("budget_status", "submit_report")


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


@pytest.fixture
def capsule(ctx: MethodContext, task_canary: CanaryProvision) -> AuthorityCapsule:
    """An executor capsule granted ``submit_report``, bound to the Run."""
    granted = executor_capsule(tool_grants=REPORT_GRANTS)
    bind(ctx, task_canary, granted)
    return granted


def submit(
    ctx: MethodContext,
    canary: CanaryProvision,
    capsule: AuthorityCapsule,
    *,
    body_digest: str,
    verdict: str,
    ordinal: int,
) -> dict[str, Any]:
    """Submit one report under its own idempotency key and return the output."""
    answer = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_report",
            key=f"report-key-{ordinal:02d}",
            ordinal=ordinal,
            payload={
                "tool_id": "submit_report",
                "report_schema_ref": capsule.report_schema_ref,
                "contract_digest": capsule.contract_digest,
                "body_ref": f"artifact://report/executor/ar-{ordinal:04d}",
                "body_digest": body_digest,
                "verdict": verdict,
            },
        ),
        capsule,
    )
    assert answer["result"]["status"] == "succeeded", answer["result"]
    output: dict[str, Any] = answer["result"]["bounded_output"]
    return output


def accepted_rows(canary: CanaryProvision, runtime_root: Path) -> list[dict[str, Any]]:
    """Return every accepted-report row the run ledger holds."""
    context = root_context(canary, runtime_root)
    with context.session([RUN_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    return [
        dict(item.payload)
        for item in records
        if item.payload.get("payload_kind") == "accepted_report"
    ]


def test_submit_report_first_submission_records_exactly_one_row(
    task_canary: CanaryProvision,
    ctx: MethodContext,
    capsule: AuthorityCapsule,
    runtime_root: Path,
) -> None:
    """Boundary: a single submission is accepted and filed once."""
    output = submit(ctx, task_canary, capsule, body_digest=digest("7"), verdict="fail", ordinal=1)

    assert output["accepted"] is True
    rows = accepted_rows(task_canary, runtime_root)
    assert len(rows) == 1
    assert rows[0]["report_digest"] == digest("7")
    assert rows[0]["verdict"] == "fail"


def test_submit_report_retry_of_the_same_report_is_idempotent(
    task_canary: CanaryProvision,
    ctx: MethodContext,
    capsule: AuthorityCapsule,
    runtime_root: Path,
) -> None:
    """Boundary: resending the standing report is accepted without a second row."""
    submit(ctx, task_canary, capsule, body_digest=digest("7"), verdict="pass", ordinal=1)
    again = submit(ctx, task_canary, capsule, body_digest=digest("7"), verdict="pass", ordinal=2)

    assert again["accepted"] is True
    assert again["findings"] == []
    assert len(accepted_rows(task_canary, runtime_root)) == 1


@pytest.mark.parametrize(
    ("body_digest", "verdict", "field_path"),
    [
        (digest("8"), "fail", "/body_digest"),
        (digest("7"), "pass", "/verdict"),
        (digest("8"), "pass", "/body_digest"),
    ],
    ids=["other_digest", "other_verdict", "both_differ"],
)
def test_submit_report_resubmission_naming_another_report_is_refused(
    task_canary: CanaryProvision,
    ctx: MethodContext,
    capsule: AuthorityCapsule,
    runtime_root: Path,
    body_digest: str,
    verdict: str,
    field_path: str,
) -> None:
    """Error path: a different report is refused, never accepted and dropped."""
    submit(ctx, task_canary, capsule, body_digest=digest("7"), verdict="fail", ordinal=1)

    output = submit(ctx, task_canary, capsule, body_digest=body_digest, verdict=verdict, ordinal=2)

    assert output["accepted"] is False
    assert [finding["code"] for finding in output["findings"]] == ["report_already_accepted"]
    assert output["findings"][0]["field_path"] == field_path
    rows = accepted_rows(task_canary, runtime_root)
    assert [(row["report_digest"], row["verdict"]) for row in rows] == [(digest("7"), "fail")]


def test_submit_report_seal_binds_the_one_report_the_run_accepted(
    task_canary: CanaryProvision,
    ctx: MethodContext,
    capsule: AuthorityCapsule,
    runtime_root: Path,
) -> None:
    """The seal binds exactly the report submit_report answered ``accepted`` for."""
    ref, tree_digest = seal_fixture(task_canary, runtime_root)
    submit(ctx, task_canary, capsule, body_digest=digest("7"), verdict="fail", ordinal=1)
    refused = submit(ctx, task_canary, capsule, body_digest=digest("8"), verdict="pass", ordinal=2)
    assert refused["accepted"] is False

    result = integrate_skill.IntegrateSkill(caller=skill_caller(ctx, task_canary)).action(
        SkillContext(
            scope="scope",
            session="session",
            args={
                "action": "seal",
                "subject_ref": ref,
                "run": RUN_URN,
                "resulting_tree_digest": tree_digest,
            },
        )
    )

    assert isinstance(result.body, dict)
    # The bound ``fail`` verdict is what keeps the candidate unsealed: had the
    # seal bound the refused ``pass`` resubmission, it would have sealed.
    assert result.body["outcome"] == "blocked"
    assert "verdict_admits_delivery" in result.body["reason"]
    context = root_context(task_canary, runtime_root)
    with context.session([RUN_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    bindings = [
        item.payload
        for item in records
        if item.payload.get("candidate_ref") == ref and "report_digest" in item.payload
    ]
    assert bindings
    assert {(row["report_digest"], row["verdict"]) for row in bindings} == {(digest("7"), "fail")}
