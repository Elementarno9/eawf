"""How far the Tasks filed under a Track, a Milestone or a Batch have come.

A Batch stays PLANNED until it activates, and a Track lets only so many Batches be
active in one repository at once, so a Milestone whose work is done can still read
"0 of 15 batches done" for a long while. The counts here are taken one level down, off
the Tasks the read model holds, so the frame says how much of the work is ready rather
than how much of it has been integrated.

Every count is one pass over the rows the route already holds: :class:`Progress` files
each Task under its Batch and each Batch under its Milestone once per frame, and every
question after that is a lookup. Nothing reads git, a worktree or a second route.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from eawf.kernel.projection.row_facts import BATCH_CEILING_FACT
from eawf.kernel.projection.truth import TruthField
from eawf.kernel.state.epoch2.batch import BatchStatus
from eawf.kernel.state.epoch2.task_status import TaskStatus
from eawf.kernel.store.tiers import Epoch2Collection

#: The Task states that are no longer work: a cancelled or dropped Task is not counted.
_NOT_WORK: Final = frozenset({TaskStatus.CANCELLED.value, TaskStatus.DROPPED.value})

#: The Task states a worker holds: claimed for a Run, or running one.
_IN_FLIGHT: Final = frozenset({TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value})


class Counted(Protocol):
    """A projected record the counts are taken over: spine rows and route rows alike."""

    @property
    def key(self) -> str:
        """The record's public key."""

    @property
    def parent_key(self) -> str | None:
        """The key of the record it is filed under, when it names one."""

    @property
    def collection(self) -> Epoch2Collection:
        """The collection the record was read from."""

    @property
    def facts(self) -> Mapping[str, str]:
        """The further facts the projection read about the record."""

    def field(self, name: str) -> TruthField[str]:
        """Return one declared field."""


def _status(row: Counted) -> str | None:
    return row.field("status").value


@dataclass(frozen=True, slots=True)
class TaskTally:
    """The Tasks of one scope, counted by how far each has come.

    Attributes:
        completed: Tasks integrated and COMPLETED.
        ready: Tasks READY_TO_INTEGRATE, whose Run reported its work done.
        running: Tasks CLAIMED or RUNNING.
        failed: Tasks that FAILED.
        planned: Every other Task still to do.
    """

    completed: int = 0
    ready: int = 0
    running: int = 0
    failed: int = 0
    planned: int = 0

    @classmethod
    def of(cls, statuses: Iterable[str | None]) -> TaskTally:
        """Return the tally of ``statuses``, a cancelled or dropped Task left out."""
        counts = {"completed": 0, "ready": 0, "running": 0, "failed": 0, "planned": 0}
        for value in statuses:
            if value in _NOT_WORK:
                continue
            if value == TaskStatus.COMPLETED.value:
                counts["completed"] += 1
            elif value == TaskStatus.READY_TO_INTEGRATE.value:
                counts["ready"] += 1
            elif value in _IN_FLIGHT:
                counts["running"] += 1
            elif value == TaskStatus.FAILED.value:
                counts["failed"] += 1
            else:
                counts["planned"] += 1
        return cls(**counts)

    @property
    def total(self) -> int:
        """Return how many Tasks are counted."""
        return self.completed + self.ready + self.running + self.failed + self.planned

    @property
    def landed(self) -> int:
        """Return the Tasks whose work is done: ready to integrate, or already integrated."""
        return self.completed + self.ready

    def cell(self) -> str:
        """Return the tally as a Batch row reads it: ``7/7 ready`` or ``7/7 done``."""
        if not self.total:
            return "no task"
        if self.completed == self.total:
            return f"{self.completed}/{self.total} done"
        cell = f"{self.landed}/{self.total} ready"
        return f"{cell} · {self.running} running" if self.running else cell

    def line(self) -> str:
        """Return the tally by status, as the Milestone's progress line reads it."""
        parts = [
            f"{self.completed} completed",
            f"{self.ready} ready to integrate",
            f"{self.running} running",
            f"{self.planned} planned",
            *([f"{self.failed} failed"] if self.failed else []),
        ]
        return f"{' · '.join(parts)} of {self.total} {'task' if self.total == 1 else 'tasks'}"


def _ceiling_words(ceiling: int) -> str:
    return "one active batch" if ceiling == 1 else f"{ceiling} active batches"


@dataclass(frozen=True, slots=True)
class Progress:
    """The Tasks and Batches of one read model, filed once under their parents.

    Attributes:
        tasks: Each Batch's Task statuses, by Batch key.
        batches: Each Milestone's Batches, cancelled ones left out, by Milestone key.
        active: The ACTIVE Batches, by repository and Track.
    """

    tasks: Mapping[str, list[str | None]] = field(default_factory=dict)
    batches: Mapping[str, list[Counted]] = field(default_factory=dict)
    active: Mapping[tuple[str, str], list[str]] = field(default_factory=dict)

    @classmethod
    def of(cls, rows: Sequence[Counted]) -> Progress:
        """Return the rows filed under their parents, in one pass."""
        tasks: dict[str, list[str | None]] = {}
        batches: dict[str, list[Counted]] = {}
        active: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            if row.parent_key is None:
                continue
            if row.collection is Epoch2Collection.TASK:
                tasks.setdefault(row.parent_key, []).append(_status(row))
            elif row.collection is Epoch2Collection.BATCH:
                state = _status(row)
                if state != BatchStatus.CANCELLED.value:
                    batches.setdefault(row.parent_key, []).append(row)
                place = (row.facts.get("repository"), row.facts.get("track"))
                if state == BatchStatus.ACTIVE.value and place[0] and place[1]:
                    active.setdefault((place[0], place[1]), []).append(row.key)
        return cls(tasks=tasks, batches=batches, active=active)

    def batch(self, key: str) -> TaskTally:
        """Return the tally of the Tasks filed under one Batch."""
        return TaskTally.of(self.tasks.get(key, ()))

    def milestones(self, keys: Iterable[str]) -> tuple[TaskTally, list[Counted]]:
        """Return the Tasks and the live Batches of the Milestones ``keys`` name."""
        cut = [batch for key in keys for batch in self.batches.get(key, ())]
        statuses = [state for batch in cut for state in self.tasks.get(batch.key, ())]
        return TaskTally.of(statuses), cut

    def waiting(self, batch: Counted, tally: TaskTally) -> str | None:
        """Return why a PLANNED Batch whose Tasks are all ready does not activate, if so.

        It waits when its Track's ceiling of active Batches in its repository is already
        reached by other Batches; the ceiling is the Track policy's, read off the Batch.

        Returns:
            The note the Batch row carries, or ``None`` when it is not waiting on the
            ceiling or the read model does not state the facts that would show it.
        """
        if _status(batch) != BatchStatus.PLANNED.value or not tally.total:
            return None
        if tally.landed != tally.total:
            return None
        facts = batch.facts
        repository, track = facts.get("repository"), facts.get("track")
        stated = facts.get(BATCH_CEILING_FACT)
        if repository is None or track is None or stated is None or not stated.isdigit():
            return None
        ceiling = int(stated)
        others = [key for key in self.active.get((repository, track), ()) if key != batch.key]
        if len(others) < ceiling:
            return None
        return f"waiting: {_ceiling_words(ceiling)} per repository"

    def batch_cell(self, batch: Counted) -> str:
        """Return a Batch row's progress, with the ceiling it waits on when it does."""
        tally = self.batch(batch.key)
        note = self.waiting(batch, tally)
        return tally.cell() if note is None else f"{tally.cell()} · {note}"


def batches_done(batches: Sequence[Counted]) -> str:
    """Return how many of ``batches`` are COMPLETED, out of all of them."""
    done = sum(1 for batch in batches if _status(batch) == BatchStatus.COMPLETED.value)
    return f"{done} of {len(batches)} {'batch' if len(batches) == 1 else 'batches'} done"


__all__ = ["Counted", "Progress", "TaskTally", "batches_done"]
