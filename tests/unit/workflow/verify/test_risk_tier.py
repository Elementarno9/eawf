"""Tests: RiskTier is a closed StrEnum distinct from OracleTier.

:class:`RiskTier` is the closed set ``{mech, med, high, ui}``, extra-free, and
shares no value with the :class:`OracleTier` ladder.
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.common import OracleTier
from eawf.kernel.state.enums import RiskTier


def test_risk_tier_is_closed_strenum() -> None:
    """RiskTier is the closed StrEnum {mech, med, high, ui}."""
    assert {t.value for t in RiskTier} == {"mech", "med", "high", "ui"}
    with pytest.raises(ValueError, match="not a valid"):
        RiskTier("auditor")


def test_risk_tier_is_distinct_from_oracle_tier() -> None:
    """RiskTier {mech,med,high,ui} shares no value with OracleTier T1..T7."""
    risk_values = {t.value for t in RiskTier}
    oracle_values = {str(int(t)) for t in OracleTier} | {t.name for t in OracleTier}
    assert risk_values.isdisjoint(oracle_values)
