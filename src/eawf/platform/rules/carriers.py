"""Render the role carriers: one rule-owned projection per role the graph scopes.

A role carrier renders every rule scoped to one agent role in the skill file
format, so a session can load the rules of a role by name; it is not a
catalog skill. So it declares no invocation grammar, no effects boundary and
no output schema, it is marked non-user-invocable, and it never suppresses
model invocation.

The always-loaded skill listing pays each entry's description. A carrier's
description is its own name, so the listing pays a name and nothing more; an
absent description would make the runtime fall back to the first paragraph
of the body.

A carrier is emitted only for a role at least one rule in the effective
graph is scoped to. It is rendered by the same transaction as the other
projections, from the policy graph, and opens its body with the same kind of
stamp: the graph digest it was rendered from and the digest of the text
below the stamp, so a hand edit and a lagging carrier are both detectable
from the file alone.

Agent definitions and dispatch prompts are rendered without a repository
graph and must not depend on a gitignored file a fresh clone, a worktree or
a repository without ``.ea/rules.yaml`` lacks, so they embed the carrier
body :func:`builtin_carrier_body` renders from the builtin rules every
repository applies; it is the same text the carrier of a repository whose
graph adds no role rule holds below its stamp.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
from collections.abc import Sequence
from typing import Final

from pydantic import model_validator

from eawf.platform.rules.compile import RuleGraph
from eawf.platform.rules.conduct import load_conduct_rules
from eawf.platform.rules.loader import RULE_SOURCE_PATH
from eawf.platform.rules.modules import builtin_rule_modules
from eawf.platform.rules.records import RuleModel, RuleRecord, SelectorToken

logger = logging.getLogger(__name__)

#: Where carriers are written, relative to the repository root: the project
#: skill directory a Claude session loads skills from by name.
CARRIER_DIRECTORY: Final[str] = ".claude/skills"

#: The prefix of every carrier name, which keeps carriers out of the
#: catalog's namespace.
CARRIER_NAME_PREFIX: Final[str] = "eawf-rules-"

_CARRIER_STAMP: Final[re.Pattern[str]] = re.compile(
    r"^<!-- eawf:projection kind=carrier graph=(?P<graph>sha256:[0-9a-f]{64}) "
    r"body=(?P<body>sha256:[0-9a-f]{64}) role=(?P<role>[a-z][a-z0-9_-]{0,63}) .*-->$"
)

_SECTIONS: Final[tuple[tuple[str, str, str], ...]] = (
    ("must", "Obligations", "Each rule below binds every session dispatched in this role."),
    (
        "should",
        "Guidance",
        "Follow each rule below unless a stated reason in the task overrides it.",
    ),
    ("information", "Information", "Context that shapes how the rules above apply."),
)


class RenderedCarrier(RuleModel):
    """One role carrier ready to write.

    Attributes:
        role: The role the carrier delivers rules to.
        name: The skill name the carrier registers under.
        target: Repository-relative path of the carrier.
        text: The complete file content, frontmatter and stamp included.
        rule_ids: The rules the carrier delivers, in render order.
    """

    role: SelectorToken
    name: str
    target: str
    text: str
    rule_ids: tuple[str, ...]

    @model_validator(mode="after")
    def _name_and_target_follow_role(self) -> RenderedCarrier:
        """Keep the name and the target derived from the role.

        Returns:
            The validated carrier.

        Raises:
            ValueError: When the name or the target is not the role's.
        """
        if self.name != carrier_name(self.role) or self.target != carrier_target(self.role):
            raise ValueError(f"carrier name and target must derive from role {self.role!r}")
        return self


def carrier_name(role: str) -> str:
    """Return the skill name a role's carrier registers under.

    Args:
        role: The role selector token.

    Returns:
        ``eawf-rules-<role>``.
    """
    return f"{CARRIER_NAME_PREFIX}{role}"


def carrier_target(role: str) -> str:
    """Return the repository-relative path of a role's carrier.

    Args:
        role: The role selector token.

    Returns:
        ``.claude/skills/eawf-rules-<role>/SKILL.md``.
    """
    return f"{CARRIER_DIRECTORY}/{carrier_name(role)}/SKILL.md"


def carrier_roles(graph: RuleGraph) -> tuple[str, ...]:
    """Name every role at least one rule in ``graph`` is scoped to.

    Args:
        graph: The effective graph.

    Returns:
        The roles, sorted.
    """
    return tuple(sorted({role for rule in graph.rules for role in rule.record.scope.roles}))


@functools.cache
def _applied_builtin_records() -> tuple[RuleRecord, ...]:
    """Return the builtin records that bind every repository, in graph order.

    Core modules and the conduct module apply whether or not a repository
    selects them. Every one of them is a builtin record, so the effective
    graph orders them by identifier and revision.

    Returns:
        The records, sorted the way the effective graph sorts them.
    """
    records = [
        *(
            record
            for module in builtin_rule_modules().values()
            if module.document.kind == "core"
            for record in module.records
        ),
        *load_conduct_rules(),
    ]
    return tuple(sorted(records, key=lambda record: (record.rule_id, record.revision)))


@functools.cache
def builtin_carrier_roles() -> tuple[str, ...]:
    """Name every role a rule that binds every repository is scoped to.

    Returns:
        The roles, sorted; each has a :func:`builtin_carrier_body`.
    """
    records = _applied_builtin_records()
    return tuple(sorted({role for record in records for role in record.scope.roles}))


@functools.cache
def builtin_carrier_body(role: str) -> str | None:
    """Render the carrier body of *role* from the rules every repository applies.

    Agent definitions and dispatch prompts embed this text, so it depends
    on the installed package alone and never on a rendered file.

    Args:
        role: The agent role.

    Returns:
        The carrier text below the stamp, or ``None`` when no builtin rule
        is scoped to *role*.
    """
    if role not in builtin_carrier_roles():
        return None
    return _carrier_body(role, _applied_builtin_records())[0]


def render_role_carriers(graph: RuleGraph) -> tuple[RenderedCarrier, ...]:
    """Render one carrier per role the effective graph scopes rules to.

    Args:
        graph: The effective graph the policy projection renders from.

    Returns:
        One carrier per role in :func:`carrier_roles` order; empty when no
        rule is scoped to a role.
    """
    carriers = tuple(_render_carrier(role, graph) for role in carrier_roles(graph))
    logger.debug(f"role carriers rendered roles={[carrier.role for carrier in carriers]}")
    return carriers


def carrier_stamp_matches_body(text: str) -> bool | None:
    """Check a carrier's frontmatter and stamp against the text they describe.

    Args:
        text: The complete file content.

    Returns:
        ``None`` when ``text`` is not a carrier, else whether the frontmatter
        is the one the stamped role renders and the stamped body digest
        equals the digest of the text below the stamp.
    """
    head, stamp, body = _split_carrier(text)
    if stamp is None:
        return None
    match = _CARRIER_STAMP.fullmatch(stamp)
    if match is None:
        return None
    return head == _frontmatter(match.group("role")) and _sha256_text(body) == match.group("body")


def carrier_stamp_line(text: str) -> str | None:
    """Return the stamp line of a carrier, below its frontmatter.

    Args:
        text: The complete file content.

    Returns:
        The stamp line without its newline, or ``None`` when ``text`` opens
        with no frontmatter.
    """
    return _split_carrier(text)[1]


def _render_carrier(role: str, graph: RuleGraph) -> RenderedCarrier:
    """Render the carrier of one role.

    Args:
        role: The role.
        graph: The effective graph.

    Returns:
        The carrier.
    """
    body, rule_ids = _carrier_body(role, tuple(rule.record for rule in graph.rules))
    stamp = (
        f"<!-- eawf:projection kind=carrier graph={graph.digest} body={_sha256_text(body)} "
        f"role={role} generated from {RULE_SOURCE_PATH.as_posix()} by eawf sync; "
        f"a hand edit fails validation -->"
    )
    return RenderedCarrier(
        role=role,
        name=carrier_name(role),
        target=carrier_target(role),
        text=f"{_frontmatter(role)}{stamp}\n{body}",
        rule_ids=rule_ids,
    )


def _carrier_body(role: str, records: Sequence[RuleRecord]) -> tuple[str, tuple[str, ...]]:
    """Render the text a carrier holds below its stamp.

    Args:
        role: The role.
        records: Effective rules in graph order.

    Returns:
        The body and the identifiers of the rules it renders, in order.
    """
    scoped = tuple(record for record in records if role in record.scope.roles)
    lines = [
        f"# Rules for the {role} role",
        "",
        f"These rules bind every session dispatched as `{role}`, in addition to the "
        f"repository policy.",
        "",
    ]
    rendered: list[str] = []
    for force, heading, intro in _SECTIONS:
        rules = tuple(record for record in scoped if record.force == force)
        if not rules:
            continue
        lines += [f"## {heading}", "", intro, ""]
        lines += [_rule_line(record) for record in rules]
        lines.append("")
        rendered.extend(record.rule_id for record in rules)
    return "\n".join(lines), tuple(rendered)


def _frontmatter(role: str) -> str:
    """Render a carrier's skill frontmatter.

    Args:
        role: The role.

    Returns:
        The frontmatter block, newline terminated.
    """
    name = carrier_name(role)
    return f"---\nname: {name}\ndescription: {name}\nuser-invocable: false\n---\n"


def _split_carrier(text: str) -> tuple[str, str | None, str]:
    """Split a carrier into its frontmatter, its stamp line and its body.

    Args:
        text: The complete file content.

    Returns:
        ``(frontmatter, stamp, body)``; ``stamp`` is ``None`` when ``text``
        opens with no closed frontmatter block.
    """
    if not text.startswith("---\n"):
        return "", None, text
    end = text.find("\n---\n", 3)
    if end == -1:
        return "", None, text
    head_end = end + len("\n---\n")
    stamp, _, body = text[head_end:].partition("\n")
    return text[:head_end], stamp, body


def _rule_line(record: RuleRecord) -> str:
    """Render one rule as a single list line.

    Args:
        record: The rule.

    Returns:
        ``- **Title.** instruction`` plus the procedure.
    """
    procedure = f" Procedure: `{record.procedure_ref}`." if record.procedure_ref else ""
    return f"- **{record.title}.** {record.instruction}{procedure}"


def _sha256_text(text: str) -> str:
    """Digest UTF-8 text.

    Args:
        text: The text.

    Returns:
        ``sha256:`` followed by the hex digest.
    """
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


__all__ = [
    "CARRIER_DIRECTORY",
    "CARRIER_NAME_PREFIX",
    "RenderedCarrier",
    "builtin_carrier_body",
    "builtin_carrier_roles",
    "carrier_name",
    "carrier_roles",
    "carrier_stamp_line",
    "carrier_stamp_matches_body",
    "carrier_target",
    "render_role_carriers",
]
