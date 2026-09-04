"""Tests for :mod:`eawf.kernel.spec.measured_contract`.

Pins the strict/frozen shape of the measured-before-build records:

1. A well-formed :class:`MeasuredContract` validates and exposes every
   named field.
2. ``contract_id`` is pinned to ``MCT-`` plus eight digits.
3. A blank (empty or whitespace-only) ``boundary`` raises
   ``ValidationError`` — the load-bearing rejection.
4. Every record is frozen and forbids extra keys.
5. Boundary cases on the collection fields: empty ``observed``, empty
   ``limits``, single-entry of each, and ``population_size`` at its
   off-by-one floor.
6. :func:`scale_band_satisfies` orders the bands and rejects a
   non-member band.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.measured_contract import (
    SCALE_BAND_ORDER,
    MeasuredContract,
    MeasurementEnvironment,
    ObservedLimit,
    ScaleBand,
    scale_band_satisfies,
)

_OBSERVED_AT = datetime(2026, 8, 13, tzinfo=UTC)


def _environment(**overrides: object) -> MeasurementEnvironment:
    kwargs: dict[str, object] = {
        "scale_band": ScaleBand.PRODUCTION,
        "population": "one epoch-1 state.json of 5,743,039 bytes",
        "population_size": 10684,
        "host_platform": "darwin",
        "toolchain": "python 3.14.3",
    }
    kwargs.update(overrides)
    return MeasurementEnvironment(**kwargs)  # type: ignore[arg-type]


def _limit(**overrides: object) -> ObservedLimit:
    kwargs: dict[str, object] = {
        "name": "state_bytes",
        "value": 5743039.0,
        "unit": "bytes",
        "direction": "ceiling",
        "basis": "P5 source_bytes census",
    }
    kwargs.update(overrides)
    return ObservedLimit(**kwargs)  # type: ignore[arg-type]


def _contract(**overrides: object) -> MeasuredContract:
    kwargs: dict[str, object] = {
        "contract_id": "MCT-26081301",
        "surface": "epoch-1 state.json population",
        "probe_command": "uv run python probe.py",
        "observed": {"state_bytes": 5743039},
        "limits": (_limit(),),
        "boundary": "measured on one population at schema_version 1.19",
        "observed_at": _OBSERVED_AT,
        "observed_at_ref": ".ea/local/spikes/2026-08-13-v07-preflight/x/observations.json",
        "environment": _environment(),
    }
    kwargs.update(overrides)
    return MeasuredContract(**kwargs)  # type: ignore[arg-type]


# ---- happy path ------------------------------------------------------------


def test_measured_contract_accepts_every_named_field() -> None:
    contract = _contract()
    assert contract.contract_id == "MCT-26081301"
    assert contract.surface == "epoch-1 state.json population"
    assert contract.probe_command == "uv run python probe.py"
    assert contract.observed["state_bytes"] == 5743039
    assert contract.limits[0].direction == "ceiling"
    assert contract.boundary
    assert contract.observed_at == _OBSERVED_AT
    assert contract.observed_at_ref.endswith("observations.json")
    assert contract.environment.scale_band is ScaleBand.PRODUCTION


def test_measured_contract_limit_returns_named_row() -> None:
    contract = _contract(limits=(_limit(), _limit(name="max_row_bytes", value=25794.0)))
    assert contract.limit("max_row_bytes").value == pytest.approx(25794.0)


def test_measured_contract_limit_unknown_name_raises_key_error() -> None:
    with pytest.raises(KeyError, match="no limit named 'nope'"):
        _contract().limit("nope")


# ---- contract_id pattern ---------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["MCT-2608130", "MCT-260813011", "MCT-2608130a", "mct-26081301", "26081301", ""],
)
def test_measured_contract_rejects_malformed_contract_id(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        _contract(contract_id=bad_id)


def test_measured_contract_accepts_eight_digit_contract_id() -> None:
    assert _contract(contract_id="MCT-00000000").contract_id == "MCT-00000000"


# ---- the blank-boundary rejection ------------------------------------------


@pytest.mark.parametrize("blank", ["", " ", "\t", "\n", "   \t \n "])
def test_measured_contract_blank_boundary_raises_validation_error(blank: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _contract(boundary=blank)
    assert "boundary" in str(excinfo.value)


def test_measured_contract_single_character_boundary_is_accepted() -> None:
    assert _contract(boundary="x").boundary == "x"


@pytest.mark.parametrize(
    "field",
    ["surface", "probe_command", "observed_at_ref"],
)
def test_measured_contract_blank_text_fields_raise_validation_error(field: str) -> None:
    with pytest.raises(ValidationError):
        _contract(**{field: "   "})


# ---- strict + frozen -------------------------------------------------------


def test_measured_contract_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _contract(unexpected="x")


def test_measured_contract_is_frozen() -> None:
    with pytest.raises(ValidationError):
        _contract().boundary = "rewritten"  # type: ignore[misc]


def test_measurement_environment_is_frozen_and_strict() -> None:
    env = _environment()
    with pytest.raises(ValidationError):
        env.scale_band = ScaleBand.TOY  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _environment(unexpected="x")


def test_observed_limit_is_frozen_and_strict() -> None:
    row = _limit()
    with pytest.raises(ValidationError):
        row.value = 1.0  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _limit(unexpected="x")


def test_observed_limit_rejects_unknown_direction() -> None:
    with pytest.raises(ValidationError):
        _limit(direction="sideways")


def test_measurement_environment_requires_environment_on_contract() -> None:
    with pytest.raises(ValidationError, match="environment"):
        MeasuredContract(  # type: ignore[call-arg]
            contract_id="MCT-26081301",
            surface="s",
            probe_command="p",
            observed={"a": 1},
            limits=(_limit(),),
            boundary="b",
            observed_at=_OBSERVED_AT,
            observed_at_ref="r",
        )


# ---- collection boundaries -------------------------------------------------


def test_measured_contract_empty_observed_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        _contract(observed={})


def test_measured_contract_empty_limits_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        _contract(limits=())


def test_measured_contract_single_observation_and_limit_are_accepted() -> None:
    contract = _contract(observed={"only": True}, limits=(_limit(),))
    assert len(contract.observed) == 1
    assert len(contract.limits) == 1


def test_measurement_environment_population_size_floor_is_one() -> None:
    assert _environment(population_size=1).population_size == 1
    with pytest.raises(ValidationError):
        _environment(population_size=0)


def test_measured_contract_rejects_non_scalar_observation() -> None:
    with pytest.raises(ValidationError):
        _contract(observed={"nested": {"a": 1}})


def test_measured_contract_rejects_naive_observed_at() -> None:
    with pytest.raises(ValidationError):
        _contract(observed_at=datetime(2026, 8, 13))


# ---- scale-band ordering ---------------------------------------------------


def test_scale_band_order_is_ascending_and_total() -> None:
    assert SCALE_BAND_ORDER == (
        ScaleBand.TOY,
        ScaleBand.DEV,
        ScaleBand.PRODUCTION,
        ScaleBand.FLEET,
    )
    assert set(SCALE_BAND_ORDER) == set(ScaleBand)


@pytest.mark.parametrize(
    ("observed", "required", "expected"),
    [
        (ScaleBand.PRODUCTION, ScaleBand.PRODUCTION, True),
        (ScaleBand.FLEET, ScaleBand.PRODUCTION, True),
        (ScaleBand.DEV, ScaleBand.PRODUCTION, False),
        (ScaleBand.TOY, ScaleBand.DEV, False),
        (ScaleBand.TOY, ScaleBand.TOY, True),
    ],
)
def test_scale_band_satisfies_orders_bands(
    observed: ScaleBand, required: ScaleBand, expected: bool
) -> None:
    assert scale_band_satisfies(observed, required=required) is expected


def test_scale_band_satisfies_rejects_non_member_band() -> None:
    with pytest.raises(ValueError, match="absent from SCALE_BAND_ORDER"):
        scale_band_satisfies("gigantic", required=ScaleBand.TOY)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="absent from SCALE_BAND_ORDER"):
        scale_band_satisfies(ScaleBand.TOY, required="gigantic")  # type: ignore[arg-type]
