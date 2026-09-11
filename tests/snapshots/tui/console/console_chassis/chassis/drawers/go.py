"""The go drawer: the twelve destinations, shown while the ``g`` prefix is armed."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

ROWS: tuple[str, ...] = (
    " GO        g h  home · scope     g a  activity      g n  needs you",
    "           g b  backlog          g t  timeline      g r  release",
    "           g s  settings         g y  history       g d  health",
    "           g i  notifications    g l  sandbox log   g u  unattended",
)


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    return list(ROWS)
