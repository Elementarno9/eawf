"""Per-skill body Pydantic models.

Per ``docs/architecture/envelope.md``.
Field set is additive: any new field must be optional and must not change
the meaning of an existing field. A breaking change requires a new body
version rather than redefining an existing field.

Six core bodies (W02 fills implementations):

- :class:`~eawf.workflow.skills.bodies.research.ResearchBody`
- :class:`~eawf.workflow.skills.bodies.prep.PrepBody`
- :class:`~eawf.workflow.skills.bodies.audit.AuditBody`
- :class:`~eawf.workflow.skills.bodies.ship.ShipBody`
- :class:`~eawf.workflow.skills.bodies.review.ReviewBody`
- :class:`~eawf.workflow.skills.bodies.polish.PolishBody`

Four meta bodies (W03 fills implementations):

- :class:`~eawf.workflow.skills.bodies.init.InitBody`
- :class:`~eawf.workflow.skills.bodies.roadmap.RoadmapBody`
- :class:`~eawf.workflow.skills.bodies.differentiate.DifferentiateBody`
- :class:`~eawf.workflow.skills.bodies.flow.FlowBody`

Advisory body (no engine implementation; registry-only skill):

- :class:`~eawf.workflow.skills.bodies.mockup.MockupBody`

``/blitz`` and the six skill-surface bodies:

- :class:`~eawf.workflow.skills.bodies.blitz.BlitzBody`
- :class:`~eawf.workflow.skills.bodies.coauthor.CoauthorBody`
- :class:`~eawf.workflow.skills.bodies.memory.MemoryBody`
- :class:`~eawf.workflow.skills.bodies.agent_dispatch.AgentDispatchBody`
- :class:`~eawf.workflow.skills.bodies.compress.CompressBody`
- :class:`~eawf.workflow.skills.bodies.wave_spec.WaveSpecBody`
- :class:`~eawf.workflow.skills.bodies.security_review.SecurityReviewBody`

The :class:`UserQuestion` payload is shared across every body — strict
validation requires it on ``header.status == "needs_user"``.
"""

from __future__ import annotations

from pydantic import BaseModel

from eawf.workflow.skills.bodies.agent_dispatch import AgentDispatchBody
from eawf.workflow.skills.bodies.audit import AuditBody
from eawf.workflow.skills.bodies.blitz import BlitzBody
from eawf.workflow.skills.bodies.coauthor import CoauthorBody
from eawf.workflow.skills.bodies.compress import CompressBody
from eawf.workflow.skills.bodies.differentiate import DifferentiateBody
from eawf.workflow.skills.bodies.flow import FlowBody
from eawf.workflow.skills.bodies.init import InitBody
from eawf.workflow.skills.bodies.memory import MemoryBody
from eawf.workflow.skills.bodies.mockup import (
    MockupBody,
    MockupVariant,
    resolve_mockup_pick,
)
from eawf.workflow.skills.bodies.polish import PolishBody
from eawf.workflow.skills.bodies.prep import PrepBody
from eawf.workflow.skills.bodies.research import ResearchBody
from eawf.workflow.skills.bodies.review import ReviewBody
from eawf.workflow.skills.bodies.roadmap import RoadmapBody
from eawf.workflow.skills.bodies.security_review import SecurityReviewBody
from eawf.workflow.skills.bodies.ship import ShipBody
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.bodies.wave_spec import WaveSpecBody

# Canonical skill-name -> body-model map. This is the single source of
# truth the engine and the CLI both bind against, so neither layer has to
# import the other (the CLI ``skill`` subapp re-exports this rather than
# re-declaring the mapping). Key order matches
# :data:`eawf.surfaces.render.envelope.CANONICAL_SKILL_NAMES` so the
# ``skill list`` fingerprint column renders in the frozen builtin order.
SKILL_BODY_MODELS: dict[str, type[BaseModel]] = {
    "/research": ResearchBody,
    "/prep": PrepBody,
    "/audit": AuditBody,
    "/ship": ShipBody,
    "/review": ReviewBody,
    "/polish": PolishBody,
    "/init": InitBody,
    "/roadmap": RoadmapBody,
    "/differentiate": DifferentiateBody,
    "/flow": FlowBody,
    "/blitz": BlitzBody,
    "/coauthor": CoauthorBody,
    "/memory": MemoryBody,
    "/agent-dispatch": AgentDispatchBody,
    "/compress": CompressBody,
    "/wave-spec": WaveSpecBody,
    "/security-review": SecurityReviewBody,
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
    "AgentDispatchBody",
    "AuditBody",
    "BlitzBody",
    "CoauthorBody",
    "CompressBody",
    "DifferentiateBody",
    "FlowBody",
    "InitBody",
    "MemoryBody",
    "MockupBody",
    "MockupVariant",
    "PolishBody",
    "PrepBody",
    "ResearchBody",
    "ReviewBody",
    "RoadmapBody",
    "SecurityReviewBody",
    "ShipBody",
    "UserQuestion",
    "UserQuestionOption",
    "WaveSpecBody",
    "body_model_for",
    "resolve_mockup_pick",
]
