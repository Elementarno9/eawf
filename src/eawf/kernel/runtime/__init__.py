"""Strict runtime authority records: providers, routes, and compiled Runs.

The package holds typed records only. It performs no I/O and imports no
provider SDK, so every consumer, from the configuration loader to the
daemon and the provider adapters, reads the same validated shapes.
"""

from __future__ import annotations
