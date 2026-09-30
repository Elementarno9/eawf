# Research campaigns walkthrough

*Plan, drive, follow and cancel a research Campaign from the command line.*

A research Campaign investigates a set of questions under one Track through an approved plan of bounded steps. Each step is one Run over one question, so its spend, its report and its outcome are recorded against it. The daemon owns the Campaign record: the CLI verbs below forward to its `runtime.campaign.*` verbs, and the console's Campaign screen reads the same record as the rounds land.

This page walks the operator surface end to end.

## 1. Plan a Campaign

Give the Campaign its title, the Track that owns it, and the questions it investigates, the Campaign's own question first:

```bash
eawf campaign new "Establish whether replay preserves event order" \
  --actor OP-0001 --track <track-urn> \
  --question "Does replay preserve event order?" \
  --question "Which restarts reorder events?"
```

The daemon files each question as an open question and plans one step per question and method, then one synthesis step that depends on every other. The depth sets the methods: `shallow` and `medium` survey each question, `deep` adds an adversarial pass, and `exhaustive` adds a depth pass as well. Without `--depth` the `research.default_depth` setting decides. The plan is approved as shown, with every step pending.

A Campaign is bounded by its budget. Without one, it gets one round per step; `--budget-rounds` and `--budget-tokens` set hard limits instead.

## 2. Drive it

```bash
eawf campaign run CAM-0001 --actor OP-0001
```

The daemon drives the plan round by round. Each round starts every ready step on a Run of its own, up to the fan-out width: `--agents`, or the `research.agent_count` setting. A step is ready once every step it depends on is done and no open contradiction blocks it. For each round, the research agent investigates the step's question read-only, and then the daemon:

1. charges the round's spend to the step and to the Campaign;
2. keeps the round's report as an artifact revision of the step;
3. finishes the step with the outcome the agent stated, and promotes the findings it names as held `CFN-####` findings.

`eawf campaign new --run` plans and starts driving in one go.

## 3. Follow it

The console's Campaign screen shows the plan line (`1 of 3 steps done · 1 running · 1 blocked by step 2`), each step with its runner, spend and outcome, each artifact revision, and the promoted findings. The same record is read through `projection.campaign.view`, and each artifact through `projection.campaign.artifact`.

## 4. How a drive ends

- **Converged.** Every step is done, and the Campaign converges.
- **Budget exhausted.** A hard budget axis reached its limit while steps still waited, so no further step starts. The Campaign records a budget stop naming the axis, and converges with that stop once nothing is running, so a starved Campaign never reads as one that converged on its own.
- **Paused.** A round failed, and its step returned to pending. `eawf campaign run` resumes the drive on a new Run. A step a stopped daemon left running is returned to pending when the next drive starts.

To widen the questions, the steps or the budget, revise the plan through `runtime.campaign.plan.revise`. A revision replaces only the steps that have not started; every started step stays as it stands.

## 5. Cancel a Campaign

```bash
eawf campaign cancel CAM-0001 --actor OP-0001 --reason "superseded by a narrower question"
```

Cancelling records the reason on the Campaign and keeps its steps, artifact revisions and findings. A cancelled Campaign takes no further changes.

## 6. Keep a brief

To remove a local draft before promotion, delete the file under `.ea/local/research/`. Drafts stay local-only (gitignored) until promoted to `.ea/artifacts/`, which happens in the commit that lands the decision the brief supports.

## See also

- [Quickstart](quickstart.md) — the command-only bootstrap path.
- [TUI tour](tui-tour.md) — the operator console.
- [Workflow](../architecture/workflow.md) — the research / plan / execute lifecycle.
