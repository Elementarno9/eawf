"""Track, its scope discriminator, and the strict create document.

A Track has exactly two stored states. ``ACTIVE`` means work may be
placed under it and ``RETIRED`` is terminal, readable, and never
reactivated. There is no third: a state a profile invents would need an
edge in the transition table, a guard, and a rule for every proof bound
under it, none of which a profile can supply. Domain variation belongs to
:class:`~eawf.kernel.state.epoch2.policy.TrackPolicy` instead, which is
why the status field is a closed enumeration rather than a configured
string.

Scope is discriminated rather than optional. A repository-scoped Track
names exactly one repository and a workspace-scoped Track lists its
members explicitly; a single nullable list would make "no repositories
yet" and "every repository" the same stored value.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from eawf.kernel.state.epoch2.base import (
    CharterStr,
    Epoch2Model,
    NonEmptyStr,
    TitleStr,
    TrackKey,
    reject_normalized_duplicates,
)
from eawf.kernel.state.epoch2.policy import TrackPolicy
from eawf.kernel.state.epoch2.urns import (
    CampaignUrn,
    MilestoneUrn,
    RepositoryUrn,
    TrackUrn,
)
from eawf.kernel.state.epoch2.values import Epoch2Record, OwnerPrincipal


class TrackStatus(StrEnum):
    """The two stored lifecycle states of a Track.

    Values:
        ACTIVE: Work may be placed under the Track.
        RETIRED: Terminal. Readable, never reused, never reactivated.
    """

    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class RepoTrackScope(Epoch2Model):
    """A Track integrating into exactly one registered repository."""

    scope_kind: Literal["repository"]
    repository_ref: RepositoryUrn


class WorkspaceTrackScope(Epoch2Model):
    """A Track spanning an explicit list of member repositories.

    The list is stored sorted by canonical URN. Membership is a set, so
    two orderings of the same members must digest identically; sorting at
    the boundary is what makes the digest a function of the membership
    rather than of the author's typing order.
    """

    scope_kind: Literal["workspace"]
    repository_refs: tuple[RepositoryUrn, ...]

    @field_validator("repository_refs")
    @classmethod
    def _members_are_present_unique_and_sorted(
        cls, value: tuple[RepositoryUrn, ...]
    ) -> tuple[RepositoryUrn, ...]:
        """Require a non-empty, duplicate-free membership and canonicalise its order.

        Raises:
            ValueError: The membership is empty or names a repository
                twice.
        """
        if not value:
            raise ValueError("workspace scope must list at least one member repository")
        if len({str(ref) for ref in value}) != len(value):
            raise ValueError("workspace scope names the same repository twice")
        return tuple(sorted(value, key=str))


#: A Track's scope, discriminated on ``scope_kind``.
TrackScope = Annotated[RepoTrackScope | WorkspaceTrackScope, Field(discriminator="scope_kind")]


def _check_scope_phrases(in_scope: Iterable[str], out_of_scope: Iterable[str]) -> None:
    """Raise when the two scope lists repeat a phrase within or across each other.

    Args:
        in_scope: Phrases declared in scope.
        out_of_scope: Phrases declared out of scope.

    Raises:
        ValueError: A phrase repeats inside one list, or the same phrase
            is declared both in and out of scope.
    """
    in_scope = tuple(in_scope)
    out_of_scope = tuple(out_of_scope)
    reject_normalized_duplicates(in_scope, field="in_scope")
    reject_normalized_duplicates(out_of_scope, field="out_of_scope")
    reject_normalized_duplicates((*in_scope, *out_of_scope), field="in_scope/out_of_scope")


def _check_owner_matches_policy(owner: OwnerPrincipal, policy: TrackPolicy) -> None:
    """Raise when the Track owner and the policy principal disagree.

    Two spellings of one owner let a transfer update one and leave the
    other, after which the answer to "who owns this" depends on which
    field the caller read.

    Args:
        owner: The Track's declared owner.
        policy: The policy carrying the ownership principal.

    Raises:
        ValueError: The two principals differ.
    """
    if owner != policy.ownership_principal:
        raise ValueError(
            f"Track owner {owner.principal_id!r} differs from policy ownership_principal "
            f"{policy.ownership_principal.principal_id!r}"
        )


class TrackCreateSpec(Epoch2Model):
    """The strict create document for a Track.

    Status, identity, revision and the derived indexes are absent by
    construction: creation yields exactly one status, and accepting it as
    an input would let a caller create a Track already retired.
    """

    key: TrackKey
    title: TitleStr
    charter: CharterStr
    scope: TrackScope
    in_scope: tuple[NonEmptyStr, ...] = ()
    out_of_scope: tuple[NonEmptyStr, ...] = ()
    owner: OwnerPrincipal
    policy: TrackPolicy

    @model_validator(mode="after")
    def _scope_and_ownership_agree(self) -> Self:
        """Require unique scope phrases and one agreed owner.

        Raises:
            ValueError: A scope phrase repeats, or the owner disagrees
                with the policy's ownership principal.
        """
        _check_scope_phrases(self.in_scope, self.out_of_scope)
        _check_owner_matches_policy(self.owner, self.policy)
        return self


class Track(Epoch2Record):
    """The durable vehicle a Milestone or Campaign is placed under.

    ``milestone_refs`` and ``campaign_refs`` are derived indexes, not
    containment authority: the containment edge is stored on the child,
    and these exist so a Track view does not have to scan every record.
    """

    key: TrackKey
    urn: TrackUrn
    title: TitleStr
    charter: CharterStr
    scope: TrackScope
    in_scope: tuple[NonEmptyStr, ...] = ()
    out_of_scope: tuple[NonEmptyStr, ...] = ()
    owner: OwnerPrincipal
    policy: TrackPolicy
    status: TrackStatus
    milestone_refs: tuple[MilestoneUrn, ...] = ()
    campaign_refs: tuple[CampaignUrn, ...] = ()

    @model_validator(mode="after")
    def _scope_and_ownership_agree(self) -> Self:
        """Require unique scope phrases, one agreed owner, and unique indexes.

        Raises:
            ValueError: A scope phrase repeats, the owner disagrees with
                the policy's ownership principal, or a derived index names
                the same record twice.
        """
        _check_scope_phrases(self.in_scope, self.out_of_scope)
        _check_owner_matches_policy(self.owner, self.policy)
        if len(set(self.milestone_refs)) != len(self.milestone_refs):
            raise ValueError("milestone_refs names the same Milestone twice")
        if len(set(self.campaign_refs)) != len(self.campaign_refs):
            raise ValueError("campaign_refs names the same Campaign twice")
        return self


__all__ = [
    "RepoTrackScope",
    "Track",
    "TrackCreateSpec",
    "TrackScope",
    "TrackStatus",
    "WorkspaceTrackScope",
]
