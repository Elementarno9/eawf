"""The legacy alias index: one epoch-1 identifier, one canonical target.

Migration keeps every epoch-1 identifier resolvable, which is what lets
an operator paste a wave id from a two-year-old commit message and land
on the record it became. The index that does it is keyed by the four
facts that make a source row unique across the whole corpus --
``(source_schema_version, source_kind, source_id, source_project_code)``
-- because ``source_id`` alone repeats across schema versions and across
projects.

Three invariants make a lookup trustworthy rather than merely present.

A key appears once. Two entries for one source row mean the importer
converted it twice and one of the two conversions is a fabrication.

Distinct sources resolve to distinct targets. Two epoch-1 rows collapsing
onto one epoch-2 record is a collision the importer must surface, not a
merge it may perform silently.

No value is itself an alias. A target that is another entry's source
address needs a second hop to reach a record, and a resolver that follows
one hop returns something plausible while a resolver that follows two
returns something else. Values are exactly one native target URN or one
immutable legacy-record URN, and never a rung fragment, which addresses
part of a record rather than the record.

Resolution is read-only. Passing an alias to a native mutator is refused
with ``legacy_identity_read_only`` and the canonical URN to read instead:
the epoch-1 identifier names history, and history does not accept writes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eawf.kernel.identity.errors import IdentityError, IdentityRejection
from eawf.kernel.identity.keys import RE_SYMBOL_KEY
from eawf.kernel.identity.urn import parse_qualified_urn


class LegacyAliasKey(BaseModel):
    """The four facts that identify one epoch-1 source row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_schema_version: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_project_code: str = Field(pattern=RE_SYMBOL_KEY.pattern)


class LegacyAliasEntry(BaseModel):
    """One epoch-1 source row and the canonical record it became."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: LegacyAliasKey
    target: str
    source_urn: str | None = None

    @field_validator("target")
    @classmethod
    def _target_addresses_a_record(cls, value: str) -> str:
        """Refuse a target that does not address exactly one record.

        Raises:
            IdentityError: ``alias_target_invalid`` when the value is not
                a qualified URN or selects part of a record.
        """
        return _validate_target(value)

    @field_validator("source_urn")
    @classmethod
    def _source_urn_is_qualified(cls, value: str | None) -> str | None:
        """Refuse a preserved source URN that is not qualified.

        Raises:
            IdentityError: ``alias_target_invalid`` when the preserved
                source address is not a qualified URN.
        """
        if value is None:
            return None
        return _validate_target(value)

    @model_validator(mode="after")
    def _target_is_not_its_own_source(self) -> Self:
        """Refuse an entry that resolves to its own source address.

        Raises:
            IdentityError: ``alias_chain_forbidden`` when the target
                repeats the entry's own source URN.
        """
        if self.source_urn is not None and self.target == self.source_urn:
            raise IdentityError(
                IdentityRejection.ALIAS_CHAIN_FORBIDDEN,
                f"alias {self.target!r} resolves to its own source address",
            )
        return self


def _validate_target(value: str) -> str:
    """Return *value* when it addresses exactly one epoch-2 record."""
    try:
        parsed = parse_qualified_urn(value)
    except IdentityError as exc:
        raise IdentityError(
            IdentityRejection.ALIAS_TARGET_INVALID,
            f"alias value {value!r} is not a qualified URN: {exc}",
        ) from exc
    if parsed.rung is not None:
        raise IdentityError(
            IdentityRejection.ALIAS_TARGET_INVALID,
            f"alias value {value!r} selects a rung, not a record",
        )
    return value


def check_alias_invariants(entries: Sequence[LegacyAliasEntry]) -> None:
    """Refuse an entry set that breaks a resolution invariant.

    Args:
        entries: The alias entries to check as one set.

    Raises:
        IdentityError: ``alias_key_duplicate`` when one source row is
            keyed twice, ``alias_target_not_injective`` when two source
            rows resolve to one record, or ``alias_chain_forbidden`` when
            a value is itself an alias source address.
    """
    seen_keys: set[LegacyAliasKey] = set()
    seen_targets: dict[str, LegacyAliasKey] = {}
    source_urns = {entry.source_urn for entry in entries if entry.source_urn is not None}
    for entry in entries:
        if entry.key in seen_keys:
            raise IdentityError(
                IdentityRejection.ALIAS_KEY_DUPLICATE,
                f"source row {entry.key.source_id!r} is aliased twice",
            )
        seen_keys.add(entry.key)
        owner = seen_targets.get(entry.target)
        if owner is not None:
            raise IdentityError(
                IdentityRejection.ALIAS_TARGET_NOT_INJECTIVE,
                f"{entry.target!r} already resolves from {owner.source_id!r}",
            )
        seen_targets[entry.target] = entry.key
        if entry.target in source_urns:
            raise IdentityError(
                IdentityRejection.ALIAS_CHAIN_FORBIDDEN,
                f"alias {entry.target!r} is itself an alias source address",
            )


class LegacyAliasIndex(BaseModel):
    """Every epoch-1 identifier the cutover kept resolvable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[LegacyAliasEntry, ...] = ()

    @model_validator(mode="after")
    def _invariants_hold(self) -> Self:
        """Refuse an index whose entries break a resolution invariant.

        Raises:
            IdentityError: The rejection :func:`check_alias_invariants`
                raised, folded into the surrounding ``ValidationError``.
        """
        check_alias_invariants(self.entries)
        return self

    @classmethod
    def build(cls, entries: Iterable[LegacyAliasEntry]) -> LegacyAliasIndex:
        """Build an index from *entries*, raising the typed rejection.

        Model construction folds a validator's rejection into a Pydantic
        ``ValidationError``, which hides the branchable code. This
        entry point checks first, so the importer sees the
        :class:`~eawf.kernel.identity.errors.IdentityError` itself.

        Args:
            entries: The alias entries to index.

        Returns:
            The built index.

        Raises:
            IdentityError: A resolution invariant is broken.
        """
        rows = tuple(entries)
        check_alias_invariants(rows)
        return cls(entries=rows)

    def add(self, entry: LegacyAliasEntry) -> LegacyAliasIndex:
        """Return a new index carrying *entry* as well.

        Args:
            entry: The alias entry to add.

        Returns:
            A new index; the receiver is unchanged.

        Raises:
            IdentityError: Adding *entry* would break a resolution
                invariant.
        """
        return LegacyAliasIndex.build((*self.entries, entry))

    def as_mapping(self) -> Mapping[LegacyAliasKey, str]:
        """Return the index as a plain key-to-target mapping."""
        return {entry.key: entry.target for entry in self.entries}

    def resolve(self, key: LegacyAliasKey) -> str:
        """Return the canonical URN *key* resolves to.

        Args:
            key: The epoch-1 source row to resolve.

        Returns:
            The canonical target URN.

        Raises:
            IdentityError: ``identity_not_found`` when no entry keys
                *key*.
        """
        for entry in self.entries:
            if entry.key == key:
                return entry.target
        raise IdentityError(
            IdentityRejection.IDENTITY_NOT_FOUND,
            f"no alias for source row {key.source_id!r}",
        )

    def assert_mutable(self, key: LegacyAliasKey) -> None:
        """Refuse a native mutation addressed by an alias.

        A native mutator calls this before proceeding: an epoch-1
        identifier names history and resolves read-only, so a write
        addressed by one is refused with the canonical URN to read.

        Args:
            key: The identifier the mutation was addressed by.

        Raises:
            IdentityError: ``legacy_identity_read_only`` when *key* is
                an alias, carrying the canonical URN as the history link.
        """
        for entry in self.entries:
            if entry.key == key:
                raise IdentityError(
                    IdentityRejection.LEGACY_IDENTITY_READ_ONLY,
                    f"source row {key.source_id!r} is a read-only alias of {entry.target}",
                    canonical_history_link=entry.target,
                )
