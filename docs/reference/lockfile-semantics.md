# Lockfile semantics

*`portalocker`-backed sibling lockfiles, atomic write, and stale-lock recovery.*

All store and state writes acquire a sibling lockfile, perform
read-modify-write, atomic-rename the result, and release the lock.

## Sibling lockfile location

- Sibling lockfile: `<target>.lock` in the same directory as `<target>`.
- Examples:
  - `.ea/state.json` is locked by `.ea/state.json.lock`.
  - `.ea/store/memory.jsonl` is locked by
    `.ea/store/memory.jsonl.lock`.
  - `~/.ea/store/memory.jsonl` is locked by
    `~/.ea/store/memory.jsonl.lock`.

## Acquire timeout

- Default acquire timeout: **5 seconds**.
- Configurable via `EA_LOCK_TIMEOUT` (seconds, float).
- Acquire failure raises `LOCK_CONFLICT` (exit code 5; see
  `docs/reference/exit-codes.md`).

## Lock holder body

The lockfile body records the current holder:

```json
{
  "pid": 12345,
  "hostname": "workhorse",
  "started_at": "2026-05-08T12:00:00Z",
  "heartbeat_at": "2026-05-08T12:00:30Z"
}
```

`hostname` here is the local short hostname returned by
`socket.gethostname()` — used only for cross-host lock collision
detection (e.g., NFS mounts). Not persisted into committed artifacts.

## Stale-lock detection and recovery

A lock is stale when:

- the holder PID is dead (no process with that PID exists), **OR**
- `heartbeat_at` is more than **60 seconds** old.

On stale-lock detection:

1. Acquire the lock by stealing it.
2. Emit a `lock_stolen` event to `.ea/store/event.jsonl` with the
   stolen holder identity, the new holder identity, and the reason
   (dead PID or heartbeat-stale).
3. Continue the requested write.

The stale-lock check runs both before acquire (to skip waiting for a
provably-dead holder) and on acquire timeout (to recover from holders
that died mid-write).

## Atomic write protocol

Every committed write follows this sequence:

1. Acquire sibling lock.
2. Read current target into memory.
3. Validate input + apply mutation in memory.
4. Write the mutated content to a sibling tempfile (`<target>.tmp.<rand>`)
   in the same directory.
5. `fsync` the tempfile.
6. `os.replace(tempfile, target)` — atomic rename on POSIX and Windows
   (Python 3.3+).
7. Release lock.

Crash mid-write (kill -9 between step 4 and step 6) leaves the prior
target file intact; the orphan tempfile is cleaned up by the next
acquire of the same lock.

## Property-test coverage

Phase 1 W04 property tests assert:

- 10000 random writes preserve invariants under concurrent acquire.
- N concurrent writers produce exactly-once semantics: every successful
  acquire-write-release pair is reflected in the final target.
- Crash-mid-write fixtures (kill -9 between tempfile and rename) leave
  the prior file intact.
- Steal-while-stealing races converge: at most one steal succeeds; the
  other fails with `LOCK_CONFLICT`.

## CLI behaviour

`uv run eawf <mutation>`:

- Times out after 5s on contended locks unless `EA_LOCK_TIMEOUT`
  overrides.
- Emits `LOCK_CONFLICT` exit code (5) on timeout.
- Heartbeat is refreshed every 30s during long-running mutations
  (e.g., `eawf store compact`).

## Cross-references

- Exit codes — `docs/reference/exit-codes.md`.
- Hook events — `docs/reference/hook-events.md`. (`lock_stolen` is a
  logged event tag in `src/eawf/lock/portalock.py`, **not** a
  `HookEventType`.)
- State CLI as the only mutator — `docs/architecture/cli-surface.md`.
- Source: `src/eawf/lock/portalock.py`, `src/eawf/lock/sibling.py`,
  `src/eawf/lock/stale.py`, `src/eawf/state/writer.py`.
