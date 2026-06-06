"""Derived fidelity-spine metrics over typed spec rows.

This package holds small, pure projections that quantify how
deterministic a wave's verification surface is and how much slips past
each gate. The first member is the Oracle-Determinism-Ratio (ODR)
metric plus its escape-ledger primitive
(:mod:`eawf.observability.metrics.odr`): both read typed
:class:`~eawf.kernel.spec.common.CriterionSpec` rows (and a tiny tagged
finding model) and return plain numbers, so they compose into the
verify spine without touching the daemon or any store.
"""

from __future__ import annotations
