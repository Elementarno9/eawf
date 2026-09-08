"""The raw drawer: a bounded, scrubbed segment of the runner's own words."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis.derive import target_id

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    return [
        f" RAW       {target_id(session, fixture)} · the runner’s own words, quoted exactly",
        "           seq 41,206  kind=progress          ×212 coalesced",
        "           seq 41,207  kind=heartbeat         bytes=48",
        "           seq 41,208  kind=tool.completed    bytes=380",
        "           bounded · scrubbed · retention-governed",
    ]
