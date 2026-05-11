# A10-P09 ship-gate audit

Fresh-context auditor verified P09 (backlog cleanups deferred from
P08) against the five backlog items captured at the P08 close:

> 1. `project_init` handler missing `_cmd` suffix.
> 2. `_insort` duplicated across `lifecycle.py` and `wave_land.py`.
> 3. `Wave.blocks` invariant validator missing.
> 4. Windows abs-path skip in worktree `path-fix` (POSIX-only
>    `is_absolute()`).
> 5. Wave-budget `consume` rollback semantics — surface the discarded
>    delta in the envelope.

## Per-criterion verdicts

| Criterion | Verdict |
|---|---|
| W01 — `def _insort` removed from `src/eawf/cli/commands/lifecycle.py` and `src/eawf/worktree/wave_land.py`; both call sites use `bisect.insort` | pass |
| W01 — `project_init` renamed to `project_init_cmd`; docstring crossref in `install/wizard.py` updated | pass |
| W02 — `check_wave_blocks_invariant` added to `eawf.validate.invariants`; two distinct codes (`INV.GRAPH.BLOCKS_MISSING_REVERSE`, `INV.GRAPH.DEPS_MISSING_REVERSE`); dangling peers silently skipped; in `ALL_INVARIANTS` | pass |
| W03 — `_is_path_absolute_any_platform` uses both `PurePosixPath` and `PureWindowsPath`; helper consumed by `worktree path-fix` | pass |
| W04 — pre-rollback `tokens_consumed` captured before `budget_record`; error message names `pre+delta=post` and `delta of N discarded`; docstring documents rollback | pass |
| Pytest 1826 passing | pass |
| Pre-commit clean on all touched files | pass |
| Commit-chain prefix discipline: `[P09-W0N]` / `[P09-CORE]` on every commit | pass |
| No merge commits on `feature/P09-v0.1` (cherry-pick-only) | pass |

## Pre-fix findings (one minor, resolved forward)

1. **PII placeholder in W03 docstring + test fixtures** — `/$USER/...`
   appeared in `src/eawf/cli/commands/worktree.py` and
   `tests/unit/test_worktree_path_fix.py`. The pattern is on
   AGENTS.md's machine-path prohibit list regardless of `<name>` being
   real or placeholder. Resolved by `[P09-W03] fix: scrub /$USER/...
   placeholders per PII hygiene` (commit 2c444a7c) — substituted the
   project's `/foo/...` convention (mirrors `/var/log`, `C:\\foo`,
   etc).

## Aggregate verdict

After the in-phase forward fix: **pass**. Source tree is clean of the
flagged pattern; pre-existing line at
`src/eawf/cli/commands/worktree.py:393` is in the P08 baseline and out
of P09's deletion scope per AGENTS.md §deletion-rule.

## Evidence

- `git log feature/P09-v0.1 ^main --oneline` — 5 commits with
  `[P09-W0N]` prefix discipline intact.
- `uv run pytest tests/ -q` — `1826 passed`.
- `uv run pre-commit run --all-files` — clean (after detect-secrets
  baseline accepted the 4 wave-closure commit SHAs in `.ea/state.json`).
- `grep -rn "/$USER/..." src/eawf/ tests/` — `0` matches.
- `uv run eawf wave graph --iter P09-I01` — all four waves `closed`.

## Carry-over to v0.3 backlog (not blocking P09)

- Wave-close SHA normalisation: `wave close --commit SHA` accepts
  full 40-char SHAs and stores them verbatim. P08 used short 7-char
  SHAs (below the detect-secrets `HexHighEntropyString` threshold);
  P09 used full SHAs and had to extend `.secrets.baseline` by four
  entries. Future enhancement: state CLI normalises to 7-char on
  store, or `.secrets.baseline` ignores `"commit":` keys in
  `.ea/state.json`.
