"""Assemble every shipped skill page from the six-slot prompt chassis.

Every page is built from the same ordered slots, and the order is normative:
authority before task, task before method, method before output.

1. Authority - rendered from the catalog entry's audience and effects
   boundary, never from prose, so a body cannot add or widen a grant.
2. Context - what the invocation resolves before acting.
3. Task - the one action or bounded procedure, with the catalog grammar.
4. Method - the ordered steps.
4b. Applicable rules - the obligations the rule graph holds for the skill's
   activity and role selectors; semantic obligations only.
5. Constraints - what makes the result invalid, and the stop condition.
6. Output - the typed report and its closed terminal outcomes.

A page is checked before it ships: a missing or reordered slot, stale
grammar, a retired skill named as an invocation, or an option the skill's
grammar does not declare refuses packaging instead of shipping.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Final

from eawf.platform.rules.records import RuleRecord
from eawf.workflow.skills.bodies.prompts import SkillPrompt, skill_prompt
from eawf.workflow.skills.catalog import SKILL_CATALOG, SkillCatalogEntry

logger = logging.getLogger(__name__)

#: The slot headings of every page, in the order they must appear.
CHASSIS_SLOTS: Final[tuple[str, ...]] = (
    "1. Authority",
    "2. Context",
    "3. Task",
    "4. Method",
    "4b. Applicable rules",
    "5. Constraints",
    "6. Output",
)

_RULES_SLOT: Final[str] = "4b. Applicable rules"

#: Text a page must never carry: refusal codes of the pre-catalog lifecycle
#: bodies, which describe engine internals rather than the invocation grammar.
STALE_PAGE_TOKENS: Final[tuple[str, ...]] = ("integration_request_unnamed",)

#: Options every skill accepts whatever its own grammar declares.
UNIVERSAL_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--output", "--expected-revision", "--idempotency-key", "--args-json", "--dry-run"}
)

#: Outcomes that mean "stopped for a reason", which the stop condition names.
_STOP_OUTCOMES: Final[tuple[str, ...]] = ("blocked", "needs_operator", "paused")

_FORCE_ORDER: Final[dict[str, int]] = {"must": 0, "should": 1}
_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^## (.+)$", re.MULTILINE)
_OPTION_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")


class SkillPageError(ValueError):
    """A rendered skill page breaks the chassis; the message lists every finding."""


def applicable_rules(prompt: SkillPrompt, records: Iterable[RuleRecord]) -> tuple[RuleRecord, ...]:
    """Select the rules slot 4b renders for *prompt*.

    A rule applies when one of its activities or roles is among the prompt's
    selectors and it states an obligation (``must`` or ``should``). An
    unscoped rule binds every session and is already carried by the
    repository policy, so it is not repeated on a page; an ``information``
    rule obliges nothing, so it stays in the policy.

    Args:
        prompt: The skill's prompt, carrying its selectors.
        records: The effective rules, in graph order.

    Returns:
        The selected rules, ``must`` before ``should``, graph order within
        one force.
    """
    activities = frozenset(prompt.activities)
    roles = frozenset(prompt.roles)
    selected = [
        record
        for record in records
        if record.force in _FORCE_ORDER
        and (
            activities & frozenset(record.scope.activities) or roles & frozenset(record.scope.roles)
        )
    ]
    return tuple(sorted(selected, key=lambda record: _FORCE_ORDER[record.force]))


def assemble_skill_page(
    entry: SkillCatalogEntry, prompt: SkillPrompt, rules: Iterable[RuleRecord]
) -> str:
    """Render one skill page through the chassis.

    Args:
        entry: The catalog entry supplying authority, grammar and output.
        prompt: The skill's prose.
        rules: The rules slot 4b renders, already selected.

    Returns:
        The page markdown: a title, the description, then one section per
        slot in :data:`CHASSIS_SLOTS` order.

    Raises:
        ValueError: *prompt* belongs to another skill than *entry*.
    """
    if prompt.skill_id != entry.skill_id:
        raise ValueError(
            f"prompt for {prompt.skill_id!r} cannot render catalog skill {entry.skill_id!r}"
        )
    slots = (
        _authority(entry),
        _context(prompt),
        _task(entry, prompt),
        _numbered(prompt.method, start=prompt.method_start),
        _rules(prompt, tuple(rules)),
        _constraints(entry, prompt),
        _output(entry, prompt),
    )
    sections = [
        f"## {heading}\n\n{body}\n" for heading, body in zip(CHASSIS_SLOTS, slots, strict=True)
    ]
    return f"# {entry.invocation_name}\n\n{entry.description}\n\n" + "\n".join(sections)


def chassis_findings(page: str, entry: SkillCatalogEntry) -> tuple[str, ...]:
    """List every way a rendered page breaks the chassis for *entry*.

    Rule text in slot 4b is owned by the rule graph, so it is exempt from
    the option and retired-name checks; stale tokens are refused anywhere.

    Args:
        page: A rendered page, with or without its frontmatter.
        entry: The catalog entry the page claims to render.

    Returns:
        One message per finding; empty when the page is sound.
    """
    findings: list[str] = []
    headings = tuple(_HEADING_RE.findall(page))
    if headings != CHASSIS_SLOTS:
        findings.append(f"slots {list(headings)} are not the chassis order {list(CHASSIS_SLOTS)}")
    findings.extend(
        f"stale grammar {token!r} is on the page" for token in STALE_PAGE_TOKENS if token in page
    )
    prose = _without_rules_slot(page)
    for row in SKILL_CATALOG.retired:
        if re.search(rf"(?<![\w./-])/{re.escape(row.skill_id)}(?![\w-])", prose):
            findings.append(f"retired skill /{row.skill_id} is named; use {row.successor_text()}")
    declared = frozenset(entry.grammar.options) | UNIVERSAL_OPTIONS
    findings.extend(
        f"option {option} is not in the {entry.invocation_name} grammar"
        for option in dict.fromkeys(_OPTION_RE.findall(prose))
        if option not in declared
    )
    return tuple(findings)


def check_skill_page(page: str, entry: SkillCatalogEntry) -> None:
    """Refuse a page that breaks the chassis.

    Args:
        page: The rendered page.
        entry: The catalog entry it renders.

    Raises:
        SkillPageError: The page has at least one :func:`chassis_findings`
            finding.
    """
    findings = chassis_findings(page, entry)
    if findings:
        raise SkillPageError(f"{entry.invocation_name} page is refused: {'; '.join(findings)}")


def shipped_skill_page(entry: SkillCatalogEntry) -> str:
    """Render the page every plugin packager ships for *entry*.

    Shipped pages are rendered without a repository, so slot 4b composes the
    rules every repository applies: the builtin core and conduct rules.

    Args:
        entry: The catalog entry.

    Returns:
        The page markdown; the packaging path refuses it through
        :func:`check_skill_page` before shipping.

    Raises:
        KeyError: No prompt is declared for the entry.
    """
    # Imported here because the carrier renderer pulls the rule compiler,
    # which a caller that only reads prompt records should not pay for.
    from eawf.platform.rules.carriers import applied_builtin_records

    prompt = skill_prompt(entry.skill_id)
    rules = applicable_rules(prompt, applied_builtin_records())
    page = assemble_skill_page(entry, prompt, rules)
    logger.debug(f"skill page rendered skill={entry.skill_id} rules={len(rules)}")
    return page


def _authority(entry: SkillCatalogEntry) -> str:
    effects = entry.effects
    if entry.audience == "user_only":
        audience = (
            "Only an authenticated operator initiates this skill. An agent may prepare evidence"
            " or recommend the invocation, but never calls it."
        )
    elif entry.audience == "agent_only":
        audience = "Only an agent initiates this skill, inside an enclosing scope."
    else:
        audience = (
            "An operator or an authorized agent may initiate this skill. Agent invocation never"
            " widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled"
            " capsule already grants every read, write, RPC, budget and external effect below."
        )
    lines = [f"- {audience}"]
    if entry.operator_only_actions:
        actions = ", ".join(f"`{action}`" for action in entry.operator_only_actions)
        lines.append(
            f"- Operator-only actions: {actions}. An agent that reaches one prepares a"
            " PendingAction and stops; it never chooses the recommended option itself."
        )
    lines.append(f"- Effects: {effects.summary}")
    if effects.rpcs:
        rpcs = ", ".join(f"`{rpc}`" for rpc in effects.rpcs)
        lines.append(
            f"- Allowed RPCs: {rpcs}. Any other RPC is denied before it reaches a handler."
        )
    else:
        lines.append("- Allowed RPCs: none. This skill calls no daemon RPC.")
    if effects.canonical_mutates:
        lines.append(
            "- Canonical state changes only through those RPCs, and every mutating call carries"
            " `--expected-revision` and `--idempotency-key`."
        )
    else:
        lines.append("- Canonical state: never mutated by this skill.")
    if effects.local_write_scope is not None:
        lines.append(
            f"- Local write root: `{effects.local_write_scope}`; nothing is written outside it."
        )
    else:
        lines.append("- Local write root: none.")
    lines.append(
        "- Executable grants come from the compiled capsule of the enclosing scope alone; nothing"
        " on this page adds or widens a tool, path, RPC, credential or external effect."
    )
    return "\n".join(lines)


def _context(prompt: SkillPrompt) -> str:
    return (
        f"{prompt.context}\n\n"
        "Resolve the subject before acting. Name every entity with its identifier and its exact"
        " current revision so staleness is detectable; a fact without a revision is a summary,"
        " not context."
    )


def _task(entry: SkillCatalogEntry, prompt: SkillPrompt) -> str:
    text = f"{prompt.task}\n\n```text\n{entry.grammar.usage}\n```"
    if entry.grammar.actions:
        actions = ", ".join(f"`{action}`" for action in entry.grammar.actions)
        text += (
            f"\n\nSelect exactly one action: {actions}. An option the selected action does not"
            " declare is refused before you start."
        )
    return text


def _numbered(items: tuple[str, ...], *, start: int) -> str:
    return "\n".join(f"{number}. {item}" for number, item in enumerate(items, start=start))


def _rules(prompt: SkillPrompt, rules: tuple[RuleRecord, ...]) -> str:
    selectors = _selector_text(prompt)
    if not rules:
        return (
            f"No rule in the effective rule graph is scoped to {selectors}; the repository"
            " policy still binds every session."
        )
    lines = [
        f"The obligations the effective rule graph holds for {selectors}. They bind what you"
        " do; they grant no capability.",
        "",
    ]
    lines += [f"- {rule.force}: **{rule.title}.** {rule.instruction}" for rule in rules]
    return "\n".join(lines)


def _selector_text(prompt: SkillPrompt) -> str:
    parts = []
    if prompt.activities:
        parts.append("activities " + ", ".join(f"`{token}`" for token in prompt.activities))
    if prompt.roles:
        parts.append("roles " + ", ".join(f"`{token}`" for token in prompt.roles))
    return " and ".join(parts) if parts else "this skill, which selects no activity or role"


def _constraints(entry: SkillCatalogEntry, prompt: SkillPrompt) -> str:
    stops = [o for o in entry.output.terminal_outcomes if o in _STOP_OUTCOMES]
    stop_text = " or ".join(f"`{outcome}`" for outcome in stops)
    stop = (
        f"Stopping is a valid outcome, not a failure: when the answer needs an operator or a"
        f" precondition fails, return {stop_text} with the reason rather than guessing."
        if stops
        else "Stopping is a valid outcome, not a failure: when the answer needs an operator,"
        " stop and say so rather than guessing."
    )
    return "\n".join(f"- {item}" for item in (*prompt.constraints, stop))


def _output(entry: SkillCatalogEntry, prompt: SkillPrompt) -> str:
    outcomes = ", ".join(f"`{outcome}`" for outcome in entry.output.terminal_outcomes)
    return (
        f"{prompt.output}\n\n"
        f"The report validates against `{entry.output.schema_name}`, and its terminal outcome is"
        f" exactly one of {outcomes}. Prose in the report is explanation, never the result."
    )


def _without_rules_slot(page: str) -> str:
    start = page.find(f"## {_RULES_SLOT}\n")
    if start == -1:
        return page
    end = page.find("\n## ", start + 1)
    return page[:start] + (page[end:] if end != -1 else "")


__all__ = [
    "CHASSIS_SLOTS",
    "STALE_PAGE_TOKENS",
    "UNIVERSAL_OPTIONS",
    "SkillPageError",
    "applicable_rules",
    "assemble_skill_page",
    "chassis_findings",
    "check_skill_page",
    "shipped_skill_page",
]
