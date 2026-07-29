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

The scan builds reachable-identity and first-parent indexes, so tests
monkeypatch those indexes to stay isolated from a real git repo.
"""

from __future__ import annotations

import subprocess
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


def _patch_derive(monkeypatch: pytest.MonkeyPatch, table: dict[str, str | None]) -> None:
    """Build deterministic reachable + first-parent indexes from *table*."""
    reachable = {sha: {wave_id} for wave_id, sha in table.items() if sha is not None}
    candidates = {wave_id: [sha] for wave_id, sha in table.items() if sha is not None}
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._reachable_wave_keys",
        lambda repo_root=None: reachable,
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._first_parent_wave_candidates",
        lambda repo_root=None: candidates,
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.commit_identity_digest",
        lambda commit, repo_root=None: f"sha256:{'0' * 64}",
    )


def _patch_legacy_pin_history(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wave_id: str,
    candidate_shas: list[str],
    identity_digests: dict[str, str],
    subjects: dict[str, str],
) -> None:
    """Model an existing pinned object outside reachable integration history."""
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._reachable_wave_keys",
        lambda repo_root=None: {sha: {wave_id} for sha in candidate_shas},
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._first_parent_wave_candidates",
        lambda repo_root=None: {wave_id: candidate_shas},
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.commit_identity_digest",
        lambda commit, repo_root=None: identity_digests.get(commit),
    )

    def run_git(
        args: list[str], *, repo_root: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        subject = subjects[args[-1]]
        return subprocess.CompletedProcess(args, 0, stdout=f"{subject}\n", stderr="")

    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha._run_git", run_git)


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


def test_scan_commit_pins_existing_unreachable_wrong_identity_repairs_unique_title_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy wrong-wave pin repairs only to one uniquely titled commit."""
    _git_on_path(monkeypatch)
    wave_id = "P28-I01-W01"
    pinned = "a" * 40
    selected = "b" * 40
    unrelated = "d" * 40
    old_digest = f"sha256:{'0' * 64}"
    selected_digest = f"sha256:{'1' * 64}"
    _patch_legacy_pin_history(
        monkeypatch,
        wave_id=wave_id,
        candidate_shas=[selected, unrelated],
        identity_digests={
            pinned: old_digest,
            selected: selected_digest,
            unrelated: f"sha256:{'2' * 64}",
        },
        subjects={
            selected: f"[{wave_id}] fix: repair registry index",
            unrelated: f"[{wave_id}] docs: refresh operator guide",
        },
    )
    payload = _wave_payload(wave_id, commit=pinned)
    payload["title"] = "Repair registry index"
    state = _state_with_waves([payload])

    issues = scan_commit_pins(state)

    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "pinned_mismatch"
    assert issue.state_commit == pinned
    assert issue.git_commit == selected
    assert issue.git_identity_digest == selected_digest
    assert issue.repair_basis == "unique_legacy_title_match"
    assert issue.repairable is True

    repaired, skipped = repair_commit_pins(state, issues)

    assert skipped == []
    assert len(repaired) == 1
    assert repaired[0].basis == "unique_legacy_title_match"
    assert state.waves[wave_id].commit == selected
    assert state.waves[wave_id].commit_identity_digest == selected_digest


def test_scan_commit_pins_duplicate_semantic_successors_stay_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two first-parent commits with the stored identity are not a unique repair."""
    _git_on_path(monkeypatch)
    wave_id = "P28-I01-W01"
    pinned = "a" * 40
    first = "b" * 40
    second = "c" * 40
    shared_digest = f"sha256:{'1' * 64}"
    _patch_legacy_pin_history(
        monkeypatch,
        wave_id=wave_id,
        candidate_shas=[first, second],
        identity_digests={
            pinned: shared_digest,
            first: shared_digest,
            second: shared_digest,
        },
        subjects={
            first: f"[{wave_id}] fix: repair registry index",
            second: f"[{wave_id}] fix: repair registry index",
        },
    )
    state = _state_with_waves([_wave_payload(wave_id, commit=pinned)])

    issues = scan_commit_pins(state)

    assert len(issues) == 1
    assert issues[0].kind == "ambiguous_successor"
    assert issues[0].repairable is False
    assert issues[0].git_commit is None


def test_scan_commit_pins_existing_unreachable_wrong_identity_ambiguous_stays_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equal title matches from distinct identities never choose a repair target."""
    _git_on_path(monkeypatch)
    wave_id = "P28-I01-W01"
    pinned = "a" * 40
    first = "b" * 40
    second = "c" * 40
    _patch_legacy_pin_history(
        monkeypatch,
        wave_id=wave_id,
        candidate_shas=[first, second],
        identity_digests={
            pinned: f"sha256:{'0' * 64}",
            first: f"sha256:{'1' * 64}",
            second: f"sha256:{'2' * 64}",
        },
        subjects={
            first: f"[{wave_id}] fix: repair registry index",
            second: f"[{wave_id}] fix: repair registry index",
        },
    )
    payload = _wave_payload(wave_id, commit=pinned)
    payload["title"] = "Repair registry index"
    state = _state_with_waves([payload])

    issues = scan_commit_pins(state)

    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "ambiguous_successor"
    assert issue.state_commit == pinned
    assert issue.git_commit is None
    assert issue.git_identity_digest is None
    assert issue.repair_basis is None
    assert issue.repairable is False

    repaired, skipped = repair_commit_pins(state, issues)

    assert repaired == []
    assert skipped == issues
    assert state.waves[wave_id].commit == pinned
    assert state.waves[wave_id].commit_identity_digest is None


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
