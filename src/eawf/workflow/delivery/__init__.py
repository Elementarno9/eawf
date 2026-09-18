"""Delivery workflow: what a Task promised, compiled, and proved on the Batch.

:mod:`eawf.workflow.delivery.criteria` validates authored criteria and gates
over the planning-owned :class:`~eawf.kernel.spec.common.CriterionSpec` and
:class:`~eawf.kernel.spec.common.GateSpec` types and compiles them into the
digest-bound execution contracts that proof receipts are keyed on.

:mod:`eawf.workflow.delivery.completion` consumes those contracts at the
other end: it refuses a Task whose reported success carries no seal and no
selected generation, and splits the rest into the criteria an integration
invalidated -- required again at the current Batch head -- and the criteria
that stay proved at the generation that delivered the Task.
"""

from __future__ import annotations
