"""Unit tests for the P29-I02-W06 commit-pin verify/repair scan.

Covers the library layer behind ``eawf wave verify-commits``:

- :func:`eawf.workflow.lifecycle.wave_sha.scan_commit_pins` -- the
  repair-oriented superset of
  :func:`~eawf.workflow.lifecycle.wave_sha.detect_git_state_drift` that
  surfaces the four hard-drift kinds plus the soft ``unpinned_derivable``
  kind (closed, no pin, but git can still derive a SHA).
- :func:`eawf.workflow.lifecycle.wave_sha.repair_commit_pins` -- the pure
  in-process mutator that re-pins the two repairable kinds and reports
  the rest as skipped.

The scan reuses :func:`~eawf.workflow.lifecycle.wave_sha.derive_wave_sha`,
so the tests monkeypatch it (and ``shutil.which``) to keep the fixtures
isolated from a real git repo -- mirroring
``tests/unit/test_drift_reconciler.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.workflow.lifecycle.wave_sha import (
    CommitPinIssue,
    RepairAction,
    repair_commit_pins,
    save_drift_acks,
    scan_commit_pins,
)


def _base_state_payload() -> dict[str, Any]:
    """Minimal :class:`State` payload acceptable by Pydantic."""
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


def _wave_payload(
    wave_id: str = "P28-I01-W01",
    *,
    status: str = "closed",
    commit: str | None = None,
) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": f"{wave_id.rsplit('-', 1)[0]}",
        "title": f"wave {wave_id}",
        "status": status,
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
        "closed_at": "2026-05-27T00:01:00Z" if status == "closed" else None,
    }


def _state_with_waves(waves: list[dict[str, Any]]) -> State:
    payload = _base_state_payload()
    payload["waves"] = {w["id"]: w for w in waves}
    return State.model_validate(payload)


def _git_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    # ``scan_commit_pins`` builds a shared SHA index once (W10). These tests
    # patch ``derive_wave_sha`` directly, so stub the builder to an empty map
    # so no live ``git log`` runs and the patched derive answers.
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.build_wave_sha_index",
        lambda repo_root=None: {},
    )


def _patch_derive(monkeypatch: pytest.MonkeyPatch, table: dict[str, str | None]) -> None:
    """Make ``derive_wave_sha`` return ``table[wid]`` (default ``None``)."""
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: table.get(wid),
    )


# ---- scan_commit_pins: clean / boundary ------------------------------------


def test_scan_commit_pins_empty_state_is_clean() -> None:
    """No waves at all -> no issues (boundary: empty)."""
    assert scan_commit_pins(_state_with_waves([])) == []


def test_scan_commit_pins_open_waves_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only CLOSED waves are scanned; a pending wave never drifts."""
    _git_on_path(monkeypatch)
    _patch_derive(monkeypatch, {})
    state = _state_with_waves([_wave_payload(status="pending")])
    assert scan_commit_pins(state) == []


def test_scan_commit_pins_clean_when_pinned_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned SHA that equals the derived SHA is in sync -> no issue."""
    _git_on_path(monkeypatch)
    sha40 = "a" * 40
    _patch_derive(monkeypatch, {"P28-I01-W01": sha40})
    state = _state_with_waves([_wave_payload(commit=sha40)])
    assert scan_commit_pins(state) == []


def test_scan_commit_pins_clean_on_prefix_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prefix-tolerant match (state short prefix vs full derived) is clean."""
    _git_on_path(monkeypatch)
    full = "abc1234" + "0" * 33
    _patch_derive(monkeypatch, {"P28-I01-W01": full})
    state = _state_with_waves([_wave_payload(commit=full)])
    assert scan_commit_pins(state) == []


# ---- scan_commit_pins: each kind -------------------------------------------


def test_scan_commit_pins_pinned_mismatch_is_repairable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git_on_path(monkeypatch)
    _patch_derive(monkeypatch, {"P28-I01-W01": "b" * 40})
    state = _state_with_waves([_wave_payload(commit="a" * 40)])
    issues = scan_commit_pins(state)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "pinned_mismatch"
    assert issue.state_commit == "a" * 40
    assert issue.git_commit == "b" * 40
    assert issue.repairable is True


def test_scan_commit_pins_unpinned_derivable_is_repairable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closed wave, no pin, but git derives a SHA -> the soft harden-able kind."""
    _git_on_path(monkeypatch)
    _patch_derive(monkeypatch, {"P28-I01-W01": "c" * 40})
    state = _state_with_waves([_wave_payload(commit=None)])
    issues = scan_commit_pins(state)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "unpinned_derivable"
    assert issue.state_commit is None
    assert issue.git_commit == "c" * 40
    assert issue.repairable is True


def test_scan_commit_pins_pinned_but_missing_is_unrepairable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git_on_path(monkeypatch)
    _patch_derive(monkeypatch, {})  # derive returns None
    state = _state_with_waves([_wave_payload(commit="a" * 40)])
    issues = scan_commit_pins(state)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "pinned_but_missing"
    assert issue.state_commit == "a" * 40
    assert issue.git_commit is None
    assert issue.repairable is False


def test_scan_commit_pins_loads_repo_ack_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acknowledged hard drift is omitted from the verify-commits issue scan."""
    _git_on_path(monkeypatch)
    _patch_derive(monkeypatch, {})  # derive returns None
    state = _state_with_waves(
        [
            _wave_payload("P28-I01-W01", commit="a" * 40),
            _wave_payload("P28-I01-W02", commit="b" * 40),
        ]
    )
    save_drift_acks({"P28-I01-W01"}, tmp_path)

    issues = scan_commit_pins(state, repo_root=tmp_path)

    assert [issue.wave_id for issue in issues] == ["P28-I01-W02"]


def test_scan_commit_pins_explicit_ack_set_overrides_repo_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit empty set lets callers scan all rows despite a repo ack file."""
    _git_on_path(monkeypatch)
    _patch_derive(monkeypatch, {})  # derive returns None
    state = _state_with_waves([_wave_payload("P28-I01-W01", commit="a" * 40)])
    save_drift_acks({"P28-I01-W01"}, tmp_path)

    issues = scan_commit_pins(state, repo_root=tmp_path, acked_wave_ids=set())

    assert [issue.wave_id for issue in issues] == ["P28-I01-W01"]


def test_scan_commit_pins_closed_no_pin_is_unrepairable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git_on_path(monkeypatch)
    _patch_derive(monkeypatch, {})  # no pin AND nothing derivable
    state = _state_with_waves([_wave_payload(commit=None)])
    issues = scan_commit_pins(state)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "closed_no_pin"
    assert issue.git_commit is None
    assert issue.repairable is False


def test_scan_commit_pins_closed_unfindable_when_git_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """git not on PATH -> a closed-no-pin wave is indeterminate, not repairable."""
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: None)
    state = _state_with_waves([_wave_payload(commit=None)])
    issues = scan_commit_pins(state)
    assert len(issues) == 1
    assert issues[0].kind == "closed_unfindable"
    assert issues[0].repairable is False


def test_scan_commit_pins_orders_rows_by_wave_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue rows are sorted by wave id for stable render output."""
    _git_on_path(monkeypatch)
    _patch_derive(monkeypatch, {"P28-I01-W02": "b" * 40, "P28-I01-W01": "a" * 40})
    state = _state_with_waves(
        [
            _wave_payload("P28-I01-W02", commit="f" * 40),
            _wave_payload("P28-I01-W01", commit="e" * 40),
        ]
    )
    issues = scan_commit_pins(state)
    assert [i.wave_id for i in issues] == ["P28-I01-W01", "P28-I01-W02"]


# ---- repair_commit_pins ----------------------------------------------------


def test_repair_commit_pins_repins_mismatch_and_unpinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both repairable kinds get re-pinned to the git-derived SHA in place."""
    _git_on_path(monkeypatch)
    _patch_derive(
        monkeypatch,
        {"P28-I01-W01": "b" * 40, "P28-I01-W02": "c" * 40},
    )
    state = _state_with_waves(
        [
            _wave_payload("P28-I01-W01", commit="a" * 40),  # pinned_mismatch
            _wave_payload("P28-I01-W02", commit=None),  # unpinned_derivable
        ]
    )
    issues = scan_commit_pins(state)
    repaired, skipped = repair_commit_pins(state, issues)
    assert skipped == []
    assert {a.wave_id for a in repaired} == {"P28-I01-W01", "P28-I01-W02"}
    # State mutated in place to the derived SHAs.
    assert state.waves["P28-I01-W01"].commit == "b" * 40
    assert state.waves["P28-I01-W02"].commit == "c" * 40
    # RepairAction carries the before/after.
    by_id = {a.wave_id: a for a in repaired}
    assert by_id["P28-I01-W01"].old_commit == "a" * 40
    assert by_id["P28-I01-W01"].new_commit == "b" * 40
    assert by_id["P28-I01-W02"].old_commit is None
    assert by_id["P28-I01-W02"].new_commit == "c" * 40


def test_repair_commit_pins_skips_unrepairable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kinds with no derivable SHA are returned as skipped, state untouched."""
    _git_on_path(monkeypatch)
    _patch_derive(monkeypatch, {})  # nothing derivable
    state = _state_with_waves(
        [
            _wave_payload("P28-I01-W01", commit="a" * 40),  # pinned_but_missing
            _wave_payload("P28-I01-W02", commit=None),  # closed_no_pin
        ]
    )
    issues = scan_commit_pins(state)
    repaired, skipped = repair_commit_pins(state, issues)
    assert repaired == []
    assert {i.wave_id for i in skipped} == {"P28-I01-W01", "P28-I01-W02"}
    # pinned_but_missing keeps its (unreachable) pin; closed_no_pin stays None.
    assert state.waves["P28-I01-W01"].commit == "a" * 40
    assert state.waves["P28-I01-W02"].commit is None


def test_repair_commit_pins_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second scan+repair after the first finds nothing to do."""
    _git_on_path(monkeypatch)
    _patch_derive(monkeypatch, {"P28-I01-W01": "b" * 40})
    state = _state_with_waves([_wave_payload("P28-I01-W01", commit="a" * 40)])

    first = repair_commit_pins(state, scan_commit_pins(state))
    assert len(first[0]) == 1  # one repaired on the first pass

    # Re-scan: the pin now equals the derived SHA -> clean.
    second_issues = scan_commit_pins(state)
    assert second_issues == []
    repaired, skipped = repair_commit_pins(state, second_issues)
    assert repaired == []
    assert skipped == []


def test_repair_commit_pins_partial_mix(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mixed batch repairs the repairable rows and skips the rest."""
    _git_on_path(monkeypatch)
    _patch_derive(
        monkeypatch,
        {
            "P28-I01-W01": "b" * 40,  # mismatch -> repair
            "P28-I01-W03": "d" * 40,  # unpinned_derivable -> repair
            # W02 derives nothing -> pinned_but_missing -> skip
        },
    )
    state = _state_with_waves(
        [
            _wave_payload("P28-I01-W01", commit="a" * 40),
            _wave_payload("P28-I01-W02", commit="9" * 40),
            _wave_payload("P28-I01-W03", commit=None),
        ]
    )
    issues = scan_commit_pins(state)
    repaired, skipped = repair_commit_pins(state, issues)
    assert {a.wave_id for a in repaired} == {"P28-I01-W01", "P28-I01-W03"}
    assert {i.wave_id for i in skipped} == {"P28-I01-W02"}


def test_repair_action_and_issue_are_frozen() -> None:
    """The two dataclasses are immutable value objects."""
    from dataclasses import FrozenInstanceError

    action = RepairAction(
        wave_id="P28-I01-W01", kind="pinned_mismatch", old_commit=None, new_commit="b" * 40
    )
    issue = CommitPinIssue(wave_id="P28-I01-W01", kind="closed_no_pin")
    with pytest.raises(FrozenInstanceError):
        action.wave_id = "P28-I01-W02"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        issue.wave_id = "P28-I01-W02"  # type: ignore[misc]
