<!-- Generated from the eawf profile render block `planned-scope-revisability`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=planned-scope-revisability version=1.2 hash=7bf4dff7a0180ac1 -->
# `planned-scope-revisability`

Scope mutability is status-tiered: PLANNED scope is freely editable, ACTIVE scope is append-only with PENDING-only wave edits, and CLOSED scope changes only via a reopen.

### Planned-scope revisability

Phases and iters are first-class state records that move through ``PLANNED -> ACTIVE -> CLOSED`` (waves move through ``PENDING -> CLAIMED -> IN_PROGRESS -> CLOSED``). Mutability is status-tiered:

- **PLANNED** scope is freely mutable. ``eawf plan submit`` edits the phase before it activates.
- **ACTIVE** scope is append-only at the phase level — only PENDING waves under it may still be mutated. The W01 ``edit_wave_plan`` / ``remove_wave_plan`` / ``set_wave_deps`` transitions enforce the PENDING-only invariant on their own.
- **CLOSED** scope is immutable except via the retired ``phase reopen`` verb (which flips CLOSED back to ACTIVE; audit linkage is preserved for traceability).

Mid-flight reshapes go through ``eawf plan submit`` too; the same PENDING-only invariant applies. Drop-and-redo (the retired ``roadmap drop`` verb + ``eawf plan submit``) is the escape hatch when more than half the waves need to change.
<!-- END EAWF:managed id=planned-scope-revisability -->
