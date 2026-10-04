---
name: reflect
description: "Report where effort, time and money actually went, without mutating canonical state."
argument-hint: "<run|show|export|prune> [--out <path>] [--local-only]"
user-invocable: true
disable-model-invocation: true
---

# /reflect

Report where effort, time and money actually went, without mutating canonical state.

## 1. Authority

- Only an authenticated operator initiates this skill, by design: it is kept out of the model's reach. An agent may prepare evidence or recommend the invocation, but never calls it.
- Operates on: measurement record, through `eawf reflect run`, `eawf reflect show`, `eawf reflect export`, `eawf reflect prune`.
- Effects: The reflect run, show, export and prune verbs: read-only apart from the session title fill, which --local-only disables, and the local report files.
- Allowed RPCs: none. This skill calls no daemon RPC.
- Canonical state: never mutated by this skill.
- Local write root: `.ea/local/reflect`; nothing is written outside it.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

Inputs are the current tree's measurement collections: runtime counters, actuals, and estimates, as the reflect verbs read them. Do not re-scrape provider session history; a second reader of the same facts drifts from the first, and the measured rows already carry harness, model, quality, and exclusion state. The one input from outside those collections is the local title cache, which holds provider answers keyed by digest hash and never a transcript line.

The read and the render send nothing anywhere, and no content excerpt is persisted beyond the bounded scrubbed title. The only egress is the title fill of the run action, which sends a scrubbed structural digest and never prompt text; --local-only disables it, every title then falls back to scrubbed_extract or structural, and the report states which source each title carries.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

Report where effort, time, and money actually went. This skill is operator-only and mutates no canonical state. It is read-only apart from one effect: the title fill of the run action, which sends a scrubbed structural digest to the provider and caches the answer, and which --local-only disables.

```text
/reflect <run|show|export|prune> [--out <path>] [--local-only]
```

Select exactly one action: `run`, `show`, `export`, `prune`. An option the selected action does not declare is refused before you start.

## 4. Method

1. Select the action. run reads the tree's Runs, fills their titles, and writes the report, to --out when given; show prints the newest report in the local collection; export writes the newest report as a static page, to --out when given; prune removes local reports and cached titles past their retention class.
2. Aggregate measured rows only. Never substitute an estimate for an actual, never render a ratio when either side is unavailable, and never compare across effort-unit mapping revisions without labelling the comparison. Exclude prior runs of this skill, and every helper session it spawned, from every baseline.
3. Classify excluded rows separately and report them in their own section with their reasons. Excluded work is still observed usage; it is removed from the baseline, not from the report.
4. Report unpriced spend as unpriced. A placeholder rate is never presented as a dollar figure, and zero is never printed where the source recorded unknown.
5. Mark every row by quotability. A statistic scoped to the current project is quotable into a committed artifact; a row that is not is labelled non-quotable in the report itself, so a later agent citing it can see the boundary rather than infer it.
6. For run, fill session titles from the local cache, then from the provider unless --local-only is set, sending only the scrubbed structural digest. Leave an unanswerable digest uncached, and fall back per title to scrubbed_extract then structural, stating the source each title carries.

## 4b. Applicable rules

No rule in the effective rule graph is scoped to this skill, which selects no activity or role; the repository policy still binds every session.

## 5. Constraints

- Persist statistics and metadata only. Bounded content may be inspected in memory to classify a candidate finding; no excerpt is persisted beyond the bounded scrubbed title.
- You are operator-invoked, you mutate no canonical state, and you send nothing beyond the scrubbed structural digests of the run action; producing agent-readable output does not make this skill agent-invocable.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one ReflectReport containing the measured totals by Track, Milestone, Batch, Task, and Run, estimate-versus-actual variance with its mapping revision, pricing quality, the excluded-row section, and every unavailable field named with its reason. Emit it in a form an agent can consume without re-deriving it: a machine-readable overview and typed highlighted issues beside the rendered prose, each issue carrying its subject, its evidence rows, and its quotability mark.

The report validates against `ReflectReport`, and its terminal outcome is exactly one of `reported`, `partial`, `unavailable`, `blocked`. Prose in the report is explanation, never the result. Check it with `eawf skill check-report /reflect` before returning it.
