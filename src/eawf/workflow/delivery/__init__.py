"""Delivery workflow: criteria authoring validation and execution-contract compilation.

:mod:`eawf.workflow.delivery.criteria` validates authored criteria and gates
over the planning-owned :class:`~eawf.kernel.spec.common.CriterionSpec` and
:class:`~eawf.kernel.spec.common.GateSpec` types and compiles them into the
digest-bound execution contracts that proof receipts are keyed on.
"""

from __future__ import annotations
