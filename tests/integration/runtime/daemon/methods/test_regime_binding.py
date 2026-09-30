"""PLAN-019 and PLAN-006 through the daemon verbs that bind a regime.

``runtime.regime.bind`` is where a Milestone or Batch is bound to a
delivery regime, so it is where the regime table must hold: three values
only, promotion only as an audited successor, and no fast path past a
safety gate. A fast binding is held to its quota and window and mints the
debts that stable release approval reads back from the same store, and
``runtime.regime.discharge_debt`` is the only way that debt closes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.spec.release import ReleaseChannel, ReleaseStatus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.regime import bind, discharge
from eawf.workflow.delivery.regime_bindings import FAST_BINDING_QUOTA, read_regime_bindings
from eawf.workflow.release.lifecycle import ReleaseTransitionError
from eawf.workflow.release.preflight import approve_release
from eawf.workflow.release.verification_debts import read_verification_debts
from eawf.workflow.verify.release_readiness import compute_readiness
from tests._release_helpers import (
    MANIFEST_DIGEST,
    NOW,
    all_passing,
    dev1_config,
    release_record,
)
from tests.integration.runtime.daemon.methods.conftest import run_async

pytestmark = pytest.mark.integration

SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
EVIDENCE: Final = f"{SLOT}/evidence/EVD-0001"
HEAD: Final = "a" * 40


def _milestone(number: int = 30) -> str:
    return f"{SLOT}/milestone/MLS-{number:04d}"


def _fast(scope: str, **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "regime": "fast",
        "scope_ref": scope,
        "incident_ref": "INC-001",
        "policy_revision": 1,
        "expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        "deferred_gates": ["review"],
    }
    fields.update(overrides)
    return fields


def _call(handler: Any, ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def body() -> None:
        captured.update(await handler(ctx, params))

    run_async(body)
    return captured


def _state(ctx: MethodContext) -> Path:
    return Path(str(ctx.state_path))


def _approve_stable(ctx: MethodContext) -> ReleaseStatus:
    """Run the stable approval over the debts the daemon's approve verb reads."""
    candidate = release_record(
        key="REL-0.7.0", version="0.7.0", channel=ReleaseChannel.STABLE, authority_epoch=2
    )
    sweep = compute_readiness(dev1_config(), probes=all_passing(), computed_at=NOW)
    approved = approve_release(
        candidate,
        sweep.model_copy(update={"release_key": candidate.key}),
        dev1_config(),
        approval_ref="receipt://approval/0226",
        approved_at=NOW,
        proof_digest=MANIFEST_DIGEST,
        verification_debts=read_verification_debts(_state(ctx)),
    )
    return approved.status


# ---- PLAN-019: the regime table holds on the binding verb ----------------------


@pytest.mark.parametrize("value", ["promoted", "hotfix", "STEADY"])
def test_plan_019_unknown_or_promoted_regime_is_refused(ctx: MethodContext, value: str) -> None:
    with pytest.raises(DaemonValidationError, match="regime_binding_invalid"):
        _call(bind, ctx, {"regime": value, "scope_ref": _milestone(), "policy_revision": 1})
    assert read_regime_bindings(_state(ctx)) == ()


@pytest.mark.parametrize("gate", ["security", "migration_rollback", "authority"])
def test_plan_019_fast_cannot_defer_an_always_required_gate(ctx: MethodContext, gate: str) -> None:
    with pytest.raises(DaemonValidationError, match="always required"):
        _call(bind, ctx, _fast(_milestone(), deferred_gates=["review", gate]))
    assert read_regime_bindings(_state(ctx)) == ()
    assert read_verification_debts(_state(ctx)) == ()


def test_plan_019_steady_defers_no_gate(ctx: MethodContext) -> None:
    with pytest.raises(DaemonValidationError, match="defers no gate"):
        _call(
            bind,
            ctx,
            {
                "regime": "steady",
                "scope_ref": _milestone(),
                "policy_revision": 1,
                "deferred_gates": ["review"],
            },
        )


def test_plan_019_experimental_promotion_is_an_audited_successor(ctx: MethodContext) -> None:
    scope = _milestone()
    experimental = {
        "regime": "experimental",
        "scope_ref": scope,
        "hypothesis_ref": "H01-01",
        "policy_revision": 1,
    }
    _call(bind, ctx, experimental)
    promotion = {"regime": "steady", "scope_ref": scope, "policy_revision": 2}

    with pytest.raises(DaemonValidationError, match="regime_binding_exists"):
        _call(bind, ctx, promotion)
    unaudited = {"prior_regime": "experimental", "prior_policy_revision": 1}
    with pytest.raises(DaemonValidationError, match="promotion_audit_ref"):
        _call(bind, ctx, {**promotion, "succeeds": unaudited})

    audited = {**unaudited, "promotion_audit_ref": "A001"}
    _call(bind, ctx, {**promotion, "succeeds": audited})

    bindings = read_regime_bindings(_state(ctx))
    assert [b.regime.value for b in bindings] == ["experimental", "steady"]
    assert bindings[0].regime.value == "experimental"


def test_plan_019_fast_work_is_not_rebound_experimental(ctx: MethodContext) -> None:
    scope = _milestone()
    _call(bind, ctx, _fast(scope, deferred_gates=[]))
    with pytest.raises(DaemonValidationError, match="cannot be re-bound"):
        _call(
            bind,
            ctx,
            {
                "regime": "experimental",
                "scope_ref": scope,
                "hypothesis_ref": "H01-01",
                "policy_revision": 2,
                "succeeds": {"prior_regime": "fast", "prior_policy_revision": 1},
            },
        )


# ---- PLAN-006: a fast Incident creates debt, bounded, consumed before stable ---


def test_plan_006_fast_incident_debt_blocks_stable_approval_until_discharged(
    ctx: MethodContext,
) -> None:
    answer = _call(bind, ctx, _fast(_milestone()))

    (debt,) = answer["debts"]
    assert debt["deferred_gate"] == "review"
    assert debt["incident_ref"] == "INC-001"
    assert answer["binding"]["verification_debt_refs"] == [debt["key"]]
    with pytest.raises(ReleaseTransitionError, match="open verification debt"):
        _approve_stable(ctx)

    discharged = _call(discharge, ctx, {"key": debt["key"], "head": HEAD, "evidence_ref": EVIDENCE})

    assert discharged["debt"]["status"] == "DISCHARGED"
    assert _approve_stable(ctx) is ReleaseStatus.APPROVED
    with pytest.raises(DaemonValidationError, match="verification_debt_closed"):
        _call(discharge, ctx, {"key": debt["key"], "head": HEAD, "evidence_ref": EVIDENCE})


def test_plan_006_fast_quota_is_enforced(ctx: MethodContext) -> None:
    for number in range(FAST_BINDING_QUOTA):
        _call(bind, ctx, _fast(_milestone(number + 1)))

    with pytest.raises(DaemonValidationError, match="fast_regime_quota_exhausted"):
        _call(bind, ctx, _fast(_milestone(99)))
    assert len(read_regime_bindings(_state(ctx))) == FAST_BINDING_QUOTA


@pytest.mark.parametrize(
    ("offset", "code"),
    [
        (timedelta(days=30), "fast_regime_window_unbounded"),
        (timedelta(days=-1), "must expire after it takes effect"),
    ],
    ids=["unbounded", "lapsed"],
)
def test_plan_006_fast_expiry_is_enforced(ctx: MethodContext, offset: timedelta, code: str) -> None:
    expires = (datetime.now(UTC) + offset).isoformat()
    with pytest.raises(DaemonValidationError, match=code):
        _call(bind, ctx, _fast(_milestone(), expires_at=expires))
    assert read_verification_debts(_state(ctx)) == ()


def test_plan_006_fast_needs_its_incident(ctx: MethodContext) -> None:
    with pytest.raises(DaemonValidationError, match="incident_ref is required"):
        _call(bind, ctx, _fast(_milestone(), incident_ref=None))
