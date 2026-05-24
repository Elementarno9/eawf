"""Property tests for Phase 5 W02 — random kill + resume convergence.

Hypothesis (per spec §6.3):

- ``kill_at`` ranges across all six step indices.
- ``tamper`` chooses one of {None, state, git, profile} to perturb the
  workspace between checkpoint write and resume.

Property: after the resume call, **either** the envelope's
``status == "ok"`` (clean resume) **or** the envelope's
``status == "blocked"`` (drift refusal) AND the resume produced a
populated ``drift`` body. In all branches the on-disk ``flow.jsonl``
parses end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.kernel.state.enums import FlowStatus, StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.flow import FlowCheckpointPayload, FlowPayload
from eawf.kernel.store.paths import store_path
from eawf.workflow.skills import flow as flow_module
from eawf.workflow.skills.flow import (
    _CORE_FLOW_ORDER,
    _canonical_args_per_step_hash,
    _current_profile_ids,
    _state_hash,
    load_flow_records,
)


def _seed_flow(
    repo: Path,
    *,
    flow_id: str,
    kill_at: int,
    parent_state_hash: str,
    parent_git_head: str | None,
    parent_profile_ids: list[str],
) -> tuple[str, str]:
    """Seed flow.jsonl with an in-progress flow + safe checkpoints up to
    kill_at. Returns (flow_record_id, last_safe_checkpoint_id)."""
    state_path = repo / ".ea" / "state.json"
    flow_jsonl = store_path(state_path, StoreKind.FLOW)

    # in_progress flow_record at the top.
    rec_payload = FlowPayload(
        flow_id=flow_id,
        goal="prop",
        policy={},
        last_safe_checkpoint=None,
        next_action=None,
        status=FlowStatus.IN_PROGRESS,
    )
    rec_id = "EV-rec000000aa"
    rec_envelope = Envelope(
        schema_version="1.0",
        id=rec_id,
        kind=StoreKind.FLOW,
        scope_id="urn:test",
        created_at=datetime.now(UTC),
        updated_at=None,
        summary=f"flow record in_progress {flow_id}",
        payload=rec_payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(flow_jsonl, rec_envelope)

    # Append safe checkpoints for steps 0..kill_at inclusive.
    last_id = ""
    for step_index in range(kill_at + 1):
        step_name = _CORE_FLOW_ORDER[step_index][0]
        envelope_id = f"EV-ckpt{step_index:08x}"
        ckpt_payload = FlowCheckpointPayload(
            flow_id=flow_id,
            step_index=step_index,
            step_name=step_name,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            last_safe=True,
            payload_hash="sha256:" + ("a" * 64),
            parent_state_hash=parent_state_hash,
            parent_git_head=parent_git_head,
            parent_profile_ids=parent_profile_ids,
            args_per_step_hash=_canonical_args_per_step_hash(None),
        )
        ckpt_envelope = Envelope(
            schema_version="1.0",
            id=envelope_id,
            kind=StoreKind.FLOW,
            scope_id="urn:test",
            created_at=datetime.now(UTC),
            updated_at=None,
            summary=f"checkpoint {step_index} {step_name}",
            payload=ckpt_payload.model_dump(mode="json"),
            blob_refs=[],
            artifact_ids=[],
        )
        append_envelope(flow_jsonl, ckpt_envelope)
        last_id = envelope_id

    return rec_id, last_id


@pytest.fixture(scope="function")
def isolated_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("flow-prop")
    state_dir = repo / ".ea"
    (state_dir / "store").mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    return repo


@given(
    kill_at=st.integers(min_value=0, max_value=len(_CORE_FLOW_ORDER) - 1),
    tamper=st.sampled_from(["none", "state", "git", "profile"]),
)
@settings(
    max_examples=24,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_random_kill_at_every_step_resume_converges_or_refuses_cleanly(
    tmp_path_factory: pytest.TempPathFactory,
    kill_at: int,
    tamper: str,
) -> None:
    # Each Hypothesis example must run on a fresh tmp dir. We construct
    # a per-example monkeypatch context so env vars and module patches
    # are reset between examples (Hypothesis re-uses the outer
    # ``monkeypatch`` fixture, which would leak state across examples).
    with pytest.MonkeyPatch.context() as monkeypatch:
        repo = tmp_path_factory.mktemp("flow-prop-run")
        state_dir = repo / ".ea"
        (state_dir / "store").mkdir(parents=True, exist_ok=True)
        state_path = state_dir / "state.json"
        state_path.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("EA_STATE", str(state_path))
        monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))

        flow_id = "FL-aaaaaaaaaaaa"
        parent_hash = _state_hash(state_path)
        profile_ids = _current_profile_ids(state_path)
        # Pin a deterministic parent_git_head so the tamper="git" branch
        # can induce drift via monkey-patching alone.
        parent_git_head = "0" * 40

        _seed_flow(
            repo,
            flow_id=flow_id,
            kill_at=kill_at,
            parent_state_hash=parent_hash,
            parent_git_head=parent_git_head,
            parent_profile_ids=profile_ids,
        )

        # Apply the tamper between kill and resume.
        if tamper == "state":
            state_path.write_text('{"tampered": true}', encoding="utf-8")
            # Still neutralise git so only one drift dimension fires.
            monkeypatch.setattr(flow_module, "_current_git_head", lambda root: parent_git_head)
        elif tamper == "git":
            monkeypatch.setattr(flow_module, "_current_git_head", lambda root: "1" * 40)
        elif tamper == "profile":
            new_profile_list = [*profile_ids, "extra-profile-for-drift"]
            monkeypatch.setattr(flow_module, "_current_profile_ids", lambda sp: new_profile_list)
            monkeypatch.setattr(flow_module, "_current_git_head", lambda root: parent_git_head)
        else:
            # tamper == "none": neutralise the live git lookup so it
            # returns the seeded SHA on a non-repo workspace (otherwise
            # a real lookup returns None and the (None, "0*40) pair
            # registers as drift).
            monkeypatch.setattr(flow_module, "_current_git_head", lambda root: parent_git_head)

        runner = CliRunner()
        result = runner.invoke(app, ["--json", "flow", "run", "--resume"])

        # Property: either the resume converges (exit 0) OR refuses on
        # drift (exit 8 with a populated drift body).
        if result.exit_code == 0:
            payload = orjson.loads(result.stdout)
            body = payload["body"]
            assert body["drift"] is None
            assert body["resume_from_checkpoint_id"] is not None
        else:
            assert result.exit_code == 3, result.stdout
            payload = orjson.loads(result.stdout)
            assert payload["error"] == "IntegrityViolation"
            assert payload["drift"], "drift dict must be populated on refusal"

        # Invariant: flow.jsonl parses end-to-end (no corruption).
        records = load_flow_records(state_path)
        assert isinstance(records, list)
        for envelope_id, payload_dict in records:
            assert isinstance(envelope_id, str) and envelope_id
            assert isinstance(payload_dict, dict)
            assert payload_dict.get("kind") in {"flow_record", "flow_checkpoint"}
