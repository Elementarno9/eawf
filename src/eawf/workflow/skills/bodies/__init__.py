"""Per-skill body Pydantic models.

Per ``docs/architecture/envelope.md``.
Field set is additive: any new field must be optional and must not change
the meaning of an existing field. A breaking change requires a new body
version rather than redefining an existing field.

Bodies still bound by the engine:

- :class:`~eawf.workflow.skills.bodies.research.ResearchBody`
- :class:`~eawf.workflow.skills.bodies.audit.AuditBody`
- :class:`~eawf.workflow.skills.bodies.ship.ShipBody`
- :class:`~eawf.workflow.skills.bodies.review.ReviewBody`
- :class:`~eawf.workflow.skills.bodies.flow.FlowBody`
- :class:`~eawf.workflow.skills.bodies.memory.MemoryBody`

Advisory body (no engine implementation; registry-only skill):

- :class:`~eawf.workflow.skills.bodies.mockup.MockupBody`

The three lifecycle bodies that drive the native delivery path:

- :class:`~eawf.workflow.skills.bodies.dispatch.DispatchBody`
- :class:`~eawf.workflow.skills.bodies.integrate.IntegrateBody`
- :class:`~eawf.workflow.skills.bodies.verify.VerifyBody`

The :class:`UserQuestion` payload is shared across every body — strict
validation requires it on ``header.status == "needs_user"``.
"""

from __future__ import annotations

from pydantic import BaseModel

from eawf.workflow.skills.bodies.audit import AuditBody
from eawf.workflow.skills.bodies.dispatch import DispatchBody
from eawf.workflow.skills.bodies.flow import FlowBody
from eawf.workflow.skills.bodies.integrate import IntegrateBody
from eawf.workflow.skills.bodies.memory import MemoryBody
from eawf.workflow.skills.bodies.mockup import (
    MockupBody,
    MockupVariant,
)
from eawf.workflow.skills.bodies.research import ResearchBody
from eawf.workflow.skills.bodies.review import ReviewBody
from eawf.workflow.skills.bodies.ship import ShipBody
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.bodies.verify import VerifyBody

# Canonical skill-name -> body-model map. This is the single source of
# truth the engine and the CLI both bind against, so neither layer has to
# import the other (the CLI ``skill`` subapp re-exports this rather than
# re-declaring the mapping). Key order matches
# :data:`eawf.surfaces.render.envelope.CANONICAL_SKILL_NAMES` so the
# ``skill list`` fingerprint column renders in the frozen builtin order.
SKILL_BODY_MODELS: dict[str, type[BaseModel]] = {
    "/research": ResearchBody,
    "/audit": AuditBody,
    "/ship": ShipBody,
    "/review": ReviewBody,
    "/flow": FlowBody,
    "/memory": MemoryBody,
    "/dispatch": DispatchBody,
    "/integrate": IntegrateBody,
    "/verify": VerifyBody,
}


def body_model_for(name: str) -> type[BaseModel]:
    """Return the body-model class registered for canonical skill *name*.

    Args:
        name: A canonical slash-prefixed skill name (e.g. ``"/research"``).

    Returns:
        The :class:`pydantic.BaseModel` subclass that types the skill's
        body payload.

    Raises:
        KeyError: *name* is not a canonical skill name in
            :data:`SKILL_BODY_MODELS`.
    """
    try:
        return SKILL_BODY_MODELS[name]
    except KeyError as exc:
        raise KeyError(f"unknown skill: {name!r}") from exc


__all__ = [
    "SKILL_BODY_MODELS",
    "AuditBody",
    "DispatchBody",
    "FlowBody",
    "IntegrateBody",
    "MemoryBody",
    "MockupBody",
    "MockupVariant",
    "ResearchBody",
    "ReviewBody",
    "ShipBody",
    "UserQuestion",
    "UserQuestionOption",
    "VerifyBody",
    "body_model_for",
]
