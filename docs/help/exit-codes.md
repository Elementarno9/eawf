# Exit codes

Every `eawf` CLI handler exits with one of six canonical codes. The
constants live in `src/eawf/cli/exit_codes.py`; the matching `CliError`
subclasses live in `src/eawf/cli/errors.py`.

| Code | Name | When | Repair |
|------|--------------------|------------------------------------------|--------|
| 0 | OK | success | — |
| 1 | USER_ERROR | bad CLI args, schema mismatch on input, missing scope id, missing external tool, user-declined gate, protocol mismatch | `eawf <verb> --help`; check ids and env |
| 2 | VALIDATION_ERROR | strict invariant validation rejected the candidate state | `eawf validate` to inspect schema errors |
| 3 | STATE_CONFLICT | sibling lock held, integrity/hash mismatch, hook fail-closed, runtime ladder exhausted | `eawf doctor` for diagnosis |
| 4 | DAEMON_UNREACHABLE | daemon down, unresponsive, or shutting down | `eawf daemon start` then retry; `--daemonless` for read-only verbs |
| 5 | INTERNAL_ERROR | uncaught raised path — file an issue | attach the error envelope + `eawf daemon logs --lines 200` |

## Legacy specificity

The v0.3 surface compressed the legacy nine codes (0..9) into the six
buckets above. The finer-grained legacy distinction survives as the
`data.kind` string on the error envelope so CI scripts can still pivot on a
specific failure mode without depending on the retired numeric codes:

- `NotFound`, `InvalidInput`, `InstrumentMissing`, `UserDeclined`,
  `ProtocolMismatch` fold into `1 USER_ERROR`.
- `LockConflict`, `IntegrityViolation`, `HookBlocked`, `RuntimeUnavailable`
  fold into `3 STATE_CONFLICT`.

## Error envelope

When `--json` is set, every error is emitted with the canonical envelope:

```json
{
  "schema_version": "1.0",
  "error": "StateConflict",
  "message": "another writer holds the lock",
  "exit_code": 3,
  "exit_name": "STATE_CONFLICT",
  "suggested_next_step": "another writer / hook / runtime conflict; run `eawf doctor`",
  "data": {"kind": "LockConflict"},
  "correlation_id": null,
  "protocol_version": null,
  "timestamp": "2026-05-20T12:00:00Z"
}
```

The `error` field carries the canonical bucket name; the legacy class name
(when applicable) lives under `data.kind`. Without `--json`, the same error
renders as `error: <message>` plus `hint:` / `exit_code:` / `kind:` lines on
stdout, and the process exits with the matching numeric code.

## Daemon JSON-RPC mapping

When an error originates daemon-side, the JSON-RPC error code maps onto a
bucket via `cli_error_for_rpc` in `src/eawf/cli/errors.py` — for example
`-32001` (lock) → `3 STATE_CONFLICT`, `-32009` (shutting down) →
`4 DAEMON_UNREACHABLE`, `-32602` (invalid params) → `1 USER_ERROR`. Unknown
codes fall back to `5 INTERNAL_ERROR`.
