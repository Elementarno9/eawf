# Research campaigns walkthrough

*Start, steer, cancel, and inspect a multi-domain research campaign from the command line.*

A research campaign is one topic fanned out across several research domains. The daemon owns the campaign store, the round store, and the operator-input channel; the CLI verbs below proxy through the daemon (falling back to a direct write when the daemon is unavailable) so every mutation lands through the single canonical writer.

This page walks the operator surface end to end. The same actions are available in the TUI Research board (digit `3`); see the [TUI tour](tui-tour.md) for the keyboard surface.

## 1. Start a campaign

Stage a campaign for the active scope. The topic fans out across the domains declared in the scope's `research:` profile block:

```bash
eawf research campaign new "Survey the options-pricing landscape"
```

The command stages the plan-only campaign and persists it; the staged record surfaces in the Research board's topic tree. Staging never spawns a subprocess — it is a plan-only hand-off.

## 2. Track open questions

A campaign accumulates open questions as it surveys. Add one (the title is an imperative noun-phrase, 1–72 characters):

```bash
eawf research question add "which curve model fits the short tenor"
```

Mark a question as blocking when its answer gates further work — a blocking question is the one the balanced-autonomy interrupt raises to the operator:

```bash
eawf research question add "is the venue feed authoritative" --blocking
```

List the scope's open questions:

```bash
eawf research question list
```

Each row renders its id, status (`open` / `blocked` / `answered` / `dropped`), and a `blocking` marker when set. The verb exits `0` with `no open questions` when the scope has none.

## 3. Steer a running campaign

The operator channels push typed inputs onto the daemon-owned blackboard while a campaign runs. They are append-only, so every later round sees the input.

Steer a topic between rounds (narrow / widen / park — feedback, not a blocking interrupt):

```bash
# In the TUI Research board (digit 3): press t, type the steer note, Enter.
```

Broadcast a notice to every running round:

```bash
# In the TUI Research board: press b, type the notice, Enter.
```

Override a blocking fork with an operator verdict (a locked override persists across rounds until cleared):

```bash
# In the TUI Research board: press v, type the verdict, Enter.
```

The steer / broadcast / override channels target the campaign selected in the board's tree; a mid-run steer surfaces on the next round's recorded dispatch set.

## 4. Inspect the run

Render the campaign's progress, round, and checkpoint state:

```bash
eawf research status
```

The summary folds the staged campaigns, the executed rounds, and the open-question ledger into a single answer to "can the campaign proceed":

- `runnable` — the frontier has ready domain work.
- `blocked_await_user` — a blocking question or operator input is open (the round is soft-paused).
- `saturated` — the loop-until-dry gates all passed (a good terminal).

The line also reports `rounds` (how many rounds the run executed), `checkpoints` (rounds that coincided with an operator-review pause), and `open_questions`. The same campaign summary appears under [`eawf status`](../architecture/cli-surface.md) when a campaign is staged.

`eawf research status` exits `0` with `no research campaign staged` when the scope has staged none.

## 5. Cancel a campaign

Cancelling tombstones the campaign — the append-only store keeps the record (with a cancel time + reason) rather than deleting it, so the history stays traceable:

```bash
# In the TUI Research board: select the campaign node, press x.
```

A cancelled campaign no longer counts as live research signal on the board and is dropped from the topic tree.

## 6. Delete a draft / promote a synthesis

A campaign's surviving claims synthesise into a promotable research brief. Promotion runs the EviBound rung-1 gate over the brief's evidence references — a synthesis whose evidence does not resolve is rejected, a fully-referenced one promotes:

```bash
eawf research promote <slug>
```

To remove a local draft before promotion, delete the file under `.ea/local/research/`; drafts are local-only (gitignored) until promoted to `.ea/artifacts/`.

## See also

- [Quickstart](quickstart.md) — the command-only bootstrap path.
- [TUI tour](tui-tour.md) — the Research board keyboard surface.
- [Workflow](../architecture/workflow.md) — the research / plan / execute lifecycle.
