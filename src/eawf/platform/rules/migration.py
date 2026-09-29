"""Inventory the legacy profile-renderer content a rule-graph migration disposes.

Before the rule graph, steering prose lived in three places: the render blocks
of the enabled profiles, the fields of the profile model, and hand-written
text around the managed regions of ``AGENTS.md``. A migration gives each of
them exactly one typed disposition:

- every field of the legacy profile model is disposed in
  :data:`PROFILE_FIELD_DISPOSITIONS`, and reflection over the model refuses a
  field with no disposition or a disposition naming no field, so the table
  cannot drift from the model it describes;
- every render block and any hand-written root prose is an inventory item
  the operator disposes in the ``legacy`` list of ``.ea/rules.yaml``.

The inventory never interprets prose. A block's text is carried verbatim and
stays legacy prose until an operator disposition says where it goes, and
hand-written root prose is uncertified input, never a rule source. Ownership
is settled by obligation identifier only: textual similarity lists review
candidates beside an item and never disposes it.

After the switch to ``.ea/rules.yaml`` the legacy blocks are read here and
nowhere else on the steering path; no renderer writes them into a projection.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Final, Literal

from eawf.platform.profiles.models import ProfileBody
from eawf.platform.rules.compile import RuleGraph, compile_rule_graph
from eawf.platform.rules.loader import load_rule_source
from eawf.platform.rules.modules import select_rule_modules
from eawf.platform.rules.records import (
    OBLIGATION_DISPOSITIONS,
    LegacyDisposition,
    LegacyDispositionKind,
    LegacyItemKey,
    RuleModel,
)

logger = logging.getLogger(__name__)

#: The disposition of the ``render_blocks`` field: each block is its own item.
PER_BLOCK: Final = "per_block"

#: One disposition per field of the legacy profile model.
PROFILE_FIELD_DISPOSITIONS: Final[Mapping[str, LegacyDispositionKind | Literal["per_block"]]] = (
    MappingProxyType(
        {
            # Composition bookkeeping: identity, format and layering carry no
            # obligation once profiles stop rendering steering text.
            "schema_version": "rejected",
            "name": "rejected",
            "version": "rejected",
            "description": "rejected",
            "extends": "rejected",
            "conflicts_with": "rejected",
            "overrides": "rejected",
            "state_extensions": "domain_policy",
            "instrument_requirements": "tool_policy",
            "render_blocks": PER_BLOCK,
            "skills_referenced": "procedure_reference",
            "hooks_referenced": "tool_policy",
            "dispatch_session_policy": "run_policy",
            "verify": "verification_policy",
            "research": "domain_policy",
            "output": "operator_owned",
            "track": "domain_policy",
        }
    )
)

#: The share of a rule's instruction words a legacy text must contain for the
#: rule to be listed as a review candidate.
REVIEW_CANDIDATE_FLOOR: Final[float] = 0.6

_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")

LegacyItemKind = Literal["render_block", "profile_field", "root_prose"]


class LegacyMigrationError(ValueError):
    """The legacy model and its disposition table disagree.

    Attributes:
        code: The stable failure code.
    """

    code: ClassVar[str] = "legacy_migration"


class LegacyItem(RuleModel):
    """One piece of legacy content, carried exactly as the legacy source held it.

    Attributes:
        kind: A render block, a profile-model field or hand-written root prose.
        key: The inventory key a disposition names.
        prose: The verbatim legacy text; empty for a profile-model field.
        certified: ``False`` for hand-written root prose, which no renderer
            produced and which is migration input only.
    """

    kind: LegacyItemKind
    key: LegacyItemKey
    prose: str
    certified: bool = True


class LegacyEntry(RuleModel):
    """An inventory item joined to its disposition.

    Attributes:
        item: The legacy content.
        disposition: The disposition, or ``None`` while the item is still
            legacy prose awaiting the operator.
        review_candidates: Rules whose instruction the item's text largely
            repeats; a prompt for review that never disposes the item.
    """

    item: LegacyItem
    disposition: LegacyDisposition | None
    review_candidates: tuple[str, ...] = ()


class LegacyMigration(RuleModel):
    """The migration inventory with every disposition checked against it.

    Attributes:
        entries: One entry per inventory item, fields first, then blocks,
            then root prose.
        unknown: Disposed keys the inventory does not hold.
        repeated: Inventory keys disposed more than once.
        unresolved: Obligations a disposition names that no rule in the
            effective graph owns.
    """

    entries: tuple[LegacyEntry, ...]
    unknown: tuple[str, ...] = ()
    repeated: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    @property
    def undisposed(self) -> tuple[str, ...]:
        """Return the keys still awaiting an operator disposition."""
        return tuple(entry.item.key for entry in self.entries if entry.disposition is None)

    @property
    def complete(self) -> bool:
        """Return whether every item has exactly one resolvable disposition."""
        return not (self.undisposed or self.unknown or self.repeated or self.unresolved)


def profile_field_dispositions() -> dict[str, LegacyDispositionKind]:
    """Reflect over the legacy profile model and return each field's disposition.

    Returns:
        Every field except ``render_blocks``, which is disposed block by block.

    Raises:
        LegacyMigrationError: When a model field has no disposition or a
            disposition names a field the model does not have.
    """
    fields = set(ProfileBody.model_fields)
    missing = sorted(fields - set(PROFILE_FIELD_DISPOSITIONS))
    stale = sorted(set(PROFILE_FIELD_DISPOSITIONS) - fields)
    if missing or stale:
        raise LegacyMigrationError(
            f"the legacy profile model and its disposition table disagree: fields without a "
            f"disposition {missing}, dispositions without a field {stale}"
        )
    return {
        name: disposition
        for name, disposition in PROFILE_FIELD_DISPOSITIONS.items()
        if disposition != PER_BLOCK
    }


def legacy_inventory(repo_root: Path) -> tuple[LegacyItem, ...]:
    """Inventory the legacy content of a repository, machine-produced.

    Args:
        repo_root: The repository root.

    Returns:
        One item per legacy profile field, per render block of the enabled
        profiles, and for hand-written prose in ``AGENTS.md``.

    Raises:
        LegacyMigrationError: When the profile model and its disposition
            table disagree.
        ValueError: When ``profiles.enabled`` is malformed or names an
            unknown profile.
    """
    from eawf.platform.profiles.compose import compose
    from eawf.platform.profiles.loader import load_profile
    from eawf.platform.profiles.selection import resolve_enabled_profiles

    fields = tuple(
        LegacyItem(kind="profile_field", key=f"field:{name}", prose="")
        for name in profile_field_dispositions()
    )
    composed = compose(
        [
            load_profile(profile, workspace=repo_root)
            for profile in resolve_enabled_profiles(repo_root)
        ]
    )
    blocks = tuple(
        LegacyItem(kind="render_block", key=f"block:{block.id}", prose=block.body_text)
        for block in composed.render_blocks
    )
    return (*fields, *blocks, *_root_prose(repo_root))


def plan_legacy_migration(repo_root: Path, *, home: Path | None = None) -> LegacyMigration:
    """Join the inventory to the operator's dispositions in ``.ea/rules.yaml``.

    Args:
        repo_root: The repository root holding ``.ea/rules.yaml``.
        home: The directory holding the ``.eawf`` home that registers a
            workspace; ``None`` for the user's home directory.

    Returns:
        The joined migration; :attr:`LegacyMigration.complete` says whether
        every item is disposed exactly once.

    Raises:
        RuleSourceError: When the rule source fails to load.
        RuleCompileError: When the rules fail compilation.
        LegacyMigrationError: When the profile model and its disposition
            table disagree.
    """
    from eawf.platform.rules.render import builtin_rule_provider

    source = load_rule_source(repo_root)
    graph = compile_rule_graph(
        repo_root, builtin_rules=builtin_rule_provider(select_rule_modules(source)), home=home
    )
    dispositions = source.legacy
    inventory = legacy_inventory(repo_root)
    fields = profile_field_dispositions()
    counts = Counter(disposition.item for disposition in dispositions)
    by_key = {disposition.item: disposition for disposition in dispositions}
    keys = {item.key for item in inventory}
    entries = tuple(
        LegacyEntry(
            item=item,
            disposition=(
                LegacyDisposition(item=item.key, disposition=fields[item.key.split(":", 1)[1]])
                if item.kind == "profile_field"
                else by_key.get(item.key)
            ),
            review_candidates=_review_candidates(item.prose, graph),
        )
        for item in inventory
    )
    field_keys = {item.key for item in inventory if item.kind == "profile_field"}
    migration = LegacyMigration(
        entries=entries,
        unknown=tuple(sorted(set(counts) - keys)),
        repeated=tuple(sorted(key for key, n in counts.items() if n > 1 or key in field_keys)),
        unresolved=unresolved_legacy_obligations(dispositions, graph),
    )
    logger.info(
        f"legacy migration planned items={len(entries)} undisposed={len(migration.undisposed)} "
        f"unknown={len(migration.unknown)} repeated={len(migration.repeated)} "
        f"unresolved={len(migration.unresolved)}"
    )
    return migration


def unresolved_legacy_obligations(
    dispositions: Iterable[LegacyDisposition], graph: RuleGraph
) -> tuple[str, ...]:
    """Name the obligations dispositions hand content to that the graph lacks.

    Args:
        dispositions: The operator's legacy dispositions.
        graph: The effective graph.

    Returns:
        ``item -> obligation`` lines, in disposition order.
    """
    owned = {rule.record.obligation_id for rule in graph.rules}
    return tuple(
        f"{disposition.item} -> {disposition.obligation_id}"
        for disposition in dispositions
        if disposition.disposition in OBLIGATION_DISPOSITIONS
        and disposition.obligation_id not in owned
    )


def _root_prose(repo_root: Path) -> tuple[LegacyItem, ...]:
    """Inventory hand-written text in a root file no renderer stamped.

    Args:
        repo_root: The repository root.

    Returns:
        One uncertified item holding the text outside managed regions, or
        nothing when the card is absent, generated or regions only.
    """
    from eawf.platform.rules.render import CARD_TARGET, classify_projection
    from eawf.surfaces.render.regions import RegionParseError, find_regions

    path = repo_root / CARD_TARGET
    if classify_projection(path) != "operator_owned":
        return ()
    text = path.read_text(encoding="utf-8")
    try:
        spans = [region.span for region in find_regions(text)]
    except RegionParseError:
        spans = []
    cursor = 0
    parts: list[str] = []
    for start, end in spans:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    prose = "".join(parts).strip()
    return (LegacyItem(kind="root_prose", key=f"root:{CARD_TARGET}", prose=prose, certified=False),)


def _review_candidates(prose: str, graph: RuleGraph) -> tuple[str, ...]:
    """List rules whose instruction words a legacy text mostly contains.

    Args:
        prose: The legacy text.
        graph: The effective graph.

    Returns:
        Sorted rule identifiers at or above :data:`REVIEW_CANDIDATE_FLOOR`.
    """
    words = frozenset(_WORD.findall(prose.casefold()))
    if not words:
        return ()
    candidates: list[str] = []
    for rule in graph.rules:
        target = frozenset(_WORD.findall(rule.record.instruction.casefold()))
        if target and len(target & words) >= REVIEW_CANDIDATE_FLOOR * len(target):
            candidates.append(rule.record.rule_id)
    return tuple(sorted(candidates))


__all__ = [
    "PER_BLOCK",
    "PROFILE_FIELD_DISPOSITIONS",
    "REVIEW_CANDIDATE_FLOOR",
    "LegacyEntry",
    "LegacyItem",
    "LegacyItemKind",
    "LegacyMigration",
    "LegacyMigrationError",
    "legacy_inventory",
    "plan_legacy_migration",
    "profile_field_dispositions",
    "unresolved_legacy_obligations",
]
