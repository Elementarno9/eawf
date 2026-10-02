"""What an operator runs instead of an epoch-1 verb, after the flag day.

Two tables answer that. :data:`EPOCH1_REPLACEMENTS` covers the epoch-1 verbs
still on the command tree -- the migration-support verbs a tree needs to
reach the cutover. A tree that carries the epoch marker refuses their writes
with ``legacy_operation_removed`` at the ``state.json`` write chokepoint,
which cannot know which verb the operator typed; the table says what to run
instead, keyed by the typed command path, and the error envelope appends it.

:data:`RETIRED_VERBS` covers the verbs the flag day removed from the tree.
The root group answers them before click would report an unknown command,
naming the verb that replaces each, or -- where nothing does -- pointing at
``eawf migrate epoch2 --plan``, the way to inspect an epoch-1 tree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

#: The command a retired verb with no replacement points at.
MIGRATION_PLAN_VERB: Final = "migrate epoch2 --plan"

#: The epoch-1 verbs still on the tree, mapped to the epoch-2 verb that
#: replaces each, or ``None`` when none does.
EPOCH1_REPLACEMENTS: Final[Mapping[str, str | None]] = {
    "session close": "run finish",
    "session recover": None,
    "worktree cleanup": None,
    "worktree merge-back": "batch integrate",
    "worktree reconcile": None,
}

#: Verbs the flag day removed from the command tree, mapped to the verb that
#: replaces each, or ``None`` when none does.
RETIRED_VERBS: Final[Mapping[str, str | None]] = {
    "actual recover": None,
    "actual start": None,
    "actual stop": None,
    "agent-report add": "run finish",
    "agent-report list": None,
    "agent-report show": None,
    "artifact add": "record append",
    "artifact file-spike-report": "record append",
    "artifact promote-contract": None,
    "artifact show": None,
    "artifact submit-evidence": "record evidence",
    "artifact update": "record append",
    "artifact validate": None,
    "artifact verify": None,
    "audit add": "record append",
    "audit integrity": "record append",
    "audit list": None,
    "audit promote": "record append",
    "audit run": "record append",
    "audit set-verdict": "record append",
    "audit show": None,
    "backfill titles": None,
    "backfill wave-intents": None,
    "backlog add": "task create",
    "backlog backfill-titles": None,
    "backlog close": "task complete",
    "backlog correct": None,
    "backlog edit": None,
    "backlog set-priority": None,
    "calibrate apply": "metrics refit",
    "calibrate buckets": "metrics refit",
    "close cancel": None,
    "close follow": None,
    "close rereceipt": "task prove",
    "close resume": None,
    "close status": None,
    "close submit": "task complete",
    "decision add": "record append",
    "decision graph": None,
    "decision list": None,
    "decision promote": "record append",
    "decision supersede": "record append",
    "dispatch pause": "run pause-dispatch",
    "dispatch resume": "run resume-dispatch",
    "dispatch wave": "run create",
    "draft new": None,
    "draft validate": None,
    "estimate set": None,
    "estimate update": None,
    "evidence attest": "record evidence",
    "flow abort": None,
    "flow run": None,
    "flow status": None,
    "goal define": None,
    "hypothesis define": None,
    "hypothesis list": None,
    "hypothesis promote": "record append",
    "hypothesis verdict": None,
    "incident close": None,
    "incident open": None,
    "incident promote": "record append",
    "incident view": None,
    "iter activate": "batch activate",
    "iter candidate-tag": "release candidate",
    "iter close": "batch complete",
    "iter open": "batch create",
    "iter plan": "plan submit",
    "operator rollup": None,
    "outcome define": None,
    "outcome set": None,
    "phase activate": "milestone activate",
    "phase close": "milestone accept",
    "phase open": "milestone create",
    "phase prepare-close": "milestone open-review",
    "phase reopen": None,
    "phase retro": None,
    "plan promote": "plan submit",
    "project init": "repository create",
    "question add": None,
    "question list": None,
    "question resolve": "question reply",
    "repo link": "workspace member add",
    "repo link-workspace": "workspace member add",
    "research campaign cancel": "campaign cancel",
    "research campaign new": "campaign new",
    "research campaign run": "campaign run",
    "research promote": "record append",
    "research question add": None,
    "research question list": None,
    "research question resolve": "question reply",
    "research show": None,
    "research status": None,
    "roadmap apply": "plan apply",
    "roadmap drop": None,
    "roadmap propose": "plan submit",
    "roadmap revise": "plan submit",
    "roadmap show": None,
    "session checkpoint": None,
    "session start": "run start",
    "skill resume": "question reply",
    "spec archive": None,
    "spec convert-legacy": None,
    "spec init": None,
    "spec promote": None,
    "spec repoint-gates": None,
    "spec repoint-scopes": None,
    "spec rewrite-gate-kind": None,
    "spec show": None,
    "spec sync": None,
    "spec validate": None,
    "state resolve": None,
    "state rpc": None,
    "state show": "status",
    "track add": "track create",
    "track switch": None,
    "track sync": None,
    "tui": "ui",
    "wave ack-drift": None,
    "wave archive-refs": None,
    "wave autoland": "batch integrate",
    "wave blocks-rebuild": None,
    "wave budget consume": None,
    "wave budget set": None,
    "wave budget show": None,
    "wave claim": "task claim",
    "wave close": "task complete",
    "wave dispatch": "run create",
    "wave dispatch-batch": "run create",
    "wave fail": "run fail",
    "wave fix-ci": None,
    "wave fix-ci-loop": None,
    "wave graph": None,
    "wave integration adopt": "batch adopt-landed",
    "wave integration show": None,
    "wave land": "batch integrate",
    "wave land-batch": "batch integrate",
    "wave next-ready": None,
    "wave plan": "task create",
    "wave policy set": None,
    "wave policy show": None,
    "wave prune-branches": None,
    "wave release": None,
    "wave review": None,
    "wave show": None,
    "wave update": None,
    "wave verify-commits": None,
    "wave waivers": None,
    "workspace add-repo": "workspace member add",
    "workspace init": "workspace add",
    "workspace remove-repo": "workspace member remove",
    "workspace status": "workspace show",
    "workspace validate": "workspace show",
    "worktree create": None,
    "worktree list": None,
    "worktree path-fix": None,
}

#: The longest retired command path, in words.
_LONGEST_RETIRED: Final = max(len(path.split()) for path in RETIRED_VERBS)


def retired_verb(args: Sequence[str]) -> str | None:
    """Return the retired verb ``args`` begins with, or ``None``.

    Args:
        args: The tokens after the root's own options.

    Returns:
        The retired command path, or ``None`` when ``args`` names none.
    """
    for length in range(_LONGEST_RETIRED, 0, -1):
        path = " ".join(args[:length])
        if path in RETIRED_VERBS:
            return path
    return None


def replacement_guidance(command_path: str) -> str:
    """Return what to run instead of the epoch-1 verb at ``command_path``.

    Args:
        command_path: The verb path after ``eawf``, words separated by one
            space (``wave claim``); empty when no command was running.

    Returns:
        One clause naming the replacement, or -- for a verb nothing replaces
        -- pointing at ``eawf migrate epoch2 --plan``; for a path neither
        table carries, pointing at the four epoch-2 lifecycle nouns.
    """
    table = RETIRED_VERBS if command_path in RETIRED_VERBS else EPOCH1_REPLACEMENTS
    if command_path not in table:
        return "use the epoch-2 verbs instead (eawf milestone|batch|task|run --help)"
    replacement = table[command_path]
    if replacement is None:
        return (
            f"no epoch-2 verb replaces `eawf {command_path}`; inspect or migrate an epoch-1 "
            f"tree with `eawf {MIGRATION_PLAN_VERB}`"
        )
    return f"run `eawf {replacement}` instead, the verb that replaces `eawf {command_path}`"


__all__ = [
    "EPOCH1_REPLACEMENTS",
    "MIGRATION_PLAN_VERB",
    "RETIRED_VERBS",
    "replacement_guidance",
    "retired_verb",
]
