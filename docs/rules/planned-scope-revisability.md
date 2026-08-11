<!-- Generated from the eawf profile render block `planned-scope-revisability`. Do not hand-edit: re-run `eawf sync`. -->

# `planned-scope-revisability`

Scope mutability is status-tiered: PLANNED scope is freely editable, ACTIVE scope is append-only with PENDING-only wave edits, and CLOSED scope changes only via a reopen.

### Planned-scope revisability

Phases and iters are first-class state records that move through ``PLANNED -> ACTIVE -> CLOSED`` (waves move through ``PENDING -> CLAIMED -> IN_PROGRESS -> CLOSED``). Mutability is status-tiered:

- **PLANNED** scope is freely mutable. ``eawf roadmap revise <phase-id> --add-wave / --remove-wave / --set-deps / --retitle`` edits the phase before it activates.
- **ACTIVE** scope is append-only at the phase level — only PENDING waves under it may still be mutated. The W01 ``edit_wave_plan`` / ``remove_wave_plan`` / ``set_wave_deps`` transitions enforce the PENDING-only invariant on their own.
- **CLOSED** scope is immutable except via ``eawf phase reopen`` (which flips CLOSED back to ACTIVE; audit linkage is preserved for traceability).

Mid-flight reshapes go through ``eawf roadmap revise <active-phase>`` too; the same PENDING-only invariant applies. Drop-and-redo (``eawf roadmap drop`` + ``eawf roadmap propose``) is the escape hatch when more than half the waves need to change.
