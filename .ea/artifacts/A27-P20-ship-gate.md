# A27-P20 ship-gate audit

## Summary

- P20 iter `P20-I01` executed 13 closed feature/chore waves at audit time (W01..W13 + W15); W14 (AGENTS.md spike-workflow + skill prompt updates) remains pending by design and will land before phase close [1].
- Each closed wave's recorded `success_criteria` cleared against the landed commit's outcome string; no wave landed with a delta that would invalidate its declared criteria [1].
- W01 ship: `roadmap show` lifted to a `rich.table.Table` renderer with stale-row muted annotation; 7 new integration tests; gauntlet clean [2].
- W02 ship: repo-scope quadrant TUI on `rich.live` + `rich.layout`; `Eä` brand outside-left of breadcrumb, 2×2 quadrant, header+body+footer frame, offline + online tick modes; 65 new tests; full suite 2632 pass [3].
- W03 ship: wave-board view (list + drill detail) with typed DAG-edge consumer, operator-priority sort, 5-mode filter cycle, `b`-key dispatch; 62 new tests; 2760 pass [4].
- W04 ship: 5 detail overlays (hypothesis / decision / memory / events / dispatch) via single `open_overlay` dispatch; verb-prefixed keymap `oH` / `oD` / `oM` / `oE` / `oR`; 5 golden snapshots; 45 wave-specific tests [5].
- W05 ship: workspace dashboard with read-only registry helper + typed `Registry`, top-strip multi-repo, active-repo quadrant reuse, stale chips on 3 signals; 85 wave tests; 2845 pass [6].
- W06 ship: portfolio dashboard `rich.table` + explicit `eawf repo {add,remove,prune}` CLI verbs (no scan/walk); 3 goldens; 111 wave tests; 3070 pass [7].
- W07 ship: audit-running overlay with remediation hints, read-only `AuditAttachment` (pid + harness_adapter + session), v0.4-deferred action menu; 63 wave tests; 3022 pass [8].
- W08 ship: `eawf metrics` CLI; shared rich/plain renderer; typed `MetricsSummary` `schema_version=1`; 22 unit + 4 integration tests [9].
- W09 ship: `weekly_eu_target` field on `Project` (default `None`), `WeeklyBurnMetric` + `compute_weekly_burn` helper, TUI footer divisor when set; 20 tests; 2980 pass [10].
- W10 ship: `questionary` menu + `ConfigKey` metadata registry (498 LoC); 30 unit + 12 integration tests; subagent leak salvaged inline [11].
- W11 ship: TUI config modal with `c` hotkey, tabbed metadata-driven form, type-sized inputs, `s` save through W10 `_save_value_to_layer` helper; 69 wave-specific tests; 2829 pass [12].
- W12 ship: `RegistryOrderedTyperGroup` drives help panel order from `CONFIG_REGISTRY.tabs_sorted()`; stale v0.1 banner scrubbed; golden + unit + integration tests [13].
- W13 ship: this audit + `docs/architecture/tui.md` — TUI surface reference with renderer-owned chassis + dense citations; `eawf artifact validate` passes [14][15].
- W15 ship: typed DAG edges via `eawf.state.wave_graph` (`deps` / `blocks` / `blocked_by` / `edges` / `edges_for_iter`); blocks-rebuild primes typed view; derived design (no model field); 22 unit + 5 integration tests [16].
- Local verification: `uv run pre-commit run --all-files` clean; `uv run mypy src/` clean (278 source files, no issues); `uv run pytest tests/ -q` 3154 passed, 12 deselected — at HEAD `a7029c5` immediately before this audit body landed [17].
- W14 explicitly out of scope for this audit (status=pending; deps=[W13]); the AGENTS.md spike-workflow addendum + `/prep` + `/research` skill prompt updates land in W14 and will gate phase-close.

## References

[1] `.ea/state.json` (waves block: every `P20-I01-W##` status + outcome)
[2] commit `7a8e0b1` — `[P20-W01] feat: rich-table renderer for roadmap show with stale annotation`
[3] commit `3dbf201` — `[P20-W02] feat: repo-scope quadrant TUI on rich.live + rich.layout`
[4] commit `39bc45e` — `[P20-W03] feat: TUI wave-board view (list + drill detail)`
[5] commit `67fd033` — `[P20-W04] feat: detail overlays for hypothesis/decision/memory/events/dispatch`
[6] commit `30abfa0` — `[P20-W05] feat: workspace-scope dashboard + registry reader`
[7] commit `3820a04` — `[P20-W06] feat: user-scope portfolio dashboard + explicit registry mutators`
[8] commit `18d2162` — `[P20-W07] feat: audit-running overlay + failure action menu (read-only)`
[9] commit `fc693de` — `[P20-W08] feat: eawf metrics CLI (EU variance, audit pass, wave elapsed, planned/reactive)`
[10] commit `318553d` — `[P20-W09] feat: weekly_eu_target field on Project + TUI burn divisor`
[11] commit `541da25` — `[P20-W10] feat: questionary menu + metadata registry for eawf config`
[12] commit `321847b` — `[P20-W11] feat: TUI config hotkey + tabbed modal backed by metadata`
[13] commit `03ae041` — `[P20-W12] chore: regroup CLI help panels via metadata registry`
[14] commit `a7029c5` — `[P20-W13] docs: TUI surface reference (docs/architecture/tui.md)`
[15] `docs/architecture/tui.md`
[16] commit `1a9a208` — `[P20-W15] feat: typed Wave DAG edges via eawf.state.wave_graph`
[17] `uv run pytest tests/ -q` at HEAD `a7029c5`: `3154 passed, 12 deselected`; `uv run mypy src/`: `Success: no issues found in 278 source files`; `uv run pre-commit run --all-files`: clean.

## Provenance

- kind: ship-gate
- phase: P20
- iter: P20-I01
- audit_id: A27-P20
- scope_id: P20
- branch: feature/eawf-v0.3-p20 (inline)
- verification: `uv run pre-commit run --all-files`; `uv run mypy src/`; `uv run pytest tests/ -q`; `uv run eawf artifact validate docs/architecture/tui.md`
- waves audited: P20-I01-W01..W13 + P20-I01-W15 (13 closed); P20-I01-W14 explicitly pending (deps=[W13])

## Scrub

- status: clean
- notes: repo-relative paths only; no absolute paths, no host-local URLs, no PII beyond the canonical `Claude <noreply@anthropic.com>` co-author trailer (allowlisted under the secrets-hygiene policy).
