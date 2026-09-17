"""The authority capsule one Run is dispatched under.

:class:`AuthorityCapsule` is small enforcement metadata, not a prose
constitution: it names the Run, its scope, its role, its authority per
axis, the semantic tools it may call, its budget ceilings, the digests it
must echo, and the conditions that stop it. Everything in it is a fact a
broker can check; nothing in it is advice.

The capsule is frozen and digest-bound. ``contract_digest`` is recomputed
on every validation over every other field, so a capsule edited after
sealing fails to load rather than running under authority nobody granted,
and any change to a grant, a denial, or a budget ceiling moves the digest
the provider process echoes back.

Tool grants are resolved against the closed catalog in
:mod:`eawf.kernel.runtime.semantic`. A grant that names no catalog tool
is refused at the boundary, and an empty grant list means the Run reaches
no semantic tool at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import Field, StrictInt, model_validator

from eawf.kernel.identity import EntityKind
from eawf.kernel.runtime.compiled import canonical_digest
from eawf.kernel.runtime.provider import (
    AuthorityGrant,
    Digest,
    FilesystemPolicyUrn,
    NetworkPolicyUrn,
    RuntimeRecord,
    SchemaUrn,
    UniqueToolIds,
)
from eawf.kernel.runtime.semantic import SEMANTIC_TOOL_CATALOG, SemanticToolId
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2.run import MUTATING_PURPOSES, RunPurpose
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, RunUrn

#: Stands in for the digest :meth:`AuthorityCapsule.seal` computes.
UNSEALED_DIGEST: Final = f"sha256:{'0' * 64}"


class StopCondition(StrEnum):
    """When a Run stops of its own accord and hands back.

    Each member is a condition an agent can observe about its own
    situation. Running on past one of them is the failure mode the
    capsule exists to make impossible to mistake for progress.
    """

    SCOPE_AMBIGUITY = "scope_ambiguity"
    APPROVAL_REQUIRED = "approval_required"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONTRACT_MISMATCH = "contract_mismatch"
    QUARANTINE = "quarantine"


class CapsuleBudget(RuntimeRecord):
    """The immutable ceilings of one Run.

    Remaining budget is live and read through ``budget_status``; only the
    ceiling belongs in the capsule, because a number that moved would
    move the contract digest under a running provider.
    """

    tokens: Annotated[StrictInt, Field(ge=1)] | None = None
    cost_microusd: Annotated[StrictInt, Field(ge=0)] | None = None
    wall_seconds: Annotated[StrictInt, Field(ge=1, le=86_400)]
    output_bytes: Annotated[StrictInt, Field(ge=1, le=16_777_216)]
    child_runs: Annotated[StrictInt, Field(ge=0, le=32)] = 0
    concurrency: Annotated[StrictInt, Field(ge=1, le=32)] = 1


class _CapsuleContent(RuntimeRecord):
    """Every capsule field and every rule except the digest check.

    :meth:`AuthorityCapsule.seal` validates content through this class
    first, so the digest is computed over validated values without a
    switch that could skip verification on a real capsule.
    """

    protocol_version: Literal["agent-run/v1"] = "agent-run/v1"
    run_ref: RunUrn
    parent_run_ref: RunUrn | None = None
    scope_ref: AnyEntityUrn
    scope_digest: Digest
    agent_role: AgentSessionRole
    purpose: RunPurpose
    authority: AuthorityGrant
    tool_grants: UniqueToolIds = ()
    tool_denials: UniqueToolIds = ()
    filesystem_policy_ref: FilesystemPolicyUrn | None = None
    network_policy_ref: NetworkPolicyUrn | None = None
    budget: CapsuleBudget
    criteria_digest: Digest
    policy_digest: Digest
    compiled_spec_digest: Digest
    report_schema_ref: SchemaUrn
    stop_conditions: Annotated[tuple[StopCondition, ...], Field(min_length=1)]
    contract_digest: Digest

    @property
    def semantic_tools(self) -> tuple[SemanticToolId, ...]:
        """The tools this capsule actually reaches, in grant order.

        A denial wins over a grant, so the resolved set is the grants
        minus the denials. An empty grant list yields no tools at all:
        the capsule is a positive allow-list, never a starting point a
        provider can widen.
        """
        denied = frozenset(self.tool_denials)
        return tuple(
            SemanticToolId(tool_id) for tool_id in self.tool_grants if tool_id not in denied
        )

    def contract_payload(self) -> dict[str, Any]:
        """Return the JSON form ``contract_digest`` covers: all but itself."""
        return self.model_dump(mode="json", exclude={"contract_digest"})

    @model_validator(mode="after")
    def _tools_are_catalog_tools_the_scope_admits(self) -> Self:
        """Resolve every grant and denial against the closed tool catalog.

        Raises:
            ValueError: A grant or denial names no catalog tool, or a
                tool that writes a workspace is granted to a Run that is
                not a mutating task-scoped Run.
        """
        declared = (("tool_grants", self.tool_grants), ("tool_denials", self.tool_denials))
        for field_name, tool_ids in declared:
            for tool_id in tool_ids:
                if tool_id not in SEMANTIC_TOOL_CATALOG:
                    raise ValueError(f"{field_name} names no catalog tool: {tool_id!r}")
        mutating = self.purpose in MUTATING_PURPOSES and self.scope_ref.kind is EntityKind.TASK
        if mutating:
            return self
        writing = tuple(
            tool_id
            for tool_id in self.semantic_tools
            if SEMANTIC_TOOL_CATALOG[tool_id].requires_mutating_task
        )
        if writing:
            raise ValueError(
                f"tools {[tool.value for tool in writing]} need a mutating task-scoped Run"
            )
        return self

    @model_validator(mode="after")
    def _lineage_is_not_a_cycle(self) -> Self:
        """Refuse a capsule naming itself as its own parent.

        Raises:
            ValueError: ``parent_run_ref`` equals ``run_ref``.
        """
        if self.parent_run_ref is not None and self.parent_run_ref == self.run_ref:
            raise ValueError("parent_run_ref must name another Run")
        return self


class AuthorityCapsule(_CapsuleContent):
    """The immutable, digest-bound authority one Run is dispatched under."""

    @classmethod
    def seal(cls, fields: Mapping[str, Any]) -> Self:
        """Validate *fields* into a capsule whose digest is computed here.

        The digest rule lives on this class alone, so a dispatcher
        supplies content and never spells a digest itself.

        Args:
            fields: Every field except ``contract_digest``.

        Returns:
            The validated, sealed capsule.

        Raises:
            pydantic.ValidationError: The content breaks a capsule rule.
        """
        content = _CapsuleContent.model_validate({**fields, "contract_digest": UNSEALED_DIGEST})
        digest = canonical_digest(content.contract_payload())
        return cls.model_validate({**content.model_dump(), "contract_digest": digest})

    @model_validator(mode="after")
    def _digest_matches_content(self) -> Self:
        """Recompute the contract digest and refuse a mismatch.

        Raises:
            ValueError: The stored digest does not cover the fields
                beside it.
        """
        if self.contract_digest != canonical_digest(self.contract_payload()):
            raise ValueError("contract_digest does not match the capsule content")
        return self
