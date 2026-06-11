"""Unit tests for the P30-I16-W22 git/state drift acknowledgement record.

Covers the committed-ack mechanism that lets ``eawf doctor`` stop warning on a
known, accepted historical-drift backlog:

1. :func:`eawf.workflow.lifecycle.wave_sha.load_drift_acks` /
   :func:`~eawf.workflow.lifecycle.wave_sha.save_drift_acks` round-trip and
   degrade gracefully on a missing / malformed ack file.
2. :func:`eawf.workflow.lifecycle.wave_sha.detect_git_state_drift` filters out
   acknowledged wave ids via the ``acked_wave_ids`` argument.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.workflow.lifecycle.wave_sha import (
    DRIFT_ACKS_DIRNAME,
    DRIFT_ACKS_FILENAME,
    detect_git_state_drift,
    drift_acks_path,
    load_drift_acks,
    save_drift_acks,
)


def _base_state_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ZZ",
        "updated_at": "2026-05-27T00:00:00Z",
        "project": {
            "code": "ZZ",
            "slug": "zz",
            "title": "ZZ",
            "description": "",
            "domains": [],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ZZ",
        },
        "current": {
            "project_code": "ZZ",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _wave_payload(wave_id: str, *, commit: str | None = None) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": wave_id.rsplit("-", 1)[0],
        "title": f"wave {wave_id}",
        "status": "closed",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "claim_session_id": None,
        "worktree_id": None,
        "token_budget": None,
        "tokens_consumed": 0,
        "outcome": None,
        "commit": commit,
        "opened_at": "2026-05-27T00:00:00Z",
        "closed_at": "2026-05-27T00:01:00Z",
    }


def _state_with_waves(waves: list[dict[str, Any]]) -> State:
    payload = _base_state_payload()
    payload["waves"] = {w["id"]: w for w in waves}
    return State.model_validate(payload)


# ---- path + load/save round-trip -------------------------------------------


def test_drift_acks_path_lives_outside_dot_ea(tmp_path: Path) -> None:
    """The ack file lives at ``<repo>/.eawf/drift-acks.json`` -- NOT under .ea/."""
    path = drift_acks_path(tmp_path)
    assert path == tmp_path / DRIFT_ACKS_DIRNAME / DRIFT_ACKS_FILENAME
    assert ".ea" not in path.parts  # daemon state authority untouched
    assert path.parts[-2] == ".eawf"


def test_load_drift_acks_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_drift_acks(tmp_path) == set()


def test_save_then_load_drift_acks_round_trips(tmp_path: Path) -> None:
    saved = save_drift_acks({"P30-I15-W12", "P22-I01-W05"}, tmp_path)
    assert saved.exists()
    assert load_drift_acks(tmp_path) == {"P30-I15-W12", "P22-I01-W05"}


def test_save_drift_acks_is_sorted_and_byte_stable(tmp_path: Path) -> None:
    """Re-saving the same set is a byte-stable no-op (diff-clean commits)."""
    first = save_drift_acks({"P30-I15-W19", "P22-I01-W05", "P30-I05-W09"}, tmp_path)
    body_one = first.read_text(encoding="utf-8")
    save_drift_acks({"P30-I05-W09", "P30-I15-W19", "P22-I01-W05"}, tmp_path)
    body_two = first.read_text(encoding="utf-8")
    assert body_one == body_two
    parsed = json.loads(body_one)
    assert parsed["acked_wave_ids"] == sorted(parsed["acked_wave_ids"])


def test_load_drift_acks_malformed_payload_is_empty(tmp_path: Path) -> None:
    """A corrupt ack file degrades to empty -- never crashes doctor."""
    path = drift_acks_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert load_drift_acks(tmp_path) == set()


def test_load_drift_acks_wrong_shape_is_empty(tmp_path: Path) -> None:
    """A JSON file missing the ``acked_wave_ids`` list yields an empty set."""
    path = drift_acks_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"other": [1, 2]}), encoding="utf-8")
    assert load_drift_acks(tmp_path) == set()


def test_load_drift_acks_skips_non_string_entries(tmp_path: Path) -> None:
    path = drift_acks_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"acked_wave_ids": ["P22-I01-W05", 42, None, "P30-I05-W09"]}),
        encoding="utf-8",
    )
    assert load_drift_acks(tmp_path) == {"P22-I01-W05", "P30-I05-W09"}


# ---- detect_git_state_drift filtering --------------------------------------


def test_detect_git_state_drift_filters_acked_wave(monkeypatch: pytest.MonkeyPatch) -> None:
    """An acknowledged wave id is dropped from the surfaced drift list."""
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.build_wave_sha_index",
        lambda repo_root=None: {},
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: None,
    )
    state = _state_with_waves(
        [
            _wave_payload("P28-I01-W01", commit="a" * 40),
            _wave_payload("P28-I01-W02", commit="b" * 40),
        ]
    )
    # Without acks both drift (pinned_but_missing).
    assert len(detect_git_state_drift(state)) == 2
    # Ack the first; only the second survives.
    drifts = detect_git_state_drift(state, acked_wave_ids={"P28-I01-W01"})
    assert [d.wave_id for d in drifts] == ["P28-I01-W02"]


def test_detect_git_state_drift_all_acked_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.build_wave_sha_index",
        lambda repo_root=None: {},
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: None,
    )
    state = _state_with_waves([_wave_payload("P28-I01-W01", commit="a" * 40)])
    assert detect_git_state_drift(state, acked_wave_ids={"P28-I01-W01"}) == []


def test_detect_git_state_drift_none_acks_surfaces_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``acked_wave_ids=None`` behaves identically to the no-ack default."""
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.build_wave_sha_index",
        lambda repo_root=None: {},
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: None,
    )
    state = _state_with_waves([_wave_payload("P28-I01-W01", commit="a" * 40)])
    assert len(detect_git_state_drift(state, acked_wave_ids=None)) == 1
