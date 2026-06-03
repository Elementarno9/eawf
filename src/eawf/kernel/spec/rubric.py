"""Rubric extraction over a :class:`~eawf.kernel.spec.wave.WaveSpec`.

A wave's quality rubric lives inside ``WaveSpec.behaviors``: the
behaviours flagged ``jury_scorable=True`` are the rubric items the
spec-jury later scores, each tagged with the ISO-25010
``quality_dimension`` it is measured on. Keeping the rubric inside the
one WaveSpec document means the existing spec loader
(``verify_implements``) keeps reading the same
``.ea/specs/<phase>/**/*.md`` file — no second artifact to load or
keep in sync.

This module is the read-only projection from a WaveSpec to its rubric
items; the jury-scoring machinery consumes the tuple this returns.
"""

from __future__ import annotations

from eawf.kernel.spec.wave import WaveBehavior, WaveSpec


def rubric_items(spec: WaveSpec) -> tuple[WaveBehavior, ...]:
    """Return the jury-scorable behaviours of ``spec`` in spec order.

    The rubric is the subset of ``spec.behaviors`` flagged
    ``jury_scorable=True`` — the RubricItems the spec-jury scores.
    Order is preserved so the rubric reads top-to-bottom in the same
    sequence the WaveSpec author wrote the behaviours. Returns an empty
    tuple when no behaviour is scorable.

    Args:
        spec: The wave deliverable spec whose behaviours hold the
            rubric.

    Returns:
        The jury-scorable :class:`~eawf.kernel.spec.wave.WaveBehavior`
        rows, in spec order.
    """
    return tuple(behavior for behavior in spec.behaviors if behavior.jury_scorable)


__all__ = ["rubric_items"]
