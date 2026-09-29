"""Run measurement: read a Run's counters and spans through its vendor session.

:mod:`~eawf.observability.measurement.capture` is the capture producer the
daemon calls at a Run's start and stop, and
:mod:`~eawf.observability.measurement.transcript_spans` partitions a Run's
window of a transcript into the spans the stop reading summarizes.
"""

from __future__ import annotations
