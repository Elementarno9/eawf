"""Integration workflow: turning a blocked attempt into a readable frame.

:mod:`eawf.workflow.integration.conflict` blocks an attempt that could
not apply and writes the one
:class:`~eawf.kernel.delivery.integration.IntegrationConflict` that
describes it, with the typed exit resolution lands at. Every exit kind is
routed from a cause, and the routing table is compiled at import, so a
conflict frame nobody can leave cannot be produced.
"""

from __future__ import annotations
