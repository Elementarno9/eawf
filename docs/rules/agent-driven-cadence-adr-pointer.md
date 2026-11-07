<!-- Generated from the eawf profile render block `agent-driven-cadence-adr-pointer`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=agent-driven-cadence-adr-pointer version=1.7 hash=41d6d1c4160a4872 -->
# `agent-driven-cadence-adr-pointer`

The one-PR-per-Milestone cadence, the dev integration branch and the commit-history regime are ratified by a typed decision; cite its id in review and reverse the cadence only by superseding it.

### Rationale

The large-PR cadence contradicts the industry small-CL / trunk-based consensus on purpose, so a reader who hits the large-PR rule needs the evidence chain that ratified it — otherwise the rule reads as an oversight and gets "fixed" back to small-CL. Naming the decision keeps the divergence auditable and reversible from the decision ledger alone.


### Mechanism

The one-PR-per-Milestone cadence, the dev integration branch and the commit-history regime are set by Decision D94, which superseded D92. D94 keeps what D92 set and moves integration to ``dev``: Milestone PRs target ``dev``, pre-release tags ``{version}-dev.{n}`` are cut on ``dev``, a stable release fast-forwards ``main`` to a reviewed ``dev`` head and is tagged there, and CI runs a fast lane on PRs and the full matrix, coverage and pre-release on ``dev``. D92 superseded D87; D87 itself replaced D85, D-GIT-HISTORY (D76) and the earlier one-PR-per-delivery and rebase-merge decisions: each Batch closes and binds the Milestone branch head when it lands, the branch is squashed to one commit per Batch (a few for a large Batch) carrying its ``Batch:`` and ``Task:`` trailers plus one trailing ``state:`` commit only before review opens, the bindings the squash replaced are rebound by appended superseding rows, review findings are fixed by appended Batches, the Milestone is accepted last, the integration branch fast-forwards to the reviewed commit, and published branches and tags are never rewritten. The canonical workflow reference is ``docs/architecture/workflow.md`` (VCS, commit, PR, and merge policy). Cite the Decision id, not this rule's prose, when a review or planning discussion questions the cadence; reverse it only by superseding D94 with a new Decision row.


### Verification

The decision exists in the decision ledger and surfaces in ``eawf status`` recent decisions. ``docs/architecture/workflow.md`` resolves as a repo-relative path. A claim that small-CL should replace this rule is checked against the decision's status — only a superseding Decision discharges the divergence.
<!-- END EAWF:managed id=agent-driven-cadence-adr-pointer -->
