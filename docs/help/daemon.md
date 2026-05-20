# Daemon

`eawfd` is the per-user daemon and the sole canonical mutator of
`state.json`, layered config YAML, the registry JSON, the event/audit
stores, and the telemetry DB (Decision D-SUP-01). Read access stays free and
direct; mutations route through the daemon over JSON-RPC, with a
`portalocker` direct-write fallback when the daemon is unavailable (CI /
one-shot / recovery shell).

## Lifecycle

```text
eawf daemon enable    # install the autostart service (opt-in)
eawf daemon start     # spawn the daemon now
eawf daemon status    # show pid, socket, protocol version, uptime
eawf daemon stop      # graceful stop (use --drain to finish in-flight work)
eawf daemon disable   # remove the autostart service
eawf daemon logs      # tail the daemon log (--lines N, --follow)
eawf daemon ping      # round-trip health check
```

The CLI cold-spawns the daemon automatically on the first mutating call when
it is not already running. The spawn is silent unless you pass `--verbose`
(or set `EAWF_VERBOSE=1`), which surfaces the spawn step, IPC framing, and
WAL transitions on stderr.

## Transport

POSIX hosts use a Unix domain socket at `<runtime-dir>/eawfd.sock`; Windows
uses a named pipe via the pywin32 bridge. Frames are newline-delimited
JSON-RPC 2.0 objects. Peer credentials are OS-enforced (UDS SO_PEERCRED /
named-pipe DACL) so only the owning user's processes can connect.

## Daemonless escape hatch

Read-only verbs can bypass the daemon with `--daemonless`, or automatically
when `EAWF_DAEMONLESS=1` / `CI=true` is set. Mutating verbs reject
`--daemonless` with exit 1 USER_ERROR (`data.kind="InvalidInput"`) — every
write must go through the canonical mutator or its portalocker fallback.

## Troubleshooting

- **`4 DAEMON_UNREACHABLE`** — connection refused, stale pid, or the daemon
  is shutting down. Run `eawf daemon status`, then `eawf daemon start`. On
  restart the daemon replays its write-ahead log.
- **Protocol mismatch (`data.kind="ProtocolMismatch"`)** — the CLI and
  daemon disagree on protocol version. Upgrade with `uv tool upgrade eawf`,
  then `eawf daemon stop` and retry.
- **Lock conflict (`3 STATE_CONFLICT`)** — another writer holds the lock.
  Retry shortly or run `eawf doctor` to inspect holders.
- **Subscription dropped (`data.kind="SubscriptionDropped"`)** — a
  `--stream` consumer fell behind and the daemon dropped the subscription;
  partial output is already on stdout. Re-run without piping to a slow sink.

See `eawf help streaming` for the `--stream` event-subscription surface and
`eawf help exit-codes` for the full exit-code table.
