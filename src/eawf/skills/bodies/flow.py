"""``/flow`` skill body.

Phase 4 W01 freezes the field set. Per `ea-proposal.md` §15.2, the
``/flow`` body wraps the per-step envelopes of the six core skills it
runs in sequence. Each step is stored as the dict-form of an
:class:`~eawf.render.envelope.OutputEnvelope`; the runtime can re-validate
each step via :meth:`OutputEnvelope.model_validate`.

Storing the steps as ``dict[str, Any]`` rather than ``OutputEnvelope``
avoids a circular import between :mod:`eawf.render.envelope` and the
bodies package while preserving the typed shape on the wire.

Phase 5 W02 widens the body with two strictly-additive optional fields:
``resume_from_checkpoint_id`` (the ``EV-...`` id of the checkpoint a
``--resume`` invocation replayed from) and ``drift`` (the structured
drift report when a resume refuses with ``INTEGRITY_VIOLATION``). Both
default to ``None`` so the field set stays backwards-compatible per the
W01 frozen contract.
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
        user_question: Optional :class:`UserQuestion` payload — populated
            on the ``needs_user`` short-circuit path.
        resume_from_checkpoint_id: When the flow ran with ``--resume``,
            the ``EV-...`` id of the ``flow_checkpoint`` record that
            served as the replay anchor. ``None`` on a fresh run.
        drift: When ``--resume`` refuses on drift detection, the
            structured drift report (keys ``state_json``, ``git_head``,
            ``profile_ids``, ``args_per_step`` — only the populated
            ones). ``None`` on a clean run / clean resume.
    """

    model_config = ConfigDict(extra="forbid")

    topic: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    terminal_status: str | None = None
    user_question: UserQuestion | None = None
    resume_from_checkpoint_id: str | None = None
    drift: dict[str, Any] | None = None


__all__ = ["FlowBody"]
