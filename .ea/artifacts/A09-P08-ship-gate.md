# A09-P08 ship-gate audit

Fresh-context auditor verified P08 (Orchestration core) against the six
backlog items and the ship-gate criterion from
`.ea/local/research/survey-2026-05-10-roadmap.md` §2:

> End-to-end run: open phase → `wave plan` → `wave dispatch` → `wave land` →
> `phase close` with no human shell commands between `wave dispatch` and
> `wave land`.

## Per-criterion verdicts

| Criterion | Verdict |
|---|---|
| W01/B025 `wave dispatch` + `dispatch-batch` discoverable; renderer in `src/eawf/dispatch/renderer.py` emits required sections | pass |
| W02/B026 `Wave.blocks` populated by `plan_wave`; cycle check refuses self-deps and longer cycles; `wave graph` / `wave next-ready` wired; schema regenerated | pass |
| W03/B027 `wave land` uses cherry-pick only; conflict does NOT close wave + repair hint; `wave land-batch` exists | pass |
| W04/B028 `Wave.token_budget` / `tokens_consumed`; `wave budget set/consume/show`; claim refuses over-budget; 75% warn / 100% block | pass |
| W05/B040 `src/eawf/ci_loop/` parsers (pytest/ruff/mypy); `wave fix-ci` + `fix-ci-loop`; loop exit 4 on repeated signature | pass |
| W06/B041 `src/eawf/pr_review/` parser + policy; `wave review` discoverable; severity + verdict maps correct | pass |
| Pytest 1807 passing | pass |
| `wave --help` lists every required subcommand | pass |
| P08 commit chain prefix discipline | pass |
| No merge commits on the feature branch | pass |

## Pre-fix findings (both major, both resolved forward)

1. **Pre-commit SIM108 leak** at `src/eawf/ci_loop/parser.py:167` plus
   stale ruff-format autofixes on `cli/app.py`, `cli/commands/wave_ci.py`,
   and the W05/W06 integration tests. Resolved by the `[P08-CORE] fix:`
   commit landing alongside this audit.
2. **Absolute paths in `.ea/state.json` `worktrees[*].path`** — six
   `WT-P08-I01-W0N` records carried `/Users/...` prefixes (PII rule 15
   violation). Resolved by storing repo-relative paths in
   `src/eawf/worktree/create.py`, adding the `eawf worktree path-fix --all`
   admin verb, and running it. Consumers in `cleanup.py` / `merge_back.py`
   now combine via `repo_root / record.path` for back-compatibility.

## Aggregate verdict

After the in-phase forward fix: **pass**. The two major findings were
both addressed before the ship-gate commit; no follow-up backlog item
is required.

## Evidence

- `git log feature/EAWF-v0.2 ^main --oneline` — 18 commits, `[P08-W0N]` /
  `[P08-CORE]` prefix discipline intact.
- `uv run pytest tests/ -q` — `1807 passed`.
- `uv run pre-commit run --all-files` — clean.
- `grep -c "/Users/" .ea/state.json` — `0`.
- `uv run eawf wave --help` — lists `dispatch`, `dispatch-batch`, `graph`,
  `next-ready`, `land`, `land-batch`, `budget`, `fix-ci`, `fix-ci-loop`,
  `review`.

## Carry-over to v0.3 (not blocking P08)

None — all P08 scope items shipped with their tests and CLI surface. The
"actually drive the dispatch loop end-to-end without a human in between"
muscle is exercised by this very phase: `wave plan` ran for every wave,
`wave land` cherry-picked, `phase close` will follow.
