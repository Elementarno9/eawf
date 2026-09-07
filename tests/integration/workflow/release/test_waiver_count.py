"""What a counted waiver does to a checkpoint approval, end to end.

The unit tier pins the classification itself. This module pins the
consequence: a waiver is a *durable row*, not a number, and the row is
what the approval path reads.

Four dispositions are asserted, each through the approval guard rather
than through the classifier, because the claim is about what a waiver
blocks and not about how it is labelled:

1. **zero** -- nothing is counted, the sweep is ready, the candidate
   approves.
2. **explained-unacknowledged** -- every counted waiver names its
   scope, reason and protected principal, so nothing is unexplained --
   and approval is still refused, because nobody has yet accepted the
   loss of the protection the waiver suspends.
3. **acknowledged** -- the receipt carries an acceptance per counted
   waiver, the block lifts, and the candidate approves with the
   acceptances recorded beside the waivers they cover.
4. **unexplained** -- a counted waiver missing one of the three fields
   (or counted with no row at all) is red, and an acknowledgement does
   not clear it: there is nothing to acknowledge.

The wire leg matters as much as the library leg. ``release.approve``
refuses on the same denial code the library raises, so an operator
cannot walk past an unacknowledged waiver by going through the daemon
instead of the function.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from eawf.kernel.release.signals import ReleaseSignalName, ReleaseSignalStatus
from eawf.kernel.release.waiver import ReleaseWaiver, WaiverDisposition
from eawf.kernel.spec.release import (
    Release,
    ReleaseChannel,
    ReleaseStatus,
)
from eawf.kernel.spec.release_config import load_release_config
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import approve as approve_rpc
from eawf.workflow.release.lifecycle import ReleaseDenialCode, ReleaseTransitionError
from eawf.workflow.release.preflight import approve_release
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.verify.release_readiness import (
    ReleaseReadiness,
    WaiverAcknowledgement,
    compute_readiness,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

DEV1_VERSION = "0.7.0.dev1"
SOURCE_SHA = "a" * 40
TREE_SHA = "b" * 40
MANIFEST_DIGEST = f"sha256:{'c' * 64}"
APPROVAL_REF = "receipt://approval/rel-0.7.0.dev1"

#: A waiver carrying all three fields, so it is explained and therefore
#: acknowledgeable. Modelled on the shape a real close-gate waiver has:
#: the scope it was granted on, why the evidence could not be produced,
#: and the protection whose loss the operator is being asked to accept.
EXPLAINED = ReleaseWaiver(
    scope="P31-I01-W36",
    reason="the executor ran inline with no captured runtime",
    protected_principal="every wave close records a runtime gate receipt",
)

#: A second explained waiver on a different scope, so a test can
#: acknowledge one of two and watch the other keep blocking.
EXPLAINED_OTHER = ReleaseWaiver(
    scope="P31-I01-W38",
    reason="the symbol-tool ladder had no live backend on this host",
    protected_principal="every verify ladder rung runs against a live backend",
)

ACK = WaiverAcknowledgement(
    scope=EXPLAINED.scope,
    protected_principal=EXPLAINED.protected_principal,
    acknowledged_by="release-operator",
)

ACK_OTHER = WaiverAcknowledgement(
    scope=EXPLAINED_OTHER.scope,
    protected_principal=EXPLAINED_OTHER.protected_principal,
    acknowledged_by="release-operator",
)

CTX = MethodContext(
    started_at="2026-09-07T00:00:00+00:00",
    pid=4321,
    protocol_version="1",
    version=DEV1_VERSION,
)


def _config() -> Any:
    """Return the authored dev1 checkpoint configuration."""
    return load_release_config(checkpoint_config_yaml(DEV1_VERSION), train=V07_TRAIN)


def _green_probes() -> dict[ReleaseSignalName, Any]:
    """Return a probe registry in which every signal passes.

    The waiver block is the subject here, so every row is green: a red
    row would make the refusal ambiguous between "a signal failed" and
    "a waiver is outstanding", which is exactly the distinction these
    tests are drawing.
    """

    def passing(_context: Any) -> Any:
        from eawf.kernel.release.signals import ReleaseSignalOutcome

        return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS)

    return dict.fromkeys(ReleaseSignalName, passing)


def _sweep(**kwargs: Any) -> ReleaseReadiness:
    """Return an otherwise-green dev1 sweep with *kwargs* forwarded."""
    return compute_readiness(_config(), probes=_green_probes(), computed_at=NOW, **kwargs)


def _candidate() -> Release:
    """Return the pinned dev1 candidate the approval is attempted on."""
    return Release(
        uid=UUID(int=26),
        key=f"REL-{DEV1_VERSION}",
        version=DEV1_VERSION,
        channel=ReleaseChannel.DEV,
        authority_epoch=1,
        status=ReleaseStatus.CANDIDATE,
        source_sha=SOURCE_SHA,
        source_tree_sha=TREE_SHA,
        manifest_ref="artifact://release/manifest/0.7.0.dev1",
        manifest_digest=MANIFEST_DIGEST,
    )


def _approve(readiness: ReleaseReadiness) -> Release:
    """Approve the pinned candidate against *readiness*."""
    return approve_release(_candidate(), readiness, approval_ref=APPROVAL_REF, approved_at=NOW)


def _approve_over_rpc(readiness: ReleaseReadiness) -> dict[str, Any]:
    """Approve through ``release.approve``, the daemon-facing surface."""
    return asyncio.run(
        approve_rpc(
            CTX,
            {
                "release": _candidate().model_dump(mode="json"),
                "readiness": readiness.model_dump(mode="json"),
                "approval_ref": APPROVAL_REF,
            },
        )
    )


# --- 1. zero ---------------------------------------------------------


def test_a_zero_waiver_count_is_reported_and_does_not_block() -> None:
    sweep = _sweep()
    assert sweep.waiver_count == 0
    assert sweep.waivers == ()
    assert sweep.waiver_disposition is WaiverDisposition.NONE
    assert sweep.unacknowledged_waivers == ()
    assert sweep.waivers_cleared is True
    assert sweep.ready is True
    assert _approve(sweep).status is ReleaseStatus.APPROVED


# --- 2. explained, unacknowledged ------------------------------------


def test_an_explained_waiver_is_the_durable_row_not_a_bare_count() -> None:
    sweep = _sweep(waivers=(EXPLAINED,))
    assert sweep.waiver_count == 1
    row = sweep.waivers[0]
    assert (row.scope, row.reason, row.protected_principal) == (
        EXPLAINED.scope,
        EXPLAINED.reason,
        EXPLAINED.protected_principal,
    )
    assert row.explained is True
    assert row.missing_fields == ()


def test_an_explained_waiver_blocks_approval_until_it_is_acknowledged() -> None:
    sweep = _sweep(waivers=(EXPLAINED,))
    assert sweep.waiver_disposition is WaiverDisposition.AWAITING_ACKNOWLEDGEMENT
    assert sweep.unacknowledged_waivers == (EXPLAINED,)
    assert sweep.waivers_cleared is False
    assert sweep.ready is False
    with pytest.raises(ReleaseTransitionError) as excinfo:
        _approve(sweep)
    assert excinfo.value.code is ReleaseDenialCode.RELEASE_NOT_READY
    assert "every gate is green" in str(excinfo.value)
    assert "awaiting_acknowledgement" in str(excinfo.value)


def test_the_wire_refuses_an_unacknowledged_waiver_with_the_same_denial() -> None:
    with pytest.raises(DaemonValidationError) as excinfo:
        _approve_over_rpc(_sweep(waivers=(EXPLAINED,)))
    assert ReleaseDenialCode.RELEASE_NOT_READY.value in str(excinfo.value)


def test_acknowledging_one_of_two_explained_waivers_still_blocks() -> None:
    sweep = _sweep(waivers=(EXPLAINED, EXPLAINED_OTHER), acknowledgements=(ACK,))
    assert sweep.waiver_count == 2
    assert sweep.unacknowledged_waivers == (EXPLAINED_OTHER,)
    assert sweep.ready is False


# --- 3. acknowledged -------------------------------------------------


def test_an_acknowledged_waiver_clears_the_block_and_approves() -> None:
    sweep = _sweep(waivers=(EXPLAINED,), acknowledgements=(ACK,))
    assert sweep.waiver_count == 1
    assert sweep.waiver_disposition is WaiverDisposition.AWAITING_ACKNOWLEDGEMENT
    assert sweep.unacknowledged_waivers == ()
    assert sweep.waivers_cleared is True
    assert sweep.ready is True
    approved = _approve(sweep)
    assert approved.status is ReleaseStatus.APPROVED
    assert approved.approval_ref == APPROVAL_REF


def test_the_acknowledgement_is_carried_in_the_readiness_receipt() -> None:
    payload = _sweep(waivers=(EXPLAINED,), acknowledgements=(ACK,)).model_dump(mode="json")
    assert payload["waiver_count"] == 1
    assert payload["waiver_disposition"] == "awaiting_acknowledgement"
    assert payload["waivers"][0]["protected_principal"] == EXPLAINED.protected_principal
    assert payload["waiver_acknowledgements"] == [
        {
            "scope": EXPLAINED.scope,
            "protected_principal": EXPLAINED.protected_principal,
            "acknowledged_by": "release-operator",
        }
    ]
    assert payload["ready"] is True
    assert ReleaseReadiness.model_validate(payload).ready is True


def test_the_wire_approves_once_every_waiver_is_acknowledged() -> None:
    result = _approve_over_rpc(
        _sweep(waivers=(EXPLAINED, EXPLAINED_OTHER), acknowledgements=(ACK, ACK_OTHER))
    )
    assert result["release"]["status"] == ReleaseStatus.APPROVED.value


# --- 4. unexplained --------------------------------------------------


@pytest.mark.parametrize("blank", ["scope", "reason", "protected_principal"])
def test_a_waiver_missing_any_one_of_the_three_fields_is_red(blank: str) -> None:
    waiver = EXPLAINED.model_copy(update={blank: ""})
    sweep = _sweep(waivers=(waiver,))
    assert waiver.missing_fields == (blank,)
    assert sweep.waiver_disposition is WaiverDisposition.UNEXPLAINED
    assert sweep.waivers_cleared is False
    assert sweep.ready is False


def test_a_count_with_no_waiver_row_at_all_is_red() -> None:
    sweep = _sweep(waiver_count=1)
    assert sweep.waiver_count == 1
    assert sweep.waivers == ()
    assert sweep.waiver_disposition is WaiverDisposition.UNEXPLAINED
    assert sweep.ready is False


def test_no_acknowledgement_clears_an_unexplained_waiver() -> None:
    unexplained = EXPLAINED.model_copy(update={"reason": ""})
    sweep = _sweep(
        waivers=(unexplained,),
        acknowledgements=(
            WaiverAcknowledgement(
                scope=unexplained.scope,
                protected_principal=unexplained.protected_principal,
                acknowledged_by="release-operator",
            ),
        ),
    )
    assert sweep.waiver_disposition is WaiverDisposition.UNEXPLAINED
    assert sweep.unacknowledged_waivers == ()
    assert sweep.waivers_cleared is False
    assert sweep.ready is False
    with pytest.raises(ReleaseTransitionError) as excinfo:
        _approve(sweep)
    assert "unexplained" in str(excinfo.value)


def test_one_unexplained_waiver_reds_an_otherwise_acknowledged_set() -> None:
    sweep = _sweep(
        waivers=(EXPLAINED, EXPLAINED_OTHER.model_copy(update={"scope": ""})),
        acknowledgements=(ACK,),
    )
    assert sweep.waiver_disposition is WaiverDisposition.UNEXPLAINED
    assert sweep.ready is False


# --- boundaries and error paths --------------------------------------


def test_a_count_larger_than_the_rows_is_red_rather_than_an_error() -> None:
    sweep = _sweep(waiver_count=3, waivers=(EXPLAINED,), acknowledgements=(ACK,))
    assert sweep.waiver_count == 3
    assert sweep.waiver_disposition is WaiverDisposition.UNEXPLAINED
    assert sweep.ready is False


def test_a_count_smaller_than_the_rows_is_refused() -> None:
    with pytest.raises(ValueError, match="smaller than"):
        _sweep(waiver_count=1, waivers=(EXPLAINED, EXPLAINED_OTHER))


def test_a_negative_waiver_count_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        _sweep(waiver_count=-1)


def test_an_acknowledgement_naming_no_counted_waiver_is_refused() -> None:
    with pytest.raises(ValueError, match="name no counted waiver"):
        _sweep(waivers=(EXPLAINED,), acknowledgements=(ACK_OTHER,))


def test_an_acknowledgement_on_a_waiverless_sweep_is_refused() -> None:
    with pytest.raises(ValueError, match="name no counted waiver"):
        _sweep(acknowledgements=(ACK,))


@pytest.mark.parametrize("blank", ["scope", "protected_principal", "acknowledged_by"])
def test_an_acknowledgement_with_a_blank_field_is_refused(blank: str) -> None:
    payload = {
        "scope": ACK.scope,
        "protected_principal": ACK.protected_principal,
        "acknowledged_by": ACK.acknowledged_by,
        blank: "",
    }
    with pytest.raises(ValidationError):
        WaiverAcknowledgement.model_validate(payload)


def test_an_acknowledgement_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        WaiverAcknowledgement.model_validate(
            {
                "scope": ACK.scope,
                "protected_principal": ACK.protected_principal,
                "acknowledged_by": ACK.acknowledged_by,
                "expires_at": "2026-09-07T00:00:00+00:00",
            }
        )


def test_a_receipt_claiming_ready_over_an_unacknowledged_waiver_is_refused() -> None:
    payload = _sweep(waivers=(EXPLAINED,)).model_dump(mode="json")
    with pytest.raises(ValidationError, match="ready=True disagrees"):
        ReleaseReadiness.model_validate(payload | {"ready": True})


def test_a_receipt_dropping_the_acknowledgement_but_keeping_ready_is_refused() -> None:
    payload = _sweep(waivers=(EXPLAINED,), acknowledgements=(ACK,)).model_dump(mode="json")
    with pytest.raises(ValidationError, match="ready=True disagrees"):
        ReleaseReadiness.model_validate(payload | {"waiver_acknowledgements": []})


def test_a_receipt_with_a_dangling_acknowledgement_is_refused() -> None:
    payload = _sweep(waivers=(EXPLAINED,), acknowledgements=(ACK,)).model_dump(mode="json")
    payload["waiver_acknowledgements"] = [ACK_OTHER.model_dump(mode="json")]
    with pytest.raises(ValidationError, match="name no counted waiver"):
        ReleaseReadiness.model_validate(payload)


def test_the_acknowledged_receipt_round_trips() -> None:
    sweep = _sweep(waivers=(EXPLAINED, EXPLAINED_OTHER), acknowledgements=(ACK, ACK_OTHER))
    assert ReleaseReadiness.model_validate(sweep.model_dump(mode="json")) == sweep
