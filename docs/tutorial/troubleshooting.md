# Troubleshooting

Start with `eawf doctor`. It runs install-readiness probes, validates core workspace assumptions, and appends the drift checks described in the [doctor reference](../reference/doctor.md). Use `--json` when a script needs stable fields instead of table text.

```text
eawf doctor
eawf --json doctor
```

If the CLI exits non-zero, map the exit bucket in [exit-codes.md](../reference/exit-codes.md), then use the more precise cause in [error-codes.md](../reference/error-codes.md).

## Daemon unreachable

`4 DAEMON_UNREACHABLE` means the CLI could not talk to `eawfd`, the daemon is shutting down, or the socket / pipe belongs to a mismatched daemon version.

```text
eawf daemon status
eawf daemon start
eawf doctor --reprobe
```

If the error mentions protocol skew, restart the daemon after upgrading so the CLI and daemon negotiate the same protocol version. See [daemon.md](../help/daemon.md) for the daemon lifecycle and log commands.

## State or schema validation failed

`2 VALIDATION_ERROR` usually means strict Pydantic validation rejected a state, config, registry, or artifact payload. Do not edit `.ea/state.json` by hand. Fix the offending field through the owning command, then re-run:

```text
eawf validate
eawf doctor
```

The error envelope names the failing field or invariant. If an operator-facing command accepts the same id or flag, prefer that command so the daemon remains the canonical writer.

## Plugin drift or duplicate plugin scope

`PLUGIN_DRIFT_DETECTED` means a rendered plugin tree no longer matches the manifest that records managed file hashes. Inspect and repair with:

```text
eawf plugin doctor --strict
eawf plugin sync
eawf plugin doctor --strict
```

If `eawf doctor` reports `plugin_cross_scope_dup`, the same generated plugin region appears under more than one install scope. Pick the intended scope, uninstall or overwrite the other install through the plugin command, then re-run `eawf doctor`.

## Stale repo registry entries

When the repo registry contains entries whose paths no longer exist, run the [explicit prune verb](../reference/doctor.md#prune-verbs). The pruner is staged: it reports candidates first and writes only after confirmation.

```text
eawf repo prune
eawf repo prune --yes --no-input
```

Use the non-interactive form only after reviewing the candidates in a local run or CI log. The prune removes registry rows; it does not delete repos.

## Generated-doc drift

Auto-generated reference pages under `docs/reference/autogen/` are render owned. If a docs check reports drift, regenerate or verify from the CLI instead of hand-editing those files:

```text
eawf doc verify --strict
```

Hand-written pages can link to generated pages, but generated pages must keep matching the live source tree.
