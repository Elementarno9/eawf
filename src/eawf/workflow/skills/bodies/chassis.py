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
grammar, a retired skill named as an invocation, an option the skill's
grammar does not declare, a placeholder where prose belongs, or an epoch-1
noun refuses packaging instead of shipping.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from typing import Final, Literal, get_args, get_origin

from pydantic import BaseModel

from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.platform.rules.records import RuleRecord
from eawf.surfaces.cli.verb_effects import CLI_VERB_EFFECTS
from eawf.workflow.skills.arguments import UNIVERSAL_OPTIONS
from eawf.workflow.skills.bodies.prompts import SkillPrompt, skill_prompt
from eawf.workflow.skills.catalog import SKILL_CATALOG, SkillCatalogEntry, skill_lanes
from eawf.workflow.skills.census import retired_nouns

logger = logging.getLogger(__name__)

#: The verb an agent files an operator-only choice through, as an operator decision.
DECISION_RPC: Final = "runtime.question.open_decision"

#: The verb a reply to a decision's numbered prompt is sealed through.
ANSWER_RPC: Final = "runtime.question.answer_numbered"


def _route_commands() -> dict[str, str]:
    # Where several verbs carry a route, the one carrying the fewest routes is
    # the verb that exists to drive it; ties keep table order. A projection
    # route is left out: it is a read a mutating verb makes first, so no verb
    # carrying it performs that read.
    commands: dict[str, str] = {}
    for verb, effect in sorted(CLI_VERB_EFFECTS.items(), key=lambda item: len(item[1].routes)):
        for route in effect.routes:
            if not route.startswith("projection."):
                commands.setdefault(route, verb)
    return commands


#: The CLI verb a host session runs to drive each daemon route: a host session
#: acts through ``eawf`` commands and never calls a route by name.
_ROUTE_COMMANDS: Final[dict[str, str]] = _route_commands()


def asks_operator(entry: SkillCatalogEntry) -> bool:
    """Return whether *entry* puts a choice to the operator.

    A skill does when it declares an operator-only action or carries the
    ``ask_operator`` Run tool; either way the choice is filed as a pending
    action, so the page names the decision verbs and the catalog grants them.
    """
    return bool(entry.operator_only_actions) or SemanticToolId.ASK_OPERATOR in entry.effects.tools


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

#: Text that marks a body nobody finished writing. A deferred or placeholder
#: body must never ship, because the model reads it as the instruction.
PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:TODO|TBD|FIXME|XXX)\b|(?i:lorem ipsum|to be written|body deferred)"
)


#: Outcomes that mean "stopped for a reason", which the stop condition names.
_STOP_OUTCOMES: Final[tuple[str, ...]] = ("blocked", "needs_operator", "paused")

_FORCE_ORDER: Final[dict[str, int]] = {"must": 0, "should": 1}
_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^## (.+)$", re.MULTILINE)
_OPTION_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
#: An inline ``eawf`` command; its options belong to that CLI command, not the skill grammar.
_EAWF_COMMAND_RE: Final[re.Pattern[str]] = re.compile(r"`eawf [^`\n]*`")


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
    findings.extend(
        f"placeholder {match.group(0)!r} is on the page"
        for match in PLACEHOLDER_PATTERN.finditer(page)
    )
    findings.extend(f"epoch-1 noun {noun!r} is on the page" for noun in retired_nouns(page))
    prose = _without_rules_slot(page)
    for row in SKILL_CATALOG.retired:
        if re.search(rf"(?<![\w./-])/{re.escape(row.skill_id)}(?![\w-])", prose):
            findings.append(f"retired skill /{row.skill_id} is named; use {row.successor_text()}")
    declared = frozenset(entry.grammar.options) | UNIVERSAL_OPTIONS
    findings.extend(
        f"option {option} is not in the {entry.invocation_name} grammar"
        for option in dict.fromkeys(_OPTION_RE.findall(_EAWF_COMMAND_RE.sub("", prose)))
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
    if "agent" not in skill_lanes(entry):
        audience = (
            "Only an authenticated operator initiates this skill, by design: it is kept out of"
            " the model's reach. An agent may prepare evidence or recommend the invocation, but"
            " never calls it."
        )
    elif "operator" not in skill_lanes(entry):
        audience = "Only an agent initiates this skill, inside an enclosing scope."
    else:
        audience = (
            "An operator or an authorized agent may initiate this skill. Agent invocation never"
            " widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled"
            " capsule already grants every read, write, RPC, budget and external effect below."
        )
    lines = [f"- {audience}", f"- Operates on: {_operates_on(entry)}."]
    if asks_operator(entry):
        actions = ", ".join(f"`{action}`" for action in entry.operator_only_actions)
        lead = (
            f"- Operator-only actions: {actions}. An agent that reaches one"
            if actions
            else "- Operator choices: an agent that reaches a choice this skill puts to the"
            " operator"
        )
        lines.append(
            f"{lead} files it with `eawf question open-decision` (`{DECISION_RPC}`), shows the"
            " bound question the answer carries, and stops; it never chooses the recommended"
            " option itself. In Codex the bound question is the answer's `numbered_prompt`,"
            " printed verbatim, and the operator's reply is relayed with `eawf question answer`"
            f" (`{ANSWER_RPC}`); the console answers the same record, and the first answer wins."
        )
    lines.append(f"- Effects: {effects.summary}")
    if effects.rpcs:
        rpcs = ", ".join(
            f"`eawf {_ROUTE_COMMANDS[rpc]}` (`{rpc}`)" if rpc in _ROUTE_COMMANDS else f"`{rpc}`"
            for rpc in effects.rpcs
        )
        lines.append(
            f"- Allowed RPCs: {rpcs}. Any other RPC is denied before it reaches a handler."
            " A host session drives a route through the `eawf` command named beside it; a"
            " route with no command beside it is driven by the daemon or a native Run."
        )
    else:
        lines.append("- Allowed RPCs: none. This skill calls no daemon RPC.")
    if effects.tools:
        tools = ", ".join(f"`{tool.value}`" for tool in effects.tools)
        lines.append(
            f"- Run tools: {tools}. These exist only inside a native Run eawf dispatches; a host"
            " session has none of them and uses the `eawf` commands above instead."
        )
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


def _operates_on(entry: SkillCatalogEntry) -> str:
    # The decision verbs are how a skill asks, named on their own line, not a route to its subject.
    routes = [
        f"`eawf {_ROUTE_COMMANDS[rpc]}`" if rpc in _ROUTE_COMMANDS else f"`{rpc}`"
        for rpc in entry.effects.rpcs
        if rpc not in (DECISION_RPC, ANSWER_RPC)
    ]
    routes += [f"`eawf {verb}`" for verb in entry.effects.verbs]
    if not routes:
        return f"{entry.subject}, through no lifecycle route; this is an explicit skill contract"
    return f"{entry.subject}, through {', '.join(dict.fromkeys(routes))}"


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
        f" Check it with `eawf skill check-report {entry.invocation_name}` before returning it."
        " It carries these fields and no other; a nested field is written under its parent.\n\n"
        f"{report_fields_table(entry.output.model_for(entry.skill_id))}"
    )


#: Python types a report field may carry, as the words a page shows for them.
_SCALAR_TYPES: Final[dict[object, str]] = {str: "string", int: "integer", bool: "boolean"}


def report_fields_table(model: type[BaseModel]) -> str:
    """Render the fields a report *model* accepts as a markdown table.

    The table is generated from the model the report validates against, so a
    page lists exactly the fields the validator accepts. A field holding a
    model, or a list of models, is followed by one row per nested field, its
    path dotted under the parent (``[]`` marks a list item).

    Args:
        model: The strict report model, such as one
            :meth:`~eawf.workflow.skills.catalog.OutputSchema.model_for` returns.

    Returns:
        A table with a ``Field``, ``Type``, ``Required`` and ``Description``
        column and one row per field, in declaration order.

    Raises:
        TypeError: A field's type has no page rendering.
    """
    lines = ["| Field | Type | Required | Description |", "| --- | --- | --- | --- |"]
    lines += [
        f"| `{path}` | {kind} | {'yes' if required else 'no'} | {description} |"
        for path, kind, required, description in _field_rows(model, "")
    ]
    return "\n".join(lines)


def _field_rows(model: type[BaseModel], prefix: str) -> Iterator[tuple[str, str, bool, str]]:
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        yield path, _type_text(info.annotation), info.is_required(), info.description or ""
        origin = get_origin(info.annotation)
        item = get_args(info.annotation)[0] if origin in (tuple, list) else info.annotation
        if isinstance(item, type) and issubclass(item, BaseModel):
            yield from _field_rows(item, f"{path}[]." if origin in (tuple, list) else f"{path}.")


def _type_text(annotation: object) -> str:
    origin = get_origin(annotation)
    if origin is Literal:
        values = ", ".join(f'`"{value}"`' for value in get_args(annotation))
        return values if len(get_args(annotation)) == 1 else f"one of {values}"
    if origin in (tuple, list):
        return f"list of {_type_text(get_args(annotation)[0])}"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object"
    if annotation in _SCALAR_TYPES:
        return _SCALAR_TYPES[annotation]
    raise TypeError(f"report field type {annotation!r} has no page rendering")


def _without_rules_slot(page: str) -> str:
    start = page.find(f"## {_RULES_SLOT}\n")
    if start == -1:
        return page
    end = page.find("\n## ", start + 1)
    return page[:start] + (page[end:] if end != -1 else "")


__all__ = [
    "CHASSIS_SLOTS",
    "PLACEHOLDER_PATTERN",
    "STALE_PAGE_TOKENS",
    "UNIVERSAL_OPTIONS",
    "SkillPageError",
    "applicable_rules",
    "assemble_skill_page",
    "chassis_findings",
    "check_skill_page",
    "report_fields_table",
    "shipped_skill_page",
]
