# Migration

Steps for moving an eawf-managed repo across schema and surface bumps. Run `eawf doctor` after any of them to confirm the state, config and registry are consistent.

## Upgrading a 0.6 tree

A 0.6 tree keeps its records in `.ea/state.json`; 0.7.0 reads it but does not write it, and carries no command that migrates it. The schema-version chain and the epoch-2 cutover shipped last in 0.7.0rc4. Follow `eawf help upgrade-from-0.6`, which installs 0.7.0rc4, cuts the tree over with it and then upgrades to 0.7.0.

## Exit-code surface (v0.3, BREAKING)

The CLI exit-code surface compressed from the legacy 0..9 codes to 0..5. This is a BREAKING change for downstream consumers that pinned numeric codes. Update CI scripts and runners to the new buckets — see `eawf help exit-codes`. Fine-grained legacy distinctions remain available on the error envelope under `data.kind`, so scripts that need a specific failure mode pivot on `data.kind` instead of the retired numeric code.

## Profile-schema bumps

The profile config schema versions independently. A `runtime.adapters: list[str]` field supersedes the older `runtime.kind` scalar (kept as a deprecated alias mapped onto the first element). Re-run `eawf profile validate` after editing a profile, and re-render so the composed `AGENTS.md` and plugin trees pick up the change.

## Plugin sync

After upgrading eawf, re-emit the harness plugin trees so the Claude / Codex / OpenCode adapters match the new CLI surface:

```text
eawf plugin sync        # re-render plugin trees from the current surface
eawf plugin doctor      # verify no drift between source and rendered trees
```

## Daemon protocol bumps

When the CLI and daemon disagree on protocol version the CLI exits 1 USER_ERROR with `data.kind="ProtocolMismatch"` plus both versions. Upgrade with `uv tool upgrade eawf`, then `eawf daemon stop` so the next call spawns a daemon at the matching version.

See `eawf help daemon` for daemon lifecycle and `eawf help profiles` for the profile system.
