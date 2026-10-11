"""A fresh host's certify walk earns the semantic tools a later Task Run is granted.

On a tree that holds no certification, ``eawf runtime certify --task`` must
grant its canary the semantic tools, record ``semantic_tools`` once the canary
files through them, and leave a Task Run dispatched afterwards granted its
patch and report tools. A canary worker that answers without filing is one
model's miss, so the walk dispatches another canary rather than recording a
certification under which every later Run is granted no tool.

The canary's worker is the certifier suite's fake launcher, so no agent binary
runs and no model is reached.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.runtime_certifier import CANARY_ATTEMPTS
from eawf.runtime.mcp.grant import SEMANTIC_TOOLS_CAPABILITY
from eawf.runtime.runtimes.adapter import NativeLaunchOutcome, NativeLaunchRequest, TurnOutcome
from eawf.workflow.evidence.machine_certification import read_machine_certifications
from tests.integration.runtime.daemon.test_certifier_canary import (
    CanaryLauncher,
    _private_harness,  # noqa: F401  (the autouse harness every walk runs against)
    dispatch_task,
    make_tree,
    outcomes,
    refused_dispatch,
    walk,
)
from tests.integration.runtime.daemon.test_native_dispatch import method_ctx
from tests.integration.runtime.daemon.test_task_dispatch import runs

pytestmark = pytest.mark.integration

#: The tools a Task Run must reach to land its edit and bind its report.
TASK_RUN_TOOLS = (SemanticToolId.WORKSPACE_APPLY_PATCH.value, SemanticToolId.SUBMIT_REPORT.value)


class MissingCanaryLauncher(CanaryLauncher):
    """A canary worker that answers without filing its report on its first *misses* turns."""

    def __init__(self, ctx: MethodContext, canary: CanaryProvision, *, misses: int) -> None:
        """Miss the report on the first *misses* launches and file it on every later one."""
        super().__init__(ctx, canary)
        self.capsules: list[AuthorityCapsule] = []
        self._misses = misses

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Keep each launch's capsule, then launch as the certifier suite's worker does."""
        self.capsules.append(request.capsule)
        return await super().launch(request)

    async def _turn(self, request: NativeLaunchRequest) -> TurnOutcome:
        self._report = len(self.prompts) > self._misses
        return await super()._turn(request)


def semantic(row: MachineCertification) -> bool:
    """Return whether *row* carries the semantic tools."""
    return SEMANTIC_TOOLS_CAPABILITY in {item.capability_id for item in row.capabilities}


@pytest.fixture
def fresh(tmp_path: Path) -> tuple[MethodContext, CanaryProvision]:
    """Return a method context and a tree that holds no certification yet."""
    canary = make_tree(tmp_path)
    assert read_machine_certifications(canary.root / ".ea" / "state.json") == ()
    return method_ctx(tmp_path / "runtime"), canary


def test_the_canary_run_is_granted_the_semantic_tools(
    fresh: tuple[MethodContext, CanaryProvision],
) -> None:
    ctx, canary = fresh
    launcher = MissingCanaryLauncher(ctx, canary, misses=0)

    walk(ctx, canary, launcher)

    (capsule,) = launcher.capsules
    assert set(TASK_RUN_TOOLS) <= set(capsule.tool_grants)


def test_a_canary_that_files_through_the_tools_certifies_them(
    fresh: tuple[MethodContext, CanaryProvision],
) -> None:
    ctx, canary = fresh

    row = walk(ctx, canary, MissingCanaryLauncher(ctx, canary, misses=0))

    assert row.outcome == "certified"
    assert semantic(row)
    worker = dispatch_task(ctx, canary)
    assert worker.capsule is not None
    assert set(TASK_RUN_TOOLS) <= set(worker.capsule.tool_grants)


@pytest.mark.parametrize("misses", [1, CANARY_ATTEMPTS - 1], ids=["one-miss", "last-chance"])
def test_a_canary_that_misses_its_report_is_dispatched_again(
    fresh: tuple[MethodContext, CanaryProvision], misses: int
) -> None:
    ctx, canary = fresh
    launcher = MissingCanaryLauncher(ctx, canary, misses=misses)

    row = walk(ctx, canary, launcher)

    assert len(launcher.prompts) == misses + 1
    assert all(set(TASK_RUN_TOOLS) <= set(capsule.tool_grants) for capsule in launcher.capsules)
    assert row.outcome == "certified"
    assert semantic(row)
    assert "filed no report" not in row.reason
    worker = dispatch_task(ctx, canary)
    assert worker.capsule is not None
    assert set(TASK_RUN_TOOLS) <= set(worker.capsule.tool_grants)


def test_every_canary_dispatched_is_its_own_run_under_the_task(
    fresh: tuple[MethodContext, CanaryProvision],
) -> None:
    ctx, canary = fresh

    walk(ctx, canary, MissingCanaryLauncher(ctx, canary, misses=1))

    assert runs(ctx, canary) == ["RUN-00000001", "RUN-00000002"]


def test_a_worker_that_never_files_stops_after_the_bounded_canaries(
    fresh: tuple[MethodContext, CanaryProvision],
) -> None:
    ctx, canary = fresh
    launcher = MissingCanaryLauncher(ctx, canary, misses=CANARY_ATTEMPTS)

    row = walk(ctx, canary, launcher)

    assert len(launcher.prompts) == CANARY_ATTEMPTS
    assert row.outcome == "certified"
    assert not semantic(row)
    assert f"filed no report through {SEMANTIC_TOOLS_CAPABILITY}" in row.reason
    assert "eawf runtime certify claude-code --task" in row.reason
    rows = read_machine_certifications(canary.root / ".ea" / "state.json")
    assert [len(item.stages) for item in rows] == [0, 2] * CANARY_ATTEMPTS
    assert "dispatch_semantic_tools_uncertified" in str(refused_dispatch(ctx, canary))


def test_a_quarantined_canary_is_not_dispatched_again(
    fresh: tuple[MethodContext, CanaryProvision],
) -> None:
    ctx, canary = fresh
    escaped = {"attempt": "host_process_metadata", "denied": False, "returned_bytes": 5}
    answer = json.dumps({"outcomes": outcomes(host_process_metadata=escaped)})
    launcher = CanaryLauncher(ctx, canary, answer=answer, report=False)

    row = walk(ctx, canary, launcher)

    assert row.outcome == "quarantined"
    assert len(launcher.prompts) == 1


def test_a_reported_canary_records_one_walk_within_its_lifetime(
    fresh: tuple[MethodContext, CanaryProvision],
) -> None:
    ctx, canary = fresh

    row = walk(ctx, canary, MissingCanaryLauncher(ctx, canary, misses=0))

    rows = read_machine_certifications(canary.root / ".ea" / "state.json")
    assert [len(item.stages) for item in rows] == [0, 2]
    assert row.expires_at is not None
    assert row.expires_at - datetime.now(UTC) > timedelta(days=1)
