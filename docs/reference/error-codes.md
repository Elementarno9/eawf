# Eä error codes

Cause-level error vocabulary emitted by the `eawf` CLI. The members live in
`src/eawf/cli/error_codes.py` (:class:`ErrorCode`); the five-bucket exit
codes they fold onto live in `src/eawf/cli/exit_codes.py` and are documented
in [exit-codes.md](exit-codes.md).

`ErrorCode` layers cause-level precision *over* the exit-code surface
without growing it. The process always exits with one of the stable five
buckets (`USER_ERROR`, `VALIDATION_ERROR`, `STATE_CONFLICT`,
`DAEMON_UNREACHABLE`, `INTERNAL_ERROR`); the cause-level code names the
precise failure mode so operators and CI can pivot on it. When an error
carries a code, the text envelope appends a `See <code>` line whose anchor
points back to this page.

Every `ErrorCode` member has exactly one anchor here. The
`tests/unit/test_error_codes.py` anchor-coverage test enforces that no
member ships without its anchor.

## Schema / state

### STATE_VALIDATION_FAILED

Strict invariant validation rejected the candidate `state.json`. Bucket:
`VALIDATION_ERROR (2)`. Inspect the violation list, then fix the offending
field and retry.

### STATE_VERSION_MISMATCH

The on-disk `state.json` schema version does not match the version the
running binary expects. Bucket: `STATE_CONFLICT (3)`. Run `eawf migrate` to
bring the state file forward (or upgrade/downgrade the binary to match).

### BACKUP_WRITE_FAILED

Writing a pre-mutation backup file failed (disk full, permission denied).
Bucket: `INTERNAL_ERROR (5)`. Free space or fix permissions on the backup
directory, then retry the mutating verb.

### MIGRATION_STEP_FAILED

A migration step raised mid-chain. Bucket: `INTERNAL_ERROR (5)`. Resume from
the last committed step with `eawf migrate --resume <from-version>` after
addressing the underlying failure.

### MIGRATION_POSTCONDITION_FAILED

A migration step ran but its postcondition assertion did not hold. Bucket:
`VALIDATION_ERROR (2)`. The state was restored from backup; file an issue
with the migration name and the postcondition that failed.

### MIGRATION_TARGET_UNKNOWN

The requested migration target version is not a known migration step.
Bucket: `USER_ERROR (1)`. Run `eawf migrate --list` to see available targets.

## Daemon / IPC

### DAEMON_PROTOCOL_MAJOR_SKEW

The daemon and CLI disagree on the JSON-RPC protocol major version. Bucket:
`DAEMON_UNREACHABLE (4)`. Upgrade both to the same release with
`uv tool upgrade eawf`, then restart the daemon.

### DAEMON_PROTOCOL_MINOR_SKEW

The daemon and CLI differ by a protocol minor version. Bucket:
`DAEMON_UNREACHABLE (4)`. Restart the daemon with `eawf daemon restart` so
both ends negotiate the newer minor.

### DAEMON_SPAWN_FAILED

Auto-spawning the daemon process failed. Bucket: `DAEMON_UNREACHABLE (4)`. Check `eawf daemon logs --tail 200` for the spawn error, then start it manually with `eawf daemon start`.

### DAEMON_LOCK_HELD

A sibling writer holds the daemon mutation lock. Bucket: `STATE_CONFLICT
(3)`. Retry in a moment, or run `eawf doctor` to find the live holder.

### DAEMON_SOCKET_UNREACHABLE

The daemon UDS socket refused the connection or is stale. Bucket: `DAEMON_UNREACHABLE (4)`. Run `eawf daemon restart` then retry; pass `--daemonless` for read-only verbs.

## Scope / lifecycle

### SCOPE_CONFLICT

The requested scope id collides with an existing scope. Bucket: `USER_ERROR
(1)`. Pick a unique id, or run `eawf state resolve` to inspect the existing
scope.

### WAVE_DEPS_NOT_SATISFIED

A wave was claimed before its declared dependency waves were closed. Bucket:
`USER_ERROR (1)`. Close the blocking wave(s) first, or pass
`--out-of-order` to opt out of the gate.

### PHASE_NOT_ACTIVE

The verb requires an ACTIVE phase but the target phase is not active.
Bucket: `USER_ERROR (1)`. Activate it with `eawf phase activate <id>` (or
reopen a CLOSED phase first).

### ITER_NOT_ACTIVE

The verb requires an ACTIVE iter but the target iter is not active. Bucket:
`USER_ERROR (1)`. Open the iter, or target the currently-active one.

### WAVE_OUT_OF_ORDER_REJECTED

A lower-numbered sibling wave is still PENDING with its deps satisfied, so
the claim is rejected by the monotonic-ordering gate. Bucket: `USER_ERROR
(1)`. Claim the lower-numbered wave first, or pass `--out-of-order`.

## Worktree / git

### WORKTREE_DIRTY

The worktree has uncommitted or untracked changes that block the operation.
Bucket: `USER_ERROR (1)`. Commit, stash, or clean the working tree, then
retry.

### WORKTREE_BRANCH_STALE

The feature branch is behind its intended source branch. Bucket:
`USER_ERROR (1)`. Fetch and rebase/fast-forward the branch before starting
new work.

### CHERRY_PICK_CONFLICT

Cherry-picking a worktree commit into the parent branch hit a conflict.
Bucket: `STATE_CONFLICT (3)`. Resolve the conflict in the parent worktree,
continue the cherry-pick, then tear the worktree down.

## Runtime / dispatch

### RUNTIME_AUTH_EXPIRED

The selected agent runtime rejected the request with an expired credential.
Bucket: `USER_ERROR (1)`. Re-authenticate the runtime, then re-dispatch.

### RUNTIME_RATE_LIMIT

The agent runtime returned a rate-limit response. Bucket: `INTERNAL_ERROR
(5)`. Back off and retry, or switch runtime preference with
`eawf config set runtime.preference`.

### RUNTIME_SERVER_ERROR

The agent runtime returned a server-side error. Bucket: `INTERNAL_ERROR
(5)`. Retry; if it persists, check the runtime status page or fall back to
another runtime.

### DISPATCH_BUDGET_EXCEEDED

A dispatch exceeded its configured token or wall-clock budget. Bucket:
`INTERNAL_ERROR (5)`. Raise the budget, narrow the wave scope, or inspect
the partial result before re-dispatching.

### SESSION_LOG_MISSING

The agent session log expected for a dispatch could not be found. Bucket:
`INTERNAL_ERROR (5)`. Confirm the runtime wrote its log, then re-dispatch
the wave.

## Plugin / sync

### PLUGIN_MANIFEST_INVALID

A plugin or skill manifest failed schema validation. Bucket:
`VALIDATION_ERROR (2)`. Fix the manifest fields flagged in the violation
list, then re-run `eawf plugin sync`.

### PLUGIN_DRIFT_DETECTED

The installed plugin tree drifted from its manifest. Bucket: `STATE_CONFLICT
(3)`. Run `eawf plugin doctor --strict` to inspect the drift, then
re-sync.

## Config / profile

### PROFILE_CONFLICT_UNDECLARED

Two enabled profiles set the same field without declaring the conflict.
Bucket: `USER_ERROR (1)`. Declare the precedence in the profile, or disable
one of the conflicting profiles.

### CONFIG_LAYER_NOT_WRITABLE

The targeted config layer is read-only for the current operation. Bucket:
`USER_ERROR (1)`. Target a writable layer with `eawf config set --layer
<layer>`.

### CONFIG_FIELD_UNKNOWN

A config get/set referenced a field the schema does not define. Bucket:
`USER_ERROR (1)`. Run `eawf config list` to see valid field paths.

## User input

### INVALID_INPUT

Bad CLI arguments or a schema mismatch on input. Bucket: `USER_ERROR (1)`.
Run `eawf <verb> --help` to see the expected option shapes.

### MISSING_REQUIRED_ANSWER

A required interactive answer was missing (and `--no-input` aborted the
prompt). Bucket: `USER_ERROR (1)`. Re-run without `--no-input`, or pass the
answer via the matching flag.

## External

### EXTERNAL_API_FAILURE

An external API call (outside the agent runtimes) failed. Bucket:
`INTERNAL_ERROR (5)`. Retry; if it persists, check the external service
status and the configured endpoint.

## Fallback

### UNKNOWN

An uncategorised cause — the error site has not yet adopted a specific
`ErrorCode`. Bucket: `INTERNAL_ERROR (5)`. File an issue with the error
envelope so the cause can be classified.
