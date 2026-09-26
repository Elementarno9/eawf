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
"""

from __future__ import annotations

import logging
import re
from functools import cache
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

if TYPE_CHECKING:
    from eawf.surfaces.render.skills.render import SkillSpec

logger = logging.getLogger(__name__)

SkillClass = Literal["lifecycle", "investigation", "knowledge", "engineering"]
InvocationAudience = Literal["user_only", "agent_only", "both"]

_SKILL_ID_PATTERN = r"^[a-z][a-z0-9-]{1,31}$"
_ACTION_PATTERN = r"^[a-z][a-z0-9-]*$"
_OUTCOME_PATTERN = r"^[a-z][a-z0-9_]*$"
_OPTION_RE = re.compile(r"--[a-z][a-z0-9-]*")


class InvocationGrammar(BaseModel):
    """The exhaustive invocation line of one skill and its closed action set.

    ``usage`` is the single source of the grammar: the option set and the
    one-line argument hint are derived from it so the two can never drift.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    usage: str = Field(min_length=3)
    actions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _actions_are_declared_in_usage(self) -> InvocationGrammar:
        if len(set(self.actions)) != len(self.actions):
            raise ValueError(f"duplicate action in {self.actions!r}")
        for action in self.actions:
            if re.fullmatch(_ACTION_PATTERN, action) is None:
                raise ValueError(f"action {action!r} does not match {_ACTION_PATTERN}")
            if action not in self.usage:
                raise ValueError(f"action {action!r} is not in usage {self.usage!r}")
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


class EffectsBoundary(BaseModel):
    """What one skill may touch: its RPC allowlist and its local write root.

    An RPC outside ``rpcs`` is denied before it reaches a handler, and a skill
    with no ``local_write_scope`` may not write local files at all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1)
    rpcs: tuple[str, ...] = ()
    canonical_mutates: bool
    local_write_scope: str | None = None

    @model_validator(mode="after")
    def _write_scope_is_repo_relative(self) -> EffectsBoundary:
        scope = self.local_write_scope
        if scope is not None and (scope.startswith("/") or ".." in scope.split("/")):
            raise ValueError(f"local_write_scope {scope!r} must be a repo-relative path")
        if len(set(self.rpcs)) != len(self.rpcs):
            raise ValueError(f"duplicate rpc in {self.rpcs!r}")
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


class OutputSchema(BaseModel):
    """The typed terminal report a skill invocation must produce."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: str = Field(pattern=r"^[A-Z][A-Za-z]+Report$")
    terminal_outcomes: tuple[str, ...] = Field(min_length=1)

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
            ``skill_id`` and ``outcome`` accept only the declared literals.
        """
        return _report_model(self.schema_name, skill_id, self.terminal_outcomes)


@cache
def _report_model(schema_name: str, skill_id: str, outcomes: tuple[str, ...]) -> type[SkillReport]:
    # Literal[...] over a runtime tuple is the documented way to build a closed
    # enum field on a generated model; mypy cannot follow it, pydantic can.
    outcome_type = Literal[outcomes]  # type: ignore[valid-type]
    skill_type = Literal[skill_id]  # type: ignore[valid-type]
    return create_model(
        schema_name,
        __base__=SkillReport,
        skill_id=(skill_type, ...),
        outcome=(outcome_type, ...),
    )


class SkillCatalogEntry(BaseModel):
    """One presented skill: identity, audience, grammar, effects and output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(pattern=_SKILL_ID_PATTERN)
    skill_class: SkillClass
    audience: InvocationAudience
    operator_only_actions: tuple[str, ...] = ()
    description: str = Field(min_length=1, max_length=160)
    grammar: InvocationGrammar
    effects: EffectsBoundary
    output: OutputSchema

    @model_validator(mode="after")
    def _grammar_and_audience_agree(self) -> SkillCatalogEntry:
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
        """Return the entry for bare *skill_id*, or ``None`` when absent."""
        return next((e for e in self.entries if e.skill_id == skill_id), None)

    def retired_row(self, skill_id: str) -> RetiredSkill | None:
        """Return the retirement row for bare *skill_id*, or ``None``."""
        return next((r for r in self.retired if r.skill_id == skill_id), None)

    def replaced_by(self, skill_id: str) -> tuple[str, ...]:
        """Return the retired names whose successors include *skill_id*."""
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


def _grammar(usage: str, *actions: str) -> InvocationGrammar:
    return InvocationGrammar(usage=usage, actions=actions)


def _out(schema_name: str, outcomes: str) -> OutputSchema:
    return OutputSchema(schema_name=schema_name, terminal_outcomes=tuple(outcomes.split("|")))


_ENTRIES: tuple[SkillCatalogEntry, ...] = (
    SkillCatalogEntry(
        skill_id="accept",
        skill_class="lifecycle",
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
        ),
        effects=EffectsBoundary(
            summary="Milestone acceptance RPCs.",
            rpcs=(
                "read_entity",
                "query_evidence",
                "milestone.acceptance.prepare",
                "milestone.acceptance.decide",
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
        audience="both",
        operator_only_actions=("resolve",),
        description="Work the Attention queue: pending actions, open questions and open pauses.",
        grammar=_grammar(
            "/attend [<list|prepare|resolve|defer|watch>] [<item-ref>]"
            " [--kind <action|question|pause>] [--urgency <watch|normal|high|urgent>]"
            " [--option <id>] [--answer <text>] [--until <datetime>] [--limit <N>]"
            " [--scope <urn>] [--dry-run]",
            "list",
            "prepare",
            "resolve",
            "defer",
            "watch",
        ),
        effects=EffectsBoundary(
            summary=(
                "Attention read and preparation plus the protected resolution RPC"
                " when explicitly selected."
            ),
            rpcs=(
                "read_entity",
                "query_evidence",
                "operations.pending_action.resolve",
                "operations.pending_action.hold",
                "operations.pending_action.cancel",
                "research.question.answer",
                "research.question.drop",
                "operations.pause.hold",
                "operations.pause.resume",
                "operations.pause.cancel",
            ),
            canonical_mutates=True,
        ),
        output=_out(
            "AttentionSkillReport",
            "listed|prepared|resolved|deferred|watching|needs_operator|blocked",
        ),
    ),
    SkillCatalogEntry(
        skill_id="backlog",
        skill_class="lifecycle",
        audience="both",
        operator_only_actions=("drop",),
        description="Add, prioritize, defer, drop or propose promotion of draft Tasks.",
        grammar=_grammar(
            "/backlog <add|list|prioritize|defer|drop|promote> [<task-ref>]"
            " [--intent <text>] [--priority <critical|high|normal|low>]"
            " [--due-scope <urn>] [--to-batch <ref>] [--criterion <text>...]"
            " [--owner <path-selector>...] [--reason <text>] [--limit <N>] [--dry-run]",
            "add",
            "list",
            "prioritize",
            "defer",
            "drop",
            "promote",
        ),
        effects=EffectsBoundary(
            summary="Task-draft and PlanRevision proposal RPCs.",
            rpcs=(
                "read_entity",
                "domain.task.create_draft",
                "domain.task.set_priority",
                "domain.task.defer",
                "domain.task.drop",
                "planning.plan_revision.propose",
            ),
            canonical_mutates=True,
        ),
        output=_out(
            "BacklogSkillReport",
            "listed|added|updated|deferred|dropped|promotion_proposed|blocked",
        ),
    ),
    SkillCatalogEntry(
        skill_id="campaign",
        skill_class="investigation",
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
        ),
        effects=EffectsBoundary(
            summary="Campaign, evidence, question, dispatch, review and completion RPCs.",
            rpcs=(
                "read_entity",
                "query_evidence",
                "submit_evidence",
                "raise_question",
                "submit_report",
                "research.campaign.create",
                "research.campaign.request_approval",
                "research.campaign.activate",
                "research.campaign.dispatch_frontier",
                "research.campaign.checkpoint",
                "research.campaign.begin_synthesis",
                "research.campaign.submit_artifact",
                "research.campaign.request_review",
                "research.campaign.complete",
                "research.campaign.drop",
                "research.campaign.fail",
                "research.campaign.get",
            ),
            canonical_mutates=True,
        ),
        output=_out(
            "CampaignRunReport",
            "completed|dropped|failed|needs_operator|paused|budget_exhausted",
        ),
    ),
    SkillCatalogEntry(
        skill_id="decide",
        skill_class="knowledge",
        audience="both",
        operator_only_actions=("ratify", "reject", "supersede", "obsolete"),
        description="Propose, ratify, reject, supersede or obsolete a Decision.",
        grammar=_grammar(
            "/decide <propose|ratify|reject|supersede|obsolete|show> [<decision-ref>]"
            " [--title <text>] [--rationale <text>] [--alternative <text>...]"
            " [--consequence <text>...] [--evidence <ref>...] [--supersedes <ref>]"
            " [--scope <urn>] [--from <ref>...] [--dry-run]",
            "propose",
            "ratify",
            "reject",
            "supersede",
            "obsolete",
            "show",
        ),
        effects=EffectsBoundary(
            summary="Decision RPCs.",
            rpcs=(
                "read_entity",
                "query_evidence",
                "decision.propose",
                "decision.request_ratification",
                "decision.reject",
                "decision.supersede",
                "decision.obsolete",
                "decision.get",
            ),
            canonical_mutates=True,
        ),
        output=_out(
            "DecisionSkillReport",
            "shown|proposed|ratified|rejected|superseded|obsoleted|blocked",
        ),
    ),
    SkillCatalogEntry(
        skill_id="dispatch",
        skill_class="lifecycle",
        audience="both",
        description="Coordinate one Delivery Batch: bring its ready Tasks to a candidate.",
        grammar=_grammar(
            "/dispatch <batch-ref> [--task <ref>...]"
            " [--until <frontier-empty|candidate-ready|attention>] [--max-parallel <N>]"
            " [--provider <id>] [--resume <operation-ref>] [--budget <spec>] [--dry-run]"
        ),
        effects=EffectsBoundary(
            summary="Coordinator and Run-dispatch RPCs.",
            rpcs=(
                "read_entity",
                "domain.task.dispatch",
                "run.dispatch",
                "run.control.interrupt",
                "run.control.cancel",
                "run.control.retry",
                "run.control.resume",
                "operation.status",
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
        audience="both",
        operator_only_actions=("apply", "retry"),
        description=("Prepare or execute one daemon-owned integration action on a Delivery Batch."),
        grammar=_grammar(
            "/integrate <seal|select|apply|retry|show> <batch-or-candidate-ref>"
            " [--candidate <ref>...] [--strategy <declared-strategy>]"
            " [--expected-head <sha>] [--verify-after] [--reason <text>] [--dry-run]",
            "seal",
            "select",
            "apply",
            "retry",
            "show",
        ),
        effects=EffectsBoundary(
            summary="Candidate and IntegrationGeneration RPCs.",
            rpcs=(
                "read_entity",
                "query_evidence",
                "candidate.seal",
                "integration.submit",
                "integration.status",
                "integration.reconcile",
                "verification.submit",
                "operation.status",
                "operation.resume",
                "operation.cancel",
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
        audience="both",
        operator_only_actions=("promote", "forget"),
        description="Search, write, promote or forget memory entries.",
        grammar=_grammar(
            "/memory <search|write|promote|forget|show> [<query-or-memory-ref>...]"
            " [--scope <urn>] [--kind <fact|preference|procedure|warning|summary>]"
            " [--content <text>] [--evidence <ref>...] [--tag <text>...]"
            " [--expires <datetime|never>] [--limit <N>] [--dry-run]",
            "search",
            "write",
            "promote",
            "forget",
            "show",
        ),
        effects=EffectsBoundary(
            summary="Memory RPCs; evidence queries only for promotion.",
            rpcs=(
                "memory.search",
                "memory.write",
                "memory.promote",
                "memory.forget",
                "memory.get",
                "query_evidence",
            ),
            canonical_mutates=True,
        ),
        output=_out("MemorySkillReport", "listed|shown|written|promoted|forgotten|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="milestone",
        skill_class="lifecycle",
        audience="user_only",
        description="Define, activate, revise, repair or cancel a Milestone.",
        grammar=_grammar(
            "/milestone <define|show|activate|revise|repair|cancel> [<milestone-ref>]"
            " [--track <ref>] [--title <text>] [--outcome <text>] [--appetite <duration>]"
            " [--exclude <text>...] [--journey-step <text>...] [--batch <ref>...]"
            " [--reason <text>] [--from-spec <path|->] [--dry-run]",
            "define",
            "show",
            "activate",
            "revise",
            "repair",
            "cancel",
        ),
        effects=EffectsBoundary(
            summary="Milestone RPCs.",
            rpcs=(
                "read_entity",
                "domain.milestone.create",
                "domain.milestone.activate",
                "domain.milestone.revise",
                "domain.milestone.repair",
                "domain.milestone.cancel",
            ),
            canonical_mutates=True,
        ),
        output=_out(
            "MilestoneSkillReport",
            "shown|defined|activated|revised|repair_requested|cancelled|blocked",
        ),
    ),
    SkillCatalogEntry(
        skill_id="mockup",
        skill_class="engineering",
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
            rpcs=("ask_operator",),
            canonical_mutates=False,
            local_write_scope=".ea/local/mockups",
        ),
        output=_out("MockupReport", "presented|selected|needs_operator|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="plan",
        skill_class="lifecycle",
        audience="both",
        operator_only_actions=("approve", "apply"),
        description="Propose, validate, revise, approve and apply a PlanRevision.",
        grammar=_grammar(
            "/plan <propose|validate|revise|approve|apply|show|diff>"
            " [<milestone-or-revision-ref>] [--from <ref>...]"
            " [--strategy <minimal|balanced|parallel>] [--scope <urn>] [--agents <1..8>]"
            " [--budget <spec>] [--set <declared-key=value>...] [--feedback <ref>...]"
            " [--dry-run]",
            "propose",
            "validate",
            "revise",
            "approve",
            "apply",
            "show",
            "diff",
        ),
        effects=EffectsBoundary(
            summary="PlanRevision RPCs.",
            rpcs=(
                "read_entity",
                "query_evidence",
                "planning.plan_revision.propose",
                "planning.plan_revision.validate",
                "planning.plan_revision.request_approval",
                "planning.plan_revision.apply",
                "planning.plan_revision.get",
                "planning.plan_revision.diff",
            ),
            canonical_mutates=True,
        ),
        output=_out(
            "PlanSkillReport",
            "shown|proposed|valid|rejected|approval_requested|approved|applied|blocked",
        ),
    ),
    SkillCatalogEntry(
        skill_id="refactor",
        skill_class="engineering",
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
        audience="user_only",
        description=(
            "Report where effort, time and money actually went, without mutating canonical state."
        ),
        grammar=_grammar(
            "/reflect [--window <duration|from..to>]"
            " [--scope <all_local|workspace|project|track|milestone>]"
            " [--cohort <project-first|personal>] [--format <report|json>]"
            " [--out <path>] [--local-only]"
        ),
        effects=EffectsBoundary(
            summary=(
                "Read-only apart from the session title fill, which --local-only"
                " disables; persists statistics and metadata to the resolved output path."
            ),
            rpcs=("read_entity", "query_measurement", "query_telemetry"),
            canonical_mutates=False,
            local_write_scope=".ea/local/reflect",
        ),
        output=_out("ReflectReport", "reported|partial|unavailable|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="release",
        skill_class="lifecycle",
        audience="user_only",
        description="Prove, preflight, approve, publish, observe and recover a release.",
        grammar=_grammar(
            "/release <create|show|pin|preflight|approve|publish|observe|retry-target|recover>"
            " [<release-ref>] [--version <version>] [--milestone <ref>...]"
            " [--channel <dev|rc|stable>] [--source <sha>] [--target <id>...] [--wait]"
            " [--resume <operation-ref>] [--dry-run]",
            "create",
            "show",
            "pin",
            "preflight",
            "approve",
            "publish",
            "observe",
            "retry-target",
            "recover",
        ),
        effects=EffectsBoundary(
            summary="Release and external-operation RPCs.",
            rpcs=(
                "read_entity",
                "query_evidence",
                "release.create",
                "release.pin",
                "release.proof.prepare",
                "release.approve",
                "release.publish",
                "release.observe",
                "release.recover",
                "operation.status",
                "operation.resume",
                "operation.cancel",
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
        audience="both",
        description="Answer one question with a swift one-page investigation; no Campaign.",
        grammar=_grammar(
            "/research <topic...> [--question <text>] [--scope <urn>] [--from <ref>...]"
            " [--include <selector>...] [--exclude <selector>...]"
            " [--sources <repo|external|both>] [--web <auto|allow|deny|required>]"
            " [--domains <domain>...] [--recency-days <N>] [--max-sources <1..20>]"
            " [--agents <1..3>] [--budget <spec>] [--save [<relative-path>]]"
            " [--output <markdown|json>]"
        ),
        effects=EffectsBoundary(
            summary="No Campaign or lifecycle RPC; optional gitignored local brief.",
            rpcs=("read_entity", "query_evidence", "retrieve_source", "submit_report"),
            canonical_mutates=False,
            local_write_scope=".ea/local/research",
        ),
        output=_out("SwiftResearchReport", "answered|open|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="spike",
        skill_class="investigation",
        audience="both",
        description="Build, test, independently verify and present a local proof of concept.",
        grammar=_grammar(
            "/spike <idea...> [--hypothesis <text>] [--confirm <condition>]"
            " [--reject <condition>] [--from <ref>...] [--constraint <text>...]"
            " [--stack <auto|python|shell|node|other>] [--entrypoint <relative-path>]"
            " [--verify <command>...] [--fixture <ref>...] [--agents <2..8>]"
            " [--budget <spec>] [--slug <slug>]"
            " [--local-root <path-under-.ea/local/spikes>] [--network <deny|allow>]"
            " [--retention <keep|expire-after-review>] [--resume <folder>]"
        ),
        effects=EffectsBoundary(
            summary=(
                "Writes only under the resolved local spike folder; separate builder and"
                " verifier Runs; extracted contracts are submitted for promotion."
            ),
            rpcs=("retrieve_source", "run.dispatch", "submit_report", "submit_evidence"),
            canonical_mutates=False,
            local_write_scope=".ea/local/spikes",
        ),
        output=_out("SpikeReport", "ready|inconclusive|failed|cancelled|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="test",
        skill_class="engineering",
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
        ),
        effects=EffectsBoundary(
            summary="Leased test-workspace edits and test execution; no canonical RPC.",
            canonical_mutates=False,
        ),
        output=_out("TestSkillReport", "strategy_ready|tests_added|passed|failed|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="track",
        skill_class="lifecycle",
        audience="user_only",
        description="Create a Track, set its policy, or retire it.",
        grammar=_grammar(
            "/track <create|show|set-policy|retire> [<track-ref>] [--title <text>]"
            " [--charter <text>] [--owner <principal>] [--repository <ref>...]"
            " [--scope <urn>] [--policy <key=value>...] [--reason <text>]"
            " [--from-spec <path|->] [--dry-run]",
            "create",
            "show",
            "set-policy",
            "retire",
        ),
        effects=EffectsBoundary(
            summary="Track RPCs.",
            rpcs=(
                "read_entity",
                "domain.track.create",
                "domain.track.set_policy",
                "domain.track.retire",
            ),
            canonical_mutates=True,
        ),
        output=_out("TrackSkillReport", "shown|created|updated|retired|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="verify",
        skill_class="lifecycle",
        audience="both",
        description="Verify one Delivery Batch at one exact revision, as auditor or as reviewer.",
        grammar=_grammar(
            "/verify <batch-or-revision-ref> [--mode <gates|audit|review|security|all>]"
            " [--gate <id>...] [--severity-floor <P0|P1|P2|P3>] [--agents <1..8>]"
            " [--budget <spec>] [--no-cache] [--output <human|json|markdown>]"
        ),
        effects=EffectsBoundary(
            summary="Read and check effects plus verification receipts.",
            rpcs=(
                "read_entity",
                "query_evidence",
                "verification.submit",
                "verification.status",
                "verification.resume",
                "batch.audit.submit",
                "batch.review.submit",
                "operation.status",
            ),
            canonical_mutates=True,
        ),
        output=_out("VerificationReport", "passed|failed|unverified|stale|blocked"),
    ),
    SkillCatalogEntry(
        skill_id="why",
        skill_class="knowledge",
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
                "read_entity",
                "query_evidence",
                "semantic.result.read",
                "run.events.read",
                "operation.status",
            ),
            canonical_mutates=False,
        ),
        output=_out("WhyReport", "explained|partial|not_found|blocked"),
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


@cache
def shipped_skill_specs() -> tuple[SkillSpec, ...]:
    """Project the catalog into the render specs every plugin packager ships.

    Order follows :data:`SKILL_CATALOG`. The argument hint is derived from the
    catalog grammar and every body is rendered through the six-slot prompt
    chassis, so no shipped page is hand-written or carried over from a
    pre-catalog body. A ``user_only`` or canonical-mutating skill is never
    model-invoked, so the model cannot autonomously drive a state transition.

    Returns:
        One :class:`~eawf.surfaces.render.skills.render.SkillSpec` per entry.

    Raises:
        eawf.workflow.skills.bodies.chassis.SkillPageError: A rendered page
            breaks the chassis.
    """
    from eawf.surfaces.render.skills.render import SkillSpec
    from eawf.workflow.skills.bodies.chassis import check_skill_page, shipped_skill_page

    pages = {entry.skill_id: shipped_skill_page(entry) for entry in SKILL_CATALOG.entries}
    for entry in SKILL_CATALOG.entries:
        check_skill_page(pages[entry.skill_id], entry)
    return tuple(
        SkillSpec(
            skill_name=entry.skill_id,
            description=entry.description,
            argument_hint=entry.grammar.argument_hint,
            user_invocable=True,
            disable_model_invocation=(
                entry.audience == "user_only" or entry.effects.canonical_mutates
            ),
            body=pages[entry.skill_id],
        )
        for entry in SKILL_CATALOG.entries
    )


__all__ = [
    "SKILL_CATALOG",
    "EffectsBoundary",
    "InvocationAudience",
    "InvocationGrammar",
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
]
