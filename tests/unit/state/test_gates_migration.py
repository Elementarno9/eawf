"""Tests for the ``1.7 -> 1.8`` Wave.gates backfill migration.

The v1.8 edge adds the typed :attr:`eawf.kernel.state.models.Wave.gates` list
(the per-wave :class:`~eawf.kernel.spec.common.GateSpec` close-gate rows) and
backfills an explicit ``gates: []`` on every wave that lacks the key. These
tests pin the per-wave backfill, the idempotency (an existing ``gates`` list is
left untouched), the version bump on a deep copy, the pre/post invariant
rejections, and a full v1.7 state re-loading under the live model after
``apply``.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.migrations.v1_7_to_v1_8 import MigrationV17ToV18
from eawf.kernel.state.models import State

_TS = "2026-06-07T00:00:00Z"

#: A typed grandfathered success-criterion row -- the live (>=1.7) model
#: requires ``success_criteria`` to be a ``list[CriterionSpec]``, so the
#: fixture carries an already-typed criterion rather than a bare string.
_GRANDFATHERED_CRITERION: dict[str, Any] = {
    "id": "CR-01",
    "text": "the migrated wave validates against the typed model",
    "kind": "legacy",
    "acceptance_style": "binary",
    "evidence_kind": "attested",
    "quality_dimension": "functional_suitability",
    "measurable_signal": "the migrated wave validates against the typed model",
}


def _state_v1_7(*, include_gates: bool = False) -> dict[str, Any]:
    """Return a referentially complete v1.7 state with one wave.

    Args:
        include_gates: When ``True`` the wave already carries a ``gates`` list
            (one row) so the idempotency path -- leaving an existing list
            untouched -- can be exercised.

    Returns:
        A raw v1.7 state dict the live ``State`` model re-loads after the bump.
    """
    wave: dict[str, Any] = {
        "id": "P00-I01-W01",
        "iter_id": "P00-I01",
        "title": "Wave one",
        "status": "pending",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "success_criteria": [_GRANDFATHERED_CRITERION],
        "opened_at": _TS,
        "closed_at": None,
    }
    if include_gates:
        wave["gates"] = [
            {
                "id": "G-01",
                "criterion_id": "CR-01",
                "kind": "command_exit_zero",
                "args": {"argv": ["uv", "run", "pytest", "-q"]},
                "policy": "block",
                "cadence": "every-wave",
                "required": True,
            }
        ]
    return {
        "schema_version": "1.7",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": _TS,
        "project": {
            "code": "QR",
            "slug": "quant-research",
            "title": "Quant Research",
            "description": "",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "subproject_id": None,
            "phase_id": "P00",
            "iter_id": "P00-I01",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "dispatch_paused": False,
        "phases": {
            "P00": {
                "id": "P00",
                "scope_id": "QR",
                "subproject_id": None,
                "title": "Phase zero",
                "status": "active",
                "iter_ids": ["P00-I01"],
                "outcome_ids": [],
                "depends_on": [],
                "source_brief_ids": [],
                "opened_at": _TS,
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P00-I01": {
                "id": "P00-I01",
                "phase_id": "P00",
                "title": "Iter one",
                "status": "active",
                "wave_ids": ["P00-I01-W01"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _TS,
                "closed_at": None,
            }
        },
        "waves": {"P00-I01-W01": wave},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def test_apply_backfills_empty_gates_on_every_wave() -> None:
    """A wave without ``gates`` gets an explicit ``gates: []`` after the bump."""
    out = MigrationV17ToV18().apply(_state_v1_7())
    assert out["waves"]["P00-I01-W01"]["gates"] == []


def test_apply_bumps_schema_version_and_does_not_mutate_input() -> None:
    """``apply`` bumps the version on a deep copy without touching the input."""
    src = _state_v1_7()
    out = MigrationV17ToV18().apply(src)

    assert out["schema_version"] == "1.8"
    # The input is unchanged (deep-copied): still 1.7 with no gates key.
    assert src["schema_version"] == "1.7"
    assert "gates" not in src["waves"]["P00-I01-W01"]


def test_apply_is_idempotent_on_existing_gates() -> None:
    """Re-run safety: an existing ``gates`` list is passed through untouched."""
    src = _state_v1_7(include_gates=True)
    out = MigrationV17ToV18().apply(src)
    gates = out["waves"]["P00-I01-W01"]["gates"]
    assert len(gates) == 1
    assert gates[0]["id"] == "G-01"


def test_apply_replays_cleanly_on_already_migrated_output() -> None:
    """Boundary: applying twice yields the same backfilled empty list."""
    once = MigrationV17ToV18().apply(_state_v1_7())
    twice = MigrationV17ToV18().apply(once)
    assert twice["waves"]["P00-I01-W01"]["gates"] == []
    assert twice["schema_version"] == "1.8"


def test_apply_handles_state_with_no_waves() -> None:
    """Boundary: a state whose ``waves`` map is empty bumps with no backfill."""
    src = _state_v1_7()
    src["waves"] = {}
    out = MigrationV17ToV18().apply(src)
    assert out["schema_version"] == "1.8"
    assert out["waves"] == {}


def test_apply_then_model_validate_reloads_without_validation_error() -> None:
    """A v1.7 fixture re-loads under the live model after apply.

    The migrated state carries the new ``gates`` field, which the live
    ``Wave`` model now requires the schema_version to permit; the round-trip
    validates cleanly at 1.8.
    """
    out = MigrationV17ToV18().apply(_state_v1_7())
    state = State.model_validate(out)
    assert state.schema_version == "1.8"
    assert state.waves["P00-I01-W01"].gates == []


def test_check_pre_rejects_non_1_7_dict() -> None:
    """``check_pre`` rejects a payload that is not at schema 1.7."""
    step = MigrationV17ToV18()
    with pytest.raises(ValidationError):
        step.check_pre({"schema_version": "1.6"})


def test_check_post_rejects_non_1_8_dict() -> None:
    """``check_post`` rejects a payload that is not at schema 1.8."""
    step = MigrationV17ToV18()
    with pytest.raises(ValidationError):
        step.check_post({"schema_version": "1.7"})
