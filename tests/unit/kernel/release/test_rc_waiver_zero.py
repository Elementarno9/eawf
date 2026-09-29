"""AUTH-052: the zero-or-explained waiver reading lapses at the first release candidate.

On the development channel a counted waiver may be explained and then
acknowledged. From ``rc1`` on the waiver count must be exactly zero: an
explained waiver is as red as an unexplained one, and no acknowledgement
clears it.
"""

from __future__ import annotations

import pytest

from eawf.kernel.release.signals import ReleaseSignalStatus
from eawf.kernel.release.waiver import ReleaseWaiver, WaiverDisposition, classify_waivers
from eawf.kernel.spec.release import ReleaseChannel
from eawf.kernel.spec.release_config import ReleaseConfig, ReleaseGateName, load_release_config
from eawf.workflow.release.train import DEV2_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.verify.release_readiness import (
    ReleaseReadiness,
    WaiverAcknowledgement,
    compute_readiness,
)
from tests._release_helpers import NOW, all_passing

_EXPLAINED = ReleaseWaiver(
    scope="P31-I01-W36",
    reason="the executor ran inline with no captured runtime",
    protected_principal="every wave close records a runtime gate receipt",
)


def _dev2_config() -> ReleaseConfig:
    """Return the first configuration whose profile carries the waiver gate."""
    return load_release_config(DEV2_RELEASE_CONFIG_YAML, train=V07_TRAIN)


def _candidate_config(version: str, channel: ReleaseChannel) -> ReleaseConfig:
    """Return the dev2 configuration re-addressed at a later checkpoint."""
    return _dev2_config().model_copy(update={"version": version, "channel": channel})


def _sweep(config: ReleaseConfig, **kwargs: object) -> ReleaseReadiness:
    return compute_readiness(
        config,
        probes=all_passing(),
        computed_at=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def _acknowledged(waiver: ReleaseWaiver) -> WaiverAcknowledgement:
    return WaiverAcknowledgement(
        scope=waiver.scope,
        protected_principal=waiver.protected_principal,
        acknowledged_by="operator",
    )


@pytest.mark.parametrize("channel", [ReleaseChannel.RC, ReleaseChannel.STABLE])
def test_auth_052_an_explained_waiver_is_forbidden_past_the_dev_channel(
    channel: ReleaseChannel,
) -> None:
    assert (
        classify_waivers((_EXPLAINED,), waiver_count=1, channel=channel)
        is WaiverDisposition.FORBIDDEN
    )


@pytest.mark.parametrize("channel", [ReleaseChannel.RC, ReleaseChannel.STABLE])
def test_auth_052_a_counted_waiver_with_no_row_is_forbidden_past_the_dev_channel(
    channel: ReleaseChannel,
) -> None:
    assert classify_waivers((), waiver_count=1, channel=channel) is WaiverDisposition.FORBIDDEN


@pytest.mark.parametrize("channel", list(ReleaseChannel))
def test_auth_052_zero_waivers_pass_on_every_channel(channel: ReleaseChannel) -> None:
    assert classify_waivers((), waiver_count=0, channel=channel) is WaiverDisposition.NONE


def test_auth_052_the_dev_channel_keeps_the_zero_or_explained_reading() -> None:
    assert (
        classify_waivers((_EXPLAINED,), waiver_count=1, channel=ReleaseChannel.DEV)
        is WaiverDisposition.AWAITING_ACKNOWLEDGEMENT
    )
    assert (
        classify_waivers((ReleaseWaiver(),), waiver_count=1, channel=ReleaseChannel.DEV)
        is WaiverDisposition.UNEXPLAINED
    )


def test_auth_052_the_channel_rule_does_not_mask_a_negative_count() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        classify_waivers((), waiver_count=-1, channel=ReleaseChannel.RC)


def test_auth_052_an_rc1_sweep_with_an_acknowledged_waiver_is_red() -> None:
    sweep = _sweep(
        _candidate_config("0.7.0rc1", ReleaseChannel.RC),
        waivers=(_EXPLAINED,),
        acknowledgements=(_acknowledged(_EXPLAINED),),
    )
    assert sweep.waiver_disposition is WaiverDisposition.FORBIDDEN
    assert sweep.waivers_cleared is False
    assert sweep.ready is False
    gate = sweep.gate_row(ReleaseGateName.WAIVER_COUNT)
    assert gate.status is ReleaseSignalStatus.FAIL
    assert "must be zero" in gate.remediation


def test_auth_052_an_rc1_sweep_with_no_waiver_is_ready() -> None:
    sweep = _sweep(_candidate_config("0.7.0rc1", ReleaseChannel.RC))
    assert sweep.waiver_disposition is WaiverDisposition.NONE
    assert sweep.ready is True


def test_auth_052_a_dev_sweep_with_an_acknowledged_waiver_stays_ready() -> None:
    sweep = _sweep(
        _dev2_config(), waivers=(_EXPLAINED,), acknowledgements=(_acknowledged(_EXPLAINED),)
    )
    assert sweep.waiver_disposition is WaiverDisposition.AWAITING_ACKNOWLEDGEMENT
    assert sweep.ready is True


def test_auth_052_a_stored_rc_readiness_claiming_acknowledgement_is_refused() -> None:
    sweep = _sweep(_candidate_config("0.7.0rc1", ReleaseChannel.RC), waivers=(_EXPLAINED,))
    payload = sweep.model_dump(mode="json")
    payload["waiver_disposition"] = WaiverDisposition.AWAITING_ACKNOWLEDGEMENT.value
    with pytest.raises(ValueError, match="disagrees"):
        ReleaseReadiness.model_validate(payload)
