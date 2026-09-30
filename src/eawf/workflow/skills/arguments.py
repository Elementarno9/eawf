"""One strict argument schema per skill, derived from its catalog grammar.

The grammar's usage line is the only place a skill's arguments are written.
:func:`argument_schema` parses it once into an :class:`ArgumentSchema`, and
every surface that needs the arguments reads that object: the host help is
the usage line itself, :meth:`ArgumentSchema.json_schema` is the machine
schema, :meth:`ArgumentSchema.completion` is the completion metadata, and
:func:`check_invocation` is the invocation validation. Four surfaces over one
parse cannot disagree about what a skill accepts.

:func:`check_invocation` runs before a skill's producer is instantiated, so an
unknown argument, an argument the selected action does not take, an operator
lane restriction or an undeclared action is refused before any Run starts.
A host-lane invocation arrives as the text the operator or the model typed
after the slashed name; :func:`parse_host_arguments` reads that text against
the same schema, so the host lane is validated by the same check.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from eawf.workflow.skills.catalog import Lane, SkillCatalogEntry, skill_lanes

logger = logging.getLogger(__name__)

#: Options every skill accepts whatever its own grammar declares.
UNIVERSAL_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--output", "--expected-revision", "--idempotency-key", "--args-json", "--dry-run"}
)

#: Argument keys that address the tree rather than parameterise the skill.
ADDRESSING_KEYS: Final[frozenset[str]] = frozenset({"repo_root"})

#: The argument key an action is passed under.
ACTION_KEY: Final = "action"

#: One option in a usage line: its flag, whether a placeholder follows, and a
#: trailing ``...`` when it repeats.
_OPTION_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"(--[a-z][a-z0-9-]*)((?:\s+\[?<[^>]+>(?:=<[^>]+>)?\]?)?)(\.\.\.)?"
)

RefusalCode = Literal[
    "unparseable_arguments",
    "unknown_argument",
    "action_undeclared",
    "action_incompatible",
    "lane_refused",
    "operator_only_action",
]


class ArgumentOption(BaseModel):
    """One option a skill accepts.

    Attributes:
        flag: The option as typed, ``--candidate``.
        field: The argument key it is passed under, ``candidate``.
        takes_value: Whether a value follows the flag.
        repeatable: Whether it may be given more than once.
        actions: The actions that accept it; every action when the skill
            has none of its own.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    flag: str
    field: str
    takes_value: bool
    repeatable: bool
    actions: tuple[str, ...] = ()


class ArgumentSchema(BaseModel):
    """The strict argument schema of one skill.

    Attributes:
        skill_id: The catalog id.
        usage: The usage line the schema was parsed from, which is the host
            help.
        actions: The closed action set, empty for a one-action skill.
        subject_field: The argument key of the positional subject.
        options: Every option the usage line declares, in usage order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str
    usage: str
    actions: tuple[str, ...]
    subject_field: str
    options: tuple[ArgumentOption, ...]

    def json_schema(self) -> dict[str, Any]:
        """Return the machine schema: a closed JSON Schema object of the arguments."""
        properties: dict[str, Any] = {self.subject_field: {"type": ["string", "array"]}}
        if self.actions:
            properties[ACTION_KEY] = {"enum": list(self.actions)}
        for option in self.options:
            if not option.takes_value:
                properties[option.field] = {"type": "boolean"}
            elif option.repeatable:
                properties[option.field] = {"type": "array"}
            else:
                properties[option.field] = {}
        return {
            "title": f"/{self.skill_id} arguments",
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }

    def completion(self) -> dict[str, Any]:
        """Return the completion metadata: the actions and each action's flags."""
        per_action = {
            action: [option.flag for option in self.options if action in option.actions]
            for action in self.actions
        }
        return {
            "skill": f"/{self.skill_id}",
            "actions": list(self.actions),
            "options": [option.flag for option in self.options],
            "action_options": per_action,
        }


class InvocationRefusedError(ValueError):
    """An invocation the skill's schema or lanes refuse, before any Run starts.

    Attributes:
        code: Why it was refused.
        skill: The slashed skill name.
    """

    def __init__(self, *, code: RefusalCode, skill: str, detail: str) -> None:
        """Keep the routable code beside the sentence.

        Args:
            code: Why the invocation was refused.
            skill: The slashed skill name.
            detail: One sentence naming what was refused.
        """
        self.code = code
        self.skill = skill
        super().__init__(f"{skill} refused the invocation: {code}: {detail}")


def _field(flag: str) -> str:
    return flag.removeprefix("--").replace("-", "_")


def argument_schema(entry: SkillCatalogEntry) -> ArgumentSchema:
    """Parse *entry*'s usage line into its strict argument schema.

    Args:
        entry: A catalog entry.

    Returns:
        The schema every argument surface of the skill reads.
    """
    grammar = entry.grammar
    parsed: dict[str, ArgumentOption] = {}
    for match in _OPTION_TOKEN.finditer(grammar.usage):
        flag = match.group(1)
        if flag in parsed:
            continue
        actions = tuple(a for a in grammar.actions if flag in grammar.options_for(a))
        parsed[flag] = ArgumentOption(
            flag=flag,
            field=_field(flag),
            takes_value=bool(match.group(2)),
            repeatable=match.group(3) is not None,
            actions=actions,
        )
    return ArgumentSchema(
        skill_id=entry.skill_id,
        usage=grammar.usage,
        actions=grammar.actions,
        subject_field=grammar.subject_field,
        options=tuple(parsed.values()),
    )


def _action_required(schema: ArgumentSchema) -> bool:
    """Return whether the usage line makes the action a required first word."""
    words = schema.usage.split(maxsplit=2)
    return len(words) > 1 and words[1].startswith("<") and bool(schema.actions)


def parse_host_arguments(entry: SkillCatalogEntry, text: str) -> dict[str, Any]:
    """Read the argument text of a host-lane invocation into an argument mapping.

    Options become fields as the schema spells them; the first bare word is the
    action when it names one, or when the usage line requires an action, and the
    remaining bare words are the subject. The mapping is what
    :func:`check_invocation` then judges, so an unknown option survives the parse
    to be refused there.

    Args:
        entry: The resolved catalog entry.
        text: What followed the slashed name.

    Returns:
        The argument mapping.

    Raises:
        InvocationRefusedError: The text does not split into words.
    """
    schema = argument_schema(entry)
    try:
        words = shlex.split(text)
    except ValueError as exc:
        raise InvocationRefusedError(
            code="unparseable_arguments", skill=entry.invocation_name, detail=str(exc)
        ) from exc
    by_flag = {option.flag: option for option in schema.options}
    args: dict[str, Any] = {}
    subject: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if not word.startswith("--"):
            first = not subject and ACTION_KEY not in args
            if first and (word in schema.actions or _action_required(schema)):
                args[ACTION_KEY] = word
            else:
                subject.append(word)
            continue
        flag, has_value, value = word.partition("=")
        option = by_flag.get(flag)
        takes_value = option.takes_value if option is not None else has_value == "="
        if takes_value and not has_value and index < len(words):
            value = words[index]
            index += 1
        given: Any = value if takes_value else True
        if option is not None and option.repeatable:
            args.setdefault(option.field, []).append(given)
        else:
            args[_field(flag)] = given
    if subject:
        args[schema.subject_field] = subject
    return args


def _given(value: object) -> bool:
    """Return whether an argument value was actually supplied."""
    return value is not None and value is not False and value != () and value != []


def check_invocation(entry: SkillCatalogEntry, args: Mapping[str, Any], *, lane: Lane) -> None:
    """Refuse an invocation the skill's schema or lanes do not admit.

    Args:
        entry: The resolved catalog entry.
        args: The argument mapping the invocation carries.
        lane: Who is invoking: ``operator`` or ``agent``.

    Raises:
        InvocationRefusedError: The lane may not invoke the skill or the
            selected action, the action is not declared, an argument is
            unknown, or an argument is not one the selected action takes.
    """
    skill = entry.invocation_name
    if lane not in skill_lanes(entry):
        raise InvocationRefusedError(
            code="lane_refused", skill=skill, detail=f"the {lane} lane may not invoke it"
        )
    schema = argument_schema(entry)
    action = args.get(ACTION_KEY)
    if action is not None and action not in schema.actions:
        raise InvocationRefusedError(
            code="action_undeclared",
            skill=skill,
            detail=f"action {action!r} is not one of {list(schema.actions)}",
        )
    if lane == "agent" and action in entry.operator_only_actions:
        raise InvocationRefusedError(
            code="operator_only_action",
            skill=skill,
            detail=f"action {action!r} is operator-only; prepare it and stop",
        )
    by_field = {option.field: option for option in schema.options}
    universal = {_field(flag) for flag in UNIVERSAL_OPTIONS}
    positional = {schema.subject_field} | ({ACTION_KEY} if schema.actions else set())
    for key, value in args.items():
        if key in positional or key in ADDRESSING_KEYS or key in universal:
            continue
        option = by_field.get(key)
        if option is None:
            raise InvocationRefusedError(
                code="unknown_argument", skill=skill, detail=f"{key!r} is not in its grammar"
            )
        if action is not None and _given(value) and action not in option.actions:
            raise InvocationRefusedError(
                code="action_incompatible",
                skill=skill,
                detail=f"{option.flag} is not an argument of action {action!r}",
            )
    logger.debug(f"check_invocation skill={skill} lane={lane} action={action!r}")


__all__ = [
    "ACTION_KEY",
    "ADDRESSING_KEYS",
    "UNIVERSAL_OPTIONS",
    "ArgumentOption",
    "ArgumentSchema",
    "InvocationRefusedError",
    "argument_schema",
    "check_invocation",
    "parse_host_arguments",
]
