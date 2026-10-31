"""Unit tests for the C03 loader-side validators + heuristics.

- Heuristics (:mod:`eawf.kernel.spec.heuristics`):
  - :func:`is_ui_scope` — true for UI prefixes, false for non-UI, false
    for empty input.

- Loader-side (:mod:`eawf.kernel.spec.validators`):
  - :func:`validate_phase_spec_has_kpis` raises on empty
    ``kpis``; passes when at least one KPI is present.
"""

from __future__ import annotations

from typing import Any

import pytest

from eawf.kernel.spec.heuristics import is_ui_scope
from eawf.kernel.spec.phase import PhaseKPI, PhaseShipCriterion, PhaseSpec
from eawf.kernel.spec.validators import (
    SpecValidationError,
    validate_phase_spec_has_kpis,
)

# ---- Test fixtures ----------------------------------------------------------


def _phase_spec_factory(**overrides: Any) -> PhaseSpec:
    defaults: dict[str, Any] = {
        "id": "P25",
        "title": "Spec infrastructure phase",
        "outcome": "land C03/C07a/C07b/C08 deliverables together",
        "failure_modes": ["specs drift from state tree"],
        "ship_criteria": [PhaseShipCriterion(id="SC1", text="all waves closed")],
    }
    defaults.update(overrides)
    return PhaseSpec.model_validate(defaults)


# ---- is_ui_scope ------------------------------------------------------------


@pytest.mark.parametrize(
    "scopes,expected",
    [
        (["src/eawf/surfaces/tui/app.py"], True),
        (["src/eawf/surfaces/render/envelope.py"], True),
        (
            [
                "src/eawf/state/models.py",
                "src/eawf/surfaces/tui/widgets/header.py",
            ],
            True,
        ),
        (["src/eawf/spec/wave.py"], False),
        (["src/eawf/state/models.py"], False),
        (["tools/scripts/x.py"], False),
        ([], False),
    ],
)
def test_is_ui_scope_matches_prefixes(scopes: list[str], expected: bool) -> None:
    assert is_ui_scope(scopes) is expected


# ---- validate_phase_spec_has_kpis -------------------------------------------


def test_validate_phase_spec_has_kpis_passes_with_one_kpi() -> None:
    spec = _phase_spec_factory(
        kpis=[
            PhaseKPI(
                metric="agent_eu_total",
                target=100.0,
                direction="min",
                threshold_kind="soft",
            )
        ],
    )
    validate_phase_spec_has_kpis(spec)


def test_validate_phase_spec_has_kpis_raises_when_empty() -> None:
    spec = _phase_spec_factory()
    assert spec.kpis == []
    with pytest.raises(SpecValidationError) as excinfo:
        validate_phase_spec_has_kpis(spec)
    assert "P25" in str(excinfo.value)
    assert "kpis=[]" in str(excinfo.value)


# ---- SpecValidationError ----------------------------------------------------


def test_spec_validation_error_inherits_from_value_error() -> None:
    # CLI dispatch wants one ``except ValueError`` branch to cover
    # both pydantic.ValidationError (already a ValueError subclass) and
    # the loader-side errors.
    assert issubclass(SpecValidationError, ValueError)
