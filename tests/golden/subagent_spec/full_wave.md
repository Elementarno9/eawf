# Wave P27-I03-W14: Typed subagent-spec library + roles

## Wave tags

- agent_role: executor
- effort_bucket: XL
- success_criteria:
  - a SubagentSpec model and a role registry exist
  - a wave dispatch renders from a typed spec rather than an ad-hoc prompt

## Scope

src/eawf/agents/specs/**, src/eawf/dispatch/renderer.py

Scope is anchored on iter P27-I03 under scope EAWF. Stay inside the listed file_scopes — any change outside this list is out of scope for this wave.

## Dependencies

- P27-I03-W12: Model-only code-quality skills (status=closed)
- P27-I03-W99: (missing from state) (status=unknown)

## Decisions

### D12: v0.3 harness scope: Claude + Codex + OpenCode only

Goose/Aider/Cursor/Cline deferred to v0.4 to lock scope.

## Hypotheses

- H27-01: metric='render_drift_count'
    confirm: drift == 0
    reject:  drift > 0
    verdict: confirmed

## Recent audits

- A35: evaluation verdict=pending
- A30: ship-gate verdict=minor

## References

Spike briefs whose filename references this wave / iter / phase. Read these before starting work — they capture the read-only investigation that motivated the wave's success criteria.

- .ea/local/research/2026-05-23-p27-i03-subagent-spec.md

## Working tree

Branch: feature/eawf-v0.3-p27-w14
Worktree path: .ea/worktrees/p27-w14
Base commit: feature/eawf-v0.3-p27

## Workflow

1. cd into the wave's worktree (see `## Working tree` above).
2. Implement edits in dependency order: schemas → logic → CLI → tests.
3. Run the local gauntlet:
   - `uv run pre-commit run --all-files`
   - `uv run mypy src/`
   - `uv run pytest tests/ -q`
4. Commit with prefix `[P27-W14] <type>: <summary>` (3-6 bullet body) and the
   recognized Claude or Codex `Co-Authored-By` trailer.
5. Close the wave through the CLI with the final token tally:
   - `uv run eawf wave close P27-I03-W14 --outcome "<summary>" --tokens-consumed <tokens>`

## Out of scope

- Do **not** push the branch.
- Do **not** open a PR.
- Do **not** edit `.ea/state.json` or `.ea/store/event.jsonl` directly — every mutation goes through `uv run eawf state ...`.
- Never `git commit --no-verify`; root-cause the hook instead.

## Estimate

- bucket: XL
- expected_eu: 8.0
- expected_minutes: 240.0
- token_budget: 32768
- parallel_siblings: P27-I03-W13, P27-I03-W15
