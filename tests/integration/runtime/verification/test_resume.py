"""A timed-out gate leg resumes only the residue its progress manifest proves.

The leg is real: a gate child runs ``pytest`` with the progress plugin
loaded, the command reports each test through the channel, and one test
blocks until the leg's budget expires. While it blocks, this process (which
is not the runner) reads the durable manifest. After the timeout, a resumed
leg runs only the tests the manifest does not prove passed, and a resume
whose inputs changed, whose leg did not time out, or whose command published
nothing is refused rather than guessed.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.runtime.daemon import gate_execution
from eawf.runtime.verification.progress import (
    LegIdentity,
    LegLiveness,
    LegOutcome,
    ObligationDisposition,
    ObligationRecord,
    ProgressManifest,
    ProgressMode,
    collection_digest,
    list_progress_manifests,
    progress_manifest_id,
    read_progress_manifest,
)
from eawf.runtime.verification.resume import (
    ResumeKind,
    ResumePlan,
    ResumeReason,
    plan_resume,
    residue_digest,
    residue_proven,
    with_residue,
)
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec, GateFreshnessInput

#: Budget of the leg that times out. Long enough for the child and pytest to
#: start and finish the two quick tests before the blocking one.
_TIMEOUT_S = 10
_LEG_MODULE = "test_progress_leg.py"
_NODE = f"{_LEG_MODULE}::test_"
_PLUGIN_ARGV = [
    "pytest",
    "-p",
    "no:cacheprovider",
    "-p",
    "eawf.runtime.verification.progress",
    "-q",
    _LEG_MODULE,
]
_LEG_BODY = """
import pathlib
import time


def _ran(name):
    with open(EXECUTED, "a", encoding="utf-8") as handle:
        handle.write(name + "\\n")


def test_a():
    _ran("test_a")


def test_b():
    _ran("test_b")


def test_c():
    _ran("test_c")
    while pathlib.Path(BLOCK).exists():
        time.sleep(0.05)


def test_d():
    _ran("test_d")
"""


@dataclass(frozen=True)
class _TimedOutLeg:
    """One real leg that timed out, with what a reader saw while it ran."""

    state_path: Path
    cwd: Path
    executed: Path
    spec: CheckSpec
    result: CheckResult
    mid_run: ProgressManifest
    finished_while_read: bool


def _plant_leg(cwd: Path) -> tuple[Path, Path]:
    """Write the leg module into *cwd*; return its executed log and block flag."""
    executed = cwd / "executed.log"
    block = cwd / "block.flag"
    (cwd / _LEG_MODULE).write_text(
        f"EXECUTED = {str(executed)!r}\nBLOCK = {str(block)!r}\n{_LEG_BODY}",
        encoding="utf-8",
    )
    block.write_text("", encoding="utf-8")
    return executed, block


def _spec(*, tree: str, argv: list[str] | None = None, timeout_s: int = _TIMEOUT_S) -> CheckSpec:
    return CheckSpec(
        kind="command_exit_zero",
        name="G-RESUME",
        args={"argv": argv or _PLUGIN_ARGV, "scope": "all", "timeout_s": timeout_s},
        freshness=GateFreshnessInput(tree_digest=tree),
    )


def _run(state_path: Path, cwd: Path, spec: CheckSpec, *, resume: bool = False) -> CheckResult:
    return gate_execution.run_gate_out_of_process(
        spec,
        cwd=cwd,
        context=gate_execution.GateExecutionContext(state_path=state_path, attempt_id="CA-RES"),
        criterion_id="CR-01",
        gate_id="G-RESUME",
        resume=resume,
    )


def _await_manifest(
    state_path: Path,
    predicate: Callable[[ProgressManifest], bool],
    *,
    timeout: float = 60.0,
) -> ProgressManifest:
    """Poll the durable manifests as a non-runner until one satisfies *predicate*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for manifest in list_progress_manifests(state_path):
            if predicate(manifest):
                return manifest
        time.sleep(0.05)
    raise AssertionError(f"no manifest matched within {timeout}s")


def _passed(manifest: ProgressManifest) -> set[str]:
    return {
        record.obligation_id
        for record in manifest.obligations
        if record.disposition is ObligationDisposition.PASS
    }


@pytest.fixture(scope="module")
def timed_out_leg(tmp_path_factory: pytest.TempPathFactory) -> _TimedOutLeg:
    """Run one leg to its timeout while reading its manifest mid-run."""
    root = tmp_path_factory.mktemp("timed-out-leg")
    state_path = root / "live" / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    cwd = root / "repo"
    cwd.mkdir()
    executed, _block = _plant_leg(cwd)
    spec = _spec(tree="tree-1")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run, state_path, cwd, spec)
        mid_run = _await_manifest(
            state_path,
            lambda manifest: {f"{_NODE}a", f"{_NODE}b"} <= _passed(manifest),
        )
        finished_while_read = future.done()
        result = future.result(timeout=120)
    return _TimedOutLeg(
        state_path=state_path,
        cwd=cwd,
        executed=executed,
        spec=spec,
        result=result,
        mid_run=mid_run,
        finished_while_read=finished_while_read,
    )


def test_run_gate_out_of_process_manifest_is_durable_and_read_mid_run(
    timed_out_leg: _TimedOutLeg,
) -> None:
    """DEL-026/027: the command's progress is durable and readable before the leg ends."""
    mid = timed_out_leg.mid_run

    assert timed_out_leg.finished_while_read is False, "the manifest was only read after the leg"
    assert mid.outcome is LegOutcome.RUNNING
    assert mid.progress_mode is ProgressMode.ENUMERATED
    assert mid.collected == tuple(f"{_NODE}{name}" for name in "abcd")
    assert mid.pending() == (f"{_NODE}c", f"{_NODE}d")
    assert mid.cursor >= 3
    assert mid.writer_pid != os.getpid(), "the manifest was written by the reader's process"
    assert mid.liveness.resolved_timeout_seconds == _TIMEOUT_S
    assert mid.leg.gate_id == "G-RESUME"
    assert mid.leg.attempt_id == "CA-RES"


def test_run_gate_out_of_process_timeout_leaves_residue_manifest(
    timed_out_leg: _TimedOutLeg,
) -> None:
    result = timed_out_leg.result

    assert result.status == "blocked"
    assert result.exit_status is None
    assert result.freshness_key == timed_out_leg.mid_run.leg.freshness_key
    final = read_progress_manifest(timed_out_leg.state_path, result.freshness_key)
    assert final is not None
    assert final.outcome is LegOutcome.TIMED_OUT
    assert final.cursor >= timed_out_leg.mid_run.cursor
    assert _passed(final) == {f"{_NODE}a", f"{_NODE}b"}
    assert final.pending() == (f"{_NODE}c", f"{_NODE}d")
    assert final.liveness.elapsed_ms >= _TIMEOUT_S * 1_000


def test_run_gate_out_of_process_resume_refuses_manifest_of_other_inputs(
    timed_out_leg: _TimedOutLeg,
) -> None:
    """A changed input moves the key, so the old manifest is never trusted."""
    before = timed_out_leg.executed.read_text(encoding="utf-8")
    manifests_before = list_progress_manifests(timed_out_leg.state_path)

    refused = _run(
        timed_out_leg.state_path,
        timed_out_leg.cwd,
        _spec(tree="tree-2"),
        resume=True,
    )

    assert refused.status == "blocked"
    assert refused.details is not None
    assert "resume refused (missing)" in refused.details
    assert refused.started_at is None, "a refused resume must not be persistable as a receipt"
    assert timed_out_leg.executed.read_text(encoding="utf-8") == before, "the refused leg ran"
    assert list_progress_manifests(timed_out_leg.state_path) == manifests_before


def test_run_gate_out_of_process_resume_runs_only_the_residue(
    timed_out_leg: _TimedOutLeg,
) -> None:
    """DEL-026: the resumed leg reruns exactly the unproven obligations."""
    source_key = timed_out_leg.result.freshness_key
    assert source_key is not None
    timed_out = read_progress_manifest(timed_out_leg.state_path, source_key)
    assert timed_out is not None
    plan = plan_resume(timed_out, expected_freshness_key=source_key)
    (timed_out_leg.cwd / "block.flag").unlink(missing_ok=True)
    timed_out_leg.executed.write_text("", encoding="utf-8")

    resumed = _run(timed_out_leg.state_path, timed_out_leg.cwd, timed_out_leg.spec, resume=True)

    assert resumed.passed is True, resumed.details
    assert timed_out_leg.executed.read_text(encoding="utf-8").split() == ["test_c", "test_d"]
    assert resumed.freshness_key is not None
    assert resumed.freshness_key != source_key
    assert resumed.residual_manifest_digest == plan.residue_digest
    final = read_progress_manifest(timed_out_leg.state_path, resumed.freshness_key)
    assert final is not None
    assert final.outcome is LegOutcome.PASSED
    assert final.seed is not None
    assert final.seed.resumed_from == source_key
    assert final.seed.residue == (f"{_NODE}c", f"{_NODE}d")
    assert final.pending() == ()
    carried = {record.obligation_id for record in final.obligations if record.carried}
    assert carried == {f"{_NODE}a", f"{_NODE}b"}
    assert residue_proven(final, plan) is True


def test_run_gate_out_of_process_resume_refuses_leg_that_did_not_time_out(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    executed, block = _plant_leg(tmp_path)
    block.unlink()
    spec = _spec(tree="tree-pass")

    first = _run(state_path, tmp_path, spec)
    refused = _run(state_path, tmp_path, spec, resume=True)

    assert first.passed is True, first.details
    assert refused.status == "blocked"
    assert refused.details is not None
    assert "resume refused (not_timed_out)" in refused.details
    assert executed.read_text(encoding="utf-8").split() == ["test_a", "test_b", "test_c", "test_d"]


def test_run_gate_out_of_process_resume_refuses_progress_opaque_leg(tmp_path: Path) -> None:
    """DEL-031: a command that published nothing cannot prove any residue."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    executed, _block = _plant_leg(tmp_path)
    opaque_argv = ["pytest", "-p", "no:cacheprovider", "-q", _LEG_MODULE]
    spec = _spec(tree="tree-opaque", argv=opaque_argv, timeout_s=3)

    timed_out = _run(state_path, tmp_path, spec)
    manifest = read_progress_manifest(state_path, timed_out.freshness_key or "")
    refused = _run(state_path, tmp_path, spec, resume=True)

    assert timed_out.status == "blocked"
    assert manifest is not None
    assert manifest.progress_mode is ProgressMode.NONE
    assert manifest.outcome is LegOutcome.TIMED_OUT
    assert manifest.liveness.resolved_timeout_seconds == 3
    assert refused.details is not None
    assert "resume refused (opaque)" in refused.details
    assert executed.read_text(encoding="utf-8").split() == ["test_a", "test_b", "test_c"]


_KEY = "e" * 64
_COLLECTED = ("t::a", "t::b", "t::c", "t::d")


def _manifest(**overrides: Any) -> ProgressManifest:
    now = datetime.now(UTC)
    records = overrides.pop(
        "obligations",
        (
            ObligationRecord(
                obligation_id="t::a", disposition=ObligationDisposition.PASS, completed_at=now
            ),
            ObligationRecord(
                obligation_id="t::b", disposition=ObligationDisposition.FAIL, completed_at=now
            ),
            ObligationRecord(
                obligation_id="t::c", disposition=ObligationDisposition.PASS, completed_at=now
            ),
        ),
    )
    collected = overrides.pop("collected", _COLLECTED)
    fields: dict[str, Any] = {
        "id": progress_manifest_id(_KEY),
        "leg": LegIdentity(
            attempt_id="CA-01",
            criterion_id="CR-01",
            gate_id="G-01",
            freshness_key=_KEY,
            claimed_at=now,
        ),
        "producer_digest": "sha256:" + "f" * 64,
        "writer_pid": 1,
        "progress_mode": ProgressMode.NONE if collected is None else ProgressMode.ENUMERATED,
        "outcome": LegOutcome.TIMED_OUT,
        "collected": collected,
        "collection_digest": None if collected is None else collection_digest(collected),
        "obligations": () if collected is None else records,
        "cursor": 4,
        "liveness": LegLiveness(started_at=now, heartbeat_at=now, elapsed_ms=10),
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return ProgressManifest(**fields)


def test_plan_resume_reruns_failed_and_missing_obligations() -> None:
    plan = plan_resume(_manifest(), expected_freshness_key=_KEY)

    assert plan.kind is ResumeKind.RESIDUE
    assert plan.reason is ResumeReason.RESIDUE_PROVEN
    assert plan.residue == ("t::b", "t::d")
    assert [record.obligation_id for record in plan.carried] == ["t::a", "t::c"]
    assert all(record.carried for record in plan.carried)
    assert plan.residue_digest == residue_digest(
        source_freshness_key=_KEY,
        collection_digest=collection_digest(_COLLECTED),
        residue=("t::b", "t::d"),
    )


def test_plan_resume_reruns_invalidated_passes() -> None:
    plan = plan_resume(_manifest(), expected_freshness_key=_KEY, invalidated={"t::c"})

    assert plan.residue == ("t::b", "t::c", "t::d")
    assert [record.obligation_id for record in plan.carried] == ["t::a"]


def test_plan_resume_rejects_invalidated_obligation_outside_collection() -> None:
    with pytest.raises(ValueError, match="not in the collection"):
        plan_resume(_manifest(), expected_freshness_key=_KEY, invalidated={"t::zzz"})


@pytest.mark.parametrize(
    ("manifest", "expected_key", "reason"),
    [
        (None, _KEY, ResumeReason.MISSING),
        (_manifest(), "0" * 64, ResumeReason.STALE),
        (_manifest(outcome=LegOutcome.RUNNING), _KEY, ResumeReason.NOT_TIMED_OUT),
        (_manifest(outcome=LegOutcome.FAILED), _KEY, ResumeReason.NOT_TIMED_OUT),
        (_manifest(collected=None), _KEY, ResumeReason.OPAQUE),
    ],
)
def test_plan_resume_restarts_when_residue_is_unprovable(
    manifest: ProgressManifest | None,
    expected_key: str,
    reason: ResumeReason,
) -> None:
    plan = plan_resume(manifest, expected_freshness_key=expected_key)

    assert plan.kind is ResumeKind.FULL_RESTART
    assert plan.reason is reason
    assert plan.residue == ()
    assert plan.residue_digest is None


def test_plan_resume_restarts_when_every_obligation_passed() -> None:
    """Boundary: a timeout outside every obligation leaves no residue to prove."""
    now = datetime.now(UTC)
    everything = tuple(
        ObligationRecord(
            obligation_id=item, disposition=ObligationDisposition.PASS, completed_at=now
        )
        for item in _COLLECTED
    )

    plan = plan_resume(_manifest(obligations=everything), expected_freshness_key=_KEY)

    assert plan.kind is ResumeKind.FULL_RESTART
    assert plan.reason is ResumeReason.NO_RESIDUE


def test_plan_resume_single_obligation_residue() -> None:
    """Boundary: a one-test collection with nothing done resumes that one test."""
    plan = plan_resume(
        _manifest(collected=("t::only",), obligations=()), expected_freshness_key=_KEY
    )

    assert plan.residue == ("t::only",)
    assert plan.carried == ()


def test_resume_plan_rejects_forged_residue_digest() -> None:
    plan = plan_resume(_manifest(), expected_freshness_key=_KEY)

    with pytest.raises(ValidationError, match="residue_digest does not match"):
        ResumePlan.model_validate({**plan.model_dump(), "residue_digest": "0" * 64})


def test_resume_plan_rejects_restart_carrying_a_residue() -> None:
    with pytest.raises(ValidationError, match="carries no residue"):
        ResumePlan(
            source_freshness_key=_KEY,
            kind=ResumeKind.FULL_RESTART,
            reason=ResumeReason.STALE,
            residue=("t::a",),
        )


def test_resume_plan_rejects_kind_that_contradicts_reason() -> None:
    with pytest.raises(ValidationError, match="implies kind"):
        ResumePlan(source_freshness_key=_KEY, kind=ResumeKind.RESIDUE, reason=ResumeReason.STALE)


def test_with_residue_binds_the_residue_digest_into_freshness() -> None:
    plan = plan_resume(_manifest(), expected_freshness_key=_KEY)
    spec = _spec(tree="tree-1")

    bound = with_residue(spec, plan)

    assert bound.freshness is not None
    assert bound.freshness.residual_manifest_digest == plan.residue_digest
    assert bound.freshness.tree_digest == "tree-1"
    assert spec.freshness is not None
    assert spec.freshness.residual_manifest_digest is None


def test_with_residue_rejects_full_restart_plan() -> None:
    plan = plan_resume(None, expected_freshness_key=_KEY)

    with pytest.raises(ValueError, match="only a residue plan"):
        with_residue(_spec(tree="tree-1"), plan)


def test_residue_proven_false_when_collection_changed() -> None:
    plan = plan_resume(_manifest(), expected_freshness_key=_KEY)
    changed = _manifest(collected=("t::a", "t::b"), obligations=(), outcome=LegOutcome.PASSED)

    assert residue_proven(changed, plan) is False


def test_residue_proven_false_while_obligations_pending() -> None:
    plan = plan_resume(_manifest(), expected_freshness_key=_KEY)

    assert residue_proven(_manifest(outcome=LegOutcome.PASSED), plan) is False
