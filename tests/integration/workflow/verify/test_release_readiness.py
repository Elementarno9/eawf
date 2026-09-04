"""Tests for :mod:`eawf.workflow.verify.release_readiness`.

The contract under test is that a preflight sweep is *total*: twelve
rows every run, whatever any single signal does. So the module pins:

1. All twelve rows come back with no probes registered at all, each
   ``unavailable`` and each naming the missing producer.
2. A red version signal does not short-circuit the sweep -- the other
   eleven rows are still computed, and ``first_red`` names the version
   row.
3. A probe that raises becomes a ``blocked`` row rather than an
   exception out of :func:`compute_readiness`.
4. Every row carries status, observed revision, evidence refs, the
   computed/expiry window and remediation.
5. The required subset is derived from the one authored surface: the
   bound gate names plus the three configuration flags.
6. Error paths on :func:`compute_readiness` (ttl, waiver count, naive
   timestamp) and on both records (partial sweep, lying ``ready``, a
   passing row with a failure code, a red row with no remediation, an
   expiry window that does not move forward).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.verify.release_readiness import (
    GATE_SIGNAL_BINDINGS,
    SIGNAL_FAILURE_CODES,
    ReleaseReadiness,
    ReleaseSignalContext,
    ReleaseSignalFailureCode,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalProbe,
    ReleaseSignalRow,
    ReleaseSignalStatus,
    compute_readiness,
    derive_required_signals,
)

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _config(**overrides: Any) -> ReleaseConfig:
    """Return the dev1 configuration with top-level *overrides* applied."""
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV1_RELEASE_CONFIG_YAML))["release"]
    body.update(overrides)
    return load_release_config({"release": body}, train=V07_TRAIN)


def _outcome(status: ReleaseSignalStatus, *, refs: tuple[str, ...] = ()) -> ReleaseSignalOutcome:
    """Return an outcome carrying remediation whenever it is not passing."""
    remediation = "" if status is ReleaseSignalStatus.PASS else f"repair the {status.value} row"
    return ReleaseSignalOutcome(status=status, remediation=remediation, evidence_refs=refs)


def _probe(status: ReleaseSignalStatus, *, refs: tuple[str, ...] = ()) -> ReleaseSignalProbe:
    """Return a probe that always reports *status*."""

    def run(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        assert context.signal in ReleaseSignalName
        return _outcome(status, refs=refs)

    return run


def _all_passing() -> dict[ReleaseSignalName, ReleaseSignalProbe]:
    """Return a probe registry where every signal passes."""
    return {name: _probe(ReleaseSignalStatus.PASS) for name in ReleaseSignalName}


# ---------------------------------------------------------------------------
# Totality
# ---------------------------------------------------------------------------


def test_release_readiness_reports_every_signal_without_probes() -> None:
    readiness = compute_readiness(_config(), computed_at=_NOW)
    assert len(readiness.signals) == len(ReleaseSignalName)
    assert [row.signal for row in readiness.signals] == list(ReleaseSignalName)
    # Three signals this project already produces. The platform row
    # computes from the checkpoint's claims; the dependencies and
    # artifacts rows are read back from CI receipts, so with none written
    # they are unavailable naming the producing job rather than naming a
    # producer that does not exist.
    produced = {
        ReleaseSignalName.PLATFORM,
        ReleaseSignalName.DEPENDENCIES,
        ReleaseSignalName.ARTIFACTS,
    }
    for row in readiness.signals:
        if row.signal in produced:
            continue
        assert row.status is ReleaseSignalStatus.UNAVAILABLE
        assert row.failure_code is SIGNAL_FAILURE_CODES[row.signal]
        assert "no producer is registered" in row.remediation
    assert readiness.row(ReleaseSignalName.PLATFORM).status is ReleaseSignalStatus.PASS
    for signal in (ReleaseSignalName.DEPENDENCIES, ReleaseSignalName.ARTIFACTS):
        receipt_row = readiness.row(signal)
        assert receipt_row.status is ReleaseSignalStatus.UNAVAILABLE
        assert "inventory-and-reproducibility" in receipt_row.remediation
    assert readiness.ready is False


def test_release_readiness_does_not_short_circuit_on_a_red_version_signal() -> None:
    probes: dict[ReleaseSignalName, ReleaseSignalProbe] = _all_passing()
    probes[ReleaseSignalName.VERSION_CONSISTENCY] = _probe(ReleaseSignalStatus.FAIL)
    readiness = compute_readiness(_config(), probes=probes, computed_at=_NOW)
    assert len(readiness.signals) == 12
    assert readiness.row(ReleaseSignalName.VERSION_CONSISTENCY).status is (ReleaseSignalStatus.FAIL)
    assert readiness.row(ReleaseSignalName.VERSION_CONSISTENCY).failure_code is (
        ReleaseSignalFailureCode.VERSION_MISMATCH
    )
    others = [
        row for row in readiness.signals if row.signal is not ReleaseSignalName.VERSION_CONSISTENCY
    ]
    assert all(row.status is ReleaseSignalStatus.PASS for row in others)
    assert readiness.first_red is ReleaseSignalName.VERSION_CONSISTENCY
    assert readiness.ready is False


def test_release_readiness_is_ready_when_every_required_row_passes() -> None:
    readiness = compute_readiness(_config(), probes=_all_passing(), computed_at=_NOW)
    assert readiness.first_red is None
    assert readiness.ready is True
    assert readiness.waiver_count == 0


def test_release_readiness_turns_a_raising_probe_into_a_blocked_row() -> None:
    def explode(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        raise RuntimeError("the reproducibility build host is unreachable")

    probes: dict[ReleaseSignalName, ReleaseSignalProbe] = _all_passing()
    probes[ReleaseSignalName.ARTIFACTS] = explode
    readiness = compute_readiness(_config(), probes=probes, computed_at=_NOW)
    row = readiness.row(ReleaseSignalName.ARTIFACTS)
    assert row.status is ReleaseSignalStatus.BLOCKED
    assert "RuntimeError" in row.remediation
    assert len(readiness.signals) == 12
    assert readiness.ready is False


def test_release_readiness_stamps_every_row_with_its_freshness_window() -> None:
    readiness = compute_readiness(
        _config(),
        probes={
            ReleaseSignalName.CHANGELOG: _probe(
                ReleaseSignalStatus.PASS, refs=("artifact://changelog/0.7.0.dev1",)
            )
        },
        observed_revision="a" * 40,
        computed_at=_NOW,
        ttl_seconds=900,
    )
    row = readiness.row(ReleaseSignalName.CHANGELOG)
    assert row.observed_revision == "a" * 40
    assert row.evidence_refs == ("artifact://changelog/0.7.0.dev1",)
    assert row.computed_at == _NOW
    assert row.expires_at == _NOW + timedelta(seconds=900)
    assert row.remediation == ""
    assert all(r.observed_revision == "a" * 40 for r in readiness.signals)


def test_release_readiness_waivers_block_readiness_even_when_green() -> None:
    readiness = compute_readiness(
        _config(), probes=_all_passing(), computed_at=_NOW, waiver_count=1
    )
    assert readiness.first_red is None
    assert readiness.waiver_count == 1
    assert readiness.ready is False


# ---------------------------------------------------------------------------
# The derived required subset
# ---------------------------------------------------------------------------


def test_derive_required_signals_covers_the_dev1_profile() -> None:
    assert derive_required_signals(_config()) == (
        ReleaseSignalName.VERSION_CONSISTENCY,
        ReleaseSignalName.CHANGELOG,
        ReleaseSignalName.ANCESTRY,
        ReleaseSignalName.TREE_CLEANLINESS,
        ReleaseSignalName.ARTIFACTS,
        ReleaseSignalName.DEPENDENCIES,
        ReleaseSignalName.CREDENTIALS,
    )


@pytest.mark.parametrize(
    ("flag", "dropped"),
    [
        ("require_clean_tree", ReleaseSignalName.TREE_CLEANLINESS),
        ("require_ancestor_of_remote", ReleaseSignalName.ANCESTRY),
    ],
)
def test_derive_required_signals_drops_a_row_when_its_flag_is_off(
    flag: str, dropped: ReleaseSignalName
) -> None:
    assert dropped not in derive_required_signals(_config(**{flag: False}))


def test_derive_required_signals_drops_credentials_without_a_required_target() -> None:
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV1_RELEASE_CONFIG_YAML))["release"]
    for target in body["targets"]:
        target["required"] = False
    config = load_release_config({"release": body}, train=V07_TRAIN)
    assert ReleaseSignalName.CREDENTIALS not in derive_required_signals(config)


def test_derive_required_signals_binds_two_gates_to_one_row_once() -> None:
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV1_RELEASE_CONFIG_YAML))["release"]
    body["gates"]["required"] = ["dependency_inventory", "security_review"]
    body["require_clean_tree"] = False
    body["require_ancestor_of_remote"] = False
    for target in body["targets"]:
        target["required"] = False
    config = load_release_config({"release": body}, train=V07_TRAIN)
    assert derive_required_signals(config) == (ReleaseSignalName.DEPENDENCIES,)


def test_gate_signal_bindings_are_total_over_the_declared_gate_names() -> None:
    from eawf.kernel.spec.release_config import ReleaseGateName

    assert set(GATE_SIGNAL_BINDINGS) == set(ReleaseGateName)
    assert set(SIGNAL_FAILURE_CODES) == set(ReleaseSignalName)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ttl", [0, -1])
def test_compute_readiness_rejects_a_nonpositive_ttl(ttl: int) -> None:
    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        compute_readiness(_config(), computed_at=_NOW, ttl_seconds=ttl)


def test_compute_readiness_accepts_a_one_second_ttl() -> None:
    readiness = compute_readiness(_config(), computed_at=_NOW, ttl_seconds=1)
    assert readiness.signals[0].expires_at == _NOW + timedelta(seconds=1)


def test_compute_readiness_rejects_a_negative_waiver_count() -> None:
    with pytest.raises(ValueError, match="waiver_count must not be negative"):
        compute_readiness(_config(), computed_at=_NOW, waiver_count=-1)


def test_compute_readiness_rejects_a_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="must be timezone-aware"):
        compute_readiness(_config(), computed_at=datetime(2026, 9, 4, 12, 0))


def test_release_readiness_rejects_a_partial_sweep() -> None:
    readiness = compute_readiness(_config(), probes=_all_passing(), computed_at=_NOW)
    payload: dict[str, Any] = readiness.model_dump(mode="json")
    payload["signals"] = payload["signals"][:-1]
    with pytest.raises(ValidationError, match="must report every signal"):
        ReleaseReadiness.model_validate(payload)


def test_release_readiness_rejects_a_duplicated_signal_row() -> None:
    readiness = compute_readiness(_config(), probes=_all_passing(), computed_at=_NOW)
    payload: dict[str, Any] = readiness.model_dump(mode="json")
    payload["signals"] = [*payload["signals"], payload["signals"][0]]
    with pytest.raises(ValidationError, match="more than once"):
        ReleaseReadiness.model_validate(payload)


def test_release_readiness_rejects_a_ready_flag_that_lies() -> None:
    readiness = compute_readiness(_config(), computed_at=_NOW)
    payload: dict[str, Any] = readiness.model_dump(mode="json")
    payload["ready"] = True
    with pytest.raises(ValidationError, match="disagrees with the rows"):
        ReleaseReadiness.model_validate(payload)


def test_release_readiness_rejects_a_required_signal_absent_from_the_sweep() -> None:
    readiness = compute_readiness(_config(), probes=_all_passing(), computed_at=_NOW)
    payload: dict[str, Any] = readiness.model_dump(mode="json")
    payload["signals"] = payload["signals"][:-1]
    payload["required_signals"] = ["credentials"]
    with pytest.raises(ValidationError, match="must report every signal"):
        ReleaseReadiness.model_validate(payload)


def _row(**overrides: Any) -> Mapping[str, Any]:
    """Return a passing changelog row payload with *overrides* applied."""
    payload: dict[str, Any] = {
        "signal": "changelog",
        "status": "pass",
        "computed_at": _NOW,
        "expires_at": _NOW + timedelta(seconds=60),
    }
    payload.update(overrides)
    return payload


def test_release_signal_row_rejects_a_passing_row_with_a_failure_code() -> None:
    with pytest.raises(ValidationError, match="passed but carries failure_code"):
        ReleaseSignalRow.model_validate(_row(failure_code="changelog_missing"))


def test_release_signal_row_rejects_a_red_row_with_the_wrong_failure_code() -> None:
    with pytest.raises(ValidationError, match="must report 'changelog_missing'"):
        ReleaseSignalRow.model_validate(
            _row(status="fail", failure_code="version_mismatch", remediation="x")
        )


def test_release_signal_row_rejects_a_red_row_with_no_remediation() -> None:
    with pytest.raises(ValidationError, match="must carry remediation"):
        ReleaseSignalRow.model_validate(
            _row(status="fail", failure_code="changelog_missing", remediation="   ")
        )


@pytest.mark.parametrize("delta", [0, -1])
def test_release_signal_row_rejects_a_window_that_does_not_move_forward(delta: int) -> None:
    with pytest.raises(ValidationError, match="expires_at must be after computed_at"):
        ReleaseSignalRow.model_validate(_row(expires_at=_NOW + timedelta(seconds=delta)))


def test_release_signal_row_is_frozen() -> None:
    row = ReleaseSignalRow.model_validate(_row())
    with pytest.raises(ValidationError):
        row.status = ReleaseSignalStatus.FAIL  # type: ignore[misc]
