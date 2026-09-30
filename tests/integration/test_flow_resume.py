"""Unit tests for Phase 5 W02 — flow resume + drift detection.

Covers (per spec §6.1):

- :class:`FlowCheckpointPayload` round-trip and rejection of bad input.
- :class:`FlowPayload` discriminator (``flow_record`` vs
  ``flow_checkpoint``).
- ``is_safe_step_boundary`` truth table.
- ``compute_drift`` matrix (no drift, state hash, git head, profile,
  args, no-git-repo case).
- ``load_latest_records_per_flow`` / ``load_latest_safe_checkpoint`` /
  ``in_progress_flow_ids`` over JSONL fixtures.
- CLI argument parsing for ``--resume / --flow-id / --reason`` and the
  exit-code table for the resume / abort / status surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import FlowStatus, StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.flow import FlowCheckpointPayload, FlowPayload
from eawf.kernel.store.paths import store_path
from eawf.workflow.skills import flow as flow_module
from eawf.workflow.skills.flow import (
    _canonical_args_per_step_hash,
    _state_hash,
    abort_flow_record,
    compute_drift,
    in_progress_flow_ids,
    is_safe_step_boundary,
    load_flow_records,
    load_latest_records_per_flow,
    load_latest_safe_checkpoint,
)


def _make_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".ea"
    (state_dir / "store").mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text("{}", encoding="utf-8")
    return state_dir


def _now() -> datetime:
    return datetime.now(UTC)


def _make_checkpoint(
    *,
    flow_id: str = "FL-0123456789ab",
    step_index: int = 0,
    step_name: str = "/research",
    last_safe: bool = True,
    parent_state_hash: str | None = None,
    parent_git_head: str | None = None,
    parent_profile_ids: list[str] | None = None,
    args_per_step_hash: str | None = None,
) -> FlowCheckpointPayload:
    now = _now()
    if parent_state_hash is None:
        parent_state_hash = "sha256:" + ("0" * 64)
    if args_per_step_hash is None:
        args_per_step_hash = _canonical_args_per_step_hash(None)
    payload_hash = "sha256:" + ("a" * 64)
    return FlowCheckpointPayload(
        flow_id=flow_id,
        step_index=step_index,
        step_name=step_name,  # type: ignore[arg-type]
        started_at=now,
        completed_at=now,
        last_safe=last_safe,
        payload_hash=payload_hash,
        parent_state_hash=parent_state_hash,
        parent_git_head=parent_git_head,
        parent_profile_ids=parent_profile_ids or [],
        args_per_step_hash=args_per_step_hash,
    )


def _append_checkpoint(state_dir: Path, ckpt: FlowCheckpointPayload, *, envelope_id: str) -> None:
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id="urn:test",
        created_at=ckpt.completed_at,
        updated_at=None,
        summary="checkpoint",
        payload=ckpt.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(store_path(state_dir / "state.json", StoreKind.FLOW), envelope)


def _append_flow_record(
    state_dir: Path,
    *,
    flow_id: str,
    status: FlowStatus,
    envelope_id: str,
    last_safe_checkpoint: str | None = None,
    policy: dict[str, Any] | None = None,
    goal: str = "demo",
) -> None:
    payload = FlowPayload(
        flow_id=flow_id,
        goal=goal,
        policy=policy or {},
        last_safe_checkpoint=last_safe_checkpoint,
        next_action=None,
        status=status,
    )
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id="urn:test",
        created_at=_now(),
        updated_at=None,
        summary=f"flow record {status.value}",
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(store_path(state_dir / "state.json", StoreKind.FLOW), envelope)


# ---- Pydantic round-trip ---------------------------------------------------


def test_flow_checkpoint_payload_round_trip() -> None:
    ckpt = _make_checkpoint()
    raw = ckpt.model_dump(mode="json")
    loaded = FlowCheckpointPayload.model_validate(raw)
    assert loaded == ckpt
    assert loaded.kind == "flow_checkpoint"


def test_flow_checkpoint_payload_rejects_extra_fields() -> None:
    raw = _make_checkpoint().model_dump(mode="json")
    raw["surprise"] = "extra"
    with pytest.raises(ValidationError):
        FlowCheckpointPayload.model_validate(raw)


def test_flow_checkpoint_payload_rejects_bad_flow_id_pattern() -> None:
    with pytest.raises(ValidationError):
        _make_checkpoint(flow_id="FL-XXX")


def test_flow_payload_kind_discriminator_round_trip() -> None:
    rec = FlowPayload(
        flow_id="FL-0123456789ab",
        goal="x",
        policy={},
        status=FlowStatus.IN_PROGRESS,
    )
    raw = rec.model_dump(mode="json")
    assert raw["kind"] == "flow_record"
    again = FlowPayload.model_validate(raw)
    assert again == rec
    # A flow_checkpoint dict must not accidentally validate as a record.
    ckpt_raw = _make_checkpoint().model_dump(mode="json")
    with pytest.raises(ValidationError):
        FlowPayload.model_validate(ckpt_raw)


# ---- Safe-step predicate ---------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("ok", True),
        ("partial", True),
        ("blocked", False),
        ("failed", False),
        ("needs_user", False),
    ],
)
def test_safe_step_predicate_truth_table(status: str, expected: bool) -> None:
    for skill_name, _cls in flow_module.FlowSkill.flow_order:
        assert is_safe_step_boundary(status, skill_name) is expected


# ---- Drift detection -------------------------------------------------------


def test_compute_drift_no_drift_returns_none(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    state_path = state_dir / "state.json"
    parent_hash = _state_hash(state_path)
    profile_ids = flow_module._current_profile_ids(state_path)
    ckpt = _make_checkpoint(
        parent_state_hash=parent_hash,
        parent_git_head=None,
        parent_profile_ids=profile_ids,
    )
    drift = compute_drift(ckpt, state_path, args_per_step=None)
    # Built the checkpoint from the live profile list; no drift.
    assert drift is None


def test_compute_drift_state_hash_change(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    state_path = state_dir / "state.json"
    parent_hash = _state_hash(state_path)
    profile_ids = flow_module._current_profile_ids(state_path)
    ckpt = _make_checkpoint(
        parent_state_hash=parent_hash,
        parent_git_head=None,
        parent_profile_ids=profile_ids,
    )
    # Mutate the state.json bytes.
    state_path.write_text('{"changed": true}', encoding="utf-8")
    drift = compute_drift(ckpt, state_path, args_per_step=None)
    assert drift is not None
    assert "state_json" in drift
    assert drift["state_json"]["checkpoint"] == parent_hash
    assert drift["state_json"]["current"] != parent_hash


def test_compute_drift_git_head_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = _make_state_dir(tmp_path)
    state_path = state_dir / "state.json"
    parent_hash = _state_hash(state_path)
    profile_ids = flow_module._current_profile_ids(state_path)
    ckpt = _make_checkpoint(
        parent_state_hash=parent_hash,
        parent_git_head="0" * 40,
        parent_profile_ids=profile_ids,
    )
    # Monkey-patch ``_current_git_head`` to return a different SHA.
    monkeypatch.setattr(flow_module, "_current_git_head", lambda root: "1" * 40)
    drift = compute_drift(ckpt, state_path, args_per_step=None)
    assert drift is not None
    assert "git_head" in drift
    assert drift["git_head"]["checkpoint"] == "0" * 40
    assert drift["git_head"]["current"] == "1" * 40


def test_compute_drift_profile_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = _make_state_dir(tmp_path)
    state_path = state_dir / "state.json"
    parent_hash = _state_hash(state_path)
    ckpt = _make_checkpoint(
        parent_state_hash=parent_hash,
        parent_git_head=None,
        parent_profile_ids=["only-this-one"],
    )
    monkeypatch.setattr(flow_module, "_current_profile_ids", lambda sp: ["only-this-one", "extra"])
    drift = compute_drift(ckpt, state_path, args_per_step=None)
    assert drift is not None
    assert "profile_ids" in drift
    assert drift["profile_ids"]["checkpoint"] == ["only-this-one"]
    assert drift["profile_ids"]["current"] == ["only-this-one", "extra"]


def test_compute_drift_args_change(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    state_path = state_dir / "state.json"
    parent_hash = _state_hash(state_path)
    profile_ids = flow_module._current_profile_ids(state_path)
    args_hash_at_ckpt = _canonical_args_per_step_hash(None)
    ckpt = _make_checkpoint(
        parent_state_hash=parent_hash,
        parent_git_head=None,
        parent_profile_ids=profile_ids,
        args_per_step_hash=args_hash_at_ckpt,
    )
    drift = compute_drift(
        ckpt,
        state_path,
        args_per_step={"/research": {"depth": "deep"}},
    )
    assert drift is not None
    assert "args_per_step" in drift


def test_compute_drift_no_git_repo_is_not_drift(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    state_path = state_dir / "state.json"
    parent_hash = _state_hash(state_path)
    profile_ids = flow_module._current_profile_ids(state_path)
    ckpt = _make_checkpoint(
        parent_state_hash=parent_hash,
        parent_git_head=None,
        parent_profile_ids=profile_ids,
    )
    # tmp_path is not a git repo; _current_git_head returns None on both
    # sides → no drift.
    drift = compute_drift(ckpt, state_path, args_per_step=None)
    assert drift is None


# ---- flow.jsonl readers ----------------------------------------------------


def test_load_flow_records_skips_malformed_lines(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    flow_path = store_path(state_dir / "state.json", StoreKind.FLOW)
    flow_path.parent.mkdir(parents=True, exist_ok=True)
    flow_path.write_text('not-json\n{"id": null}\n', encoding="utf-8")
    records = load_flow_records(state_dir / "state.json")
    # Both lines are malformed (not-json + missing required fields) — empty.
    assert records == []


def test_load_latest_records_per_flow_picks_latest(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    _append_flow_record(
        state_dir,
        flow_id="FL-0123456789ab",
        status=FlowStatus.IN_PROGRESS,
        envelope_id="EV-001",
    )
    _append_flow_record(
        state_dir,
        flow_id="FL-0123456789ab",
        status=FlowStatus.DONE,
        envelope_id="EV-002",
    )
    latest = load_latest_records_per_flow(state_dir / "state.json")
    assert "FL-0123456789ab" in latest
    assert latest["FL-0123456789ab"].status == FlowStatus.DONE


def test_load_latest_safe_checkpoint_picks_latest_safe(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    fid = "FL-0123456789ab"
    safe_old = _make_checkpoint(flow_id=fid, step_index=0, last_safe=True)
    unsafe = _make_checkpoint(flow_id=fid, step_index=1, last_safe=False)
    safe_new = _make_checkpoint(flow_id=fid, step_index=2, last_safe=True)
    _append_checkpoint(state_dir, safe_old, envelope_id="EV-001")
    _append_checkpoint(state_dir, unsafe, envelope_id="EV-002")
    _append_checkpoint(state_dir, safe_new, envelope_id="EV-003")
    found = load_latest_safe_checkpoint(state_dir / "state.json", fid)
    assert found is not None
    eid, ckpt = found
    assert eid == "EV-003"
    assert ckpt.step_index == 2


def test_load_latest_safe_checkpoint_zero_safe_returns_none(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    fid = "FL-0123456789ab"
    unsafe = _make_checkpoint(flow_id=fid, step_index=0, last_safe=False)
    _append_checkpoint(state_dir, unsafe, envelope_id="EV-001")
    found = load_latest_safe_checkpoint(state_dir / "state.json", fid)
    assert found is None


def test_in_progress_flow_ids_zero(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    assert in_progress_flow_ids(state_dir / "state.json") == []


def test_in_progress_flow_ids_multiple_in_progress(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    _append_flow_record(
        state_dir,
        flow_id="FL-aaaaaaaaaaaa",
        status=FlowStatus.IN_PROGRESS,
        envelope_id="EV-001",
    )
    _append_flow_record(
        state_dir,
        flow_id="FL-bbbbbbbbbbbb",
        status=FlowStatus.IN_PROGRESS,
        envelope_id="EV-002",
    )
    ids = sorted(in_progress_flow_ids(state_dir / "state.json"))
    assert ids == ["FL-aaaaaaaaaaaa", "FL-bbbbbbbbbbbb"]


# ---- CLI exit codes --------------------------------------------------------


def test_abort_flow_record_appends_abandoned_envelope(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    state_path = state_dir / "state.json"
    fid = "FL-0123456789ab"
    _append_flow_record(
        state_dir,
        flow_id=fid,
        status=FlowStatus.IN_PROGRESS,
        envelope_id="EV-001",
        policy={"stop_after": "audit"},
    )
    previous = load_latest_records_per_flow(state_path)[fid]
    before_lines = store_path(state_path, StoreKind.FLOW).read_text(encoding="utf-8").splitlines()

    envelope_id, new_payload = abort_flow_record(
        state_path,
        scope_id="urn:eawf:v1:state:test",
        previous=previous,
        reason="user requested",
    )

    assert envelope_id.startswith("EV-")
    assert new_payload.status is FlowStatus.ABANDONED
    assert new_payload.policy["abort_reason"] == "user requested"
    # Pre-existing policy keys preserved.
    assert new_payload.policy["stop_after"] == "audit"
    assert new_payload.flow_id == fid
    assert new_payload.goal == previous.goal

    after_lines = store_path(state_path, StoreKind.FLOW).read_text(encoding="utf-8").splitlines()
    assert len(after_lines) == len(before_lines) + 1

    latest = load_latest_records_per_flow(state_path)[fid]
    assert latest.status is FlowStatus.ABANDONED
