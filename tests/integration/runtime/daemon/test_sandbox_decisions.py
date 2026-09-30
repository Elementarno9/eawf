"""UI-066, SURF-172: every call the gateway guards files a sandbox decision, read live.

A call the checks admit and a call they refuse each leave one ``SandboxDecision`` on the
receipt ledger, naming the rule that decided with its value in force and the sandbox
policy revision. The Sandbox log the daemon serves lists both, counted once, and draws
them in the packet's layout; ``eawf run report --parts sandbox_decisions`` writes both
into the Run's report; and a replayed call decides nothing twice.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.operations import build_operations_view
from eawf.kernel.runtime.sandbox_decision import SandboxDecisionOutcome, sandbox_decisions
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.reflect.run_report import ReportPartName, plan_run_report
from eawf.observability.reflect.runs import read_tree_run
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.semantic_gateway import SANDBOX_POLICY_REVISION
from eawf.surfaces.cli.app import app
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import SIZES, Session
from tests.integration.runtime.daemon.test_semantic_gateway_guards import (
    RUN_KEY,
    RUN_URN,
    TASK_URN,
    bind,
    budget_payload,
    call_verb,
    invoke,
    make_canary,
    method_ctx,
    root_ctx,
    seal_call,
    seal_capsule,
)

pytestmark = pytest.mark.integration

#: A call naming a tool the capsule never granted: refused by the grant check.
EVIDENCE: dict[str, Any] = {
    "tool_id": "attach_evidence",
    "subject_ref": TASK_URN,
    "criterion_id": "UI-066",
    "evidence_kind": "deterministic",
    "artifact_ref": "artifact://log/one",
}


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    return tmp_path / "runtime"


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    return make_canary(tmp_path / "repo")


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    return method_ctx(runtime_root)


@pytest.fixture
def decided(canary: CanaryProvision, ctx: MethodContext) -> CanaryProvision:
    """Return the canary after one admitted, one replayed and one refused call."""
    capsule = seal_capsule(tool_grants=("budget_status",))
    bind(ctx, canary, capsule)
    served = seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload())
    invoke(ctx, canary, served, capsule)
    invoke(ctx, canary, served, capsule)
    refused = seal_call(
        capsule=capsule, tool_id="attach_evidence", payload=EVIDENCE, key="call-key-02", ordinal=2
    )
    invoke(ctx, canary, refused, capsule)
    return canary


def _decisions(canary: CanaryProvision, runtime_root: Path) -> list[Any]:
    with root_ctx(canary, runtime_root).session([RUN_URN]) as session:
        return list(
            sandbox_decisions(read_ledger_records(session.ledger_path(Epoch2Collection.RECEIPT)))
        )


def test_ui_066_the_gateway_files_allowed_and_denied_decisions_once(
    decided: CanaryProvision, runtime_root: Path
) -> None:
    allowed, denied = _decisions(decided, runtime_root)

    assert allowed.decision is SandboxDecisionOutcome.ALLOWED
    assert (allowed.rule, allowed.rule_value) == ("grant", "grants budget_status")
    assert allowed.reason == "budget_status · admitted"
    assert denied.decision is SandboxDecisionOutcome.DENIED
    assert (denied.rule, denied.reason) == ("grant", "attach_evidence · capability_denied")
    assert {allowed.policy_revision, denied.policy_revision} == {SANDBOX_POLICY_REVISION}
    assert {allowed.run_ref.entity_key, denied.run_ref.entity_key} == {RUN_KEY}


def test_ui_066_the_served_sandbox_log_draws_both_decisions(
    decided: CanaryProvision, ctx: MethodContext
) -> None:
    served = call_verb("projection.sandbox.log.read", ctx, repo_root=str(decided.root))
    model = build_operations_view(RouteProjection.model_validate(served))
    session = Session()
    session.route = "sandbox.log"
    w = 120
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=w,
        h=dict(SIZES)[w],
        linked=True,
        projection=model,
    )

    frame = render_route(view)

    text = "\n".join(frame)
    assert "2 decisions · 1 denied · 2 of 2 shown" in text
    assert "attach_evidence · capability_denied" in text
    assert "budget_status · admitted" in text
    assert f"sandbox policy · rev {SANDBOX_POLICY_REVISION}" in text
    assert "artifact://log/one" not in text, "the call's raw target never reaches a row"


def test_surf_172_the_run_report_writes_every_decision_of_the_run(
    decided: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = decided.root / ".ea"
    reading = read_tree_run(tree, RUN_KEY)
    plan = plan_run_report(
        reading,
        parts=(ReportPartName.SANDBOX_DECISIONS,),
        actor=None,
        tree_root=tree,
        on=date(2026, 9, 30),
    )
    (part,) = (item for item in plan.parts if item.name is ReportPartName.SANDBOX_DECISIONS)
    assert len(part.lines) == 2
    assert "allowed budget_status · admitted · rule grant" in part.lines[0]
    assert "denied attach_evidence · capability_denied" in part.lines[1]
    assert all(
        line.endswith(f"sandbox policy · rev {SANDBOX_POLICY_REVISION}") for line in part.lines
    )

    monkeypatch.delenv("EA_STATE", raising=False)
    result = CliRunner().invoke(
        app, ["-w", str(decided.root), "run", "report", RUN_KEY, "--parts", "sandbox_decisions"]
    )
    assert result.exit_code == 0, result.stderr
    assert "part sandbox_decisions: 2 lines" in result.stderr
    written = Path(result.stdout.strip()).read_text(encoding="utf-8")
    assert "denied attach_evidence · capability_denied" in written
