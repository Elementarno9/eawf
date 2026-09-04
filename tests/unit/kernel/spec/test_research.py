"""Tests for the canonical research-depth vocabulary in ``eawf.kernel.spec.research``.

Pins the single-source-of-truth invariant the P29-I01-W11 reconciliation
establishes:

- The ladder is the closed StrEnum ``shallow | medium | deep | exhaustive``.
- Every member value coerces back to itself.
- An invalid / unknown depth token is rejected by the enum and falls back
  to the canonical default via :func:`coerce_research_depth`.
- The canonical default resolves to ``medium``.
- The layered-config ``research.default_depth`` leaf (both the
  :data:`leaf_catalog` row and the :data:`config_keys` registry row) and the
  built-in defaults all derive their ``choices`` / ``default`` from this one
  source — proving the prior drift across config surfaces is collapsed.
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.research import (
    DEFAULT_RESEARCH_DEPTH,
    RESEARCH_DEPTH_VALUES,
    ResearchDepth,
    coerce_research_depth,
    research_depth_emits_fanout,
    research_depth_question_slots,
)

# Ladder shape -----------------------------------------------------------


def test_research_depth_ladder_is_exactly_four_members() -> None:
    """The reconciled ladder is the closed four-rung set, in cheap→exhaustive order."""
    assert [d.value for d in ResearchDepth] == ["shallow", "medium", "deep", "exhaustive"]


def test_research_depth_values_mirror_enum() -> None:
    """The exported tuple is derived from the enum, not hand-maintained."""
    assert tuple(d.value for d in ResearchDepth) == RESEARCH_DEPTH_VALUES


def test_research_depth_default_is_medium() -> None:
    """The canonical default resolves to ``medium`` (the wave success criterion)."""
    assert DEFAULT_RESEARCH_DEPTH is ResearchDepth.MEDIUM
    assert DEFAULT_RESEARCH_DEPTH.value == "medium"


# Coercion: each value valid -------------------------------------------------


@pytest.mark.parametrize("token", ["shallow", "medium", "deep", "exhaustive"])
def test_coerce_research_depth_accepts_each_ladder_token(token: str) -> None:
    """Every ladder token coerces to its matching member."""
    assert coerce_research_depth(token) is ResearchDepth(token)
    # StrEnum round-trips: the member equals its string value.
    assert coerce_research_depth(token) == token


# Coercion: invalid / boundary ----------------------------------------------


@pytest.mark.parametrize("bad", ["quick", "normal", "", "DEEP", "shallowish"])
def test_coerce_research_depth_rejects_unknown_falls_back_to_default(bad: str) -> None:
    """An out-of-ladder token (including the retired ``quick`` / ``normal``) falls back."""
    assert coerce_research_depth(bad) is DEFAULT_RESEARCH_DEPTH


def test_coerce_research_depth_none_falls_back_to_default() -> None:
    """A missing depth (``None``) resolves to the canonical default."""
    assert coerce_research_depth(None) is DEFAULT_RESEARCH_DEPTH


def test_research_depth_enum_rejects_invalid_token() -> None:
    """Direct construction with an invalid token raises — the closed-enum guarantee."""
    with pytest.raises(ValueError):
        ResearchDepth("normal")


# Question-slot + fanout helpers --------------------------------------------


def test_question_slots_increase_monotonically_with_depth() -> None:
    """Deeper surveys pre-allocate at least as many question slots."""
    slots = [research_depth_question_slots(d) for d in ResearchDepth]
    assert slots == [1, 2, 3, 4]
    assert slots == sorted(slots)


def test_fanout_emitted_only_for_deep_and_exhaustive() -> None:
    """The typed fan-out plan fires for the two deepest rungs only."""
    assert not research_depth_emits_fanout(ResearchDepth.SHALLOW)
    assert not research_depth_emits_fanout(ResearchDepth.MEDIUM)
    assert research_depth_emits_fanout(ResearchDepth.DEEP)
    assert research_depth_emits_fanout(ResearchDepth.EXHAUSTIVE)


# Config wiring: the drift is collapsed to one source -----------------------


def test_leaf_catalog_default_depth_derives_from_canonical_enum() -> None:
    """The layered-config leaf's choices + default come from the one source."""
    from eawf.kernel.config.registry.leaf_catalog import leaf_key_lookup

    leaf = leaf_key_lookup("research.default_depth")
    assert leaf.choices == RESEARCH_DEPTH_VALUES
    assert leaf.default == DEFAULT_RESEARCH_DEPTH.value


def test_config_keys_default_depth_derives_from_canonical_enum() -> None:
    """The TUI/CLI config registry row's choices + default come from the one source."""
    from eawf.kernel.config.registry.config_keys import registry_lookup

    entry = registry_lookup("research.default_depth")
    assert entry is not None
    assert entry.choices == RESEARCH_DEPTH_VALUES
    assert entry.default == DEFAULT_RESEARCH_DEPTH.value


def test_built_in_defaults_default_depth_is_canonical() -> None:
    """The built-in config default matches the canonical default (``medium``)."""
    from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS

    assert BUILT_IN_DEFAULTS["research"]["default_depth"] == DEFAULT_RESEARCH_DEPTH.value
