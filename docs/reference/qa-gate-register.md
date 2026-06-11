# Phase-wide QA-gate register

This is the living scenario-to-gate acceptance artifact for the eawf binding pass. It generalizes the TUI-only scenario-gate register to every quality domain in the phase: fleet, trust, cadence, and TUI. Each row names a subsystem scenario and the firing gate kind that proves it — the deterministic falsifier a wave cites as its acceptance source. Every iter's waves cite this register rather than re-inventing acceptance prose per wave.

The register is a contract, not a wishlist. A row's gate-kind column MUST name a kind that resolves against the live registry — either a registered audit-DSL `CheckKind` (the keys of `CHECK_REGISTRY` plus the state-scoring `CLOSE_GATE_KINDS` returned by `registered_audit_dsl_kinds()`) or a known `GateSpec` kind (the production-bound set returned by `wired_audit_dsl_kinds()`). A row that cites a phantom gate is a register defect: the register lint (`eawf.workflow.audit_dsl.register_lint.lint_register`) parses the gate-kind column and reds on any unresolved kind, so the acceptance artifact itself cannot cite a gate that does not exist. This applies the same idle-contract discipline the BIND-1 meta-gate enforces on code to the acceptance artifact.

## How to read a row

Each domain table is `Scenario | Gate kind | Notes`. The `Gate kind` cell is the canonical kind string the audit-DSL runner dispatches (back-quoted). The `Notes` cell records the binding-proof intent: what the gate falsifies and where it fires. A scenario with no named firing gate is a register gap, surfaced for the owning iter; do not leave the gate-kind cell empty to "fill later".

## How a wave cites the register

A wave's success criteria reference the register by repo-relative path (`docs/reference/qa-gate-register.md`) and the scenario it accepts. The wave's authored `GateSpec` rows carry the gate `kind` named in the matching register row, so the criterion's falsifier is the register's firing gate. When a wave adds a new scenario, it appends a row here in the same commit — the register grows with the phase, never trails it.

## Fleet

The spawn-substrate domain: cross-vendor session spawn, metering, sandbox safety, and the headless dispatch caller.

| Scenario | Gate kind | Notes |
|---|---|---|
| A spawned session returns a metered, priced, sandboxed result on every supported vendor | `command_exit_zero` | The cross-vendor parity gate runs the recording-spawn binding-proof and asserts a non-`$0` `SessionResult` per vendor. |
| A spawn argv is rejected when it violates the L0 deny-list policy | `schema_validate` | The argv-bearing `GateSpec` validator routes `args['argv']` through the L0 argv-policy; a denied argv fails `model_validate`. |
| A runaway wave's process group is reaped under the token cap | `command_exit_zero` | The pgid-threaded cap enforcement asserts the spawned pgid is killed; the negative path asserts an over-cap role is rejected at render. |
| A `$0`-priced spawn (a pricing-alias miss) is caught, never silently billed | `state_field_equals` | The metering binding-proof asserts the recorded cost field is non-zero on the live spawn path. |
| The headless `eawf dispatch wave` caller emits a typed `executor_report` body | `schema_validate` | The dispatch render path validates the emitted body against `AgentReportBody`; a malformed body fails validation. |

## Trust

The verdict-calibration domain: the cross-vendor jury, its earned authority, and the metrics that gate when it may block.

| Scenario | Gate kind | Notes |
|---|---|---|
| The cross-vendor jury blocks a UI/UX criterion only when its calibration clears the trust band | `command_exit_zero` | The jury-calibration gate reads the κ / Brier / ECE / Wilson-LB report and broadens the block band only when the four conditions hold. |
| An un-calibrated jury stays advisory rather than fabricating block authority | `state_field_equals` | The honest-negative path: with no scored report, the jury band stays advisory; the gate asserts it does not block on absent metrics. |
| A jury verdict's evidence chain resolves from state alone | `citation_resolves` | The verdict's cited audit / artifact rows resolve to typed citation rows; a dangling cite fails the citation gate. |
| The verifier is enforced (not idle) on the close path | `verify_implements` | The cadence-binding proof asserts `verify.enforce` reaches the close-readiness compute, so a criterion's gate actually fires at close. |

## Cadence

The lifecycle-discipline domain: the legacy-to-typed gate conversion, the wired-on sweep, and the backlog-resolution close gate.

| Scenario | Gate kind | Notes |
|---|---|---|
| Every wave success criterion has a real falsifying gate, never a grandfathered no-op | `criterion_in_diff` | The converted criterion's verification pattern is greppable across its file scopes; a vacuous criterion fails the diff gate. |
| No registered audit-DSL kind ships registered-but-idle | `command_exit_zero` | The BIND-1 idle-contract meta-gate compares `wired_audit_dsl_kinds()` against the full registered set; an idle kind reds. |
| A wave-linked backlog item is closed on land with resolution, commit, and audit | `backlog_resolution` | The state-scoring close gate reads wave-linked backlog items; a dangling item blocks close. |
| A commit prefix matches the canonical lifecycle grammar | `command_exit_zero` | The commit-prefix lint runs over the subject; a malformed prefix exits non-zero. |
| A schema migration carries a migration note when `state.json` `schema_version` advances | `regex_in_file` | The release pre-flight greps for a migration note; its absence fails the gate. |

## TUI

The operator-surface domain: the reskin render, per-key affordance parity, multi-step operator journeys, the crash-frame error boundary, and the SVG / mockup goldens.

| Scenario | Gate kind | Notes |
|---|---|---|
| Every advertised footer-hint key resolves to a live `Binding` in its mode | `affordance_parity` | The kind mounts the mode and drives each footer key through the real key->Binding path; an advertised dead key fails. |
| An operator journey reaches its terminal observable screen state | `tui_flow` | The kind drives a key sequence via the behaviour probe and asserts the terminal observable state; a divergent journey fails naming the field. |
| Every reskinned surface byte-matches its approved pick-time mockup golden | `mockup_golden_diff` | The Pilot harness captures the settled screen as normalised ASCII and byte-compares the mockup golden; a drifted surface fails with a region diff. |
| The lifecycle status FSM has every table edge covered by the stateful exploration | `transition_coverage` | The kind compares the full FSM edge set against the explored set; an uncovered edge fails naming it. |
| The committed seal SVG asset is well-formed | `svg_well_formed` | The kind shells `xmllint --noout` over the asset; a malformed SVG fails with the `line:col` diagnostic. |
| The seal SVG renders byte-identical to its committed golden PNG | `svg_pixel_diff` | The kind renders via the pinned `resvg` CLI with vendored fonts and byte-compares the golden; a drifted render fails. |
| A pane render exception is contained by the crash-frame error boundary, neighbours survive | `tui_flow` | A Pilot test injects a render exception into one pane and drives the `r` / `l` / `Esc` boundary keys; the App does not panic and the frame mounts. |
