<!-- Generated from the eawf profile render block `workflow-lifecycle`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=workflow-lifecycle version=1.1 hash=6c9d6ff0d522cde0 -->
# `workflow-lifecycle`

The lifecycle runs research, plan, execute waves, cherry-pick, ship phase, with a branch-currency check before opening or resuming any scope.

## Workflow lifecycle

Agent-driven lifecycle:

```
research → plan → execute waves → cherry-pick → ship phase
```

- **Research** is unstructured exploration of the proposal/plan.
- **Branch currency gate** = fetch and compare the current branch to the intended source branch before opening or resuming a phase, iter, or wave; rebase or fast-forward first when stale.
- **Plan** = open the next phase, enumerate waves, write per-wave success criteria.
- **Execute** = dispatch waves. Independent waves go in parallel via worktree-isolated subagents. Sequential waves run inline.
- **Cherry-pick** = bring worktree commits into the long-running feature branch. Never merge.
- **Ship** = open the phase PR, run CI, address review, merge.
<!-- END EAWF:managed id=workflow-lifecycle -->
