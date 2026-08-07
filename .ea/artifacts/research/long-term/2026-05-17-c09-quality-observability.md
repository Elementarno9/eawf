# C09 — Quality + Observability — Eä framework long-term specs

**Cluster ID:** C09

**Title:** Quality + Observability

**Status:** `accepted` (ratified 2026-05-17 via AskUserQuestion: status-flip + R1 PRICING snapshot embed + R2 Windows ±15% provisional)

**Created:** `2026-05-17`

**Author:** `claude-opus-4-7`

**Depends on:** C01, C02, C03, C04, C05, C06, C07a, C07b, C08

**Consumed by:** C10

## 1. Purpose + scope statement

Locks the **quality + observability spine** that every eawf-managed repo inherits: the test taxonomy and coverage gates, the pre-commit hook inventory, the CI pipeline shape (with per-OS matrix per V6), the perf bench harness, the snapshot-fixture surface, and the observability + telemetry subsystem (V7 vendor-and-rebuild of the upstream telemetry prototype, structured log spec, correlation-ID tracing across daemon RPC + skill chain + agent dispatch, metrics catalog, Prometheus textfile export, opt-in default).

This cluster makes **eight C00-locked verdicts implementable** in concrete schemas, CLI verbs, hook configs, and CI workflows:

- **V1** [C00:25-54] — daemon emits metrics + logs centrally; `eawf metrics` reads the daemon-projected DuckDB rather than re-walking `event.jsonl` per call.
- **V2** [C00:56-75] — snapshot tests cover every spec render path so spec edits regress visibly.
- **V3** [C00:77-97] — profile-specific test inclusion (research-profile tests skip the engineering-profile coverage gate; engineering-profile tests skip the spike-profile snapshot churn).
- **V5** [C00:128-152] — `runtime_switched` event sub-types extend the audit replay; switchover frequency is a first-class metric.
- **V6** [C00:154-183] — per-OS CI matrix (Linux + macOS + Windows runners) with portalocker `fcntl` vs `LockFileEx` parity verified on each runner.
- **V7** [C00:185-225] — vendor the telemetry-prototype schema [C09-prework:1] into `src/eawf/telemetry/`, back the user-scope projection with DuckDB (SQLite fallback gated by measurement), opt-in by default.
- **V8** [C00:227-272] — session-reuse metrics (per-`(wave_id, attempt_id)` token + cache + turn counts) and context-window pressure tracking (turns-to-compaction histogram).
- **V9** [C00:274-315] — per-runtime plugin sync drift surfaced via `eawf plugin doctor`; CI gate fails on drift.

Out of scope per C00 [818-836]: hosted monitoring (v0.5+), distributed tracing (overkill for a local-only daemon). HTML report (the prototype's `report` verb) is **not** vendored in v0.3-v0.5 — TUI `/metrics` overlay [C06:884-953] covers the operator-facing view; HTML revisited in v0.5+ if demand emerges.

## 2. Goals + non-goals

### Goals

- **G1** Single source of truth for the test taxonomy (`tests/<kind>/`), with the marker → directory → CI-stage triangle locked in `pyproject.toml [tool.pytest.ini_options]`.
- **G2** Per-layer coverage targets matched to the layer's failure cost: TUI = snapshot 100% (regression bites operators visibly); library line ≥85% (regression bites silently); CLI line ≥80% + integration coverage of every verb; skill engine line ≥90% (dispatch is the trust spine); telemetry projection line ≥80% (projection bugs corrupt history). Per-package gates, not per-file (per-file gates churn on every refactor).
- **G3** Every pre-commit hook is enumerated, owned, and self-repairable: hook → what catches → repair command. New hooks land via this cluster's table only.
- **G4** CI pipeline is a DAG of named stages (lint → type → unit → integration → snapshot → property → perf), runs per-OS matrix (Ubuntu + macOS + Windows), respects `pytest -n auto` for in-process parallelism, gates on test exit + coverage threshold.
- **G5** Perf bench harness as a first-class CLI verb (`eawf bench`), fixture-seeded (small/medium/large `state.json`), regression-detected against the prior recorded baseline at `.ea/bench/baseline.json`.
- **G6** Snapshot fixtures (state.json, envelope, dispatch render, TUI screen, plugin-install delta, plan-view render, agent-report golden) live under `tests/golden/<surface>/` with a single `eawf snapshot update` verb to refresh them.
- **G7** Structured log spec is enforceable: every `logger.info` / `logger.error` follows the AGENTS naming convention `<funcname> key=value key=value`; the doc-verify hook lints log lines that diverge. Sensitive-data scrubbing happens at emit, not at sink.
- **G8** Correlation IDs flow end-to-end: `request_id` (daemon RPC), `wave_id` (skill dispatch), `attempt_id` (per-runtime invocation) — all three present on every event written by a dispatched subagent, all three searchable via `grep`.
- **G9** Telemetry subsystem (V7) vendored, retires the prototype. Schema versioned. Opt-in by default; no implicit phone-home. Local-only DB unless `telemetry.export.endpoint` set.
- **G10** Incident-cause taxonomy promotes `Incident.root_cause` from free string to closed enum so audit-replay produces typed rows the projection can group.
- **G11** Event-kind extensions for V5 (`runtime_switched`, `session_continued`, `session_failover`) are typed `EventPayload` subclasses, persisted on `event.jsonl`, projected into DuckDB rows by the V7 pipeline.

### Non-goals

- **NG1** Hosted SaaS monitoring (Datadog / Honeycomb / Grafana Cloud push) — deferred to v0.5+.
- **NG2** Distributed tracing (multi-host span propagation) — local-only daemon means span-emit is sufficient; OTel exporter design ships in C10 packaging if demanded post-v0.5.
- **NG3** Auto-fix coverage drops via test generation — operator owns test writing; CI surfaces the drop.
- **NG4** HTML report generator vendored from the telemetry prototype — TUI overlay covers the surface.
- **NG5** Per-file coverage gates — too noisy; per-package only.
- **NG6** Anomaly detection layered into V7 in v0.3 — Axis-D rolling z-score [5:420-424] lands in a follow-up phase once V7 collects 4+ weeks of warn-only data.
- **NG7** Replacing presidio with a custom ML scrubber — vendoring presidio is opt-in (extras-gated dep); regex-only is the default scrubber.
- **NG8** Re-implementing the telemetry prototype's hook-proposer verb, which read denial patterns out of session logs and suggested pre-commit hooks — orthogonal to V7's telemetry goal; revisit in C10 onboarding if denial patterns warrant.

## 3. Prior verdicts cited

### From C00 spec index [1]

- **V1 daemon Day-1** [C00:25-54]. Daemon hosts the metrics projection thread + structured-log sink; CLI mutations re-emit through daemon so logs + metrics share one event-source path.
- **V2 three-tier specs** [C00:56-75]. Snapshot tests cover every PhaseSpec / IterSpec / WaveSpec render to detect drift.
- **V3 composable profiles** [C00:77-97]. CI test-selection respects the active profile bundle — research-profile repos run a different subset than engineering-profile repos.
- **V5 reactive runtime switchover** [C00:128-152]. Switchover emits `runtime_switched` event; switchover frequency is a metric.
- **V6 per-OS daemon** [C00:154-183]. CI matrix exercises portalocker on Linux/macOS/Windows; on-demand-spawn path tested on all three.
- **V7 vendor the telemetry-prototype schema** [C00:185-225]. This cluster's load-bearing verdict. Schema attribution: see Provenance.
- **V8 hybrid session reuse** [C00:227-272]. Session-reuse metrics (turn count per `(wave, attempt)`, cache-hit ratio, context-window pressure as turns-to-compaction).
- **V9 native per-runtime plugins** [C00:274-315]. `eawf plugin doctor` drift gate runs in CI.

### From C01 [2] foundations (consumed)

- URN scheme `urn:eawf:v1:<kind>:<path>` — used by every metrics scope identifier (`eawf metrics show --scope <urn>`).
- Closed `StoreKind` enum + closed `IncidentSeverity` enum [C01-2 §entity catalog] — extended in §5 below with closed `IncidentCause` enum.
- Lifecycle DAG for `Incident` (PENDING → INVESTIGATING → MITIGATED → RESOLVED) — projected into the DuckDB `incidents` table per V7.

### From C02 [3] daemon

- Daemon RPC method catalog includes `telemetry.metrics` (read) + `telemetry.rebuild` (mutator) + `telemetry.export` (read+side-effect to file) per C02 §5.3.
- `request_id` UUID-v4 stamped on every RPC envelope per C02 §5.2 — base of the correlation-ID chain.
- WAL recovery emits `wal_recovery` event per C02 §5.6 [3:427] — projected as an incident with cause `daemon_wal_recovery`.

### From C03 [4] specs

- Snapshot test surfaces every spec render: `tests/golden/spec/<phase|iter|wave>_render/<scope-id>.txt`.
- `eawf snapshot update` reuses `eawf <spec> render` underneath.

### From C04 [5] skills

- Skill engine emits `agent_end` envelope on every terminal handoff per AGENTS rule 19 — projected into `<role>_report` rows by V7.
- Skill manifest carries `tests:` block declaring which `tests/eval/` cases gate the skill's CI status.

### From C05 [6] CLI

- `eawf metrics show | export | rebuild` is the CLI surface for V7 [6:1214] — this cluster locks the per-verb behaviour.
- `eawf bench` is a new noun-app verb owned by this cluster (added to the C05 noun-app catalog).
- `eawf snapshot update` is a new noun-app verb owned by this cluster.

### From C06 [7] TUI

- `/metrics` palette verb → `MetricsModal` 3×2 tile grid [C06:884-953]. The six tiles consume the V7 projection: variance, weekly burn, wave elapsed, cache health, switchover freq, per-runtime tokens.
- Daemon-push protocol carries `metrics_changed` envelopes when projection updates [C06 §5.8] — tile refresh is reactive, not polled.

### From C07a [9] runtime + skill dispatch

- Per-runtime session-handle catalog [9:228-254] — V7 projection reads from each runtime's session log via the adapter's `iter_session_rows(session_handle)` method.
- Error-class normalization [9:256-291] — five canonical classes feed `Incident.cause` mapping.
- Cache-control mis-layer alarm [9:293-308] — `cache_mislayer_alarm` event-sub-type projected into the cache-health tile.

### From C07b [10] events + worktree

- `EventPayload` shape [10:399-412]; closed `StoreKind` enum [10:417-451]; new event sub-types `runtime_switched`, `session_continued`, `session_failover`, `cache_mislayer_alarm`, `dispatch_cost`, `wal_recovery` [10:436-451] — C09 owns the typed Pydantic shape for each.
- Forever-retention of `event.jsonl` for v0.3-v0.5 [10:457-461] — V7 projection is **rebuildable** from this; DuckDB is a cache, not the source of truth.

### From C08 [8] config + profiles

- `telemetry.*` config keys [8:252-261] — defaults: `telemetry.enabled=false`, `telemetry.db_kind="sqlite"` (per [B01 blitz r1] [28], overrides the C08 placeholder default of `"duckdb"`; C08 doc update gated on C09 ratification), `telemetry.export.format="prom"`, `telemetry.window_default="7d"`. C09 also introduces `telemetry.cache_mislayer.{ratio_threshold,creation_floor_tokens,window_seconds}` (defaults `10.0 / 2000 / 300` per [B03 blitz r3] [30]).
- `dispatch.session_handle_ttl_seconds=86400` [8:267-268] — C09 honours this when pruning session-handle rows from the projection.
- Profile manifest `tests:` field — declares which markers gate the profile's CI status.

## 4. Decision matrix

| # | Axis | Options | Recommendation | Rationale |
|---|---|---|---|---|
| D1 | Test directory structure | (a) flat `tests/test_*.py`; (b) per-kind subdirs `tests/{unit,integration,golden,property,eval,perf}/`; (c) per-package mirror | **(b)** | Current layout already follows (b); pytest markers + directory align; per-package mirror needs ~50 thin `__init__.py` files for no gain. |
| D2 | Coverage gate scope | (a) per-file; (b) per-package; (c) overall only | **(b) per-package** + **(c) overall floor** | Per-file gates churn on refactors; overall-only gate hides regressions in cold paths. Per-package + overall floor (60%) balances signal vs noise. |
| D3 | Pre-commit hook strict-mode for mypy + pytest | (a) `stages: [manual]` (current); (b) `stages: [pre-commit]` (run on every commit); (c) `stages: [pre-push]` (current for mypy-pre-push) | **(c) for mypy, keep manual for pytest** | Pytest at pre-commit makes commits slow; pre-push catches before others see it. Mypy strict already on pre-push. |
| D4 | Bench fixture seed sizes | (a) one small fixture; (b) small + medium; (c) small + medium + large + jumbo | **(c) but jumbo opt-in via marker** | small (~10 waves) reveals per-call cost; medium (~50) reveals collection cost; large (~200) reveals projection cost; jumbo (~2000) is the fuzz-target for next-gen scale. |
| D5 | Trace correlation grain | (a) `request_id` only; (b) `+ wave_id`; (c) `+ wave_id + attempt_id` | **(c)** | (a) loses the dispatch graph; (b) loses retry detail; (c) makes `grep wave=W17 attempt=2` trivial and matches V8 session-reuse metrics. |
| D6 | Metrics tile inventory | (a) Six fixed tiles per C06; (b) Six default + plugin-contributed extra tiles; (c) Operator-configurable per `telemetry.tiles` | **(a) for v0.3-v0.5; (b) flagged for v0.5+** | C06 already specs six; (b)/(c) are scope creep without a plugin manifest API for tiles (not yet specced). |
| D7 | Telemetry DB | (a) DuckDB; (b) SQLite; (c) both via abstract store | **(c) abstract store; default SQLite per [B01 blitz r1]** | Measurement in `[28]` against operator's real `event.jsonl` (694 rows, 340 KB) shows SQLite wins 5-189× across every op + 0 MB install (DuckDB native lib = 38.1 MB). DuckDB break-even is at ~100K rows; eawf retention math projects ~240 rows/year/repo → ~400 years to break-even. DuckDB stays as opt-in via `telemetry.db_kind=duckdb` for power users on >10K rows. |
| D8 | Telemetry-prototype audit access path | (a) Operator grants read access to the prototype source; (b) operator drops a tarball under `.ea/local/research/long-term/telemetry-prototype-snapshot.tar.gz`; (c) operator opens a vendor branch in eawf | **(a) confirmed** (audit memo [22] succeeded) | (a) worked — the operator granted read access to the prototype source directly; no tarball needed. Audit memo pins the audited source revision for reproducibility. |
| D9 | Telemetry export priority | (a) Prometheus textfile only; (b) Prom + OTLP/JSON + CSV; (c) Prom + CSV (skip OTLP until OTel gen-ai SC stabilises) | **(c)** | Prom textfile is the v0.3 default per V7; CSV opens spreadsheet workflows; OTLP gen-ai SC still "Development" status [5:621] — defer to v0.5+. |
| D10 | Per-OS CI runner budget | (a) Linux only; (b) **Linux + macOS + Windows on every push**; (c) Linux + Windows on every push + macOS merge-only | **(b) Matrix B — macOS every PR (revised 2026-05-18 per Q17)** | ~~Matrix D (macOS merge-only) deferred macOS drift detection to post-merge; raises risk of platform-specific bugs landing.~~ **Per operator Q17 (2026-05-18): macOS every PR.** Higher runner cost (~2-4× Linux per-min) but catches platform drift early. Workflow template ships configurable for private downstreams that need the budget-conscious Matrix D — `EAWF_CI_MACOS_GATE=merge-only` env override surfaces the alternate matrix without forking the workflow. |
| D11 | Coverage tool | (a) `pytest-cov` (current); (b) `coverage.py` standalone; (c) `slipcover` for speed | **(a)** | Already wired; speed not a bottleneck (CI bottleneck is mypy + pre-commit + integration tests, not coverage instrumentation). |
| D12 | Snapshot tool | (a) Custom golden-file comparison (current); (b) `syrupy`; (c) `pytest-snapshot` | **(a)** | Current golden machinery handles diff formatting, SVG/PNG byte-equality, ANSI stripping. Migrating to syrupy = ~200 LOC churn for no fresh capability. |
| D13 | Log sink topology | (a) per-process stderr only; (b) daemon-aggregated sink at `<local-path>`; (c) both | **(c)** | (a) loses dispatched-subagent logs; (b) only loses interactive CLI logs. (c) — CLI logs stderr, daemon-spawned subagents also write to the daemon sink. |
| D14 | Sensitive-data scrub layer | (a) regex-only at emit; (b) regex-only + presidio (opt-in extras dep); (c) presidio mandatory | **(b)** | presidio model dep ~200 MB; mandatory bloats every install. Regex covers AGENTS rule 16 patterns (paths, emails); presidio adds API keys / PATs / NER for export pre-publish. |
| D15 | Bench regression threshold | (a) ±10% uniform; (b) per-OS thresholds; (c) ±5% strict | **(b) per-OS** per [B07 blitz r7] [34] | Measured run-to-run relvar: Linux 6-7% (±10% safe); macOS 13-16% (±10% flakes >30%; ±20% safe within 2σ); Windows unmeasured (±15% provisional). Cross-OS spread 86% (single-baseline shared comparisons broken). Implementation: `EAWF_BENCH_THRESHOLD` env override per lane; defaults from `.ea/bench/thresholds.yaml`. |
| D16 | Hook strict mode for `eawf doc verify` | (a) `--strict` always; (b) `--strict` only on `stages: [manual]` (current); (c) `--strict` on `stages: [pre-push]` | **(c)** | (a) blocks every commit on doc nits; (b) hides drift until operator runs by hand; (c) catches before push without per-commit friction. |

## 5. Proposed schema / API / protocol

### 5.1 Test taxonomy

```
tests/
├── unit/               # marker: unit          fast, in-process, no I/O
│                                              one module under test per file
├── integration/        # marker: integration   crosses module boundaries
│                                              may touch tmp filesystem
├── property/           # marker: property      Hypothesis-driven
├── golden/             # marker: golden        cli/, envelope/, plan_view/,
│                                              tui/, agent_report/, agents_md/,
│                                              audit_dsl/, plugin_install/,
│                                              scenarios/, spec/, telemetry/
├── eval/               # marker: eval          skill-dispatch golden envelopes
│                                              (opt-in; -m 'not eval' by default)
├── perf/               # marker: perf          eawf bench harness fixtures
│                                              (opt-in; runs via `eawf bench`)
├── snapshot/           # marker: snapshot      TUI screen + render snapshots
│                                              (golden subset; lives in golden/ today,
│                                              promoted to its own dir in v0.4)
├── fixtures/           # synthetic state trees, plugin trees, JSONL fixtures
└── smoke/              # marker: smoke         post-install single-import sanity
                                                run as the final CI stage on
                                                the built wheel, not the source tree
```

**Marker → directory contract.** A test under `tests/<kind>/` MUST carry the matching `pytest.mark.<kind>` marker. CI selects via `-m`; default `pyproject.toml` already deselects `eval`. New markers extending the catalog ship with their `tests/<kind>/` directory and a `pyproject.toml` `[tool.pytest.ini_options].markers` row.

**Existing inventory** (audited 2026-05-17): `tests/unit/` 165 files; `tests/integration/` 42 files; `tests/golden/` 12 subdirs; `tests/property/` 13 files; `tests/eval/` 1 file. **Net-new in C09 implementation phase:** `tests/perf/` (created by `eawf bench` harness); `tests/golden/spec/`, `tests/golden/telemetry/`, `tests/golden/metrics_export/`; `tests/smoke/` (one file, post-install).

### 5.2 Coverage gates

| Layer | Path | Line | Branch | Snapshot | CI gate enforcement |
|---|---|---:|---:|---:|---|
| Daemon | `src/eawf/daemon/` | ≥85% | ≥70% | n/a | `pytest --cov=eawf.daemon --cov-fail-under=85` per-package |
| Telemetry projection | `src/eawf/telemetry/` | ≥80% | ≥60% | n/a | per-package gate |
| Skill engine | `src/eawf/skills/` | ≥90% | ≥75% | n/a | per-package gate; AGENTS rule 19 |
| CLI | `src/eawf/cli/` | ≥80% | ≥60% | n/a | per-package gate + integration coverage of every verb (one row in `tests/golden/cli/verb_inventory.golden.txt`) |
| TUI | `src/eawf/tui_v2/` | (line excluded from gate) | n/a | 100% screens covered | snapshot diff; coverage line-target waived because Textual widgets render asynchronously and line-cov misreports |
| Render / envelope | `src/eawf/render/`, `src/eawf/store/` | ≥85% | ≥70% | per-envelope golden | per-package gate + golden diff |
| State writer + lock | `src/eawf/state/`, `src/eawf/lock/` | ≥90% | ≥80% | n/a | per-package gate (this is the crash-safety surface) |
| Plugin install | `src/eawf/runtimes/*/plugin_install.py` | ≥80% | ≥60% | full plugin-tree golden | per-package gate + golden diff |
| **Overall floor** | `src/eawf/` | ≥60% | ≥50% | n/a | `pytest --cov=eawf --cov-fail-under=60` after package-gates pass |

**Why per-package, not per-file.** Per-file gates churn on refactors (renaming `_helper` modules trips the gate for no functional reason). Per-package gates align with the architectural boundary the operator already maintains.

**Why overall floor stays low (60%).** TUI-line-cov exclusion drops the achievable overall by ~15 points. Per-package gates already enforce the layer where coverage matters; overall floor catches dead-code accumulation only.

**Snapshot promotion.** `eawf snapshot update --kind <surface>` regenerates goldens; review-required CI gate fails on any snapshot mutation that isn't accompanied by a `[Pxx-Wyy] test: snapshot update` commit.

### 5.3 Pre-commit hook inventory

Locked inventory. Each row: hook id, stage, what catches, repair command.

| # | Hook | Stage | What catches | Repair |
|---:|---|---|---|---|
| 1 | `ruff` | pre-commit | Lint errors per `pyproject.toml [tool.ruff.lint] select=["E","F","I","N","UP","B","C4","SIM","RUF"]` | `uv run ruff check --fix .` |
| 2 | `ruff-format` | pre-commit | Format drift per `[tool.ruff.format]` | `uv run ruff format .` |
| 3 | `trailing-whitespace` | pre-commit | Trailing whitespace | hook auto-fixes; re-stage |
| 4 | `end-of-file-fixer` | pre-commit | Missing trailing newline | hook auto-fixes; re-stage |
| 5 | `check-yaml` | pre-commit | YAML parse errors | fix YAML; re-stage |
| 6 | `check-toml` | pre-commit | TOML parse errors | fix TOML; re-stage |
| 7 | `check-added-large-files` (`--maxkb=1024`) | pre-commit | Files >1 MiB staged | move binary to `tests/fixtures/` (`.gitattributes git-lfs`) or scrub |
| 8 | `check-merge-conflict` | pre-commit | `<<<<<<<` markers | resolve conflict; re-stage |
| 9 | `debug-statements` | pre-commit | `breakpoint()`, `pdb.set_trace()` | remove the line; re-stage |
| 10 | `detect-secrets` (with `.secrets.baseline`) | pre-commit | New secret-shaped strings not in baseline | review the hit; if false-positive, `uv run detect-secrets scan > .secrets.baseline` + bump the comment hash; if real, scrub the value |
| 11 | `insert-coauthor` | prepare-commit-msg | Missing `Co-Authored-By:` trailer | hook inserts canonical trailer; review |
| 12 | `normalize-coauthor` | commit-msg | Trailer format drift | hook normalises; re-attempt commit |
| 13 | `commit-prefix-lint` | commit-msg | Missing `[P##-W##]` / `[P##-CORE]` prefix or unknown type | re-write commit message per AGENTS rule 14 |
| 14 | `mypy-pre-push` | pre-push | mypy strict errors over `src/` | `uv run mypy src/` — add types, fix errors |
| 15 | `eawf doc verify --strict` | pre-push *(promoted from manual per D16)* | Doc drift between AGENTS.md, schema docs, CLI ref | run `uv run eawf doc verify` — investigate diff; update docs |
| 16 | `path-leak-lint` *(NEW; C09)* | pre-commit | a macOS, Linux or Windows user-home path root, or a `<local-path>` placeholder, in any staged blob | scrub the literal; re-stage |
| 17 | `email-leak-lint` *(NEW; C09)* | pre-commit | Email addresses not in the canonical `pyproject.toml authors` allowlist | scrub or add to allowlist (only canonical author rows allowed) |
| 18 | `log-format-lint` *(NEW; C09)* | pre-commit | `logger.{info,warning,error}` call sites not matching the `<funcname> key=value` regex per AGENTS naming conventions | re-write log line to the canonical shape. Implementation: `ruff check --select EAWF001` custom rule reusing ruff's AST per [B11 blitz r11] [38] (avoids double-parse; halves cost) |
| 19 | `plugin-doctor-drift` *(NEW; C09 via V9)* | pre-push | `eawf plugin doctor --strict` reports drift in `build/<runtime>-plugin/` | `eawf plugin sync`. Conditional skip per [B11 blitz r11] [38]: bail fast if `git diff origin/main...HEAD --name-only` shows no AGENTS.md / skill / plugin changes (drops ~5s → ~50ms on no-relevant-change pushes) |
| 20 | `pricing-currency-check` *(NEW; C09 via [B12 blitz r12] [37])* | weekly CI cron (not pre-commit/pre-push) | Vendored `PRICING` dict drifts from canonical Anthropic published rates | Auto-PR with refreshed values + bumped `pricing_version`; manual trigger via `workflow_dispatch` or `eawf telemetry pricing-currency-check --strict` |

**New hooks 16–19** are project-local under `.pre-commit-config.yaml repos: [- repo: local hooks: [...]]`; each is `entry: uv run eawf hook <name>` and the actual implementation lives in `src/eawf/cli/commands/hook.py` per AGENTS rule 1 (CLI dispatch; library implements).

**Hook ownership matrix** stays in this brief; AGENTS.md gets a back-reference to C09 §5.3 after acceptance.

### 5.4 CI pipeline DAG

```
                                 ┌────────────────┐
                                 │  checkout (v4) │
                                 │  fetch-depth=0 │
                                 └───────┬────────┘
                                         │
                                 ┌───────▼────────┐
                                 │ setup-uv (v8)  │
                                 │ enable-cache   │
                                 └───────┬────────┘
                                         │
                                 ┌───────▼────────┐
                                 │ uv sync --frozen │
                                 └───────┬────────┘
                                         │
            ┌────────────────────────────┼────────────────────────────┐
            │                            │                            │
   ┌────────▼────────┐         ┌─────────▼─────────┐         ┌────────▼────────┐
   │ stage: lint     │         │ stage: type       │         │ stage: secrets  │
   │ pre-commit      │         │ mypy --strict     │         │ detect-secrets  │
   │ run --all-files │         │ src/              │         │ --baseline      │
   └────────┬────────┘         └─────────┬─────────┘         └────────┬────────┘
            │                            │                            │
            └────────────────────────────┴────────────────────────────┘
                                         │
                                 ┌───────▼────────┐
                                 │ stage: unit    │
                                 │ -m unit        │
                                 │ -n auto        │
                                 └───────┬────────┘
                                         │
                       ┌─────────────────┼─────────────────┐
                       │                 │                 │
              ┌────────▼────────┐ ┌──────▼─────┐ ┌─────────▼────────┐
              │ integration     │ │ property   │ │ golden + snapshot │
              │ -m integration  │ │ -m property│ │ -m golden         │
              │ -n auto         │ │ deadline=0 │ │                   │
              └────────┬────────┘ └──────┬─────┘ └─────────┬────────┘
                       │                 │                 │
                       └─────────────────┴─────────────────┘
                                         │
                                 ┌───────▼────────┐
                                 │ stage: coverage│
                                 │ per-package    │
                                 │ + overall ≥60 │
                                 └───────┬────────┘
                                         │
                                 ┌───────▼────────┐
                                 │ stage: bench   │  conditional: PR label
                                 │ eawf bench all │  or push to main
                                 │ vs baseline    │
                                 └───────┬────────┘
                                         │
                                 ┌───────▼────────┐
                                 │ stage: smoke   │
                                 │ pip install .  │
                                 │ → import eawf  │
                                 │ → eawf --version│
                                 └────────────────┘
```

**Stage parallelism.** Lint + type + secrets run in parallel as separate jobs (same checkout). Unit then fans out to integration + property + golden as a second wave. Coverage stage gates the merge.

**Per-OS matrix** (per V6, Matrix D shape per [B02 blitz r2]):

```yaml
jobs:
  test-linux-windows:                # every push + every PR
    name: ${{ matrix.os }} / py${{ matrix.python }}
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-24.04, windows-2025]
        python: ["3.14"]

  test-macos:                        # merge-to-main only
    name: macos-15 / py${{ matrix.python }}
    runs-on: macos-15
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    strategy:
      matrix:
        python: ["3.14"]
```

Current CI [12] runs `[macos-26, macos-15, ubuntu-24.04, ubuntu-22.04]` × `py3.14`. C09 implementation phase **replaces with Matrix D**: PRs run Linux + Windows on every push (V6 portalocker parity gate enforced); merges to `main` additionally run macOS (catches macOS-specific regressions before they ship). macOS minute spend drops ~70% vs the all-push Matrix B alternative, fitting GHA Free plan (2,000 min/mo) on private downstreams without overrun. For the eawf repo itself (public on GHA → unlimited free), the matrix shape is equivalent in cost; the gating is operator-facing latency control on PRs.

**Portalocker parity gate.** `tests/integration/test_portalocker_fcntl_vs_lockfileex.py` (NEW; C09 implementation) runs on every OS in the matrix; verifies the same lock-conflict-then-retry sequence works under both `fcntl` (POSIX) and `LockFileEx` (Win32) per V6 [C00:170-177]. Gate fails on cross-OS divergence.

**Plugin-doctor drift gate.** `eawf plugin doctor --strict` runs as a separate CI job on Linux only (drift detection is OS-independent); fails the PR if `build/<runtime>-plugin/` is stale vs AGENTS.md + skill registry.

### 5.5 Perf bench harness — `eawf bench`

New noun-app verb. Lives at `src/eawf/cli/commands/bench.py` per AGENTS rule 1; library lives at `src/eawf/bench/`.

```
eawf bench list                          # show every fixture × harness
eawf bench run [--fixture <name>] [--harness <name>]
               [--baseline .ea/bench/baseline.json] [--update-baseline]
               [--format json|table|prom]
eawf bench compare --before <path> --after <path>
eawf bench fixture seed --size small|medium|large|jumbo --out <path>
eawf bench all                           # CI entry point; runs every (fixture, harness)
```

**Fixture seeds.**

| Size | Wave count | Phase count | Event count | Use case |
|---|---:|---:|---:|---|
| `small` | 10 | 1 | ~200 | Per-call cost (state-load, validate, render). |
| `medium` | 50 | 3 | ~2,000 | Collection cost (plan-view, eawf wave list). |
| `large` | 200 | 8 | ~20,000 | Projection cost (V7 rebuild, plan-view full repo). |
| `jumbo` | 2,000 | 50 | ~500,000 | Scale ceiling fuzz; **opt-in** (`--harness telemetry_rebuild_jumbo`). |

Seeds are deterministic (`hashlib.sha256("bench-fixture-v1-<size>").digest()` seeds the RNG). Stored under `tests/fixtures/bench/<size>.json` + `tests/fixtures/bench/<size>-event.jsonl`. Regenerated by `eawf bench fixture seed`.

**Harness catalog.**

| Harness | Measures | Wall-clock target (small) | Wall-clock target (medium) | Wall-clock target (large) |
|---|---|---:|---:|---:|
| `state_load_validate` | Pydantic validate `state.json` | ≤2 ms | ≤8 ms | ≤30 ms |
| `state_writer_atomic` | acquire + write + fsync + release | ≤10 ms | ≤10 ms | ≤10 ms |
| `event_append` | `append_envelope` round-trip | ≤5 ms | ≤5 ms | ≤5 ms |
| `plan_view_render` | `eawf wave list` table render | ≤30 ms | ≤120 ms | ≤500 ms |
| `dispatch_render` | one wave dispatch envelope render | ≤15 ms | ≤15 ms | ≤15 ms |
| `telemetry_rebuild` | `eawf metrics rebuild` from event.jsonl | ≤200 ms | ≤1 s | ≤8 s |
| `daemon_rpc_roundtrip` | `state get` over Unix socket | ≤8 ms | ≤8 ms | ≤8 ms |
| `tui_first_paint` | TUI mount → first frame | ≤300 ms | ≤300 ms | ≤300 ms |

**Regression detection.** `eawf bench compare` flags any harness where `after >= before * (1 + threshold)`. Threshold is per-OS per [B07 blitz r7] [34]: Linux ±10%, macOS ±20%, Windows ±15% (provisional). Defaults live at `.ea/bench/thresholds.yaml`; per-invocation override via `--threshold` or `EAWF_BENCH_THRESHOLD` env. CI uploads bench output as a workflow artifact + posts a PR comment when run on a PR (gate: PR label `bench` or push to main). **Per-OS baseline files** at `.ea/bench/baseline-<runner-name>.json` (e.g., `baseline-ubuntu-24.04.json`, `baseline-macos-15.json`, `baseline-windows-2025.json`). Single shared baseline is broken — cross-OS spread is ~86% per [34], dwarfing any sane threshold. Update via `eawf bench run --update-baseline` + a `[P##-W##] perf: update bench baseline <os>` commit per file.

### 5.6 Snapshot fixtures — surface inventory

Locked categories under `tests/golden/<kind>/`:

| Kind | Path | Updated by | Contents |
|---|---|---|---|
| State | `tests/golden/state/` | `eawf snapshot update --kind state` | Canonical state.json sample tree per scope-pattern (single phase, multi-phase, with hypotheses, with audits, with incidents). |
| Envelope | `tests/golden/envelope/` | `eawf snapshot update --kind envelope` | Output envelope per status (ok, failed, partial, blocked, needs_user) — already in repo today. |
| Dispatch render | `tests/golden/dispatch/` *(NEW)* | `eawf snapshot update --kind dispatch` | Dispatch envelope per runtime × per skill (CC × {research, prep, audit, ship, flow}, Codex × same subset, OpenCode × same). |
| Plan view | `tests/golden/plan_view/` | `eawf snapshot update --kind plan_view` | `eawf wave list` ASCII render per fixture (small/medium/large). |
| TUI | `tests/golden/tui/` | `eawf snapshot update --kind tui` | Textual screen capture (`.txt`) per screen × state. Already in repo today. SVG captures via `console.export_svg()` arrive in C06 implementation phase. |
| Spec render | `tests/golden/spec/` *(NEW)* | `eawf snapshot update --kind spec` | PhaseSpec, IterSpec, WaveSpec render per `eawf {phase,iter,wave} spec render`. |
| Agent report | `tests/golden/agent_report/` | `eawf snapshot update --kind agent_report` | Per-role typed-body envelope sample (`researcher_report.json`, `executor_report.json`, ...). Already in repo today. |
| Plugin install | `tests/golden/plugin_install/` | `eawf snapshot update --kind plugin_install` | `build/<runtime>-plugin/` full tree per runtime. |
| Audit DSL | `tests/golden/audit_dsl/` | `eawf snapshot update --kind audit_dsl` | DSL render per audit-kind. |
| Scenarios | `tests/golden/scenarios/` | `eawf snapshot update --kind scenarios` | End-to-end lifecycle goldens (fresh repo, enrich existing, full flow). |
| Telemetry | `tests/golden/telemetry/` *(NEW; C09)* | `eawf snapshot update --kind telemetry` | DuckDB schema dump (`.schema`) + projection output for a fixture event.jsonl. |
| Metrics export | `tests/golden/metrics_export/` *(NEW; C09)* | `eawf snapshot update --kind metrics_export` | `eawf metrics export --format prom\|json\|csv` output per fixture. |
| AGENTS.md | `tests/golden/agents_md/` | `eawf snapshot update --kind agents_md` | Rendered AGENTS.md after every sync. Already in repo today. |

**Snapshot update flow.** Operator runs `eawf snapshot update --kind <surface>` → fixtures regenerate → operator diffs → commits as `[P##-W##] test: snapshot update <kind>`. CI gate refuses snapshot mutations not paired with that exact prefix.

### 5.7 Observability — structured log spec

**Format.** Canonical per AGENTS naming conventions: `<funcname> key=value key=value` — bare keys, space-separated, f-string interpolated, no leading colon.

```python
# Library modules (every src/eawf/**/*.py except CLI handlers):
logger = logging.getLogger(__name__)

logger.info(f"create_worktree wave={wave_id} branch={name!r}")
logger.error(f"validate_state path={path!r} field={field} reason={reason!r}")
```

**Levels.**

| Level | When | Examples |
|---|---|---|
| `DEBUG` | Per-call detail useful for the developer | RPC payload size, cache hit/miss, retry-window evictions |
| `INFO` | Operator-visible state transitions | `phase_activate phase=P19 base=main`, `wave_claim wave=W03 by=executor` |
| `WARNING` | Recoverable degradation | `cache_creation_input_tokens >> cache_read_input_tokens; mislayer suspect` |
| `ERROR` | Operation failed; recovered or surfaced | `dispatch_failed wave=W03 runtime=claude code=RUNTIME_TIMEOUT` |
| `CRITICAL` | Data-loss risk or invariant violation | `wal_recovery_failed path=...` |

**Defaults.** Library default `WARNING`; CLI default `INFO`; `EAWF_LOG_LEVEL=<level>` env override; `--log-level` CLI flag.

**Sinks.**

- CLI process: `stderr` via the root handler.
- Daemon process: rotated file at `<local-path>` (10 MB × 5 files); JSON-formatted via `logging.handlers.RotatingFileHandler` + `pythonjsonlogger.json.JsonFormatter` (or hand-rolled JSON formatter — no new heavy dep needed).
- Daemon-spawned subagents: log to daemon sink via the daemon RPC (`telemetry.log` method) so dispatched-subagent logs surface alongside operator commands.

**Sensitive-data scrubbing at emit.** Filter chain runs **before** the formatter:

```python
# src/eawf/logging/scrub.py
class SensitiveScrubber(logging.Filter):
    """Strips path / email / API-key patterns from every log record at emit."""
    PATTERNS = (
        re.compile(r"(<local-path>)"),                  # macOS home
        re.compile(r"(C:\\Users\\[^\\\s]+)", re.I),       # Windows home
        re.compile(r"(<local-path>)"),                   # Linux home
        re.compile(r"([\w.+-]+@[\w-]+\.[\w.-]+)"),        # email
        re.compile(r"(sk-[a-zA-Z0-9_-]{20,})"),           # OpenAI-shaped key
        re.compile(r"(sk-ant-[a-zA-Z0-9_-]{20,})"),       # Anthropic-shaped key
        re.compile(r"(ghp_[a-zA-Z0-9]{36,})"),            # GitHub PAT
    )
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat in self.PATTERNS:
            msg = pat.sub("<scrubbed>", msg)
        record.msg = msg
        record.args = ()
        return True
```

**Allowlist** (regex bypass): emails matching the canonical `pyproject.toml authors` block ARE preserved. AGENTS.md `## Sensitive-info hygiene` documents the allowlist mechanism.

### 5.8 Tracing — correlation ID chain

Three IDs flow end-to-end on every event:

| ID | Stamped by | Used to | Lifecycle |
|---|---|---|---|
| `request_id` (UUID-v4) | Daemon RPC handler on every inbound method call | Tie CLI → daemon → projection writes | One per RPC call; logged on every daemon-side log record |
| `wave_id` (`W<NN>` per AGENTS rule 5) | `eawf wave claim` mutation | Tie all dispatched-subagent logs to the wave | Lives for the wave's lifetime |
| `attempt_id` (UUID-v4) | Daemon's dispatch envelope writer per attempt | Per-runtime invocation; replays trace V5 fallbacks | One per dispatch attempt; new attempt on V5 switchover |

**Propagation.**

- CLI → daemon: `request_id` injected as RPC envelope header; daemon stamps every log record with `extra={"request_id": ...}`.
- Daemon → subagent: dispatch envelope carries `trace.request_id`, `trace.wave_id`, `trace.attempt_id` fields; subagent's `eawf wave close` reads them from env (`EAWF_TRACE_REQUEST_ID`, `EAWF_TRACE_WAVE_ID`, `EAWF_TRACE_ATTEMPT_ID`) and emits matching `EventPayload.trace_*` fields.
- `EventPayload` extension:

```python
class EventPayload(BaseModel):
    # ... existing fields per C07b §5.4 [10:399-412]
    trace_request_id: str | None = None       # NEW; C09
    trace_wave_id: str | None = None          # NEW; C09 (matches state wave_id)
    trace_attempt_id: str | None = None       # NEW; C09
```

**Operator search.** `grep -F 'wave=W17' <local-path>` returns the wave's daemon-side activity; `eawf wave logs W17` is the future thin wrapper (deferred to C10 onboarding polish — out of scope for v0.3-v0.5).

**OTel correlation.** When `telemetry.otel.enabled=true` (v0.5+ stretch — not enabled in v0.3-v0.5; placeholder config row reserved in C08 §5.2.7 for future bumps), the `trace_request_id` doubles as the OTel `trace_id` lower-64; `trace_wave_id` becomes a span attribute; `trace_attempt_id` becomes the per-span name. Defer the wiring until the OTel gen-ai SC stabilises per Axis-D Open Q9 [5:621].

### 5.9 Telemetry subsystem (V7) — vendoring + projection

#### 5.9.1 Module layout

```
src/eawf/telemetry/
├── __init__.py
├── models.py              # Pydantic v2 row models (vendored shape, retyped)
├── pricing.py             # Decimal ModelPricing dict — B12-snapshot embed per R1 ratification (2026-05-17 12:00 UTC fetch from Anthropic canonical pricing page). Telemetry-prototype snapshot stale (Opus 4.x = 3× over-bill, Haiku 4.5 = 25% under-bill). Extended schema: cache_write_5m + cache_write_1h split + pricing_version + fetched_at metadata. See §5.9.6.1 for the snapshot table. Weekly CI cron `eawf telemetry pricing-currency-check` opens auto-PR on drift.
├── store/
│   ├── __init__.py
│   ├── base.py            # AbstractMetricsStore protocol
│   ├── duckdb_store.py    # primary
│   └── sqlite_store.py    # fallback
├── sources/
│   ├── __init__.py
│   ├── event_jsonl.py     # canonical eawf event.jsonl + audit.jsonl + <role>_report.jsonl
│   ├── claude_session.py  # <local-path>
│   ├── codex_session.py   # <local-path>
│   └── opencode_session.py # <local-path> (sqlite)
├── aggregator.py          # vendored one-pass projection
├── projector.py           # writes rows into AbstractMetricsStore
├── exporter.py            # Prometheus textfile / JSON / CSV emit
├── scrubber.py            # regex scrub (default); presidio extra
└── cli/                   # called from src/eawf/cli/commands/metrics.py
    └── format.py          # table / json / prom render
```

#### 5.9.2 Schema (Pydantic v2, retyped from telemetry-prototype dataclasses)

```python
# src/eawf/telemetry/models.py
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TelemetryProject(BaseModel):
    """One row per eawf project (a single .ea/-bearing repo)."""
    model_config = ConfigDict(extra="forbid")
    project_id: str                                  # sha256(repo_path)[:12]
    cwd: str                                          # absolute; never persisted from CLI input — always derived
    repo_name: str | None
    first_seen: datetime | None
    last_seen: datetime | None
    has_settings_local: bool = False
    has_agents_md: bool = False                       # eawf-specific (was has_claude_md in the prototype)
    has_eawf_state: bool = False                      # NEW


class TelemetrySession(BaseModel):
    """One row per dispatched session (wave attempt or interactive CLI session)."""
    model_config = ConfigDict(extra="forbid")
    session_id: str
    project_id: str
    runtime: Literal["claude", "codex", "opencode"]   # NEW vs the prototype (CC-only)
    wave_id: str | None                                # NEW vs the prototype
    attempt_id: str | None                             # NEW vs the prototype
    session_log_path: str
    started_at: datetime | None
    ended_at: datetime | None
    duration_ms: int | None
    model_primary: str | None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read: int = 0
    total_cache_write: int = 0
    total_cost_usd: Decimal = Field(default=Decimal("0"))  # Decimal per Axis D [5:401-402]
    turn_count: int = 0
    tool_call_count: int = 0
    error_count: int = 0
    denial_count: int = 0
    interrupt_count: int = 0
    compaction_count: int = 0
    subagent_dispatch_count: int = 0
    end_marker: EndMarker
    parent_uuid_orphan_rate: float = 0.0
    git_branch_first: str | None = None
    custom_title: str | None = None
    ai_title: str | None = None


class TelemetryTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    turn_idx: int
    ts: datetime | None
    duration_ms: int | None
    model: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    thinking_only: bool = False


class TelemetryToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    turn_idx: int
    tool_use_id: str
    tool_name: str
    input_hash: str
    ts: datetime | None
    ended_ts: datetime | None
    is_error: bool = False
    error_kind: ToolCallErrorKind                       # closed enum, retyped from free str
    retry_of: str | None = None


class TelemetryCompaction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    ts: datetime | None
    pre_tokens: int | None
    trigger: str | None


class TelemetryRuntimeSwitch(BaseModel):                # NEW vs the prototype; V5
    model_config = ConfigDict(extra="forbid")
    wave_id: str
    attempt_id_from: str
    attempt_id_to: str
    runtime_from: Literal["claude", "codex", "opencode"]
    runtime_to: Literal["claude", "codex", "opencode"]
    cause: RuntimeErrorClass                            # 5-class enum per C07a §5.5
    ts: datetime


class TelemetryIncident(BaseModel):                     # NEW vs the prototype; V7
    model_config = ConfigDict(extra="forbid")
    incident_id: str
    severity: IncidentSeverity                          # closed enum per current code
    cause: IncidentCause                                # closed enum per §5.10 below
    ts: datetime
    summary: str
    wave_id: str | None = None
    attempt_id: str | None = None


class TelemetryFileMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jsonl_path: str
    mtime: float
    size: int
    last_offset: int
    last_scan_ts: datetime


class TelemetrySchemaMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: str
```

**Closed enums** (new):

```python
EndMarker = Literal[
    "clean_stop", "away", "pr_link",
    "last_assistant_inflight", "last_user_typed",
    "permission_change_at_end", "runtime_switched", "other",
    # "runtime_switched" is the V5 extension (eawf-specific)
]

class ToolCallErrorKind(StrEnum):
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    RUNTIME_OOM = "runtime_oom"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"
    UNKNOWN = "unknown"

class RuntimeErrorClass(StrEnum):
    RUNTIME_RATE_LIMIT = "RUNTIME_RATE_LIMIT"
    RUNTIME_SERVER_ERROR = "RUNTIME_SERVER_ERROR"
    RUNTIME_TIMEOUT = "RUNTIME_TIMEOUT"
    RUNTIME_API_ERROR = "RUNTIME_API_ERROR"
    RUNTIME_AUTH_ERROR = "RUNTIME_AUTH_ERROR"
```

#### 5.9.3 DuckDB DDL (generated from the Pydantic models)

`telemetry.store.duckdb_store.init_schema()` emits the DDL below; SQLite store emits the same with `BIGINT` → `INTEGER` rewrite. Schema version pinned `telemetry_schema_version="1"` on `telemetry_schema_meta`.

```sql
CREATE TABLE IF NOT EXISTS telemetry_projects (
    project_id              TEXT PRIMARY KEY,
    cwd                     TEXT NOT NULL,
    repo_name               TEXT,
    first_seen              TIMESTAMP,
    last_seen               TIMESTAMP,
    has_settings_local      BOOLEAN,
    has_agents_md           BOOLEAN,
    has_eawf_state          BOOLEAN
);
CREATE TABLE IF NOT EXISTS telemetry_sessions (
    session_id              TEXT PRIMARY KEY,
    project_id              TEXT REFERENCES telemetry_projects(project_id),
    runtime                 TEXT NOT NULL,
    wave_id                 TEXT,
    attempt_id              TEXT,
    session_log_path        TEXT NOT NULL,
    started_at              TIMESTAMP,
    ended_at                TIMESTAMP,
    duration_ms             BIGINT,
    model_primary           TEXT,
    total_input_tokens      BIGINT,
    total_output_tokens     BIGINT,
    total_cache_read        BIGINT,
    total_cache_write       BIGINT,
    total_cost_usd          DECIMAL(18,6),
    turn_count              INTEGER,
    tool_call_count         INTEGER,
    error_count             INTEGER,
    denial_count            INTEGER,
    interrupt_count         INTEGER,
    compaction_count        INTEGER,
    subagent_dispatch_count INTEGER,
    end_marker              TEXT,
    parent_uuid_orphan_rate DOUBLE,
    git_branch_first        TEXT,
    custom_title            TEXT,
    ai_title                TEXT
);
CREATE TABLE IF NOT EXISTS telemetry_turns (
    session_id              TEXT REFERENCES telemetry_sessions(session_id),
    turn_idx                INTEGER,
    ts                      TIMESTAMP,
    duration_ms             BIGINT,
    model                   TEXT,
    input_tokens            INTEGER,
    output_tokens           INTEGER,
    cache_read              INTEGER,
    cache_write             INTEGER,
    thinking_only           BOOLEAN,
    PRIMARY KEY (session_id, turn_idx)
);
CREATE TABLE IF NOT EXISTS telemetry_tool_calls (
    session_id              TEXT REFERENCES telemetry_sessions(session_id),
    turn_idx                INTEGER,
    tool_use_id             TEXT PRIMARY KEY,
    tool_name               TEXT,
    input_hash              TEXT,
    ts                      TIMESTAMP,
    ended_ts                TIMESTAMP,
    is_error                BOOLEAN,
    error_kind              TEXT,
    retry_of                TEXT
);
CREATE TABLE IF NOT EXISTS telemetry_compactions (
    session_id              TEXT REFERENCES telemetry_sessions(session_id),
    ts                      TIMESTAMP,
    pre_tokens              BIGINT,
    trigger                 TEXT,
    PRIMARY KEY (session_id, ts)
);
CREATE TABLE IF NOT EXISTS telemetry_runtime_switches (
    wave_id                 TEXT,
    attempt_id_from         TEXT,
    attempt_id_to           TEXT,
    runtime_from            TEXT,
    runtime_to              TEXT,
    cause                   TEXT,
    ts                      TIMESTAMP,
    PRIMARY KEY (wave_id, attempt_id_from, attempt_id_to)
);
CREATE TABLE IF NOT EXISTS telemetry_incidents (
    incident_id             TEXT PRIMARY KEY,
    severity                TEXT,
    cause                   TEXT,
    ts                      TIMESTAMP,
    summary                 TEXT,
    wave_id                 TEXT,
    attempt_id              TEXT
);
CREATE TABLE IF NOT EXISTS telemetry_file_meta (
    jsonl_path              TEXT PRIMARY KEY,
    mtime                   DOUBLE NOT NULL,
    size                    BIGINT NOT NULL,
    last_offset             BIGINT NOT NULL,
    last_scan_ts            TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry_schema_meta (
    key                     TEXT PRIMARY KEY,
    value                   TEXT
);
```

DuckDB sizing on the operator's current corpus: estimate ~5-10 MB per 1,000 sessions; current corpus (~200 sessions per pre-work memo) projects to ~1-2 MB — DuckDB install weight (~30 MB wheel) is the real cost, not the DB file.

#### 5.9.4 Projection algorithm

`telemetry.projector.rebuild(scope_id=None)` is the canonical entry point. Reads from:

1. **eawf event.jsonl** (`.ea/store/event.jsonl`) — typed `EventPayload` envelopes; emits `TelemetryRuntimeSwitch`, `TelemetryIncident`, `TelemetrySession` rows (when `dispatch_cost` envelopes carry token totals).
2. **Per-runtime session logs** (Claude / Codex / OpenCode) — feeds `aggregate_session` for per-session row generation per the vendored algorithm.
3. **Per-role report jsonl** (`.ea/store/<role>_report.jsonl`) — emits `TelemetryIncident` rows (via `<role>_report.body.failure` paths).

```python
# src/eawf/telemetry/projector.py
def rebuild(store: AbstractMetricsStore, *, scope_id: str | None = None,
            since: datetime | None = None) -> RebuildReport:
    """Rebuild the projection from canonical sources.

    Bounded by `(scope_id, since)`. If both None: full rebuild.
    """
    counters = RebuildCounters()
    for project in iter_projects(scope_id):
        store.upsert_project(project_row_for(project))
        # Per-runtime session logs.
        for runtime_adapter in (claude_session, codex_session, opencode_session):
            for session_log in runtime_adapter.iter_session_logs(project, since=since):
                records = runtime_adapter.parse(session_log)
                bundle = aggregate_session(records,
                                            session_id=session_log.session_id,
                                            project_id=project.project_id,
                                            jsonl_path=str(session_log.path))
                store.upsert_session(bundle.session)
                store.bulk_insert_turns(bundle.turns)
                store.bulk_insert_tool_calls(bundle.tool_calls)
                store.bulk_insert_compactions(bundle.compactions)
                counters.sessions += 1
        # eawf event.jsonl per project.
        for env in iter_event_envelopes(project, since=since):
            if env.kind != StoreKind.EVENT:
                continue
            sub = env.payload.get("event_type")
            if sub == "runtime_switched":
                store.upsert_runtime_switch(parse_runtime_switch(env))
            elif sub == "dispatch_cost":
                # If session_id absent from per-runtime logs (rare; fresh wave dispatched offline),
                # synthesise a TelemetrySession row from dispatch_cost envelope.
                store.upsert_session(session_from_dispatch_cost(env))
            elif sub in INCIDENT_SUBTYPES:
                store.upsert_incident(parse_incident_event(env))
        # Per-role report jsonl.
        for role in AgentSessionRole:
            for env in iter_report_envelopes(project, role, since=since):
                for incident in extract_incidents_from_report(env):
                    store.upsert_incident(incident)
        counters.projects += 1
    store.touch_schema_meta(last_full_scan_ts=now_utc())
    return RebuildReport(counters)
```

**Idempotency.** Every `upsert_*` is keyed on the table's primary key; replay produces identical rows. `telemetry_file_meta(last_offset)` lets per-session-log tail scans skip already-projected bytes — `eawf metrics rebuild --incremental` reads only new tail bytes; `eawf metrics rebuild --full` drops + recreates the DB.

**Bounded memory.** One project at a time; one session log at a time; per-session record list is materialised in `aggregate_session` (vendored algorithm). At medium fixture (50 waves × ~200 records each) memory peak ≈ 100 MB; at large (200 waves) ≈ 400 MB. Bench harness `telemetry_rebuild` verifies the budget.

#### 5.9.5 `eawf metrics` CLI surface

```
eawf metrics show [--scope <urn>] [--window 7d|30d|90d|all]
                  [--runtime claude|codex|opencode|all]
                  [--format table|json|prom]
                  [--cache-info]

eawf metrics export [--format prom|json|csv]
                    [--scope <urn>] [--window 7d|30d|90d|all]
                    [--out <path>]
                    [--scrubber regex|presidio]

eawf metrics rebuild [--full|--incremental]
                     [--scope <urn>]
                     [--since YYYY-MM-DD]

eawf metrics info        # cache stats: DB path, size, schema_version,
                         # row counts per table, last_full_scan
```

**Default scopes.** No `--scope` → current repo. `--scope user` → all repos in the user-scope DB. `--scope workspace` → all repos in the workspace (per registry per C08).

**`--format prom` output** (Prometheus textfile collector v0.0.4 format):

```
# HELP eawf_tokens_total Total tokens by direction.
# TYPE eawf_tokens_total counter
eawf_tokens_total{direction="input",runtime="claude",scope="repo/eawf"} 812440
eawf_tokens_total{direction="output",runtime="claude",scope="repo/eawf"} 142810
eawf_tokens_total{direction="cache_read",runtime="claude",scope="repo/eawf"} 6108002
eawf_tokens_total{direction="cache_create",runtime="claude",scope="repo/eawf"} 348221

# HELP eawf_cost_usd_total Cumulative cost in USD.
# TYPE eawf_cost_usd_total counter
eawf_cost_usd_total{runtime="claude",scope="repo/eawf"} 7.31

# HELP eawf_runtime_switches_total Runtime switchovers per cause.
# TYPE eawf_runtime_switches_total counter
eawf_runtime_switches_total{from="claude",to="codex",cause="RUNTIME_RATE_LIMIT"} 12

# HELP eawf_cache_hit_ratio Cache-read / (cache-read + cache-create).
# TYPE eawf_cache_hit_ratio gauge
eawf_cache_hit_ratio{runtime="claude",scope="repo/eawf"} 0.946

# ... wave_duration_ms_p50, wave_duration_ms_p99, session_turns_p50, etc.
```

Power users wire `--format prom --out <local-path>` into a cron / launchd job.

#### 5.9.6 Metrics catalog (full V7 inventory)

Every metric below is projected from the typed sources above and surfaces in either `eawf metrics show`, `eawf metrics export`, or both. Type = Prometheus type (`counter` / `gauge` / `histogram`).

| # | Metric | Type | Source | Labels | Notes |
|---:|---|---|---|---|---|
| M01 | `eawf_tokens_total` | counter | `TelemetrySession.total_*` | direction (input/output/cache_read/cache_create), runtime, scope | Per V7 [C00:197] |
| M02 | `eawf_cost_usd_total` | counter | `TelemetrySession.total_cost_usd` | runtime, scope, model | Decimal-quantised per Axis D; values use the §5.9.6.1 PRICING snapshot (`pricing_version="2026.05.17"`); guarded by weekly `eawf telemetry pricing-currency-check` CI gate per [B12 blitz r12] [37] — drift opens auto-PR with refreshed rates + bumped `pricing_version`. |
| M03 | `eawf_session_efficiency_eu` | gauge | EU delivered / EU planned per wave | scope, phase | Cross-references EstimatePayload + ActualPayload |
| M04 | `eawf_cache_hit_ratio` | gauge | cache_read / (cache_read + cache_create) | runtime, scope | Per V7 [C00:202]; alarm trigger when < 0.5 for >2 dispatches in 5 min [9:295] |
| M05 | `eawf_turn_count_histogram` | histogram | `TelemetrySession.turn_count` | runtime, role | V8 turn-count-vs-complexity signal |
| M06 | `eawf_context_window_pressure` | gauge | turns_to_compaction = (turn count between two compactions) | runtime, model | V8 context-window pressure metric |
| M07 | `eawf_incidents_total` | counter | `TelemetryIncident` | severity, cause | Per V7 [C00:199] |
| M08 | `eawf_burn_rate_7d_usd` | gauge | rolling 7d sum of `total_cost_usd` | scope | Per V7 [C00:200] |
| M09 | `eawf_burn_rate_30d_usd` | gauge | rolling 30d sum | scope | Per V7 [C00:200] |
| M10 | `eawf_burn_rate_90d_usd` | gauge | rolling 90d sum | scope | Per V7 [C00:200] |
| M11 | `eawf_wave_duration_ms` | histogram | `TelemetrySession.duration_ms` filtered to `wave_id IS NOT NULL` | phase, runtime | p50/p99 surface to TUI tile |
| M12 | `eawf_dispatch_latency_ms` | histogram | `TelemetrySession.started_at - dispatch_envelope.created_at` | runtime | Cold-spawn vs warm-spawn rolled together |
| M13 | `eawf_runtime_switches_total` | counter | `TelemetryRuntimeSwitch` | from, to, cause | V5 switchover frequency [C00:200] |
| M14 | `eawf_session_continued_total` | counter | `event_type=session_continued` events | runtime | V8 retry continue count |
| M15 | `eawf_session_failover_total` | counter | `event_type=session_failover` events | runtime, from_continue_to | V8 continue→fresh fallback count |
| M16 | `eawf_daemon_rpc_qps` | gauge | rolling 1m rate of `request_id` count | method | V1 daemon QPS [C00 §C09:851] |
| M17 | `eawf_daemon_refresh_tick_cost_ms` | histogram | daemon-side TUI push tick | (none) | V1 refresh tick cost |
| M18 | `eawf_daemon_idle_shutdown_total` | counter | daemon `idle_shutdown` events | (none) | V1 idle-timeout shutdown rate |
| M19 | `eawf_daemon_spawn_latency_ms` | histogram | daemon `cold_spawn` events | os | V6 per-OS spawn latency |
| M20 | `eawf_cache_mislayer_alarms_total` | counter | `event_type=cache_mislayer_alarm` events | runtime, scope | Tuned per [B03 blitz r3]: trigger `cc>2000 AND cr>0 AND cc/cr>10.0 × 2 dispatches × 5-min window`. Supersedes the original `ratio>4.0` threshold from [9:308] (would fire on 16% of sessions on real corpus; tuned values fire on 4%). |
| M21 | `eawf_subagent_dispatch_total` | counter | `TelemetrySession.subagent_dispatch_count` summed | (none) | Cumulative across all sessions |
| M22 | `eawf_tool_call_errors_total` | counter | `TelemetryToolCall.is_error=true` count | tool_name, error_kind | Per-tool error attribution |
| M23 | `eawf_compaction_total` | counter | `TelemetryCompaction` row count | runtime | Per-runtime compaction count |
| M24 | `eawf_orphan_uuid_rate` | gauge | `TelemetrySession.parent_uuid_orphan_rate` | (none) | Trust signal vendored from the telemetry prototype |
| M25 | `eawf_plugin_doctor_drift_total` | counter | `event_type=plugin_drift_detected` events | runtime | V9 drift signal |
| M26 | `eawf_estimate_actual_variance_pct` | gauge | (actual EU - planned EU) / planned EU × 100 | scope, phase | Feeds C06 VarianceTile |
| M27 | `eawf_wal_recovery_total` | counter | `event_type=wal_recovery` events | (none) | Daemon-side WAL recovery; V1 |

#### 5.9.6.1 PRICING dict — embedded snapshot (per R1 ratification)

Canonical Anthropic published rates fetched 2026-05-17 12:00 UTC from `https://platform.claude.com/docs/en/about-claude/pricing`. **This is the source-of-truth committed at C09 vendor time**. Re-pricing of historical cost rows uses `pricing_version` for retroactive recompute per Axis D [5:401-406].

```python
# src/eawf/telemetry/pricing.py — embedded per R1 ratification
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ModelPricing(BaseModel):
    """Per-token USD prices for a Claude model.

    All values in USD per token (NOT per million). Decimal-quantised at
    6 decimal places to avoid float drift on long-horizon ledger sums.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_token: Decimal
    output_per_token: Decimal
    cache_read_per_token: Decimal
    cache_write_5m_per_token: Decimal
    cache_write_1h_per_token: Decimal
    pricing_version: str         # e.g. "2026.05.17"
    fetched_at: datetime         # UTC


PRICING_VERSION = "2026.05.17"
PRICING_FETCHED_AT = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)


PRICING: dict[str, ModelPricing] = {
    # Opus 4.x — $5 / $25 per MTok (2026-05-17 rates; previously $15/$75 in the prototype snapshot)
    "claude-opus-4-7":   ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("0.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-opus-4-6":   ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("0.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-opus-4-5":   ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("0.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-opus-4-1":   ModelPricing(
        input_per_token=Decimal("15e-6"),
        output_per_token=Decimal("75e-6"),
        cache_read_per_token=Decimal("1.5e-6"),
        cache_write_5m_per_token=Decimal("18.75e-6"),
        cache_write_1h_per_token=Decimal("30e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    # Sonnet 4.x — $3 / $15 per MTok
    "claude-sonnet-4-6": ModelPricing(
        input_per_token=Decimal("3e-6"),
        output_per_token=Decimal("15e-6"),
        cache_read_per_token=Decimal("0.3e-6"),
        cache_write_5m_per_token=Decimal("3.75e-6"),
        cache_write_1h_per_token=Decimal("6e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-sonnet-4-5": ModelPricing(
        input_per_token=Decimal("3e-6"),
        output_per_token=Decimal("15e-6"),
        cache_read_per_token=Decimal("0.3e-6"),
        cache_write_5m_per_token=Decimal("3.75e-6"),
        cache_write_1h_per_token=Decimal("6e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    # Haiku 4.5 — $1 / $5 per MTok (2026-05-17 rates; previously $0.80/$4 in the prototype snapshot)
    "claude-haiku-4-5-20251001": ModelPricing(
        input_per_token=Decimal("1e-6"),
        output_per_token=Decimal("5e-6"),
        cache_read_per_token=Decimal("0.1e-6"),
        cache_write_5m_per_token=Decimal("1.25e-6"),
        cache_write_1h_per_token=Decimal("2e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-haiku-4-5": ModelPricing(  # alias-only entry
        input_per_token=Decimal("1e-6"),
        output_per_token=Decimal("5e-6"),
        cache_read_per_token=Decimal("0.1e-6"),
        cache_write_5m_per_token=Decimal("1.25e-6"),
        cache_write_1h_per_token=Decimal("2e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
}


def lookup_pricing(model: str) -> ModelPricing | None:
    """Look up by exact model id; fall back to longest-prefix match."""
    if model in PRICING:
        return PRICING[model]
    matches = sorted(
        ((k, v) for k, v in PRICING.items() if model.startswith(k)),
        key=lambda kv: len(kv[0]),
        reverse=True,
    )
    return matches[0][1] if matches else None
```

Caching multipliers stated by Anthropic [40]: 5m cache write = 1.25× base input; 1h cache write = 2× base input; cache read = 0.1× base input. Encoded explicitly above (not derived) to avoid float-drift on rounding.

**Tile mapping** (C06 `MetricsModal` 3×2 tile grid [7:884-953]):

| Tile | Metrics consumed |
|---|---|
| VarianceTile | M26 |
| WeeklyBurnTile | M08 |
| WaveElapsedTile | M11 (p50/p99) |
| CacheHealthTile | M04 + M20 |
| SwitchoverFreqTile | M13 |
| PerRuntimeTokensTile | M01 grouped by runtime |

### 5.10 Incident-cause taxonomy (V7)

Promotes `IncidentPayload.root_cause: str | None` to a typed closed enum so the projection can `GROUP BY cause` without string normalisation. Existing free-string values migrate to `IncidentCause.LEGACY_FREE_TEXT` + the original prose in `Incident.summary` (no data loss).

```python
# src/eawf/state/enums.py — extended
class IncidentCause(StrEnum):
    # Runtime / dispatch surface
    RUNTIME_RATE_LIMIT = "runtime_rate_limit"        # V5 RUNTIME_RATE_LIMIT [9:285]
    RUNTIME_SERVER_ERROR = "runtime_server_error"
    RUNTIME_TIMEOUT = "runtime_timeout"
    RUNTIME_API_ERROR = "runtime_api_error"
    RUNTIME_AUTH_ERROR = "runtime_auth_error"        # halt class per [9:289]
    RUNTIME_UNAVAILABLE = "runtime_unavailable"      # ladder exhausted [10:440]
    RUNTIME_OAUTH_CACHE_STRIPPED = "runtime_oauth_cache_stripped"  # OpenCode/OAuth regression [9:306]

    # Daemon / IPC surface
    DAEMON_WAL_RECOVERY = "daemon_wal_recovery"
    DAEMON_SOCKET_BIND = "daemon_socket_bind"
    DAEMON_VERSION_SKEW = "daemon_version_skew"
    DAEMON_SUBPROCESS_OOM = "daemon_subprocess_oom"
    DAEMON_SUBSCRIPTION_DROPPED = "daemon_subscription_dropped"
    DAEMON_LOCK_TIMEOUT = "daemon_lock_timeout"

    # Cache / cost surface
    CACHE_MISLAYER = "cache_mislayer"                # [9:308]
    COST_BUDGET_BREACHED = "cost_budget_breached"

    # Session / dispatch surface
    SESSION_HANDLE_PRUNED = "session_handle_pruned"  # V8 TTL sweep [10:443]
    SESSION_FAILOVER = "session_failover"            # V8 continue→fresh fallback [10:442]

    # Worktree / git surface
    WORKTREE_CHERRY_PICK_CONFLICT = "worktree_cherry_pick_conflict"
    WORKTREE_BRANCH_STALE = "worktree_branch_stale"
    GIT_PUSH_REJECTED = "git_push_rejected"

    # Plugin / sync surface
    PLUGIN_DRIFT = "plugin_drift"                    # V9 doctor finding

    # Validation / spec surface
    SPEC_VALIDATION_FAILED = "spec_validation_failed"
    STATE_VALIDATION_FAILED = "state_validation_failed"
    AUDIT_FAILED = "audit_failed"

    # External / human surface
    OPERATOR_INTERRUPT = "operator_interrupt"
    EXTERNAL_API_FAILURE = "external_api_failure"

    # Catchall + legacy
    LEGACY_FREE_TEXT = "legacy_free_text"            # pre-C09 rows with prose root_cause
    UNKNOWN = "unknown"
```

**Severity assignment** (default per cause; operator may override on emit):

| Severity | Causes |
|---|---|
| `CRITICAL` | `DAEMON_WAL_RECOVERY` (data-loss risk), `STATE_VALIDATION_FAILED` (corruption), `RUNTIME_AUTH_ERROR` (auth ≠ availability halt) |
| `HIGH` | `RUNTIME_UNAVAILABLE`, `COST_BUDGET_BREACHED`, `GIT_PUSH_REJECTED`, `WORKTREE_CHERRY_PICK_CONFLICT`, `DAEMON_SUBPROCESS_OOM` |
| `MEDIUM` | `RUNTIME_SERVER_ERROR`, `RUNTIME_TIMEOUT`, `CACHE_MISLAYER`, `SPEC_VALIDATION_FAILED`, `AUDIT_FAILED`, `PLUGIN_DRIFT` |
| `LOW` | `RUNTIME_RATE_LIMIT`, `SESSION_HANDLE_PRUNED`, `SESSION_FAILOVER`, `OPERATOR_INTERRUPT`, `WORKTREE_BRANCH_STALE`, `RUNTIME_OAUTH_CACHE_STRIPPED` |

**Migration.** Per [B05 blitz r5] [32], the operator's eawf canonical repo has zero `incident.jsonl` rows. The migration concern is vacuous. **Simplified plan:** `IncidentPayload.cause: IncidentCause` is a **required field** on new emissions (Pydantic `Field(..., description="...")`). The `LEGACY_FREE_TEXT` enum value is retained as a documented sentinel for hypothetical downstream forks with pre-C09 rows, but the eawf canonical never defaults to it and never ships the `eawf incident classify` verb in v0.3-v0.5.

### 5.11 Event-kind extensions (V5 / V8 / V7)

Typed `EventPayload` discriminated-union sub-classes. Today's `EventPayload.event_type: str` becomes a typed union per C03 schema promotion; C09 owns the payload shapes for the new sub-types.

```python
# src/eawf/store/kinds/events/runtime_switched.py
class RuntimeSwitchedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: Literal["runtime_switched"] = "runtime_switched"
    timestamp: datetime
    wave_id: str
    attempt_id_from: str
    attempt_id_to: str
    runtime_from: Literal["claude", "codex", "opencode"]
    runtime_to: Literal["claude", "codex", "opencode"]
    cause: RuntimeErrorClass
    error_detail: str                                # scrubbed stderr per [9:258]
    idempotency_key: str

# src/eawf/store/kinds/events/session_continued.py
class SessionContinuedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: Literal["session_continued"] = "session_continued"
    timestamp: datetime
    wave_id: str
    attempt_id: str
    runtime: Literal["claude", "codex", "opencode"]
    session_handle: str                              # session_id (CC), session_id (Codex), session_id (OpenCode)
    session_log_path: str
    prior_turn_count: int                            # turn count at continue time

# src/eawf/store/kinds/events/session_failover.py
class SessionFailoverPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: Literal["session_failover"] = "session_failover"
    timestamp: datetime
    wave_id: str
    attempt_id_continue: str
    attempt_id_fresh: str
    runtime: Literal["claude", "codex", "opencode"]
    reason: Literal["session_expired", "file_deleted", "continue_rejected", "other"]
    prior_session_handle: str                        # the handle that failed --continue

# src/eawf/store/kinds/events/dispatch_cost.py
class DispatchCostPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: Literal["dispatch_cost"] = "dispatch_cost"
    timestamp: datetime
    wave_id: str | None                              # None for interactive CLI session
    attempt_id: str | None
    runtime: Literal["claude", "codex", "opencode"]
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cost_usd: Decimal
    pricing_version: str                             # per Axis D [5:401-406]

# src/eawf/store/kinds/events/cache_mislayer.py — tuned per [B03 blitz r3]
class CacheMislayerAlarmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: Literal["cache_mislayer_alarm"] = "cache_mislayer_alarm"
    timestamp: datetime
    runtime: Literal["claude", "codex", "opencode"]
    scope_id: str | None
    window_seconds: int                              # default 300; configurable via telemetry.cache_mislayer.window_seconds
    cache_creation_floor_tokens: int                 # default 2000; configurable
    ratio_threshold: float                           # default 10.0; configurable (was 4.0; over-fired on real corpus per B03)
    observed_ratio_a: float                          # first dispatch ratio in the window
    observed_ratio_b: float                          # second dispatch ratio
    observed_cc_a: int                               # cache_creation tokens, dispatch a
    observed_cc_b: int                               # cache_creation tokens, dispatch b

# Additional payloads for the catalog rows [10:436-451]:
# - RuntimePausedPayload      (V5 429 vendor-pause)
# - RuntimeAuthFailedPayload  (V5 halt)
# - RuntimeUnavailablePayload (V5 ladder exhausted)
# - SessionHandlePrunedPayload (V8 TTL sweep)
# - DaemonServiceEnabledPayload / DaemonServiceDisabledPayload (V6 service-registration)
# - WalRecoveryPayload         (V1 crash-safety)
# - SubscriptionDroppedPayload (C02 §5.7 overflow)
# - SubprocessOomKilledPayload (C02 §5.8 limits)
# - PluginDriftDetectedPayload (V9 doctor finding)

# Discriminated union owned by C03; C09 contributes the C09-owned payload sub-classes.
EventPayloadUnion = Annotated[
    RuntimeSwitchedPayload | SessionContinuedPayload | SessionFailoverPayload
    | DispatchCostPayload | ...,
    Field(discriminator="event_type"),
]
```

**Discriminator emit invariant.** Daemon-side mutation runner stamps the matching `event_type` string before envelope serialisation. Validation at append (Pydantic discriminated-union dispatch) ensures every payload-shape mismatch fails fast at write time, not at projection time.

## 6. Failure modes + named edge cases

| # | Failure | Surface | Detection | Recovery |
|---:|---|---|---|---|
| F1 | DuckDB install fails (no wheel for OS/arch) | First `eawf metrics rebuild` after enabling telemetry | `ImportError: No module named duckdb` | Fall back to `SQLiteStore` automatically; surface warning; doc points at `telemetry.db_kind=sqlite` config override |
| F2 | Per-runtime session log path missing | `rebuild` scan | Adapter `iter_session_logs` returns empty | Log INFO and skip; not an error (operator may not use that runtime) |
| F3 | `event.jsonl` corrupted mid-line | `iter_event_envelopes` | Pydantic validation fails on the malformed line | Skip + log WARNING with line number; record `TelemetryIncident(cause=STATE_VALIDATION_FAILED)`; continue projection |
| F4 | Schema version mismatch between DB and current eawf | `rebuild` startup check | `telemetry_schema_meta.schema_version != CURRENT_SCHEMA_VERSION` | Auto-drop + recreate (rebuildable invariant); log WARNING |
| F5 | Concurrent `eawf metrics rebuild` invocations | Two CLIs racing | DuckDB file lock conflict (DuckDB takes its own write lock) | Second invocation waits up to 30s then aborts with "rebuild already in progress" error |
| F6 | Bench harness flakes on noisy CI runner | `eawf bench compare` reports false-positive regression | ±10% threshold (D15) exceeded but local re-run passes | CI retries the bench job once on the same OS; on second failure, surface PR comment, don't block merge (warn-only for v0.3); promote to blocker after one phase of warn-only data |
| F7 | Snapshot golden churn breaks PR | Operator edits render code without `eawf snapshot update` | golden-diff CI job fails | Operator runs `eawf snapshot update --kind <surface>` locally, reviews diff, commits as `[P##-W##] test: snapshot update <kind>` |
| F8 | Sensitive-info hook false-positive on legitimate path | New commit blocked by `path-leak-lint` (hook 16) | Hook hit on `<local-path>` substring inside a known-safe context (e.g. quoted Codex doc path) | Wrap the literal in an `# noqa: path-leak` annotation; review the literal; alternative: scrub the literal to `<USER_HOME>` placeholder |
| F9 | Trace ID collision (UUID-v4 birthday paradox) | Two waves get the same `attempt_id` | Daemon-side dedup window catches it as duplicate dispatch | 1 in 2.7E+18 collision rate at v4; acceptable; daemon's idempotency-key check is the safety net |
| F10 | DuckDB upgrade breaks file format | `duckdb` minor version bump | `init_schema` raises `IOException` | DuckDB documents pinned compatibility; eawf pins `duckdb>=1.3,<2`; on `<2` violation, surface upgrade-path doc; drop + rebuild from canonical sources |
| F11 | Telemetry export to external endpoint leaks data | Operator sets `telemetry.export.endpoint`; payload contains scrubbable substring that regex missed | No detection — opt-in is operator's responsibility | Pre-export scrubber MUST run; `--scrubber presidio` recommended for export-to-public; log emit warns if `telemetry.export.endpoint` is set and `--scrubber=regex` (less coverage) |
| F12 | Snapshot test passes locally but fails on Windows CI | TUI golden contains Unix path separator | snapshot diff fails on `/` vs `\\` | Golden writer normalises to `/` at write time; reader normalises to `/` at compare time |
| F13 | `eawf bench` runs in parallel with operator work | Bench saturates CPU | Operator-driven CLI commands slow down | Bench harness sets `os.nice(10)` on Linux/macOS; `IDLE_PRIORITY_CLASS` on Windows |
| F14 | Coverage drop due to dead code added by another phase | CI gate (overall ≥60%) fails | Coverage drop ≥1 percentage point with no test added | Operator either adds test or deletes dead code per AGENTS rule 6 |
| F15 | Cross-OS line-ending drift in golden files | Hook 3 (trailing-whitespace) + git's `core.autocrlf` interaction | `\r\n` vs `\n` diff on Windows checkout | `.gitattributes` pins `*.txt linguist-language=Text` + `eol=lf` for `tests/golden/**`; golden writer always writes `\n` |
| F16 | Telemetry-prototype schema diverges after vendoring — closed; the prototype is retired | No upstream remains to publish a newer schema, so eawf telemetry rows cannot fall behind one | Audit-replay missing rows (unreachable while the prototype stays retired) | C09 implementation phase pins the audited source revision in `src/eawf/telemetry/_VENDOR_PROVENANCE.txt`; were the prototype ever revived, a mismatch on the next vendor sweep would trigger a re-audit |
| F17 | Per-OS CI matrix runs out of free-tier minutes | GHA billing surface | Email from billing | Drop the second-LTS slot from each OS (`ubuntu-22.04`, `macos-26`) — already proposed in §5.4 |
| F18 | Sensitive-data scrubber strips a legitimate path inside a CLI error message | `Cannot find /usr/local/etc/eawf/config.yaml` → `Cannot find <scrubbed>/etc/eawf/config.yaml` | Operator-facing error confusing | Scrubber regex is anchored at the three user-home roots only (macOS, Linux, Windows); `/usr/local/...`-rooted paths are kept |
| F19 | Plugin-doctor drift gate flakes after a successful sync | `eawf plugin sync` succeeded but checksum drifts between sync and doctor invocation | Concurrent edit to AGENTS.md | Sync + doctor share a `<local-path>` portalock; doctor reads the post-sync checksum from the same lock-scope |
| F20 | DuckDB query latency on large DB | Operator with many repos; user-scope query takes seconds | `eawf metrics show --scope user` slow | Add covering indexes on `(scope, started_at)`, `(project_id, runtime)`; bench harness `telemetry_rebuild_jumbo` verifies budget |
| F21 | OpenCode SQLite schema drift | OpenCode drizzle migration adds/removes tables; adapter's row parsing breaks | Per [B06 blitz r6] [33] — observed already (13→15 tables since C07a was written) | Adapter checks `__drizzle_migrations` fingerprint at startup; on unknown fingerprint, emits `Incident(cause=EXTERNAL_API_FAILURE, summary="opencode schema drift")` and skips OpenCode projection; operator updates adapter |

## 7. Migration plan

Phased rollout over four implementation phases (each phase ≈ one C09-touching feature wave bundle). Phase numbers below are placeholders (`P-Q##`) until the v0.3 → v0.5 roadmap assigns concrete `P<NN>` IDs in /prep.

### 7.1 Phase Q1 — Hook + log spec + log-format-lint

Surface: `.pre-commit-config.yaml`, `src/eawf/cli/commands/hook.py`, `src/eawf/logging/scrub.py`.

Waves:

- **Q1-W00** *(NEW; pre-flight per [B04 blitz r4])* Test-lift wave: raise `skills/_common.py` from 59% → ≥90%, `vcs/coauthor.py` from 80% → ≥85%, `worktree/git.py` from 59% → ≥85%. Adds dirty-repo fixture for `git.py` error-class paths; param cases for `_common.py` fallbacks; env-runtime detection cases per KISS-001 backlog [24]. 1-2 EU. Lands before any §5.2 gate activation.
- **Q1-W01** Add hooks 16-19 to `.pre-commit-config.yaml`; implement `eawf hook path-leak-lint | email-leak-lint | log-format-lint | plugin-doctor-drift` as a thin CLI dispatcher → library in `src/eawf/lint/`. **First** land the conditional-skip helper module (`src/eawf/lint/_conditional.py`: parses `git diff origin/main...HEAD --name-only`, exits early per a per-hook regex) per [B11 blitz r11] [38] — pre-push hooks would otherwise add 7-15s per push and tempt operators to `--no-verify`. `log-format-lint` implemented as `ruff check --select EAWF001` custom rule.
- **Q1-W02** Implement `SensitiveScrubber` log filter; wire as default `Handler` filter; add `tests/unit/test_log_scrub.py` with each pattern.
- **Q1-W03** Promote `eawf doc verify` from `stages: [manual]` to `stages: [pre-push]` with conditional-skip per [B11 blitz r11] [38] (drops to ~50ms on no-doc-change pushes); per-commit smoke test.

### 7.2 Phase Q2 — Bench harness + per-OS CI matrix

Surface: `.github/workflows/ci.yaml`, `src/eawf/cli/commands/bench.py`, `src/eawf/bench/`, `tests/perf/`, `tests/fixtures/bench/`.

Waves:

- **Q2-W01** `eawf bench fixture seed` + deterministic seed-based generator. Land small + medium + large fixtures.
- **Q2-W02** Bench harness catalog (eight harnesses per §5.5). `eawf bench run`, `eawf bench compare`. Per-OS baseline files at `.ea/bench/baseline-<runner-name>.json` per [B07 blitz r7] [34]. Per-OS threshold map at `.ea/bench/thresholds.yaml` (Linux 0.10, macOS 0.20, Windows 0.15 provisional). `EAWF_BENCH_THRESHOLD` env override.
- **Q2-W03** CI workflow update per Matrix D [B02 blitz r2]: two-job split (`test-linux-windows` on every push + PR; `test-macos` merge-to-main only). Drops `macos-26` + `ubuntu-22.04`. Bench job conditional on PR label.
- **Q2-W04** Portalocker `fcntl` vs `LockFileEx` parity test (`tests/integration/test_portalocker_per_os.py`) — gate fails on cross-OS divergence.
- **Q2-W05** *(NEW; R2 ratification)* Windows bench-threshold revisit. After 10+ successful Windows lane runs accumulate under Matrix D, re-measure relvar; tighten or loosen Windows lane from the provisional ±15% if measurement disagrees. Single-commit `[P##-CORE] state: windows bench threshold revisit per R2`. 0.5 EU.

### 7.3 Phase Q3 — Telemetry subsystem (V7)

Surface: `src/eawf/telemetry/`, `src/eawf/cli/commands/metrics.py`, `tests/golden/telemetry/`, `tests/golden/metrics_export/`.

Waves:

- **Q3-W01** Vendor + retype the telemetry-prototype models as Pydantic v2 under `src/eawf/telemetry/models.py`. **Commit `PRICING` dict per the §5.9.6.1 snapshot** (R1 ratification; `pricing_version="2026.05.17"`). Audit-revision pinning at `_VENDOR_PROVENANCE.txt`. Closed enums (`EndMarker`, `ToolCallErrorKind`, `RuntimeErrorClass`). Add `eawf telemetry pricing-currency-check` sub-verb that emits typed `PricingDriftReport` + bumps `PRICING_VERSION` on auto-PR.
- **Q3-W02** `AbstractMetricsStore` + `DuckDBStore` + `SQLiteStore`. Schema DDL generated from Pydantic. `init_schema` idempotent. Bench `telemetry_rebuild` baseline.
- **Q3-W03** Per-runtime source adapters under `src/eawf/telemetry/sources/`: `event_jsonl.py` (eawf canonical), `claude_session.py` (port from the telemetry prototype's parser), `codex_session.py` (new), `opencode_session.py` (new, SQLite read-only mode + JSON aggregation over `part.data` per [B06 blitz r6] [33]). OpenCode adapter scope grows to include drizzle-migration-fingerprint check + degrade-gracefully on schema drift. Estimated effort: ~100 LOC each JSONL adapter; ~200-300 LOC for OpenCode adapter. Each implements the `SessionSource` protocol.
- **Q3-W04** Vendored aggregator (`src/eawf/telemetry/aggregator.py`) — port `aggregate_session` from the telemetry prototype; replace substring incident classifiers with typed `Incident.cause` lookups.
- **Q3-W05** `eawf metrics show | export | rebuild | info` CLI dispatch + library. Prometheus-textfile exporter. CSV exporter. JSON exporter. `--scrubber regex|presidio` + `--presidio-model en_core_web_sm|md|lg` flags per [B08 blitz r8] [35]; presidio path gates on `eawf[telemetry-scrub]` extras with informative error. Goldens for each output format.
- **Q3-W06** Telemetry opt-in default (`telemetry.enabled=false`) wired through C08 config loader; one-time onboarding nudge when operator runs `eawf metrics show` on a disabled telemetry config.
- **Q3-W07** **D7 decision-envelope commit** (formerly: "bench-then-decide" — superseded by [B01 blitz r1]). Single-commit `[P##-CORE] state: telemetry-db-default decision per blitz r1 verdict (SQLite, DuckDB opt-in)`. References the blitz brief in the decision envelope's `body.evidence_ref`. No re-bench; the blitz measurement on operator's live `event.jsonl` already settled the default.

### 7.4 Phase Q4 — Incident-cause taxonomy + event-kind extensions

Surface: `src/eawf/state/enums.py`, `src/eawf/store/kinds/`, `src/eawf/store/kinds/events/`.

Waves:

- **Q4-W01** Add `IncidentCause` enum; `IncidentPayload.cause` as a **required** field (no default — per [B05 blitz r5]); severity-assignment table to docs. Migrate test fixtures only (no real-row migration; zero rows exist).
- **Q4-W02** Typed `EventPayload` discriminated-union sub-classes: `RuntimeSwitchedPayload`, `SessionContinuedPayload`, `SessionFailoverPayload`, `DispatchCostPayload`, `CacheMislayerAlarmPayload`, and the remainder per C07b [10:436-451]. C03 owns the union shape; C09 contributes the payload sub-classes.
- **Q4-W03** Daemon-side emit points: dispatch-runner emits `RuntimeSwitchedPayload` on V5 fallback; session-continue path emits `SessionContinuedPayload`; failover path emits `SessionFailoverPayload`; cost-projection emits `DispatchCostPayload` post-dispatch.
- **Q4-W04** Projection wiring: extend `telemetry.projector.rebuild` to consume the new event sub-types; goldens cover each shape.

### 7.5 Phase Q5 — Snapshot + plugin doctor surfaces

Surface: `src/eawf/cli/commands/snapshot.py`, `src/eawf/cli/commands/plugin.py` (doctor extension), `tests/golden/spec/`, `tests/golden/dispatch/`.

Waves:

- **Q5-W01** `eawf snapshot update --kind <surface>` verb across every category listed in §5.6; CI gate requires the `[P##-W##] test: snapshot update <kind>` commit prefix.
- **Q5-W02** Extend `eawf plugin doctor` with `--strict` mode (checksum-level drift detection); CI hook 19 wiring; portalock per F19.
- **Q5-W03** Per-runtime dispatch goldens (CC × {research, prep, audit, ship, flow}, Codex × subset, OpenCode × subset). Pair with C07a §5.4 path catalog.

### 7.6 Compat shims

None required mid-rollout. Telemetry projection is rebuildable; event-kind extensions are additive (new sub-types in the discriminated union). Incident-cause migration is additive (existing rows project as `LEGACY_FREE_TEXT`). Pre-commit hooks 16-19 land as `stages: [pre-commit]` so existing commits aren't retroactively rejected (the `.secrets.baseline` already covers historical hits).

### 7.7 Rollback

Each phase ships behind a flag-free additive surface. Rollback = revert the implementing commits; the canonical event.jsonl is unaffected (V7 projection is a cache). Drop `.ea/local/<phase>/telemetry/sessions.db` to reset the projection without losing source data.

## 8. Open questions for operator

These are the `AskUserQuestion` seeds for the C09 ratification round. Each carries pre-drafted options and a `(Recommended)` marker per the operator's `[[feedback_approval_via_askuserquestion]]` convention.

### Q1 — DuckDB vs SQLite default

**Question:** Which DB backend should be the default for V7 telemetry projection in v0.3-v0.5?

| Option | Description |
|---|---|
| **SQLite default; DuckDB opt-in (Recommended per [B01 blitz r1] [28])** | Measurement on operator's live `event.jsonl` (694 rows, 340 KB): SQLite wins 5-189× on every op + 0 MB install (DuckDB native lib = 38.1 MB). DuckDB break-even ~100K rows; eawf retention math projects ~240 rows/year/repo. Ship `AbstractMetricsStore` with both backends; `telemetry.db_kind=sqlite` default. |
| DuckDB default | V7 [C00:189] original hypothesis. Refuted by measurement; would pay 38 MB install + 189× slower bulk-insert for no query advantage at current scale. |
| SQLite only (drop DuckDB code path) | Even simpler; loses optional DuckDB path for power users with very large corpora. Defer this decision to v0.5+ once cross-operator usage data lands. |

Recommendation: **SQLite default; DuckDB opt-in** — per measurement.

### Q2 — telemetry-prototype audit access confirmed?

**Question:** The pre-work audit memo (`.ea/local/research/long-term/2026-05-17-telemetry-prototype-audit.md`) succeeded via the read access the operator granted to the prototype source. Confirm continued access for the C09 implementation phase's vendoring work?

| Option | Description |
|---|---|
| **Yes — gh PAT (Recommended)** | Implementation phase fetches files on demand via `gh api`. |
| Operator drops tarball | Operator exports a `.ea/local/research/long-term/telemetry-prototype-snapshot-<SHA>.tar.gz` for offline vendoring. |
| Operator flips repo public | The prototype's v0.2.0 timeline; operator may flip earlier for vendoring convenience. |

### Q3 — Telemetry export format priority

**Question:** Beyond Prometheus textfile (the default), which export formats land in v0.3-v0.5?

| Option | Description |
|---|---|
| **Prom + CSV (Recommended)** | CSV opens spreadsheet workflows; OTel gen-ai SC still "Development" status [5:621] — defer OTLP to v0.5+. |
| Prom only | Smallest surface; defer CSV + OTLP. |
| Prom + CSV + OTLP/JSON | OTLP early-adopt; risk: SC shape changes pre-stable. |

### Q4 — Per-OS CI matrix budget

**Question:** GHA macOS runners cost 10.33× Linux minutes (private repos); Windows costs 1.67×. Eawf is public on GHA → unlimited free. But the workflow template ships to private downstreams. Pick a matrix shape that works at both scales.

| Option | Description |
|---|---|
| **Matrix D — Linux + Windows on every push; macOS merge-only (Recommended per [B02 blitz r2] [29])** | V6 portalocker parity gate enforced on every PR via Linux + Windows. macOS catches its regressions on merge-to-main only. Estimated spend: ~1,796 min-eq/mo per workflow at 30 pushes/mo + 6 merges/mo — fits GHA Free plan (2,000 min/mo). |
| Matrix B — all three on every push | V6 ✓; ~4,771 min-eq/mo; overruns Free plan, fits Pro at ~$110/mo overrun. |
| Matrix C — Linux + Windows only | V6 ✓; cheapest at ~1,052 min-eq/mo. Drops macOS coverage entirely (operator catches macOS regressions only when they ship to PyPI/Brew). |
| Linux only (V6 violation) | Rejected; skips portalocker `LockFileEx` gate. |

### Q5 — Per-package coverage gates

**Question:** Coverage gates at the package level (D2 recommendation). Per [B04 blitz r4] [31], current eawf scores 86% overall with three packages below proposed gates (`skills/_common.py` 59%, `vcs/coauthor.py` 80%, `worktree/git.py` 59%). Confirm thresholds + pre-flight test-lift wave?

| Option | Description |
|---|---|
| **Keep proposed §5.2 thresholds; land pre-flight Q1-W00 test-lift wave (Recommended per [B04 blitz r4])** | One wave closes the three gaps before §5.2 gates activate. 1-2 EU. Backlog items KISS-001 + worktree-git-fixtures already exist. |
| Tighten gates to current reality (`floor(observed - 5)` per package) | Avoids the test-lift wave; locks current weak spots into the spec. Drift accumulates. |
| Per-file `# noqa: cov-gate` escape hatch | Surgical; new control surface to maintain; drifts unless reviewed. |
| Keep gates; defer test-lift until first real coverage failure | Mailbox-rule maintenance; loses the pre-flight cleanup benefit. |

### Q6 — Bench regression threshold

**Question:** Per [B07 blitz r7] [34], measured per-OS run-to-run relvar: Linux 6-7%; macOS 13-16%. Single uniform ±10% threshold over-flakes on macOS. Confirm per-OS thresholds?

| Option | Description |
|---|---|
| **Per-OS thresholds: Linux ±10%, macOS ±20%, Windows ±15% (Recommended per [B07 blitz r7])** | Each tuned to ~2σ band of observed variance. Implementation: `.ea/bench/thresholds.yaml` + env override. |
| Uniform ±10% across all OSes | Original spec. Flakes on every third macOS PR per measurement. |
| Uniform ±20% (loosen everywhere) | Reduces macOS flake; misses sub-20% Linux regressions. |
| ±5% strict | Rejected; flakes on every other PR even on Linux. |

### Q7 — Snapshot tooling

**Question:** Per [B09 blitz r9] [36], current corpus is 110 goldens / 648 KB / 41 commits/90d (low churn). Syrupy migration cost ~1 EU + 0.5 EU recurring UX loss. Confirm keep-custom?

| Option | Description |
|---|---|
| **Keep custom (Recommended per [B09 blitz r9])** | 110-golden corpus too small to amortise syrupy migration; per-format diffs + `eawf snapshot update --kind <surface>` clean noun-app verb + CI commit-prefix gate lose on migration. |
| Migrate to syrupy | ~1 EU port + 0.5 EU recurring UX loss. Negative ROI at current scale. |
| Migrate after 1,000+ goldens | Stretch threshold; revisit when corpus crosses 5× current size. |

### Q8 — Telemetry export presidio default

**Question:** Per [B08 blitz r8] [35], presidio's transitive dep closure is **157 packages** (~80-120 MB wheels + 12-750 MB ML model download). Regex covers 10 patterns; presidio adds PERSON/LOC/PHONE/IBAN/SSN NER. Confirm regex-default + presidio opt-in via `eawf[telemetry-scrub]` extras?

| Option | Description |
|---|---|
| **regex default; presidio via `eawf[telemetry-scrub]` extras (Recommended per [B08 blitz r8])** | 157-package dep closure + ML model download is too heavy to bake in. Extras-gated install. Model download stays operator-explicit. |
| presidio default | 157 deps + 750 MB model on every `uv tool install eawf` — rejected. |
| regex only (no presidio path at all) | Skips PERSON/LOC NER which is valuable for the export-to-network surface. |
| Custom scrubber (not presidio) | Re-implementation cost > extras-gated presidio; rejected unless presidio dep tree shrinks (no roadmap signal it will). |

### Q9 — `eawf bench` baseline storage

**Question:** Per [B07 blitz r7] [34], cross-OS spread on the same commit is **86%** (macos-26 136s vs macos-15 253s). Single shared baseline is broken under any sane threshold. Confirm per-OS baseline files?

| Option | Description |
|---|---|
| **Per-OS files `.ea/bench/baseline-<runner-name>.json` (Recommended per [B07 blitz r7])** | Each runner type compares only against its own historical baseline. Three files initially: `baseline-ubuntu-24.04.json`, `baseline-macos-15.json`, `baseline-windows-2025.json`. CI updates only its own file on `--update-baseline`. |
| Single file `.ea/bench/baseline.json` | Original spec. 86% cross-OS spread breaks every threshold. Rejected. |
| Per-OS-per-Python | All current runs on py3.14; defer split until 3.15 lands. |

### Q10 — Incident-cause migration

**Question:** Per [B05 blitz r5] [32], operator's eawf repo has **zero `incident.jsonl` rows**. The whole "migrate legacy free-text cause" axis is vacuous. Confirm the simplification?

| Option | Description |
|---|---|
| **Drop migration plan; make `cause` required field on new emissions (Recommended per [B05 blitz r5])** | Zero legacy rows exist; `LEGACY_FREE_TEXT` enum value reserved as documented sentinel for hypothetical downstream forks. `eawf incident classify` verb deferred. |
| Keep `LEGACY_FREE_TEXT` as default + operator-classify verb | Original plan. Costs verb implementation for no live use case on eawf canonical repo. |
| Auto-migrate via heuristics | Rejected; brittle; classification across zero rows still vacuous. |

### Q11 — Cache-mislayer tuned defaults (per [B03 blitz r3])

**Question:** B03 measured the cache-mislayer alarm against 50 sessions of the operator's live Claude Code corpus. The C07a §5.6 [9:308] / features-deep [5:599-605] original threshold (`ratio>4.0`, no floor, 2-in-5min, 5-min window) over-fires on 16% of real sessions. Tuned values (`ratio>10.0`, `cc>2000`, `cr>0`, 2-in-5min, 5-min window) fire on 4%. Confirm tuned defaults?

| Option | Description |
|---|---|
| **Tuned defaults: ratio>10, floor=2000, cr>0 exclusion (Recommended per [30])** | Drops false-positive rate 4× while still flagging the 12× regression failure mode features-deep [5:599-605] names. |
| Even tighter: ratio>15, floor=5000 | Aggressive; may miss the early stages of a mis-layer rollout. Revisit if 4% is still too noisy. |
| Keep original (ratio>4, no floor) | Original spec. Surfaces 16% session false-positive rate. Rejected unless operator wants very high sensitivity. |
| Make every value config-driven, no built-in default | Punts on the default; not a verdict; tuned values still ship as the recommended bootstrap. |

## 9. References

[1] `.ea/local/research/long-term/2026-05-16-c00-spec-index.md` — cluster index + V1-V9 verdicts.
[2] `.ea/local/research/long-term/2026-05-16-c01-foundations.md` — entity catalog, URN scheme, lifecycle DAGs.
[3] `.ea/local/research/long-term/2026-05-16-c02-daemon-topology.md` — daemon RPC, WAL recovery, service registration, session-handle tracking.
[4] `.ea/local/research/long-term/2026-05-16-c03-spec-infrastructure.md` — spec schemas, render verb, snapshot gates.
[5] `.ea/local/research/long-term/2026-05-15-long-term-features-deep.md` §"Axis D" (cost ledger + OTel) + line 599 (cache-control mis-layer alarm).
[6] `.ea/local/research/long-term/2026-05-16-c05-cli-surface.md` — `eawf metrics` verb [6:1214]; noun-app catalog.
[7] `.ea/local/research/long-term/2026-05-17-c06-operator-surface.md:884-953` — `/metrics` overlay tile grid.
[8] `.ea/local/research/long-term/2026-05-16-c08-configurability-profiles.md:252-261` — `telemetry.*` config keys.
[9] `.ea/local/research/long-term/2026-05-16-c07a-runtime-skill-dispatch.md:228-308` — per-runtime session-handle catalog + error-class normalization + cache-control hooks.
[10] `.ea/local/research/long-term/2026-05-16-c07b-vcs-worktree-events.md:353-471` — EventPayload + event-kind catalog + projection invariant.
[11] `AGENTS.md` — non-negotiable rules + naming conventions + test-discipline section.
[12] `.github/workflows/ci.yaml` — current CI workflow.
[13] `pyproject.toml` — pytest markers, mypy strict, ruff config, current dev deps.
[14] `.pre-commit-config.yaml` — current hook inventory (rows 1-15 in §5.3).
[15] `src/eawf/store/kinds/incident.py` — current `IncidentPayload`.
[16] `src/eawf/state/enums.py` — current `IncidentSeverity`, `AuditKind`, `StoreKind`.
[17] `src/eawf/store/envelope.py` — canonical envelope shape.
[18] `src/eawf/store/append.py` — single canonical JSONL writer (portalock + fsync).
[19] `tests/unit/`, `tests/integration/`, `tests/golden/`, `tests/property/`, `tests/eval/` — current test layout (audited 2026-05-17).
[20] `tests/conftest.py` — current root fixture (`tmp_repo`).
[21] `tests/eval/conftest.py` — eval-harness `SkillContext` fixture.
[22] `.ea/local/research/long-term/2026-05-17-telemetry-prototype-audit.md` — pre-work audit memo. Cite for V7 vendor schema attribution.
[23] `telemetry-prototype source` — the operator's upstream telemetry prototype, read under a direct access grant for the V7 audit. Audit-source revision pinned at `main@2026-04-30T05:25:25Z`.
[24] `.ea/local/research/yagni-kiss-dry-codebase-review-2026-05-15.md` — current codebase shape (LOC distribution, longest files, KISS-001..007 backlog).
[25] DuckDB documentation, file-format compatibility — `https://duckdb.org/docs/internals/storage` (cite for F10 pin rationale).
[26] Prometheus textfile collector format v0.0.4 — `https://github.com/prometheus/node_exporter#textfile-collector` (cite for §5.9.5 exporter shape).
[27] Anthropic prompt caching docs — cache_creation vs cache_read semantics; cited in M04 + M20.
[28] `.ea/local/research/long-term/2026-05-17-c09-blitz-duckdb-sqlite.md` — B01 blitz r1: DuckDB vs SQLite measurement on real event.jsonl. Verdict: SQLite default.
[29] `.ea/local/research/long-term/2026-05-17-c09-blitz-gha-matrix-budget.md` — B02 blitz r2: GHA matrix economics + Matrix D verdict.
[30] `.ea/local/research/long-term/2026-05-17-c09-blitz-cache-mislayer-tuning.md` — B03 blitz r3: cache-mislayer alarm threshold tuning. Verdict: ratio>10 + floor 2000 tokens + cr>0 exclusion.
[31] `.ea/local/research/long-term/2026-05-17-c09-blitz-coverage-current.md` — B04 blitz r4: current coverage 86% overall; 3 packages below proposed gates; pre-flight Q1-W00 test-lift wave verdict.
[32] `.ea/local/research/long-term/2026-05-17-c09-blitz-incident-cause-distribution.md` — B05 blitz r5: zero `incident.jsonl` rows; migration plan collapses; `cause` becomes required field.
[33] `.ea/local/research/long-term/2026-05-17-c09-blitz-opencode-schema-verify.md` — B06 blitz r6: OpenCode SQLite schema verify; 15 tables (vs C07a's 13); WAL ✓; `part.data` JSON aggregation pattern; drizzle-fingerprint drift-guard.
[34] `.ea/local/research/long-term/2026-05-17-c09-blitz-gha-wallclock-variance.md` — B07 blitz r7: per-OS GHA timing variance (Linux 6-7%, macOS 13-16%, cross-OS 86%); per-OS thresholds + per-OS baseline files verdict.
[35] `.ea/local/research/long-term/2026-05-17-c09-blitz-presidio-install-footprint.md` — B08 blitz r8: presidio 157-dep + 750 MB model closure; regex-default reaffirmed; `eawf[telemetry-scrub]` extras + explicit model download.
[36] `.ea/local/research/long-term/2026-05-17-c09-blitz-snapshot-tooling-churn.md` — B09 blitz r9: 110 goldens, 41 commits/90d; syrupy migration ROI negative; keep-custom verdict.
[37] `.ea/local/research/long-term/2026-05-17-c09-blitz-pricing-currency.md` — B12 blitz r12: the vendored telemetry-prototype PRICING dict is stale (Opus 4.x 3× over-bill; Haiku 4.5 25% under-bill); re-source from Anthropic canonical at Q3-W01; weekly CI currency-check.
[38] `.ea/local/research/long-term/2026-05-17-c09-blitz-hook-wallclock.md` — B11 blitz r11: pre-commit 4.89s --all-files, ~0.5-1.5s per-commit; hooks 16-19 + doc-verify add ~0.5s commit, ~7-15s push; conditional-skip helper mandatory.
[39] `.ea/local/research/long-term/2026-05-17-c09-blitz-codex-schema-verify.md` — B10 blitz r10: Codex JSONL shape verified against C07a §5.4; adapter ~100-150 LOC.
[40] `https://platform.claude.com/docs/en/about-claude/pricing` — canonical Anthropic pricing page (2026-05-17 source-of-truth for PRICING dict).

## 10. Provenance

- `store_record=none (local-only research brief)`
- `commit=3b86f7a (parent; revisions 2026-05-18)`
- `supersedes=none`
- `session=eawf-c09-spec-quality-observability-2026-05-17`
- `last_revised=2026-05-18 (audit-driven: D10 flipped to Matrix B macOS-every-PR per Q17; coverage gate per-package with documented exceptions per Q16 — 9-layer model with state.json-recorded waivers; SQLite confirmed locked per Q18; pricing source + cadence locked per G-34; ruff custom-rule feasibility flagged for G-28 re-verification; trace IDs required for mutations per G-32)`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md (3 MAJOR Claude findings; 12 Codex issues)`
- `authority_binding=Q1 (2026-05-18): telemetry projector = 4th daemon-internal writer (was originally 4th separate canonical mutator); folds into daemon per migration DAG.`
- `pre-work=.ea/local/research/long-term/2026-05-17-telemetry-prototype-audit.md`
- `blitz-r1=.ea/local/research/long-term/2026-05-17-c09-blitz-duckdb-sqlite.md (SQLite default)`
- `blitz-r2=.ea/local/research/long-term/2026-05-17-c09-blitz-gha-matrix-budget.md (Matrix D)`
- `blitz-r3=.ea/local/research/long-term/2026-05-17-c09-blitz-cache-mislayer-tuning.md (ratio>10 + floor 2000)`
- `blitz-r4=.ea/local/research/long-term/2026-05-17-c09-blitz-coverage-current.md (86% overall; pre-flight Q1-W00)`
- `blitz-r5=.ea/local/research/long-term/2026-05-17-c09-blitz-incident-cause-distribution.md (zero legacy rows; cause required)`
- `blitz-r6=.ea/local/research/long-term/2026-05-17-c09-blitz-opencode-schema-verify.md (15 tables; drizzle fingerprint guard)`
- `blitz-r7=.ea/local/research/long-term/2026-05-17-c09-blitz-gha-wallclock-variance.md (per-OS thresholds Linux 10 / macOS 20 / Windows 15; per-OS baselines)`
- `blitz-r8=.ea/local/research/long-term/2026-05-17-c09-blitz-presidio-install-footprint.md (157 deps + 750 MB model → extras-gated)`
- `blitz-r9=.ea/local/research/long-term/2026-05-17-c09-blitz-snapshot-tooling-churn.md (keep custom; 110 goldens, 41 commits/90d)`
- `blitz-r10=.ea/local/research/long-term/2026-05-17-c09-blitz-codex-schema-verify.md (Codex JSONL shape ✓; adapter 100-150 LOC)`
- `blitz-r11=.ea/local/research/long-term/2026-05-17-c09-blitz-hook-wallclock.md (current 4.89s --all-files; conditional-skip helper required)`
- `blitz-r12=.ea/local/research/long-term/2026-05-17-c09-blitz-pricing-currency.md (PRICING dict stale; refresh at Q3-W01 + weekly CI gate)`
- `ratified_2026-05-17_via_AUQ=status:accepted; R1:commit B12-snapshot numbers now (§5.9.6.1); R2:windows ±15% provisional + Q2-W05 revisit wave; R3+R4: not selected — defaults stand (LEGACY_FREE_TEXT retained sentinel, Q1-W00 ships)`
- `telemetry_prototype_audit_source_commit=main@2026-04-30T05:25:25Z (upstream telemetry prototype; read-only source access granted by the operator)`
- `vendored_ideas=one-pass-record-materialisation+retry-window-sliding-dict, end-marker-classifier-order, file_meta-last_offset-incremental-scan, per-token-pricing-dict-longest-prefix-match, share-artifact-detail-vs-aggregate-split (all credited to [22][23])`
- `dependent_clusters_read=C00,C01,C02,C03,C04,C05,C06,C07a,C07b,C08`
- `operator_verdicts_pending=Q1..Q10 (AskUserQuestion round on cluster ratification)`

## 11. Scrub

- status: clean
- references: repo-relative or external URL only
- local paths: none
- real emails: none in body (the canonical author email is referenced only via the pre-work memo + upstream `pyproject.toml authors` canonical author block; the brief does not interpolate the literal)
- abstract placeholder names: not applicable (no mockup repo names in this brief)
