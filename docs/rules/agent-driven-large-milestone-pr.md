<!-- Generated from the eawf profile render block `agent-driven-large-milestone-pr`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=agent-driven-large-milestone-pr version=1.7 hash=9a1ec36ff89cb965 -->
# `agent-driven-large-milestone-pr`

Ship one PR per Milestone however large it grows, keep each Batch commit individually bisectable, and fast-forward main so each Batch commit survives.

### Rationale

The small-CL / trunk-based default exists to bound human-reviewer attention per change. Agent-driven throughput dwarfs that concern: the bottleneck is operator review of *coherent deliveries*, not line count, and a Milestone reviewed as one unit reads as one story. The granularity the small-CL rule buys is already present *inside* the PR — per-Batch bisectable commits give per-change history without fragmenting the review surface into dozens of context-free PRs.


### Mechanism

Ship one PR per Milestone. A Milestone PR carrying roughly +10k lines across many files is expected and acceptable, not a smell to split. Do not break a Milestone into many small PRs to chase a line-count target. The compensating controls are real per-Batch review and a behavioural smoke gate in CI; keep each Batch commit individually buildable (one commit per Batch, or a few for a large Batch) so ``git bisect`` stays sharp inside the large PR. Fast-forward ``main`` to the reviewed head so each Batch commit survives on the trunk with its reviewed SHA; main and tags are never rewritten.


### Verification

Confirm the Milestone produced exactly one PR and that its commits are per-Batch, each carrying one ``Batch:`` trailer and a ``Task:`` trailer per Task, and individually buildable. A reviewer who proposes splitting the Milestone into multiple PRs is overruled by this rule; a reviewer who finds two Batches folded into one commit without the squash naming why flags it for rework. ``main`` carries each reviewed commit of the merged PR with the same SHA.
<!-- END EAWF:managed id=agent-driven-large-milestone-pr -->
