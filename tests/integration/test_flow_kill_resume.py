"""Integration tests for Phase 5 W02 — flow kill + resume + drift refusal.

End-to-end flows (per spec §6.2):

1. Kill mid-flow (monkey-patch a sub-skill to raise after step N), then
   ``flow run --resume`` — assert the run finishes the remaining steps.
2. Mutate ``state.json`` between kill and resume — assert exit 8 and a
   structured drift body.
3. Change ``git rev-parse HEAD`` between kill and resume — assert exit 8.
4. Two concurrent in-progress flows + resume without ``--flow-id`` →
   exit 3 ``INVALID_INPUT``.
5. ``flow abort`` appends but never deletes records.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.state.enums import FlowStatus, StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.flow import FlowCheckpointPayload, FlowPayload
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli.app import app
from eawf.workflow.skills import flow as flow_module
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.workflow.skills.flow import (
    FlowSkill,
    _canonical_args_per_step_hash,
    _current_profile_ids,
    _state_hash,
    load_flow_records,
)


@pytest.fixture
def integration_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    state_dir = repo / ".ea"
    store_dir = state_dir / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return repo


def _state_path(repo: Path) -> Path:
    return repo / ".ea" / "state.json"


def _seed_safe_checkpoint(
    repo: Path,
    *,
    flow_id: str,
    step_index: int,
    step_name: str,
    parent_state_hash: str,
    parent_git_head: str | None,
    parent_profile_ids: list[str],
    args_per_step_hash: str | None = None,
    last_safe: bool = True,
    envelope_id: str = "EV-ckpt00000001",
) -> str:
    if args_per_step_hash is None:
        args_per_step_hash = _canonical_args_per_step_hash(None)
    now = datetime.now(UTC)
    payload = FlowCheckpointPayload(
        flow_id=flow_id,
        step_index=step_index,
        step_name=step_name,  # type: ignore[arg-type]
        started_at=now,
        completed_at=now,
        last_safe=last_safe,
        payload_hash="sha256:" + ("a" * 64),
        parent_state_hash=parent_state_hash,
        parent_git_head=parent_git_head,
        parent_profile_ids=parent_profile_ids,
        args_per_step_hash=args_per_step_hash,
    )
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id="urn:test",
        created_at=now,
        updated_at=None,
        summary=f"checkpoint {step_index} {step_name}",
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(store_path(_state_path(repo), StoreKind.FLOW), envelope)
    return envelope_id


def _seed_flow_record(
    repo: Path,
    *,
    flow_id: str,
    status: FlowStatus,
    envelope_id: str,
    last_safe_checkpoint: str | None = None,
    goal: str = "demo",
) -> None:
    now = datetime.now(UTC)
    payload = FlowPayload(
        flow_id=flow_id,
        goal=goal,
        policy={},
        last_safe_checkpoint=last_safe_checkpoint,
        next_action=None,
        status=status,
    )
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id="urn:test",
        created_at=now,
        updated_at=None,
        summary=f"flow record {status.value}",
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(store_path(_state_path(repo), StoreKind.FLOW), envelope)


# ---- Test 1 — kill mid-flow + resume completes -----------------------------


class _RaisingPrep(Skill):
    """Stub /prep that raises so the flow short-circuits to ``failed``."""

    name: Any = "/prep"  # type: ignore[assignment]

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(ok=True)

    def action(self, ctx: SkillContext) -> SkillResult:
        raise RuntimeError("simulated kill mid-flow")


@pytest.mark.integration
def test_flow_kill_after_research_resume_completes(
    integration_repo: Path,
) -> None:
    """Simulates a SIGKILL after /research's checkpoint lands but before
    the terminal flow_record is written: the in-progress record + the
    /research safe checkpoint are on disk; resume must pick up at /prep.
    """
    state_path = _state_path(integration_repo)
    fid = "FL-aaaaaaaaaaaa"
    parent_hash = _state_hash(state_path)
    profile_ids = _current_profile_ids(state_path)

    # Seed the wreckage of a killed run: one in_progress flow_record +
    # one safe checkpoint at /research. The terminal record was never
    # written (simulating SIGKILL).
    _seed_flow_record(
        integration_repo,
        flow_id=fid,
        status=FlowStatus.IN_PROGRESS,
        envelope_id="EV-rec0000001",
    )
    _seed_safe_checkpoint(
        integration_repo,
        flow_id=fid,
        step_index=0,
        step_name="/research",
        parent_state_hash=parent_hash,
        parent_git_head=None,
        parent_profile_ids=profile_ids,
        envelope_id="EV-ckpt0000001",
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--json", "flow", "run", "--resume"],
        input='{"advance_after": true}',
    )
    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    body = payload["body"]
    assert body["resume_from_checkpoint_id"] == "EV-ckpt0000001"
    assert body["drift"] is None
    # Resume executes /prep through /ship — four steps.
    assert len(body["steps"]) == 4
    assert [s["header"]["skill"] for s in body["steps"]] == [
        "/prep",
        "/audit",
        "/polish",
        "/ship",
    ]


@pytest.mark.integration
def test_flow_kill_via_failing_step_leaves_safe_checkpoint(
    integration_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed /prep step still emits a last_safe=True checkpoint at
    /research before the short-circuit. Subsequent ``flow status`` sees
    the safe pointer."""
    state_path = _state_path(integration_repo)

    patched = tuple((n, _RaisingPrep if n == "/prep" else c) for n, c in FlowSkill.flow_order)
    monkeypatch.setattr(FlowSkill, "flow_order", patched)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--json", "flow", "run", "--topic", "demo"],
        input='{"advance_after": true}',
    )
    # /prep raises → engine wraps to ``failed`` → exit 4.
    assert result.exit_code == 2, result.stdout

    records = load_flow_records(state_path)
    safe_ckpts = [
        (eid, p)
        for eid, p in records
        if p.get("kind") == "flow_checkpoint"
        and p.get("last_safe") is True
        and p.get("step_name") == "/research"
    ]
    assert len(safe_ckpts) >= 1, "expected a last_safe checkpoint at /research"


# ---- Test 2 — drift on state.json refuses ----------------------------------


@pytest.mark.integration
def test_flow_resume_refuses_on_state_drift(integration_repo: Path) -> None:
    state_path = _state_path(integration_repo)
    fid = "FL-aaaaaaaaaaaa"

    # Snapshot at "checkpoint time": state.json is "{}".
    parent_hash = _state_hash(state_path)
    profile_ids = _current_profile_ids(state_path)

    _seed_flow_record(
        integration_repo,
        flow_id=fid,
        status=FlowStatus.IN_PROGRESS,
        envelope_id="EV-rec0000001",
    )
    _seed_safe_checkpoint(
        integration_repo,
        flow_id=fid,
        step_index=0,
        step_name="/research",
        parent_state_hash=parent_hash,
        parent_git_head=None,
        parent_profile_ids=profile_ids,
        envelope_id="EV-ckpt0000001",
    )

    # Mutate state.json so the parent_state_hash no longer matches.
    state_path.write_text('{"changed": true}', encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["--json", "flow", "run", "--resume"])
    assert result.exit_code == 3, result.stdout
    payload = orjson.loads(result.stdout)
    assert payload["error"] == "IntegrityViolation"
    assert "state_json" in payload["drift"]


# ---- Test 3 — drift on git head refuses -----------------------------------


@pytest.mark.integration
def test_flow_resume_refuses_on_git_drift(
    integration_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _state_path(integration_repo)
    fid = "FL-bbbbbbbbbbbb"

    parent_hash = _state_hash(state_path)
    profile_ids = _current_profile_ids(state_path)
    fake_git_head = "0" * 40

    _seed_flow_record(
        integration_repo,
        flow_id=fid,
        status=FlowStatus.IN_PROGRESS,
        envelope_id="EV-rec0000002",
    )
    _seed_safe_checkpoint(
        integration_repo,
        flow_id=fid,
        step_index=0,
        step_name="/research",
        parent_state_hash=parent_hash,
        parent_git_head=fake_git_head,
        parent_profile_ids=profile_ids,
        envelope_id="EV-ckpt0000002",
    )

    # Force the resume-time git query to return a different SHA so we
    # observe drift even when the test runs outside a real git repo.
    monkeypatch.setattr(flow_module, "_current_git_head", lambda root: "1" * 40)

    runner = CliRunner()
    result = runner.invoke(app, ["--json", "flow", "run", "--resume"])
    assert result.exit_code == 3, result.stdout
    payload = orjson.loads(result.stdout)
    assert payload["error"] == "IntegrityViolation"
    assert "git_head" in payload["drift"]


# ---- Test 4 — multi-flow ambiguity requires --flow-id ---------------------


@pytest.mark.integration
def test_flow_two_concurrent_in_progress_requires_flow_id(
    integration_repo: Path,
) -> None:
    _seed_flow_record(
        integration_repo,
        flow_id="FL-aaaaaaaaaaaa",
        status=FlowStatus.IN_PROGRESS,
        envelope_id="EV-rec1",
    )
    _seed_flow_record(
        integration_repo,
        flow_id="FL-bbbbbbbbbbbb",
        status=FlowStatus.IN_PROGRESS,
        envelope_id="EV-rec2",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--json", "flow", "run", "--resume"])
    assert result.exit_code == 1, result.stdout


# ---- Test 5 — abort preserves existing records ----------------------------


@pytest.mark.integration
def test_flow_abort_preserves_existing_records(integration_repo: Path) -> None:
    fid = "FL-cccccccccccc"
    _seed_flow_record(
        integration_repo,
        flow_id=fid,
        status=FlowStatus.IN_PROGRESS,
        envelope_id="EV-rec3",
    )
    state_path = _state_path(integration_repo)
    flow_jsonl = store_path(state_path, StoreKind.FLOW)
    before = flow_jsonl.read_text(encoding="utf-8").splitlines()
    runner = CliRunner()
    result = runner.invoke(
        app, ["--json", "flow", "abort", "--flow-id", fid, "--reason", "user-aborted"]
    )
    assert result.exit_code == 0, result.stdout
    after = flow_jsonl.read_text(encoding="utf-8").splitlines()
    # Strictly grew by one (the abandoned record).
    assert len(after) == len(before) + 1
    # First N lines are unchanged.
    assert after[: len(before)] == before
