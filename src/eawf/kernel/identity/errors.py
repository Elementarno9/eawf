"""Typed rejection codes for the epoch-2 identity grammar.

Identity failures are branched on, not read: the importer retries a
different disposition on one code, the daemon returns a stable wire code
on another, and a fixture asserts a third. A prose-only ``ValueError``
forces every caller to substring-match a message, so each rejection
carries a :class:`IdentityRejection` member instead.

Four members spell the domain's stable wire codes exactly as the domain
contract spells them -- :attr:`IdentityRejection.IDENTITY_KIND_MISMATCH`,
:attr:`IdentityRejection.IDENTITY_NOT_FOUND`,
:attr:`IdentityRejection.LEGACY_IDENTITY_READ_ONLY` and
:attr:`IdentityRejection.SCHEMA_VALIDATION_FAILED`. The remaining members
are the fine-grained grammar reasons behind a
``schema_validation_failed`` response: they name which part of the URN or
key grammar refused the input so a test or an operator message can say
so, rather than collapsing eight distinct defects into one word.
"""

from __future__ import annotations

from enum import StrEnum


class IdentityRejection(StrEnum):
    """Why an identity string, key, or alias entry was refused."""

    IDENTITY_KIND_MISMATCH = "identity_kind_mismatch"
    IDENTITY_NOT_FOUND = "identity_not_found"
    LEGACY_IDENTITY_READ_ONLY = "legacy_identity_read_only"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"

    URN_MALFORMED = "urn_malformed"
    URN_UNKNOWN_KIND = "urn_unknown_kind"
    SYMBOL_KEY_INVALID = "symbol_key_invalid"
    REPOSITORY_SLOT_INVALID = "repository_slot_invalid"
    ENTITY_KEY_INVALID = "entity_key_invalid"
    KIND_NOT_ALLOCATABLE = "kind_not_allocatable"
    KEY_SPACE_SATURATED = "key_space_saturated"

    ALIAS_KEY_DUPLICATE = "alias_key_duplicate"
    ALIAS_TARGET_INVALID = "alias_target_invalid"
    ALIAS_TARGET_NOT_INJECTIVE = "alias_target_not_injective"
    ALIAS_CHAIN_FORBIDDEN = "alias_chain_forbidden"


class IdentityError(ValueError):
    """An identity string, key, or alias entry was refused.

    Subclasses :class:`ValueError` so a Pydantic field or model validator
    that raises one is folded into the surrounding ``ValidationError``
    and existing boundary handlers keep working; :attr:`code` is what a
    caller branches on.

    Attributes:
        code: Which rejection fired.
        canonical_history_link: The canonical URN an operator should read
            instead of the refused input, set only when one exists (the
            alias index refusing a mutation names the native record the
            alias points at).
    """

    def __init__(
        self,
        code: IdentityRejection,
        message: str,
        *,
        canonical_history_link: str | None = None,
    ) -> None:
        """Store the typed *code* and optional link beside *message*.

        The rendered text leads with the code because a Pydantic field or
        model validator that raises this error is folded into a
        ``ValidationError`` that keeps only the message: without the
        prefix the branchable code would be lost at exactly the boundary
        that most needs it.

        Args:
            code: The rejection this error reports.
            message: Operator-facing explanation.
            canonical_history_link: Canonical URN to read instead, when
                the rejection has one to offer.
        """
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.canonical_history_link = canonical_history_link
