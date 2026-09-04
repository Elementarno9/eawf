# First workflow

*Carry a freshly initialized repository through one complete unit of tracked work.*

[Install](install.md) gets you a working command and [Quickstart](quickstart.md) gets you an initialized repository with an open phase. This page closes the front door: it walks the one loop that every later piece of work repeats.

## The unit of work

Work nests three levels deep, and each level has exactly one job:

| Level | Id | One of these is | Ends with |
| --- | --- | --- | --- |
| Phase | `P<NN>` | one delivery | one pull request and one release |
| Iter | `I<NN>` | one review-and-close cycle | an audit and a close |
| Wave | `W<NN>` | one agent's unit of work | one commit |

A wave is the smallest thing you dispatch, review, and close. Everything below is the loop that produces one.

## 1. Plan the phase

Planning is a roadmap proposal against the open phase, one phase at a time:

```bash
eawf roadmap propose --phase P01 --title "Add the first tracked workflow"
eawf roadmap show --phase P01 --md
eawf roadmap apply P01
```

The proposal lands in `PLANNED` status, which is the one status where scope is freely editable. Add and adjust waves until the dependency graph fits the delivery:

```bash
eawf roadmap revise P01 --add-wave ...
eawf roadmap revise P01 --set-deps ...
```

Once the phase is applied, its scope becomes append-only. That asymmetry is deliberate: it is cheap to redraw a plan and expensive to redraw a commitment.

## 2. Activate and dispatch

Activation is what turns a plan into runnable work:

```bash
eawf phase activate P01
eawf wave next-ready
```

`next-ready` lists the pending waves whose every dependency is closed. Under Claude Code the `/prep` command does the same thing with a rendered dependency graph and an approval prompt, which is the path to prefer — the raw verbs below are the surface it drives.

## 3. Execute one wave

```bash
eawf wave claim P01-I01-W01 --session operator-demo
```

A claim is rejected when a dependency is still open, or when a lower-numbered sibling is still pending with its own dependencies already satisfied. That ordering gate is what keeps a parallel fleet from interleaving into an unbisectable history; pass `--out-of-order` only when you are deliberately fanning out siblings of the same frontier.

Do the work, then commit it with the wave's prefix:

```text
[P01-I01-W01] feat: <summary>
```

Worktree-driven runs branch from the feature branch, commit there, and land the result by cherry-pick rather than merge, so the long-running branch keeps a linear history.

## 4. Close the wave

```bash
eawf wave close P01-I01-W01 --commit <sha>
```

Close only after the wave's scoped files, checks, and evidence are all real. Every success criterion needs evidence: a gate that ran with its exit code, a file-and-line citation for wired behaviour, or a decision reference. A criterion with no evidence blocks the close rather than passing quietly.

## 5. Audit, ship, close

Finishing the waves is not finishing the iter. The endgame, in order:

1. `/audit` — a fresh-context re-read of the diff against the success criteria.
2. `/polish` — consistency follow-ups, appended as waves to the same iter.
3. `/ship` — open the phase pull request, get CI green, and address review feedback as more waves on that same iter.
4. Close the iter and the phase in the final bookkeeping commit, before the merge.

Follow-up work found during audit or polish is appended to the current iter, not spun into a second one. A second iter is for a genuine scope expansion or a repair cycle.

## Where state lives

Reality lives in `.ea/state.json`. Specs describe intent; the ledger records what actually happened. Never hand-edit the ledger to make it agree with a spec — drive the mutation through the CLI and let the spec follow. The daemon is the sole canonical mutator, and the `eawf state` verbs proxy to it, falling back to a direct locked write only when the daemon is genuinely unavailable.

## Next

- [Workflow](../architecture/workflow.md) — the full lifecycle, including the skill algorithms.
- [Concepts](../concepts.md) — the vocabulary in one page.
- [CLI surface](../architecture/cli-surface.md) — the complete verb inventory.
- [Troubleshooting](troubleshooting.md) — recovering from a non-zero exit.
