---
name: prep
description: Activate the next PLANNED phase: surface its DAG for operator approval, then run the activate_phase hard gate and dispatch subagents per wave.
argument-hint: "<phase-id>"
user-invocable: true
disable-model-invocation: true
---

# /prep

## Canonical algorithm

`/prep` activates a PLANNED phase. The flow branches on the phase's
PLANNED-queue state:

1. Resolve `<phase-id>` against `state.phases`.
2. Branch on phase status + wave plan:

   - **Case A — PLANNED phase with at least one PENDING wave.**
     Render the plan via `eawf roadmap show --phase <id> --md`.
     Enter Claude Code plan mode (`EnterPlanMode`) with the rendered
     DAG, then surface an `AskUserQuestion` with the options
     `use-as-is`, `revise`, `replace`, `cancel`. On `use-as-is`,
     call `eawf phase activate <id>` (which runs the V11 hard
     gate: ≥1 wave + deps phases CLOSED). On `revise`, hand back to
     `/roadmap revise`. On `replace`, hand back to `/roadmap drop`
     + `/roadmap propose`.
   - **Case B — PLANNED phase with empty wave DAG.** Dispatch the
     `planner` agent (`build/eawf-plugin/agents/planner.md`). The
     planner returns a sequence of `eawf roadmap revise --add-wave`
     commands (or a YAML payload). **Apply the planner's commands
     first** through the state CLI — waves land as PENDING on the
     still-PLANNED iter — then render the resulting DAG via
     `eawf roadmap show --phase <id> --md` and enter Claude Code
     plan mode (`EnterPlanMode`) with that rendering. Surface
     `AskUserQuestion` with `approve`, `edit`, `cancel`. The
     operator reviews the rendered roadmap, not the planner's raw
     commands. Edits during plan mode are `/roadmap revise` calls
     (PLANNED scope is mutable). On `approve`, call
     `eawf phase activate <id>` (V11 hard gate).
   - **Case C — no PLANNED phase by that id.** Reject with exit 4
     and hint `Run \`eawf roadmap propose --phase <id> --title ...\`
     first.` for the operator.

3. **Optional spike first.** Before claiming a wave whose success
   criteria are not yet writable, run `/research <topic>` as a *spike*
   (read-only) per the `spike-workflow` rule in AGENTS.md. The spike
   produces a brief under `.ea/local/<YYYY-MM-DD>-<slug>.md` (or the
   conventional `.ea/local/research/` sub-directory). When a matching
   spike brief exists, the plan-mode proposal in case A MUST reference
   it by repo-relative path so the operator and the dispatched executor
   read the same source-of-truth artifact — the wave dispatch renderer
   surfaces matching briefs under a `## References` section
   automatically.
4. For each parallel wave under the activated iter, dispatch a
   worktree subagent.
5. For each sequential wave, run inline; cherry-pick parallel-wave
   commits in between as they finish.
6. Validate the rendered plan with `eawf plan show --md`; wave tags
   and bucket roll-ups must match state.

## Pre-flight checklist

- [ ] Confirm current branch is the long-running phase branch.
- [ ] Confirm `git status` is clean.
- [ ] Confirm worktree subagents branch from the parent HEAD.
- [ ] Every wave has success criteria, agent role, effort bucket, and
      file scope.
- [ ] The target phase exists in `state.phases` with status `planned`
      (otherwise hand back to `/roadmap propose`).
- [ ] If a spike preceded the claim, its brief path is cited in the
      plan-mode proposal (case A) so the dispatched subagent reads it.

## Decision surfaces

`AskUserQuestion` is the canonical surface for the case-A
`use-as-is/revise/replace/cancel` pick and the case-B
`approve/edit/cancel` pick. Free-text prompts are forbidden per the
project-wide approval policy.

## Output contract

Skill envelope describing the activated phase + dispatched waves and
the expected cherry-pick order. The envelope's
`body.plan_mode_approval` records the approval source
(`use-as-is`, `revise`, `replace`, `planner-approve`).
