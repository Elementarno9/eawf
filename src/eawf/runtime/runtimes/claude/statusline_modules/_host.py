"""The source every segment read off the Claude Code statusline payload names.

The host hands the statusline its own counters on stdin. Segments read them
there and nowhere else: recovering a value from the transcript that the host
already supplies would give two figures for one quantity, and they drift.
"""

from __future__ import annotations

from typing import Final

from eawf.kernel.projection.truth import Precision, TruthKind
from eawf.kernel.state.enums import MeasurementQuality
from eawf.runtime.runtimes.claude.runtime_counters import STATUSLINE_MEASURE_VERSION
from eawf.surfaces.render.statusline import SegmentSource

#: The producer every host-payload segment names.
HOST_PRODUCER: Final = "claude-code.statusline-payload"


def host_source(
    field: str,
    *,
    truth_kind: TruthKind = TruthKind.OBSERVED,
    precision: Precision = Precision.EXACT,
    quality: MeasurementQuality = MeasurementQuality.EXACT,
) -> SegmentSource:
    """Return the source for a value the host payload carries at ``field``.

    Args:
        field: The dotted payload field the value is read from.
        truth_kind: How the value came to exist; ``derived`` when the segment
            computes it from host counters.
        precision: How exactly the rendered value states its quantity.
        quality: The measurement quality of the host's figure.

    Returns:
        The source, at the statusline parse's measure version.
    """
    return SegmentSource(
        producer=HOST_PRODUCER,
        provenance=f"statusline-payload#{field}",
        revision=STATUSLINE_MEASURE_VERSION,
        truth_kind=truth_kind,
        precision=precision,
        quality=quality,
    )


__all__ = ["HOST_PRODUCER", "host_source"]
