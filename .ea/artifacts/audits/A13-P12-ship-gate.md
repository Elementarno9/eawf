# A13-P12 ship-gate audit

Fresh-context auditor verified P12 (backlog cluster: B029, B030, B031,
B032, B033, B034, B037, B038, B039) against the per-wave specs agreed
in the P12 plan. P12 lands on the same long-running branch as P11
(`feature/eawf-v0.2-p11-flow-ergonomics`, PR #9) so the PR title now
spans both phases.

## Per-wave verdicts

| Wave | Backlog | Verdict |
|---|---|---|
| W01 | B029 decision graph viz | pass |
| W02 | B030 memory GC + B037 tiered memory | pass |
| W03 | B031 doc-drift linter | pass |
| W04 | B032 PR body render + B038 wiki distillation | pass |
| W05 | B033 skill eval harness (golden envelopes) | pass |
| W06 | B034 sandbox / permission policy module | pass |
| W07 | B039 file-impact graph | pass |

## Per-criterion verdicts

| Criterion | Verdict |
|---|---|
| W01 — `eawf decision graph --format=text` lists every decision with arrows for `superseded_by` | pass |
| W01 — `--format=dot` emits valid `digraph decisions { ... }` block; `--format=mermaid` emits `graph TD` | pass |
| W01 — 6 unit tests in `tests/unit/test_render_decision_graph.py` cover text + dot + mermaid + empty state | pass |
| W02 — `MemoryTier` StrEnum (working/archival/retrieval) added + optional `MemorySummary.tier` field with default `WORKING` (backfill) | pass |
| W02 — `gc_memory(state, threshold_days, dry_run)` flips STALE+WORKING entries to ARCHIVAL on age threshold | pass |
| W02 — `eawf memory gc --dry-run` reports without mutation; `eawf memory tier <id>` writes through `state_transaction` | pass |
| W02 — state schema regenerated (`MemoryTier` def + tier field on `MemorySummary`) | pass |
| W03 — `eawf doc verify` runs `detect_drift` per manifest target + state-vs-doc cross-checks (closed phase audit_id, repo: URI resolution) | pass |
| W03 — `--strict` exits 4 on drift / cross-check violation; default exit 0 (informational) | pass |
| W03 — 6 unit tests (clean / drift / phase-missing-audit / unresolved-artifact / non-repo URI skip / CLI strict exit 4) | pass |
| W04 — `eawf pr render P11` emits Markdown body with `## Summary`, `## Phase deliverables` table embedding `[P11-W0N]` commit short-SHAs, `## Test plan` checklist | pass |
| W04 — `eawf wiki render` emits one H1 per closed phase in id order plus top-of-doc Decisions list; open phases (e.g. live P12) excluded so the wiki is a stable historical record | pass |
| W04 — 5 unit tests across `test_render_pr_body.py` + `test_render_wiki.py` | pass |
| W05 — `tests/eval/` cluster with conftest + 6 golden envelopes (research/prep/audit/ship/review/polish) | pass |
| W05 — `eval` pytest marker registered in `pyproject.toml`; default `addopts` includes `-m 'not eval'` so plain `pytest` skips the cluster (1955 default-run, 6 deselected) | pass |
| W05 — `uv run pytest -m eval -q` runs 6 golden cases, all pass | pass |
| W06 — `SandboxPolicy` Pydantic model (`extra='forbid'`, Literal scope_kind, allow/deny lists, granted_at) | pass |
| W06 — `State.sandbox_policies: dict[str, SandboxPolicy] \| None = None` (additive) | pass |
| W06 — `INV.REF.SANDBOX_POLICY_SCOPE_MISSING` invariant on dangling wave-scoped scope_id; profile/global free-form | pass |
| W06 — `eawf wave policy set/show` round-trips through `state_transaction`; mypy clean | pass |
| W06 — 13 unit tests (model + helper, CLI, invariant) | pass |
| W07 — `eawf impact` joins decision → phases → iters → waves → `wave.file_scopes`; `--decision=DXX` filters; `--format=dot` emits valid digraph | pass |
| W07 — 6 unit tests cover empty state, project scope, phase scope, --decision filter, text empty placeholder, dot block | pass |
| Full pytest `1955 passed, 6 deselected in 148.92s` | pass |
| Eval cluster `6 passed in 1.34s` | pass |
| Mypy `Success: no issues found in 212 source files` | pass |
| Pre-commit clean after `.ea/state.json` EOL + `.secrets.baseline` line-drift refresh; no `--no-verify` bypasses | pass |
| Commit-chain prefix discipline: every commit is `[P12-W0N]` or `[P12-CORE]`; no untagged commits | pass |

## Aggregate verdict

**pass.** Nine v0.2 backlog items land as a single coherent cluster
on the open PR #9 branch. The CLI surface grows seven new verbs
(`decision graph`, `memory gc`, `memory tier`, `doc verify`,
`pr render`, `wiki render`, `wave policy`, `impact`) each backed by a
pure render / GC / verify module under `src/eawf/{render,memory,doctor,
sandbox}/`. Schema regeneration is additive (new `MemoryTier` def + the
`MemorySummary.tier` and `State.sandbox_policies` optional fields) so
existing state.json files deserialise unchanged. The skill eval harness
opens an opt-in regression bar without inflating the default pytest
runtime (1955 default-run is six lower than the addition of W05 cases
would predict because the cluster sits behind the `eval` marker).

## Evidence

- `git log 695309d..HEAD --oneline` — seven `[P12-W0N]` commits.
- `uv run pytest -q` — `1955 passed, 6 deselected in 148.92s`.
- `uv run pytest -m eval -q` — `6 passed, 1955 deselected in 1.34s`.
- `uv run mypy src/eawf` — `Success: no issues found in 212 source
  files`.
- `uv run eawf decision graph --format=text` — 8 nodes (D01..D08), 0
  edges (no superseded chains yet).
- `uv run eawf memory gc --dry-run --threshold-days=30` — `would
  archive 0 entries` on the empty `memory_index`.
- `uv run eawf doc verify` — `drift detected (drift=33, cross_check=0)`
  on this local sandbox (the `.claude/` plugin tree was not
  installed locally so manifest entries dangle); the linter's
  exit-code surface is exercised by the `--strict` unit test rather
  than this run.
- `uv run eawf pr render P11` — Markdown body containing every
  `[P11-W0N]` commit short-SHA (`f951ea1`, `def9dd2`, `c062283`).
- `uv run eawf wiki render` — 13 H1 sections (1 project + 12 closed
  phases P00..P11; P12 omitted because it is still open at this
  audit-write time).
- `uv run eawf wave policy set P12-I01-W01 --allow=Read,Edit,Bash`
  then `eawf wave policy show P12-I01-W01` — `POL-1 wave P12-I01-W01
  allow=Read,Edit,Bash deny=-`.
- `uv run eawf impact --decision=D01` — 24 wave ids spanning P08..P12
  joined to ~30 deduped file globs.

## Out of scope (deferred to v0.3 backlog)

- Hard-refusal enforcement of sandbox policy at dispatch time (W06
  only seeds the table + envelope hint).
- Wiki publishing pipeline (HTML output, mkdocs integration) — wiki
  output stays Markdown-only in v0.2.
- Skill-eval CI gating — opt-in only via `pytest -m eval`; CI
  integration deferred.
- Memory compaction with rewrite (gc soft-archives only; hard
  compaction keeps deferring per `prune.py` policy notes).
- Decision graph mermaid label refinement for very long
  summaries — current implementation embeds the whole summary on a
  single Mermaid label; opt-in truncation can land later.

## Carry-over to v0.3 backlog (not blocking P12)

- Surface `eawf doc verify --strict` in pre-commit so PRs cannot
  merge with rendered-doc drift.
- Add `eawf wiki render` to CI as a post-merge artefact step so
  `docs/wiki.md` always tracks main.
- Per-wave token-budget cross-check inside `wave policy show`
  (today the two tables are independent).
