# Migration DAG — global per-file write-path + per-schema_version ordering (G10 deliverable)

**Created:** 2026-05-18
**Closes:** G10 (migration DAG written), XB06 (migration DAG missing).

## 1. Purpose

C01-C11 all define migrations, but no global dependency DAG exists. Each cluster assumed other contracts were already stable. Result: roadmap waves could start in impossible order.

Per Q1 supersede + audit XB06: one global migration DAG enumerates every per-file write-path migration + every per-schema_version bump + the ordering dependencies between them.

## 2. Per-file write-path migrations (Q1 → daemon-sole-writer)

In order — earlier rows MUST complete before later rows.

| Order | File | From | To | Owning impl wave |
|------:|------|------|------|------------------|
| 1 | `.ea/state.json` | `eawf.state.writer.atomic_write_json_locked` via state-CLI | daemon `state.mutate` RPC | C02-IMPL W09 |
| 2 | `.ea/store/event.jsonl` | `eawf.store.append.append_envelope` | daemon-internal event emitter | C02-IMPL W09 |
| 3 | `.ea/store/audit.jsonl` | `eawf.store.append.append_envelope` | daemon-internal audit emitter | C02-IMPL W09 |
| 4 | `.ea/store/<other>.jsonl` (memory, research, decision, incident, estimate, actual, flow, <role>_report) | `eawf.store.append.append_envelope` | daemon-internal emitters | C02-IMPL W09 |
| 5 | `.ea/config.yaml` (repo layer) | `eawf.cli.commands.config._save_value_to_layer` | daemon `config.set_layer_value` RPC | C02-IMPL W10 + C08-IMPL W01 |
| 6 | `<local-path>` (user layer) | `eawf.cli.commands.config._save_value_to_layer` | daemon `config.set_layer_value` RPC | C02-IMPL W10 + C08-IMPL W01 |
| 7 | `.ea/branches/<branch>.yaml` (new branch layer) | (new in C08) | daemon `config.set_layer_value` RPC | C08-IMPL W01 |
| 8 | `<local-path>` | `eawf.cli.commands.repo._persist_registry` | daemon `registry.update` RPC | C02-IMPL W10 + C07b-IMPL W03 |
| 9 | `.ea/specs/<phase>/[<iter>/]<wave\|spec>.md` (new in C03) | (new) | daemon spec writer | C03-IMPL W03 |
| 10 | Daemon spec cache | (new — daemon-resident) | daemon-internal cache | C03-IMPL W03 |
| 11 | `<local-path>` (PID + socket + WAL) | (new — daemon-managed) | daemon-internal | C02-IMPL W01 + W03 |
| 12 | `<local-path>` | (new — telemetry projector) | daemon-internal telemetry subsystem | C09-IMPL W03 |
| 13 | OS keyring (integration secrets) | `eawf.integrations.secrets` | daemon-mediated keyring access | C11-IMPL W03 |
| 14 | `.claude/`, `.codex/`, `.opencode/` per-runtime trees | `eawf.runtimes.<runtime>.plugin_install` | daemon `plugin.sync` RPC | C07a-IMPL W02 |

**Ordering invariant.** Steps 1-4 (state.json + per-kind JSONL) MUST land first — they are the canonical data surface. Steps 5-7 (layered config) land second (depend on state for `config.schema_version` field). Step 8 (registry) lands next (depends on layered config for `core.runtime_dir` etc.). Steps 9-10 (specs + cache) land next (C03 hard-precondition on URN_KINDS expansion in C01-IMPL W01). Steps 11-14 (daemon runtime + telemetry + keyring + plugins) land last (depend on daemon scaffolding).

## 3. Per-schema_version bumps

Per BOT-03 + Q5: lock `schema_version: Literal["1.0"]` (string MAJOR.MINOR) project-wide. Migration affects 4 surfaces:

| Surface | Pre-Stage-0 literal | Post-Stage-0 literal | Migration |
|---------|---------------------|----------------------|-----------|
| `State.schema_version` | `"1.0"` | `"1.0"` (unchanged — already canonical form) | none |
| Spec models (PhaseSpec / IterSpec / WaveSpec / etc.) | `Literal[1]` (integer) | `Literal["1.0"]` (string) | C03-IMPL W04 — backfill |
| `ConfigBody.schema_version` | `Literal["2"]` (string MAJOR-only) | `Literal["1.0"]` | C08-IMPL W04 — re-baseline (config-schema migrator handles) |
| `PluginManifest.schema_version` | `Literal["1"]` (string MAJOR-only) | `Literal["1.0"]` | C07a-IMPL W02 — `eawf plugin sync` regenerates |
| Daemon protocol | `"eawfd-rpc/3.0"` (composite) | `"eawfd-rpc/3.0"` (unchanged — composite, not a Literal["1.0"]) | none |
| Event envelope (per C07b canonical Event model) | `Literal["1.0"]` | `Literal["1.0"]` (unchanged) | none |
| Pricing model | separate `pricing_version` field | unchanged (independent of schema_version) | none |

**Pre-commit lint.** `eawf.lint.schema_version_literal_lint` rejects any `schema_version: Literal[<X>]` where `<X>` is not a string in MAJOR.MINOR form. Lands as a polish-sweep wave during v0.3 ship.

## 4. Per-entity schema migrations (data-shape evolution)

| Entity | Migration | Owning wave |
|--------|-----------|-------------|
| `EventPayload.actor` | Keep `actor: str` for v0.3-v0.5 backward compat; add `actor_principal_id: str | None = None` placeholder per Q3 / XB08 | C01-IMPL W02 |
| `Cost.attributed_to` | Add `attributed_to: Literal["cli"] = "cli"` placeholder per Q3 / XB08 | C01-IMPL W02 |
| `SessionAttempt.session_log_path: str` | Rename → `session_log_handle: str` (opaque per XB05 / Q1); blob-URN or daemon-side key. Migration writer reads existing rows, computes handle, persists. | C02-IMPL W07 |
| `Wave.commit: str | None` | Drop field per Q11 / BOT-07. Replaced by `eawf wave show --commit <wave-id>` which walks `git log --grep '[P##-W##]'`. | v0.4 hygiene wave |
| `flow.jsonl` rows | Bump `schema_version: 1` → `"1.0"` (string format per Q5) | v0.4 hygiene wave |
| Spec rows (PhaseSpec/IterSpec/WaveSpec) | Bump `schema_version: Literal[1] = 1` → `Literal["1.0"] = "1.0"` per Q5 | C03-IMPL W04 |
| `ConfigBody` rows | Re-baseline from `Literal["2"]` to `Literal["1.0"]` (config-schema migrator handles) | C08-IMPL W04 |

## 5. Per-cluster migration sequencing rules

- **C03 cannot ratify until C01-IMPL W01 ships** (URN_KINDS expansion is hard precondition per XB15).
- **C07a cannot ratify SDK-primary until 2026-06-15 capability probe re-runs** (per XB04 / G7 forecast gate).
- **C06 cannot ratify until C02 event-subscribe RPC ships AND C07b canonical Event model lands** (per Q14).
- **C11 webhook listener stays gated until v0.6+** (per Q15 / XB23 — local polling for v0.3-v0.5).
- **`Wave.commit` drop lands as v0.4 hygiene wave** (per Q11 — git-log walk replaces field).
- **State-CLI / layered-config writer / registry writer migration into daemon internals lands during C02-IMPL W09 + W10** (per Q1 / authority-map).

## 6. Acceptance criteria (G10 close)

- [x] Global migration DAG enumerated (this brief).
- [ ] Per-cluster migration plans cross-reference this DAG (deferred to per-cluster R-implementation specs).
- [ ] Migration commit prefix lint enforces `[P##-W##]` per migrated file (deferred to C03 / C09 hooks).

## 7. References

[1] `.ea/local/research/long-term/2026-05-17-spec-series-combined-audit.md` §XB06 + §BOT-03 + §"Operator decisions 2026-05-18"
[2] `.ea/local/research/long-term/2026-05-18-authority-map.md` — Q1 deliverable
[3] All 13 cluster briefs + 4 C04 sub-cluster briefs (per-cluster migration plans live in §7 of each brief)

## 8. Provenance

- `store_record=none (local-only)`
- `commit=3b86f7a (parent)`
- `cluster=N/A (migration DAG is a G10 deliverable artifact)`
- `consumes=Q1 + Q5 + Q11 + XB06 + XB15 + BOT-03 + BOT-07`
- `supersedes=none`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md`
- `session=eawf-stage0-migration-dag-2026-05-18`

## 9. Scrub

- status: clean
- references: repo-relative only
- local paths: 0
- real emails: 0
- abstract placeholder names: not applicable
