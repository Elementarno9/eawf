"""Strict base model, bounded scalars, and public-key aliases.

Every epoch-2 shape starts here so that "unknown key" means the same
thing in a create document, an RPC parameter, and a persisted record.
:class:`Epoch2Model` is the only base an epoch-2 model derives from, and
it forbids extras: a record that silently absorbs a misspelled field
renders plausibly and is wrong, which is the exact failure the strict
loader exists to prevent.

The scalar aliases are all ``strict=True``. Lax coercion would let the
integer ``1`` arrive where a title belongs and the boolean ``True``
arrive where a charter belongs, and both would then be persisted as the
strings ``"1"`` and ``"True"`` with no trace of the mistake.

Public keys are not re-spelled here. The key grammar has one home in
:mod:`eawf.kernel.identity.keys`, so :data:`TrackKey`,
:data:`MilestoneKey` and :data:`BatchKey` delegate to
:func:`~eawf.kernel.identity.keys.validate_entity_key` rather than
carrying a second copy of the regular expression that would drift from
it. A task key is deliberately absent: it embeds the owning project
code, which a bare field cannot know, so a task's key is checked against
the project slot of its own URN instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from eawf.kernel.identity import EntityKind, validate_entity_key
from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.state.models import ShaStr


class Epoch2Model(BaseModel):
    """Base of every epoch-2 model: an unknown key is a defect."""

    model_config = ConfigDict(extra="forbid")


# ---- bounded scalars --------------------------------------------------------

#: Free text that must carry at least one non-blank character. Surrounding
#: whitespace is stripped so ``" x "`` and ``"x"`` are the same value and
#: ``"   "`` is refused rather than persisted as a blank.
NonEmptyStr = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=2000),
]

#: A one-line label. Bounded at 80 so a title fits a rendered row.
TitleStr = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=80),
]

#: A Track charter: the long-form statement of what the Track is for.
CharterStr = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=2000),
]

#: A Milestone outcome: what an operator will be able to demonstrate.
OutcomeStr = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000),
]

#: A closed lowercase token naming a configured value (a view name, a
#: colour token, a metric unit). Tokens are compared, never displayed
#: verbatim, so the grammar is deliberately narrow.
TokenStr = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,63}$")]

#: A lowercase hyphenated identifier for a rule, a template, or a
#: rejection code.
SlugStr = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]

#: The identifier of one outcome metric declared by a Track policy.
MetricId = Annotated[str, StringConstraints(strict=True, pattern=r"^MET-[A-Z0-9][A-Z0-9-]{1,31}$")]

#: A principal's immutable qualified key. A free-form email address is
#: refused: an address is a contact route that changes, not an identity.
PrincipalKey = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9-]{1,31}$")]

#: A step of a Milestone acceptance journey. The zero-padded ordinal is
#: part of the grammar so lexical order and step order agree.
AcceptanceStepId = Annotated[str, StringConstraints(strict=True, pattern=r"^AS-\d{2,}$")]

#: A Git branch name. The excluded characters are the ones
#: ``git check-ref-format`` refuses, so a name that reaches a command
#: line cannot be mistaken for a revision selector.
BranchName = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=255, pattern=r"^[^\s~^:?*\[\\]+$"),
]

#: A compare-and-swap revision or any other counter that starts at one.
StrictPositiveInt = Annotated[StrictInt, Field(gt=0)]

#: A bound that may legitimately be zero, such as an advisory WIP limit.
StrictNonNegativeInt = Annotated[StrictInt, Field(ge=0)]


def _entity_key_validator(kind: EntityKind) -> Callable[[str], str]:
    """Return a validator admitting only canonical keys of *kind*."""

    def _validate(value: str) -> str:
        return validate_entity_key(kind, value)

    return _validate


#: An operator-chosen Track symbol under the ``TRK-`` prefix.
TrackKey = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_entity_key_validator(EntityKind.TRACK)),
]

#: A ``MLS-####`` Milestone key.
MilestoneKey = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_entity_key_validator(EntityKind.MILESTONE)),
]

#: A ``BAT-####`` delivery-batch key.
BatchKey = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_entity_key_validator(EntityKind.BATCH)),
]

#: A ``RUN-########`` run key. The width is eight rather than four
#: because a workspace mints runs by the thousand, and it is part of the
#: grammar rather than a rendering choice, so ``RUN-0001`` is refused.
RunKey = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_entity_key_validator(EntityKind.RUN)),
]


# ---- normalised free-text comparison ----------------------------------------

_WHITESPACE_RUN: Final = re.compile(r"\s+")


def normalize_phrase(value: str) -> str:
    """Return the comparison form of a free-text phrase.

    Two phrases that differ only in casing or in internal whitespace name
    the same thing, so a list carrying both has a duplicate the author did
    not see. Comparison collapses whitespace runs and case-folds; the
    stored value keeps its original spelling.

    Args:
        value: The phrase to normalise.

    Returns:
        The case-folded, whitespace-collapsed comparison form.
    """
    return _WHITESPACE_RUN.sub(" ", value.strip()).casefold()


def reject_normalized_duplicates(values: Iterable[str], *, field: str) -> None:
    """Raise when two of *values* normalise to the same phrase.

    Args:
        values: The phrases to check, in declaration order.
        field: The field name to name in the rejection message.

    Raises:
        ValueError: Two entries share a normalised form.
    """
    seen: dict[str, str] = {}
    for value in values:
        normalized = normalize_phrase(value)
        first = seen.get(normalized)
        if first is not None:
            raise ValueError(f"{field} repeats {value!r}; already present as {first!r}")
        seen[normalized] = value


__all__ = [
    "AcceptanceStepId",
    "BatchKey",
    "BranchName",
    "CharterStr",
    "Epoch2Model",
    "MetricId",
    "MilestoneKey",
    "NonEmptyStr",
    "OutcomeStr",
    "PrincipalKey",
    "RunKey",
    "Sha256DigestStr",
    "ShaStr",
    "SlugStr",
    "StrictNonNegativeInt",
    "StrictPositiveInt",
    "TitleStr",
    "TokenStr",
    "TrackKey",
    "normalize_phrase",
    "reject_normalized_duplicates",
]
