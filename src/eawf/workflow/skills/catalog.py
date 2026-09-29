"""The closed skill catalog: every presented skill, its grammar, effects and output.

The catalog is data. Each :class:`SkillCatalogEntry` declares one operator intent
with its invocation grammar, its effects boundary and its typed output schema,
and the plugin packagers and ``eawf skill list`` project the shipped surface from
it rather than from a hand-maintained list. The set is closed: a name outside it
is either retired, in which case :func:`resolve_skill` refuses it and names the
successor, or unknown. Retired names are never aliased onto their successors,
because an alias keeps the old noun alive in every operator's muscle memory and
the successor's grammar is not a superset of the retired one.

No surface states how many skills ship; any displayed cardinality is derived
from :data:`SKILL_CATALOG` at render time.

One invocation-audience map, :func:`skill_lanes`, decides who may invoke a
skill: the host menus, the generated help, the agent callable catalog and the
invocation check all filter from it, so a skill cannot be hidden in one lane
and callable in another.
"""

from __future__ import annotations

import logging
import re
from functools import cache
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from eawf.kernel.economics.prompt_budget import BudgetClassId
from eawf.kernel.runtime.semantic import SemanticToolId

if TYPE_CHECKING:
    from eawf.surfaces.render.skills.render import SkillSpec

logger = logging.getLogger(__name__)

SkillClass = Literal["lifecycle", "investigation", "knowledge", "engineering"]
InvocationAudience = Literal["user_only", "agent_only", "both"]
Lane = Literal["operator", "agent"]

#: Who may invoke a skill of each audience.
_AUDIENCE_LANES: dict[InvocationAudience, frozenset[Lane]] = {
    "user_only": frozenset({"operator"}),
    "agent_only": frozenset({"agent"}),
    "both": frozenset({"operator", "agent"}),
}

_SKILL_ID_PATTERN = r"^[a-z][a-z0-9-]{1,31}$"
_ACTION_PATTERN = r"^[a-z][a-z0-9-]*$"
_OUTCOME_PATTERN = r"^[a-z][a-z0-9_]*$"
_OPTION_RE = re.compile(r"--[a-z][a-z0-9-]*")


class InvocationGrammar(BaseModel):
    """The exhaustive invocation line of one skill and its closed action set.

    ``usage`` is the single source of the grammar: the option set and the
    one-line argument hint are derived from it so the two can never drift.

    Attributes:
        usage: The whole invocation line.
        actions: The closed action set, empty for a skill with one action.
        subject_field: The argument key the positional subject is passed
            under.
        action_options: The options each named action accepts. An action
            absent from the map accepts every option of the usage line.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    usage: str = Field(min_length=3)
    actions: tuple[str, ...] = ()
    subject_field: str = Field(default="subject_ref", pattern=r"^[a-z][a-z0-9_]*$")
    action_options: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _actions_are_declared_in_usage(self) -> InvocationGrammar:
        if len(set(self.actions)) != len(self.actions):
            raise ValueError(f"duplicate action in {self.actions!r}")
        for action in self.actions:
            if re.fullmatch(_ACTION_PATTERN, action) is None:
                raise ValueError(f"action {action!r} does not match {_ACTION_PATTERN}")
            if action not in self.usage:
                raise ValueError(f"action {action!r} is not in usage {self.usage!r}")
        stray = set(self.action_options) - set(self.actions)
        if stray:
            raise ValueError(f"action_options names undeclared actions {sorted(stray)}")
        for action, options in self.action_options.items():
            unknown = set(options) - set(self.options)
            if unknown:
                raise ValueError(f"action {action!r} names undeclared options {sorted(unknown)}")
        return self

    @property
    def options(self) -> tuple[str, ...]:
        """Return every long option the usage line accepts, in first-use order."""
        return tuple(dict.fromkeys(_OPTION_RE.findall(self.usage)))

    @property
    def argument_hint(self) -> str:
        """Return the usage line without its leading ``/<skill>`` token."""
        _, _, rest = self.usage.partition(" ")
        return rest

    def options_for(self, action: str | None) -> tuple[str, ...]:
        """Return the options *action* accepts, in usage order.

        Args:
            action: A declared action, or ``None`` for a skill with one action.

        Returns:
            The action's declared options, or every usage option when the
            action names none of its own.
        """
        if action is None or action not in self.action_options:
            return self.options
        allowed = set(self.action_options[action])
        return tuple(option for option in self.options if option in allowed)


class EffectsBoundary(BaseModel):
    """What one skill may touch: its routes, verbs, tools and local write root.

    An RPC outside ``rpcs`` is denied before it reaches a handler, and a skill
    with no ``local_write_scope`` may not write local files at all.

    Attributes:
        summary: One sentence naming the boundary.
        rpcs: The daemon routes the skill calls directly.
        verbs: The CLI verbs the skill runs, by verb-catalog path.
        tools: The semantic tools a Run of the skill calls.
        canonical_mutates: Whether any of them changes canonical state.
        local_write_scope: The one repo-relative root it may write under.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1)
    rpcs: tuple[str, ...] = ()
    verbs: tuple[str, ...] = ()
    tools: tuple[SemanticToolId, ...] = ()
    canonical_mutates: bool
    local_write_scope: str | None = None

    @model_validator(mode="after")
    def _write_scope_is_repo_relative(self) -> EffectsBoundary:
        scope = self.local_write_scope
        if scope is not None and (scope.startswith("/") or ".." in scope.split("/")):
            raise ValueError(f"local_write_scope {scope!r} must be a repo-relative path")
        if len(set(self.rpcs)) != len(self.rpcs):
            raise ValueError(f"duplicate rpc in {self.rpcs!r}")
        if len(set(self.verbs)) != len(self.verbs):
            raise ValueError(f"duplicate verb in {self.verbs!r}")
        return self


class SkillReport(BaseModel):
    """Common shape of every terminal skill report.

    Each catalog entry narrows ``skill_id`` and ``outcome`` to its own literal
    values through :meth:`OutputSchema.model_for`; prose in ``summary`` is
    explanation, never the result.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str
    outcome: str
    summary: str = ""


class NotCovered(BaseModel):
    """One item a sweep did not cover, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class CoverageBlock(BaseModel):
    """What a sweep covered and what it did not, as a machine-checkable answer.

    A report that says it looked everywhere carries nothing to check; one that
    lists what it did not reach can be re-asked about exactly that remainder.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    covered: tuple[str, ...] = Field(min_length=1)
    not_covered: tuple[NotCovered, ...]


class OutputSchema(BaseModel):
    """The typed terminal report a skill invocation must produce.

    Attributes:
        schema_name: The report model's name.
        terminal_outcomes: The closed set of ways an invocation ends.
        coverage: Whether the report must carry a :class:`CoverageBlock`,
            which every sweep-shaped skill does.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: str = Field(pattern=r"^[A-Z][A-Za-z]+Report$")
    terminal_outcomes: tuple[str, ...] = Field(min_length=1)
    coverage: bool = False

    @model_validator(mode="after")
    def _outcomes_are_unique_snake_case(self) -> OutputSchema:
        if len(set(self.terminal_outcomes)) != len(self.terminal_outcomes):
            raise ValueError(f"duplicate terminal outcome in {self.terminal_outcomes!r}")
        for outcome in self.terminal_outcomes:
            if re.fullmatch(_OUTCOME_PATTERN, outcome) is None:
                raise ValueError(f"terminal outcome {outcome!r} is not snake_case")
        return self

    def model_for(self, skill_id: str) -> type[SkillReport]:
        """Return the strict report model narrowed to *skill_id* and these outcomes.

        Args:
            skill_id: The catalog id the report must carry.

        Returns:
            A :class:`SkillReport` subclass named ``schema_name`` whose
            ``skill_id`` and ``outcome`` accept only the declared literals,
            and which requires a coverage block when :attr:`coverage` is set.
        """
        return _report_model(self.schema_name, skill_id, self.terminal_outcomes, self.coverage)


@cache
def _report_model(
    schema_name: str, skill_id: str, outcomes: tuple[str, ...], coverage: bool
) -> type[SkillReport]:
    # Literal[...] over a runtime tuple is the documented way to build a closed
    # enum field on a generated model; mypy cannot follow it, pydantic can.
    outcome_type = Literal[outcomes]  # type: ignore[valid-type]
    skill_type = Literal[skill_id]  # type: ignore[valid-type]
    if coverage:
        return create_model(
            schema_name,
            __base__=SkillReport,
            skill_id=(skill_type, ...),
            outcome=(outcome_type, ...),
            coverage=(CoverageBlock, ...),
        )
    return create_model(
        schema_name,
        __base__=SkillReport,
        skill_id=(skill_type, ...),
        outcome=(outcome_type, ...),
    )


class SkillCatalogEntry(BaseModel):
    """One presented skill: identity, audience, grammar, effects and output.

    ``budget_class`` is the prompt-budget class the skill's bytes are charged
    to. A skill loads on demand, so it can never declare the always-on zone-1
    class and enlarge what every session carries. ``subject`` names the
    epoch-2 entity the skill operates on, or, for a skill with no lifecycle
    entity, the thing it works over.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(pattern=_SKILL_ID_PATTERN)
    skill_class: SkillClass
    subject: str = Field(min_length=1, max_length=40)
    audience: InvocationAudience
    operator_only_actions: tuple[str, ...] = ()
    description: str = Field(min_length=1, max_length=160)
    budget_class: BudgetClassId
    grammar: InvocationGrammar
    effects: EffectsBoundary
    output: OutputSchema

    @model_validator(mode="after")
    def _grammar_and_audience_agree(self) -> SkillCatalogEntry:
        if self.budget_class is BudgetClassId.STEERING_ZONE1:
            raise ValueError(f"skill {self.skill_id!r} cannot load into the always-on zone 1")
        if not self.grammar.usage.startswith(f"/{self.skill_id}"):
            raise ValueError(f"usage {self.grammar.usage!r} must start with /{self.skill_id}")
        unknown = set(self.operator_only_actions) - set(self.grammar.actions)
        if unknown:
            raise ValueError(f"operator_only_actions {sorted(unknown)} are not declared actions")
        if self.audience == "user_only" and self.operator_only_actions:
            raise ValueError("a user_only skill is operator-only whole; it narrows no action")
        return self

    @property
    def invocation_name(self) -> str:
        """Return the slash-prefixed name an operator types."""
        return f"/{self.skill_id}"

    @property
    def lanes(self) -> frozenset[Lane]:
        """Return the lanes that may invoke this skill; see :func:`skill_lanes`."""
        return skill_lanes(self)

    def validate_report(self, payload: object) -> SkillReport:
        """Validate *payload* against this skill's typed output schema.

        Args:
            payload: A mapping or model instance claiming to be this skill's report.

        Returns:
            The validated report instance.

        Raises:
            pydantic.ValidationError: The payload names another skill, an
                undeclared outcome, or an undeclared field.
        """
        return self.output.model_for(self.skill_id).model_validate(payload)


class RetiredSkill(BaseModel):
    """A pre-catalog skill name that refuses invocation and names its successor.

    ``successors`` are catalog ids; ``successor_surface`` names a non-skill home
    (a CLI command or harness behaviour) when the intent left the skill surface.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(pattern=_SKILL_ID_PATTERN)
    successors: tuple[str, ...] = ()
    successor_surface: str | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _names_a_successor(self) -> RetiredSkill:
        if not self.successors and self.successor_surface is None:
            raise ValueError(f"retired skill {self.skill_id!r} names no successor")
        return self

    def successor_text(self) -> str:
        """Return the successor phrase quoted by the retirement refusal."""
        names = [f"/{name}" for name in self.successors]
        if self.successor_surface is not None:
            names.append(self.successor_surface)
        return " or ".join(names)


class SkillCatalog(BaseModel):
    """The closed set of presented skills plus the exhaustive retired-name map."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[SkillCatalogEntry, ...] = Field(min_length=1)
    retired: tuple[RetiredSkill, ...] = ()

    @model_validator(mode="after")
    def _closed_and_consistent(self) -> SkillCatalog:
        ids = [entry.skill_id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate catalog skill id in {ids!r}")
        retired_ids = [row.skill_id for row in self.retired]
        if len(set(retired_ids)) != len(retired_ids):
            raise ValueError(f"duplicate retired skill id in {retired_ids!r}")
        aliased = set(ids) & set(retired_ids)
        if aliased:
            raise ValueError(f"retired names {sorted(aliased)} are still catalog entries")
        for row in self.retired:
            dangling = set(row.successors) - set(ids)
            if dangling:
                raise ValueError(
                    f"retired skill {row.skill_id!r} names non-catalog successors "
                    f"{sorted(dangling)}"
                )
        return self

    def entry(self, skill_id: str) -> SkillCatalogEntry | None:
        """Return the entry for bare *skill_id*, or ``None`` when absent.

        Args:
            skill_id: The bare skill id, without a namespace prefix.

        Returns:
            The matching catalog entry, or ``None``.
        """
        return next((e for e in self.entries if e.skill_id == skill_id), None)

    def retired_row(self, skill_id: str) -> RetiredSkill | None:
        """Return the retirement row for bare *skill_id*, or ``None``.

        Args:
            skill_id: The bare skill id, without a namespace prefix.

        Returns:
            The matching retirement row, or ``None`` when not retired.
        """
        return next((r for r in self.retired if r.skill_id == skill_id), None)

    def replaced_by(self, skill_id: str) -> tuple[str, ...]:
        """Return the retired names whose successors include *skill_id*.

        Args:
            skill_id: The bare id of a live catalog skill.

        Returns:
            The retired skill ids *skill_id* succeeds, in catalog order.
        """
        return tuple(r.skill_id for r in self.retired if skill_id in r.successors)


class SkillRetiredError(LookupError):
    """Raised when a retired skill name is invoked; the message names the successor."""

    def __init__(self, row: RetiredSkill) -> None:
        self.row = row
        super().__init__(
            f"skill '/{row.skill_id}' is retired; use {row.successor_text()} instead ({row.reason})"
        )


class UnknownSkillError(LookupError):
    """Raised when a name is neither a catalog skill nor a retired one."""


def _grammar(
    usage: str,
    *actions: str,
    subject_field: str = "subject_ref",
    **action_options: tuple[str, ...],
) -> InvocationGrammar:
    # An action spelled with a hyphen is passed as its snake_case keyword.
    return InvocationGrammar(
        usage=usage,
        actions=actions,
        subject_field=subject_field,
        action_options={name.replace("_", "-"): opts for name, opts in action_options.items()},
    )


def _out(schema_name: str, outcomes: str, *, coverage: bool = False) -> OutputSchema:
    return OutputSchema(
        schema_name=schema_name, terminal_outcomes=tuple(outcomes.split("|")), coverage=coverage
    )


_ENTRIES: tuple[SkillCatalogEntry, ...] = (
    SkillCatalogEntry(
        skill_id="accept",
        skill_class="lifecycle",
        subject="Milestone",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="user_only",
        description=(
            "Decide a Milestone acceptance bundle: prepare, accept, reject or request repair."
        ),
        grammar=_grammar(
            "/accept <prepare|accept|reject|request-repair|show> <milestone-ref>"
            " [--bundle <ref>] [--criterion <id>...] [--reason <text>]"
            " [--repair-scope <text>] [--dry-run]",
            "prepare",
            "accept",
            "reject",
            "request-repair",
            "show",
            prepare=("--bundle", "--criterion", "--dry-run"),
            accept=("--bundle", "--reason", "--dry-run"),
            reject=("--reason", "--dry-run"),
            request_repair=("--criterion", "--reason", "--repair-scope", "--dry-run"),
            show=(),
        ),
        effects=EffectsBoundary(
            summary="Milestone acceptance reads plus the approval, acceptance and repair verbs.",
            rpcs=(
                "projection.milestone.read",
                "projection.milestone.acceptance",
                "runtime.delivery.open_acceptance_approval",
                "runtime.delivery.seal_acceptance_approval",
                "domain.milestone.accept",
                "runtime.delivery.request_acceptance_repair",
            ),
            canonical_mutates=True,
        ),
        output=_out(
            "AcceptanceSkillReport", "shown|prepared|accepted|rejected|repair_requested|blocked"
        ),
    ),
    SkillCatalogEntry(
        skill_id="attend",
        skill_class="lifecycle",
        subject="PendingAction",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        operator_only_actions=("resolve",),
        description="Work the Attention queue: pending actions, open questions and open pauses.",
        grammar=_grammar(
            "/attend [<list|prepare|resolve>] [<item-ref>]"
            " [--kind <action|question|pause>] [--urgency <watch|normal|high|urgent>]"
            " [--option <id>] [--answer <text>] [--limit <N>] [--scope <urn>] [--dry-run]",
            "list",
            "prepare",
            "resolve",
            list=("--kind", "--urgency", "--limit", "--scope"),
            prepare=("--kind", "--scope"),
            resolve=("--option", "--answer", "--dry-run"),
        ),
        effects=EffectsBoundary(
            summary=(
                "Attention reads plus the approval and permission verbs that answer a"
                " pending action, when resolve is explicitly selected."
            ),
            rpcs=(
                "projection.attention.read",
                "projection.notifications.read",
                "runtime.delivery.seal_acceptance_approval",
                "runtime.permission.decide",
                "runtime.question.open_decision",
            ),
            canonical_mutates=True,
        ),
        output=_out("AttentionSkillReport", "listed|prepared|resolved|needs_operator|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="backlog",
        skill_class="lifecycle",
        subject="Task",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        description="Add or list draft Tasks and propose their promotion through a plan.",
        grammar=_grammar(
            "/backlog <add|list|promote> [<task-ref>]"
            " [--intent <text>] [--priority <critical|high|normal|low>]"
            " [--to-batch <ref>] [--criterion <text>...] [--owner <path-selector>...]"
            " [--limit <N>] [--dry-run]",
            "add",
            "list",
            "promote",
            add=("--intent", "--priority", "--criterion", "--owner", "--dry-run"),
            list=("--priority", "--limit"),
            promote=("--to-batch", "--criterion", "--owner", "--dry-run"),
        ),
        effects=EffectsBoundary(
            summary="The backlog read, the draft Task create verb and the plan submission verb.",
            rpcs=(
                "projection.backlog.read",
                "domain.task.create",
                "planning.plan_revision.submit",
            ),
            canonical_mutates=True,
        ),
        output=_out("BacklogSkillReport", "listed|added|promotion_proposed|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="campaign",
        skill_class="investigation",
        subject="Campaign",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="user_only",
        description="Run a complete Campaign from definition through terminal synthesis.",
        grammar=_grammar(
            "/campaign <run|resume|steer|cancel|show> [<topic-or-campaign-ref>...]"
            " [--goal <text>] [--track <ref>] [--audience <text>] [--use <text>]"
            " [--artifact <kind>] [--question <text>...] [--exclude <text>...]"
            " [--require-source <selector>...] [--method <policy>] [--rounds <N>]"
            " [--agents <N>] [--budget <spec>] [--checkpoint <each-round|on-risk|terminal>]"
            " [--stop <rule>...] [--feedback <ref>...] [--resume <campaign-ref>]"
            " [--reason <text>]",
            "run",
            "resume",
            "steer",
            "cancel",
            "show",
            resume=("--resume", "--budget", "--feedback"),
            steer=("--question", "--exclude", "--feedback", "--reason"),
            cancel=("--reason",),
            show=(),
        ),
        effects=EffectsBoundary(
            summary="Campaign create, run, steer and cancel verbs, their reads, and Run tools.",
            rpcs=(
                "projection.campaign.read",
                "projection.campaign.step.read",
                "projection.campaign.artifact.read",
                "research.create_campaign",
                "research.run",
                "research.steer",
                "research.cancel_campaign",
            ),
            tools=(
                SemanticToolId.SUBMIT_EVIDENCE,
                SemanticToolId.SUBMIT_REPORT,
                SemanticToolId.ASK_OPERATOR,
            ),
            canonical_mutates=True,
        ),
        output=_out(
            "CampaignRunReport",
            "completed|dropped|failed|needs_operator|paused|budget_exhausted",
            coverage=True,
        ),
    ),
    SkillCatalogEntry(
        skill_id="decide",
        skill_class="knowledge",
        subject="Decision",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        operator_only_actions=("supersede",),
        description="Propose, supersede or show a Decision.",
        grammar=_grammar(
            "/decide <propose|supersede|show> [<decision-ref>]"
            " [--summary <text>] [--rationale <text>] [--alternative <text>...]"
            " [--supersedes <ref>] [--scope <urn>] [--dry-run]",
            "propose",
            "supersede",
            "show",
            propose=("--summary", "--rationale", "--alternative", "--scope", "--dry-run"),
            supersede=("--supersedes", "--dry-run"),
            show=("--scope",),
        ),
        effects=EffectsBoundary(
            summary="The decision add, supersede, list and graph verbs.",
            rpcs=("runtime.question.open_decision",),
            verbs=("decision add", "decision supersede", "decision list", "decision graph"),
            canonical_mutates=True,
        ),
        output=_out("DecisionSkillReport", "shown|proposed|superseded|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="dispatch",
        skill_class="lifecycle",
        subject="Batch",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        description="Coordinate one Delivery Batch: bring its ready Tasks to a candidate.",
        grammar=_grammar(
            "/dispatch <batch-ref> [--task <ref>...]"
            " [--until <frontier-empty|candidate-ready|attention>]"
            " [--provider <id>] [--resume <run-ref>] [--run <run-ref>]"
            " [--run-request <compiled>] [--budget <tokens>] [--dry-run]"
            " [--idempotency-key <key>] [--output <human|json|markdown>]",
            subject_field="batch_ref",
        ),
        effects=EffectsBoundary(
            summary="Coordinator read models plus the Run dispatch and retry verbs.",
            rpcs=(
                "projection.batch.detail.read",
                "projection.task.detail.read",
                "projection.run.detail.read",
                "runtime.run.dispatch",
                "runtime.run.retry",
            ),
            canonical_mutates=True,
        ),
        output=_out(
            "CoordinationReport",
            "candidate_ready|frontier_empty|needs_operator|budget_exhausted|blocked|cancelled",
        ),
    ),
    SkillCatalogEntry(
        skill_id="integrate",
        skill_class="lifecycle",
        subject="Batch",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        operator_only_actions=("apply", "retry"),
        description=("Prepare or execute one daemon-owned integration action on a Delivery Batch."),
        grammar=_grammar(
            "/integrate <seal|select|apply|retry|show> <batch-or-candidate-ref>"
            " [--candidate <ref>...] [--strategy <declared-strategy>] [--expected-head <sha>]"
            " [--verify-after] [--reason <text>] [--base <revision-binding>]"
            " [--exit <kind>=<ref>...] [--diagnostic <evidence-ref>] [--dry-run]"
            " [--run <run-ref>] [--report-schema-ref <ref>] [--report-digest <digest>]"
            " [--verdict <verdict>] [--resulting-tree-digest <digest>]"
            " [--expected-revision <N>] [--idempotency-key <key>]"
            " [--output <human|json|markdown>]",
            "seal",
            "select",
            "apply",
            "retry",
            "show",
            seal=(
                "--run",
                "--report-schema-ref",
                "--report-digest",
                "--verdict",
                "--resulting-tree-digest",
                "--expected-revision",
                "--idempotency-key",
                "--output",
            ),
            select=("--candidate", "--strategy", "--output"),
            apply=(
                "--candidate",
                "--expected-head",
                "--verify-after",
                "--base",
                "--exit",
                "--diagnostic",
                "--dry-run",
                "--expected-revision",
                "--idempotency-key",
                "--output",
            ),
            retry=(
                "--candidate",
                "--expected-head",
                "--reason",
                "--base",
                "--exit",
                "--diagnostic",
                "--dry-run",
                "--expected-revision",
                "--idempotency-key",
                "--output",
            ),
            show=("--output",),
        ),
        effects=EffectsBoundary(
            summary=(
                "Batch and conflict reads plus the candidate-report, delivery-assembly and"
                " delivery-integration verbs."
            ),
            rpcs=(
                "projection.batch.detail.read",
                "projection.merge.conflict.read",
                "runtime.candidate.report.bind",
                "runtime.delivery.assemble",
                "runtime.delivery.integrate",
                "runtime.question.open_decision",
            ),
            canonical_mutates=True,
        ),
        output=_out(
            "IntegrationSkillReport",
            "shown|sealed|selected|integrated|conflicted|stale|blocked",
        ),
    ),
    SkillCatalogEntry(
        skill_id="memory",
        skill_class="knowledge",
        subject="memory entry",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        operator_only_actions=("promote",),
        description="Search, write, promote or show memory entries.",
        grammar=_grammar(
            "/memory <search|write|promote|show> [<query-or-memory-ref>...]"
            " [--scope <urn>] [--title <text>] [--body <text>]"
            " [--confidence <low|medium|high>] [--limit <N>] [--dry-run]",
            "search",
            "write",
            "promote",
            "show",
            search=("--scope", "--limit"),
            write=("--scope", "--title", "--body", "--confidence", "--dry-run"),
            promote=("--scope", "--confidence", "--dry-run"),
            show=("--scope",),
        ),
        effects=EffectsBoundary(
            summary="The memory list, view, add and promote verbs.",
            rpcs=("runtime.question.open_decision",),
            verbs=("memory list", "memory view", "memory add", "memory promote"),
            canonical_mutates=True,
        ),
        output=_out("MemorySkillReport", "listed|shown|written|promoted|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="milestone",
        skill_class="lifecycle",
        subject="Milestone",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="user_only",
        description="Define, show, activate or cancel a Milestone.",
        grammar=_grammar(
            "/milestone <define|show|activate|cancel> [<milestone-ref>]"
            " [--track <ref>] [--title <text>] [--outcome <text>] [--appetite <duration>]"
            " [--exclude <text>...] [--journey-step <text>...] [--batch <ref>...]"
            " [--reason <text>] [--from-spec <path|->] [--dry-run]",
            "define",
            "show",
            "activate",
            "cancel",
            define=(
                "--track",
                "--title",
                "--outcome",
                "--appetite",
                "--exclude",
                "--journey-step",
                "--batch",
                "--from-spec",
                "--dry-run",
            ),
            show=(),
            activate=("--from-spec", "--dry-run"),
            cancel=("--reason", "--from-spec", "--dry-run"),
        ),
        effects=EffectsBoundary(
            summary="The Milestone read plus its create, activate and cancel verbs.",
            rpcs=(
                "projection.milestone.read",
                "domain.milestone.create",
                "domain.milestone.activate",
                "domain.milestone.cancel",
            ),
            canonical_mutates=True,
        ),
        output=_out("MilestoneSkillReport", "shown|defined|activated|cancelled|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="mockup",
        skill_class="engineering",
        subject="operator-visible surface",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        description="Build and compare operator-visible design options.",
        grammar=_grammar(
            "/mockup <surface...> [--question <text>] [--option <text>...]"
            " [--count <2..4>] [--format <ascii|html|image>] [--viewport <spec>...]"
            " [--state <name>...] [--compare <axis>...] [--output <inline|local>]"
            " [--local-root <path-under-.ea/local/mockups>] [--budget <spec>]"
        ),
        effects=EffectsBoundary(
            summary="Inline rendering or proposal-local assets only.",
            tools=(SemanticToolId.ASK_OPERATOR,),
            canonical_mutates=False,
            local_write_scope=".ea/local/mockups",
        ),
        output=_out("MockupReport", "presented|selected|needs_operator|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="plan",
        skill_class="lifecycle",
        subject="PlanRevision",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        operator_only_actions=("approve", "apply"),
        description="Propose, approve and apply a PlanRevision.",
        grammar=_grammar(
            "/plan <propose|approve|apply> [<milestone-or-revision-ref>] [--from <ref>...]"
            " [--strategy <minimal|balanced|parallel>] [--scope <urn>] [--agents <1..8>]"
            " [--budget <spec>] [--feedback <ref>...] [--dry-run]",
            "propose",
            "approve",
            "apply",
            propose=(
                "--from",
                "--strategy",
                "--scope",
                "--agents",
                "--budget",
                "--feedback",
                "--dry-run",
            ),
            approve=("--dry-run",),
            apply=("--dry-run",),
        ),
        effects=EffectsBoundary(
            summary="The roadmap read plus the PlanRevision submit, approve and apply verbs.",
            rpcs=(
                "projection.roadmap.read",
                "planning.plan_revision.submit",
                "planning.plan_revision.approve",
                "planning.plan_revision.apply",
                "runtime.question.open_decision",
            ),
            canonical_mutates=True,
        ),
        output=_out("PlanSkillReport", "proposed|rejected|approved|applied|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="refactor",
        skill_class="engineering",
        subject="repository code",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        description="Inspect or apply a bounded structural refactor.",
        grammar=_grammar(
            "/refactor <target...>"
            " [--pattern <extract-function|extract-module|split-class|graduate|custom>]"
            " [--goal <text>] [--mode <inspect|apply>] [--include <selector>...]"
            " [--exclude <selector>...] [--constraint <text>...] [--test <command>...]"
            " [--budget <spec>] [--dry-run]"
        ),
        effects=EffectsBoundary(
            summary="Leased workspace edits only in apply mode; no canonical RPC.",
            canonical_mutates=False,
        ),
        output=_out("RefactorReport", "plan_ready|applied|verified|failed|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="reflect",
        skill_class="knowledge",
        subject="measurement record",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="user_only",
        description=(
            "Report where effort, time and money actually went, without mutating canonical state."
        ),
        grammar=_grammar(
            "/reflect <run|show|export|prune> [--out <path>] [--local-only]",
            "run",
            "show",
            "export",
            "prune",
            run=("--out", "--local-only"),
            show=(),
            export=("--out",),
            prune=(),
        ),
        effects=EffectsBoundary(
            summary=(
                "The reflect run, show, export and prune verbs: read-only apart from the"
                " session title fill, which --local-only disables, and the local report files."
            ),
            verbs=("reflect run", "reflect show", "reflect export", "reflect prune"),
            canonical_mutates=False,
            local_write_scope=".ea/local/reflect",
        ),
        output=_out("ReflectReport", "reported|partial|unavailable|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="release",
        skill_class="lifecycle",
        subject="Release",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="user_only",
        description="Prove, preflight, approve, publish, observe and recover a release.",
        grammar=_grammar(
            "/release <create|show|pin|preflight|approve|publish|observe|retry-target|recover>"
            " [<release-ref>] [--version <version>] [--milestone <ref>...]"
            " [--channel <dev|rc|stable>] [--source <sha>] [--target <id>...] [--dry-run]",
            "create",
            "show",
            "pin",
            "preflight",
            "approve",
            "publish",
            "observe",
            "retry-target",
            "recover",
            create=("--version", "--milestone", "--channel", "--dry-run"),
            show=(),
            pin=("--source", "--dry-run"),
            preflight=("--source",),
            approve=("--dry-run",),
            publish=("--source", "--dry-run"),
            observe=("--target",),
            retry_target=("--target", "--dry-run"),
            recover=("--target", "--dry-run"),
        ),
        effects=EffectsBoundary(
            summary="Release reads plus the create, pin, readiness, approval and publish verbs.",
            rpcs=(
                "projection.release.read",
                "release.show",
                "release.create",
                "release.candidate",
                "release.compute_readiness",
                "release.approve",
                "release.publish",
                "release.observe_target",
                "release.retry_target",
                "release.reconcile",
            ),
            canonical_mutates=True,
        ),
        output=_out(
            "ReleaseSkillReport",
            "shown|drafted|pinned|ready|not_ready|approved|submitted|observed|recovering"
            "|released|blocked",
        ),
    ),
    SkillCatalogEntry(
        skill_id="research",
        skill_class="investigation",
        subject="question",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        description="Answer one question with a swift one-page investigation; no Campaign.",
        grammar=_grammar(
            "/research <topic...> [--question <text>] [--scope <urn>] [--from <ref>...]"
            " [--include <selector>...] [--exclude <selector>...]"
            " [--sources <repo|external|both>] [--web <auto|allow|deny|required>]"
            " [--domains <domain>...] [--recency-days <N>] [--max-sources <1..20>]"
            " [--agents <1..3>] [--budget <spec>] [--save [<relative-path>]]"
            " [--output <markdown|json>]",
            subject_field="topic",
        ),
        effects=EffectsBoundary(
            summary="Evidence read and repository reads; optional gitignored local brief.",
            rpcs=("projection.evidence.read",),
            tools=(
                SemanticToolId.EAWF_STATE_QUERY,
                SemanticToolId.REPO_READ,
                SemanticToolId.REPO_SEARCH,
                SemanticToolId.SUBMIT_REPORT,
            ),
            canonical_mutates=False,
            local_write_scope=".ea/local/research",
        ),
        output=_out("SwiftResearchReport", "answered|open|blocked", coverage=True),
    ),
    SkillCatalogEntry(
        skill_id="spike",
        skill_class="investigation",
        subject="proof of concept",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        description="Build, test, independently verify and present a local proof of concept.",
        grammar=_grammar(
            "/spike <idea...> [--hypothesis <text>] [--confirm <condition>]"
            " [--reject <condition>] [--from <ref>...] [--constraint <text>...]"
            " [--stack <auto|python|shell|node|other>] [--entrypoint <relative-path>]"
            " [--verify <command>...] [--fixture <ref>...] [--provider <id>...]"
            " [--agents <2..8>] [--budget <spec>] [--slug <slug>]"
            " [--local-root <path-under-.ea/local/spikes>] [--network <deny|allow>]"
            " [--retention <keep|expire-after-review>] [--resume <folder>]"
        ),
        effects=EffectsBoundary(
            summary=(
                "Writes only under the resolved local spike folder; separate builder and"
                " verifier Runs; the report is filed and its contracts submitted for promotion."
            ),
            rpcs=("runtime.run.dispatch", "runtime.evidence.spike_report.file"),
            tools=(
                SemanticToolId.REPO_READ,
                SemanticToolId.SUBMIT_REPORT,
                SemanticToolId.SUBMIT_EVIDENCE,
            ),
            canonical_mutates=False,
            local_write_scope=".ea/local/spikes",
        ),
        output=_out("SpikeReport", "ready|inconclusive|failed|cancelled|blocked", coverage=True),
    ),
    SkillCatalogEntry(
        skill_id="test",
        skill_class="engineering",
        subject="test contract",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        description="Design, add, repair or run a bounded test contract.",
        grammar=_grammar(
            "/test <design|add|repair|run> <target...>"
            " [--kind <unit|property|integration|golden|conformance|e2e>]"
            " [--invariant <text>] [--case <text>...] [--command <command>...]"
            " [--seed <N>] [--examples <N>] [--mode <inspect|apply>] [--budget <spec>]"
            " [--dry-run]",
            "design",
            "add",
            "repair",
            "run",
            run=("--command", "--seed", "--budget"),
        ),
        effects=EffectsBoundary(
            summary="Leased test-workspace edits and test execution; no canonical RPC.",
            canonical_mutates=False,
        ),
        output=_out(
            "TestSkillReport", "strategy_ready|tests_added|passed|failed|blocked", coverage=True
        ),
    ),
    SkillCatalogEntry(
        skill_id="track",
        skill_class="lifecycle",
        subject="Track",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="user_only",
        description="Create, show or retire a Track.",
        grammar=_grammar(
            "/track <create|show|retire> [<track-ref>] [--title <text>]"
            " [--charter <text>] [--owner <principal>] [--repository <ref>...]"
            " [--scope <urn>] [--reason <text>] [--from-spec <path|->] [--dry-run]",
            "create",
            "show",
            "retire",
            create=(
                "--title",
                "--charter",
                "--owner",
                "--repository",
                "--scope",
                "--from-spec",
                "--dry-run",
            ),
            show=(),
            retire=("--reason", "--from-spec", "--dry-run"),
        ),
        effects=EffectsBoundary(
            summary="The Track read plus its create and retire verbs.",
            rpcs=("projection.track.read", "domain.track.create", "domain.track.retire"),
            canonical_mutates=True,
        ),
        output=_out("TrackSkillReport", "shown|created|retired|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="verify",
        skill_class="lifecycle",
        subject="Batch",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        description="Verify one Delivery Batch at one exact revision, as auditor or as reviewer.",
        grammar=_grammar(
            "/verify <batch-or-revision-ref> [--mode <gates|audit|review|security|all>]"
            " [--gate <id>...] [--severity-floor <P0|P1|P2|P3>] [--agents <1..8>]"
            " [--budget <spec>] [--milestone <ref>] [--journey <step>...]"
            " [--accepted-binding <binding>] [--requested-by <principal>] [--no-cache]"
            " [--expected-revision <N>] [--idempotency-key <key>]"
            " [--output <human|json|markdown>]"
        ),
        effects=EffectsBoundary(
            summary=(
                "Batch and evidence reads plus the Batch verification, Task completion and"
                " acceptance-question verbs, which file verification receipts."
            ),
            rpcs=(
                "projection.batch.detail.read",
                "projection.evidence.read",
                "runtime.delivery.verify_batch",
                "runtime.delivery.assess_completion",
                "runtime.delivery.open_acceptance_approval",
            ),
            canonical_mutates=True,
        ),
        output=_out("VerificationReport", "passed|failed|unverified|stale|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="why",
        skill_class="knowledge",
        subject="any canonical entity",
        budget_class=BudgetClassId.STEERING_ZONE2,
        audience="both",
        description="Explain the provenance of an entity; read-only.",
        grammar=_grammar(
            "/why <subject-ref> [--at <revision-or-time>] [--depth <summary|chain|full>]"
            " [--include <decisions|events|evidence|receipts>...]"
            " [--format <text|graph|json>] [--verify-links]"
        ),
        effects=EffectsBoundary(
            summary="Read-only provenance queries.",
            rpcs=(
                "projection.history.read",
                "projection.evidence.read",
                "semantic.result.read",
                "runtime.run.events.read",
            ),
            tools=(SemanticToolId.EAWF_STATE_QUERY,),
            canonical_mutates=False,
        ),
        output=_out("WhyReport", "explained|partial|not_found|blocked", coverage=True),
    ),
)


def _retired(skill_id: str, *successors: str, reason: str) -> RetiredSkill:
    return RetiredSkill(skill_id=skill_id, successors=successors, reason=reason)


_RETIRED: tuple[RetiredSkill, ...] = (
    _retired("prep", "plan", "dispatch", reason="planning and dispatch are separate intents"),
    _retired("roadmap", "plan", "milestone", reason="PlanRevision and Milestone own planning"),
    _retired("wave-spec", "plan", reason="task specs are authored inside a PlanRevision"),
    _retired("flow", "dispatch", reason="orchestration is a compiled coordinator contract"),
    _retired("agent-dispatch", "dispatch", reason="the runtime router is internal to /dispatch"),
    _retired("polish", "dispatch", reason="polish is a Task role run under a Batch"),
    _retired("audit", "verify", reason="audit is one mode of the verification cycle"),
    _retired("review", "verify", reason="review is one mode of the verification cycle"),
    _retired("security-review", "verify", reason="security is one verification mode"),
    _retired("ship", "release", reason="release owns proof, publish and recovery"),
    _retired("blitz", "campaign", reason="follow-up rounds belong to a Campaign"),
    _retired("differentiate", "spike", "research", reason="discriminate by code or evidence"),
    _retired("design", "mockup", "plan", reason="surfaces and behaviour contracts split"),
    _retired("math-explainer", "research", "test", reason="explanation and proof split"),
    _retired("write-adr", "decide", reason="decisions are typed Decision rows"),
    _retired("add-property-test", "test", reason="property tests are one /test kind"),
    _retired("extract-function", "refactor", reason="one /refactor pattern"),
    _retired("extract-module", "refactor", reason="one /refactor pattern"),
    _retired("refactor-god-class", "refactor", reason="one /refactor pattern"),
    _retired("graduate-research-code", "refactor", reason="one /refactor pattern"),
    RetiredSkill(
        skill_id="init",
        successor_surface="`eawf init`",
        reason="bootstrap is a CLI command, not a skill",
    ),
    RetiredSkill(
        skill_id="coauthor",
        successor_surface="the commit hook's trailer policy",
        reason="trailer policy is harness hygiene",
    ),
    RetiredSkill(
        skill_id="compress",
        successor_surface="the runtime harness",
        reason="context compaction is harness behaviour",
    ),
)

SKILL_CATALOG: SkillCatalog = SkillCatalog(entries=_ENTRIES, retired=_RETIRED)


def resolve_skill(name: str) -> SkillCatalogEntry:
    """Resolve an invoked skill name to its catalog entry.

    Accepts the slashed (``/plan``) and bare (``plan``) forms.

    Args:
        name: The name as typed by the operator or caller.

    Returns:
        The catalog entry for *name*.

    Raises:
        TypeError: *name* is not a string.
        SkillRetiredError: *name* is retired; the message names its successor.
        UnknownSkillError: *name* is neither a catalog skill nor retired.
    """
    if not isinstance(name, str):
        raise TypeError(f"skill name must be str, got {type(name).__name__}")
    bare = name.removeprefix("/")
    entry = SKILL_CATALOG.entry(bare)
    if entry is not None:
        return entry
    retired = SKILL_CATALOG.retired_row(bare)
    if retired is not None:
        raise SkillRetiredError(retired)
    known = ", ".join(e.invocation_name for e in SKILL_CATALOG.entries)
    raise UnknownSkillError(f"unknown skill {name!r}; expected one of {known}")


def skill_lanes(entry: SkillCatalogEntry) -> frozenset[Lane]:
    """Return who may invoke *entry*: the one invocation-audience map.

    Args:
        entry: A catalog entry.

    Returns:
        ``operator`` when an operator may invoke it, ``agent`` when a model
        may. The host menu, the generated help, the agent callable catalog and
        the invocation check all read this answer and no other.
    """
    return _AUDIENCE_LANES[entry.audience]


def skills_for_lane(lane: Lane) -> tuple[SkillCatalogEntry, ...]:
    """Return the catalog entries *lane* may invoke, in catalog order.

    Args:
        lane: ``operator`` for generated user help, ``agent`` for the agent
            callable catalog.

    Returns:
        The entries whose :func:`skill_lanes` include *lane*.
    """
    return tuple(entry for entry in SKILL_CATALOG.entries if lane in skill_lanes(entry))


@cache
def shipped_skill_specs() -> tuple[SkillSpec, ...]:
    """Project the catalog into the render specs every plugin packager ships.

    Order follows :data:`SKILL_CATALOG`. The argument hint is derived from the
    catalog grammar and every body is rendered through the six-slot prompt
    chassis, so no shipped page is hand-written or carried over from a
    pre-catalog body. The host-menu flags come from :func:`skill_lanes`: a
    skill the agent lane may not invoke forbids model invocation, and its
    page states it is operator-invoked by design. Generation first joins the
    catalog to the verb catalog, so a skill naming a route no verb carries
    ships nothing.

    Returns:
        One :class:`~eawf.surfaces.render.skills.render.SkillSpec` per entry.

    Raises:
        eawf.workflow.skills.catalog_join.CatalogJoinError: A skill names a
            missing or incompatible route.
        eawf.workflow.skills.bodies.chassis.SkillPageError: A rendered page
            breaks the chassis.
    """
    from eawf.surfaces.render.skills.render import SkillSpec
    from eawf.workflow.skills.bodies.chassis import check_skill_page, shipped_skill_page
    from eawf.workflow.skills.catalog_join import require_joined

    require_joined(SKILL_CATALOG)
    pages = {entry.skill_id: shipped_skill_page(entry) for entry in SKILL_CATALOG.entries}
    for entry in SKILL_CATALOG.entries:
        check_skill_page(pages[entry.skill_id], entry)
    return tuple(
        SkillSpec(
            skill_name=entry.skill_id,
            description=entry.description,
            argument_hint=entry.grammar.argument_hint,
            user_invocable="operator" in skill_lanes(entry),
            disable_model_invocation="agent" not in skill_lanes(entry),
            body=pages[entry.skill_id],
        )
        for entry in SKILL_CATALOG.entries
    )


__all__ = [
    "SKILL_CATALOG",
    "CoverageBlock",
    "EffectsBoundary",
    "InvocationAudience",
    "InvocationGrammar",
    "Lane",
    "NotCovered",
    "OutputSchema",
    "RetiredSkill",
    "SkillCatalog",
    "SkillCatalogEntry",
    "SkillClass",
    "SkillReport",
    "SkillRetiredError",
    "UnknownSkillError",
    "resolve_skill",
    "shipped_skill_specs",
    "skill_lanes",
    "skills_for_lane",
]
