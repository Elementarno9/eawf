# Streaming output

A long-running verb that subscribes to the daemon event bus can stream progress as it happens instead of buffering a single block at the end. Streaming is opt-in via `--stream` and off by default so CI consumers keep a deterministic single-block stdout.

No shipped verb takes `--stream` yet; the frame shapes below are the contract the streaming renderer (`src/eawf/surfaces/cli/streaming.py`) emits for a verb that wires it. To watch a long operation today, pass the operation reference a long verb answered with to `eawf follow <operation-ref>`, which polls its states until it succeeds, fails or is lost.

## NDJSON shape (`--json --stream`)

One complete JSON object per newline-terminated line. The first line is a `start` frame, each daemon `event.push` becomes an `event` frame, and the stream is terminated by an `end` frame carrying the terminal status (the event kinds shown are illustrative):

```text
{"correlation_id":"c-1","scope_id":"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010","started_at":"2026-05-20T12:00:00Z","type":"start"}
{"kind":"run_started","payload":{"task":"EAWF-0042"},"timestamp":"2026-05-20T12:00:01Z","type":"event"}
{"kind":"dispatch_log","line":"runtime: claude-code","timestamp":"2026-05-20T12:00:01Z","type":"event"}
{"kind":"run_finished","payload":{"status":"ok"},"timestamp":"2026-05-20T12:00:30Z","type":"event"}
{"correlation_id":"c-1","finished_at":"2026-05-20T12:00:30Z","status":"ok","type":"end"}
```

Each line is independently parseable. A partial line means a consumer-side parser bug or a killed process, never a half-written object.

## Human shape (`--stream` alone)

Bracketed `[HH:MM:SS]` progress lines, terminated by a single blank line:

```text
[12:00:00] starting for eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010...
[12:00:01]   run_started: ...
[12:00:01]   dispatch_log: runtime: claude-code... (truncated; pass --verbose for full)
[12:00:30]   run_finished: ...

```

## EOF semantics

- NDJSON: the final line is the `end` frame. Subscribers detect end-of-stream by parsing it; its `status` drives the exit code.
- Human: the final line is a single blank `^$` marker.

Exit code echoes the terminal status: `ok` → 0, `failed` → 5 INTERNAL_ERROR, daemon disconnect → 4 DAEMON_UNREACHABLE.

## Flag combinations

- `--json --stream` = NDJSON (the canonical machine stream).
- `--md --stream` is rejected because markdown is not round-trippable line-by-line. Exits 1 USER_ERROR (`data.kind="InvalidInput"`).
- `--quiet --verbose` is rejected as contradictory. Exits 1 USER_ERROR (`data.kind="InvalidInput"`).

Without `--stream`, the verb prints a single envelope at the end and exits, with no per-event frames.
