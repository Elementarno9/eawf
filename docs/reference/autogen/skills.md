# eawf skill catalog

Auto-generated from `eawf.render.skills:SKILL_REGISTRY`. Each row is
an Eä skill the runtime can install as a slash command.

| Skill | User-invocable | Argument hint | Description |
|---|---|---|---|
| `/agent-dispatch` | yes | `<wave-id> [--runtime=<id>]` | Dispatch a claimed wave to a runtime per the V8 session-reuse ladder. |
| `/audit` | yes | `<phase-id\|wave-id\|commit-range>` | Fresh-context verification of a phase deliverable or wave outcome. Spawns a fresh auditor subagent that re-reads the diff against the success criteria. |
| `/blitz` | yes | `[--residual-unknowns=<n>]` | Auto-chained research follow-up skill with recursion guard for residual unknowns. |
| `/coauthor` | yes | `[--mode=runtime\|project\|disabled]` | Resolve the Co-Authored-By trailer policy for the active repo. |
| `/compress` | yes | `[--tokens-before=<n>] [--tokens-after=<n>] [--runtime=<id>]` | Compress the session conversation when context approaches the limit. |
| `/differentiate` | yes | `<candidate-id>` | Recommend the cheapest experiment that discriminates between two or more candidate paths. |
| `/flow` | yes | `<task-slug> [--auto-accept=<stage>[,<stage>...]]` | Run /research → /prep → /audit → /polish → /ship sequentially; review folds into /ship as the PR-review pass. Short-circuit on any non-ok status. |
| `/init` | yes | `[--profile=<id>]` | Initialise a new Eä Workflow workspace. Renders managed regions of AGENTS.md and the .claude/ plugin tree. |
| `/memory` | yes | `save\|list\|forget [<name>] [--tier=working\|archival\|retrieval]` | Save, list, or forget curated durable memory entries. |
| `/polish` | yes | `[--scope=<dir\|file>]` | Repo-wide consistency sweep. Aligns naming, docstring style, log fields, error message phrasing, and removes dead code. |
| `/prep` | yes | `<phase-id>` | Activate the next PLANNED phase: surface its DAG for operator approval, then run the activate_phase hard gate and dispatch subagents per wave. |
| `/research` | yes | `<topic-slug> [--final]` | Read-only investigation of an open question. Produces a research brief or surfaces findings inline; no code changes, no state mutations. |
| `/review` | yes | `[<PR# \| commit-range>]` | Code review of an open PR or local diff. Surfaces issues with severity tags; no scope creep, no praise. |
| `/roadmap` | yes | `propose\|revise\|apply\|drop\|show <phase-id> [flags]` | Plan / revise / apply / drop / show PLANNED-scope phases on the eawf roadmap queue. Mutates state.json via the lifecycle transitions; one phase at a time. |
| `/security-review` | yes | `--spec=<path> [--cwd=<dir>]` | Run the security-audit DSL against a closed scope and emit findings. |
| `/ship` | yes | `<phase-id> [--dry-run]` | Close out a phase by running the full local CI surface, opening the phase PR, and (after merge) advancing state. |
| `/wave-spec` | yes | `init\|validate <wave-id> [--mockup-waiver-reason=<text>]` | Scaffold or validate a WaveSpec deliverable for a claimed wave. |
