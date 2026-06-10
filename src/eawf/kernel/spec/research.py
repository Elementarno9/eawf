"""Canonical research-depth vocabulary.

The ``/research`` survey depth ladder has one home: :class:`ResearchDepth`.
Before this module, the depth vocabulary drifted across three surfaces —
the skill runner used ``quick | normal | deep``, the layered-config
``research.default_depth`` leaf used ``shallow | normal | deep``, and the
render-surface help text documented ``shallow | medium | deep |
exhaustive``. This module collapses all three onto the single closed
StrEnum the help text already advertised so the skill body, the config
default, and the runner all reference the same source.

The ladder runs from the cheapest survey budget to the most exhaustive:

- :attr:`ResearchDepth.SHALLOW` — minimal sweep (single question slot).
- :attr:`ResearchDepth.MEDIUM` — default sweep (two slots).
- :attr:`ResearchDepth.DEEP` — fan-out sweep (three slots; emits a typed
  :class:`~eawf.workflow.skills.bodies.research.ResearchPlan` for the
  runtime to dispatch).
- :attr:`ResearchDepth.EXHAUSTIVE` — widest sweep (four slots; also
  emits a fan-out plan).

:data:`DEFAULT_RESEARCH_DEPTH` is the canonical default the
``research.default_depth`` config leaf ships with, and
:func:`research_depth_question_slots` maps a depth onto the synthetic
question-slot count the skill's v0.1 synthesis path pre-allocates.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class ResearchDepth(StrEnum):
    """Closed ladder of ``/research`` survey budgets.

    The string values are the on-the-wire / config / CLI tokens; the
    enum is the single source the skill runner, the skill body, and the
    layered-config ``research.default_depth`` leaf all resolve against.
    """

    SHALLOW = "shallow"
    MEDIUM = "medium"
    DEEP = "deep"
    EXHAUSTIVE = "exhaustive"


#: Canonical default research depth — the value the ``research.default_depth``
#: layered-config leaf and the skill runner fall back to when no depth is
#: supplied or an unknown token is passed.
DEFAULT_RESEARCH_DEPTH: ResearchDepth = ResearchDepth.MEDIUM

#: Closed tuple of valid depth tokens, derived from the enum so the runner's
#: validation and the config leaf's ``choices`` stay in lockstep with the
#: single source.
RESEARCH_DEPTH_VALUES: tuple[str, ...] = tuple(d.value for d in ResearchDepth)

#: Depths that trigger the typed deep-research fan-out plan rather than the
#: v0.1 placeholder synthesis path.
_FANOUT_DEPTHS: frozenset[ResearchDepth] = frozenset({ResearchDepth.DEEP, ResearchDepth.EXHAUSTIVE})

#: Synthetic question-slot count per depth — more depth pre-allocates more
#: slots so a richer body falls out of the v0.1 synthesis path.
_DEPTH_QUESTION_SLOTS: dict[ResearchDepth, int] = {
    ResearchDepth.SHALLOW: 1,
    ResearchDepth.MEDIUM: 2,
    ResearchDepth.DEEP: 3,
    ResearchDepth.EXHAUSTIVE: 4,
}


def coerce_research_depth(raw: str | None) -> ResearchDepth:
    """Coerce a raw depth token onto the canonical ladder.

    Unknown / missing tokens fall back to :data:`DEFAULT_RESEARCH_DEPTH`
    rather than raising — the skill surface treats an out-of-ladder
    ``--depth`` flag as "use the default" so a typo never aborts a run.

    Args:
        raw: The raw depth token (CLI flag, config value, or ``None``).

    Returns:
        The matching :class:`ResearchDepth`, or the canonical default
        when *raw* is ``None`` or not a ladder member.
    """
    if raw is None:
        return DEFAULT_RESEARCH_DEPTH
    try:
        return ResearchDepth(raw)
    except ValueError:
        return DEFAULT_RESEARCH_DEPTH


#: Dotted layered-config leaf the research stage resolves its no-flag default
#: from. Kept here so the leaf name has one home next to the ladder it feeds.
RESEARCH_DEFAULT_DEPTH_KEY: str = "research.default_depth"


def resolve_default_research_depth(merged_config: Mapping[str, Any]) -> ResearchDepth:
    """Resolve the no-flag research default from a merged layered-config dict.

    The research stage calls this when no ``--depth`` flag is supplied so the
    ``research.default_depth`` config leaf is honoured rather than the bare
    module constant. The caller owns the layered-config merge (this module
    stays free of any config-layer import so the one-way
    ``config -> spec.research`` dependency holds); pass the nested ``merged``
    dict :func:`eawf.kernel.config.layered.merge_config` returns.

    A missing leaf falls back to :data:`DEFAULT_RESEARCH_DEPTH` — an absent
    key just means "ships with the default". A *present* leaf set to an
    out-of-ladder token is a real misconfiguration and raises, unlike the
    lenient ``--depth`` flag path in :func:`coerce_research_depth` where a
    transient CLI typo must never abort a run.

    Args:
        merged_config: The composed layered-config dict (nested form).

    Returns:
        The configured :class:`ResearchDepth`, or :data:`DEFAULT_RESEARCH_DEPTH`
        when the leaf is absent.

    Raises:
        ValueError: the ``research.default_depth`` leaf is present but holds
            a token outside the closed ladder.
    """
    research = merged_config.get("research")
    if not isinstance(research, Mapping) or "default_depth" not in research:
        return DEFAULT_RESEARCH_DEPTH
    raw = research["default_depth"]
    try:
        return ResearchDepth(raw)
    except ValueError:
        raise ValueError(
            f"invalid {RESEARCH_DEFAULT_DEPTH_KEY!r} config value: {raw!r} "
            f"(expected one of {RESEARCH_DEPTH_VALUES})"
        ) from None


def research_depth_question_slots(depth: ResearchDepth) -> int:
    """Return the synthetic question-slot count for *depth*.

    Args:
        depth: The resolved canonical depth.

    Returns:
        The number of placeholder question slots the v0.1 synthesis path
        pre-allocates for this depth.
    """
    return _DEPTH_QUESTION_SLOTS[depth]


def research_depth_emits_fanout(depth: ResearchDepth) -> bool:
    """Return whether *depth* triggers the typed deep-research fan-out plan.

    Args:
        depth: The resolved canonical depth.

    Returns:
        ``True`` for :attr:`ResearchDepth.DEEP` and
        :attr:`ResearchDepth.EXHAUSTIVE`; ``False`` otherwise.
    """
    return depth in _FANOUT_DEPTHS


__all__ = [
    "DEFAULT_RESEARCH_DEPTH",
    "RESEARCH_DEFAULT_DEPTH_KEY",
    "RESEARCH_DEPTH_VALUES",
    "ResearchDepth",
    "coerce_research_depth",
    "research_depth_emits_fanout",
    "research_depth_question_slots",
    "resolve_default_research_depth",
]
