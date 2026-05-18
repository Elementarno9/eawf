# Authority map — per-file canonical writer (Q1 deliverable)

**Created:** 2026-05-18
**Closes:** G2 (writer authority map ratified), XB01 (canonical writer authority conflict), Stage-0 TO-DOs 0-04..0-19.
**Operator decision Q1 (2026-05-18):** SUPERSEDE AGENTS rules 4 + 17 — daemon = sole canonical mutator. The three legacy writers (state-CLI, layered-config writer, registry writer) migrate into daemon internals.

## 1. Purpose

Single page naming, for **every** file in the eawf stateful surface, the canonical writer and the operation type. Resolves XB01 (canonical writer authority conflict) by committing to path (b) — supersede the three-writer rule with a daemon-sole-mutator invariant. AGENTS rules 4 + 17 reframe accordingly (commit text lands as `[P20-CORE] docs:` after this brief ratifies).

## 2. Per-file authority table

| File / surface | Path | Pre-Q1 canonical writer | **Post-Q1 canonical writer** | Operations | Migration phase |
|---|---|---|---|---|---|
| State document | `.ea/state.json` | state-CLI (`uv run eawf state ...`) + `eawf.state.writer` via portalocker | **daemon (`eawfd`) — sole writer** | Read/write all state mutations | v0.4 hygiene wave: migrate state-CLI to call daemon RPC `state.mutate` |
| Event store | `.ea/store/event.jsonl` | `eawf.store.append.append_envelope` | **daemon — sole writer** | append-only emit | v0.4 hygiene wave |
| Audit store | `.ea/store/audit.jsonl` | `eawf.store.append.append_envelope` | **daemon — sole writer** | append-only emit | v0.4 hygiene wave |
| All other per-kind JSONL under `.ea/store/` | `.ea/store/*.jsonl` | `eawf.store.append.append_envelope` | **daemon — sole writer** | append-only emit | v0.4 hygiene wave |
| Layered config (per-repo) | `.ea/config.yaml` | `eawf.cli.commands.config._save_value_to_layer` | **daemon — sole writer** | Read/write per-layer YAML | v0.4 hygiene wave |
| Layered config (per-user) | `<local-path>` | `eawf.cli.commands.config._save_value_to_layer` | **daemon — sole writer** | Read/write per-layer YAML | v0.4 hygiene wave |
| Layered config (branch) | `.ea/branches/<branch>.yaml` | (new in C08) | **daemon — sole writer** | Read/write per-branch YAML | v0.4 hygiene wave |
| Registry | `<local-path>` | `eawf.cli.commands.repo._persist_registry` | **daemon — sole writer** | Add/remove/update repos | v0.4 hygiene wave |
| Spec files | `.ea/specs/<phase>/[<iter>/]<wave\|spec>.md` | (new in C03) | **daemon — sole writer** | Spec init/promote/archive | C03 implementation phase |
| Spec cache (daemon-resident) | `<local-path>` | (new — daemon-internal) | **daemon — sole writer** | Cache hydrate/invalidate | C03 implementation phase |
| Daemon metadata | `<local-path>` + `eawfd.sock` + WAL dir | daemon | **daemon — sole writer** | PID + socket + outcome-WAL records | C02 implementation phase |
| WAL records | `<local-path>` | daemon | **daemon — sole writer** | Outcome-WAL per XB12 / Q10 | C02 implementation phase |
| Telemetry DB | `<local-path>` | (new in C09 — telemetry projector) | **daemon (telemetry projector subsystem) — sole writer** | SQLite projection of event store | C09 implementation phase |
| Integration secrets (keyring) | OS keyring backend | `eawf.integrations.secrets` | **daemon — sole writer (via keyring API)** | Set / rotate / verify | C11 implementation phase |
| Worktree records | `state.json:Wave.worktrees` (state-resident) + `.git/worktrees/` (git-managed) | state-CLI / `git worktree` | **daemon (state writes) + git (.git/worktrees managed by git itself)** | Add / remove / cherry-pick | C07b implementation phase |
| AgentReport JSONL | `.ea/store/<role>_report.jsonl` | `eawf.store.append.append_envelope` | **daemon — sole writer** | append-only per AGENTS rule 19 | v0.4 hygiene wave |
| Memory store | `.ea/store/memory.jsonl` | `eawf.store.append.append_envelope` | **daemon — sole writer** | append-only | v0.4 hygiene wave |
| Plugin install output | `.claude/`, `.codex/`, `.opencode/` per-runtime tree | `eawf.runtimes.<runtime>.plugin_install` | **daemon — sole writer** | Plugin sync / install / regenerate | C07a implementation phase |

## 3. Reader exemption (V1 daemonless reader)

Per V1, reads MAY bypass daemon in three contexts:

1. **CI environments** without long-running processes
2. **Read-only one-shot CLI calls** (`eawf state show`, `eawf wave list`, `eawf validate`)
3. **Recovery shell** when daemon broken or version-skewed

Daemonless reader path: direct file IO + Pydantic load. Never mutates.

## 4. Migration mechanism

Per Q1 supersede, the three legacy writers migrate into daemon internals across the v0.4 hygiene wave. The full migration DAG lives in `.ea/local/research/long-term/2026-05-18-migration-dag.md`.

Per-writer migration approach:

- **state-CLI → daemon proxy.** `uv run eawf state mutate-* ...` becomes a CLI wrapper that calls `state.mutate` RPC. Pre-flight refuses if daemon unreachable AND the operation is not in the reader-exempt set.
- **Layered-config writer → daemon proxy.** `_save_value_to_layer` becomes `daemon.config.set_layer_value` RPC. Same pre-flight rule.
- **Registry writer → daemon proxy.** `_persist_registry` becomes `daemon.registry.update` RPC. Same pre-flight rule.
- **Telemetry projector — internal subsystem.** `src/eawf/telemetry/projector.py` runs inside daemon process; never as separate process.

## 5. Backward-compatibility window

v0.3-v0.5: legacy callers may continue to call the three writers directly **only via the new daemon-proxy wrappers**. Pre-flight enforcement: if daemon unreachable and operation is mutating, fail-fast per AGENTS rule 8 with `repair_commands=["eawf daemon start"]`. Once v0.5 lands, legacy writer modules removed from public surface (kept as daemon-internal modules; not importable from user code).

## 6. Acceptance criteria (G2 close)

- [x] One-page authority map written (this brief).
- [ ] AGENTS rules 4 + 17 rewritten in `AGENTS.md` (deferred to post-ratification `[P20-CORE] docs:` commit).
- [ ] Decision row `D-G2-AUTHORITY-MAP` recorded in `state.json` (deferred to post-ratification `[P20-CORE] state:` commit).

## 7. References

[1] `.ea/local/research/long-term/2026-05-17-spec-series-combined-audit.md` §XB01 + §"Operator decisions 2026-05-18" Q1
[2] `.ea/local/research/long-term/2026-05-18-migration-dag.md` — migration ordering
[3] `AGENTS.md` rules 4 + 17 (to be amended post-ratification)
[4] C02 `2026-05-16-c02-daemon-topology.md` — daemon implementation spec
[5] C08 `2026-05-16-c08-configurability-profiles.md` D13 — config writer migration
[6] C10 `2026-05-17-c10-operations.md` — migration phase plan

## 8. Provenance

- `store_record=none (local-only)`
- `commit=3b86f7a (parent)`
- `cluster=N/A (authority-map brief is a Q1 deliverable artifact)`
- `consumes=Q1 operator decision (2026-05-18)`
- `supersedes=AGENTS rules 4 + 17 (legacy three-writer canon; superseded 2026-05-18 per Q1)`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md`
- `session=eawf-stage0-authority-map-2026-05-18`

## 9. Scrub

- status: clean
- references: repo-relative only
- local paths: 0
- real emails: 0
- abstract placeholder names: not applicable
