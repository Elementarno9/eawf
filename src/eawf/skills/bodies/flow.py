"""``/flow`` skill body.

Phase 4 W01 freezes the field set. Per `ea-proposal.md` §15.2, the
``/flow`` body wraps the per-step envelopes of the six core skills it
runs in sequence. Each step is stored as the dict-form of an
:class:`~eawf.render.envelope.OutputEnvelope`; the runtime can re-validate
each step via :meth:`OutputEnvelope.model_validate`.

Storing the steps as ``dict[str, Any]`` rather than ``OutputEnvelope``
avoids a circular import between :mod:`eawf.render.envelope` and the
bodies package while preserving the typed shape on the wire.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.skills.bodies.user_question import UserQuestion


class FlowBody(BaseModel):
    """Body for ``/flow``.

    Attributes:
        topic: Free-form description of what the flow run is about.
        steps: List of per-skill envelope dicts (one per executed core
            skill in the flow run, in order).
        terminal_status: The terminal envelope status of the flow run
            (mirrors the outer header.status; included for inspection
            tools that only parse the body).
    """

    model_config = ConfigDict(extra="forbid")

    topic: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    terminal_status: str | None = None
    user_question: UserQuestion | None = None


__all__ = ["FlowBody"]
