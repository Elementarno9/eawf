# Eä exit codes

Canonical exit codes emitted by every `eawf` CLI handler. The constants live
in `src/eawf/cli/exit_codes.py`; the matching `CliError` subclasses live in
`src/eawf/cli/errors.py`.

| Code | Name | When |
|------|----------------------|---------------------------------------------|
| 0    | OK                   | success |
| 1    | GENERIC_ERROR        | uncategorised failure |
| 2    | NOT_FOUND            | scope, state, artifact not found |
| 3    | INVALID_INPUT        | bad CLI args, schema mismatch on input |
| 4    | VALIDATION_FAILED    | strict invariant validation rejection |
| 5    | LOCK_CONFLICT        | sibling lock held by live holder, or timeout |
| 6    | INSTRUMENT_MISSING   | required external tool absent |
| 7    | USER_DECLINED        | user declined at confirmation gate |
| 8    | INTEGRITY_VIOLATION  | hash mismatch, drift, corrupted store |
| 9    | HOOK_BLOCKED         | pre-/post-tool hook fail-closed |

## Envelope

When `--json` is set, every error is emitted with the canonical envelope:

```json
{
  "error": "<class name>",
  "message": "<str(err)>",
  "exit_code": 5,
  "exit_name": "LOCK_CONFLICT"
}
```

Without `--json`, the same error renders as `error: <message>` on stdout and
the process exits with the matching numeric code.
