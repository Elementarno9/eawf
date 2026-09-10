"""An event name is either the registered one or it is refused.

Three near-misses carry the weight here. ``task.completed`` and
``batch.ready`` drop the namespace, and ``domain.delivery_batch.completed``
spells the entity the long way. Each reads correctly, each would create a
second name for an event that already has one, and a subscriber matching
the canonical name would simply go quiet -- with nothing failing anywhere
to say why. So the refusal has to happen before the append, which is what
constructing :class:`DomainEvent` enforces.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.epoch2.domain_events import (
    DOMAIN_EVENT_NAMES,
    DomainEvent,
    DomainEventNameError,
    DomainEventRejection,
    domain_event_name,
    validate_domain_event_name,
)
from eawf.kernel.state.epoch2.transitions import (
    TRANSITION_ROWS,
    LifecycleEntity,
    TransitionVerb,
)

pytestmark = pytest.mark.unit

TASK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
AT = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)


def _event(name: str) -> DomainEvent:
    """Build one appendable event carrying *name*."""
    return DomainEvent(
        name=name,
        subject=parse_qualified_urn(TASK_URN),
        occurred_at=AT,
        revision=2,
    )


# ---- the canonical name -----------------------------------------------------


def test_domain_task_completed_validates() -> None:
    assert validate_domain_event_name("domain.task.completed") == "domain.task.completed"


def test_domain_task_completed_is_appendable() -> None:
    event = _event("domain.task.completed")
    assert event.name == "domain.task.completed"
    assert event.revision == 2


def test_domain_batch_ready_is_the_canonical_batch_name() -> None:
    assert "domain.batch.ready" in DOMAIN_EVENT_NAMES


# ---- the three aliases the vocabulary refuses -------------------------------


def test_task_completed_without_the_namespace_is_refused() -> None:
    with pytest.raises(DomainEventNameError) as excinfo:
        validate_domain_event_name("task.completed")
    assert excinfo.value.reason is DomainEventRejection.MALFORMED


def test_batch_ready_without_the_namespace_is_refused() -> None:
    with pytest.raises(DomainEventNameError) as excinfo:
        validate_domain_event_name("batch.ready")
    assert excinfo.value.reason is DomainEventRejection.MALFORMED


def test_delivery_batch_long_spelling_is_refused() -> None:
    with pytest.raises(DomainEventNameError) as excinfo:
        validate_domain_event_name("domain.delivery_batch.completed")
    assert excinfo.value.reason is DomainEventRejection.UNKNOWN_ENTITY


@pytest.mark.parametrize(
    "alias",
    ["task.completed", "batch.ready", "domain.delivery_batch.completed"],
)
def test_an_alias_fails_before_it_can_be_appended(alias: str) -> None:
    with pytest.raises(ValidationError):
        _event(alias)


# ---- the other ways a name can be wrong -------------------------------------


def test_a_foreign_namespace_is_refused() -> None:
    with pytest.raises(DomainEventNameError) as excinfo:
        validate_domain_event_name("runtime.task.completed")
    assert excinfo.value.reason is DomainEventRejection.UNKNOWN_NAMESPACE


def test_an_unknown_verb_is_refused() -> None:
    with pytest.raises(DomainEventNameError) as excinfo:
        validate_domain_event_name("domain.task.exploded")
    assert excinfo.value.reason is DomainEventRejection.UNKNOWN_VERB


def test_a_known_verb_on_the_wrong_entity_is_refused() -> None:
    with pytest.raises(DomainEventNameError) as excinfo:
        validate_domain_event_name("domain.track.completed")
    assert excinfo.value.reason is DomainEventRejection.UNREGISTERED_EVENT
    assert excinfo.value.name == "domain.track.completed"


@pytest.mark.parametrize(
    "candidate",
    ["", "domain", "domain.task", "domain.task.completed.again", "..", "domain..completed"],
)
def test_a_name_that_is_not_three_segments_is_refused(candidate: str) -> None:
    with pytest.raises(DomainEventNameError):
        validate_domain_event_name(candidate)


def test_a_non_string_name_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError):
        _event(17)  # type: ignore[arg-type]


# ---- the vocabulary is derived from the registry ----------------------------


def test_every_registered_edge_has_an_event_name() -> None:
    assert {
        domain_event_name(row.entity, row.verb) for row in TRANSITION_ROWS
    } == DOMAIN_EVENT_NAMES


def test_every_event_name_names_a_registered_edge() -> None:
    for name in DOMAIN_EVENT_NAMES:
        _namespace, entity_token, verb_token = name.split(".")
        entity = LifecycleEntity(entity_token)
        verb = TransitionVerb(verb_token)
        assert any(row.entity is entity and row.verb is verb for row in TRANSITION_ROWS)


def test_the_vocabulary_is_smaller_than_every_entity_verb_pair() -> None:
    # A closed vocabulary is the point: most (entity, verb) pairs are not
    # events, and a registry that admitted all of them would admit
    # `domain.track.completed`.
    assert len(DOMAIN_EVENT_NAMES) < len(LifecycleEntity) * len(TransitionVerb)


def test_domain_event_is_frozen() -> None:
    event = _event("domain.task.completed")
    with pytest.raises(ValidationError):
        event.name = "domain.task.failed"


def test_domain_event_refuses_an_unknown_key() -> None:
    with pytest.raises(ValidationError):
        DomainEvent(
            name="domain.task.completed",
            subject=parse_qualified_urn(TASK_URN),
            occurred_at=AT,
            revision=2,
            actor="OP-0001",  # type: ignore[call-arg]
        )


def test_domain_event_refuses_a_zero_revision() -> None:
    with pytest.raises(ValidationError):
        DomainEvent(
            name="domain.task.completed",
            subject=parse_qualified_urn(TASK_URN),
            occurred_at=AT,
            revision=0,
        )
