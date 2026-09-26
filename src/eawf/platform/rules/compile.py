"""Compile rule records into the one effective rule graph.

:func:`compile_rule_graph` is the single resolver every generated-policy
consumer calls: it composes the builtin, workspace and repository layers
through :func:`load_rule_layers`, validates each record, binds trust to the
resolved source, applies exact supersession, and stamps the result with a
digest in one pass. Two consumers of the same sources therefore see the same
effective graph and the same digest, whatever order the sources arrived in.
:func:`compile_card_graph` is its committed counterpart: it compiles the
builtin and repository layers only, so the committed project card never
depends on a machine-local workspace source.

Compilation fails closed, with a typed :class:`RuleCompileError`, when:

- two effective rules own one obligation, or share one rule identifier;
- a supersession does not name an existing rule's exact identifier, revision
  and digest, comes from a layer that is not strictly higher than its target,
  targets a protected rule, or competes with another supersession of the same
  target;
- a non-builtin source claims an identifier in the builtin namespace;
- a verification or enforcement reference does not resolve against the
  registered gate kinds and lint rules;
- a ``must`` rule has no delivery: it is neither backed by a registered
  enforcement reference, a role rule every rendered agent definition of its
  role embeds, nor rendered into a projection that every supported runtime
  loads;
- rule prose grants or widens tools, capabilities, network or filesystem
  reach, model selection, budgets, concurrency, retries or external effects;
  or
- rule prose carries executable interpolation or authority-language
  injection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pkgutil
import re
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import ClassVar, Final

from eawf.kernel.runtime.host_facts import ProjectionKind
from eawf.kernel.spec.release import Sha256DigestStr
from eawf.observability.telemetry.models import RuntimeName
from eawf.platform.rules.compose import (
    COMMITTED_SOURCE_KINDS,
    BuiltinRuleProvider,
    load_rule_layers,
    no_builtin_rules,
    require_committed_inputs,
)
from eawf.platform.rules.modules import builtin_rule_modules
from eawf.platform.rules.records import (
    AuthoredRule,
    QualifiedId,
    RuleModel,
    RuleRecord,
    RuleSourceIdentity,
    RuleSourceKind,
)

logger = logging.getLogger(__name__)

#: The namespace reserved for rules shipped with the tool. Only a source that
#: actually resolved as builtin may carry an identifier in it, so another
#: layer cannot borrow a builtin identity and the trust that comes with it.
BUILTIN_NAMESPACE: Final[str] = "eawf"

#: Namespace of an enforcement reference naming a registered gate kind.
GATE_REF_NAMESPACE: Final[str] = "gate"

#: Namespace of an enforcement reference naming a shipped lint rule.
LINT_REF_NAMESPACE: Final[str] = "lint"

# Layer precedence: a rule may only be superseded from a strictly higher layer.
_LAYER_RANK: Final[dict[RuleSourceKind, int]] = {"builtin": 0, "workspace": 1, "repository": 2}

_LINT_MODULE: Final[re.Pattern[str]] = re.compile(r"^(eawf\d{3})(?:_|$)")

# A restriction ("never grant", "must not push") is not a grant, so a match
# preceded by a negation is not a widening.
_NEGATED_TAIL: Final[re.Pattern[str]] = re.compile(
    r"\b(?:never|not|no|cannot|can't|don't|won't|mustn't|without)\s+(?:[\w'-]+\s+){0,2}$",
    re.IGNORECASE,
)

_SUBJECT = (
    r"(?:you|agents?|executors?|subagents?|reviewers?"
    r"|the\s+(?:agent|harness|executor|model|assistant))"
)
_PERMISSION = (
    r"(?:may|can|(?:is|are)\s+(?:now\s+)?(?:allowed|permitted|authori[sz]ed|free)\s+to)(?!\s+not\b)"
)
_ACTION = (
    r"(?:use|call|invoke|run|execute|access|install|enable|spawn|launch|write|delete|push"
    r"|deploy|publish|merge|release|fetch|download|connect|send|post|bypass)"
)
_MODEL_FAMILY = (
    r"(?:(?:claude[\s-]+)?(?:opus|sonnet|haiku|fable)(?:[\s-]*\d+(?:\.\d+)?)?"
    r"|gpt-?\d[\w.-]*|gemini[\w.-]*|o[134](?:-mini)?)"
)

# Each entry names the kind of authority the prose tries to declare. Model
# selection, budgets, concurrency and retries are typed runtime-policy leaves;
# tools, capabilities and effects are granted by the compiled run capsule.
_WIDENING_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("a tool or effect grant", rf"\b{_SUBJECT}\s+{_PERMISSION}\s+{_ACTION}\b"),
        (
            "a capability grant",
            r"\b(?:grants?|granting|granted|gives?|allows?|allowing|permits?|authori[sz]es?"
            r"|enables?)\s+(?:\S+\s+){0,4}?(?:access|use\s+of|tools?|permissions?"
            r"|capabilit(?:y|ies)|mcp|servers?|network|internet|filesystem|rpc|sudo|root)\b",
        ),
        (
            "a model selection",
            rf"\b(?:use|prefer|choose|select|switch\s+to|pin|default\s+to|run\s+(?:on|with)"
            rf"|dispatch\s+(?:on|with))\s+(?:the\s+)?(?:[\w-]+\s+){{0,2}}?{_MODEL_FAMILY}\b",
        ),
        ("a model selection", r"\bmodel\s*(?:selection|choice|preference)?\s*[:=]"),
        (
            "a budget widening",
            r"\b(?:raise|increase|extend|lift|double|triple|bump|remove|ignore|exceed|disable)"
            r"\s+(?:the\s+|any\s+|all\s+)?(?:[\w-]+\s+){0,2}?(?:budgets?|token\s+limits?"
            r"|spend(?:ing)?\s+limits?|cost\s+caps?|quotas?|timeouts?|caps?|limits?)\b",
        ),
        (
            "a budget widening",
            r"\b(?:unlimited|unbounded)\s+(?:[\w-]+\s+)?(?:budgets?|tokens?|spend|time|runs?"
            r"|retries|attempts?|agents?)\b|\bno\s+(?:budget|token|time|spend|cost)\s+(?:limit|cap)s?\b",
        ),
        (
            "a concurrency declaration",
            r"\b(?:up\s+to|as\s+many\s+as)\s+\d+\s+(?:[\w-]+\s+){0,2}?(?:in\s+parallel"
            r"|concurrently|subagents?|agents?|workers?|worktrees?|sessions?)\b",
        ),
        (
            "a concurrency declaration",
            r"\b(?:parallel(?:ism)?|concurrency)\s+(?:cap|limit|width)?\s*(?:to|=|:|of)\s*\d+",
        ),
        (
            "a retry declaration",
            r"\bretr(?:y|ies)\b(?:\s+\S+){0,5}?\s+(?:\d+|until|forever|indefinitely|unlimited)\b",
        ),
        ("a retry declaration", r"\b\d+\s+(?:times\s+)?retr(?:y|ies)\b"),
    )
)

_INTERPOLATION: Final[re.Pattern[str]] = re.compile(
    r"\$\{[^}]*\}|\$\([^)]*\)|\{\{[^}]*\}\}|\{%[^%]*%\}|<%[^%]*%>"
)

_INJECTION: Final[re.Pattern[str]] = re.compile(
    r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+)?(?:the\s+)?"
    r"(?:(?:previous|prior|above|earlier|other|system|developer|operator)\s+)+"
    r"(?:instructions?|rules?|prompts?|messages?|polic(?:y|ies))\b"
    r"|\bsystem\s+prompt\b|\byou\s+are\s+now\b|\b(?:developer|jailbreak|god)\s+mode\b"
    r"|</?(?:system|assistant|user|developer)>"
    r"|\b(?:overrides?|takes?\s+precedence\s+over)\s+(?:all|any|every)\s+(?:other\s+)?"
    r"(?:rules?|instructions?|polic(?:y|ies))\b",
    re.IGNORECASE,
)

_PROSE_FIELDS: Final[tuple[str, ...]] = ("title", "instruction", "rationale")

#: The projections each supported runtime loads at session start.
ProjectionReaders = Mapping[RuntimeName, frozenset[ProjectionKind]]

# The committed card compiles from committed sources only, so a rule from any
# other layer reaches the policy projection and never the card.
_COMMITTED_ROUTES: Final[frozenset[ProjectionKind]] = frozenset({"card", "policy"})
_LOCAL_ROUTES: Final[frozenset[ProjectionKind]] = frozenset({"policy"})


class RuleCompileError(ValueError):
    """Base class for a refusal raised while compiling the rule graph.

    Attributes:
        code: The stable failure code, so tooling matches on a code rather
            than parsing an English message.
    """

    code: ClassVar[str] = "rule_compile_error"


class RuleDuplicateOwnerError(RuleCompileError):
    """Two effective rules own the same obligation."""

    code: ClassVar[str] = "rule_duplicate_owner"


class RuleDuplicateIdError(RuleCompileError):
    """Two effective rules share one rule identifier."""

    code: ClassVar[str] = "rule_duplicate_id"


class RuleSupersessionError(RuleCompileError):
    """A supersession does not name an existing rule exactly."""

    code: ClassVar[str] = "rule_supersession_unresolved"


class RuleSupersessionLayerError(RuleCompileError):
    """A supersession comes from a layer not strictly above its target."""

    code: ClassVar[str] = "rule_supersession_layer"


class RuleProtectedError(RuleCompileError):
    """A supersession targets a protected rule."""

    code: ClassVar[str] = "rule_protected"


class RuleCompetingWriterError(RuleCompileError):
    """Two rules supersede the same target."""

    code: ClassVar[str] = "rule_competing_writer"


class RuleIdentityShadowError(RuleCompileError):
    """A non-builtin source claims an identifier in the builtin namespace."""

    code: ClassVar[str] = "rule_identity_shadow"


class RuleEnforcementError(RuleCompileError):
    """A verification or enforcement reference does not resolve."""

    code: ClassVar[str] = "rule_enforcement_unregistered"


class RuleAuthorityWideningError(RuleCompileError):
    """Rule prose declares or widens runtime authority."""

    code: ClassVar[str] = "rule_authority_widening"


class RuleInterpolationError(RuleCompileError):
    """Rule prose carries executable interpolation."""

    code: ClassVar[str] = "rule_interpolation"


class RuleAuthorityInjectionError(RuleCompileError):
    """Rule prose tries to override the instruction hierarchy."""

    code: ClassVar[str] = "rule_authority_injection"


class RuleMustUndeliveredError(RuleCompileError):
    """A ``must`` rule reaches no runtime deterministically, or not every one."""

    code: ClassVar[str] = "rule_must_undelivered"


class RuleRoleUndeliveredError(RuleMustUndeliveredError):
    """A role-scoped ``must`` rule is embedded in no rendered agent definition."""

    code: ClassVar[str] = "rule_role_undelivered"


class CompiledRule(RuleModel):
    """One rule in the effective graph.

    Attributes:
        record: The rule with the source it was read from.
        digest: The rule's content digest, the value a supersession names.
        protected: True for a constitution rule that resolved as builtin; no
            other layer may supersede it.
    """

    record: RuleRecord
    digest: Sha256DigestStr
    protected: bool


class SupersessionEdge(RuleModel):
    """One applied supersession, kept as provenance.

    Attributes:
        superseding_rule_id: The rule that replaced the prior revision.
        superseded_rule_id: The replaced rule's identifier.
        superseded_revision: The replaced revision.
        superseded_digest: The replaced rule's content digest.
        decision_ref: The decision that authorised it, when one was named.
    """

    superseding_rule_id: QualifiedId
    superseded_rule_id: QualifiedId
    superseded_revision: int
    superseded_digest: Sha256DigestStr
    decision_ref: str | None


class RuleGraph(RuleModel):
    """The effective rule graph with its digest.

    Attributes:
        digest: Digest of the effective content: every effective rule's
            digest and provenance plus the selected modules. Independent of
            input order and of source bytes that carry no rule content.
        rules: Effective rules, builtin before workspace before repository,
            then by identifier. The order is canonical, not a precedence.
        supersessions: Applied supersessions in the same canonical order.
        modules: Selected builtin module references, sorted and unique.
        sources: Every source a compiled record was read from, sorted.
    """

    digest: Sha256DigestStr
    rules: tuple[CompiledRule, ...]
    supersessions: tuple[SupersessionEdge, ...]
    modules: tuple[QualifiedId, ...]
    sources: tuple[RuleSourceIdentity, ...]


def rule_digest(rule: AuthoredRule) -> str:
    """Return the content digest a supersession of ``rule`` must name.

    The digest covers the authored fields only, so a rule keeps its digest
    wherever it is read from.

    Args:
        rule: An authored rule or a rule record.

    Returns:
        ``sha256:`` followed by the hex digest of the canonical JSON form.
    """
    body = rule.model_dump(mode="json", exclude={"source"})
    return _sha256(body)


def registered_enforcement_refs() -> frozenset[str]:
    """Return every enforcement reference a rule may resolve against.

    A reference is ``gate.<kind>`` for each registered gate kind, or
    ``lint.<code>`` (such as ``lint.eawf012``) for each shipped lint rule
    module.

    Returns:
        The frozenset of resolvable references.
    """
    # Imported here because the audit-DSL registry pulls the verify spine,
    # which a caller that only reads rule records should not pay for.
    import eawf.platform.lint as lint_package
    import eawf.platform.lint.tools as lint_tools_package
    from eawf.workflow.audit_dsl.register_lint import known_gate_kinds

    gates = {f"{GATE_REF_NAMESPACE}.{kind}" for kind in known_gate_kinds()}
    lints = {
        f"{LINT_REF_NAMESPACE}.{match.group(1)}"
        for package in (lint_package, lint_tools_package)
        for info in pkgutil.iter_modules(package.__path__)
        if (match := _LINT_MODULE.match(info.name))
    }
    return frozenset(gates | lints)


def registered_projection_readers() -> dict[RuntimeName, frozenset[ProjectionKind]]:
    """Return which projections each supported runtime loads, from the host facts.

    Returns:
        Every supported runtime mapped to the projections it reads.

    Raises:
        HostFactError: When the shipped host-fact record is untrustworthy.
    """
    # Imported here because the host-fact loader pulls runtime certification,
    # which a caller that only reads rule records should not pay for.
    from eawf.platform.rules.host_facts import load_host_facts

    return {record.runtime: frozenset(record.reads) for record in load_host_facts().records}


@cache
def registered_agent_roles() -> frozenset[str]:
    """Return every role a rendered agent definition exists for.

    Every runtime's agent definitions and the dispatch role registry are
    rendered from one agent registry, so it is the set of roles a role rule
    can reach.

    Returns:
        The roles of the agent registry.
    """
    # Imported here because the agent registry imports the carrier renderer,
    # which imports this module.
    from eawf.surfaces.render.agents import AGENT_REGISTRY

    return frozenset(spec.role for spec in AGENT_REGISTRY)


def compile_rule_graph(
    repo_root: Path,
    *,
    builtin_rules: BuiltinRuleProvider = no_builtin_rules,
    home: Path | None = None,
) -> RuleGraph:
    """Resolve the effective rule graph for a repository across every layer.

    This is the one resolver for generated policy: every consumer that needs
    policy calls it rather than reading a rule source itself. The result
    includes the machine-local workspace layer, so it must never be rendered
    into a committed file; use :func:`compile_card_graph` for that.

    Args:
        repo_root: The repository root holding ``.ea/rules.yaml``.
        builtin_rules: Resolves the selected module references to builtin
            records.
        home: The directory holding the ``.eawf`` home that registers the
            workspace; ``None`` for the user's home directory.

    Returns:
        The compiled, digest-stamped graph.

    Raises:
        RuleSourceError: When a rule source fails to load or resolve.
        RuleCompileError: When the composed rules fail compilation.
    """
    layers = load_rule_layers(repo_root, builtin_rules=builtin_rules, home=home)
    graph = compile_rule_records(
        layers.records(),
        modules=layers.modules,
        enforcement_refs=registered_enforcement_refs(),
        projection_readers=registered_projection_readers(),
    )
    logger.info(
        f"rule graph compiled digest={graph.digest} rules={len(graph.rules)} "
        f"supersessions={len(graph.supersessions)}"
    )
    return graph


def compile_card_graph(
    repo_root: Path,
    *,
    builtin_rules: BuiltinRuleProvider = no_builtin_rules,
) -> RuleGraph:
    """Resolve the rule graph the committed project card renders from.

    Only committed inputs contribute: the builtin and repository layers.
    The registry and any workspace source are never read, so every machine
    renders the same card from the same commit.

    Args:
        repo_root: The repository root holding ``.ea/rules.yaml``.
        builtin_rules: Resolves the selected module references to builtin
            records.

    Returns:
        The compiled, digest-stamped committed graph.

    Raises:
        RuleSourceError: When the repository source fails to load.
        RuleCompileError: When the committed rules fail compilation.
    """
    layers = load_rule_layers(repo_root, builtin_rules=builtin_rules, workspace=False)
    return compile_committed_records(
        layers.committed_records(),
        modules=layers.modules,
        enforcement_refs=registered_enforcement_refs(),
        projection_readers=registered_projection_readers(),
    )


def compile_committed_records(
    records: Iterable[RuleRecord],
    *,
    modules: Iterable[str],
    enforcement_refs: frozenset[str],
    projection_readers: ProjectionReaders,
) -> RuleGraph:
    """Compile records for a committed projection, refusing machine-local input.

    Args:
        records: Records from committed sources only.
        modules: Selected builtin module references.
        enforcement_refs: The resolvable enforcement references.
        projection_readers: The projections each supported runtime loads.

    Returns:
        The effective committed graph.

    Raises:
        RuleCommittedInputError: When a record resolved from a machine-local
            source.
        RuleCompileError: See :func:`compile_rule_records`.
    """
    return compile_rule_records(
        require_committed_inputs(records),
        modules=modules,
        enforcement_refs=enforcement_refs,
        projection_readers=projection_readers,
    )


def compile_rule_records(
    records: Iterable[RuleRecord],
    *,
    modules: Iterable[str],
    enforcement_refs: frozenset[str],
    projection_readers: ProjectionReaders,
) -> RuleGraph:
    """Compile rule records from any mix of layers into the effective graph.

    Input order carries no meaning: records are put in a canonical order
    before anything is checked, so the same records in any order yield the
    same graph, the same digest and the same first refusal.

    Args:
        records: Rule records from every contributing source.
        modules: Selected builtin module references.
        enforcement_refs: The references a verification or enforcement may
            resolve against; production passes
            :func:`registered_enforcement_refs`.
        projection_readers: The projections each supported runtime loads;
            production passes :func:`registered_projection_readers`.

    Returns:
        The effective graph.

    Raises:
        ValueError: When ``projection_readers`` names no runtime, which
            would let every ``must`` rule pass the delivery check vacuously.
        RuleInterpolationError: When prose carries executable interpolation.
        RuleAuthorityInjectionError: When prose overrides the instruction
            hierarchy.
        RuleAuthorityWideningError: When prose declares runtime authority.
        RuleIdentityShadowError: When a non-builtin rule uses the builtin
            namespace.
        RuleEnforcementError: When a reference does not resolve.
        RuleDuplicateIdError: When an identifier revision is repeated or two
            effective rules share an identifier.
        RuleSupersessionError: When a supersession names no exact rule.
        RuleSupersessionLayerError: When a supersession is not from a
            strictly higher layer.
        RuleProtectedError: When a supersession targets a protected rule.
        RuleCompetingWriterError: When two rules supersede one target.
        RuleDuplicateOwnerError: When two effective rules own one obligation.
        RuleMustUndeliveredError: When an effective ``must`` rule has no
            delivery to every supported runtime.
    """
    if not projection_readers:
        raise ValueError("projection_readers names no runtime; the delivery check needs one")
    compiled = sorted(
        (_compile_one(record, enforcement_refs=enforcement_refs) for record in records),
        key=_canonical_key,
    )
    by_exact: dict[tuple[str, int, str], CompiledRule] = {}
    for rule in compiled:
        key = (rule.record.rule_id, rule.record.revision, rule.digest)
        if key in by_exact:
            raise RuleDuplicateIdError(
                f"{rule.record.rule_id} revision {rule.record.revision} is supplied twice "
                f"({_where(by_exact[key])}, {_where(rule)})"
            )
        by_exact[key] = rule
    edges = _apply_supersessions(compiled, by_exact)
    superseded = {(e.superseded_rule_id, e.superseded_revision, e.superseded_digest) for e in edges}
    effective = tuple(
        rule
        for rule in compiled
        if (rule.record.rule_id, rule.record.revision, rule.digest) not in superseded
    )
    _require_single_owner(effective)
    for rule in effective:
        _require_must_delivery(rule.record, projection_readers)
    selected = tuple(sorted(set(modules)))
    digest = _sha256(
        {
            "modules": list(selected),
            "rules": [
                {
                    "digest": rule.digest,
                    "source_kind": rule.record.source.kind,
                    "source_locator": rule.record.source.locator,
                }
                for rule in effective
            ],
        }
    )
    sources = sorted(
        {rule.record.source for rule in compiled},
        key=lambda source: (_LAYER_RANK[source.kind], source.locator, source.digest),
    )
    return RuleGraph(
        digest=digest,
        rules=effective,
        supersessions=edges,
        modules=selected,
        sources=tuple(sources),
    )


def _compile_one(record: RuleRecord, *, enforcement_refs: frozenset[str]) -> CompiledRule:
    """Validate one record in isolation and stamp its digest.

    Args:
        record: The record to check.
        enforcement_refs: The resolvable enforcement references.

    Returns:
        The compiled rule.

    Raises:
        RuleInterpolationError: See :func:`_screen_prose`.
        RuleAuthorityInjectionError: See :func:`_screen_prose`.
        RuleAuthorityWideningError: See :func:`_screen_prose`.
        RuleIdentityShadowError: When a non-builtin rule uses the builtin
            namespace.
        RuleEnforcementError: See :func:`_check_enforcement`.
    """
    _screen_prose(record)
    namespace = record.rule_id.split(".", 1)[0]
    if namespace == BUILTIN_NAMESPACE and record.source.kind != "builtin":
        raise RuleIdentityShadowError(
            f"{record.rule_id}: the {BUILTIN_NAMESPACE!r} namespace is reserved for builtin "
            f"rules, but this rule resolved from a {record.source.kind} source "
            f"({record.source.locator})"
        )
    _check_enforcement(record, enforcement_refs)
    return CompiledRule(
        record=record,
        digest=rule_digest(record),
        protected=record.source.kind == "builtin" and record.zone == "constitution",
    )


def _screen_prose(record: RuleRecord) -> None:
    """Refuse prose that executes, injects authority or widens authority.

    Args:
        record: The record whose title, instruction and rationale are read.

    Raises:
        RuleInterpolationError: On executable or template interpolation.
        RuleAuthorityInjectionError: On an attempt to override the
            instruction hierarchy.
        RuleAuthorityWideningError: On a grant or a runtime-policy
            declaration.
    """
    for field in _PROSE_FIELDS:
        text = getattr(record, field)
        if text is None:
            continue
        where = f"{record.rule_id}.{field}"
        interpolation = _INTERPOLATION.search(text)
        if interpolation is not None:
            raise RuleInterpolationError(
                f"{where}: executable interpolation {interpolation.group(0)!r} is refused"
            )
        injection = _affirmative_match(_INJECTION, text)
        if injection is not None:
            raise RuleAuthorityInjectionError(
                f"{where}: authority language {injection.group(0)!r} is refused"
            )
        for label, pattern in _WIDENING_PATTERNS:
            widening = _affirmative_match(pattern, text)
            if widening is not None:
                raise RuleAuthorityWideningError(
                    f"{where}: {widening.group(0)!r} reads as {label}; rule prose cannot "
                    f"declare or widen runtime authority, which comes from typed policy"
                )


def _affirmative_match(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    """Return the first match of ``pattern`` that no negation precedes.

    Args:
        pattern: The compiled pattern.
        text: The prose to search.

    Returns:
        The first affirmative match, or ``None``.
    """
    for match in pattern.finditer(text):
        if _NEGATED_TAIL.search(text[max(0, match.start() - 40) : match.start()]) is None:
            return match
    return None


def _check_enforcement(record: RuleRecord, enforcement_refs: frozenset[str]) -> None:
    """Require every verification and enforcement reference to resolve.

    Args:
        record: The record to check.
        enforcement_refs: The resolvable references.

    Raises:
        RuleEnforcementError: When a ``registered_check`` rule names no
            check, or a named check or enforcement reference is unregistered.
    """
    verification = record.verification
    if verification.method == "registered_check" and not verification.check_refs:
        raise RuleEnforcementError(
            f"{record.rule_id}: a registered_check verification names no check_refs"
        )
    named = [*verification.check_refs]
    if record.enforcement_ref is not None:
        named.append(record.enforcement_ref)
    unresolved = [ref for ref in named if ref not in enforcement_refs]
    if unresolved:
        raise RuleEnforcementError(
            f"{record.rule_id}: {', '.join(unresolved)} is not a registered gate kind "
            f"({GATE_REF_NAMESPACE}.<kind>) or lint rule ({LINT_REF_NAMESPACE}.<code>)"
        )


def _require_must_delivery(record: RuleRecord, readers: ProjectionReaders) -> None:
    """Refuse a ``must`` rule that some supported runtime never receives.

    A ``must`` rule is delivered when a registered enforcement refuses the
    violation, when every rendered agent definition of one of its roles
    embeds it, or when a projection renders it that every supported runtime
    loads. The card renders every committed non-role ``must`` rule and the
    policy projection every non-role ``must`` rule; a module view is never a
    delivery, because it depends on the model electing to read it.

    Args:
        record: An effective rule.
        readers: The projections each supported runtime loads.

    Raises:
        RuleRoleUndeliveredError: When the rule is scoped to roles and no
            rendered agent definition embeds it.
        RuleMustUndeliveredError: When the rule has none of those routes.
    """
    if record.force != "must" or record.enforcement_ref is not None:
        return
    if record.scope.roles:
        _require_role_delivery(record)
        return
    remedy = (
        "make it an unscoped constitution rule, back it with a registered enforcement_ref, "
        "scope it to a role (builtin rules only), or lower its force to should"
    )
    if record.zone == "retrievable":
        raise RuleMustUndeliveredError(
            f"{record.rule_id}: a retrievable must rule reaches a session only through a "
            f"module view a model may never read; {remedy}"
        )
    routes = _COMMITTED_ROUTES if record.source.kind in COMMITTED_SOURCE_KINDS else _LOCAL_ROUTES
    missed = sorted(runtime for runtime, reads in readers.items() if not reads & routes)
    if missed:
        raise RuleMustUndeliveredError(
            f"{record.rule_id}: a {record.source.kind} must rule is rendered only into "
            f"{', '.join(sorted(routes))}, which {', '.join(missed)} never loads; {remedy}"
        )


def _require_role_delivery(record: RuleRecord) -> None:
    """Refuse a role-scoped ``must`` rule no rendered agent definition embeds.

    Agent definitions are rendered without a repository graph, so they embed
    the role rules of the builtin modules that bind every repository: a
    catalog module applies only where it is selected, and a workspace or
    repository rule exists only in the graph of the repository that authors
    it. The rule must also name a role an agent definition exists for.

    Args:
        record: An effective role-scoped ``must`` rule without enforcement.

    Raises:
        RuleRoleUndeliveredError: When the rule is not a builtin rule that
            binds every repository, or names no role with an agent
            definition.
    """
    remedy = "back it with a registered enforcement_ref or lower its force to should"
    agent_roles = registered_agent_roles()
    if not agent_roles.intersection(record.scope.roles):
        raise RuleRoleUndeliveredError(
            f"{record.rule_id}: scoped only to {', '.join(record.scope.roles)}, which no "
            f"rendered agent definition exists for (agents: {', '.join(sorted(agent_roles))}); "
            f"{remedy}"
        )
    module = builtin_rule_modules().get(record.source.locator.partition("@")[0])
    catalog = module is not None and module.document.kind == "catalog"
    if record.source.kind != "builtin" or catalog:
        origin = "a catalog module" if catalog else f"the {record.source.kind} layer"
        raise RuleRoleUndeliveredError(
            f"{record.rule_id}: a role-scoped must rule from {origin} is embedded in no "
            f"agent definition, which carry only the builtin role rules every repository "
            f"applies; {remedy}"
        )


def _apply_supersessions(
    compiled: list[CompiledRule],
    by_exact: dict[tuple[str, int, str], CompiledRule],
) -> tuple[SupersessionEdge, ...]:
    """Resolve every declared supersession against the compiled records.

    Args:
        compiled: The records in canonical order.
        by_exact: Each record keyed by identifier, revision and digest.

    Returns:
        The applied supersessions in canonical order.

    Raises:
        RuleSupersessionError: When a supersession names no exact record.
        RuleSupersessionLayerError: When the superseding layer is not
            strictly above the target's layer.
        RuleProtectedError: When the target is protected.
        RuleCompetingWriterError: When a target is superseded twice.
    """
    writers: dict[tuple[str, int, str], CompiledRule] = {}
    edges: list[SupersessionEdge] = []
    for rule in compiled:
        for claim in rule.record.supersedes:
            key = (claim.rule_id, claim.revision, claim.digest)
            target = by_exact.get(key)
            if target is None:
                raise RuleSupersessionError(
                    f"{rule.record.rule_id} supersedes {claim.rule_id} revision "
                    f"{claim.revision} at {claim.digest}, which no compiled rule matches exactly"
                )
            if _LAYER_RANK[rule.record.source.kind] <= _LAYER_RANK[target.record.source.kind]:
                raise RuleSupersessionLayerError(
                    f"{rule.record.rule_id} ({rule.record.source.kind}) cannot supersede "
                    f"{claim.rule_id} ({target.record.source.kind}); only a strictly higher "
                    f"layer may supersede"
                )
            if target.protected:
                raise RuleProtectedError(
                    f"{rule.record.rule_id} cannot supersede {claim.rule_id}: a builtin "
                    f"constitution rule is protected"
                )
            if key in writers:
                raise RuleCompetingWriterError(
                    f"{writers[key].record.rule_id} and {rule.record.rule_id} both supersede "
                    f"{claim.rule_id} revision {claim.revision}"
                )
            writers[key] = rule
            edges.append(
                SupersessionEdge(
                    superseding_rule_id=rule.record.rule_id,
                    superseded_rule_id=claim.rule_id,
                    superseded_revision=claim.revision,
                    superseded_digest=claim.digest,
                    decision_ref=claim.decision_ref,
                )
            )
    return tuple(edges)


def _require_single_owner(effective: tuple[CompiledRule, ...]) -> None:
    """Require one effective rule per identifier and per obligation.

    Args:
        effective: The rules left after supersession.

    Raises:
        RuleDuplicateIdError: When two effective rules share an identifier.
        RuleDuplicateOwnerError: When two effective rules own one obligation.
    """
    ids: dict[str, CompiledRule] = {}
    owners: dict[str, CompiledRule] = {}
    for rule in effective:
        prior_id = ids.setdefault(rule.record.rule_id, rule)
        if prior_id is not rule:
            raise RuleDuplicateIdError(
                f"{rule.record.rule_id} is defined by {_where(prior_id)} and {_where(rule)} "
                f"without an exact supersession"
            )
        prior_owner = owners.setdefault(rule.record.obligation_id, rule)
        if prior_owner is not rule:
            raise RuleDuplicateOwnerError(
                f"obligation {rule.record.obligation_id!r} is owned by both "
                f"{prior_owner.record.rule_id} ({_where(prior_owner)}) and "
                f"{rule.record.rule_id} ({_where(rule)}); one rule must supersede the other"
            )


def _canonical_key(rule: CompiledRule) -> tuple[int, str, int, str]:
    """Return the order-independent sort key of a compiled rule.

    Args:
        rule: The compiled rule.

    Returns:
        Layer rank, identifier, revision and digest.
    """
    record = rule.record
    return (_LAYER_RANK[record.source.kind], record.rule_id, record.revision, rule.digest)


def _where(rule: CompiledRule) -> str:
    """Name a rule's source for a refusal message.

    Args:
        rule: The compiled rule.

    Returns:
        ``<kind>:<locator>``.
    """
    return f"{rule.record.source.kind}:{rule.record.source.locator}"


def _sha256(body: object) -> str:
    """Digest a JSON-serialisable body in canonical form.

    Args:
        body: The value to digest.

    Returns:
        ``sha256:`` followed by the hex digest.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


__all__ = [
    "BUILTIN_NAMESPACE",
    "GATE_REF_NAMESPACE",
    "LINT_REF_NAMESPACE",
    "CompiledRule",
    "ProjectionReaders",
    "RuleAuthorityInjectionError",
    "RuleAuthorityWideningError",
    "RuleCompetingWriterError",
    "RuleCompileError",
    "RuleDuplicateIdError",
    "RuleDuplicateOwnerError",
    "RuleEnforcementError",
    "RuleGraph",
    "RuleIdentityShadowError",
    "RuleInterpolationError",
    "RuleMustUndeliveredError",
    "RuleProtectedError",
    "RuleRoleUndeliveredError",
    "RuleSupersessionError",
    "RuleSupersessionLayerError",
    "SupersessionEdge",
    "compile_card_graph",
    "compile_committed_records",
    "compile_rule_graph",
    "compile_rule_records",
    "registered_agent_roles",
    "registered_enforcement_refs",
    "registered_projection_readers",
    "rule_digest",
]
