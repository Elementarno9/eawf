"""The stall fact: a Run went quiet past its interval, and what resumes it.

A stall is an observation the daemon records, never a transition. The fact names the
last thing the Run produced, how long it has been silent, the interval that silence was
measured against and the control a principal asks for to resume it. It never moves the
Run: a Run still stored as running with a stall standing over it is *lost* -- its outcome
is unknown, which is neither a success nor a failure -- and only a principal's control
ends it.

One fact is raised per quiet episode. The episode is named by the sequence of the last
activity it is measured from, so a sweep that finds the same silence again answers with
the fact already standing, and a Run that produces anything afterwards has moved past it.
A Run that has produced nothing at all is measured from its start, as episode zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Final, Literal

from pydantic import Field

from eawf.kernel.runtime.events import RunEventKind
from eawf.kernel.runtime.provider import ControlKind, RuntimeRecord
from eawf.kernel.state.epoch2.base import NonEmptyStr, StrictNonNegativeInt
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime

#: The episode of a Run that has recorded no activity: it has been silent since it started.
SILENT_SINCE_START: Final = 0


class RunStallFact(RuntimeRecord):
    """One quiet episode of one Run, as the daemon observed it.

    Attributes:
        payload_kind: The discriminator separating a stall from the other run-ledger
            lines.
        run_ref: The Run that went quiet.
        anchor_sequence: The sequence of the last activity the silence is measured
            from, which names the episode; zero for a Run silent since it started.
        last_activity_at: When the daemon recorded that activity, or when the Run
            started when it has recorded none.
        last_activity_kind: What that activity was; ``None`` when there was none.
        elapsed_seconds: How long the Run had produced nothing when the fact was raised.
        interval_seconds: The stall interval it was measured against.
        resume_method: The verb a principal resumes the Run through.
        resume_control: The control that verb asks for.
        raised_at: When the daemon raised the fact.
    """

    payload_kind: Literal["run_stall"] = "run_stall"
    run_ref: RunUrn
    anchor_sequence: StrictNonNegativeInt
    last_activity_at: UtcDatetime
    last_activity_kind: RunEventKind | None
    elapsed_seconds: Annotated[float, Field(ge=0)]
    interval_seconds: StrictNonNegativeInt
    resume_method: NonEmptyStr
    resume_control: ControlKind
    raised_at: UtcDatetime


def standing_stall(
    facts: Sequence[RunStallFact], last_activity_sequence: int | None
) -> RunStallFact | None:
    """Return the stall still standing over a Run, or ``None`` when it has moved past it.

    Args:
        facts: The Run's stall facts, in ledger order.
        last_activity_sequence: The sequence of the Run's latest activity, or ``None``
            when it has recorded none.

    Returns:
        The fact whose episode is the Run's current silence, which is episode zero
        while it has recorded nothing. A fact anchored on an earlier activity is
        history: the Run produced something after it.
    """
    anchor = SILENT_SINCE_START if last_activity_sequence is None else last_activity_sequence
    return next((fact for fact in reversed(facts) if fact.anchor_sequence == anchor), None)


__all__ = ["SILENT_SINCE_START", "RunStallFact", "standing_stall"]
