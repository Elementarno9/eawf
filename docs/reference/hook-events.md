# Eä hook events (v1)

Source of truth: `src/eawf/hooks/event.py` (:class:`HookEventType`, :class:`HookEvent`). The Claude Code translation table lives in `src/eawf/runtimes/claude/hooks_router.py`.

Adding or removing a `HookEventType` is a `[CORE]` schema bump on `feature/eawf-v0.1` and requires an entry in this document.

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

Idempotence key: `(event_type, scope_id, occurred_at)`. The runner / CLI handler treats two events with the same triple as the same event; `.ea/store/event.jsonl` appends one row per triple.

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
| `wave_close`     | `eawf task complete` succeeds                             | `{ "wave_id": str, "result": str }`                                                                      |
| `iter_open`      | `eawf batch create` succeeds                              | `{ "iter_id": str, "phase_id": str }`                                                                    |
| `iter_close`     | `eawf batch complete` succeeds                             | `{ "iter_id": str, "verdict": str }`                                                                     |
| `phase_open`     | `eawf milestone create` succeeds                            | `{ "phase_id": str }`                                                                                    |
| `phase_close`    | `eawf milestone accept` succeeds                           | `{ "phase_id": str, "outcome": str }`                                                                    |
| `permission_request` | The host holds a tool call for its operator (Claude `PermissionRequest`) | `{ "session_id": str, "agent_id": str?, "tool_name": str, "tool_input": dict }` |
| `pre_tool_use` | The host is about to run a tool call (Claude `PreToolUse`, every tool) | `{ "session_id": str, "agent_id": str?, "tool_name": str, "tool_input": dict, "tool_use_id": str }` |
| `post_tool_use` | The host ran a tool call (Claude `PostToolUse`, every tool) | `{ "session_id": str, "agent_id": str?, "tool_name": str, "tool_use_id": str, "tool_response": any }` |
| `post_tool_use_failure` | A host tool call ran and failed (Claude `PostToolUseFailure`) | `{ "session_id": str, "agent_id": str?, "tool_name": str, "tool_use_id": str, "error": str }` |

The shapes above are illustrative — at v1 the router merely forwards the incoming dict under the chosen key. Strict shape validation (per `payloads.<key>` Pydantic models) is reserved for a future schema bump.

## Claude Code mapping

Claude Code emits hook payloads with a stable `hook_event_name` field. The translation table is owned by `runtimes/claude/hooks_router.py`:

| Claude `hook_event_name` | Eä `HookEventType`                              |
|--------------------------|-------------------------------------------------|
| `SessionStart`           | `session_start`                                 |
| `SessionEnd`             | `session_end`                                   |
| `Stop`                   | `session_end`                                   |
| `PreToolUse` (Bash)      | `pre_commit` if `git commit`; `pre_push` if `git push` |
| `PostToolUse` (Bash)     | `post_commit` if `git commit`; `post_push` if `git push` |
| `PermissionRequest`      | `permission_request`                            |
| `PreToolUse` (plugin wrapper, every tool) | `pre_tool_use`                  |
| `PostToolUse` (plugin wrapper, every tool) | `post_tool_use`                |
| `PostToolUseFailure`     | `post_tool_use_failure`                         |

Two handlers share the tool-use events and each filters by `tool_name`: `runtime.host_tool` states every call on its Run's transcript, and `runtime.host_file_edit` brackets `Edit`, `Write` and `MultiEdit` so each edit lands as a file change carrying the trees on either side and its diff.

Before any of them, `pre_tool_use` runs the data-loss guard (`runtime/sandbox/data_loss.py`) on Claude Code and Codex. It refuses exactly four patterns, fail-closed: a managed worktree removed, moved or reset by a non-Eawf route; a worktree created outside `.ea/worktrees` (host-native worktrees included); a `git commit` or `git push` naming no directory from a working directory that drifted out of the session's work tree; and a direct write to `.ea/state.json`, `.ea/store`, `.ea/ledger`, `.ea/telemetry.db` or `.ea/local/epoch2`. A refusal is printed as the host's `permissionDecision: deny` and filed through `runtime.host.tool.deny` as a sandbox decision; an unreachable daemon loses only the record. OpenCode's bridge has no pre-tool event and cannot enforce the guard.

Unrecognised payloads (missing `hook_event_name`, unknown event, non-Bash tools without a v1 mapping) → `route_claude_payload` returns `None` and emits a `logging.warning(...)`. The router never raises.

## CLI surface

```
eawf hook run <event_type> \
  [--runtime claude|opencode|generic] \
  [--scope <id>] \
  [--command <str>] \
  < payload.json
```

- Reads stdin as JSON (empty stdin permitted; treated as `{}`).
- Folds the decoded payload under `payloads[<event_type>]` on the built `HookEvent`.
- Dispatches through a fresh `HookRunner` (no hooks registered in v1 — runtime adapters wire registrations in W05).
- Exit `0` on the no-block path (default), `9` (`HOOK_BLOCKED`) when any hook returns `block=True`, `3` (`INVALID_INPUT`) on malformed stdin or unknown event type.

The command always emits a JSON output envelope on stdout — `header.skill = /audit`, `header.status = "ok"` or `"blocked"`, `body.results` carries one `{name, block, output, duration_ms, raised}` row per registered hook.
