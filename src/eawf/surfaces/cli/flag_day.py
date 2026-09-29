"""The epoch-2 verb that replaces each epoch-1 mutating verb after the flag day.

A tree that carries the epoch marker refuses every epoch-1 write with
``legacy_operation_removed``, whichever verb attempted it: the refusal
sits at the ``state.json`` write chokepoint, so a verb added later cannot
slip past it. What the chokepoint cannot know is which verb the operator
typed, so it cannot say what to run instead. This table says it, keyed by
the command path the operator typed (``wave claim``), and the error
envelope appends the answer to the refusal.

A path mapped to ``None`` names a verb the flag day retired with no
epoch-2 counterpart; saying so is guidance too, because it stops the
operator hunting for a replacement that does not exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

#: Every epoch-1 verb that writes epoch-1 state, mapped to the path of the
#: epoch-2 verb that replaces it, or ``None`` when the verb retired.
EPOCH1_REPLACEMENTS: Final[Mapping[str, str | None]] = {
    "actual recover": None,
    "actual start": None,
    "actual stop": None,
    "agent-report add": "run finish",
    "artifact add": "record append",
    "artifact file-spike-report": "record append",
    "artifact promote-contract": None,
    "artifact submit-evidence": "record evidence",
    "artifact update": "record append",
    "audit add": "record append",
    "audit integrity": "record append",
    "audit promote": "record append",
    "audit run": "record append",
    "audit set-verdict": "record append",
    "backfill titles": None,
    "backfill wave-intents": None,
    "backlog add": "task create",
    "backlog backfill-titles": None,
    "backlog close": "task complete",
    "backlog correct": None,
    "backlog edit": None,
    "backlog set-priority": None,
    "close cancel": None,
    "close rereceipt": "task prove",
    "close resume": None,
    "close submit": "task complete",
    "decision add": "record append",
    "decision promote": "record append",
    "decision supersede": "record append",
    "dispatch pause": None,
    "dispatch resume": None,
    "dispatch wave": "run create",
    "estimate set": None,
    "estimate update": None,
    "evidence attest": "record evidence",
    "flow abort": None,
    "flow run": None,
    "goal define": None,
    "hypothesis define": None,
    "hypothesis promote": "record append",
    "hypothesis verdict": None,
    "incident close": None,
    "incident open": None,
    "incident promote": "record append",
    "iter activate": "batch activate",
    "iter candidate-tag": "release candidate",
    "iter close": "batch complete",
    "iter open": "batch create",
    "iter plan": "plan submit",
    "outcome define": None,
    "outcome set": None,
    "phase activate": "milestone activate",
    "phase close": "milestone accept",
    "phase open": "milestone create",
    "phase prepare-close": "milestone open-review",
    "phase reopen": None,
    "plan promote": "plan submit",
    "project init": "repository create",
    "research promote": "record append",
    "question add": None,
    "question resolve": None,
    "roadmap apply": "plan apply",
    "roadmap drop": None,
    "roadmap propose": "plan submit",
    "roadmap revise": "plan submit",
    "session checkpoint": None,
    "session close": "run finish",
    "session recover": None,
    "session start": "run start",
    "spec archive": None,
    "spec convert-legacy": None,
    "spec init": None,
    "spec promote": None,
    "spec repoint-gates": None,
    "spec repoint-scopes": None,
    "spec rewrite-gate-kind": None,
    "spec sync": None,
    "state rpc": None,
    "track add": "track create",
    "track switch": None,
    "wave ack-drift": None,
    "wave autoland": "batch integrate",
    "wave blocks-rebuild": None,
    "wave budget consume": None,
    "wave budget set": None,
    "wave claim": "task claim",
    "wave close": "task complete",
    "wave dispatch": "run create",
    "wave dispatch-batch": "run create",
    "wave fail": "run fail",
    "wave fix-ci": None,
    "wave fix-ci-loop": None,
    "wave integration adopt": "batch adopt-landed",
    "wave land": "batch integrate",
    "wave land-batch": "batch integrate",
    "wave plan": "task create",
    "wave policy set": None,
    "wave release": None,
    "wave review": None,
    "wave update": None,
    "wave verify-commits": None,
    "worktree cleanup": None,
    "worktree create": None,
    "worktree merge-back": "batch integrate",
    "worktree path-fix": None,
    "worktree reconcile": None,
}


def replacement_guidance(command_path: str) -> str:
    """Return what to run instead of the refused epoch-1 verb at ``command_path``.

    Args:
        command_path: The verb path after ``eawf``, words separated by one
            space (``wave claim``); empty when no command was running.

    Returns:
        One clause naming the epoch-2 replacement, stating that the verb
        retired without one, or -- for a path the table does not carry --
        pointing at the four epoch-2 lifecycle nouns.
    """
    if command_path not in EPOCH1_REPLACEMENTS:
        return "use the epoch-2 verbs instead (eawf milestone|batch|task|run --help)"
    replacement = EPOCH1_REPLACEMENTS[command_path]
    if replacement is None:
        return f"`eawf {command_path}` retired at the flag day and no epoch-2 verb replaces it"
    return f"run `eawf {replacement}` instead, the epoch-2 verb that replaces `eawf {command_path}`"


__all__ = ["EPOCH1_REPLACEMENTS", "replacement_guidance"]
