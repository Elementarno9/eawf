"""Daemon close-gate jury-authority resolution tests (P30-I09-W04, TRUST-4).

The daemon close path computes the cross-vendor jury's EARNED block authority
once per close via
:func:`eawf.runtime.daemon.methods.state._resolve_jury_block_authority` and
threads it into every per-criterion ``run_oracle`` call. These tests pin the
daemon half of success criterion C2: the resolver returns ADVISORY on the empty
validation substrate (today's honest state -- no labelled cohort, no recorded
ballots), so the enforcing close never blocks on an uncalibrated jury, and it
returns ADVISORY when no verify block is resolved at all.

The substrate is empty by construction (zero AUDITOR verdict rows on disk), so a
honest-empty cohort short-circuits to advisory before any scoring -- the resolver
never fabricates a calibrated jury.
"""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.state.models import State
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.platform.profiles.models import JuryAuthorityConfig, VerifyBlock
from eawf.runtime.daemon.methods.state import _resolve_jury_block_authority


def _empty_state() -> State:
    """Build a minimal valid :class:`State` with an empty wave / verdict tree."""
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
        "current": {
            "project_code": "ABC",
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
    return State.model_validate(payload)


def test_resolve_authority_advisory_on_empty_substrate(tmp_path: Path) -> None:
    """C2: an empty validation substrate resolves to ADVISORY (default-safe).

    With no labelled cohort and no recorded ballots on disk, the cohort is
    honest-empty and the jury has earned no authority -- the resolver returns
    advisory so the enforcing close never blocks on an uncalibrated jury, even
    when the profile opts into the (default) jury-authority floors.
    """
    state = _empty_state()
    verify_block = VerifyBlock(enforce=True, cross_vendor_jury=True)

    authority = _resolve_jury_block_authority(
        state, state_path=tmp_path / "state.json", verify_block=verify_block
    )

    assert authority is BlockAuthority.ADVISORY


def test_resolve_authority_advisory_when_no_verify_block(tmp_path: Path) -> None:
    """C2: a None verify block resolves to ADVISORY without touching the substrate.

    A wave whose resolved verify block is ``None`` has no jury-authority floors
    to score against, so the resolver short-circuits to advisory.
    """
    state = _empty_state()

    authority = _resolve_jury_block_authority(
        state, state_path=tmp_path / "state.json", verify_block=None
    )

    assert authority is BlockAuthority.ADVISORY


def test_resolve_authority_advisory_with_custom_floors(tmp_path: Path) -> None:
    """C2: a relaxed jury-authority leaf still resolves advisory on empty data.

    Even a profile that relaxes every floor cannot earn the jury blocking
    authority while the validation substrate is empty -- the floors only matter
    once a real cohort accrues, so the empty-substrate short-circuit dominates.
    """
    state = _empty_state()
    verify_block = VerifyBlock(
        enforce=True,
        cross_vendor_jury=True,
        jury_authority=JuryAuthorityConfig(
            min_labeled_waves=1,
            known_bad_catch_lb_floor=0.0,
            unanimous_pass_ceiling=1.0,
        ),
    )

    authority = _resolve_jury_block_authority(
        state, state_path=tmp_path / "state.json", verify_block=verify_block
    )

    assert authority is BlockAuthority.ADVISORY
