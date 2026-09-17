"""Verification runtime: receipt freshness and reuse decisions.

:mod:`eawf.runtime.verification.receipts` assembles the
:class:`~eawf.kernel.delivery.receipts.ProofFreshnessKey` of a compiled
execution contract, rejects a receipt taken against other inputs, and
decides which required legs reuse a fresh receipt and which must rerun.
"""

from __future__ import annotations
