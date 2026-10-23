<!-- Generated from the eawf profile render block `planned-scope-revisability`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=planned-scope-revisability version=1.3 hash=1e882ec248e74fb1 -->
# `planned-scope-revisability`

Scope mutability is status-tiered: a PLANNED Milestone is freely re-planned, an ACTIVE Milestone only takes appended Batches and Tasks, and started or completed work is never rewritten.

### Planned-scope revisability

Milestones, Delivery Batches and Tasks are first-class state records; a Milestone moves through ``PLANNED -> ACTIVE -> ACCEPTANCE_REVIEW -> COMPLETED`` (or ``CANCELLED``). A plan changes only through a PlanRevision that is submitted, approved and applied, and mutability is status-tiered:

- **PLANNED** scope is freely mutable. A child of an applied PlanRevision re-plans the PLANNED Milestone its parent created, rewriting only Batches and Tasks that are still PLANNED; what the child drops is cancelled, not deleted, so the history of the dropped work survives.
- **ACTIVE** scope is append-only. A re-plan of an ACTIVE Milestone may add new Batches and Tasks, and Batch refs after the required ones, but leaves every existing row exactly as it is, because work on it may be underway; the plan apply refuses any other change.
- **Started and completed** work is never rewritten by a plan: a correction to it is delivered as new scope.

A rejected PlanRevision is repaired by a child revision carrying a disposition for each finding, never by editing the rejected one.
<!-- END EAWF:managed id=planned-scope-revisability -->
