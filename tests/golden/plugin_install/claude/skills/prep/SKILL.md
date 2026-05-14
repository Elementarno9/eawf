---
name: prep
description: Open the next phase or wave by writing a work plan and dispatching subagents for execution.
argument-hint: "<phase-id> [wave-id] [--auto-plan]"
user-invocable: true
disable-model-invocation: true
---

# /prep

## Canonical algorithm

P19-W07 turns `/prep` into an activator. The flow now branches on the
phase's PLANNED-queue state:

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
     planner returns either a sequence of `eawf roadmap revise
     --add-wave` commands or a YAML payload. Surface `AskUserQuestion`
     with `approve`, `edit`, `cancel`. On `approve`, apply the
     planner's commands through the state CLI, then
     `eawf phase activate <id>`.
   - **Case C — no PLANNED phase by that id.** Reject with exit 4
     and hint `Run \`eawf roadmap propose --phase <id> --title ...\`
     first.` for the operator.

3. For each parallel wave under the activated iter, dispatch a
   worktree subagent.
4. For each sequential wave, run inline; cherry-pick parallel-wave
   commits in between as they finish.
5. Validate the rendered plan with `eawf plan show --md`; wave tags
   and bucket roll-ups must match state.

## Pre-flight checklist

- [ ] Confirm current branch is the long-running phase branch.
- [ ] Confirm `git status` is clean.
- [ ] Confirm worktree subagents branch from the parent HEAD.
- [ ] Every wave has success criteria, agent role, effort bucket, and
      file scope.
- [ ] The target phase exists in `state.phases` with status `planned`
      (otherwise hand back to `/roadmap propose`).

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
