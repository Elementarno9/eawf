"""Observability layer: telemetry, logging, and diagnostics packages.

The observability super-package groups the packages that measure and
report on eawf's own behaviour on top of the kernel, workflow, runtime,
and surfaces layers: :mod:`~eawf.observability.telemetry` (vendored row
models, pricing snapshot, projection), :mod:`~eawf.observability.logging`
(structured-logging support for library modules),
:mod:`~eawf.observability.doctor` (install / runtime diagnostics +
doc-verify), :mod:`~eawf.observability.bench` (the performance bench
harness), and :mod:`~eawf.observability.eval` (the skill-eval
semantic-scoring layer).
"""

from __future__ import annotations
