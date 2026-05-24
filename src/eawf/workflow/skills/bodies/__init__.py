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

from eawf.workflow.skills.bodies.agent_dispatch import AgentDispatchBody
from eawf.workflow.skills.bodies.audit import AuditBody
from eawf.workflow.skills.bodies.blitz import BlitzBody
from eawf.workflow.skills.bodies.coauthor import CoauthorBody
from eawf.workflow.skills.bodies.compress import CompressBody
from eawf.workflow.skills.bodies.differentiate import DifferentiateBody
from eawf.workflow.skills.bodies.flow import FlowBody
from eawf.workflow.skills.bodies.init import InitBody
from eawf.workflow.skills.bodies.memory import MemoryBody
from eawf.workflow.skills.bodies.polish import PolishBody
from eawf.workflow.skills.bodies.prep import PrepBody
from eawf.workflow.skills.bodies.research import ResearchBody
from eawf.workflow.skills.bodies.review import ReviewBody
from eawf.workflow.skills.bodies.roadmap import RoadmapBody
from eawf.workflow.skills.bodies.security_review import SecurityReviewBody
from eawf.workflow.skills.bodies.ship import ShipBody
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.bodies.wave_spec import WaveSpecBody

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
