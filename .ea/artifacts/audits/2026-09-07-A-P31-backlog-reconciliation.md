# P31 backlog reconciliation audit

## Summary

Sixty backlog rows stood open at the close of phase P31. A read-only investigation verified each against the tree at branch `feature/eawf-v0.7` rather than against wave descriptions. Fourteen rows are satisfied by code that ships on this branch and passes its own tests, so they can be closed. Four are covered by a planned P32 wave, three of them only partially. Forty-two are orphaned: no planned wave covers them and nothing in the roadmap references them.

The dominant finding is not any individual row. It is that **the backlog and the roadmap are separate structures in the same state file with no linkage between them**, so delivering what a row asks for does not close the row. P32's thirty-one waves were authored from the P31 incident rows and the epoch-2 migration specification; the backlog was never consulted. The two ledgers have never met.

## Attribution

Of the fourteen closeable rows, exactly one is a P31 achievement:

- **B120** — the Linux sandbox TMPDIR pin and the bubblewrap mask fix, delivered by P31-W16 and gated by the `linux-jail` CI job that wave added.

The other thirteen were satisfied by earlier phases and left open:

| Row | Satisfied by |
|---|---|
| B001 | `[P00-W01]` — the ubuntu matrix has been in CI since the bootstrap phase |
| B009 | `[P13-W02]`, 2026-05-11 — the commit subject names the row it closes |
| B002 | `[P30-I19-W07]` — P31-W42 added only the console-suppression flag |
| B057, B058 | P14-W04 / P14-W05 |
| B062 | `[P28-I03-W09]` |
| B071 | `[P27-I05-W33]` |
| B094 | `[P30-I26-W03]` |
| B095 | `[P29-I12-W02]` |
| B097 | P29-I12-W07 |
| B117 | 2026-07-23 |
| B124 | 2026-08-11 |
| B133 | v0.6.5 hotfix |

B009 is the clearest case: its own commit subject reads `test: end-to-end golden scenarios (B009)` and the row stayed open for four months.

## Why the debt accumulates

`eawf backlog close` requires a resolution, a resolving commit, and an audit id referencing a complete audit. That is a sound evidence chain, and it is also why rows persist: landing the fix costs one commit, while recording the closure costs an audit. The friction is structural rather than a lapse of diligence, and it will keep producing this drift until closing is cheaper or until wave close reconciles the rows it satisfies.

## Verified security finding

Backlog row **B121** is accurate and its exposure widened during this phase. `_check_command_exit_zero` builds its child environment as the full parent environment and passes it to the subprocess without calling either `validate_gate_argv` or `build_child_env`, though both already exist in the tree. The argv reaches that call from `CheckSpec.args`, shape-validated only, and the `/audit` skill constructs its `CheckSpec` from a free-form agent-supplied argument. P31 did not introduce this, but it widened the reach: the out-of-process close child added by P31-W10 routes through the same runner.

Recorded as incident INC-P31-12 and scheduled as P32-W32. Backlog row B049 duplicates it with a weaker framing and should be dropped rather than worked.

## References

| Ref | Location | What it establishes |
|---|---|---|
| R1 | `src/eawf/runtime/sandbox/env_scrub.py:68` | the pinned TMPDIR split that satisfies B120 |
| R2 | `src/eawf/runtime/sandbox/jail.py:381` | the entry-type branch for bubblewrap masks |
| R3 | `.github/workflows/ci.yaml:166` | the `linux-jail` job executing the real-bwrap tests |
| R4 | `tests/golden/surfaces/cli/test_scenarios.py:81` | the three golden scenarios that satisfy B009 |
| R5 | `src/eawf/workflow/audit_dsl/registry.py:685` | the unscrubbed child environment behind B121 |
| R6 | `src/eawf/runtime/daemon/gate_execution.py:478` | the close path that reaches that runner |

## Provenance

Compiled 2026-09-07 at the close of P31-I01 on branch `feature/eawf-v0.7`. Every closeable verdict was verified against the working tree; the attribution column was derived from `git log` on the satisfying commits. No row was proposed for closure on the strength of a wave description alone.
