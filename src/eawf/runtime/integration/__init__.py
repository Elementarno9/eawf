"""Integration runtime: attempt transitions and generation selection.

:mod:`eawf.runtime.integration.generations` moves an
:class:`~eawf.kernel.delivery.integration.IntegrationAttempt` only along
its declared edges and appends a selected
:class:`~eawf.kernel.delivery.integration.IntegrationGeneration` to a
Batch's ledger.
"""

from __future__ import annotations
