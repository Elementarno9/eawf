"""D54: the drift detector accepts a wave whose close bookkeeping was folded in.

Wave-close bookkeeping now rides the wave commit: the cherry-picked commit is
amended to carry ``state.json`` plus the typed stores, which rewrites its SHA.
``Wave.commit`` is therefore left unpinned, and
:func:`~eawf.workflow.lifecycle.wave_sha.detect_git_state_drift` must recognise
the amended commit from the subject prefix / ``Eawf-Wave`` trailer scan rather
than report ``closed_no_pin``.

The git graph is stubbed throughout — these cases pin the classification, not
the ``git log`` invocation.
"""

from __future__ import annotations

from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.workflow.lifecycle.wave_sha import Drift, detect_git_state_drift

_AMENDED_SHA = "a" * 40
_RIVAL_SHA = "b" * 40
_WAVE_ID = "P31-I01-W44"


def _base_state_payload() -> dict[str, Any]:
    """Minimal :class:`State` payload acceptable by Pydantic."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ZZ",
        "updated_at": "2026-09-04T00:00:00Z",
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
    wave_id: str = _WAVE_ID,
    *,
    status: str = "closed",
    commit: str | None = None,
) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": wave_id.rsplit("-", 1)[0],
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
        "opened_at": "2026-09-04T00:00:00Z",
        "closed_at": "2026-09-04T00:01:00Z" if status == "closed" else None,
    }


def _state_with_waves(waves: list[dict[str, Any]]) -> State:
    payload = _base_state_payload()
    payload["waves"] = {w["id"]: w for w in waves}
    return State.model_validate(payload)


def _stub_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidates: dict[str, list[str]],
    git_on_path: bool = True,
) -> None:
    """Point the detector at a synthetic first-parent subject index."""
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.shutil.which",
        lambda _: "/usr/bin/git" if git_on_path else None,
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._reachable_wave_keys",
        lambda repo_root=None: {},
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._first_parent_wave_candidates",
        lambda repo_root=None: candidates,
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.commit_identity_digest",
        lambda commit, repo_root=None: None,
    )


def test_detect_drift_accepts_amended_commit_found_by_subject_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-03: an unpinned wave whose amended commit carries the bracket prefix.

    This is the fold's steady state — ``Wave.commit`` is ``None`` because the
    amend rewrote the SHA, and the canonical short prefix still names the wave.
    """
    _stub_git(monkeypatch, candidates={"[P31-W44]": [_AMENDED_SHA]})
    state = _state_with_waves([_wave_payload()])

    assert detect_git_state_drift(state) == []


def test_detect_drift_accepts_amended_commit_found_by_long_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-03: the long ``[P##-I01-W##]`` spelling resolves the same wave."""
    _stub_git(monkeypatch, candidates={"[P31-I01-W44]": [_AMENDED_SHA]})

    assert detect_git_state_drift(_state_with_waves([_wave_payload()])) == []


def test_detect_drift_accepts_amended_commit_found_by_wave_trailer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-03: the default trailer subject style leaves only an ``Eawf-Wave`` key."""
    _stub_git(monkeypatch, candidates={_WAVE_ID: [_AMENDED_SHA]})

    assert detect_git_state_drift(_state_with_waves([_wave_payload()])) == []


def test_detect_drift_accepts_a_non_i01_wave_by_its_only_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-03 boundary: a non-I01 wave has one bracket spelling, not two."""
    wave_id = "P31-I02-W03"
    _stub_git(monkeypatch, candidates={"[P31-I02-W03]": [_AMENDED_SHA]})

    assert detect_git_state_drift(_state_with_waves([_wave_payload(wave_id)])) == []


def test_detect_drift_reports_closed_no_pin_when_no_subject_carries_the_wave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-03 boundary (empty): with nothing in history the drift is real.

    The fold must not blanket-suppress ``closed_no_pin`` — a wave whose commit
    genuinely never landed still has to surface.
    """
    _stub_git(monkeypatch, candidates={})
    drifts = detect_git_state_drift(_state_with_waves([_wave_payload()]))

    assert drifts == [Drift(wave_id=_WAVE_ID, kind="closed_no_pin")]


def test_detect_drift_reports_ambiguous_successor_on_two_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-03 boundary (off-by-one): two carriers is ambiguous, not derivable."""
    _stub_git(
        monkeypatch,
        candidates={"[P31-W44]": [_AMENDED_SHA], _WAVE_ID: [_RIVAL_SHA]},
    )
    drifts = detect_git_state_drift(_state_with_waves([_wave_payload()]))

    assert [d.kind for d in drifts] == ["ambiguous_successor"]


def test_detect_drift_reports_closed_unfindable_without_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-03 error path: no git binary makes the unpinned fold indeterminate."""
    _stub_git(monkeypatch, candidates={"[P31-W44]": [_AMENDED_SHA]}, git_on_path=False)
    drifts = detect_git_state_drift(_state_with_waves([_wave_payload()]))

    assert [d.kind for d in drifts] == ["closed_unfindable"]


def test_detect_drift_ignores_a_wave_still_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-03 boundary: an unclosed wave has no commit expectation to fold."""
    _stub_git(monkeypatch, candidates={})

    assert detect_git_state_drift(_state_with_waves([_wave_payload(status="claimed")])) == []


def test_detect_drift_accepts_every_wave_of_a_folded_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-03: a whole claim batch folded wave-by-wave produces no drift rows."""
    shas = {"P31-I01-W44": "a" * 40, "P31-I01-W45": "c" * 40, "P31-I01-W46": "d" * 40}
    _stub_git(
        monkeypatch,
        candidates={f"[P31-W{wave_id[-2:]}]": [sha] for wave_id, sha in shas.items()},
    )
    state = _state_with_waves([_wave_payload(wave_id) for wave_id in shas])

    assert detect_git_state_drift(state) == []
