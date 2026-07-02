"""``/agent-dispatch`` skill body.

Mirrors the dict body emitted by
:class:`eawf.workflow.skills.agent_dispatch.AgentDispatchSkill`: the resolved
runtime ladder for a claimed wave. A missing ``wave_id`` degrades to
``needs_user``; an unresolvable runtime is a soft ``partial`` outcome
with ``resolved_runtime`` left ``None``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentDispatchBody(BaseModel):
    """Body for ``/agent-dispatch`` runtime resolution."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["agent_dispatch"] = "agent_dispatch"
    wave_id: str | None = None
    runtime_preference: list[str] = Field(default_factory=list)
    resolved_runtime: str | None = None
    reason: str | None = None
    # W46 runtime options: ``headless`` routes the suggested next action to
    # the daemon live-spawn verb; ``model`` is the spawn-model override that
    # rides it. Both default so pre-W46 envelopes re-validate unchanged.
    headless: bool = False
    model: str | None = None


__all__ = ["AgentDispatchBody"]
