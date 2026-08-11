<!-- Generated from the eawf profile render block `agent-driven-cadence-adr-pointer`. Do not hand-edit: re-run `eawf sync`. -->

# `agent-driven-cadence-adr-pointer`

The phase-equals-release and one-PR-per-phase divergences from small-CL practice are ratified by typed decisions; cite those ids in review and reverse the cadence only by superseding them.

### Rationale

These two divergences contradict the industry small-CL / trunk-based consensus on purpose, so a reader who hits the large-PR rule needs the evidence chain that ratified it — otherwise the rule reads as an oversight and gets "fixed" back to small-CL. Naming the decisions keeps the divergence auditable and reversible from ``state.json`` alone.


### Mechanism

The phase=release + large-phase-PR cadence is ratified by Decision D10 (keep one-PR-per-phase cadence) and the rebase-merge strategy by Decision D07 (rebase-and-merge; never squash). The canonical workflow reference is ``docs/architecture/workflow.md`` (VCS, commit, PR, and merge policy). Cite the Decision id, not this rule's prose, when a review or roadmap discussion questions the cadence; reverse the divergence only by superseding D10 with a new Decision row.


### Verification

The decisions exist in ``state.json`` and surface in the rendered ``## Decisions`` section: ``eawf decisions show`` (or grep the rendered AGENTS.md) lists D07 and D10. ``docs/architecture/workflow.md`` resolves as a repo-relative path. A claim that small-CL should replace this rule is checked against D10's status — only a superseding Decision discharges the divergence.
