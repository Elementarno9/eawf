<!-- Generated from the eawf profile render block `agent-driven-large-phase-pr`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=agent-driven-large-phase-pr version=1.1 hash=8ff4fec8e1a09746 -->
# `agent-driven-large-phase-pr`

Ship one PR per phase however large it grows, keep each wave commit individually bisectable, and merge with rebase rather than squash.

### Rationale

The small-CL / trunk-based default exists to bound human-reviewer attention per change. Agent-driven throughput dwarfs that concern: the bottleneck is operator review of *coherent deliveries*, not line count, and a phase reviewed as one unit reads as one story. The granularity the small-CL rule buys is already present *inside* the PR — per-wave bisectable commits give per-change history without fragmenting the review surface into dozens of context-free PRs.


### Mechanism

Ship one PR per phase. A phase PR carrying roughly +10k lines across many files is expected and acceptable, not a smell to split. Do not break a phase into many small PRs to chase a line-count target. The compensating controls are real per-wave review and a behavioural smoke gate in CI; keep per-wave commits individually bisectable (one wave per ``[P<NN>-W<NN>]`` commit) so ``git bisect`` stays sharp inside the large PR. Merge with rebase, never squash, so the per-wave history survives on the trunk.


### Verification

Confirm the phase produced exactly one PR (``gh pr list --search 'P<NN>'``) and that its commits are per-wave, each prefixed ``[P<NN>-W<NN>]`` and individually buildable. A reviewer who proposes splitting the phase into multiple PRs is overruled by this rule; a reviewer who finds a non-bisectable wave commit (two waves squashed into one) flags it for rework. The merge strategy is rebase, verified by a linear, non-squashed commit history on the target branch.
<!-- END EAWF:managed id=agent-driven-large-phase-pr -->
