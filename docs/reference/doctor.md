# Doctor

`eawf doctor` is the first diagnostic command for an eawf workspace. It runs
install-readiness checks, state / config probes, the repo project-record check,
and drift checks that compare state and generated artifacts with the current
workspace.

```text
eawf doctor
eawf doctor --reprobe
eawf --json doctor
```

Text output is for operators. JSON output is for scripts and includes a
top-level `checks` list with stable `name`, `status`, and `detail` fields.

## Options

| Option | Use |
|---|---|
| `--reprobe` | Clear the cached instrument probe and re-run checks. |
| `--user-scope` | Add a check for a user-scope eawf install and version drift. |
| `--runtime <id>` | Run capability-matrix drift for one runtime: `claude-code`, `codex`, or `opencode`. |
| `--json` | Emit a machine-readable envelope. Same intent as root `--json`. |

## Reading results

Doctor rows are checks, not repairs. `OK` rows mean the probe reconciled with
the current workspace. `WARN` rows mean the command stayed useful but found
operator work. Hard failures use the standard error envelope and exit-code
surface documented in [exit-codes.md](exit-codes.md) and
[error-codes.md](error-codes.md).

The normal workflow is:

```text
eawf doctor
eawf --json doctor        # inspect full structured rows when needed
eawf doctor --reprobe     # after installing tools or changing runtime setup
```

## Git/state drift reconciler

The `git_state_drift` row reconciles lifecycle state with git history. It
loads `.ea/state.json`, walks CLOSED waves, and compares each wave's pinned
`Wave.commit` with the commit derived from the wave's bracketed commit prefix.
The derived commit comes from git history, so cherry-picked commits remain
discoverable when their subject prefix is intact.

`status="ok"` means every CLOSED wave reconciles, or state is absent /
unparseable and another doctor row owns that failure. `status="warn"` means
the row includes one or more drift records.

JSON shape:

```json
{
  "name": "git_state_drift",
  "status": "warn",
  "detail": "1 drift(s): P28-I03-W24=closed_no_pin",
  "drifts": [
    {
      "wave_id": "P28-I03-W24",
      "kind": "closed_no_pin",
      "state_commit": null,
      "git_commit": null
    }
  ]
}
```

Drift kinds:

| Kind | Meaning |
|---|---|
| `pinned_but_missing` | State has `Wave.commit`, but git cannot find a matching commit. |
| `pinned_mismatch` | State and git both find a commit, but the SHAs do not match. |
| `closed_no_pin` | A CLOSED wave has no pinned commit and no derivable commit prefix in git history. |
| `closed_unfindable` | Git is unavailable, so the check cannot decide whether history reconciles. |

When the reconciler warns, `eawf doctor` also emits a bounded
`git_state_drift_detected` event for downstream telemetry. Use
`eawf --json doctor` to see every drift row; the text table summarizes only
the first few rows.

## Plugin cross-scope duplicates

The `plugin_cross_scope_dup` row inspects `.ea/indexes/generated.json` and
groups generated entries by `region_id`. A warning means the same generated
plugin region appears under more than one scope, commonly project and user.
Runtime precedence is intentionally not guessed by doctor; pick one scope and
repair through the plugin command.

```text
eawf plugin doctor --strict
eawf plugin sync
eawf doctor
```

## Runtime capability drift

`eawf doctor --runtime <id>` bypasses the general check set and reports
capability-matrix drift for one runtime adapter. Use it when dispatch behavior
does not match the runtime surface you expect.

```text
eawf doctor --runtime codex
eawf doctor --runtime claude-code
eawf doctor --runtime opencode
```

Rows marked `DRIFT` mean declared capabilities and observed probe flags
disagree. `MISSING` means the runtime is not installed or not discoverable by
the probe.

## Prune verbs

Eawf uses explicit prune verbs for stale operator-owned records. A prune
command should report what it will drop and require an explicit confirmation
path before it writes.

Common prune surfaces:

| Command | Effect |
|---|---|
| `eawf repo prune` | Drops registry entries whose on-disk paths no longer exist. Does not delete repos. |
| `eawf memory prune` | Soft-deletes matching memory rows by flipping status to `PRUNED`. |
| `eawf backup prune --keep <N>` | Keeps the newest backup snapshots and removes older snapshots. |

When troubleshooting, prefer prune verbs over manual JSON edits because they
preserve schema validation, event emission, and confirmation gates.

## Related pages

- [Troubleshooting](../tutorial/troubleshooting.md)
- [Daemon help](../help/daemon.md)
- [Migration help](../help/migration.md)
- [Auto-generated CLI reference](autogen/cli.md#eawf-doctor)
