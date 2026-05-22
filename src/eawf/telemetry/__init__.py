"""Telemetry subsystem — vendored row models, pricing snapshot, projection.

The telemetry package owns the observability projection (V7): Pydantic v2
row models retyped from the vendored agent-lens dataclasses, the embedded
``PRICING`` snapshot used by the cost ledger, and the per-runtime / event
projection that feeds ``eawf metrics``.

This wave (P27-I01-W11) lands the foundation: the row models
(:mod:`eawf.telemetry.models`) and the Decimal pricing snapshot
(:mod:`eawf.telemetry.pricing`). The store / sources / projector / exporter
modules described in C09 §5.9 land in later waves.
"""

from __future__ import annotations
