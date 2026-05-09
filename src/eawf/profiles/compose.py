"""Profile composition — deep-merge a sequence of profile bodies.

Per ``eawf-v0.1-plan.md`` §P03 W02 (line 247):

- Maps: deep-merge — later overrides earlier per key, except for keys named in
  :data:`STRICTEST_KEYS` where the more-restrictive value wins regardless of
  ordering.
- Lists of dicts with an ``id`` field (e.g. ``render_blocks`` keyed by ``id``,
  ``instrument_requirements`` keyed by ``name``): merge by id; later overrides
  earlier per id; first-seen insertion order is preserved (so ``render_blocks``
  emit in caller order across profiles).
- Plain string lists (``skills_referenced``, ``hooks_referenced``): union,
  deterministically sorted.
- ``state_extensions.fields_required``: union, deterministically sorted (it is
  a plain string list under the nested model).

The output :class:`ComposedProfile` records ``provenance``: for every
top-level field that received at least one input contribution, the list of
input profile names that contributed (in caller order). Default-only fields
get an empty list.

Strictest-wins keys for v0.1 are scoped to the profile-body domain only — the
config-layer keys like ``security.*`` and ``hooks.fail_closed`` belong to the
layered-config merge in :mod:`eawf.config.layered`. The only profile-body
strictest-wins rule shipped in W02 is ``instrument_requirements[].kind``:
``hard`` always overrides ``soft``.

Public API:

    compose(profiles)           -> ComposedProfile
    STRICTEST_KEYS              # tuple of strictest-wins field paths
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Final

from eawf.profiles.models import (
    ComposedProfile,
    InstrumentReq,
    ProfileBody,
    RenderBlock,
    StateExtensions,
)

logger = logging.getLogger(__name__)


# Field paths that opt into strictest-wins composition. Each entry is a
# dotted path inside the composed profile body. v0.1 only ships the
# ``instrument_requirements[].kind`` rule (``hard`` > ``soft``) — broader
# safety policy keys (``security.*``, ``hooks.fail_closed``,
# ``vcs.protected_branches``, ``acceptance.required_before_ship``) live on
# the config layers, not on profile bodies, and are merged by
# :mod:`eawf.config.layered`.
STRICTEST_KEYS: Final[tuple[str, ...]] = ("instrument_requirements[].kind",)


def _merge_instruments(
    profiles: Sequence[ProfileBody],
) -> tuple[list[InstrumentReq], list[str]]:
    """Merge ``instrument_requirements`` across *profiles*.

    Rules:

    - Keyed by :attr:`InstrumentReq.name`.
    - First-seen insertion order is preserved across profiles.
    - For duplicates, later overrides earlier *except* for ``kind`` where
      strictest-wins applies (``hard`` beats ``soft``). Non-``kind`` fields
      take the later value.

    Returns:
        ``(merged, contributors)`` where ``contributors`` is the ordered list
        of profile names that supplied at least one entry.
    """
    by_name: dict[str, InstrumentReq] = {}
    contributors: list[str] = []
    for body in profiles:
        if body.instrument_requirements and body.name not in contributors:
            contributors.append(body.name)
        for req in body.instrument_requirements:
            existing = by_name.get(req.name)
            if existing is None:
                by_name[req.name] = req.model_copy()
                continue
            # Strictest-wins on ``kind``: hard beats soft.
            new_kind = "hard" if "hard" in (existing.kind, req.kind) else "soft"
            merged = req.model_copy(update={"kind": new_kind})
            by_name[req.name] = merged
    return list(by_name.values()), contributors


def _merge_render_blocks(
    profiles: Sequence[ProfileBody],
) -> tuple[list[RenderBlock], list[str]]:
    """Merge ``render_blocks`` across *profiles* preserving caller order.

    Keyed by :attr:`RenderBlock.id`. Later overrides earlier per id, but the
    first-seen position in the merged list is locked to the order the id was
    first encountered (so a downstream profile cannot reshuffle blocks the
    upstream profile already laid out).
    """
    by_id: dict[str, RenderBlock] = {}
    order: list[str] = []
    contributors: list[str] = []
    for body in profiles:
        if body.render_blocks and body.name not in contributors:
            contributors.append(body.name)
        for block in body.render_blocks:
            if block.id not in by_id:
                order.append(block.id)
            by_id[block.id] = block.model_copy()
    return [by_id[bid] for bid in order], contributors


def _merge_string_list(
    profiles: Sequence[ProfileBody],
    field: str,
) -> tuple[list[str], list[str]]:
    """Union-merge a plain string list field across *profiles*, sorted.

    Args:
        profiles: Caller-given profile sequence.
        field: Attribute name on :class:`ProfileBody` (e.g.
            ``"skills_referenced"``).

    Returns:
        ``(sorted_unique_values, contributors)``.
    """
    seen: set[str] = set()
    contributors: list[str] = []
    for body in profiles:
        values = getattr(body, field)
        if values and body.name not in contributors:
            contributors.append(body.name)
        seen.update(values)
    return sorted(seen), contributors


def _merge_state_extensions(
    profiles: Sequence[ProfileBody],
) -> tuple[StateExtensions, list[str]]:
    """Merge ``state_extensions.fields_required`` (sorted union)."""
    seen: set[str] = set()
    contributors: list[str] = []
    for body in profiles:
        if body.state_extensions.fields_required and body.name not in contributors:
            contributors.append(body.name)
        seen.update(body.state_extensions.fields_required)
    return StateExtensions(fields_required=sorted(seen)), contributors


def _composed_name(profiles: Sequence[ProfileBody]) -> str:
    """Build the deterministic composed-profile label.

    ``compose([core, python])`` → ``"core+python"``;
    ``compose([])``             → ``"composed:empty"``.
    """
    if not profiles:
        return "composed:empty"
    return "+".join(body.name for body in profiles)


def _composed_description(profiles: Sequence[ProfileBody]) -> str:
    """Pick the description: last-wins (semantic merge later if needed)."""
    desc = ""
    for body in profiles:
        if body.description:
            desc = body.description
    return desc


def compose(profiles: Iterable[ProfileBody]) -> ComposedProfile:
    """Deep-merge *profiles* into a single :class:`ComposedProfile`.

    Determinism guarantees:

    - For non-render-block fields, the output is order-insensitive across
      caller orderings (e.g. ``compose([a, b, c]) == compose([a, c, b])``)
      because every list is a sorted-union and dict-merges only happen on
      ``instrument_requirements`` (where the strictest-wins rule on ``kind``
      is symmetric in ``a/b``).
    - For ``render_blocks``, the output preserves the caller's order: the
      first profile in the list to declare an id locks that id's slot in
      the merged list. Downstream profiles can override a block's contents
      but not its position.
    - For empty input, returns a default :class:`ComposedProfile` whose
      ``name`` is ``"composed:empty"`` and whose ``provenance`` is empty.

    Args:
        profiles: Iterable of :class:`ProfileBody`. Materialised internally.

    Returns:
        :class:`ComposedProfile` with all merged fields and a complete
        ``provenance`` map (every populated field traces back to ≥1 input
        profile name).
    """
    profile_list = list(profiles)
    logger.debug(f"compose: merging {len(profile_list)} profile(s)")

    state_ext, prov_state = _merge_state_extensions(profile_list)
    instruments, prov_instruments = _merge_instruments(profile_list)
    render_blocks, prov_blocks = _merge_render_blocks(profile_list)
    skills, prov_skills = _merge_string_list(profile_list, "skills_referenced")
    hooks, prov_hooks = _merge_string_list(profile_list, "hooks_referenced")

    provenance = {
        "state_extensions": prov_state,
        "instrument_requirements": prov_instruments,
        "render_blocks": prov_blocks,
        "skills_referenced": prov_skills,
        "hooks_referenced": prov_hooks,
    }

    return ComposedProfile(
        name=_composed_name(profile_list),
        version="1.0",
        description=_composed_description(profile_list),
        state_extensions=state_ext,
        instrument_requirements=instruments,
        render_blocks=render_blocks,
        skills_referenced=skills,
        hooks_referenced=hooks,
        provenance=provenance,
    )
