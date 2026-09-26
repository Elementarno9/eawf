"""Strict frozen records for repository-authored rules.

A rule is one normative obligation an interactive harness must honour. The
record keeps the instruction, the rationale, the procedure reference and the
verification in separate typed fields so a projection can render one compact
line per rule while a detailed view carries the full triad, and so no field
doubles as another.

Two shapes live here:

- :class:`AuthoredRule` is what a rule source spells out. It has no ``source``
  field: where a rule came from is established by the loader that read it,
  never self-declared, because trust binds the resolved source.
- :class:`RuleRecord` is an authored rule stamped with the
  :class:`RuleSourceIdentity` it was read from. It is the unit every later
  consumer (graph compilation, layer composition, rendering) accepts.

Every model is ``extra="forbid"`` and ``frozen=True``; collections are tuples
so a record is hashable and cannot be mutated after load.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
    model_validator,
)

from eawf.kernel.spec.release import Sha256DigestStr

#: The namespace every repository-authored rule identifier lives under, so a
#: repository rule can never collide with a builtin or workspace identifier.
REPOSITORY_NAMESPACE = "repo"

#: The namespace every workspace-authored rule identifier lives under, so a
#: workspace rule can never collide with a builtin or repository identifier.
WORKSPACE_NAMESPACE = "workspace"

#: The prefix that marks a ``procedure_ref`` as a registered locator rather
#: than a repository path.
REGISTERED_LOCATOR_PREFIX = "ref:"

#: A namespace-qualified identifier: at least two dot-separated segments, the
#: first naming the namespace (``repo.commit-prefix``, ``eawf.core.deletion``).
QualifiedId = Annotated[
    str,
    StringConstraints(
        strict=True,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_-]*)+$",
    ),
]

#: The semantic identifier of the concept a rule governs. One effective graph
#: has exactly one owner per obligation identifier.
ObligationId = Annotated[
    str,
    StringConstraints(
        strict=True,
        max_length=128,
        pattern=r"^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)*$",
    ),
]

#: A closed lowercase selector token naming an activity or a role.
SelectorToken = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_-]{0,63}$")]

#: A repository path or path glob. Confinement under the repository root is a
#: filesystem question, so the loader checks it, not the schema.
RepoPathStr = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]

#: A registered workspace key, the same shape as a project code.
WorkspaceKeyStr = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9_-]{1,15}$")]

RuleZone = Literal["constitution", "steering", "retrievable"]
RuleForce = Literal["must", "should", "information"]
RuleAudience = Literal["interactive_harness"]
RuleEffectiveness = Literal["informational", "behavioral", "guarded"]
VerificationMethod = Literal["review", "registered_check", "structural"]
RuleSourceKind = Literal["builtin", "workspace", "repository"]


def _refuse_trailing_period(title: str) -> str:
    """Enforce the entity-title rule that a title never ends in a period.

    Args:
        title: The already length-bounded title.

    Returns:
        ``title`` unchanged.

    Raises:
        ValueError: When ``title`` ends with a period.
    """
    if title.endswith("."):
        raise ValueError("a rule title must not end with a period")
    return title


RuleTitle = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=72),
    AfterValidator(_refuse_trailing_period),
]

#: The direct obligation. Bounded so a projection line stays compact.
InstructionStr = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=320),
]

RationaleStr = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=2000),
]


class RuleModel(BaseModel):
    """Base of every rule-source model: closed and immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RuleScope(RuleModel):
    """Where a rule applies. Empty on every axis means the rule is unscoped.

    Attributes:
        paths: Repository paths or path globs the rule governs.
        activities: Activities the rule binds, such as ``review``.
        roles: Agent roles the rule binds, such as ``executor``.
    """

    paths: tuple[RepoPathStr, ...] = ()
    activities: tuple[SelectorToken, ...] = ()
    roles: tuple[SelectorToken, ...] = ()


class VerificationSpec(RuleModel):
    """How compliance with a rule is established.

    Attributes:
        method: ``review`` (a reader judges), ``registered_check`` (a named
            check runs) or ``structural`` (a mutator refuses the violation).
        check_refs: Registered check references backing ``registered_check``.
            Whether they resolve is a compilation verdict.
    """

    method: VerificationMethod
    check_refs: tuple[QualifiedId, ...] = ()


class RuleSupersession(RuleModel):
    """An exact claim that a rule replaces one prior rule revision.

    Attributes:
        rule_id: The superseded rule's qualified identifier.
        revision: The superseded revision.
        digest: The superseded rule's content digest.
        decision_ref: The decision that authorised the supersession, when one
            is required.
    """

    rule_id: QualifiedId
    revision: PositiveInt
    digest: Sha256DigestStr
    decision_ref: (
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)] | None
    ) = None


class AuthoredRule(RuleModel):
    """One rule as a rule source spells it out.

    Attributes:
        rule_id: Globally namespace-qualified identifier.
        obligation_id: The concept the rule governs.
        revision: Bound into supersession records.
        title: At most 72 characters, no trailing period.
        zone: Which projection carries the rule.
        force: Normative strength.
        audience: Closed to the interactive harness; a rule never addresses a
            managed run.
        effectiveness: ``authoritative`` is absent on purpose: prose grants no
            runtime authority.
        instruction: The direct obligation.
        rationale: Why the obligation exists.
        scope: Path, activity and role selectors.
        verification: How compliance is established.
        enforcement_ref: The registered structural enforcement, required when
            the verification method is ``structural``.
        procedure_ref: A repository path, or a registered locator prefixed
            ``ref:``; never a URL or an executable.
        supersedes: Exact supersession records.
    """

    rule_id: QualifiedId
    obligation_id: ObligationId
    revision: PositiveInt
    title: RuleTitle
    zone: RuleZone
    force: RuleForce
    audience: RuleAudience = "interactive_harness"
    effectiveness: RuleEffectiveness
    instruction: InstructionStr
    rationale: RationaleStr | None = None
    scope: RuleScope = Field(default_factory=RuleScope)
    verification: VerificationSpec
    enforcement_ref: QualifiedId | None = None
    procedure_ref: RepoPathStr | None = None
    supersedes: tuple[RuleSupersession, ...] = ()

    @model_validator(mode="after")
    def _structural_needs_enforcement(self) -> AuthoredRule:
        """Refuse a structural rule that names no enforcement.

        Returns:
            The validated rule.

        Raises:
            ValueError: When ``verification.method`` is ``structural`` and
                ``enforcement_ref`` is missing.
        """
        if self.verification.method == "structural" and self.enforcement_ref is None:
            raise ValueError("a structural verification requires an enforcement_ref")
        return self


class RuleSourceIdentity(RuleModel):
    """The resolved source a set of rules was read from.

    Attributes:
        kind: The layer the source belongs to.
        locator: Repository-relative path of the source file, or the
            registered reference of a builtin module.
        digest: Digest of the exact bytes that were read.
    """

    kind: RuleSourceKind
    locator: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]
    digest: Sha256DigestStr


class RuleRecord(AuthoredRule):
    """An authored rule stamped with the source it was read from.

    Attributes:
        source: The resolved source identity.
    """

    source: RuleSourceIdentity


class WorkspaceRuleRef(RuleModel):
    """The repository's explicit reference to a workspace rule source.

    The workspace layer is never discovered: a repository names the
    registered workspace it inherits from and pins the exact bytes it
    accepted, so a changed workspace file is refused until the repository
    re-pins it.

    Attributes:
        key: The registered workspace key.
        digest: Digest of the workspace rule source bytes the repository
            accepted.
    """

    key: WorkspaceKeyStr
    digest: Sha256DigestStr


class RuleSourceDocument(RuleModel):
    """The on-disk shape of the repository rule source.

    Attributes:
        schema_version: Gates unknown future formats.
        workspace: The workspace rule source the repository inherits, or
            ``None`` for no workspace layer.
        modules: Registered references of the builtin rule modules the
            repository selects. Selection only; module prose is never copied.
        rules: Repository-authored rules.
    """

    schema_version: Literal[1]
    workspace: WorkspaceRuleRef | None = None
    modules: tuple[QualifiedId, ...] = ()
    rules: tuple[AuthoredRule, ...] = ()


class WorkspaceRuleDocument(RuleModel):
    """The on-disk shape of a workspace rule source.

    Module selection stays a repository decision, so a workspace source
    carries rules only.

    Attributes:
        schema_version: Gates unknown future formats.
        rules: Workspace-authored rules.
    """

    schema_version: Literal[1]
    rules: tuple[AuthoredRule, ...] = ()


class LoadedRuleSource(RuleModel):
    """The repository rule source after a successful load.

    Attributes:
        source: The resolved identity of the file that was read.
        workspace: The workspace rule source the repository names, if any.
        modules: Selected builtin module references, in authored order.
        rules: Repository rules stamped with ``source``, in authored order.
            Order carries no precedence.
    """

    source: RuleSourceIdentity
    workspace: WorkspaceRuleRef | None = None
    modules: tuple[QualifiedId, ...]
    rules: tuple[RuleRecord, ...]


__all__ = [
    "REGISTERED_LOCATOR_PREFIX",
    "REPOSITORY_NAMESPACE",
    "WORKSPACE_NAMESPACE",
    "AuthoredRule",
    "LoadedRuleSource",
    "RuleRecord",
    "RuleScope",
    "RuleSourceDocument",
    "RuleSourceIdentity",
    "RuleSupersession",
    "VerificationSpec",
    "WorkspaceRuleDocument",
    "WorkspaceRuleRef",
]
