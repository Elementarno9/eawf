"""``/coauthor`` skill body.

Mirrors the dict body emitted by :class:`eawf.workflow.skills.coauthor.CoauthorSkill`:
the resolved ``Co-Authored-By`` trailer policy. The ``needs_user`` path
(``mode=runtime`` with no resolvable runtime) carries a ``reason`` so the
runtime adapter can surface a ``coauthor resolve`` prompt.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

CoauthorMode = Literal["runtime", "project", "disabled"]


class CoauthorBody(BaseModel):
    """Body for ``/coauthor`` trailer resolution."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["coauthor_resolution"] = "coauthor_resolution"
    mode: CoauthorMode
    runtime: str | None = None
    trailer: str | None = None
    reason: str | None = None


__all__ = ["CoauthorBody", "CoauthorMode"]
