# Epoch-2 cutover guide

The epoch-2 cutover imports a repository's whole epoch-1 `.ea` corpus into a new generation tree, once. Every step below except the apply is read-only against the repository, and the apply only ever writes into a target tree that has declared itself disposable. `eawf help migration` carries the short form.

## 1. Stage the committed corpus

```text
eawf migrate epoch2 --stage-to ../eawf-stage --workspace-key EAWF --project-key EAWF
```

`--stage-to` assembles the snapshot root the other modes read: `document.json` from `.ea/state.json`, `config/base.yaml` from `.ea/config.yaml`, every committed `.ea/store/*.jsonl` ledger, a one-workspace `registry.json` declaring `--workspace-key`, and a neutral `telemetry.json`.

- The bytes come from git objects at HEAD, not from the working tree, so an uncommitted daemon write, a gitignored firehose, or a writer racing the copy cannot reach the snapshot. The envelope names the revision it read.
- Which paths are staged is decided by the `.ea/` commit-policy table: a path must be both tracked at HEAD and declared committed.
- The destination must be empty and must sit outside `.ea/`; either refusal is `migration_staging_refused` (exit `2`).
- A key that is not a bounded uppercase symbol (`^[A-Z][A-Z0-9_-]{1,15}$`) is refused before anything is read, with exit `2 VALIDATION_ERROR` and `symbol_key_invalid` in the message.

## 2. Register the workspace

`--apply` mints a URN for every imported record under `--workspace-key`, so the key must resolve in the machine registry (`~/.eawf/registry.json`) first. A registry written by `eawf init` / `eawf repo add` alone carries an empty `workspaces` map.

```text
eawf workspace add EAWF --home EAWF --title "eawf"
eawf workspace show EAWF
```

The home repo is the project code whose `state.json` anchors the workspace; `--member` adds further project codes. Registering an existing key refuses with `workspace_already_registered`; edit membership with `eawf workspace member add/remove`.

## 3. Plan, then apply

```text
eawf migrate epoch2 --plan --snapshot-root ../eawf-stage --allowlist <file> \
  --workspace-key EAWF --project-key EAWF --repository-key <key> --default-track-key TRK-EAWF-CORE
eawf migrate epoch2 --apply --plan-digest <approval digest> --target-root <canary> ...
```

The plan is read-only and reports the approval digest the apply must be given. The apply re-plans under its authority locks and refuses a digest that no longer matches, a target that has not declared itself a disposable canary, and an unregistered workspace (`workspace_not_registered`). `--registry-path` names a registry other than the machine one, which is how a rehearsal on a pinned clone supplies its own.

`--recover` and `--rollback` repair an apply that stopped part way; they read the restore point inside `--target-root` and take no snapshot.
