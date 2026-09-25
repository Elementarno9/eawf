"""The prototype's hand-drawn rows: ids and literal rows the packet drew in its frames.

These belong to the golden harness, beside the prototype registers the fixture loads.
Only the prototype branch of a renderer reads them, the branch a console takes when it
holds the prototype registers and no read model; a console built from the packaged chrome
never reaches that branch and draws the unknown frame instead. Keeping every such literal
in this one module is what lets the literal scan prove no renderer embeds its own.
"""

from __future__ import annotations

#: The age of the snapshot the prototype's offline frames report.
SNAPSHOT_AGE = "6m 12s"

#: The Run a Run-scoped frame opens onto when the session names none.
OWN_RUN = "RUN-9e3779b1"

#: The Milestone the Milestone frame opens onto when the session names none.
OWN_MILESTONE = "MLS-0004"

#: The Run whose Transcript the prototype draws.
TRANSCRIPT_RUN = "RUN-daa66d18"

#: The Run the three Run sub-surfaces name as their parent.
RUN_PARENT = "RUN-538453eb"

#: The Run whose pause outcome is unknown.
PAUSED_RUN = "RUN-a708a7d6"

#: The Run whose events retention purged.
PURGED_RUN = "RUN-3c6ef367"

#: History: the fact, the revision it landed at, its source and when.
HISTORY_FACTS: tuple[tuple[str, str, str, str], ...] = (
    ("MLS-0004 accepted", "41,208", "operator", "14:01"),
    ("BAT-0003 authority invalidated", "41,199", "head moved", "13:58"),
    ("EAWF-0044 criteria set", "41,140", "imported", "Jul 11"),
    (f"{PURGED_RUN} events purged", "41,002", "retention", "Jul 4"),
)

#: History diff: the revision pairs ``p`` cycles, the entity diffed, and its bounds.
DIFF_PAIRS: tuple[str, ...] = ("41,150 → 41,208", "41,088 → 41,150", "40,990 → 41,088")
DIFF_ENTITY = RUN_PARENT
DIFF_BETWEEN = " BETWEEN      41,150  13:58:04   →   41,208  14:02:11"

#: Cost ceiling: the Runs the ceiling stopped, when, and why.
STOPPED_RUNS: tuple[tuple[str, str, str], ...] = (
    ("RUN-1c93af08", "13:41", "hard limit reached"),
    ("RUN-4e2b6c77", "12:08", "hard limit reached"),
)

#: Export: the Run reported, then each part, whether it is included, its size and why.
EXPORT_RUN = RUN_PARENT
EXPORT_PARTS: tuple[tuple[str, str, str, str], ...] = (
    ("timeline", "yes", "41,208 events", "Every event this Run recorded, in order."),
    ("usage and cost", "yes", "~ 4.62 · derived", "Derived from the events, not from a bill."),
    ("transcript", "yes", "6,102 lines known", "What the runner said, quoted exactly."),
    ("secrets", "never", "∅ redacted by policy", "Policy redacts these; no export can carry them."),
    (
        "sandbox decisions",
        "no",
        "142 · space includes",
        "Left out — including them adds 142 lines.",
    ),
)

#: Unattended: the queue's Runs with their Task, state chip and progress, then the Run a
#: queue request names.
QUEUE: tuple[tuple[str, str, str, str, str], ...] = (
    ("RUN-538453eb", "EAWF-0042", "ok", "RUNNING", "~ 62%"),
    ("RUN-7b0e4d31", "EAWF-0044", "ok", "RUNNING", "~ 18%"),
    ("RUN-1c93af08", "EAWF-0051", "info", "QUEUED", ""),
    ("RUN-4e2b6c77", "EAWF-0052", "info", "QUEUED", ""),
)
QUEUE_TARGET = "RUN-7b0e4d31"

#: Sandbox log: the Run each authorisation decision was about, in row order.
SANDBOX_RUNS: tuple[str, ...] = ("RUN-538453eb", "RUN-538453eb", "RUN-7b0e4d31", "RUN-7b0e4d31")

#: Release: the candidate's Milestones, their Track and when each was accepted.
RELEASE_MEMBERS: tuple[tuple[str, str, str], ...] = (
    ("MLS-0001 Replay-safe activity", "Runtime", "Jul 14"),
    ("MLS-0007 Calibration set", "Research", "Jul 15"),
    ("MLS-0004 Escape-ledger", "Trust", "not yet"),
)

#: Search: each hit, what matched and where.
SEARCH_HITS: tuple[tuple[str, str, str], ...] = (
    ("RUN-538453eb", "task title", "EAWF-0042 Bound replay"),
    ("RUN-be1e085a", "task title", "EAWF-0051 Coalesce"),
    ("EAWF-0042", "name", "Bound replay window"),
    ("BAT-0001", "name", "Semantic event slice"),
    ("MLS-0001", "name", "Replay-safe activity"),
    ("CLM-0004", "claim text", "ordering under replay"),
)
