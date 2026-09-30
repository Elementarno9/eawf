"""RUN-062, UI-072: a brokered call states itself on its Run's transcript, once.

A call that passes the nine checks reaches the Run's own stream as ``tool_requested``,
``tool_accepted`` and a ``tool_result`` naming the receipt it earned; a refused call
states the request and the refusal's code and was never accepted; a replay of an
answered call adds nothing. The result then unfolds through ``runtime.run.content.read``
to the bounded output the gateway kept on its receipt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.run import RUN_EVENTS_READ_METHOD
from eawf.runtime.daemon.methods.run_content import RUN_CONTENT_READ_METHOD
from tests.integration.runtime.daemon.test_semantic_gateway_guards import (
    RUN_URN,
    bind,
    budget_payload,
    call_verb,
    invoke,
    make_canary,
    method_ctx,
    seal_call,
    seal_capsule,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    return make_canary(tmp_path / "repo")


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    return method_ctx(tmp_path / "runtime")


def _tool_lines(ctx: MethodContext, canary: CanaryProvision) -> list[dict[str, Any]]:
    answer = call_verb(RUN_EVENTS_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    return [line for line in answer["events"] if line["payload"]["payload_kind"] == "tool"]


def test_run_062_a_served_call_states_its_three_phases_and_unfolds_to_its_output(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    call = seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload())

    served = invoke(ctx, canary, call, capsule)
    replayed = invoke(ctx, canary, call, capsule)

    lines = _tool_lines(ctx, canary)
    assert replayed == served
    assert [line["event_kind"] for line in lines] == [
        "tool_requested",
        "tool_accepted",
        "tool_result",
    ], "a replay states nothing twice"
    assert {line["payload"]["call_ref"] for line in lines} == {served["call_id"]}
    assert {line["payload"]["tool_id"] for line in lines} == {"budget_status"}
    assert lines[-1]["payload"]["result_ref"] == served["receipt_id"]
    assert lines[-1]["provenance"] == "daemon_observed"
    sequences = [line["run_sequence"] for line in lines]
    assert sequences == sorted(sequences)

    read = call_verb(
        RUN_CONTENT_READ_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        refs=[served["call_id"], served["receipt_id"], "call-ffffffffffffffff"],
    )
    by_call, by_receipt = read["contents"]
    assert by_call["lines"] == by_receipt["lines"]
    assert '  "tool_id": "budget_status",' in by_call["lines"]
    assert read["missing"] == ["call-ffffffffffffffff"]


def test_run_062_a_refused_call_states_the_request_and_the_refusal_only(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule(tool_grants=("budget_status",))
    bind(ctx, canary, capsule)
    payload = {
        "tool_id": "attach_evidence",
        "subject_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042",
        "criterion_id": "RUN-062",
        "evidence_kind": "deterministic",
        "artifact_ref": "artifact://log/one",
    }
    denied = invoke(
        ctx, canary, seal_call(capsule=capsule, tool_id="attach_evidence", payload=payload), capsule
    )

    lines = _tool_lines(ctx, canary)
    assert denied["result"]["status"] == "denied"
    assert [line["event_kind"] for line in lines] == ["tool_requested", "tool_result"]
    assert lines[-1]["payload"]["error_code"] == "CAPABILITY_DENIED"
    assert lines[-1]["payload"]["result_ref"] is None
