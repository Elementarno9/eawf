"""Per-skill body Pydantic models.

Per ``docs/architecture/envelope.md``.
Field set is additive: any new field must be optional and must not change
the meaning of an existing field. A breaking change requires a new body
version rather than redefining an existing field.

Six core bodies (W02 fills implementations):

- :class:`~eawf.skills.bodies.research.ResearchBody`
- :class:`~eawf.skills.bodies.prep.PrepBody`
- :class:`~eawf.skills.bodies.audit.AuditBody`
- :class:`~eawf.skills.bodies.ship.ShipBody`
- :class:`~eawf.skills.bodies.review.ReviewBody`
- :class:`~eawf.skills.bodies.polish.PolishBody`

Four meta bodies (W03 fills implementations):

- :class:`~eawf.skills.bodies.init.InitBody`
- :class:`~eawf.skills.bodies.roadmap.RoadmapBody`
- :class:`~eawf.skills.bodies.differentiate.DifferentiateBody`
- :class:`~eawf.skills.bodies.flow.FlowBody`

``/blitz`` and the six skill-surface bodies:

- :class:`~eawf.skills.bodies.blitz.BlitzBody`
- :class:`~eawf.skills.bodies.coauthor.CoauthorBody`
- :class:`~eawf.skills.bodies.memory.MemoryBody`
- :class:`~eawf.skills.bodies.agent_dispatch.AgentDispatchBody`
- :class:`~eawf.skills.bodies.compress.CompressBody`
- :class:`~eawf.skills.bodies.wave_spec.WaveSpecBody`
- :class:`~eawf.skills.bodies.security_review.SecurityReviewBody`

The :class:`UserQuestion` payload is shared across every body — strict
validation requires it on ``header.status == "needs_user"``.
"""

from __future__ import annotations

from eawf.skills.bodies.agent_dispatch import AgentDispatchBody
from eawf.skills.bodies.audit import AuditBody
from eawf.skills.bodies.blitz import BlitzBody
from eawf.skills.bodies.coauthor import CoauthorBody
from eawf.skills.bodies.compress import CompressBody
from eawf.skills.bodies.differentiate import DifferentiateBody
from eawf.skills.bodies.flow import FlowBody
from eawf.skills.bodies.init import InitBody
from eawf.skills.bodies.memory import MemoryBody
from eawf.skills.bodies.polish import PolishBody
from eawf.skills.bodies.prep import PrepBody
from eawf.skills.bodies.research import ResearchBody
from eawf.skills.bodies.review import ReviewBody
from eawf.skills.bodies.roadmap import RoadmapBody
from eawf.skills.bodies.security_review import SecurityReviewBody
from eawf.skills.bodies.ship import ShipBody
from eawf.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.skills.bodies.wave_spec import WaveSpecBody

__all__ = [
    "AgentDispatchBody",
    "AuditBody",
    "BlitzBody",
    "CoauthorBody",
    "CompressBody",
    "DifferentiateBody",
    "FlowBody",
    "InitBody",
    "MemoryBody",
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
]
