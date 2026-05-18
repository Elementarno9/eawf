# C04a — Workflow commands — Eä framework long-term specs

**Cluster:** C04a (Workflow commands — /research, /roadmap, /prep, /flow, /audit, /ship)
**Title:** Workflow commands
**Status:** `accepted` (split per Q19 / G9; 2026-05-18)
**Depends on:** C00 (V1..V9), C01 (foundations), C02 (daemon), C03 (spec infrastructure), C04 (parent index)
**Consumed by:** C05 (CLI verb surface), C06 (TUI palette), C10 (operations docs)

## 1. Purpose + scope statement

C04a is the **workflow-commands sub-cluster** spawned from C04 per the audit split (Q19 / XB16 / G9). Canonical content for `/research`, `/roadmap`, `/prep`, `/flow`, `/audit`, `/ship` lives in `2026-05-16-c04-workflow-skills.md` §5 — this brief is the typed contract index that downstream clusters (C05, C06, C10) reference.

**In scope.**

- `/research` — 8-step research pipeline; outputs `.ea/local/research/<date>-<slug>.md`; consumes prior research; emits `research_started`/`research_completed` events.
- `/roadmap propose|revise|apply|drop` — phase proposal flow; status=needs_user envelopes; AskUserQuestion-driven ratification per AGENTS rule 21.
- `/prep` — phase activation; V11 hard gate (renamed `[P20-DIR-V11]` cite-token per Q7 / XB17); dispatches waves per DAG.
- `/flow` — 8-step pipeline (revised from 6 per audit CROSS.F39); per-step checkpoints to `flow.jsonl`; bumped to `schema_version: "1.0"` (was `1`); `--resume` derived from state transitions only (per Codex C04-I007).
- `/audit` — typed audit document with `verify-implements` kind; emits envelopes per C03 §5.6.
- `/ship` — PR open + merge + close iter/phase; reads `pr_merge_method` from layered config (C08 D16); cherry-pick captures parent branch (per XB21).

**Out of scope (covered in sibling sub-clusters).**

- Skill manifest schema + the 6 missing skills → C04b.
- Agent entity + AgentReport binding → C04c.
- Runtime adapter cross-refs → C04d.

## 2. Goals + non-goals

Inherits Goals G1..G5 from C00. Workflow-specific:

- Every workflow command has a fully typed envelope contract (input args, output envelope kind, state mutations, escalation paths).
- `/flow --resume` is deterministic from state transitions; no flow.jsonl read required for resume correctness.
- `/roadmap propose` happy path resolved per Codex C04-I005: propose can create phase+iter; apply requires non-empty waves.
- `/prep` idempotency: re-running on an already-active phase returns `status=ok` with `data.no_op=true` (Codex C04-I008).

## 3. Prior verdicts cited

V1..V9 from C00 (load-bearing). [P20-DIR-V11] cite-token resolves to P20 direction brief V11 (per Q7 / XB17 / D-03).

## 4. Decision matrix

Sub-cluster inherits all C04 decisions plus:

| # | Axis | Recommendation | Rationale |
|---|---|---|---|
| **D-a1** | `/flow --resume` driver | **State transitions only** (per Codex C04-I007 fix 2026-05-18) | flow.jsonl is operator-readable log, not the resume contract. State transitions are canonical. |
| **D-a2** | `/prep` active-phase semantics | **Idempotent — no-op with `data.no_op=true`** (per Codex C04-I008) | Re-prep should not re-dispatch; operator's expectation. |
| **D-a3** | `/roadmap propose` happy path | **Allow planned-incomplete phase (no waves yet)** (per Codex C04-I005) | Apply requires non-empty waves; propose may create empty phase+iter for editing. |
| **D-a4** | `/roadmap reorder` | **Deferred to v0.5+** (Codex C04-I011 + plan §"Bulk propose") | Operator workaround: drop + re-propose. |
| **D-a5** | `/differentiate` semantics | **Rename pending — flagged for blitz** (Codex C04-I009) | Current semantic drift; resolve before P30+ work. |

## 5. Body

Canonical bodies live in `2026-05-16-c04-workflow-skills.md` §5.4 — this brief references rather than duplicates.

## 6. Failure modes

See parent C04 §6. Sub-cluster adds:

- `F-a01` `/flow --resume` against stale flow.jsonl: ignored — state transitions are canonical.
- `F-a02` `/prep` on closed phase: refuse with envelope `status=blocked`, `repair_commands=["eawf phase reopen <phase>"]`.

## 7. Migration plan

Existing `/flow` 6-step → 8-step bump: flow.jsonl `schema_version: 1` → `"1.0"` (string MAJOR.MINOR per Q5/BOT-03); migrator at `src/eawf/migrations/flow_v1_to_v1_0.py` (no-op transform for legacy rows).

## 8. Open questions

Carried forward to v0.5+:
- Q-a1 — `/differentiate` rename target (Codex C04-I009).
- Q-a2 — `/flow` step count vs schema_version bump cadence (every workflow step add bumps the schema?).

## 9. References

[1] `2026-05-16-c04-workflow-skills.md` — parent C04 brief (canonical content).
[2] `2026-05-17-spec-series-combined-audit.md` — Stage-0 audit consumed.
[3] `.ea/local/research/long-term/2026-05-18-c12-implementation-rollup.md` — EU envelope.
[4] AGENTS.md rule 21 — roadmap procedure.

## 10. Provenance + Scrub

### Provenance

- `store_record=none (local-only research brief)`
- `commit=3b86f7a (parent)`
- `cluster=C04a (split from C04 per Q19 2026-05-18)`
- `consumes=C00..C03, C04 (parent index)`
- `supersedes=none`
- `session=eawf-spec-c04a-workflow-2026-05-18`
- `last_revised=2026-05-18 (sub-cluster spawned from C04 audit split)`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md`
- `authority_binding=Q1 (2026-05-18): daemon = sole writer for state mutations triggered by workflow commands.`

### Scrub

- status: clean
- references: repo-relative only
- local paths: none
- real emails: none
- abstract placeholder names: not applicable
