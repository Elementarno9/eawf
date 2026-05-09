"""Per-skill body Pydantic models.

Frozen at Phase 4 W01 per design spec §3.2 and `ea-proposal.md` §15.2.
Field set is additive after this wave: any new field must be optional and
must not change the meaning of an existing field. Breaking changes
require an explicit ``[CORE]`` commit on ``feature/eawf-v0.1``.

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

The :class:`UserQuestion` payload is shared across every body — strict
validation requires it on ``header.status == "needs_user"``.
"""

from __future__ import annotations

from eawf.skills.bodies.audit import AuditBody
from eawf.skills.bodies.differentiate import DifferentiateBody
from eawf.skills.bodies.flow import FlowBody
from eawf.skills.bodies.init import InitBody
from eawf.skills.bodies.polish import PolishBody
from eawf.skills.bodies.prep import PrepBody
from eawf.skills.bodies.research import ResearchBody
from eawf.skills.bodies.review import ReviewBody
from eawf.skills.bodies.roadmap import RoadmapBody
from eawf.skills.bodies.ship import ShipBody
from eawf.skills.bodies.user_question import UserQuestion, UserQuestionOption

__all__ = [
    "AuditBody",
    "DifferentiateBody",
    "FlowBody",
    "InitBody",
    "PolishBody",
    "PrepBody",
    "ResearchBody",
    "ReviewBody",
    "RoadmapBody",
    "ShipBody",
    "UserQuestion",
    "UserQuestionOption",
]
