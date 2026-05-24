"""SessionSource protocol — the shared contract for telemetry adapters.

A *source adapter* knows how to (a) discover the files of one kind under a
project root and (b) parse each file into a stream of typed projection rows.
The projector (C09 §5.9.4) drives every adapter through this protocol so it
stays agnostic of the on-disk shape of each runtime / store.

The protocol is :func:`~typing.runtime_checkable` so callers (and tests) can
assert structural conformance with ``isinstance``. It is generic over the row
type each adapter yields:

- :class:`~eawf.observability.telemetry.sources.event_jsonl.EventJsonlSource` yields
  :class:`~eawf.kernel.store.envelope.Envelope` rows.
- :class:`~eawf.observability.telemetry.sources.claude_session.ClaudeSessionSource` yields
  :class:`~eawf.observability.telemetry.models.TelemetrySession` rows.

Sibling waves add per-runtime adapters to this package; they implement this
same protocol and are driven identically by the projector.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

RowT_co = TypeVar("RowT_co", covariant=True)


@runtime_checkable
class SessionSource(Protocol[RowT_co]):
    """A typed reader the projector drives to materialise rows.

    Implementations expose ``source_name`` (a stable identifier used in log
    keys and rebuild counters), discover their files under a project root, and
    parse each discovered file into a bounded iterator of typed rows. Parsing
    is per-file and streaming so the projector keeps one file in memory at a
    time (C09 §5.9.4, bounded-memory invariant).
    """

    @property
    def source_name(self) -> str:
        """Stable adapter identifier (e.g. ``"event_jsonl"``, ``"claude"``)."""
        ...

    def discover(self, root: Path) -> Iterator[Path]:
        """Yield the source files this adapter reads under *root*.

        A missing path is not an error — the adapter yields nothing so the
        projector skips a runtime / store the operator does not use (C09 §6
        F2). Discovery never raises on a missing root.
        """
        ...

    def iter_rows(self, path: Path) -> Iterator[RowT_co]:
        """Parse *path* into a stream of typed projection rows.

        Malformed individual records are skipped with a logged warning rather
        than aborting the whole file (C09 §6 F3); a wholly missing *path*
        yields nothing.
        """
        ...


__all__ = ["SessionSource"]
