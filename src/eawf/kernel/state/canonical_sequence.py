"""Workspace-global monotonic ordinal allocation.

Two different numbers order an epoch-2 workspace and confusing them
produces a projection that reads plausibly and is wrong. ``revision`` is
per-entity, exists only as a compare-and-swap token, and is meaningless
across two entities: a Task revision of 7 says nothing about a Run
revision of 7. ``canonical_sequence`` is workspace-global and is the only
number that can justify rendering rows from several entity kinds in one
strict order.

This module owns the allocation of that number, and the primitive under
it:

* :class:`MonotonicAllocator` -- a counter that hands out strictly
  increasing ordinals inside a transaction, advances its committed
  high-water mark only when the transaction completes, and burns the
  ordinals of a transaction that raised so a rolled-back number is never
  handed out a second time in the same process.
* :class:`CanonicalSequenceAllocator` -- the workspace-global
  specialisation, which additionally refuses to append an event whose
  sequence is at or below the committed high-water mark.

Crash recovery restores an allocator from the durable high-water mark
alone, because that is the only figure that survives a crash. Ordinals
allocated by a transaction that never committed are therefore re-issuable
after recovery -- they were never stamped on a record, so re-issuing one
cannot collide with a committed stamp, which is the invariant that
matters. In-process rollback burns instead of reclaiming, so a number
that already reached a log line is not silently reused.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from typing import Final

#: First ordinal any allocator hands out. Sequences and public keys are
#: positive integers, so a fresh counter has committed nothing (0) and
#: issues 1 next.
FIRST_ORDINAL: Final = 1


class CanonicalSequenceRejection(StrEnum):
    """Why a sequence allocation or append was refused."""

    SEQUENCE_NOT_MONOTONIC = "canonical_sequence_not_monotonic"
    SEQUENCE_SPACE_SATURATED = "canonical_sequence_space_saturated"


class CanonicalSequenceError(ValueError):
    """An ordinal allocation or an event append was refused.

    Subclasses :class:`ValueError` so existing boundary handlers keep
    working; :attr:`code` is what a caller branches on.

    Attributes:
        code: Which rejection fired.
    """

    def __init__(self, code: CanonicalSequenceRejection, message: str) -> None:
        """Store the typed *code* alongside the operator-facing *message*.

        Args:
            code: The rejection this error reports.
            message: Operator-facing explanation.
        """
        super().__init__(message)
        self.code = code


class AllocationTransaction:
    """The ordinals one transaction has drawn, before it commits.

    A transaction is handed out by :meth:`MonotonicAllocator.transaction`
    and is never constructed directly: its ordinals are only durable once
    the surrounding ``with`` block exits without raising.
    """

    def __init__(self, *, first_ordinal: int, limit: int | None, name: str) -> None:
        """Open a transaction that starts issuing at *first_ordinal*.

        Args:
            first_ordinal: The next ordinal this transaction may issue.
            limit: Largest ordinal the counter's key space admits, or
                ``None`` when the space is unbounded.
            name: Counter name, used in rejection messages.
        """
        self._next = first_ordinal
        self._limit = limit
        self._name = name
        self._allocated: list[int] = []

    @property
    def allocated(self) -> tuple[int, ...]:
        """Ordinals drawn so far, in allocation order."""
        return tuple(self._allocated)

    @property
    def next_ordinal(self) -> int:
        """The ordinal this transaction would issue next."""
        return self._next

    def allocate(self) -> int:
        """Draw the next ordinal.

        Returns:
            The allocated ordinal, strictly greater than every ordinal
            the owning allocator has ever issued.

        Raises:
            CanonicalSequenceError: The counter's key space is exhausted.
                The caller's ``with`` block then unwinds, so the whole
                transaction aborts rather than committing unordered.
        """
        if self._limit is not None and self._next > self._limit:
            raise CanonicalSequenceError(
                CanonicalSequenceRejection.SEQUENCE_SPACE_SATURATED,
                f"{self._name} ordinal space exhausted at {self._limit}",
            )
        ordinal = self._next
        self._next += 1
        self._allocated.append(ordinal)
        return ordinal

    def allocate_run(self, count: int) -> tuple[int, ...]:
        """Draw *count* consecutive ordinals in one go.

        The importer allocates in source order inside the cutover
        transaction, so a migrated repository still yields one total
        order; this is the call that does it.

        Args:
            count: How many ordinals to draw. Must be positive.

        Returns:
            The allocated ordinals, ascending and contiguous.

        Raises:
            ValueError: *count* is not positive.
            CanonicalSequenceError: The key space is exhausted part-way,
                which aborts the whole transaction.
        """
        if count < 1:
            raise ValueError(f"count must be positive, got {count}")
        return tuple(self.allocate() for _ in range(count))


class MonotonicAllocator:
    """A counter issuing strictly increasing ordinals inside a transaction.

    The counter carries two figures. ``committed`` is the highest ordinal
    a completed transaction stamped on a record; it is the durable value
    and the one crash recovery restores. The private issue cursor is at
    least ``committed + 1`` and never decreases, so an ordinal burned by
    a rolled-back transaction is not re-issued in this process.
    """

    def __init__(self, *, name: str, committed: int = 0, limit: int | None = None) -> None:
        """Open a counter that has already committed *committed* ordinals.

        Args:
            name: Counter name, used in rejection messages.
            committed: Durable high-water mark, ``0`` for a fresh counter.
            limit: Largest ordinal the key space admits, or ``None``.

        Raises:
            ValueError: *committed* is negative, or *limit* is below the
                first issuable ordinal, or *committed* already exceeds
                *limit*.
        """
        if committed < 0:
            raise ValueError(f"committed high-water mark must not be negative, got {committed}")
        if limit is not None and limit < FIRST_ORDINAL:
            raise ValueError(f"limit must be at least {FIRST_ORDINAL}, got {limit}")
        if limit is not None and committed > limit:
            raise ValueError(f"committed {committed} exceeds limit {limit}")
        self._name = name
        self._committed = committed
        self._limit = limit
        self._next = committed + 1

    @property
    def name(self) -> str:
        """The counter's name."""
        return self._name

    @property
    def committed(self) -> int:
        """Highest ordinal a completed transaction stamped on a record."""
        return self._committed

    @property
    def limit(self) -> int | None:
        """Largest ordinal the key space admits, or ``None`` when unbounded."""
        return self._limit

    @contextmanager
    def transaction(self) -> Iterator[AllocationTransaction]:
        """Open a transaction whose ordinals commit on clean exit.

        Yields:
            The :class:`AllocationTransaction` to draw ordinals from.
        """
        txn = AllocationTransaction(
            first_ordinal=self._next,
            limit=self._limit,
            name=self._name,
        )
        try:
            yield txn
        except BaseException:
            # Burn rather than reclaim: a rolled-back ordinal may already
            # have reached a log line or an in-flight message, and the
            # cheapest way to keep those unambiguous is to never hand the
            # number out again.
            self._next = max(self._next, txn.next_ordinal)
            raise
        self._next = max(self._next, txn.next_ordinal)
        if txn.allocated:
            self._committed = txn.allocated[-1]


class CanonicalSequenceAllocator:
    """The workspace-global ``canonical_sequence`` counter.

    One allocator serves every entity kind of one workspace, which is
    what makes the number comparable across entities. Two workspaces hold
    two allocators and their sequences never interleave.
    """

    def __init__(self, *, workspace_key: str, high_water_mark: int = 0) -> None:
        """Open the counter of *workspace_key* at *high_water_mark*.

        Args:
            workspace_key: The workspace whose sequence this counter owns.
            high_water_mark: Highest committed sequence, ``0`` when the
                workspace has committed nothing.

        Raises:
            ValueError: *workspace_key* is empty or *high_water_mark* is
                negative.
        """
        if not workspace_key:
            raise ValueError("workspace_key must be non-empty")
        self._workspace_key = workspace_key
        self._counter = MonotonicAllocator(
            name=f"canonical_sequence[{workspace_key}]",
            committed=high_water_mark,
        )

    @classmethod
    def recover(cls, *, workspace_key: str, high_water_mark: int) -> CanonicalSequenceAllocator:
        """Rebuild the counter of *workspace_key* after a crash.

        Only the committed high-water mark survives a crash, so recovery
        resumes at ``high_water_mark + 1``. Sequences drawn by a
        transaction that never committed are re-issuable, because no
        record carries them.

        Args:
            workspace_key: The workspace being recovered.
            high_water_mark: The durable high-water mark read back from
                the committed state.

        Returns:
            A counter that issues ``high_water_mark + 1`` next.

        Raises:
            ValueError: *workspace_key* is empty or *high_water_mark* is
                negative.
        """
        return cls(workspace_key=workspace_key, high_water_mark=high_water_mark)

    @property
    def workspace_key(self) -> str:
        """The workspace this counter orders."""
        return self._workspace_key

    @property
    def high_water_mark(self) -> int:
        """Highest sequence a committed transaction stamped."""
        return self._counter.committed

    @contextmanager
    def transaction(self) -> Iterator[AllocationTransaction]:
        """Open a commit transaction that allocates sequences.

        Yields:
            The :class:`AllocationTransaction` the commit path draws its
            ``canonical_sequence`` values from.
        """
        with self._counter.transaction() as txn:
            yield txn

    def assert_appendable(self, sequence: int) -> None:
        """Refuse an event whose sequence cannot extend the total order.

        Args:
            sequence: The ``canonical_sequence`` the event carries.

        Raises:
            ValueError: *sequence* is not a positive integer.
            CanonicalSequenceError: *sequence* is at or below the
                workspace high-water mark, so appending it would leave
                two records claiming the same or an inverted position.
        """
        if sequence < FIRST_ORDINAL:
            raise ValueError(f"canonical_sequence must be positive, got {sequence}")
        if sequence <= self.high_water_mark:
            raise CanonicalSequenceError(
                CanonicalSequenceRejection.SEQUENCE_NOT_MONOTONIC,
                f"canonical_sequence {sequence} is at or below the "
                f"{self._workspace_key} high-water mark {self.high_water_mark}",
            )
