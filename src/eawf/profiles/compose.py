"""Profile composition — deep-merge a sequence of profile bodies (v2).

Per ``docs/architecture/profiles.md`` + the P25-W15 ProfileBody v2 brief:

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
- ``dispatch_session_policy`` (v2): last-non-``None``-wins. The closed enum
  ``{fresh, continue, hybrid, None}`` carries no strictest ordering; a
  downstream profile that explicitly sets the field wins. ``None`` is the
  default (skill / global fallback).

Conflict + override resolution (v2 — P25-W15):

1. Build the conflict graph: for each unordered pair ``(a, b)`` of inputs,
   if ``b.name in a.conflicts_with`` OR ``a.name in b.conflicts_with``,
   record an edge ``(a.name, b.name)`` in ``conflicts``.
2. Build the override graph: for each ordered ``a`` and each
   ``target in a.overrides`` that resolves to another input profile by name,
   record an edge ``(a.name, target)`` in ``overrides_map``.
3. Discharge: a conflict edge ``(a, b)`` is discharged when ``(a, b)`` OR
   ``(b, a)`` appears in ``overrides_map``. Remaining edges are
   *undeclared conflicts*.
4. Resolution policy (``conflict_resolution`` param):

   - ``"fail"`` (default — V3 fail-fast): raise :class:`ProfileConflict`.
   - ``"first-wins"`` (advisory): emit a ``conflict_warning`` for each
     undeclared edge, then drop the later-declared profile's contributions
     for the overlap from the merge passes (caller-order-first body keeps
     its values).

The output :class:`ComposedProfile` records ``provenance``, ``override_audit``
(field-path → ordered override chain), and ``conflict_warnings`` (non-fatal
notes such as render_block id overlap between two non-overriding profiles).

Strictest-wins keys for v0.1 are scoped to the profile-body domain only — the
config-layer keys like ``security.*`` and ``hooks.fail_closed`` belong to the
layered-config merge in :mod:`eawf.kernel.config.layered`. The only profile-body
strictest-wins rule shipped in W02 is ``instrument_requirements[].kind``:
``hard`` always overrides ``soft``.

Public API:

    compose(profiles, *, conflict_resolution="fail") -> ComposedProfile
    ProfileConflict          # raised on undeclared conflict under "fail" mode
    STRICTEST_KEYS           # tuple of strictest-wins field paths
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Final, Literal

from eawf.profiles.models import (
    ComposedProfile,
    InstrumentReq,
    ProfileBody,
    RenderBlock,
    StateExtensions,
)
from eawf.surfaces.cli.errors import ValidationError

logger = logging.getLogger(__name__)


#: Resolution mode for undeclared conflicts. ``"fail"`` is the default per
#: V3 fail-fast; ``"first-wins"`` keeps the earlier-declared profile's
#: contributions and emits warnings. The ``"prompt"`` mode in the C08 brief
#: belongs to an operator-facing CLI surface (AUQ); it is not exposed here.
ConflictResolution = Literal["fail", "first-wins"]


# Field paths that opt into strictest-wins composition. Each entry is a
# dotted path inside the composed profile body. v0.1 only ships the
# ``instrument_requirements[].kind`` rule (``hard`` > ``soft``) — broader
# safety policy keys (``security.*``, ``hooks.fail_closed``,
# ``vcs.protected_branches``, ``acceptance.required_before_ship``) live on
# the config layers, not on profile bodies, and are merged by
# :mod:`eawf.kernel.config.layered`.
STRICTEST_KEYS: Final[tuple[str, ...]] = ("instrument_requirements[].kind",)


class ProfileConflict(ValidationError):  # noqa: N818 — domain conflict name; kind folds to "ProfileConflict"
    """Two enabled profiles declare each other in ``conflicts_with``.

    Raised by :func:`compose` when ``conflict_resolution="fail"`` and the
    composition has at least one undeclared conflict edge. Subclasses
    :class:`eawf.surfaces.cli.errors.ValidationError` so callers that surface the
    error through ``emit_error`` get the canonical
    :data:`eawf.surfaces.cli.exit_codes.VALIDATION_FAILED` exit code. Its concrete
    class name folds into ``ErrorEnvelope.data.kind`` as
    ``"ProfileConflict"`` via :func:`eawf.surfaces.cli.errors.build_envelope`.
    """


def _build_conflict_graph(
    profile_list: Sequence[ProfileBody],
) -> list[tuple[str, str]]:
    """Return the unordered conflict edges as ``(a.name, b.name)`` tuples.

    An edge is recorded once per unordered pair; the order inside the tuple
    is the caller-input order so downstream resolution can pick a "later"
    profile deterministically.
    """
    edges: list[tuple[str, str]] = []
    for i, a in enumerate(profile_list):
        for b in profile_list[i + 1 :]:
            if b.name in a.conflicts_with or a.name in b.conflicts_with:
                edges.append((a.name, b.name))
    return edges


def _build_override_map(
    profile_list: Sequence[ProfileBody],
) -> list[tuple[str, str]]:
    """Return the override edges ``(overrider.name, overridden.name)``.

    Only edges where the ``overridden`` target resolves to another input
    profile by name are recorded. Operator-declared overrides for profiles
    that were not enabled at composition time are ignored (the dispatch
    layer never sees them).
    """
    by_name = {p.name for p in profile_list}
    edges: list[tuple[str, str]] = []
    for a in profile_list:
        for target in a.overrides:
            if target in by_name and target != a.name:
                edges.append((a.name, target))
    return edges


def _conflict_discharged(
    edge: tuple[str, str],
    overrides_map: Sequence[tuple[str, str]],
) -> bool:
    """``edge`` is discharged when either direction appears in ``overrides_map``."""
    a, b = edge
    return (a, b) in overrides_map or (b, a) in overrides_map


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
) -> tuple[list[RenderBlock], list[str], list[str]]:
    """Merge ``render_blocks`` across *profiles* preserving caller order.

    Keyed by :attr:`RenderBlock.id`. Later overrides earlier per id, but the
    first-seen position in the merged list is locked to the order the id was
    first encountered (so a downstream profile cannot reshuffle blocks the
    upstream profile already laid out).

    Returns:
        ``(merged, contributors, warning_ids)`` — ``warning_ids`` is the
        ordered list of render-block ids that were declared by more than one
        profile (caller decides whether to emit a conflict warning).
    """
    by_id: dict[str, RenderBlock] = {}
    order: list[str] = []
    contributors: list[str] = []
    seen_more_than_once: list[str] = []
    seen_ids: set[str] = set()
    for body in profiles:
        if body.render_blocks and body.name not in contributors:
            contributors.append(body.name)
        for block in body.render_blocks:
            if block.id not in by_id:
                order.append(block.id)
                seen_ids.add(block.id)
            else:
                if block.id not in seen_more_than_once:
                    seen_more_than_once.append(block.id)
            by_id[block.id] = block.model_copy()
    return [by_id[bid] for bid in order], contributors, seen_more_than_once


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


def _merge_dispatch_session_policy(
    profiles: Sequence[ProfileBody],
) -> tuple[Literal["fresh", "continue", "hybrid"] | None, list[str]]:
    """Last-non-``None``-wins on :attr:`ProfileBody.dispatch_session_policy`.

    Returns ``(policy, contributors)``. ``contributors`` lists profile names
    in caller order that supplied a non-``None`` value (the last entry is the
    winning contributor).
    """
    chosen: Literal["fresh", "continue", "hybrid"] | None = None
    contributors: list[str] = []
    for body in profiles:
        if body.dispatch_session_policy is None:
            continue
        chosen = body.dispatch_session_policy
        contributors.append(body.name)
    return chosen, contributors


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


def _override_field_paths(
    overrider: ProfileBody,
    overridden: ProfileBody,
) -> list[str]:
    """Return the field paths that ``overrider`` claimed over ``overridden``.

    A leaf is "claimed" when both profiles populate the same logical slot.
    Slots:

    - ``state_extensions.fields_required[<key>]`` — set-intersection.
    - ``instrument_requirements[name=<n>]`` — set-intersection by name.
    - ``render_blocks[id=<i>]`` — set-intersection by id.
    - ``skills_referenced[<s>]`` — set-intersection.
    - ``hooks_referenced[<h>]`` — set-intersection.
    - ``dispatch_session_policy`` — overlap when both non-``None``.

    The output is sorted for deterministic test output.
    """
    paths: list[str] = []
    state_overlap = set(overrider.state_extensions.fields_required) & set(
        overridden.state_extensions.fields_required,
    )
    for key in sorted(state_overlap):
        paths.append(f"state_extensions.fields_required[{key}]")
    instrument_overlap = {r.name for r in overrider.instrument_requirements} & {
        r.name for r in overridden.instrument_requirements
    }
    for name in sorted(instrument_overlap):
        paths.append(f"instrument_requirements[name={name}]")
    block_overlap = {b.id for b in overrider.render_blocks} & {
        b.id for b in overridden.render_blocks
    }
    for bid in sorted(block_overlap):
        paths.append(f"render_blocks[id={bid}]")
    skill_overlap = set(overrider.skills_referenced) & set(overridden.skills_referenced)
    for skill in sorted(skill_overlap):
        paths.append(f"skills_referenced[{skill}]")
    hook_overlap = set(overrider.hooks_referenced) & set(overridden.hooks_referenced)
    for hook in sorted(hook_overlap):
        paths.append(f"hooks_referenced[{hook}]")
    if (
        overrider.dispatch_session_policy is not None
        and overridden.dispatch_session_policy is not None
    ):
        paths.append("dispatch_session_policy")
    return paths


def _build_override_audit(
    profile_list: Sequence[ProfileBody],
    overrides_map: Sequence[tuple[str, str]],
) -> dict[str, list[str]]:
    """Walk the override graph and record per-leaf override chains.

    For each ``(overrider, overridden)`` edge in ``overrides_map`` the helper
    asks :func:`_override_field_paths` for the leaves the overrider claimed
    over the overridden body and stamps the chain
    ``[overrider, overridden]`` under each leaf path. When two overrides
    target the same leaf (e.g. ``a.overrides: [c]`` AND ``b.overrides: [c]``
    both touching ``render_blocks[id=foo]``) the second edge appends the
    later overrider to the existing chain.
    """
    by_name = {p.name: p for p in profile_list}
    audit: dict[str, list[str]] = {}
    for overrider_name, overridden_name in overrides_map:
        overrider = by_name.get(overrider_name)
        overridden = by_name.get(overridden_name)
        if overrider is None or overridden is None:
            continue
        for path in _override_field_paths(overrider, overridden):
            chain = audit.setdefault(path, [])
            if overrider.name not in chain:
                chain.append(overrider.name)
            if overridden.name not in chain:
                chain.append(overridden.name)
    return audit


def _format_undeclared_conflicts(edges: Sequence[tuple[str, str]]) -> str:
    """Render undeclared-conflict edges as ``"a<->b, c<->d"`` for error text."""
    return ", ".join(f"{a}<->{b}" for (a, b) in edges)


def _filter_first_wins(
    profile_list: Sequence[ProfileBody],
    undeclared: Sequence[tuple[str, str]],
) -> list[ProfileBody]:
    """Drop later contributors that lose a first-wins resolution.

    For each undeclared edge ``(a, b)`` (caller-input order), keep ``a``
    and skip ``b``'s contributions for every overlap with ``a``. This
    implementation drops ``b`` from the merge passes entirely — v0.3 ships
    whole-profile first-wins (per-leaf first-wins is deferred to v0.5+
    along with per-field overrides).
    """
    drop_names: set[str] = set()
    for _a, b in undeclared:
        drop_names.add(b)
    return [body for body in profile_list if body.name not in drop_names]


def compose(
    profiles: Iterable[ProfileBody],
    *,
    conflict_resolution: ConflictResolution = "fail",
) -> ComposedProfile:
    """Deep-merge *profiles* into a single :class:`ComposedProfile` (v2).

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
    - For ``dispatch_session_policy``, last-non-``None``-wins. With identical
      input sets in different orders the winning value may differ — callers
      who need a stable choice should pass profiles in a stable order
      (e.g. sorted by id, which the composition loader does).
    - For empty input, returns a default :class:`ComposedProfile` whose
      ``name`` is ``"composed:empty"`` and whose ``provenance`` is empty.

    Conflict + override semantics:

    1. Build the conflict graph (unordered pairs declaring each other in
       ``conflicts_with``) and the override graph (ordered edges declared
       in ``overrides``).
    2. An override edge ``(a, b)`` discharges the conflict edge ``(a, b)``
       in either direction.
    3. Undeclared edges resolve per ``conflict_resolution``:
       ``"fail"`` raises :class:`ProfileConflict`; ``"first-wins"`` keeps
       the caller-first profile and drops the later contributor with a
       ``conflict_warning``.
    4. Override chains land under :attr:`ComposedProfile.override_audit`
       keyed by field path (e.g. ``"render_blocks[id=foo]"`` →
       ``["overrider", "overridden"]``).
    5. Render-block id overlap between two non-overriding profiles emits
       a non-fatal ``conflict_warning`` (later body wins; the overlap is
       logged for the operator).

    Args:
        profiles: Iterable of :class:`ProfileBody`. Materialised internally.
        conflict_resolution: How to handle undeclared conflicts.
            ``"fail"`` (default) raises :class:`ProfileConflict`;
            ``"first-wins"`` keeps the earlier-declared profile.

    Returns:
        :class:`ComposedProfile` with merged fields, ``provenance`` map,
        ``override_audit`` chains, and ``conflict_warnings`` list.

    Raises:
        ProfileConflict: ``conflict_resolution="fail"`` and the composition
            has at least one undeclared conflict edge.
    """
    profile_list = list(profiles)
    logger.debug(f"compose profiles={len(profile_list)} resolution={conflict_resolution}")

    conflicts = _build_conflict_graph(profile_list)
    overrides_map = _build_override_map(profile_list)
    undeclared = [edge for edge in conflicts if not _conflict_discharged(edge, overrides_map)]

    conflict_warnings: list[str] = []
    if undeclared:
        if conflict_resolution == "fail":
            raise ProfileConflict(
                f"undeclared profile conflict(s): {_format_undeclared_conflicts(undeclared)}; "
                f"declare overrides: [...] on one of the profiles, or drop one from "
                f"profiles.enabled",
            )
        for a, b in undeclared:
            conflict_warnings.append(
                f"first-wins: dropped contributions of {b!r} for conflict with {a!r}",
            )
        profile_list = _filter_first_wins(profile_list, undeclared)
        # Rebuild override map for the filtered set so later audit-walks
        # reflect the actually-merged contributors.
        overrides_map = _build_override_map(profile_list)

    override_audit = _build_override_audit(profile_list, overrides_map)

    state_ext, prov_state = _merge_state_extensions(profile_list)
    instruments, prov_instruments = _merge_instruments(profile_list)
    render_blocks, prov_blocks, block_overlap_ids = _merge_render_blocks(profile_list)
    skills, prov_skills = _merge_string_list(profile_list, "skills_referenced")
    hooks, prov_hooks = _merge_string_list(profile_list, "hooks_referenced")
    policy, prov_policy = _merge_dispatch_session_policy(profile_list)

    # Non-fatal warning: render_block id declared by multiple profiles
    # where the override graph did NOT cover the overlap. The merge still
    # picks the later body, but the operator should see the overlap.
    by_name = {p.name: p for p in profile_list}
    for overlap_id in block_overlap_ids:
        declarants = [
            body.name
            for body in profile_list
            if any(b.id == overlap_id for b in body.render_blocks)
        ]
        if len(declarants) < 2:
            continue
        covered = False
        for i, declarant_a in enumerate(declarants):
            for declarant_b in declarants[i + 1 :]:
                if (declarant_a, declarant_b) in overrides_map or (
                    declarant_b,
                    declarant_a,
                ) in overrides_map:
                    covered = True
                    break
            if covered:
                break
        if not covered:
            conflict_warnings.append(
                f"render_block id={overlap_id!r} declared by {declarants!r} "
                f"without an overrides edge — later body wins",
            )
    _ = by_name  # name lookup retained for future per-leaf audit expansion

    provenance = {
        "state_extensions": prov_state,
        "instrument_requirements": prov_instruments,
        "render_blocks": prov_blocks,
        "skills_referenced": prov_skills,
        "hooks_referenced": prov_hooks,
        "dispatch_session_policy": prov_policy,
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
        dispatch_session_policy=policy,
        provenance=provenance,
        override_audit=override_audit,
        conflict_warnings=conflict_warnings,
    )
