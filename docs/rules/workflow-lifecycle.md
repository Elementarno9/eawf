<!-- Generated from the eawf profile render block `workflow-lifecycle`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=workflow-lifecycle version=1.3 hash=993af7fbdef759a1 -->
# `workflow-lifecycle`

The lifecycle runs research, plan, dispatch Tasks, cherry-pick, ship the Milestone, with a branch-currency check before opening or resuming any scope.

## Workflow lifecycle

Agent-driven lifecycle:

```
research → plan → dispatch Tasks → cherry-pick → ship Milestone
```

- **Research** is unstructured exploration of the proposal/plan.
- **Branch currency gate** = fetch and compare the current branch to the intended source branch before opening or resuming a Milestone, Batch, or Task; rebase or fast-forward first when stale.
- **Plan** = propose a PlanRevision for the Milestone: its Delivery Batches, their Tasks, dependencies and per-Task success criteria; the operator approves and applies it.
- **Dispatch** = run each ready Task of a Batch in its own Run, one lease and one workspace per Task. Tasks the dependency graph and write claims leave independent run side by side; a dependent Task waits for the proof it needs.
- **Cherry-pick** = bring worktree commits into the long-running Milestone branch. Never merge.
- **Ship** = squash the branch to one commit per Batch, open the Milestone PR, run CI, fix review findings by appending Batches, accept the Milestone last, then fast-forward main to the reviewed commit.
<!-- END EAWF:managed id=workflow-lifecycle -->
