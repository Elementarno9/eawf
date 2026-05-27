# Troubleshooting

Start with `eawf doctor`. It runs install-readiness probes, validates core
workspace assumptions, and appends the drift checks described in the
[doctor reference](../reference/doctor.md). Use `--json` when a script needs
stable fields instead of table text.

```text
eawf doctor
eawf --json doctor
```

If the CLI exits non-zero, map the exit bucket in
[exit-codes.md](../reference/exit-codes.md), then use the more precise cause
in [error-codes.md](../reference/error-codes.md).

## Daemon unreachable

`4 DAEMON_UNREACHABLE` means the CLI could not talk to `eawfd`, the daemon is
shutting down, or the socket / pipe belongs to a mismatched daemon version.

```text
eawf daemon status
eawf daemon start
eawf doctor --reprobe
```

If the error mentions protocol skew, restart the daemon after upgrading so the
CLI and daemon negotiate the same protocol version. See
[daemon.md](../help/daemon.md) for the daemon lifecycle and log commands.

## State or schema validation failed

`2 VALIDATION_ERROR` usually means strict Pydantic validation rejected a
state, config, registry, or artifact payload. Do not edit `.ea/state.json` by
hand. Fix the offending field through the owning command, then re-run:

```text
eawf validate
eawf doctor
```

The error envelope names the failing field or invariant. If an operator-facing
command accepts the same id or flag, prefer that command so the daemon remains
the canonical writer.

## Git/state drift

When `eawf doctor` reports `git_state_drift`, the
[drift reconciler](../reference/doctor.md#gitstate-drift-reconciler) compared
CLOSED waves in state with commits discoverable from git history and found a
mismatch.

Common rows:

| Kind | Meaning | Next step |
|---|---|---|
| `pinned_but_missing` | `Wave.commit` is set, but no matching commit is reachable from git history. | Fetch missing refs, inspect history, then close or repair the wave commit pointer through the lifecycle command. |
| `pinned_mismatch` | State and git both find a commit, but the SHAs differ. | Check whether a rebase or cherry-pick changed the wave commit; re-pin the wave if the new commit is the accepted one. |
| `closed_no_pin` | A CLOSED wave has no pinned commit and no commit subject with the wave prefix. | Find the intended close commit, then pin it through `eawf wave close --commit <ref>` when reopening / repairing the lifecycle state is appropriate. |
| `closed_unfindable` | Git was unavailable, so the check could not decide. | Re-run with `git` available before changing state. |

Use `eawf --json doctor` for the full `checks[].drifts[]` list. The table
view caps detail to keep terminal output readable.

## Plugin drift or duplicate plugin scope

`PLUGIN_DRIFT_DETECTED` means a rendered plugin tree no longer matches the
manifest that records managed file hashes. Inspect and repair with:

```text
eawf plugin doctor --strict
eawf plugin sync
eawf plugin doctor --strict
```

If `eawf doctor` reports `plugin_cross_scope_dup`, the same generated plugin
region appears under more than one install scope. Pick the intended scope,
uninstall or overwrite the other install through the plugin command, then
re-run `eawf doctor`.

## Stale repo registry entries

When the repo registry contains entries whose paths no longer exist, run the
[explicit prune verb](../reference/doctor.md#prune-verbs). The pruner is
staged: it reports candidates first and writes only after confirmation.

```text
eawf repo prune
eawf repo prune --yes --no-input
```

Use the non-interactive form only after reviewing the candidates in a local
run or CI log. The prune removes registry rows; it does not delete repos.

## Generated-doc drift

Auto-generated reference pages under `docs/reference/autogen/` are render
owned. If a docs check reports drift, regenerate or verify from the CLI
instead of hand-editing those files:

```text
eawf doc verify --strict
```

Hand-written pages can link to generated pages, but generated pages must keep
matching the live source tree.
