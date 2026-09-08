"""Shared value objects and the identity head every stored record carries.

Four of these shapes exist because the same fact was previously spelled
differently at each place it appeared. An origin says whether a record
was created here or projected from the previous epoch; a reference says
which record it points at and of what kind; a reason says why a
transition happened and what proves it; a binding says which exact tree a
proof was taken against. Sharing one strict shape per fact is what lets a
consumer read any of them without knowing which entity produced it.

The identity head lives here too. :class:`Epoch2Record` carries the
immutable UUID, the public key, the qualified URN, the origin, and the
compare-and-swap revision, and it enforces the one cross-field rule that
makes the pair trustworthy: the URN addresses the same public key the
record spells, so a row whose key and locator disagree is refused instead
of resolving to two different records depending on which field the caller
read.
"""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import ConfigDict, model_validator

from eawf.kernel.identity import EntityKind
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    NonEmptyStr,
    PrincipalKey,
    Sha256DigestStr,
    ShaStr,
    SlugStr,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, EvidenceUrn
from eawf.kernel.state.types import UtcDatetime

#: How a legacy record's fields were derived from its source row. Only a
#: natively created record may claim ``native``; every other value names
#: an inference whose evidence is recorded separately.
MappingBasis = Literal["native", "mechanical", "observed", "operator", "split", "projection"]

#: How much the mapping is trusted. A native record is always exact.
OriginConfidence = Literal["exact", "supported", "ambiguous"]

#: The source-describing fields a legacy origin must carry and a native
#: origin must leave unset.
_SOURCE_FIELDS: tuple[str, ...] = (
    "source_schema_version",
    "source_kind",
    "source_id",
    "source_digest",
)


class EntityOrigin(Epoch2Model):
    """Whether a record was created here or projected from epoch 1.

    A migrated projection is immutable to the native lifecycle, so the
    discriminator is not a label: it decides which mutators may touch the
    record at all. The source-describing fields are required together
    with ``legacy`` and forbidden with ``native``, because a native record
    carrying a source digest is claiming a provenance it does not have.
    """

    kind: Literal["native", "legacy"]
    source_schema_version: NonEmptyStr | None = None
    source_kind: NonEmptyStr | None = None
    source_id: NonEmptyStr | None = None
    source_urn: AnyEntityUrn | None = None
    source_digest: Sha256DigestStr | None = None
    mapping_basis: MappingBasis
    confidence: OriginConfidence

    @model_validator(mode="after")
    def _source_fields_match_kind(self) -> Self:
        """Require the source fields of a legacy origin and forbid them otherwise.

        Raises:
            ValueError: A legacy origin omits a source field, a native
                origin supplies one, or the mapping basis and confidence
                disagree with the discriminator.
        """
        if self.kind == "legacy":
            missing = [name for name in _SOURCE_FIELDS if getattr(self, name) is None]
            if missing:
                raise ValueError(f"legacy origin requires {', '.join(missing)}")
            if self.mapping_basis == "native":
                raise ValueError("mapping_basis 'native' belongs to a natively created record")
            return self
        supplied = [name for name in _SOURCE_FIELDS if getattr(self, name) is not None]
        if self.source_urn is not None:
            supplied.append("source_urn")
        if supplied:
            raise ValueError(f"native origin forbids {', '.join(supplied)}")
        if self.mapping_basis != "native":
            raise ValueError(
                f"native origin requires mapping_basis 'native', got {self.mapping_basis!r}"
            )
        if self.confidence != "exact":
            raise ValueError(f"native origin requires confidence 'exact', got {self.confidence!r}")
        return self


class EntityRef(Epoch2Model):
    """A typed pointer at one record: the kind it claims plus its URN.

    The kind is the discriminator a consumer branches on and the URN is
    what resolves. A reference typed as a task holding a milestone URN
    would let a caller dispatch on one field and resolve on the other, so
    the two are checked against each other here rather than at each
    consumer.
    """

    kind: EntityKind
    urn: AnyEntityUrn

    @model_validator(mode="after")
    def _urn_agrees_with_kind(self) -> Self:
        """Require the URN to address the kind the reference discriminates.

        Raises:
            ValueError: The URN's entity kind differs from ``kind``.
        """
        if self.urn.kind is not self.kind:
            raise ValueError(
                f"reference discriminates {self.kind.value} but the URN addresses "
                f"{self.urn.kind.value}"
            )
        return self


class TransitionReason(Epoch2Model):
    """Why a transition happened, in a form a consumer can branch on.

    The code is what a projection groups by and the message is what an
    operator reads; carrying only the message would force every consumer
    to substring-match prose. Cancellation, failure, reset, retirement and
    recovery all require one of these, so the reason is never inferred
    from the absence of a field.
    """

    code: SlugStr
    message: NonEmptyStr
    evidence_refs: tuple[EvidenceUrn, ...] = ()


class ExactRevisionBinding(Epoch2Model):
    """The exact tree, contract and evidence a proof was taken against.

    All five members are required and none defaults, because a proof
    bound to four of them is a proof about an unspecified fifth. The model
    is frozen for the same reason: a binding that can be edited in place
    turns a stale proof into an apparently fresh one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    head_sha: ShaStr
    tree_sha: ShaStr
    contract_digest: Sha256DigestStr
    policy_revision: StrictPositiveInt
    evidence_digest: Sha256DigestStr


class OwnerPrincipal(Epoch2Model):
    """Exactly who owns something: a kind and an immutable qualified key.

    A free-form email address is refused. An address is a contact route
    that changes when a person changes employer or provider, so using one
    as an identity makes ownership history unreadable after the change.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_kind: Literal["operator", "team", "service"]
    principal_id: PrincipalKey


class Hold(Epoch2Model):
    """A pause placed on one scope, with the reason that justifies it.

    Pausing is a fact about a scope, not a lifecycle state of the thing
    paused. Keeping it here means a Track or a Campaign never grows a
    ``PAUSED`` status whose exit conditions would then have to be
    duplicated into every transition table.
    """

    hold_id: UUID
    scope: EntityRef
    reason: TransitionReason
    created_by: OwnerPrincipal
    created_at: UtcDatetime
    released_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _release_follows_creation(self) -> Self:
        """Require a release timestamp at or after the hold was placed.

        Raises:
            ValueError: The hold was released before it existed.
        """
        if self.released_at is not None and self.released_at < self.created_at:
            raise ValueError("released_at precedes created_at")
        return self


class Epoch2Record(Epoch2Model):
    """The identity head every stored epoch-2 entity carries.

    Mutable title, placement, policy and status never enter identity, so
    a subclass adds them beside these fields rather than folding any of
    them into the key or the URN. ``revision`` is the compare-and-swap
    token of this one record and is meaningless against another entity's
    revision; cross-entity ordering is the workspace-global canonical
    sequence instead.
    """

    uid: UUID
    key: NonEmptyStr
    urn: AnyEntityUrn
    origin: EntityOrigin
    revision: StrictPositiveInt
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _urn_addresses_this_record(self) -> Self:
        """Require the URN to locate the very key this record spells.

        Raises:
            ValueError: The URN's entity key differs from ``key``.
        """
        if self.urn.entity_key != self.key:
            raise ValueError(
                f"record keyed {self.key!r} carries a URN addressing {self.urn.entity_key!r}"
            )
        return self


__all__ = [
    "EntityOrigin",
    "EntityRef",
    "Epoch2Record",
    "ExactRevisionBinding",
    "Hold",
    "MappingBasis",
    "OriginConfidence",
    "OwnerPrincipal",
    "TransitionReason",
]
