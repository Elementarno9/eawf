"""What a count may claim and what a verb may do in each connection state.

``session.conn`` is the input; the projection binder becomes its producer. A connection
state that cannot vouch for a count says so in the frame (a label under the title and an
``ATTACHED`` line), and a state that refuses writes names its reason from the chrome. The
``ATTACHED`` line names the revision and age the caller read, never one of its own.

The two contracts each have one door: :func:`reads` says what a count may claim, and
:func:`write_refusal` says why a write may not be sent. A verb refuses from there whether
its key came from the keybar, the action menu or an overlay.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.operations import binding_refusal
from eawf.surfaces.tui.console.session import Session

# The connection states a write may be sent in. Only a complete live read vouches for the
# rows a write is previewed from, so a partial or degraded read refuses like any other.
MUTABLE: frozenset[str] = frozenset({"LIVE"})
# The connection states under which no request can leave the console at all.
TRANSPORT_LOSS: frozenset[str] = frozenset({"DISCONNECTED", "OFFLINE SNAPSHOT"})

#: Why the link is lost, as a disconnected frame's cause line and its write gate say it.
#: The transport dropped mid-session, so no request can be issued at all.
DISCONNECTED_CAUSE = "the daemon cannot be reached"

# The refusals the port states for connection values the packet's chrome names none for.
_PORT_REFUSALS: dict[str, str] = {
    "DISCONNECTED": f"{DISCONNECTED_CAUSE} · no request can be issued"
}


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
    "DISCONNECTED": Reads(False, f"disconnected · {DISCONNECTED_CAUSE}", Age.AGED),
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


def transport_lost(session: Session) -> bool:
    """Return whether no request can leave in the session's connection state, whatever it is."""
    return session.conn in TRANSPORT_LOSS


def mut_reason(session: Session, fixture: Fixture) -> str:
    """Return the live reason a write is refused, naming the connection state that refuses it."""
    conn = session.conn
    refusal = fixture.proto.states.refuse.get(conn) or _PORT_REFUSALS.get(conn)
    return f"{conn} · {refusal or 'not permitted in this connection state'}"


def write_refusal(
    session: Session,
    fixture: Fixture,
    *,
    verb: str,
    kind: str | None = None,
    principal_refusal: str = "",
) -> str:
    """Return why a write may not be sent, or nothing when it may; the one write gate.

    The connection state is judged first, so an offline console names the state that stops
    every write rather than one verb's missing mutator; a verb no mutator carries names that
    before the principal, because no principal would make it work.

    Args:
        session: The session whose connection state is judged.
        fixture: The registers the connection state's reason is read from.
        kind: The route or target kind the verb acts on; ``None`` where the verb is already
            addressed to a mutator, so only the state and the principal are judged.
        verb: The verb's name, as the menu lists it.
        principal_refusal: Why every bound write is refused because the daemon link acts as
            nobody; empty when it acts as someone or there is no link.
    """
    if not can_mutate(session):
        return mut_reason(session, fixture)
    unbound = binding_refusal(kind, verb) if kind is not None else ""
    return unbound or principal_refusal


def attn_cell(session: Session, n: int) -> str:
    """Return an attention count, labelled ``known`` when the reads cannot vouch for it.

    A count is never the no-value dash: the dash means a value that does not exist by
    design, and an unvouched count exists and is a floor.
    """
    return str(n) if reads(session).complete else f"{n} known"
