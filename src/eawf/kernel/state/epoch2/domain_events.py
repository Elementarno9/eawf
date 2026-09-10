"""The closed ``domain.entity.verb`` event vocabulary and its boundary.

An event name is a contract with every subscriber that ever filters on
it, so the vocabulary is closed and derived rather than free text. The
legal names are exactly the ones the transition registry produces: an
entity token from :class:`~eawf.kernel.state.epoch2.transitions.LifecycleEntity`
and the verb of a registered edge. Adding an event therefore means adding
an edge, which is the only way a new name can also have a diagram, a
guard and a denial code.

Near-misses are the reason the check exists. ``task.completed`` drops the
namespace, ``batch.ready`` drops it too, and
``domain.delivery_batch.completed`` spells the entity the long way. Each
reads correctly to a human and each would silently create a second name
for an event that already has one, so a subscriber matching the canonical
name would go quiet without anything failing. :class:`DomainEvent` is
where they are refused: an event that cannot be constructed cannot be
appended.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final

from pydantic import AfterValidator, ConfigDict, StringConstraints

from eawf.kernel.state.epoch2.base import Epoch2Model, StrictPositiveInt
from eawf.kernel.state.epoch2.transitions import (
    TRANSITION_ROWS,
    LifecycleEntity,
    TransitionVerb,
)
from eawf.kernel.state.epoch2.urns import AnyEntityUrn
from eawf.kernel.state.types import UtcDatetime

#: The first segment of every domain event name. It exists so a domain
#: event is distinguishable at a glance from a runtime or transport
#: event that happens to mention the same entity.
DOMAIN_EVENT_NAMESPACE: Final = "domain"

#: How many dot-separated segments a domain event name has.
_SEGMENT_COUNT: Final = 3


class DomainEventRejection(StrEnum):
    """Why one candidate event name was refused.

    Values:
        MALFORMED: The name is not three dot-separated segments.
        UNKNOWN_NAMESPACE: The first segment is not ``domain``.
        UNKNOWN_ENTITY: The second segment is not an entity token.
        UNKNOWN_VERB: The third segment is not a transition verb.
        UNREGISTERED_EVENT: Both tokens are known but no registered edge
            of that entity carries that verb.
    """

    MALFORMED = "malformed"
    UNKNOWN_NAMESPACE = "unknown_namespace"
    UNKNOWN_ENTITY = "unknown_entity"
    UNKNOWN_VERB = "unknown_verb"
    UNREGISTERED_EVENT = "unregistered_event"


class DomainEventNameError(ValueError):
    """A candidate event name is not in the closed vocabulary.

    Attributes:
        reason: Which check refused the name, so a caller can branch on
            the class of mistake rather than parse the message.
        name: The candidate that was refused.
    """

    def __init__(self, reason: DomainEventRejection, name: str, message: str) -> None:
        """Store the typed *reason* beside the *name* it refused."""
        super().__init__(message)
        self.reason = reason
        self.name = name


def domain_event_name(entity: LifecycleEntity, verb: TransitionVerb) -> str:
    """Return the canonical event name of *verb* on *entity*.

    Args:
        entity: The entity the event is about.
        verb: The past-tense name of what happened.

    Returns:
        The ``domain.entity.verb`` name.
    """
    return f"{DOMAIN_EVENT_NAMESPACE}.{entity.value}.{verb.value}"


#: Every legal domain event name, derived from the registry so the two
#: cannot drift: an unregistered edge has no event and an unemitted event
#: has no name.
DOMAIN_EVENT_NAMES: Final[frozenset[str]] = frozenset(
    domain_event_name(row.entity, row.verb) for row in TRANSITION_ROWS
)


def validate_domain_event_name(name: str) -> str:
    """Return *name* when it is in the closed domain vocabulary.

    Args:
        name: The candidate event name.

    Returns:
        *name* unchanged.

    Raises:
        DomainEventNameError: The name is malformed, names an unknown
            namespace, entity or verb, or spells a combination no
            registered edge emits.
    """
    segments = name.split(".")
    if len(segments) != _SEGMENT_COUNT:
        raise DomainEventNameError(
            DomainEventRejection.MALFORMED,
            name,
            f"event name {name!r} must be '{DOMAIN_EVENT_NAMESPACE}.<entity>.<verb>'",
        )
    namespace, entity_token, verb_token = segments
    if namespace != DOMAIN_EVENT_NAMESPACE:
        raise DomainEventNameError(
            DomainEventRejection.UNKNOWN_NAMESPACE,
            name,
            f"event name {name!r} is not in the {DOMAIN_EVENT_NAMESPACE!r} namespace",
        )
    if entity_token not in frozenset(entity.value for entity in LifecycleEntity):
        raise DomainEventNameError(
            DomainEventRejection.UNKNOWN_ENTITY,
            name,
            f"{entity_token!r} is not a lifecycle entity token; expected one of "
            f"{sorted(entity.value for entity in LifecycleEntity)}",
        )
    if verb_token not in frozenset(verb.value for verb in TransitionVerb):
        raise DomainEventNameError(
            DomainEventRejection.UNKNOWN_VERB,
            name,
            f"{verb_token!r} is not a transition verb",
        )
    if name not in DOMAIN_EVENT_NAMES:
        raise DomainEventNameError(
            DomainEventRejection.UNREGISTERED_EVENT,
            name,
            f"no registered {entity_token} transition emits {verb_token!r}",
        )
    return name


#: An event name that has already been checked against the vocabulary.
DomainEventNameStr = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(validate_domain_event_name),
]


class DomainEvent(Epoch2Model):
    """One emitted lifecycle fact, named from the closed vocabulary.

    Constructing this model is the append boundary: a store writes what
    it is handed, so a name that is only checked inside the writer is
    checked after the decision to write it was already made. The model is
    frozen because an appended fact that can be edited in place is not a
    fact.

    Attributes:
        name: The ``domain.entity.verb`` name; refused unless registered.
        subject: The record the event is about.
        occurred_at: When the transition happened, as recorded by its
            caller rather than by the writer's own clock.
        revision: The subject's revision after the transition, so a
            consumer can order two events about one record without
            trusting the wall clock.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: DomainEventNameStr
    subject: AnyEntityUrn
    occurred_at: UtcDatetime
    revision: StrictPositiveInt


__all__ = [
    "DOMAIN_EVENT_NAMES",
    "DOMAIN_EVENT_NAMESPACE",
    "DomainEvent",
    "DomainEventNameError",
    "DomainEventNameStr",
    "DomainEventRejection",
    "domain_event_name",
    "validate_domain_event_name",
]
