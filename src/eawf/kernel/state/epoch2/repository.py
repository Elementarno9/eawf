"""Repository: the integration target a Batch lands its Tasks into.

A repository has no lifecycle machine. It never terminates and no edge
moves it, so it is admitted once and then only read: a plan binds the head
the row records, and an apply refuses when that head has moved. The row is
therefore the one fact a plan cannot be written without, which is why it
gets a native create of its own rather than a hand-edited document row.

The head is never an input. It is read from the repository's git history
at admission, so a row cannot name a commit the repository does not hold.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, ConfigDict, StringConstraints

from eawf.kernel.identity import EntityKind, validate_entity_key
from eawf.kernel.state.epoch2.base import Epoch2Model, ShaStr, StrictPositiveInt
from eawf.kernel.state.epoch2.urns import RepositoryUrn
from eawf.kernel.state.types import UtcDatetime


def _repository_key(value: str) -> str:
    return validate_entity_key(EntityKind.REPOSITORY, value)


#: A ``REP-`` repository symbol.
RepositoryKey = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_repository_key),
]


class RepositoryCreateSpec(Epoch2Model):
    """The strict create document for a repository row.

    The head is absent by construction: the daemon reads it from git, so a
    caller cannot bind a plan to a head it merely asserted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: RepositoryKey


class Repository(Epoch2Model):
    """One admitted repository and the head it was admitted at.

    Attributes:
        key: The repository's public key.
        urn: Its qualified URN.
        revision: The compare-and-swap token, one at admission.
        head_sha: The commit ``HEAD`` resolved to when the row was admitted.
        created_at: When the row was admitted.
        updated_at: When it last moved.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: RepositoryKey
    urn: RepositoryUrn
    revision: StrictPositiveInt
    head_sha: ShaStr
    created_at: UtcDatetime
    updated_at: UtcDatetime


__all__ = ["Repository", "RepositoryCreateSpec", "RepositoryKey"]
