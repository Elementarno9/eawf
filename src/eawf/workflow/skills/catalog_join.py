"""Join the skill catalog to the verb catalog before anything is generated.

The skill catalog names the routes and verbs each skill drives; the verb
catalog says which routes exist and what each verb does. Generation joins the
two first, so a skill that names a route the daemon never registered, a route
no verb carries, a verb the tree does not have, or a mutating route behind a
read-only contract refuses to ship instead of failing at its first call.

Only lifecycle skills must be carried by verbs: a read-only, Campaign,
local-prototype or engineering procedure stays an explicit skill contract, so
its routes need only be registered, never mirrored by a CLI verb.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Final

from eawf.surfaces.cli.verb_catalog import VerbCatalog, VerbEntry
from eawf.workflow.skills.catalog import SkillCatalog, SkillCatalogEntry

logger = logging.getLogger(__name__)

#: The verb-contract flags a lifecycle skill's grammar may forward to a verb.
CONTRACT_OPTIONS: Final[tuple[str, ...]] = (
    "--expected-revision",
    "--idempotency-key",
    "--from-spec",
)


class CatalogJoinError(ValueError):
    """A skill names a missing or incompatible route; the message lists every finding."""


def _accepts(verb: VerbEntry, option: str) -> bool:
    """Return whether *verb* declares the parameter a skill's *option* forwards to."""
    field = option.removeprefix("--").replace("-", "_")
    return any(option in param.flags or param.name == field for param in verb.parameters)


def _entry_findings(
    entry: SkillCatalogEntry, verbs: VerbCatalog, registered: frozenset[str]
) -> list[str]:
    """Return every join finding of one skill."""
    name = entry.invocation_name
    effects = entry.effects
    findings = [
        f"{name} names unregistered route {rpc!r}" for rpc in effects.rpcs if rpc not in registered
    ]
    for verb in effects.verbs:
        row = verbs.entry(verb)
        if row is None:
            findings.append(f"{name} names verb {verb!r}, which the verb catalog does not carry")
        elif not effects.canonical_mutates and row.effect_class != "read":
            findings.append(f"{name} is read-only but drives {row.effect_class} verb {verb!r}")
    if entry.skill_class == "lifecycle":
        findings.extend(_lifecycle_findings(entry, verbs))
    return findings


def _lifecycle_findings(entry: SkillCatalogEntry, verbs: VerbCatalog) -> list[str]:
    """Return the findings only a lifecycle skill can have: routes no verb carries."""
    name = entry.invocation_name
    effects = entry.effects
    findings: list[str] = []
    if not effects.rpcs and not effects.verbs:
        findings.append(f"{name} is a lifecycle skill that names no route")
    carriers: list[VerbEntry] = []
    for rpc in effects.rpcs:
        carried = verbs.route_verbs(rpc)
        carriers.extend(carried)
        if not carried:
            findings.append(f"{name} names route {rpc!r}, which no verb carries")
        elif not effects.canonical_mutates and all(v.effect_class != "read" for v in carried):
            findings.append(f"{name} is read-only but names mutating route {rpc!r}")
    carriers.extend(row for verb in effects.verbs if (row := verbs.entry(verb)) is not None)
    findings.extend(
        f"{name} forwards {option}, which none of its verbs accepts"
        for option in CONTRACT_OPTIONS
        if option in entry.grammar.options and not any(_accepts(v, option) for v in carriers)
    )
    return findings


def join_findings(
    catalog: SkillCatalog, verbs: VerbCatalog, registered: Iterable[str]
) -> tuple[str, ...]:
    """List every way *catalog* fails to join *verbs*.

    Args:
        catalog: The skill catalog.
        verbs: The verb catalog.
        registered: Every route the daemon registers.

    Returns:
        One message per finding; empty when every skill joins.
    """
    routes = frozenset(registered)
    return tuple(
        finding for entry in catalog.entries for finding in _entry_findings(entry, verbs, routes)
    )


def require_joined(catalog: SkillCatalog) -> None:
    """Refuse generation when *catalog* does not join the installed verb catalog.

    Args:
        catalog: The skill catalog about to be rendered.

    Raises:
        CatalogJoinError: At least one :func:`join_findings` finding.
    """
    from eawf.runtime.daemon.methods import registered_methods
    from eawf.surfaces.cli.verb_catalog import verb_catalog

    findings = join_findings(catalog, verb_catalog(), registered_methods())
    if findings:
        raise CatalogJoinError(f"the skill catalog does not join: {'; '.join(findings)}")
    logger.debug(f"require_joined skills={len(catalog.entries)}")


__all__ = ["CONTRACT_OPTIONS", "CatalogJoinError", "join_findings", "require_joined"]
