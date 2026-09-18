"""Read models whose inputs are process records rather than document rows alone.

A read model lives in :mod:`eawf.kernel.projection` when everything it draws comes from
the epoch-2 document. The acceptance family cannot: a sealed bundle, the approval given
to its digest and a proof receipt are process records, and the approval type is declared
in :mod:`eawf.workflow.delivery.acceptance`. The family is therefore stated here, one
layer up, and takes those records beside the projection the daemon served.
"""

from __future__ import annotations
