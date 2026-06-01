# Migration

Per-cluster migration steps for moving an eawf-managed repo across schema
and surface bumps. Run `eawf doctor` after any migration to confirm the
state / config / registry are consistent.

## State-version bumps

`state.json` carries a `schema_version`. When a release bumps it, the daemon
migrates the on-disk state on first write and re-stamps the version. To
migrate explicitly:

```text
eawf daemon start       # ensure the canonical mutator is running
eawf validate           # confirm the current state validates
eawf state digest       # record the post-migration digest
```

If a migration validator rejects the state (`2 VALIDATION_ERROR`), the
violation list names the offending fields — fix them through the owning
domain verb, never by hand-editing `state.json`.

### v0.5.0: `schema_version` 1.1 -> 1.2

v0.5.0 bumped `state.json` `schema_version` from `1.1` to `1.2`. The 1.1 -> 1.2 step backfills the additive `Iter.trigger` field (an idempotent `setdefault("trigger", "none")` on each iter row) so the planned-vs-reactive metric classifies waves by intent instead of the `I##` id suffix. Backfilled historical iters carry `trigger="none"` and drop out of the reactive-share denominator. The step is a lossless round-trip and re-running it is a no-op. Migrate explicitly with:

```text
eawf migrate            # auto-detect from + to; run the chain
eawf migrate status     # show current schema_version + chain
```

### v0.5.0: `schema_version` 1.2 -> 1.3

v0.5.0 also bumped `state.json` `schema_version` from `1.2` to `1.3`. The 1.2 -> 1.3 step adds the additive `Wave.claimed_at` work-start timestamp and backfills each wave's value from its `wave_claimed` event in the sibling event store (the latest claim event per wave wins). Before this bump the elapsed-clock consumers (the TUI roadmap time-burn bar and the daemon wave-elapsed publisher) anchored on `opened_at`, which is plan/creation time, so a wave planned hours before it was claimed inflated its elapsed clock; both now anchor on `claimed_at` and render no clock at all while it is unset. A wave with no `wave_claimed` event (never claimed, or its events were pruned) keeps `claimed_at` unset. The backfill is an idempotent `setdefault` that never overwrites an existing value, and a state written before the bump re-loads unchanged because the field is optional. Migrate explicitly with:

```text
eawf migrate            # auto-detect from + to; run the chain
eawf migrate status     # show current schema_version + chain
```

## Exit-code surface (v0.3, BREAKING)

The CLI exit-code surface compressed from the legacy 0..9 codes to 0..5.
This is a BREAKING change for downstream consumers that pinned numeric
codes. Update CI scripts and runners to the new buckets — see
`eawf help exit-codes`. Fine-grained legacy distinctions remain available on
the error envelope under `data.kind`, so scripts that need a specific
failure mode pivot on `data.kind` instead of the retired numeric code.

## Profile-schema bumps

The profile config schema versions independently. A
`runtime.adapters: list[str]` field supersedes the older `runtime.kind`
scalar (kept as a deprecated alias mapped onto the first element). Re-run
`eawf profile validate` after editing a profile, and re-render so the
composed `AGENTS.md` and plugin trees pick up the change.

## Plugin sync

After upgrading eawf, re-emit the harness plugin trees so the Claude /
Codex / OpenCode adapters match the new CLI surface:

```text
eawf plugin sync        # re-render plugin trees from the current surface
eawf plugin doctor      # verify no drift between source and rendered trees
```

## Daemon protocol bumps

When the CLI and daemon disagree on protocol version the CLI exits 1
USER_ERROR with `data.kind="ProtocolMismatch"` plus both versions. Upgrade
with `uv tool upgrade eawf`, then `eawf daemon stop` so the next call
spawns a daemon at the matching version.

See `eawf help daemon` for daemon lifecycle and `eawf help profiles` for the
profile system.
