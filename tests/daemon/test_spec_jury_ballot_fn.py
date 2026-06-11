"""Daemon spec-jury live-ballot-fn binding tests (P30-I09-W05, TRUST-5).

The daemon close path binds the LIVE per-item ballot fn for a banded wave via
:func:`eawf.runtime.daemon.methods.state._spec_jury_ballot_fn`, which reuses the
cross-vendor jury's per-runtime spawn factory + the wave's on-disk rubric to
drive each disjoint juror runtime through the bounded re-ask loop. These tests
pin the daemon half of success criterion C1: the binder returns a CALLABLE (not
``None``) for a banded wave when enough vendor lanes resolve, and returns
``None`` (keeping the producer idle) when too few lanes resolve so the close
degrades to the single-auditor / cross-vendor gate.

The lane pre-check (:func:`_cross_vendor_lanes_ready`) is monkeypatched so a host
without the vendor CLIs does not perturb the binding decision -- no real
``claude`` / ``codex`` / ``opencode`` subprocess runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.state.models import State, Wave
from eawf.runtime.daemon.methods.state import _spec_jury_ballot_fn


def _empty_state() -> State:
    """Build a minimal valid :class:`State` with an empty wave tree."""
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": "2026-06-11T00:00:00Z",
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "Abc",
            "domains": ["infra"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def _band_wave() -> Wave:
    """Build a UI-banded claimed wave (a TUI file scope bands it structurally)."""
    return Wave.model_validate(
        {
            "id": "P30-I09-W05",
            "iter_id": "P30-I09",
            "title": "bind the live spec-jury ballot fn",
            "status": "claimed",
            "file_scopes": ["src/eawf/surfaces/tui/widgets/footer.py"],
            "success_criteria": [
                {
                    "id": "CR-01",
                    "text": "the live ballot fn drives each juror runtime",
                    "kind": "legacy",
                    "acceptance_style": "binary",
                    "evidence_kind": "attested",
                    "quality_dimension": "functional_suitability",
                    "measurable_signal": "the live ballot fn drives each juror runtime",
                }
            ],
            "agent_role": "executor",
            "effort_bucket": "L",
            "opened_at": "2026-06-11T00:00:00Z",
            "claimed_at": "2026-06-11T00:00:00Z",
        }
    )


def test_ballot_fn_returns_callable_for_banded_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: a banded wave with enough lanes binds a LIVE callable (not None).

    With the lane pre-check forced True (enough disjoint vendor CLIs resolve),
    the binder reuses the spawn factory + the (here absent, so empty) rubric to
    return the live per-item ballot fn -- a callable the producer can drive,
    un-idling the second jury path.
    """
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._cross_vendor_lanes_ready",
        lambda *, quorum: True,
    )
    fn = _spec_jury_ballot_fn(_empty_state(), _band_wave(), repo_root=tmp_path)
    assert fn is not None
    assert callable(fn)


def test_ballot_fn_idle_when_too_few_lanes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sub-quorum host keeps the producer idle: the binder returns None.

    A box that cannot cast independent cross-vendor ballots (too few vendor
    CLIs) keeps the spec-jury producer idle and degrades to the single-auditor /
    cross-vendor gate rather than spawning a degenerate jury.
    """
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._cross_vendor_lanes_ready",
        lambda *, quorum: False,
    )
    fn = _spec_jury_ballot_fn(_empty_state(), _band_wave(), repo_root=tmp_path)
    assert fn is None
