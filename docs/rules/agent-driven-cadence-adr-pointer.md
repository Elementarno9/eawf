<!-- Generated from the eawf profile render block `agent-driven-cadence-adr-pointer`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=agent-driven-cadence-adr-pointer version=1.4 hash=01bcaa041d1d74c4 -->
# `agent-driven-cadence-adr-pointer`

The one-PR-per-Milestone cadence and the commit-history regime are ratified by a typed decision; cite its id in review and reverse the cadence only by superseding it.

### Rationale

The large-PR cadence contradicts the industry small-CL / trunk-based consensus on purpose, so a reader who hits the large-PR rule needs the evidence chain that ratified it — otherwise the rule reads as an oversight and gets "fixed" back to small-CL. Naming the decision keeps the divergence auditable and reversible from the decision ledger alone.


### Mechanism

The one-PR-per-Milestone cadence and the commit-history regime are ratified by Decision D87, which superseded D85, itself the replacement for D-GIT-HISTORY (D76) and the earlier one-PR-per-phase and rebase-merge decisions: each Batch closes and binds the phase-branch head when it lands, each push is squashed to one commit per Batch (a few for a large Batch) carrying its ``Batch:`` and ``Task:`` trailers plus one trailing ``state:`` commit, the bindings the squash replaced are rebound by appended superseding rows, and main and tags are never rewritten. The canonical workflow reference is ``docs/architecture/workflow.md`` (VCS, commit, PR, and merge policy). Cite the Decision id, not this rule's prose, when a review or roadmap discussion questions the cadence; reverse it only by superseding D87 with a new Decision row.


### Verification

The decision exists in the decision ledger and surfaces in ``eawf status`` recent decisions. ``docs/architecture/workflow.md`` resolves as a repo-relative path. A claim that small-CL should replace this rule is checked against the decision's status — only a superseding Decision discharges the divergence.
<!-- END EAWF:managed id=agent-driven-cadence-adr-pointer -->
