# Eä hook events (v1)

Source of truth: `src/eawf/hooks/event.py`
(:class:`HookEventType`, :class:`HookEvent`). The Claude Code translation
table lives in `src/eawf/runtimes/claude/hooks_router.py`.

Adding or removing a `HookEventType` is a `[CORE]` schema bump on
`feature/eawf-v0.1` and requires an entry in this document.

## HookEvent shape

```yaml
event_type: <HookEventType>           # one of the values below
scope_id: <str>                       # Eä scope ID (wave/iter/phase) or ""
command: <str>                        # originating CLI command, may be ""
args: dict[str, Any]                  # parsed CLI flags or runtime context
runtime: claude | opencode | generic
occurred_at: datetime                 # UTC timezone-aware
payloads:                             # per-event extension shapes
  <event_type or runtime>: dict[str, Any]
```

`extra="forbid"` — unknown top-level keys are rejected.

Idempotence key: `(event_type, scope_id, occurred_at)`. The runner /
CLI handler treats two events with the same triple as the same event;
`events.jsonl` appends one row per triple.

## Event types

| `event_type`     | Triggered when                                               | `payloads.<key>` shape (v1)                                                                              |
|------------------|--------------------------------------------------------------|----------------------------------------------------------------------------------------------------------|
| `pre_commit`     | Before a git commit (Claude `PreToolUse` Bash `git commit`)  | `{ "files_changed": list[str], "branch": str }`                                                          |
| `post_commit`    | After a git commit                                            | `{ "sha": str, "branch": str }`                                                                          |
| `pre_push`       | Before a git push                                             | `{ "remote": str, "branch": str }`                                                                       |
| `post_push`      | After a git push                                              | `{ "remote": str, "branch": str, "rejected": bool }`                                                     |
| `pre_audit`      | Before `/audit` skill runs                                    | `{ "scope": str }`                                                                                       |
| `post_audit`     | After `/audit` finishes                                       | `{ "scope": str, "verdict": str }`                                                                        |
| `session_start`  | New agent session opens (Claude `SessionStart`)               | `{ "session_id": str, "cwd": str }`                                                                      |
| `session_end`    | Agent session closes (Claude `Stop` / `SessionEnd`)           | `{ "session_id": str, "duration_s": float }`                                                             |
| `wave_open`      | `eawf wave open <wave>` succeeds                              | `{ "wave_id": str, "iter_id": str }`                                                                     |
| `wave_close`     | `eawf wave close <wave>` succeeds                             | `{ "wave_id": str, "result": str }`                                                                      |
| `iter_open`      | `eawf iter open <iter>` succeeds                              | `{ "iter_id": str, "phase_id": str }`                                                                    |
| `iter_close`     | `eawf iter close <iter>` succeeds                             | `{ "iter_id": str, "verdict": str }`                                                                     |
| `phase_open`     | `eawf phase open <phase>` succeeds                            | `{ "phase_id": str }`                                                                                    |
| `phase_close`    | `eawf phase close <phase>` succeeds                           | `{ "phase_id": str, "outcome": str }`                                                                    |

The shapes above are illustrative — at v1 the router merely forwards the
incoming dict under the chosen key. Strict shape validation (per
`payloads.<key>` Pydantic models) is reserved for a future schema bump.

## Claude Code mapping

Claude Code emits hook payloads with a stable `hook_event_name` field.
The translation table is owned by `runtimes/claude/hooks_router.py`:

| Claude `hook_event_name` | Eä `HookEventType`                              |
|--------------------------|-------------------------------------------------|
| `SessionStart`           | `session_start`                                 |
| `SessionEnd`             | `session_end`                                   |
| `Stop`                   | `session_end`                                   |
| `PreToolUse` (Bash)      | `pre_commit` if `git commit`; `pre_push` if `git push` |
| `PostToolUse` (Bash)     | `post_commit` if `git commit`; `post_push` if `git push` |

Unrecognised payloads (missing `hook_event_name`, unknown event,
non-Bash tools without a v1 mapping) → `route_claude_payload` returns
`None` and emits a `logging.warning(...)`. The router never raises.

## CLI surface

```
eawf hook run <event_type> \
  [--runtime claude|opencode|generic] \
  [--scope <id>] \
  [--command <str>] \
  < payload.json
```

- Reads stdin as JSON (empty stdin permitted; treated as `{}`).
- Folds the decoded payload under `payloads[<event_type>]` on the
  built `HookEvent`.
- Dispatches through a fresh `HookRunner` (no hooks registered in v1 —
  runtime adapters wire registrations in W05).
- Exit `0` on the no-block path (default), `9` (`HOOK_BLOCKED`) when any
  hook returns `block=True`, `3` (`INVALID_INPUT`) on malformed stdin
  or unknown event type.

The command always emits a JSON output envelope on stdout —
`header.skill = /audit`, `header.status = "ok"` or `"blocked"`,
`body.results` carries one `{name, block, output, duration_ms, raised}`
row per registered hook.
