"""What a count may claim and what a verb may do in each connection state.

``session.conn`` is the input; the projection binder becomes its producer. A connection
state that cannot vouch for a count says so in the frame (a label under the title and an
``ATTACHED`` line), and a state that refuses writes names its reason from the chrome. The
``ATTACHED`` line names the revision and age the caller read, never one of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.session import Session

# The connection states a write may be sent in.
MUTABLE: frozenset[str] = frozenset({"LIVE", "LIVE / PARTIAL", "DEGRADED"})


class Age(Enum):
    """What a state's ``ATTACHED`` line says about the revision the frame was read at."""

    NONE = "none"
    UNAVAILABLE = "unavailable"
    REVISION = "revision"
    AGED = "aged"


@dataclass(frozen=True, slots=True)
class Reads:
    """What the frame may claim about its counts.

    Attributes:
        complete: Whether a count can be called complete.
        label: Why it cannot, shown under the title; empty when it can.
        age: What the ``ATTACHED`` line says; :func:`attached` words it.
    """

    complete: bool
    label: str
    age: Age


_COMPLETE = Reads(True, "", Age.NONE)
_READS: dict[str, Reads] = {
    "SNAPSHOT REQUIRED": Reads(
        False, "no usable revision · operator repair needed", Age.UNAVAILABLE
    ),
    "LIVE": _COMPLETE,
    "LIVE / PARTIAL": Reads(False, "partial · affected aggregates are labelled", Age.REVISION),
    "GAP DETECTED": Reads(False, "no count can be called complete for the gap", Age.REVISION),
    "OFFLINE SNAPSHOT": Reads(False, "a snapshot · nothing is arriving", Age.AGED),
    "REPLAYING": Reads(False, "replaying · cached facts only", Age.REVISION),
    "SNAPSHOT LOADING": Reads(
        False, "loading · the previous revision, labelled with its age", Age.REVISION
    ),
    "DEGRADED": Reads(False, "degraded · a feed is unhealthy", Age.REVISION),
    "DISCONNECTED": Reads(False, "disconnected · nothing is arriving", Age.REVISION),
}


def reads(session: Session) -> Reads:
    """Return what a count may claim in the session's connection state."""
    return _READS.get(session.conn, _COMPLETE)


def attached(rd: Reads, *, revision: str, age: str = "") -> str:
    """Return the ``ATTACHED`` line's text for ``rd``, empty when the counts are complete.

    Args:
        rd: What the connection state lets the frame claim.
        revision: The revision the frame's rows were read at, as the frame prints it.
        age: How long ago that revision was read, as the frame prints it; empty when
            the caller has not read an age for it, which states the revision alone
            rather than a claim about its age.
    """
    if rd.age is Age.UNAVAILABLE:
        return "∅ unavailable"
    if rd.age is Age.REVISION:
        return f"revision {revision}"
    if rd.age is Age.AGED:
        return f"revision {revision} · {age} old" if age else f"revision {revision}"
    return ""


def prototype_attached(rd: Reads, fixture: Fixture) -> str:
    """Return the ``ATTACHED`` line at the prototype registers' revision and snapshot age."""
    return attached(rd, revision=group(fixture.proto.revision), age=pt.SNAPSHOT_AGE)


def can_mutate(session: Session) -> bool:
    """Return whether the session's connection state admits a write."""
    return session.conn in MUTABLE


def mut_reason(session: Session, fixture: Fixture) -> str:
    """Return the live reason a write is refused in the session's connection state."""
    return fixture.proto.states.refuse.get(session.conn) or "not permitted in this connection state"


def attn_cell(session: Session, n: int) -> str:
    """Return an attention count, unknown rather than zero when the reads cannot vouch for it."""
    if reads(session).complete:
        return str(n)
    return str(n) if n else "–"  # noqa: RUF001
