# First workflow

*Carry a freshly initialized repository through one tracked task, from plan to proof.*

[Install](install.md) gets you a working command and [Quickstart](quickstart.md) shows what `eawf init` writes. This page closes the front door: it walks one task through the whole native loop, and a test replays every command on it, in order, in a fresh repository, so the page cannot drift away from the CLI.

## The native model

Work is planned and recorded as five nested records, each with one job:

| Record | Key | One of these is | Ends when |
| --- | --- | --- | --- |
| Track | `TRK-<CODE>` | a long-running line of work with its own policy | it is retired |
| Milestone | `MLS-<NNNN>` | one outcome, with the acceptance journey that proves it | it is accepted |
| Batch | `BAT-<NNNN>` | the tasks that integrate into one branch together | its merge is reconciled |
| Task | `<CODE>-<NNNN>` | one unit of work with its success criteria | it is proved on the integrated commit |
| Run | `RUN-<NNNNNNNN>` | one attempt at a task, by an agent or by you | it finishes or fails |

Every record is addressed by a URN (uniform resource name) of the form `eawf://<workspace>/<project>/<repository>/<kind>/<KEY>`. This page uses the project code `DEMO` for all three segments, so the task below is `eawf://DEMO/DEMO/DEMO/task/DEMO-0001`.

## Before you start

Start from a fresh Git repository whose `main` branch has at least one commit, and run everything from its root. The page initializes the repository itself, with the scripted form of `eawf init` from Quickstart, so every key and URN below is fixed; a repository you already initialized refuses a second `init`. The request documents this page asks you to save go under `.ea/local/`, which `eawf init` adds to `.gitignore`.

Every command that changes a record is a request to the daemon, the background process that is the only writer of project state, and carries three flags:

- `--actor` names who the change is attributed to.
- `--idempotency-key` names the request, so a retry replays its receipt instead of applying twice.
- `--expected-<kind>-revision` is the revision you read the record at; the daemon refuses the change if the record has moved since. A create takes `--expected-revision` instead: the tree's canonical sequence, which starts at `0` and grows by one with every committed change. Each receipt prints the new value as `result.canonical_sequence`.

The daemon starts on the first command that needs it. Before it commits a change, each command prints a consequence card saying what will move and what will not.

## 1. Initialize and register

```bash
eawf --no-input init --project-code DEMO --project-title "Demo Project" --profiles core
eawf repo register . --yes
eawf workspace add DEMO --home DEMO
```

`init` bears the tree at authority epoch 2, the native model this page uses. `repo register` and `workspace add` list the repository in your user-scope registry, which is what lets `eawf ui` find it later.

## 2. Plan a track, a milestone, a task and a batch

Each record is admitted from a create document. Save the track as `.ea/local/track.json`:

<!-- eawf:file .ea/local/track.json -->

```json
{
  "key": "TRK-DEMO",
  "title": "Ship the demo",
  "charter": "Carry one task through the native loop.",
  "scope": {"scope_kind": "repository", "repository_ref": "eawf://DEMO/DEMO/DEMO/repository/DEMO"},
  "owner": {"principal_kind": "operator", "principal_id": "OPERATOR"},
  "policy": {
    "revision": 1,
    "wip": {"active_milestones": 1, "active_batches_per_repo": 1},
    "ownership_principal": {"principal_kind": "operator", "principal_id": "OPERATOR"},
    "permitted_milestone_kinds": ["product"],
    "campaign_templates": [],
    "outcome_metrics": [],
    "promotion_rules": [],
    "integration_priority": 50,
    "presentation": {"default_view": "roadmap", "color_token": "accent_blue"}
  }
}
```

The milestone names the outcome and the acceptance journey that will prove it. Save it as `.ea/local/milestone.json`:

<!-- eawf:file .ea/local/milestone.json -->

```json
{
  "key": "MLS-0001",
  "primary_track_ref": "eawf://DEMO/DEMO/DEMO/track/TRK-DEMO",
  "title": "Greet the user",
  "outcome": "The repository ships a module that prints a greeting.",
  "appetite": "S",
  "exclusions": ["any localized greeting"],
  "acceptance_journey": [
    {
      "step_id": "AS-01",
      "actor": "operator",
      "action": "run the greeting module",
      "expected_observation": "it prints hello",
      "evidence_kinds": ["artifact"]
    }
  ]
}
```

A task starts as a draft in the backlog, with only a priority and an intent. Save it as `.ea/local/task.json`:

<!-- eawf:file .ea/local/task.json -->

```json
{"key": "DEMO-0001", "priority": "P1", "intent": "Add a module that prints a greeting"}
```

The batch groups the tasks that integrate together. Save it as `.ea/local/batch.json`:

<!-- eawf:file .ea/local/batch.json -->

```json
{
  "key": "BAT-0001",
  "milestone_ref": "eawf://DEMO/DEMO/DEMO/milestone/MLS-0001",
  "repository_ref": "eawf://DEMO/DEMO/DEMO/repository/DEMO",
  "task_refs": ["eawf://DEMO/DEMO/DEMO/task/DEMO-0001"]
}
```

Admit the four records. The tree starts at canonical sequence `0` and each create moves it by one:

```bash
eawf track create eawf://DEMO/DEMO/DEMO/track/TRK-DEMO --expected-revision 0 --idempotency-key create-track --actor OPERATOR --from-spec .ea/local/track.json
eawf milestone create eawf://DEMO/DEMO/DEMO/milestone/MLS-0001 --expected-revision 1 --idempotency-key create-milestone --actor OPERATOR --from-spec .ea/local/milestone.json
eawf task create eawf://DEMO/DEMO/DEMO/task/DEMO-0001 --expected-revision 2 --idempotency-key create-task --actor OPERATOR --from-spec .ea/local/task.json
eawf batch create eawf://DEMO/DEMO/DEMO/batch/BAT-0001 --expected-revision 3 --idempotency-key create-batch --actor OPERATOR --from-spec .ea/local/batch.json
```

## 3. Activate the plan and promote the task

A batch activates only once the branch it integrates into is pinned. Save `.ea/local/batch-activate.json`:

<!-- eawf:file .ea/local/batch-activate.json -->

```json
{"updates": {"target_branch": "main"}}
```

Promotion moves the task out of the backlog, and it needs the three things a planned task carries: its batch, its due scope and at least one success criterion. The criterion below is proved deterministically, by the gate `G-01` you will write in step 6. Save `.ea/local/task-promote.json`:

<!-- eawf:file .ea/local/task-promote.json -->

```json
{
  "updates": {
    "batch_ref": "eawf://DEMO/DEMO/DEMO/batch/BAT-0001",
    "due_scope": "eawf://DEMO/DEMO/DEMO/milestone/MLS-0001",
    "criteria": [
      {
        "id": "CR-01",
        "text": "the greeting module prints hello",
        "kind": "behavioral",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "gate_ids": ["G-01"],
        "quality_dimension": "functional_suitability",
        "measurable_signal": "git grep finds the greeting in src/hello.py"
      }
    ]
  }
}
```

Each record starts at revision `1`:

```bash
eawf milestone activate eawf://DEMO/DEMO/DEMO/milestone/MLS-0001 --expected-milestone-revision 1 --idempotency-key activate-milestone --actor OPERATOR
eawf batch activate eawf://DEMO/DEMO/DEMO/batch/BAT-0001 --expected-batch-revision 1 --idempotency-key activate-batch --actor OPERATOR --from-spec .ea/local/batch-activate.json
eawf task promote eawf://DEMO/DEMO/DEMO/task/DEMO-0001 --expected-task-revision 1 --idempotency-key promote-task --actor OPERATOR --from-spec .ea/local/task-promote.json
```

## 4. Claim the task and start a run

A run is one attempt at the task. Save its create document as `.ea/local/run.json`, then the stamp it starts with as `.ea/local/run-start.json`, and the run the task starts on as `.ea/local/task-start.json`:

<!-- eawf:file .ea/local/run.json -->

```json
{
  "key": "RUN-00000001",
  "scope": {
    "scope_kind": "task",
    "purpose": "implement",
    "task_ref": "eawf://DEMO/DEMO/DEMO/task/DEMO-0001",
    "write_set": ["src"]
  }
}
```

<!-- eawf:file .ea/local/run-start.json -->

```json
{"updates": {"started_at": "2026-09-01T09:00:00Z"}}
```

<!-- eawf:file .ea/local/task-start.json -->

```json
{"updates": {"active_run_ref": "eawf://DEMO/DEMO/DEMO/run/RUN-00000001"}}
```

Claim the task, open the run, and start both. The eight changes so far put the tree at canonical sequence `8`:

```bash
eawf task claim eawf://DEMO/DEMO/DEMO/task/DEMO-0001 --expected-task-revision 2 --idempotency-key claim-task --actor OPERATOR
eawf run create eawf://DEMO/DEMO/DEMO/run/RUN-00000001 --expected-revision 8 --idempotency-key create-run --actor OPERATOR --from-spec .ea/local/run.json
eawf run start eawf://DEMO/DEMO/DEMO/run/RUN-00000001 --expected-run-revision 1 --idempotency-key start-run --actor OPERATOR --from-spec .ea/local/run-start.json
eawf task start eawf://DEMO/DEMO/DEMO/task/DEMO-0001 --expected-task-revision 3 --idempotency-key start-task --actor OPERATOR --from-spec .ea/local/task-start.json
```

## 5. Do the work and record its evidence

The work itself is ordinary. Save `src/hello.py`:

<!-- eawf:file src/hello.py -->

```python
print("hello")
```

Commit it to `main`, then record what it shows as evidence against the task:

```bash
git add src/hello.py
git commit -m "feat: add the greeting"
eawf record evidence eawf://DEMO/DEMO/DEMO/task/DEMO-0001 --kind artifact --summary "src/hello.py prints the greeting" --expected-revision 4 --idempotency-key record-greeting --actor OPERATOR
```

The receipt names the evidence `eawf://DEMO/DEMO/DEMO/evidence/EVD-0001`. Finish the run with its report bound, and mark the task ready to integrate on that evidence. Save `.ea/local/run-finish.json` and `.ea/local/task-ready.json`:

<!-- eawf:file .ea/local/run-finish.json -->

```json
{"observations": ["run_report_bound"], "updates": {"ended_at": "2026-09-01T10:00:00Z"}}
```

<!-- eawf:file .ea/local/task-ready.json -->

```json
{"observations": ["run_report_bound"], "binding_refs": ["eawf://DEMO/DEMO/DEMO/evidence/EVD-0001"]}
```

```bash
eawf run finish eawf://DEMO/DEMO/DEMO/run/RUN-00000001 --expected-run-revision 2 --idempotency-key finish-run --actor OPERATOR --from-spec .ea/local/run-finish.json
eawf task ready eawf://DEMO/DEMO/DEMO/task/DEMO-0001 --expected-task-revision 4 --idempotency-key ready-task --actor OPERATOR --from-spec .ea/local/task-ready.json
```

## 6. Prove the task and close it

The commit already landed on the batch's target branch, so the batch adopts it rather than integrating a candidate from an agent worktree. Adoption reads the commits back from Git, and cites the evidence by its key:

```bash
eawf batch adopt-landed eawf://DEMO/DEMO/DEMO/batch/BAT-0001 --head $(git rev-parse HEAD) --base $(git rev-parse HEAD~1) --task eawf://DEMO/DEMO/DEMO/task/DEMO-0001 --evidence EVD-0001 --expected-batch-revision 2 --idempotency-key adopt-greeting --actor OPERATOR
```

A proof runs the gate behind every criterion at the adopted commit and files a receipt per gate. Save the gate as `.ea/local/gates.json`:

<!-- eawf:file .ea/local/gates.json -->

```json
{
  "gates": [
    {
      "id": "G-01",
      "criterion_id": "CR-01",
      "kind": "command_exit_zero",
      "args": {"argv": ["git", "grep", "-q", "hello", "--", "src/hello.py"]},
      "policy": "block",
      "cadence": "every-wave"
    }
  ]
}
```

Prove the task, write the assessment that judges it, and complete it on the commit the assessment names:

```bash
eawf task prove eawf://DEMO/DEMO/DEMO/task/DEMO-0001 --gates .ea/local/gates.json --expected-task-revision 5 --idempotency-key prove-task --actor OPERATOR --wait
eawf task assess eawf://DEMO/DEMO/DEMO/task/DEMO-0001 --actor OPERATOR --out .ea/local/assessment.json
eawf task complete eawf://DEMO/DEMO/DEMO/task/DEMO-0001 --expected-task-revision 5 --idempotency-key complete-task --actor OPERATOR --integrated-commit $(git rev-parse HEAD) --assessment .ea/local/assessment.json
```

`task complete` refuses unless every criterion stands on a passing receipt taken at the named commit, and it records the binding the daemon derives rather than any the caller supplies. The task is now `COMPLETED`.

## 7. View it in the console

```bash
eawf ui
```

In a terminal, `eawf ui` opens the Eä console on the registered workspace; off a terminal it prints the console's plain frame and exits. Pass `--actor OPERATOR` to act from the console as well as read it. The [TUI tour](tui-tour.md) walks the console itself.

## What comes after

The batch and the milestone close on the same pattern of revision-checked verbs. The batch moves through `eawf batch ready`, `eawf batch merge`, `eawf batch reconcile`, `eawf batch observe-merge` and `eawf batch complete`, where `reconcile` reads the merge back from the branch. The milestone opens its acceptance review with `eawf milestone open-review`, and is accepted through `eawf milestone open-approval`, `eawf milestone seal-approval` and `eawf milestone accept`, which cite recorded evidence for every step of its acceptance journey. Run any of them with `--help` for its request document.

Work an agent does in a leased worktree takes the other path into a batch: `eawf task submit` and `eawf task seal` hand over its candidate, and `eawf batch integrate` assembles it, in place of `eawf batch adopt-landed`.

## Where state lives

Reality lives in the epoch-2 generation under `.ea/generations/`; `.ea/state.json` is the frozen epoch-1 document, kept readable. Specs describe intent and the ledger records what actually happened. Never hand-edit either to make it agree with a spec: drive the change through the CLI and let the spec follow. The daemon is the only writer, and every verb above is a request to it.

## Next

- [Workflow](../architecture/workflow.md) — the full lifecycle, including the skill algorithms.
- [Concepts](../concepts.md) — the vocabulary in one page.
- [CLI surface](../architecture/cli-surface.md) — the complete verb inventory.
- [Troubleshooting](troubleshooting.md) — recovering from a non-zero exit.
