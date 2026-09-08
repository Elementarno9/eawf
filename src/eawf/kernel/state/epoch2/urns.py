"""Typed URN fields: one alias per relationship an epoch-2 record holds.

A relationship is a typed URN, never a bare string. A field annotated
:data:`MilestoneUrn` refuses a Batch URN at the loader, so no downstream
projection has to re-check what the field already promised; a field typed
``str`` defers that check to whichever consumer remembers to make it, and
the one that forgets renders a plausible and wrong row.

Parsing and rendering both funnel through
:mod:`eawf.kernel.identity.urn`. The validator calls
:func:`~eawf.kernel.identity.urn.parse_qualified_urn`, so every slot rule
of the grammar -- the reserved repository slot, the container-addresses-
itself rule, the public-key grammar -- is enforced once, at the boundary,
by the module that owns it. Serialisation calls
:func:`~eawf.kernel.identity.urn.format_qualified_urn` for the same
reason in the other direction: a model that hand-spelled the string
would be a second renderer able to drift from the parser that has to read
it back.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Final

from pydantic import PlainSerializer, PlainValidator, WithJsonSchema

from eawf.kernel.identity import (
    EntityKind,
    IdentityError,
    IdentityRejection,
    QualifiedUrn,
    format_qualified_urn,
    parse_qualified_urn,
)


def render_qualified_urn(value: QualifiedUrn) -> str:
    """Render *value* as its canonical URN string.

    Args:
        value: The parsed URN to render.

    Returns:
        The canonical ``eawf://`` string.

    Raises:
        IdentityError: A slot, key, or rung rule is broken.
    """
    return format_qualified_urn(
        workspace_key=value.workspace_key,
        project_key=value.project_key,
        repository_key=value.repository_key,
        kind=value.kind,
        entity_key=value.entity_key,
        rung=value.rung,
    )


def _urn_validator(*kinds: EntityKind) -> Callable[[Any], QualifiedUrn]:
    """Return a validator admitting only URNs addressing one of *kinds*."""
    admitted = frozenset(kinds)

    def _validate(value: Any) -> QualifiedUrn:
        if isinstance(value, QualifiedUrn):
            parsed = value
        elif isinstance(value, str):
            parsed = parse_qualified_urn(value)
        else:
            raise IdentityError(
                IdentityRejection.SCHEMA_VALIDATION_FAILED,
                f"expected a qualified URN string, got {type(value).__name__}",
            )
        if parsed.kind not in admitted:
            expected = ", ".join(sorted(kind.value for kind in admitted))
            raise IdentityError(
                IdentityRejection.IDENTITY_KIND_MISMATCH,
                f"URN addresses a {parsed.kind.value} but the field admits {expected}",
            )
        return parsed

    return _validate


_SERIALIZER: Final = PlainSerializer(render_qualified_urn, return_type=str)
_JSON_SCHEMA: Final = WithJsonSchema({"type": "string", "format": "eawf-urn"})

RepositoryUrn = Annotated[
    QualifiedUrn,
    PlainValidator(_urn_validator(EntityKind.REPOSITORY)),
    _SERIALIZER,
    _JSON_SCHEMA,
]

TrackUrn = Annotated[
    QualifiedUrn,
    PlainValidator(_urn_validator(EntityKind.TRACK)),
    _SERIALIZER,
    _JSON_SCHEMA,
]

CampaignUrn = Annotated[
    QualifiedUrn,
    PlainValidator(_urn_validator(EntityKind.CAMPAIGN)),
    _SERIALIZER,
    _JSON_SCHEMA,
]

MilestoneUrn = Annotated[
    QualifiedUrn,
    PlainValidator(_urn_validator(EntityKind.MILESTONE)),
    _SERIALIZER,
    _JSON_SCHEMA,
]

BatchUrn = Annotated[
    QualifiedUrn,
    PlainValidator(_urn_validator(EntityKind.BATCH)),
    _SERIALIZER,
    _JSON_SCHEMA,
]

TaskUrn = Annotated[
    QualifiedUrn,
    PlainValidator(_urn_validator(EntityKind.TASK)),
    _SERIALIZER,
    _JSON_SCHEMA,
]

RunUrn = Annotated[
    QualifiedUrn,
    PlainValidator(_urn_validator(EntityKind.RUN)),
    _SERIALIZER,
    _JSON_SCHEMA,
]

EvidenceUrn = Annotated[
    QualifiedUrn,
    PlainValidator(_urn_validator(EntityKind.EVIDENCE)),
    _SERIALIZER,
    _JSON_SCHEMA,
]

#: When a Task must be finally defined and executed by. The three
#: admitted kinds are the three horizons a Task can be due against; a
#: Track URN is refused because a Track never completes.
DueScopeUrn = Annotated[
    QualifiedUrn,
    PlainValidator(
        _urn_validator(EntityKind.MILESTONE, EntityKind.BATCH, EntityKind.RELEASE),
    ),
    _SERIALIZER,
    _JSON_SCHEMA,
]

#: Any addressable record. Used where the kind is carried beside the URN
#: (an :class:`~eawf.kernel.state.epoch2.values.EntityRef` discriminator)
#: or preserved from a source system rather than chosen.
AnyEntityUrn = Annotated[
    QualifiedUrn,
    PlainValidator(_urn_validator(*EntityKind)),
    _SERIALIZER,
    _JSON_SCHEMA,
]


__all__ = [
    "AnyEntityUrn",
    "BatchUrn",
    "CampaignUrn",
    "DueScopeUrn",
    "EvidenceUrn",
    "MilestoneUrn",
    "RepositoryUrn",
    "RunUrn",
    "TaskUrn",
    "TrackUrn",
    "render_qualified_urn",
]
