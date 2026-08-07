# C02 — Daemon + Topology + Security spine — Eä framework long-term specs

**Cluster:** C02 (Daemon + Topology + Security spine — IPC, lease/lock, supervisor, event bus, resource limits, per-OS service surface, runtime fallback, session-handle tracking)

**Title:** Daemon + Topology + Security spine

**Status:** `local-draft`, `needs-user` (pending operator ratification of §8 open questions)

**Created:** `2026-05-16T00:00:00Z`

**Author:** `claude-opus-4-7`

**Depends on:** C00 (verdicts V1, V5, V6, V8 locked) [1], C01 (entity catalog + URN scheme + persona authority matrix) [2]

**Consumed by:** C03 (specs ride daemon RPC), C04 (skills invoke daemon RPC + subscribe to push), C05 (CLI verb-noun matrix routes mutations to daemon), C06 (TUI subscribes to event bus), C07 (per-runtime adapter exposes session-handle + error-class surface), C08 (per-layer config sourced from daemon), C09 (per-OS CI matrix + structured logs sourced from daemon)

## 1. Purpose + scope statement

C02 makes V1 [1:24-53] implementable. The brief locks the daemon architecture — IPC protocol, lease/lock semantics, supervisor + spawn model, event bus, subscription contract, resource limits, security boundary, per-OS service-registration surface, runtime fallback handler, session-handle tracking — and the migration path from today's state-CLI-as-writer [11][12] to a daemon-mediated mutator surface.

V1 names eawfd as the Day-1 coordinator: `state.json` mutations route through it, reads MAY bypass for CI / read-only one-shot CLI / recovery shell, daemon stays alive on a 300-second idle timeout, daemon protocol is versioned with fail-fast on skew, and `portalocker` is retained as defense-in-depth. V5 [1:127-151] layers a reactive runtime-fallback handler on top of the daemon's dispatcher. V6 [1:153-182] adds per-OS native service-registration (systemd-user / launchd / Windows Service) over the on-demand spawn. V8 [1:226-271] adds per-`(wave, attempt)` session-handle tracking inside the daemon so retry envelopes route to existing runtime sessions.

The current code is a CLI-as-mutator surface: `state_transaction` [11] yields a typed `State` under a `portalocker` advisory lock; each of ~22 CLI command modules [counted via grep] reads-validates-mutates-validates-writes inside one lock acquisition; `atomic_write_json_locked` [12] commits via tempfile + `os.replace` + parent-dir fsync. There are 73 callsites total across `src/eawf/` that depend on `state_transaction` / `atomic_write_json`. C02 lays out how those callsites move from in-process locking to daemon RPC without breaking the AGENTS rule 4 single-canonical-mutator invariant [13].

**In scope (C00 §C02 [1:362-425]):**

- IPC protocol pick (JSON-RPC 2.0 over Unix domain socket on POSIX; named pipe on Windows per V6 [1:159]).
- Method catalog (request/response shapes for state mutation, state read, event subscription, agent dispatch, daemon control).
- Auth model (file permissions on the socket, `SO_PEERCRED` on Linux + `LOCAL_PEERCRED` on macOS BSD, named-pipe ACL on Windows).
- Spawn model (on-demand auto-spawn per V1; PID + socket files at `$XDG_RUNTIME_DIR/eawfd/` on Linux + `<local-path>` fallback on macOS/Windows).
- Idle-timeout shutdown (default 300 s, configurable).
- Subscription bus (push-based event-stream with per-subscriber backpressure window).
- Supervisor + crash safety (WAL written before transaction; replay on startup).
- Resource limits (concurrent dispatch cap, per-wave wall-clock cap, per-runtime budget broker, OOM kill thresholds).
- Version skew handling (CLI v0.4 against daemon v0.3 → structured fail with upgrade hint).
- Per-OS service-registration spec (Linux `systemd --user` unit, macOS `launchd` LaunchAgent plist, Windows Service via `pywin32` *or* NSSM wrapper — recommendation below).
- Portalocker per-OS matrix (`fcntl` on Linux/macOS, `LockFileEx` on Windows) + CI test plan.
- Runtime fallback handler per V5 (error-class detection per runtime adapter, ladder traversal, `runtime_switched` event emit, halt-on-no-backup with BLOCKED status, manual override `eawf wave switch --to <runtime>`).
- Session-handle tracking per V8 (`state.waves[wave_id].sessions[attempt_id]` persisted by daemon; retry routes back to existing session; `--continue` failure falls back to fresh with `Wave.dispatch_history` annotation).
- Process topology diagram (CLI / TUI / web stub / eawfd / agent runtimes).

**Out of scope (deferred per C00 [1:379-382]):**

- Multi-user / multi-tenant daemon (deferred to v0.6+). V1 [1:178-179] locks the single-user invariant.
- Network daemon (Unix socket / named pipe only — no TCP listener).
- Hot-reload of daemon code (always full restart on upgrade).
- Per-runtime SDK surface (deferred to C07 [1:670-679]; this brief assumes subprocess CLI as the V8 [1:255-262] primary path).
- Telemetry schema or DuckDB rollup (V7 deferred to C09 [1:769-841]).
- Daemon-side specs cache implementation (C01 §5.4.15 [2:1125-1151] reserves `<local-path>`; C03 finalises the cache structure).
- Federation / cross-machine handshake (R5 reserved per C01 D4 [2:Q4]; v0.5+).

## 2. Goals + non-goals

### Goals

| G# | Goal | Source |
|---|---|---|
| G1 | Single canonical mutator path: every write to `state.json`, `event.jsonl`, `audit.jsonl`, `<local-path>`, `.ea/config.yaml`, `<local-path>` proxies through eawfd when the daemon is up; portalocker retained as defense-in-depth so a daemonless mutator (CI, recovery shell) is still safe. | C00 V1 [1:24-53] |
| G2 | IPC protocol is stdlib-friendly, deterministic, version-tagged. JSON-RPC 2.0 over Unix domain socket (POSIX) / named pipe (Windows) is the wire. | C00 §C02 axis 1 [1:395]; C00 V6 [1:159] |
| G3 | Auto-spawn cold-start under 400 ms on a warm cache; warm-daemon mutation under 50 ms p99 (including validator traversal). | C00 V1 [1:44] |
| G4 | Reactive runtime fallback covers HTTP 429 / 5xx / timeout / API-error / auth-error; emits `runtime_switched` event; halts the wave with `BLOCKED` if every runtime in the ladder fails. | C00 V5 [1:127-151] |
| G5 | Per-OS service-registration via `eawf daemon enable|disable|status`; on-demand spawn remains the default; service-file install is opt-in and fully reversible. | C00 V6 [1:153-182] |
| G6 | Daemon persists per-`(wave, attempt)` session handles so retry envelopes route to the existing runtime session; `--continue` failure falls back to fresh with a `DispatchAnnotation`. | C00 V8 [1:226-271] |
| G7 | Crash safety: write-ahead log captures the mutation intent before the transaction commits; daemon replay on startup makes incomplete writes either complete or rolled-back, never wedged half-applied. | C00 §C02 axis 6 [1:400] |
| G8 | Resource-limit policy is profile-conditional: a default cap on concurrent dispatch (W1), a per-wave wall-clock cap with SIGTERM→SIGKILL ladder, an EU budget broker, an OOM kill threshold. | C00 §C02 axis "Resource limits" [1:373]; roadmap-synthesis §"Budget broker" [7:115-128] |
| G9 | Version-skew handling: CLI ↔ daemon protocol carries `protocol_version`; mismatch fails fast with a structured envelope that names the upgrade command. | C00 §C02 axis 9 [1:374] |
| G10 | Security: socket file permission `0600`, peer-credential check (`SO_PEERCRED` / `LOCAL_PEERCRED` on POSIX; named-pipe DACL on Windows), structured error envelopes never leak machine paths or PII. | C00 §C02 axis "Security boundary" [1:375]; AGENTS rule 16 [13] |
| G11 | Brief is self-contained: quotes V1 / V5 / V6 / V8 inline, cites every source-file reference at file:line, ratifiable in one fresh CC session. | C00 V4 [1:99-125] |

### Non-goals

| NG# | Non-goal | Why deferred |
|---|---|---|
| NG1 | Multi-user / multi-tenant daemon. | V1 [1:178-179] locks single-user; v0.6+ scope. |
| NG2 | Network daemon, TCP listener, cross-machine federation. | Unix socket / named pipe only per V6 [1:159] + R5 deferred. |
| NG3 | Hot reload of daemon code. | C00 §C02 axis [1:382] — always full restart. |
| NG4 | Per-runtime SDK adapter (vs subprocess CLI). | C00 V8 [1:255-262] picks subprocess CLI as primary; C07 owns the per-runtime adapter shape. |
| NG5 | Telemetry DB (DuckDB schema, projection algorithm). | C00 V7 [1:184-224] deferred to C09. |
| NG6 | TUI widget catalog, modal stack, `/` palette. | C06 owns it. |
| NG7 | CLI verb-noun matrix surface. | C05 owns it; C02 specifies only the *daemon* surface (RPC methods). |
| NG8 | Profile composition algorithm. | C08 owns it. |
| NG9 | Spec-cache implementation. | C03 owns it; C01 reserves `<local-path>`. |
| NG10 | Agent-lens schema vendoring. | V7 → C09. |

## 3. Prior verdicts cited

### V1 — eawfd daemon Day-1 + smart-spawn writer [1:24-53]

> "Mutations to `state.json` (and all future stateful surfaces — config layers, registry, event log) route through the eawfd daemon. CLI auto-spawns daemon on demand if not running (tmux / systemd-user / jupyter-kernel pattern); daemon stays alive on idle-timeout (default 300 s, configurable). Reads MAY bypass daemon (direct file IO + Pydantic load) for: CI environments without long-running processes, read-only one-shot CLI calls, recovery shell when daemon broken or version-skewed."

**C02 binding.** This brief implements V1. Daemon is the only writer when up. Daemon protocol is versioned per V1 hard non-negotiable [1:50]; `portalocker` is retained alongside daemon RPC for defense-in-depth [1:51]. Cold-spawn budget: 200-400 ms first call [1:44]; warm: ~5-10 ms IPC + state cached.

### V5 — Runtime fallback: reactive switchover on error [1:127-151]

> "Daemon uses reactive auto-switch on primary-runtime failure (HTTP 429 / 5xx / timeout / API-error). No active health-probe. On error, daemon flips the affected wave to the next runtime in the configured preference ladder and re-issues the dispatch envelope against that runtime with the idempotency key preserved."

**C02 binding.** §5.12 specifies the runtime-fallback state machine. Per-runtime adapter MUST surface a stable error-class taxonomy (`RUNTIME_RATE_LIMIT`, `RUNTIME_SERVER_ERROR`, `RUNTIME_TIMEOUT`, `RUNTIME_API_ERROR`, `RUNTIME_AUTH_ERROR`) so the dispatcher does not decode per-runtime error strings — C07 enforces. Fallback never silently rewrites `Wave.runtime` [1:146]; daemon emits a `runtime_switched` event with `from / to / cause` fields. If every runtime in the ladder fails, daemon halts the wave with `BLOCKED` status [1:147]. Operator-manual override: `eawf wave switch <wave-id> --to <runtime>` [1:148-149]. The reactive policy extends the existing 429 vendor-pause logic in roadmap-synthesis [7:130-133].

### V6 — Cross-platform daemon: per-OS native service + on-demand spawn [1:153-182]

> "Daemon bootstraps natively on each supported OS using each platform's idiomatic service-registration surface, layered on top of the on-demand spawn from V1: Linux `systemd --user` unit at `<local-path>`; macOS `launchd` LaunchAgent plist at `<local-path>`; Windows per-user Windows Service via `pywin32` (`win32serviceutil`) or NSSM wrapper. Auto-start on login is opt-in via `eawf daemon enable`. On-demand spawn from V1 remains the default."

**C02 binding.** §5.10 specifies the three service-file templates + the install/uninstall verbs (`eawf daemon enable|disable|status`). §5.11 specifies the portalocker per-OS matrix and the CI test plan. The Windows `pywin32` vs NSSM tradeoff is decided in §4 D2 of this brief. The Windows file-ID portability concern raised in state-history-cache-design [8:266-277] is addressed by hashing the repo + relative path rather than relying on OS file IDs.

### V8 — Agent dispatch: hybrid session reuse [1:226-271]

> "Fresh process per new wave dispatch — clean context, full KV-cache hit on the stable prefix, deterministic token cost. Reuse session (`claude --continue <session-id>` / Codex `--resume` / OpenCode equivalent) on retry / edit / follow-up against the same wave — preserves turn history, avoids re-explaining state. Daemon tracks the session handle per `(wave_id, attempt_id)` and routes retry envelopes back to the existing session."

**C02 binding.** §5.13 specifies the daemon-side session-handle table. C01 §5.3.5 [2:362-381] reserved the field shape; C02 implements daemon read + write via the dispatcher RPC. `--continue` failures (session expired, session-log file deleted, runtime adapter cannot resume) fall back to fresh dispatch with a `DispatchAnnotation` recorded on the wave. Cross-runtime fallback (V5) opens a fresh session on the new runtime — session handles are runtime-specific, never portable [1:267].

### C01 — Foundations (vocabulary + URN + entities + lifecycle) [2]

C02 cites:

- **URN scheme** [2:179-242] — `urn:eawf:v1:<kind>:<path>`. C02 introduces no new URN kinds but uses `daemon`, `runtime`, `wave`, `session`, `event` URNs in envelope payloads.
- **Persona authority matrix** [2:1240-1276] — only the `daemon` persona writes `state.json`. Every CLI mutator path effectively proxies through the daemon when it is up; the matrix cell "Mutate `state.json`" is `🟡 (via CLI)` for the operator and `✅ (sole writer per V1)` for the daemon.
- **AgentSession lifecycle** [2:1063-1086] — V8 makes the CHECKPOINTED→ACTIVE transition a `--continue` invocation; daemon validates the session handle and falls back to fresh with `DispatchAnnotation`.
- **Wave entity** [2:338-385] — `Wave.sessions: dict[int, SessionAttempt]`, `Wave.runtime_preference: list[str] | None`, `Wave.dispatch_history: list[DispatchAnnotation]` — C01 reserves the fields; C02 wires the daemon to populate them.
- **Persona definitions** [2:147] — `daemon` is the single OS-user-scoped service in the authority matrix.

### Daemon concurrency model — F5 + D49 [3:77-87, 3:144-145]

Asyncio JSON-RPC over the listening socket + `loop.run_in_executor` for the portalocker single-writer mutator + validator traversal. Python 3.14 free-threaded build [3:194] retained on the table for v0.5 if the validator hot loop bottlenecks. C02 honours this pick; §5.2 sequence diagrams show the asyncio shape.

### Budget broker + quota recovery [7:115-133]

Counterfactual cost ledger from per-spawn `usage` blocks; per-wave request-count + wall-clock cap; phase-level quota tracking with auto-pause on vendor 429 and auto-resume on rate-window refresh. C02 specifies the resource-limit RPC (§5.8) and the 429 hook into the runtime-fallback state machine (§5.12).

### Axis B — bottleneck resolution + work-stealing dispatcher [4:174-251]

L_measured maxed at 2.66 on P13 — the binding constraint is the DAG critical path, not the worktree create cost [4:179-185]. Work-stealing dispatcher is "elevate later, only when concurrent demand ≥3" [4:249]; C02 specifies the daemon's job-queue surface but defers the work-stealing implementation to a later phase (the queue starts as FIFO + DAG-respecting + EU-budget-aware).

## 4. Decision matrix

Operator-confirmed axes seeded by C00 §C02 [1:394-406] + the V5 / V6 / V8 additional goals [1:386-392] and key axes [1:401-406]. Each row records the locked recommendation + rationale; this brief promotes from `local-draft` to `accepted` when §8 open questions are operator-confirmed.

| # | Axis | Options considered | Recommendation | Rationale |
|---|---|---|---|---|
| **D1** | IPC pick | (a) JSON-RPC 2.0 over Unix socket / named pipe; (b) msgpack-RPC; (c) gRPC; (d) Cap'n Proto | **JSON-RPC 2.0 over Unix domain socket (POSIX) / named pipe (Windows)** | stdlib-friendly (`asyncio.start_unix_server` on POSIX [3:194]). ~~`asyncio.start_server` over named-pipe shim on Windows~~ (**superseded 2026-05-18 per XB11 / Q8: pywin32 `CreateNamedPipe` in dedicated thread + asyncio queue bridge; original named-pipe-shim claim was structurally undeliverable since `asyncio.start_server` is a TCP listener and stdlib lacks an asyncio named-pipe server. See §5.13 Windows-transport rewrite + smoke under `.ea/local/smoke/windows-pipe-asyncio/`**). JSON wire is debuggable with `nc` / `socat`. Versioned envelope drops cleanly into the existing `Envelope` shape [2:577-584]. Per F5 [3:77-87] asyncio is the daemon concurrency model. msgpack-RPC weaker Python tooling. gRPC heavy for local IPC, requires proto codegen, opaque debug. Cap'n Proto wire is fastest but the schema-codegen path adds a build step that v0.3-v0.5 doesn't need. |
| **D2** | Windows service shape (V6) | (a) `pywin32` `win32serviceutil` Python service subclass; (b) NSSM wrapper around a plain `eawfd` executable | **pywin32** | Single-codebase install: the same Python wheel ships the service entry-point — no separate NSSM binary in the installer. pywin32 is a heavy dep (~50MB) but it is already a transitive dep for many Python-on-Windows surfaces. NSSM avoids the dep but adds a separate native binary and a multi-step install (`nssm install eawfd <python-path> <script-path>`). Recommendation: pywin32 primary; NSSM stays documented as a fallback for environments where pywin32 is forbidden (some corporate Python distributions); §5.10.3 includes both templates. |
| **D3** | Auth model on POSIX | (a) file permissions `0600` on socket only; (b) `SO_PEERCRED` / `LOCAL_PEERCRED` peer-credential check; (c) per-user shared-secret token | **(a) + (b) layered — file perms first, three per-OS peer-cred recipes (revised 2026-05-18 per XB13)** | File perms `0600` on `<local-path>` block other users at the OS layer. ~~"POSIX = one API"~~ framing was wrong: `SOL_LOCAL` is not a stdlib `socket` constant on macOS; `LOCAL_PEERCRED` returns `xucred` without usable PID; Linux `SO_PEERCRED` requires `SOL_SOCKET`. **Per XB13 (2026-05-18): three concrete per-OS recipes** — (Linux) `socket.getsockopt(SOL_SOCKET, SO_PEERCRED, struct.calcsize("3i"))`; (macOS) `os.getpeereid(fd)` (Python 3.9+); (FreeBSD) `LOCAL_PEERCRED` via ctypes. Smoke under `.ea/local/smoke/peer-cred/`. Token shared-secret rejected: no encryption story, secrets-leak risk per AGENTS rule 16 [13], buys no extra trust over peer-credential. v0.6+ multi-user would revisit. |
| **D4** | Auth model on Windows | (a) named-pipe DACL restricted to the owning user SID; (b) Windows authentication token check via `pywin32` `win32api.GetUserName` after connect | **(a) + (b) layered — DACL first, post-connect SID check second** | Same defense-in-depth as POSIX: DACL gates connection; post-connect SID check (via `win32security.GetSecurityInfo` on the pipe handle) catches DACL misconfig. C02 §5.10.3 includes the DACL template. |
| **D5** | Socket / pipe path | (a) `$XDG_RUNTIME_DIR/eawfd/eawfd.sock` (Linux) + `<local-path>` (macOS, fallback); (b) `<local-path>` everywhere; (c) `<local-path>` | **(a) — XDG honored on Linux when available, <local-path> fallback elsewhere** | Linux convention: `$XDG_RUNTIME_DIR` (typically `/run/user/<uid>`) is the right home for ephemeral sockets — tmpfs-backed, auto-cleaned on logout, OS-managed permissions [1:368]. macOS lacks `$XDG_RUNTIME_DIR` by default; `<local-path>` is the conventional substitute. Windows uses named-pipe namespace `\\.\pipe\eawfd-<user>` [1:401]. Recovery shell sets `EAWF_RUNTIME_DIR=<path>` to override per V1 [1:30]. |
| **D6** | Process pool design | (a) daemon spawns short-lived workers per agent invocation; (b) warm pool of pre-started workers; (c) thread pool inside the daemon process | **(a) — short-lived subprocess per dispatch** | The vendor runtime CLIs (`claude -p`, `codex exec`) are themselves the workers — daemon's job is to spawn / supervise / route IPC, not to host a pre-warmed Python pool. Warm-pool optimisation is YAGNI until p99 spawn cost dominates (it doesn't today — axis B [4:181] shows cycle time dominated by agent round-trips, not subprocess setup). Thread pool inside the daemon: the daemon process needs only the asyncio loop + the `run_in_executor` worker for portalocker mutator + validator (F5 [3:144-145]); no in-process agent execution. |
| **D7** | Backpressure model on the event bus | (a) drop-oldest (sliding window per subscriber); (b) block the producer; (c) per-subscriber bounded queue with disconnect on overflow | **(a) — drop-oldest sliding window per subscriber (revised 2026-05-18 per audit C02.F50)** | Producer (daemon mutator path) must never block on a slow subscriber — that would re-introduce the bottleneck the daemon is supposed to remove. ~~Disconnect-on-overflow worse than drop-oldest because it forces every subscriber into reconnect-loop on momentary backpressure.~~ **Per Claude C02.F50 (2026-05-18): flip to drop-oldest**. Subscriber sees a `subscription_lag` envelope with `dropped_count` + `last_event_id`; reconnects with `since_event_id` from persistent `event.jsonl` to backfill. Ordering preserved in the persistent log; only the live stream is lossy under sustained backpressure. |
| **D8** | Crash safety | (a) intent-WAL (record intent before apply); (b) two-phase commit; (c) snapshot-then-mutate-then-fsync; (d) **outcome-WAL (record post-apply diff)** | **(d) — outcome-WAL (revised 2026-05-18 per XB12 / Q10)** | ~~Original pick (a) intent-WAL fails on non-deterministic mutations: §5.6 step 2.b re-ran `apply + validate + write` on replay, which invokes `datetime.now()` / UUID gen / `git rev-parse HEAD` → replay produces a different envelope with different event_id; rename-WAL-to-applied-but-crash-before-fsync corrupts event.jsonl audit replay.~~ **Per XB12 + Q10 (2026-05-18): switch to outcome-WAL**. Capture post-apply state diff / full envelope payload (not mutation intent) in `<local-path>`; on commit rename to `.applied.json`; on replay: if `.applied.json` exists, re-issue *that exact* envelope; never re-execute mutator. If only `.pending.json` exists, treat as failed (operator re-issues; idempotency-key short-circuits to surface the failed run). event.jsonl audit replay stays canonical. |
| **D9** | Version-skew handling | (a) hard fail with structured envelope naming the upgrade command; (b) protocol-version negotiation (downgrade newer side); (c) tolerant of N-1 minor version | **(a) — hard fail, structured `protocol_version_mismatch` envelope** | Negotiation adds N×M test surface for every (CLI, daemon) pair; v0.3-v0.5 has no shipping channel guarantee yet [3:89-94]. Hard fail with `status=blocked`, `code=protocol_version_mismatch`, `details={cli_version, daemon_version, upgrade_command}` is unambiguous. CLI prints the upgrade command; operator runs it. Future v0.6+ may revisit if real shipping channels need transient cross-version coexistence. |
| **D10** | Concurrency model | (a) asyncio + `loop.run_in_executor` for mutator; (b) threading-only; (c) Python 3.14 free-threaded build | **(a) — asyncio JSON-RPC + threaded executor for the portalocker single-writer mutator + validator traversal** | Per F5 + D49 [3:77-87, 3:144-145] this is the locked pick. `asyncio.start_unix_server` handles JSON-RPC; mutator runs in a single worker thread so portalocker is naturally serial. Validator (~787 LOC traversal [3:33]) runs inside that thread too. Free-threaded Python 3.14 retained for v0.5 if validator hot loop bottlenecks [3:21]. |
| **D11** | Idle-timeout default | (a) 60 s; (b) 300 s (per V1); (c) 900 s; (d) configurable, no default | **(b) — 300 s (5-minute Anthropic prompt-cache TTL alignment)** | V1 [1:26] locks 300 s default. 5-minute alignment also matches the Anthropic prompt-cache TTL [4:208] so a warm-daemon dispatch reuses the cached prefix on retry. Configurable via `daemon.idle_timeout_seconds` in `<local-path>`. |
| **D12** | Runtime-fallback retry semantics (V5) | (a) exponential backoff per-runtime before falling through to next; (b) immediate fall-through to next runtime in ladder; (c) hybrid: short backoff for `RUNTIME_RATE_LIMIT`, immediate for others | **(c) — hybrid: backoff only on `RUNTIME_RATE_LIMIT`, immediate fall-through for `RUNTIME_TIMEOUT` / `RUNTIME_SERVER_ERROR` / `RUNTIME_API_ERROR`** | 429 means "the same runtime will recover in N seconds when its rate window refreshes" [7:130-133] — backoff + retry on the same runtime first (one retry at `retry_after` from the 429 header, then fall through). 5xx + timeout + generic API error mean the runtime *did* try and failed; fall through immediately to the next runtime so the wave keeps moving. Auth-error halts the wave (`BLOCKED`) and emits an operator-notify event — never falls through silently. Configurable via `runtime.fallback.retry_policy: hybrid|backoff|immediate`. Open question: should the 429 retry budget be wall-clock-capped (e.g. max 90 s of backoff before fall-through)? §8 Q3. |
| **D13** | Session-handle TTL after wave close (V8) | (a) drop on wave-close immediately; (b) 1-day TTL; (c) 7-day TTL; (d) never drop (compact on next phase close) | **(b) — 1-day TTL after wave close** | Audit replay (C01 §5.6 [2:1277-1325]) needs the session_id to reconstruct dispatch history for at least the most recent phase. Daily TTL covers same-day reopen + bug investigation. 7-day TTL accumulates session-log paths that may be deleted by Claude Code / Codex on their own retention sweep. Configurable via `daemon.session_handle_ttl_seconds: 86400`. The session-handle row is just a pointer; the source-of-truth log lives in `<local-path>` [1:244] (Claude Code) etc. |
| **D14** | Mutation envelope shape | (a) extend current `Envelope` [2:572-583] + `EventPayload` [2:557-568]; (b) new `MutationEnvelope` Pydantic class for daemon-side; (c) generic JSON-RPC payload | **(a) — extend the current Envelope** | The Envelope already carries `schema_version`, `id`, `kind`, `scope_id`, `created_at`, `summary`, `payload`, `blob_refs`, `artifact_ids` [2:572-583]. C02 adds `protocol_version` (daemon-protocol versioning) + `idempotency_key` (V5 cross-runtime re-issue [1:392]) at the JSON-RPC framing layer, outside the Envelope body. Keeps Pydantic models stable. C03 owns the typed `Mutation` discriminated union per `payload` [2:588]; C02 wraps it in JSON-RPC. |
| **D15** | Cold-spawn UX | (a) silent — CLI hides daemon spawn; (b) progress indicator on first call ("starting eawfd..."); (c) opt-in verbose | **(a) — silent, with `--verbose` opt-in** | V1 [1:48] explicitly mandates transparent auto-spawn. Operator runs `eawf wave claim`; either daemon was already up (no message), or it wasn't (also no message; spinner briefly shows under 400 ms latency). `--verbose` surfaces the spawn step for debugging. Cold-spawn failures (port collision, write permission, etc.) surface as `daemon_spawn_failed` envelope. |

## 5. Proposed schema / API / protocol

### 5.1 Process topology

The deployed system:

```
                                ┌─────────────────────────────────┐
                                │   operator + agent personas     │
                                │   (terminal / TUI / web stub)    │
                                └──────────────┬──────────────────┘
                                               │
                       ┌───────────────────────┼───────────────────────┐
                       │ CLI (`uv run eawf …`) │ TUI (`uv run eawf`)   │
                       │  one-shot subprocess  │  long-lived process   │
                       └──────────┬────────────┴──────────────────────┘
                                  │ JSON-RPC over UDS / named pipe
                                  v
                  ┌────────────────────────────────────────┐
                  │              eawfd                     │
                  │  ┌──────────────────────────────────┐  │
                  │  │ asyncio JSON-RPC listener        │  │
                  │  │ (one task per connection)        │  │
                  │  └────────────┬─────────────────────┘  │
                  │               │                        │
                  │  ┌────────────v─────────────────┐      │
                  │  │ method dispatcher             │      │
                  │  │ state.* / event.* / agent.*   │      │
                  │  │ daemon.* / wave.* / runtime.* │      │
                  │  └────┬──────────────┬───────────┘      │
                  │       │              │                  │
                  │       │              │ subscribe.*      │
                  │       v              v                  │
                  │  ┌─────────┐  ┌──────────────────┐      │
                  │  │ mutator │  │ event bus        │      │
                  │  │ thread  │  │ (per-subscriber  │      │
                  │  │ (`run_  │  │  bounded queue)  │      │
                  │  │  in_    │  └────────┬─────────┘      │
                  │  │  exec.`)│           │ push frames    │
                  │  └────┬────┘           │                │
                  │       │                │                │
                  │  ┌────v───────┐  ┌─────v────────┐       │
                  │  │ portalock  │  │ subscriber    │       │
                  │  │ + WAL      │  │ list          │       │
                  │  └────┬───────┘  └───────────────┘      │
                  │       │                                  │
                  │  ┌────v───────────────────────────┐      │
                  │  │ state.json + event.jsonl       │      │
                  │  │ audit.jsonl + report jsonls    │      │
                  │  │ <local-path>          │      │
                  │  │ .ea/config.yaml + <local-path> │      │
                  │  └────────────────────────────────┘      │
                  │                                          │
                  │  ┌──────────────────────────────────┐    │
                  │  │ dispatcher (runtime worker map)  │    │
                  │  │ wave -> (runtime, session_id,    │    │
                  │  │           subprocess pid)        │    │
                  │  └────────┬──────────────┬──────────┘    │
                  │           │              │               │
                  └───────────┼──────────────┼───────────────┘
                              │ spawn        │ spawn
                              v              v
                      ┌────────────┐  ┌────────────┐
                      │ claude -p  │  │ codex exec │ ...
                      │ (subproc)  │  │ (subproc)  │
                      └────────────┘  └────────────┘
                              │              │
                       per-runtime session-log writes (read-only by daemon):
                              v              v
                  <local-path>   <local-path>
```

**Component roles.**

- **CLI** — one-shot subprocess. Reads `eawfd` PID / socket; auto-spawns daemon if absent (V1 [1:26]); issues JSON-RPC; exits. Read-only verbs MAY bypass daemon per V1 carve-outs [1:26-30].
- **TUI** — long-lived; subscribes to `event.subscribe` and renders push frames. Mtime-poll fallback if daemon down per C06 axis [1:619]. Never mutates `state.json` directly [4:236-237].
- **Web stub** — same shape as TUI but on a WebSocket bridge proxying daemon JSON-RPC (C06 deferred).
- **eawfd** — single process per OS user. Owns the mutator surface (`state.json`, `event.jsonl`, `audit.jsonl`, role-specific report jsonls, `<local-path>`, config layers). Dispatcher subsystem spawns per-runtime subprocesses for `/flow` execute waves; tracks `(wave, attempt) → (runtime, session_id, subprocess_pid)`. Does NOT host LLM execution in-process — the vendor CLIs are the workers.
- **Agent runtimes** — vendor CLI subprocesses (`claude -p`, `codex exec`, OpenCode equivalent). Read-only consumers of daemon state; emit session logs to runtime-managed paths under `<local-path>` / `<local-path>` [1:244-247].

### 5.2 IPC protocol

Wire format: **JSON-RPC 2.0** [22].

#### 5.2.1 Framing

Each frame is one line of JSON terminated by `\n`. Producer + consumer both line-buffer. Lines must not contain literal `\n` inside JSON values (Python `json.dumps` already escapes; `orjson.dumps` also escapes).

Request frame:

```json
{"jsonrpc":"2.0","id":"<uuid>","method":"state.read","params":{...},"protocol_version":"1"}
```

Response frame (success):

```json
{"jsonrpc":"2.0","id":"<uuid>","result":{...},"protocol_version":"1"}
```

Response frame (error):

```json
{"jsonrpc":"2.0","id":"<uuid>","error":{"code":-32602,"message":"<text>","data":{"envelope":<OutputEnvelope>}},"protocol_version":"1"}
```

Notification (no `id` field — used for push frames from daemon to subscriber):

```json
{"jsonrpc":"2.0","method":"event.push","params":{"event":<Envelope>}}
```

**Idempotency key.** Mutating methods carry an optional top-level `idempotency_key` (string, UUID v4 form). Daemon deduplicates within a 60-second window; same key + same params returns the cached result + a `idempotent_replay: true` flag in the response. Required for V5 cross-runtime re-issue [1:392].

**Protocol version.** Top-level `protocol_version` on every frame. Daemon rejects mismatched-major mismatches with error code `-32004` (custom: `protocol_version_mismatch`). Minor-version skew tolerated forward-compatibly within v0.3-v0.5; new minor methods unknown to older daemon return `-32601` `method_not_found`.

#### 5.2.2 Error code table

Extends JSON-RPC reserved range with eawfd-specific codes in `-32000..-32099`:

| Code | Name | Trigger |
|---|---|---|
| -32700 | parse error | bad JSON |
| -32600 | invalid request | missing required fields |
| -32601 | method not found | unknown method, or method exists on newer protocol |
| -32602 | invalid params | wrong shape / missing required arg |
| -32603 | internal error | uncaught exception in daemon |
| -32000 | unauthorized | peer-credential check failed |
| -32001 | lock conflict | portalocker timed out |
| -32002 | validation failed | post-mutation schema or invariant violation |
| -32003 | scope mismatch | request scope_id doesn't match daemon's working scope |
| -32004 | protocol version mismatch | major-version skew between CLI and daemon |
| -32005 | resource exhausted | concurrent-dispatch cap or EU budget hit |
| -32006 | runtime unavailable | every runtime in fallback ladder failed |
| -32007 | session expired | V8 `--continue` failed; daemon will fall back to fresh |
| -32008 | subscription dropped | event bus per-subscriber queue overflow (D7) |
| -32009 | daemon shutting down | idle-timeout grace window or service-stop |

All errors carry a `data.envelope: OutputEnvelope` payload so the CLI can render the existing envelope chassis [2:9 envelope.py] without per-error-code branching.

### 5.3 Method catalog

All RPC method names use dotted namespace `<noun>.<verb>`.

#### 5.3.1 state.*

| Method | Params | Result | Notes |
|---|---|---|---|
| `state.read` | `{scope_id?: str, fields?: list[str]}` | `{state: State, version: str}` | Read-only; returns a typed `State` payload [2:1573]. `version` is the digest of the loaded state for cache-validation. Bypasses daemon when caller passes `--daemonless` flag (recovery shell). |
| `state.mutate` | `{mutation: Mutation, idempotency_key?: str}` | `{event: Envelope, before_version: str, after_version: str}` | The single canonical mutator path. `Mutation` is C03's typed discriminated union (one variant per state-mutating CLI verb; ~25 types per features-deep [4:264-267]). Validates → writes WAL → applies → re-validates → atomic-writes → appends event → emits push to subscribers → releases lock. |
| `state.subscribe` | `{scope_id?: str, since_version?: str, event_kinds?: list[StoreKind]}` | streaming `event.push` notifications | Stream-receive method. Daemon pushes a `event.push` notification per matched envelope until subscriber disconnects. `since_version` lets subscriber catch up on missed events from `event.jsonl`. |
| `state.validate` | `{state?: State, against?: list[str]}` | `{report: ValidationReport}` | Runs `validate_state` [11] against a candidate `State` (or the on-disk state). Used by `eawf validate`; daemonless path bypasses RPC. |
| `state.digest` | `{}` | `{version: str}` | Returns the digest of the current on-disk state. Used by TUI mtime-poll fallback per C06 axis [1:619]. |

#### 5.3.2 event.*

| Method | Params | Result | Notes |
|---|---|---|---|
| `event.subscribe` | `{since_id?: str, kinds?: list[StoreKind], scope_id?: str}` | streaming | Synonym for `state.subscribe` — kept for vocabulary clarity. |
| `event.list` | `{scope_id?: str, since?: str, until?: str, kinds?: list[StoreKind], limit?: int}` | `{events: list[Envelope]}` | Bounded read from `event.jsonl`; used by TUI scroll-back. |
| `event.show` | `{event_id: str}` | `{event: Envelope}` | Single-event fetch by id. |

#### 5.3.3 agent.*

| Method | Params | Result | Notes |
|---|---|---|---|
| `agent.dispatch` | `{wave_id: str, runtime?: str, session_policy?: "fresh"\|"continue"\|"hybrid"}` | `{session_id: str, attempt: int, pid: int}` | Daemon spawns the runtime subprocess for `/flow`. `session_policy` overrides skill manifest default per V8 [1:249]. Returns when subprocess is up; subprocess output streams via `event.push` `dispatch_log` envelopes. |
| `agent.session` | `{wave_id: str, attempt?: int}` | `{sessions: dict[int, SessionAttempt]}` | Inspect session-handle table for a wave. |
| `agent.kill` | `{wave_id: str, attempt: int, signal?: "term"\|"kill"}` | `{killed: bool, signal: str}` | SIGTERM (default) → grace window → SIGKILL ladder. |

#### 5.3.4 wave.*

| Method | Params | Result | Notes |
|---|---|---|---|
| `wave.switch` | `{wave_id: str, to: str, reason?: str}` | `{event: Envelope}` | Operator manual runtime-switch per V5 [1:148-149]. Emits `runtime_switched` event with `cause=manual_override`. |
| `wave.claim` | `{wave_id: str, out_of_order?: bool}` | `{wave: Wave}` | Wraps `eawf wave claim` semantics via daemon. |
| `wave.release` | `{wave_id: str, reason?: str}` | `{wave: Wave}` | Wraps `eawf wave release` semantics. |

#### 5.3.5 daemon.*

| Method | Params | Result | Notes |
|---|---|---|---|
| `daemon.ping` | `{}` | `{pid: int, version: str, protocol_version: str, started_at: str, idle_for_seconds: float}` | Liveness + version probe. |
| `daemon.status` | `{}` | `{pid, version, protocol_version, started_at, active_subscriptions: int, in_flight_mutations: int, last_event_id: str, uptime_seconds: float}` | Operator-facing status; backs `eawf daemon status`. |
| `daemon.shutdown` | `{drain?: bool, timeout_seconds?: int}` | `{shutdown_at: str}` | Operator-initiated stop; `drain=true` waits for in-flight to complete. Backs `eawf daemon stop`. |
| `daemon.reload_config` | `{}` | `{config_version: str}` | Re-reads `<local-path>` + `.ea/config.yaml`. Hot-config-reload is allowed (config-only); hot-code-reload is NOT (NG3). |
| `daemon.replay_wal` | `{}` | `{replayed: int, rolled_back: int}` | Manually trigger WAL replay; primarily a debug verb. |

#### 5.3.6 runtime.*

| Method | Params | Result | Notes |
|---|---|---|---|
| `runtime.list` | `{}` | `{runtimes: list[Runtime]}` | Lists configured runtimes from `runtime.preference` [1:139]; status per §5.13 [2:1153-1177]. |
| `runtime.set_preference` | `{preference: list[str], scope: "user"\|"repo"\|"wave", wave_id?: str}` | `{config_version: str}` | Mutates `runtime.preference` at the named layer. |
| `runtime.health` | `{runtime_id: str}` | `{status: RuntimeStatus, last_error?: str, last_success_at?: str}` | Status query (advisory; V5 is reactive not probed [1:131-136], so daemon does NOT run health probes — this method returns last observed state). |

### 5.4 Auth + security

**POSIX (Linux, macOS, BSD).** Socket file at `<runtime_dir>/eawfd.sock`, permission `0600`, owned by the daemon-spawning UID. On connection acceptance, daemon reads peer credentials:

- Linux: `socket.getsockopt(SOL_SOCKET, SO_PEERCRED, struct.calcsize("3i"))` returns `(pid, uid, gid)`. Compare `uid` to daemon's UID; reject mismatch with `-32000 unauthorized`.
- macOS/BSD: `socket.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, ...)` returns `xucred`. Same UID comparison.

**Windows.** Named pipe `\\.\pipe\eawfd-<user>`. DACL allows only the owning user SID full control; everyone else denied. Post-connect, daemon uses `pywin32` `win32security.GetSecurityInfo(pipe_handle, SE_KERNEL_OBJECT, OWNER_SECURITY_INFORMATION)` to verify the connecting SID matches the daemon's SID; reject mismatch with `-32000`.

**Prompt-injection defense at IPC boundary.** Daemon validates every incoming RPC against a Pydantic v2 model with `ConfigDict(extra="forbid")` [AGENTS rule 2 [13]] before dispatching. Unknown fields → `-32602`. The mutation `payload` is itself a typed discriminated union (C03), so a malicious runtime cannot smuggle a `phase.close` masquerading as a `memory.write`.

**No network exposure.** Daemon binds to UDS / named pipe only. No `socket.bind(('0.0.0.0', port))` code path exists. SSH-session use is supported via local UDS in the SSH session's UID context.

**Secrets-hygiene.** Daemon NEVER logs raw `Mutation` payloads (some carry text); structured logs include only `mutation_kind`, `scope_id`, `wave=`/`iter=`/`phase=` keys per AGENTS naming-conventions [13]. Error envelopes scrub local paths via the existing `scrub.scan` patterns [3:35].

### 5.5 Spawn model + idle timeout

**On-demand auto-spawn (V1 [1:26]).** CLI startup sequence:

```
1. Resolve runtime_dir per D5 (XDG on Linux, <local-path> on macOS, named-pipe on Windows).
2. Read <runtime_dir>/eawfd.pid (file exists? PID alive? owned by current UID?).
3. If alive: connect to socket; on connect-refused, treat as stale.
4. If stale or absent:
   a. fork()/CreateProcess() child detached from CLI (POSIX: double-fork → setsid → close fds → exec; Windows: CreateProcess with DETACHED_PROCESS).
   b. Child writes <runtime_dir>/eawfd.pid (atomic — tempfile + rename); writes <runtime_dir>/eawfd.sock; starts asyncio.start_unix_server; logs to <runtime_dir>/eawfd.log (rotated daily, kept 7 days).
   c. Parent CLI polls socket for up to 5 s; connects; proceeds.
5. CLI issues JSON-RPC; receives response; exits.
6. Daemon updates last_activity_at on each method call (excluding subscribe).
7. Idle-timeout watchdog (asyncio task) checks every 30 s: if no_subscribers AND no_in_flight_mutations AND (now - last_activity_at > idle_timeout): self-shutdown.
```

**PID file shape.** Plain text:

```
<pid>\n<protocol_version>\n<started_at_iso>\n
```

**Stale-PID detection.** Same algorithm as the existing `lock.stale.is_stale` [9] — read the PID, verify the process exists and is owned by the same UID, fall through to a write-test on the lock file.

**Daemon graceful shutdown.** SIGTERM (POSIX) / CTRL_BREAK_EVENT (Windows): drain in-flight mutations (wait up to 30 s) → close subscriptions with `subscription_dropped` envelope `reason=daemon_shutdown` → fsync state.json → unlink PID + socket → exit 0.

**Daemon SIGKILL recovery.** No graceful shutdown. WAL replay on next startup (§5.6).

### 5.6 Supervisor + crash safety (WAL)

**Write-ahead log path.** `<runtime_dir>/wal/<mutation_id>.<status>.json` where `status ∈ {pending, applied, fsynced}`.

**Transaction lifecycle:**

```
1. CLI calls state.mutate(mutation, idempotency_key=K).
2. Daemon mutator thread receives the call, acquires portalock(state.json, timeout=5).
3. Write WAL pending: <wal_dir>/<id>.pending.json containing {mutation, idempotency_key, started_at, before_state_version}.
4. Read+decode state.json → validate(strict_optional=False) → fail → reply -32002.
5. Apply Mutation: deterministic transformation on State (C03 owns).
6. Re-validate post-mutation state → fail → reply -32002 (leave state.json unchanged, WAL pending stays for next-startup detection).
7. Atomic-write new state.json (tempfile + os.replace + parent-dir fsync via _write_payload [12]).
8. Append event row to event.jsonl with after_state_version set.
9. Rename WAL <id>.pending.json → <id>.applied.json.
10. fsync state.json + event.jsonl.
11. Rename WAL <id>.applied.json → <id>.fsynced.json.
12. unlink WAL <id>.fsynced.json (kept for 1 hour in <wal_dir>/done/ then removed; debugging window).
13. Push event to subscribers (event bus).
14. Release portalock; return success to CLI.
```

**Startup replay algorithm.**

```
on daemon startup, after socket bind, before accepting connections:
1. List <wal_dir>/*.json.
2. For each <id>.pending.json:
   - Read mutation + before_state_version.
   - Compare on-disk state.json digest to before_state_version.
   - If match: this transaction never committed → roll-forward attempt:
     - Re-run apply + validate + write.
     - On success: rename to .fsynced.json.
     - On failure (validation): mark as poisoned (rename to <id>.poisoned.json under <wal_dir>/poisoned/), emit incident envelope, continue startup.
   - If mismatch: state has moved since the WAL was written; this is an apply-then-crash-before-WAL-update case. Verify the matching event exists in event.jsonl; if yes, mark .fsynced.json (no replay needed); if no, mark .poisoned.json.
3. For each <id>.applied.json:
   - state.json is consistent; rename to .fsynced.json (assume fsync was reached); emit a `wal_recovery` envelope on first subscriber connect.
4. For each <id>.fsynced.json older than 1 hour: unlink.
```

**Poisoned WAL handling.** `eawf daemon replay-wal --inspect` lists poisoned mutations; operator decides retry / abandon / manual-fix. Never automatic re-apply of a poisoned WAL.

### 5.7 Subscription bus + backpressure

**Subscriber model.** Each connected client may issue at most one `event.subscribe` per connection. Subscription records `(connection_id, scope_filter, kind_filter, since_version, queue: deque[Envelope, maxlen=1024])`.

**Push path.** Mutator thread, after successful WAL fsync + state-write, calls `bus.publish(envelope)`. Publish:

```
for sub in subscribers:
    if not envelope.matches(sub.scope_filter, sub.kind_filter):
        continue
    if len(sub.queue) >= sub.queue.maxlen:
        sub.disconnect(reason="overflow", code=-32008)
        continue
    sub.queue.append(envelope)
    sub.notify()
```

**Subscriber receive.** The subscriber's asyncio task awaits the queue and writes JSON-RPC `event.push` notifications down the connection. If the subscriber's TCP buffer is full, the write blocks; the queue grows; overflow disconnects.

**Reconnect / catch-up.** On reconnect, subscriber passes `since_version=<last_version>`. Daemon replays from `event.jsonl` between `since_version` and the current head, then continues live. Catch-up reads are bounded (default 10000 events) — beyond which the daemon disconnects with `-32008 catch_up_too_large` and requires the subscriber to fetch a state snapshot first.

**Backpressure for slow disk.** Mutator path doesn't wait on the bus — `publish` is non-blocking. If subscribers can't keep up, they disconnect; mutations are never delayed.

### 5.8 Resource limits + budget broker

Profile-conditional config (C08 owns the layered-config algorithm); C02 lists the daemon-side enforcement points.

| Limit | Config key | Default | Enforced where |
|---|---|---|---|
| Concurrent dispatch cap | `daemon.max_concurrent_dispatch` | 4 | `agent.dispatch` blocks (`-32005 resource_exhausted`) when in-flight count hits cap. |
| Per-wave wall-clock cap | `daemon.wave_wall_clock_cap_seconds` | 1800 (30 min per [7:112]) | dispatcher asyncio task races a `asyncio.wait_for`; SIGTERM at cap, SIGKILL +15 s. |
| Per-runtime budget broker | `runtime.<id>.eu_cap_per_phase` | none | dispatch refuses to spawn when `phase.eu_consumed + estimate > eu_cap`. Soft breaker (warn-only) for the first four weeks per roadmap-synthesis [7:127-128] — config flag `breaker_mode: warn|hard` |
| OOM kill threshold | `daemon.subprocess_rss_kb_kill` | 4194304 (4 GB) | dispatcher monitors child RSS every 10 s; SIGKILL on overshoot; emits `subprocess_oom_killed` event. |
| Vendor 429 auto-pause | `runtime.<id>.auto_pause_on_429` | true | runtime-fallback handler (§5.12) emits `runtime_paused` event; dispatcher halts new spawns until rate window resets (parse `Retry-After` or fall through to next runtime per D12). |

**Budget broker source-of-truth.** Per-spawn `usage` blocks from runtime session logs (Claude Code `<local-path>` [1:244], Codex `<local-path>` [1:245]) per roadmap-synthesis [7:119-123]. Daemon reads these read-only after dispatch completion and emits `dispatch_cost` events.

### 5.9 Version-skew handling

**Protocol version field.** Every JSON-RPC frame carries `protocol_version: "1"` at top level (string, semver-like; v0.3-v0.5 stays at `"1"`). Daemon handshake on first request:

```
1. CLI sends request with protocol_version field.
2. Daemon compares to its own protocol_version (single source of truth: src/eawf/daemon/__init__.py PROTOCOL_VERSION constant).
3. If major-version mismatch: respond -32004 protocol_version_mismatch with data={cli_version, daemon_version, upgrade_command: "uv tool upgrade eawf"}.
4. If minor-version mismatch (cli newer than daemon, daemon doesn't know the method): respond -32601 method_not_found (normal JSON-RPC).
5. If minor-version mismatch (cli older than daemon, daemon supports method): proceed normally.
```

**CLI handling.** On `-32004`, CLI prints (to stderr + structured `OutputEnvelope`):

```
ERROR: daemon at <runtime_dir>/eawfd.sock is protocol_version <X>; this CLI expects <Y>.

Run `<upgrade_command>` then retry. If you need to keep the daemon running on the old protocol, set EAWF_DAEMONLESS=1 to bypass for read-only commands.
```

`EAWF_DAEMONLESS=1` short-circuits the daemon connection attempt — supported only for read-only verbs (V1 carve-out [1:26-30]). Mutating verbs fail with `daemon_required` envelope.

### 5.10 Per-OS service-registration spec (V6)

#### 5.10.1 Linux — systemd user unit

Template at `templates/eawfd.service.j2`:

```ini
[Unit]
Description=eawf coordinator daemon (eawfd)
After=default.target

[Service]
Type=notify
ExecStart=/usr/bin/env uv tool run eawf daemon run --foreground
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
Environment="EAWF_RUNTIME_DIR=%t/eawfd"

[Install]
WantedBy=default.target
```

Install path: `<local-path>`. `%t` expands to `$XDG_RUNTIME_DIR` per D5.

**`eawf daemon enable` flow.**

```
1. Read template; render to <local-path>
2. Run `systemctl --user daemon-reload`.
3. Run `systemctl --user enable --now eawfd.service`.
4. Wait up to 10 s for the daemon to write its PID file.
5. Emit `daemon_service_enabled` envelope.
```

**`eawf daemon disable` flow.**

```
1. Run `systemctl --user disable --now eawfd.service`.
2. unlink <local-path>
3. Run `systemctl --user daemon-reload`.
4. Emit `daemon_service_disabled` envelope.
```

Idempotent: disable on a never-enabled state is a no-op (no error).

#### 5.10.2 macOS — launchd LaunchAgent

Template at `templates/dev.eawf.eawfd.plist.j2`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>dev.eawf.eawfd</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env</string>
        <string>uv</string>
        <string>tool</string>
        <string>run</string>
        <string>eawf</string>
        <string>daemon</string>
        <string>run</string>
        <string>--foreground</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{{ runtime_dir }}/eawfd.log</string>
    <key>StandardErrorPath</key>
    <string>{{ runtime_dir }}/eawfd.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>EAWF_RUNTIME_DIR</key>
        <string>{{ runtime_dir }}</string>
    </dict>
</dict>
</plist>
```

Install path: `<local-path>`. `{{ runtime_dir }}` resolves to `<local-path>` per D5.

**`eawf daemon enable` flow.**

```
1. Render template to <local-path>
2. Run `launchctl bootstrap gui/$UID <local-path>`.
3. Run `launchctl enable gui/$UID/dev.eawf.eawfd`.
4. Run `launchctl kickstart gui/$UID/dev.eawf.eawfd`.
5. Wait for PID file; emit envelope.
```

**`eawf daemon disable` flow.**

```
1. Run `launchctl bootout gui/$UID/dev.eawf.eawfd`.
2. unlink <local-path>
3. Emit envelope.
```

#### 5.10.3 Windows — pywin32 Service (primary) + NSSM (fallback documented)

Primary path: a `pywin32` `win32serviceutil.ServiceFramework` subclass at `src/eawf/daemon/win_service.py`. Sketch:

```python
import win32serviceutil
import win32service
import win32event
import servicemanager
import asyncio

class EawfdService(win32serviceutil.ServiceFramework):
    _svc_name_ = "eawfd"
    _svc_display_name_ = "eawf coordinator daemon"
    _svc_description_ = "Single-user coordinator for the eawf framework."

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.loop: asyncio.AbstractEventLoop | None = None

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(self._graceful_shutdown(), self.loop)

    def SvcDoRun(self) -> None:
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        from eawf.daemon.main import run_daemon
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(run_daemon(stop_event=self.stop_event))
        finally:
            self.loop.close()

    async def _graceful_shutdown(self) -> None:
        from eawf.daemon.main import shutdown
        await shutdown(drain=True, timeout=30)


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(EawfdService)
```

**Install command (per-user):**

```
python -m eawf.daemon.win_service install --startup auto
```

**`eawf daemon enable` Windows flow.**

```
1. Verify pywin32 is importable; if not, hint NSSM fallback below.
2. Run `python -m eawf.daemon.win_service install --startup auto`.
3. Run `python -m eawf.daemon.win_service start`.
4. Apply DACL to \\.\pipe\eawfd-<user> restricting to user SID (post-spawn, via win32security.SetSecurityInfo).
5. Emit envelope.
```

**NSSM fallback (documented, not primary).** Bundled `nssm.exe` in `tools/nssm-2.24/` of the eawf wheel. Operator runs:

```
nssm install eawfd "C:\Python314\Scripts\uv.exe" tool run eawf daemon run --foreground
nssm set eawfd AppEnvironmentExtra EAWF_RUNTIME_DIR=%LOCALAPPDATA%\eawf\runtime
nssm start eawfd
```

**`eawf daemon disable` Windows flow.**

```
1. Run `python -m eawf.daemon.win_service stop`.
2. Run `python -m eawf.daemon.win_service remove`.
3. Emit envelope.
```

### 5.11 Portalocker per-OS matrix (V6)

| OS | Underlying lock primitive | portalocker uses | Tested in CI |
|---|---|---|---|
| Linux | `fcntl(2)` advisory lock (F_SETLK) | `portalocker.LOCK_EX \| LOCK_NB` | yes — Ubuntu 24.04 runner |
| macOS | `fcntl(2)` advisory lock (BSD `flock(2)` underneath) | same | yes — macOS 14 runner |
| Windows | `LockFileEx` (mandatory) | `portalocker.LOCK_EX \| LOCK_NB` (wrapped) | yes — windows-2022 runner |

**CI test plan.**

`.github/workflows/portalocker_matrix.yaml`:

```yaml
name: portalocker-matrix
on: [push, pull_request]
jobs:
  matrix:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-24.04, macos-14, windows-2022]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --frozen
      - run: uv run pytest tests/integration/test_portalock_cross_os.py -v
      - run: uv run pytest tests/integration/test_daemon_on_demand_spawn.py -v
```

**Cross-OS test cases.**

- `test_acquire_release_basic` — single-process acquire + release, lockfile created and removed.
- `test_concurrent_acquire_blocks` — two processes; second blocks within timeout; succeeds after first releases.
- `test_stale_lock_steals` — PID-dead lockfile stolen on next acquire (already in `lock.stale` [9]).
- `test_lock_path_with_unicode` — repo path containing non-ASCII chars; lockfile created with proper encoding (Windows: UTF-16 path; POSIX: UTF-8).
- `test_lock_under_xdg_runtime_dir` — Linux only; verify `$XDG_RUNTIME_DIR` honored.
- `test_lock_under_named_pipe_paths` — Windows only; verify `\\.\pipe\` works.

**Windows file-ID portability concern (V6 [1:173]).** state-history-cache-design [8:266-277] raised that cache keys derived from OS file IDs are not portable across Windows file moves. Resolution: cache keys derive from `sha256(repo_path_canonical + relative_path)`, never from `os.stat().st_ino` (which on Windows is `nFileIndexLow/High` and changes on file move). The daemon never reads file IDs.

### 5.12 Runtime fallback (V5) state machine

```
                     ┌──────────────────────────────────┐
                     │   dispatcher.dispatch(wave_id)   │
                     │   primary = preference[0]        │
                     └──────────────┬───────────────────┘
                                    │
                                    v
                     ┌──────────────────────────────────┐
                     │   RUNNING on primary             │
                     │   sessions[attempt] populated    │
                     └──────────────┬───────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────────────┐
              │ success             │ RUNTIME_RATE_LIMIT 429       │ RUNTIME_TIMEOUT
              │                     │                              │ RUNTIME_SERVER_ERROR
              │                     │                              │ RUNTIME_API_ERROR
              v                     v                              v
       ┌────────────┐     ┌─────────────────────────┐    ┌───────────────────────┐
       │   CLOSED   │     │ honor Retry-After      │    │ emit runtime_switched │
       │   commit   │     │ sleep min(rA, cap=90s) │    │   from=A, to=B,       │
       │   stored   │     │ retry on same runtime  │    │   cause=<error_class> │
       └────────────┘     │ once                   │    │ open SessionAttempt   │
                          │  ┌─────┴────────────┐  │    │ on runtime B          │
                          │  │ ok → CLOSED      │  │    │ DispatchAnnotation    │
                          │  │ fail → next      │  │    │   note=               │
                          │  │   runtime in     │  │    │   "switch on error"   │
                          │  │   preference     │  │    └─────────┬─────────────┘
                          │  │   ladder         │  │              │
                          │  └────┬─────────────┘  │              │
                          └───────┼────────────────┘              │
                                  v                                v
                          ┌─────────────────────────────────────────────────┐
                          │   try next runtime in preference ladder         │
                          │   if exhausted → halt wave (BLOCKED) +          │
                          │     emit operator_notify envelope               │
                          │     code=-32006 runtime_unavailable             │
                          └────────────────────┬────────────────────────────┘
                                               │ recovery
                                               v
                                  Operator runs:
                                  eawf wave switch <id> --to <runtime>
                                  → flips runtime; resumes IN_PROGRESS
```

**Error classes (C07 enforces consistency across runtime adapters per V5 [1:130]).**

| Class | HTTP / signal | Fallback action |
|---|---|---|
| `RUNTIME_RATE_LIMIT` | 429 | honor `Retry-After` (cap 90 s); retry same runtime once; fall through on second 429 |
| `RUNTIME_SERVER_ERROR` | 500-599 | immediate fall-through to next runtime |
| `RUNTIME_TIMEOUT` | wall-clock cap hit | immediate fall-through |
| `RUNTIME_API_ERROR` | 400-499 (excl 429) | immediate fall-through |
| `RUNTIME_AUTH_ERROR` | 401, 403, missing token | HALT wave with `BLOCKED`; emit `runtime_auth_failed` event; never fall through (auth ≠ availability) |

**`runtime_switched` event payload.**

```python
class RuntimeSwitchedPayload(_StrictModel):
    wave_id: WaveIdStr
    attempt: int
    runtime_from: str
    runtime_to: str
    cause: str                  # 'RUNTIME_RATE_LIMIT' | 'RUNTIME_SERVER_ERROR' | ... | 'manual_override'
    error_detail: str | None    # raw error from runtime adapter (scrubbed for PII)
    occurred_at: UtcDatetime
    idempotency_key: str        # carries across the re-issue
```

**Manual override surface.** `eawf wave switch <wave-id> --to <runtime> --reason <text>` issues a `wave.switch` RPC (§5.3.4); daemon emits `runtime_switched` with `cause=manual_override`. Operator can force a switch even if the current runtime is healthy.

### 5.13 Session-handle tracking (V8) schema

C01 §5.3.5 [2:362-381] reserved the field shape. C02 makes the daemon the canonical writer.

**Wave model extensions (already in C01 sketch):**

```python
class Wave(_StrictModel):
    # ... existing fields per C01 §5.3.5 [2:343-365] ...
    sessions: dict[int, SessionAttempt] = {}     # attempt -> handle
    runtime_preference: list[str] | None = None  # per-wave override
    dispatch_history: list[DispatchAnnotation] = []
```

**Session attempt row.**

```python
class SessionAttempt(_StrictModel):
    attempt: Annotated[int, Field(ge=1)]
    runtime: str                    # 'claude-code' | 'codex' | 'opencode'
    session_id: str                 # runtime-specific id (e.g. claude session uuid)
    session_log_handle: str         # OPAQUE handle (revised 2026-05-18 per XB05 / C02-I007): blob-URN or daemon-side index key — never an absolute filesystem path. Daemon resolves to a real path on demand via dedicated method; the path itself stays out of state.json and event.jsonl to satisfy AGENTS rule 16 + audit XB05.
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    exit_status: int | None = None  # subprocess exit code; null while running
    subprocess_pid: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
```

**XB14 fix (2026-05-18): SCM-to-asyncio shutdown bridge for Windows service.**

Windows Service `SvcStop` callback runs on the SCM thread; the asyncio event loop runs on a different thread. Without a `loop.call_soon_threadsafe(...)` bridge, the daemon won't shut down cleanly.

```python
class EawfdService(win32serviceutil.ServiceFramework):
    _svc_name_ = "eawfd"
    _svc_display_name_ = "EAWF Daemon"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

    def SvcDoRun(self):
        self._loop = asyncio.new_event_loop()
        self._stop_event = asyncio.Event()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self):
        # Start the listener thread (pywin32 CreateNamedPipe per XB11/Q8) and queue bridge
        # await the stop event for clean teardown
        await self._stop_event.wait()
        # teardown: cancel listeners, drain queues, close socket, exit

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        # Cross-thread signal — SCM thread → asyncio loop thread
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
```

**XB11 fix (2026-05-18): Windows pywin32 named-pipe listener in dedicated thread + asyncio queue bridge.**

```python
import threading
import asyncio
import pywintypes
import win32pipe, win32file, win32security

class WindowsPipeServer:
    PIPE_NAME = r"\\.\pipe\eawfd-{username}"

    def __init__(self, loop: asyncio.AbstractEventLoop, handler: Callable[[bytes], Awaitable[bytes]]):
        self._loop = loop
        self._handler = handler
        self._queue: asyncio.Queue[tuple[bytes, Callable]] = asyncio.Queue()
        self._listener_thread: threading.Thread | None = None
        self._shutdown = False

    def start(self):
        self._listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listener_thread.start()
        self._loop.create_task(self._dispatch_loop())

    def _listen_loop(self):
        # SCM thread: blocking pipe accept; on connect, post payload to asyncio queue via call_soon_threadsafe
        sec_attrs = self._build_user_only_sec_attrs()  # DACL restricted to owning user SID (per D4)
        while not self._shutdown:
            pipe = win32pipe.CreateNamedPipe(
                self.PIPE_NAME,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                win32pipe.PIPE_UNLIMITED_INSTANCES,
                65536, 65536, 0, sec_attrs,
            )
            win32pipe.ConnectNamedPipe(pipe, None)
            payload = win32file.ReadFile(pipe, 65536)[1]
            # Post (payload, reply_fn) to asyncio loop
            done = threading.Event()
            response_holder = {"reply": b""}
            def _reply_callback(reply_bytes: bytes):
                response_holder["reply"] = reply_bytes
                done.set()
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (payload, _reply_callback))
            done.wait()
            win32file.WriteFile(pipe, response_holder["reply"])
            win32file.CloseHandle(pipe)

    async def _dispatch_loop(self):
        while not self._shutdown:
            payload, reply = await self._queue.get()
            response = await self._handler(payload)
            reply(response)

    def stop(self):
        self._shutdown = True
        # Wake up the listener thread by connecting to its own pipe + closing
        ...

    def _build_user_only_sec_attrs(self):
        # D4: DACL restricted to owning user SID
        ...
```

Smoke harness in `.ea/local/smoke/windows-pipe-asyncio/` exercises 1k concurrent connections, shutdown via SvcStop, and PID/SID auth.

**Dispatch annotation.**

```python
class DispatchAnnotation(_StrictModel):
    attempt: Annotated[int, Field(ge=1)]
    note: DispatchNote               # enum below
    runtime_from: str | None = None
    runtime_to: str | None = None
    occurred_at: UtcDatetime
    reason: str | None = None        # free-form context (scrubbed)

class DispatchNote(StrEnum):
    FRESH_DISPATCH = "fresh_dispatch"
    CONTINUE_FROM_SESSION = "continue_from_session"
    CONTINUE_FAILED_FELL_BACK_TO_FRESH = "continue_failed_fell_back_to_fresh"
    SWITCH_ON_ERROR = "switch_on_error"           # V5 fallback
    SWITCH_MANUAL = "switch_manual"               # operator override
```

**Daemon-side routing algorithm (`agent.dispatch`):**

```python
async def dispatch(wave_id: str, runtime: str | None = None, session_policy: str = "hybrid") -> DispatchResult:
    wave = await state_read_wave(wave_id)
    runtime_id = runtime or _pick_primary_from_preference(wave)
    attempts = sorted(wave.sessions)
    is_retry = len(attempts) >= 1 and wave.status in (WaveStatus.IN_PROGRESS, WaveStatus.FAILED, WaveStatus.CLOSED)
    last_attempt = wave.sessions.get(max(attempts)) if attempts else None

    if session_policy == "fresh" or not is_retry or last_attempt is None or last_attempt.runtime != runtime_id:
        # Fresh path: new attempt, no --continue.
        new_attempt = (max(attempts) + 1) if attempts else 1
        annotation = DispatchAnnotation(
            attempt=new_attempt,
            note=DispatchNote.FRESH_DISPATCH if last_attempt is None or last_attempt.runtime == runtime_id else DispatchNote.SWITCH_ON_ERROR,
            runtime_from=last_attempt.runtime if last_attempt else None,
            runtime_to=runtime_id,
            occurred_at=now_utc(),
        )
        session = await spawn_runtime(runtime_id, wave, continue_id=None)
    else:
        # Hybrid continue: same runtime + retry, session-resume.
        new_attempt = max(attempts) + 1
        try:
            session = await spawn_runtime(runtime_id, wave, continue_id=last_attempt.session_id)
            annotation = DispatchAnnotation(
                attempt=new_attempt,
                note=DispatchNote.CONTINUE_FROM_SESSION,
                runtime_to=runtime_id,
                occurred_at=now_utc(),
            )
        except SessionResumeFailed:
            # --continue failed; fall back to fresh per V8 [1:268-269].
            session = await spawn_runtime(runtime_id, wave, continue_id=None)
            annotation = DispatchAnnotation(
                attempt=new_attempt,
                note=DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH,
                runtime_to=runtime_id,
                occurred_at=now_utc(),
            )

    await state_mutate(AddSessionAttempt(wave_id=wave_id, attempt=session, annotation=annotation))
    return DispatchResult(session_id=session.session_id, attempt=new_attempt, pid=session.subprocess_pid)
```

**Per-runtime adapter contract (C07 enforces).** Each adapter implements:

```python
class RuntimeAdapter(Protocol):
    id: str
    def supports_continue(self) -> bool: ...
    async def open_session(self, wave_id: str, prompt: str) -> SessionAttempt: ...
    async def continue_session(self, session_id: str, prompt: str) -> SessionAttempt: ...
    def session_log_path(self, session_id: str) -> Path: ...
    def parse_error(self, exit_status: int, stderr: bytes) -> str: ...  # returns one of RUNTIME_* class strings
```

**TTL sweep.** Daemon background asyncio task: every hour, scan `state.waves[*].sessions` for `ended_at` older than `daemon.session_handle_ttl_seconds` (D13 default 86400) AND the parent wave's status is CLOSED. Emit a `session_handle_pruned` event + a `Mutation` that removes the row. The source-of-truth runtime session log is not touched.

### 5.14 Sequence diagrams

#### D1 — Cold-spawn CLI mutation

```
operator              CLI (eawf wave claim)     daemon         portalock    state.json
   │                          │
   │ uv run eawf wave claim    │
   ├─────────────────────────>│
   │                          │ resolve runtime_dir
   │                          │ read eawfd.pid (absent)
   │                          │ fork() daemon
   │                          ├──────────────>(detach + exec)
   │                          │                  │
   │                          │                  │ asyncio.start_unix_server
   │                          │                  │ write pid + sock
   │                          │                  │ idle_timeout task
   │                          │ poll socket (≤5s)│
   │                          ├<───── connect ───┤
   │                          │                  │
   │                          │ state.mutate     │
   │                          ├─────────────────>│
   │                          │                  │ portalock.acquire(state.json, timeout=5)
   │                          │                  ├──────────────>(EX lock)
   │                          │                  │                  │
   │                          │                  │ write WAL pending
   │                          │                  │ read+decode+validate
   │                          │                  ├────────────────────────>(read)
   │                          │                  │ apply Mutation
   │                          │                  │ re-validate
   │                          │                  │ atomic-write state.json
   │                          │                  ├────────────────────────>(write tempfile + replace + fsync)
   │                          │                  │ append event.jsonl
   │                          │                  │ rename WAL applied
   │                          │                  │ fsync
   │                          │                  │ rename WAL fsynced
   │                          │                  │ publish to subscribers
   │                          │                  ├──>(none for cold-spawn)
   │                          │                  │ portalock.release
   │                          │<──── result ─────┤
   │ stdout (envelope)         │                  │
   │<─────────────────────────┤                  │
   │                          │ exit 0           │
   │                          │                  │ last_activity_at = now
   │                          │                  │ (idle timer continues)
```

Latency budget (cold): fork + exec ~150 ms, asyncio server up ~50 ms, socket poll ~50 ms, state read + validate + mutate + write ~50 ms = ~300 ms p95 cold-call total. Within V1 budget [1:44].

#### D2 — Warm-daemon mutation

```
operator              CLI                       daemon                    portalock     state.json
   │                          │
   │ eawf wave release         │
   ├─────────────────────────>│
   │                          │ read pid (alive, owned)
   │                          │ connect socket (~3 ms)
   │                          │                  │
   │                          │ state.mutate     │
   │                          ├─────────────────>│ portalock.acquire (~5 ms)
   │                          │                  ├──────────────>
   │                          │                  │ WAL pending
   │                          │                  │ read state (cached version match? hit)
   │                          │                  │ apply + re-validate
   │                          │                  │ atomic-write
   │                          │                  │ event append + WAL fsynced
   │                          │                  │ publish (TUI subscribers receive)
   │                          │<──── result ─────┤ portalock.release
   │ envelope                  │                  │
   │<─────────────────────────┤                  │
   │                          │ exit 0           │
```

Warm latency: connect ~3 ms + lock ~5 ms + WAL + validate + write + event + fsync ~25-35 ms = ~30-50 ms p99.

#### D3 — TUI subscribe + receive

```
TUI                          daemon                  bus              event.jsonl
  │                          │
  │ event.subscribe          │
  │   since_version=v123     │
  ├────────────────────────>│ verify peer-cred
  │                          │ add subscriber(id=S1, queue=deque[1024])
  │                          │
  │                          │ catch-up since v123:
  │                          │   read event.jsonl
  │                          ├──────────────────────>
  │                          │<── events ────────────
  │<── event.push frames ───┤ (one per event)
  │                          │
  │ ... live ...             │
  │                          │
  │                          │ (mutation occurred — D2)
  │                          │ bus.publish(envelope)
  │                          │   for sub in subs:
  │                          │     queue.append (S1 OK)
  │<── event.push ──────────┤
  │ render update             │
  │                          │
  │ ... slow render ...       │
  │                          │ another 1024 events arrive
  │                          │ S1.queue full → disconnect
  │<── subscription_dropped ┤   reason=overflow
  │   code=-32008             │
  │ reconnect with since=…   │
```

#### D4 — Agent dispatch (V8 fresh path)

```
operator        CLI (eawf flow)       daemon          dispatcher    spawn child (claude -p)
   │                  │
   │ /flow            │
   ├────────────────>│ agent.dispatch wave=P20-I03-W01
   │                  ├─────────────>│ pick primary = preference[0]
   │                  │              │ runtime=claude-code
   │                  │              │ is_retry = no
   │                  │              │ session_policy = hybrid → fresh
   │                  │              ├────────────>│ spawn_runtime(continue_id=None)
   │                  │              │              ├──────────>(claude -p --session-id <uuid> < prompt)
   │                  │              │              │            │
   │                  │              │              │<──── pid ─┤
   │                  │              │ state.mutate AddSessionAttempt
   │                  │              ├──────────────────────>(portalock+WAL+write)
   │                  │              │ publish dispatch_started event
   │                  │<── result ──┤
   │<── envelope ────┤
   │                  │
   │                  │              │ (dispatch_log events stream from subprocess stdout via dispatcher)
   │                  │              │<── stdout JSONL ─────────────────┤
   │                  │              │ publish dispatch_log events
```

#### D5 — Daemon crash recovery (SIGKILL during mutation)

```
operator        CLI A                CLI B (later)           daemon (new instance)    WAL          state.json
   │                  │
   │ wave claim       │
   │  (D1 in flight)   │
   │                  │ state.mutate
   │                  ├──>│ WAL pending P20-I03-W02 written
   │                  │   │ portalock acquired
   │                  │   │ state read + apply
   │                  │   │ <<<<< SIGKILL — daemon process dies <<<<<
   │                  │ XX│ socket closed; CLI sees EOF → ECONNRESET
   │                  │<──(error envelope: daemon_crashed)
   │ retry            │
   │                  │ eawf wave claim (auto-spawn retry — pid stale)
   │                  ├──>(detach + exec new daemon)
   │                  │                                       │
   │                  │                                       │ WAL scan:
   │                  │                                       │   P20-I03-W02.pending.json exists
   │                  │                                       │   before_state_version matches disk → roll-forward attempt
   │                  │                                       │   re-apply Mutation
   │                  │                                       │   validate
   │                  │                                       │   atomic-write + event append
   │                  │                                       │   rename → .fsynced
   │                  │                                       │ emit wal_recovery event
   │                  │                                       │ accept connections
   │                  │<── connect, retry state.mutate ──>(idempotency_key reused) → idempotent_replay=true
   │<── envelope ────┤
```

#### D6 — Runtime fallback on error (V5)

```
operator         daemon                  dispatcher       claude-code (primary)     codex (next)
   │                  │
   │                  │ (D4 in flight on claude)
   │                  │
   │                  │              │ <<< claude returns HTTP 429 + Retry-After=30
   │                  │              │<── error class=RUNTIME_RATE_LIMIT ────────────┤
   │                  │              │ sleep min(30, 90) = 30
   │                  │              │ retry on same runtime
   │                  │              ├────────────────────────>(claude -p --continue)
   │                  │              │<── 429 again ──────────────────────────────────┤
   │                  │              │ fall through to next runtime
   │                  │              │ state.mutate AddSessionAttempt + DispatchAnnotation(switch_on_error)
   │                  │              │ publish runtime_switched event
   │                  │              ├────────────────────────────────────────>(codex exec)
   │                  │              │                                                 │
   │<── push runtime_switched ──────┤<── pid ────────────────────────────────────────┤
   │   from=claude-code               │                                                 │
   │   to=codex                       │                                                 │
   │   cause=RUNTIME_RATE_LIMIT       │ stream dispatch_log events from codex          │
```

If codex also fails → fall through to opencode → if every runtime fails → halt wave with `BLOCKED`, emit `runtime_unavailable` operator-notify envelope, return `-32006`.

#### D7 — Session reuse on retry (V8 continue path)

```
operator        CLI (eawf wave retry)    daemon          dispatcher    claude-code
   │                  │
   │ /flow retry      │ agent.dispatch wave=P20-I03-W01
   ├────────────────>│ wave.sessions[1] = {runtime=claude-code,session_id=S1,ended_at=...}
   │                  │ session_policy=hybrid, is_retry=yes,
   │                  │  last_attempt.runtime == requested runtime → continue path
   │                  │              ├────────────>│ spawn_runtime(continue_id=S1)
   │                  │              │              ├──────────>(claude --continue S1 < new_prompt)
   │                  │              │              │            ┌── session resumed
   │                  │              │              │<── pid ────┤
   │                  │              │ state.mutate AddSessionAttempt(attempt=2)
   │                  │              │ DispatchAnnotation(note=CONTINUE_FROM_SESSION)
   │                  │              │ publish dispatch_started event (attempt=2)
   │                  │<── result ──┤
   │<── envelope ────┤

Failure variant:
   │                  │              ├────────────>│ spawn_runtime(continue_id=S1)
   │                  │              │              ├──────────>(claude --continue S1)
   │                  │              │              │<── error: session expired ─────────┤
   │                  │              │              │ catch SessionResumeFailed
   │                  │              │              ├──────────>(claude -p new session)
   │                  │              │              │<── pid ────────────────────────────┤
   │                  │              │ state.mutate AddSessionAttempt(attempt=2)
   │                  │              │ DispatchAnnotation(note=CONTINUE_FAILED_FELL_BACK_TO_FRESH)
```

## 6. Failure modes + named edge cases

C02 adds runtime / IPC / supervisor surfaces that introduce new failure shapes beyond C01's vocabulary-level concerns.

| # | Failure mode | Trigger | Detection | Repair |
|---|---|---|---|---|
| F1 | Stale PID file, no socket | Daemon SIGKILL'd before unlinking PID. | CLI startup: PID file present but `kill(pid, 0)` raises ESRCH OR socket connect refuses. | CLI treats as stale: unlinks pid + socket; auto-spawns fresh daemon. Idempotent. |
| F2 | Socket exists, daemon dead | Crash between socket-bind and PID write OR partial cleanup. | CLI: connect-refused on a path whose owner-UID matches. | CLI unlinks socket; auto-spawns. |
| F3 | Lock-file held by dead PID | Daemon SIGKILL'd while holding `portalock` on `state.json.lock`. | Existing stale-detection [9] handles via heartbeat-staleness. | Lock stolen on next acquire. |
| F4 | WAL pending older than 1 hour | Daemon crash during apply that didn't complete validate. | Startup replay: pending WAL with `before_state_version` matches disk → roll-forward; doesn't match → poisoned. | Poisoned WAL surfaced via `eawf daemon replay-wal --inspect`; operator decides. |
| F5 | WAL poisoned (post-validation crash) | Apply succeeded; re-validate failed; state.json written before validate. | Should never happen — apply order is read → validate before → apply → validate after → write. WAL ordering forbids this. | If observed, hard daemon bug; emit `incident` envelope; refuse new mutations until operator runs `eawf daemon replay-wal --inspect`. |
| F6 | Idempotency-key replay across protocol versions | CLI sends mutation with key K, daemon crashes, new daemon (different protocol_version) starts before CLI retries. | New daemon's idempotency cache is empty; CLI retry applies the mutation a second time. | D9 hard fail on protocol mismatch averts this — CLI sees `-32004` before retry lands. |
| F7 | Subscription queue overflow | Slow TUI on a long-running phase activate (~100 events in 5s). | Daemon disconnects with `-32008 subscription_dropped reason=overflow`. | TUI reconnects with `since_version`; catch-up replays from `event.jsonl`. Up to 10000 events; beyond that needs a state snapshot first (operator decision). |
| F8 | Catch-up read too large | TUI disconnected for hours; `since_version` is 50000 events behind. | Daemon returns `-32008 catch_up_too_large`. | TUI re-subscribes without `since_version` (live-only) and operator triggers a state refresh; OR TUI falls back to mtime poll. |
| F9 | Runtime fallback exhausted | Every runtime in `runtime.preference` returns error class. | Daemon emits `runtime_unavailable` event; wave status flipped to `BLOCKED`; CLI receives `-32006`. | Operator inspects via `eawf wave show <id>`; either fixes runtime config (`eawf runtime configure ...`) or marks wave `ABANDONED`. |
| F10 | `--continue` failure on every runtime | Session-log files deleted by Claude/Codex retention sweep. | Adapter raises `SessionResumeFailed`; daemon falls back to fresh dispatch with `CONTINUE_FAILED_FELL_BACK_TO_FRESH` annotation. | No operator action — fresh dispatch proceeds. |
| F11 | Cross-runtime session reuse attempted | Bug: dispatcher tries `--continue` on a session opened by a different runtime. | Per V8 [1:267] session handles are runtime-specific; dispatcher's algorithm forbids this (only goes to continue path when `last_attempt.runtime == runtime_id`). | If observed, daemon bug — emit incident; halt wave. |
| F12 | Concurrent dispatch cap hit | `daemon.max_concurrent_dispatch=4` and a 5th `agent.dispatch` arrives. | Daemon returns `-32005 resource_exhausted`. | CLI surfaces the wait-or-retry envelope; `/flow` blocks until a slot frees. |
| F13 | OOM kill on subprocess | LLM runtime spawns a large output; RSS exceeds `daemon.subprocess_rss_kb_kill=4 GB`. | Dispatcher SIGKILLs child; emits `subprocess_oom_killed` event; wave transitions to FAILED. | Operator inspects log; either retries (V8 fresh) or revises prompt scope. |
| F14 | Auth pre-flight failure | `claude --check-auth` returns expired token at flow start. | Runtime adapter raises `RUNTIME_AUTH_ERROR`; per D12 daemon halts wave with `BLOCKED`. | Operator runs the refresh command shown in the envelope. |
| F15 | Multiple daemon spawn race | Two CLI processes start simultaneously; both detect missing PID; both fork. | Second daemon hits `bind: address already in use` on the socket; emits incident; exits. First daemon wins. | Idempotent — no operator action. The losing CLI retries connect and proceeds. |
| F16 | Service file uninstall on a never-installed daemon | Operator runs `eawf daemon disable` without prior `enable`. | `systemctl disable` / `launchctl bootout` returns non-fatal "not loaded" → idempotent no-op. | None. |
| F17 | Daemon enable on a system without systemd/launchd/Windows-Service | Container with no init system; `systemctl --user` reports `Failed to connect to bus`. | `eawf daemon enable` detects error from underlying command; returns structured envelope `enable_failed` with `cause=no_init_system`. | Operator either installs a service manager OR uses on-demand spawn (V1 default; service-file is opt-in [1:161]). |
| F18 | Idempotency-key window expired | CLI takes longer than 60 s between mutation issue and retry (e.g. network hang). | Daemon no longer has the cached idempotency-key → re-applies the mutation. | Mutation logic must be idempotent at the state-level (most are — Phase/Iter/Wave transitions reject double-open via lifecycle [16 in C01]). The 60 s window covers transient hangs; multi-minute hangs are operator-investigable. |
| F19 | Version-skew during in-flight subscription | TUI connected at protocol v1; CLI upgrade flips daemon to v2; daemon restart. | Subscription severed during daemon restart; TUI reconnects, sends old protocol_version → `-32004`. | TUI prints upgrade hint; operator upgrades TUI. |
| F20 | Recovery shell on daemonless path mutates state | Per V1 [1:30] recovery shell is read-only; user accidentally runs a mutating verb with `EAWF_DAEMONLESS=1`. | CLI mutation handlers check `EAWF_DAEMONLESS`: read verbs proceed direct; write verbs fail with `daemon_required` envelope. | Operator either spawns the daemon (recovery path) or accepts the mutation can't run. |
| F21 | WAL directory full | `<runtime_dir>/wal/` accumulates >10000 `.fsynced.json` files (cleanup task crash). | Cleanup task runs hourly; if observed, emit `wal_directory_pressure` event at >1000 done files. | Operator runs `eawf daemon replay-wal --gc`; or daemon auto-prunes on startup. |
| F22 | Peer-credential check returns root | A privileged process connects to the daemon's UDS (uncommon — would require root cooperation). | Daemon's UID comparison: root UID 0 != daemon UID → reject `-32000 unauthorized`. | The daemon is per-OS-user; root invocations need to `sudo -u <user>` to the daemon's UID. Refusal is the correct policy. |
| F23 | Idle-timeout fires during a long-running subscription | TUI subscribed; no mutations for 300 s; daemon considers self-shutdown. | Idle-timeout watchdog excludes active subscriptions from the idle test (subscription = "activity"). | None — the TUI alone keeps the daemon alive. Operator-closed TUI → idle-timeout fires within 300 s. |
| F24 | Daemon graceful shutdown timeout exceeded | `daemon.shutdown(drain=true, timeout=30)` but 5 dispatches are mid-flight at >30 s each. | After 30 s, daemon force-disconnects subscribers + SIGKILLs unresponsive dispatchers. | Worktrees may be left in inconsistent state; cherry-pick discipline catches per AGENTS rule 11 [13]. |
| F25 | Windows pywin32 install fails on user's Python distribution | `pywin32` post-install step fails on conda Python or alt-distribution. | `eawf daemon enable` detects ImportError; surfaces `enable_failed cause=missing_dep details=pywin32`. | Operator either installs pywin32 OR uses the NSSM fallback (template documented in §5.10.3). |

## 7. Migration plan

C02 is the biggest mechanical change in the C00..C11 series — 73 callsites of the in-process state-mutator transition to daemon RPC clients. The migration runs in three phases over P22-P28 (per the v0.3-v0.4 roadmap [3:154-175]) and lands the long-running coordinator surface.

### 7.1 Phase 1 — Daemon prototype (P27-W01 + W02)

**Goal.** A daemon that can be enabled / disabled, accepts JSON-RPC, exposes `daemon.ping` + `daemon.status`. No state-mutator integration yet.

**Steps.**

1. Add `src/eawf/daemon/` package:
   - `__init__.py` — `PROTOCOL_VERSION = "1"`.
   - `main.py` — entry point; resolves runtime_dir; binds socket; runs asyncio loop.
   - `server.py` — `asyncio.start_unix_server` listener + JSON-RPC frame parser.
   - `auth.py` — peer-credential check (POSIX) + DACL+SID check (Windows).
   - `methods/__init__.py` — method registry.
   - `methods/daemon.py` — `daemon.ping`, `daemon.status`, `daemon.shutdown`.
2. Add `eawf daemon run --foreground` CLI verb for systemd / launchd / win32service to call.
3. Add `eawf daemon ping|status|stop|logs` CLI verbs (read-only against the daemon).
4. Per-OS service templates (§5.10) under `templates/`.
5. CI matrix per §5.11.
6. Mark P27-W01 closed when:
   - `eawf daemon run --foreground` survives 10 minutes idle.
   - `eawf daemon ping` returns version + PID on Linux + macOS + Windows runners.
   - Peer-credential reject path tested on Linux (run daemon as user A, connect from `sudo -u userb eawf daemon ping`, see `-32000`).

### 7.2 Phase 2 — Daemon-mediated mutator (P27-W03 + W04 + W05)

**Goal.** All state mutations route through daemon RPC when the daemon is up; daemonless fallback retained for the V1 read-bypass list + recovery shell.

**Steps.**

1. Add `state.mutate` method backed by an inner thread that owns the current `state_transaction` semantics (asyncio asyncio `loop.run_in_executor` to keep portalocker on a single worker thread). Mutation receives the typed `Mutation` payload + idempotency_key.
2. Define `Mutation` discriminated union in `src/eawf/state/mutations.py` (C03 owns the per-variant schema; C02 introduces the framing).
3. Wrap every existing CLI command's `state_transaction` callsite with an RPC client adapter:
   - When daemon up: marshal call into `state.mutate(Mutation(kind=...)`).
   - When daemon down + `EAWF_DAEMONLESS=1` set on a write verb: refuse.
   - When daemon down + read verb: bypass per V1 [1:26-30].
4. Add WAL implementation per §5.6 (`src/eawf/daemon/wal.py`).
5. Add startup WAL replay (`src/eawf/daemon/recovery.py`).
6. Add `eawf daemon replay-wal --inspect|--gc` CLI verb.
7. Phase-gated rollout (per `daemon.proxy_enabled: false` flag in config):
   - **Sub-phase a.** Flag-default `false`. Daemon runs; CLI continues using in-process `state_transaction`. Daemon ping/status proven up.
   - **Sub-phase b.** Flag flips to `true` in CI + eawf-self repo. Self-test gates: full pytest suite passes against daemon-proxy path.
   - **Sub-phase c.** Flag default flips to `true`. In-process `state_transaction` becomes the daemonless fallback only (V1 carve-outs).
8. Mark P27-W03..W05 closed when:
   - All ~22 CLI commands [counted via grep above] route mutations through the daemon when flag is true.
   - WAL replay test: kill -9 during mutation → restart → state consistent + event appended.
   - Idempotency replay test: same key + same params returns `idempotent_replay: true`.

### 7.3 Phase 3 — Dispatch + fallback + sessions (P28-W01..W04, P29-W01..W03)

**Goal.** `/flow` execute, runtime fallback (V5), session-handle tracking (V8) live.

**Steps.**

1. Add `agent.dispatch` method + `dispatcher.py` subsystem.
2. Add per-runtime adapter contract per §5.13 + C07 work.
3. Add runtime-fallback state machine per §5.12.
4. Add session-handle tracking schema + TTL sweep.
5. Add `wave.switch` RPC + `eawf wave switch <id> --to <runtime>` CLI verb.
6. Add resource-limit enforcement (§5.8).
7. Add event-bus + subscription protocol (§5.7).
8. Add TUI subscribe path (C06 work; C02 specifies the protocol shape).
9. Mark closed when:
   - End-to-end /flow with V5 fallback works: kill claude on 429, codex picks up, event log shows `runtime_switched`.
   - End-to-end V8 retry: failed wave → `eawf wave retry` → `--continue` succeeds; `dispatch_history` shows `CONTINUE_FROM_SESSION`.
   - End-to-end manual switch: `eawf wave switch <id> --to codex` → event log shows `runtime_switched cause=manual_override`.

### 7.4 Migration safety

**Defense-in-depth retained.** Per V1 [1:51] `portalocker` is NEVER removed; daemon mutator runs *inside* a portalock acquisition. Daemonless writers (V1 carve-outs) hit the same lock the daemon would. No state-corruption window.

**Rollback.** Every phase flag-gated. If sub-phase b proves daemon proxy unsafe, flip flag back to `false`, daemon stays up for `daemon.ping` only, in-process `state_transaction` continues serving.

**Schema unchanged.** No state schema bump in P27..P29 unless C03 typed-Mutation rollout requires it; daemon adds new RPC surface but no `state.json` field renames.

**Pre-commit + CI.** Per OS matrix per §5.11 added at P27-W01 land. The full pytest suite gains a `--daemon-proxy` flag (today's tests run direct; the daemon-proxy variant proxies through a test-spawned daemon). CI runs both modes.

## 8. Open questions for operator

### Q1 — pywin32 vs NSSM final pick (V6 [1:172])

§4 D2 recommended pywin32 primary + NSSM fallback. The cost split:

- **pywin32 primary.** Heavy dep (~50 MB), but single-source install path; the same `pip install eawf` ships the service surface.
- **NSSM fallback.** Light (one ~300 KB binary), but Windows-only build step + binary-bundle in the wheel.

AUQ:

- (a) **pywin32 primary, NSSM fallback documented (recommended)** — §4 D2.
- (b) **NSSM primary** — bundled NSSM in the Windows wheel; `eawf daemon enable` shells out to it.
- (c) **Operator picks per-machine** — both supported, `eawf daemon enable --backend pywin32|nssm`.

**Recommendation (a).** pywin32 is the conventional Windows-Python service path; NSSM is the escape hatch for environments where pywin32 install fails (corporate Python distributions).

### Q2 — Socket path on macOS (D5)

§4 D5 recommended `<local-path>` on macOS (no `$XDG_RUNTIME_DIR` by default). macOS sandbox permissions for `<local-path>` socket paths differ — some sandboxed apps cannot write there. Two macOS variants:

- (a) `<local-path>` — recommended; works for all non-sandboxed shells.
- (b) `$TMPDIR/com.eawf.eawfd.sock` — works in sandboxed contexts; but `$TMPDIR` is per-process on macOS and may not be shared.

**Recommendation (a).** Sandboxed-shell usage is an out-of-scope future concern; v0.3-v0.5 single-user CLI usage works with `<local-path>`.

### Q3 — Runtime fallback retry budget (V5)

§4 D12 picked hybrid retry semantics (backoff on `RUNTIME_RATE_LIMIT`, immediate fall-through otherwise). Should the 429 retry budget be wall-clock-capped?

- (a) **Cap at 90 s total backoff.** If `Retry-After=180`, cap to 90 s and fall through anyway. Keeps p99 wave-start under 2 min.
- (b) **Honor `Retry-After` fully.** Sleep up to the header's value (potentially minutes).
- (c) **Configurable per-runtime.** `runtime.<id>.fallback.max_backoff_seconds: 90`.

**Recommendation (a) + (c)** — default 90 s cap; per-runtime tunable.

### Q4 — Session-handle TTL after wave close (V8)

§4 D13 picked 1 day. Alternatives:

- (a) **1 day (recommended).**
- (b) **7 days** — covers week-long audit cycles.
- (c) **Until next phase close** — daemon prunes session handles only when a phase transitions to CLOSED.

**Recommendation (a).** Most retries occur within hours of a failed dispatch; 1 day covers same-day reopen + bug investigation. The audit-replay trail in `event.jsonl` remains intact regardless — only the session-handle pointer is pruned, not the underlying `event.jsonl` or per-role report jsonl row.

### Q5 — Cold-spawn UX (D15)

§4 D15 picked silent auto-spawn with `--verbose` opt-in. Alternative: always show a brief "starting eawfd..." line on first call to surface daemon-spawn to operators who didn't read V1 prose.

- (a) **Silent (recommended)** — V1 [1:48] mandates transparency.
- (b) **Brief one-liner** — "starting eawfd... done (PID 12345)" goes to stderr.
- (c) **Per-config flag** — `daemon.show_spawn_message: false` default; operator can opt in.

**Recommendation (a)** with `(c)` as the escape hatch.

### Q6 — Daemon-proxy default rollout schedule (§7.2 sub-phase c)

When does the `daemon.proxy_enabled` flag default flip from `false` to `true`?

- (a) **P27-W05 closes** — same wave as the third sub-phase. Aggressive: full rollout in one phase.
- (b) **P28 opens** — between phases. Two-week soak window in CI + eawf-self repo before flag flips.
- (c) **Operator decides** — flag stays `false` until manual flip.

**Recommendation (b).** Daemon proxy is the load-bearing change; a soak period catches integration regressions before non-eawf consumers depend on it.

### Q7 — WAL retention for debugging

§5.6 keeps `.fsynced.json` for 1 hour in `<wal_dir>/done/`. Useful for debugging crashes; but on a busy day could accumulate 100s of files.

- (a) **1 hour (recommended).**
- (b) **24 hours** — survives most debugging windows.
- (c) **Configurable** — `daemon.wal_retention_seconds: 3600`.

**Recommendation (a) + (c)** — 1-hour default, configurable.

### Q8 — Concurrent dispatch cap default

§5.8 default `daemon.max_concurrent_dispatch=4`. Picks:

- (a) **4 (recommended)** — fits a typical 4-physical-core developer machine.
- (b) **8** — heavier machines.
- (c) **CPU-derived** — `multiprocessing.cpu_count() // 2`.

**Recommendation (c)** — auto-derived; profile-conditional override per C08.

### Q9 — Subscription queue size (D7)

§4 D7 picked 1024 per-subscriber. On a phase-activate burst (~100 events in seconds) this leaves headroom; on extreme phases (10000+ events on a /research-heavy phase) could overflow.

- (a) **1024 (recommended).**
- (b) **4096** — larger memory footprint.
- (c) **Configurable** — `daemon.subscription_queue_size: 1024`.

**Recommendation (a) + (c)** — 1024 default, configurable.

### Q10 — Recovery shell behaviour on mutating verb (F20)

§5.5 + F20 says daemonless write verbs fail with `daemon_required`. Alternative: allow daemonless writes when `EAWF_DAEMONLESS_FORCE=1` is also set (very explicit opt-in).

- (a) **Refuse daemonless writes (recommended)** — single mutator path enforced.
- (b) **Allow with `EAWF_DAEMONLESS_FORCE=1`** — operator escape hatch; portalock provides serialization.
- (c) **Allow always under `EAWF_DAEMONLESS=1`** — V1's read-bypass becomes a full bypass.

**Recommendation (a).** Single mutator path is the V1 invariant; an explicit override would bypass the WAL and the event-bus push, making audit replay incomplete. If operator truly needs daemonless writes (extreme recovery), suggest a procedure: stop daemon → write via direct `state_transaction` → start daemon (WAL is empty so startup-replay no-ops).

### Q11 — Idempotency window (D14 + F18)

§5.2 specifies a 60-second idempotency-key dedup window. Alternatives:

- (a) **60 s (recommended).**
- (b) **5 min** — survives slow network retries.
- (c) **Configurable** — `daemon.idempotency_window_seconds: 60`.

**Recommendation (a) + (c)** — 60 s default, configurable up to 5 min. Beyond 5 min the cost (in-memory dict size) outgrows the benefit; the underlying `Mutation` apply is idempotent at the state-level [16 in C01].

### Q12 — Daemon log path + rotation policy

§5.5 says daemon writes `<runtime_dir>/eawfd.log` rotated daily, kept 7 days. Picks:

- (a) **Daily, 7-day retention (recommended).**
- (b) **Hourly, 24-hour retention.**
- (c) **Configurable** — `daemon.log_rotation_policy: daily|hourly`.

**Recommendation (a) + (c)** — daily default; opt-in hourly when investigating an active incident.

### Q13 — Per-OS CI runner budget (V6 [1:817])

V6 mandates per-OS CI matrix. Windows runners are slower + more expensive on GitHub Actions. Pick:

- (a) **Run full pytest suite on all 3 OSes on every PR.**
- (b) **Run full suite on Linux + macOS on every PR; Windows-only smoke tests on PR; full Windows suite nightly.**
- (c) **Run portalock + spawn smoke tests on all 3 OSes; full suite Linux-only per PR.**

**Recommendation (b).** Windows runners stay budget-bounded by running the full suite nightly; PR feedback stays fast (Linux+macOS) with Windows safety from smoke tests.

### Q14 — Event log retention (deferred from C00 §C07 [1:689])

Event log is canonical for audit replay [4:262-267]. Should daemon rotate it?

- (a) **Never rotate** — append-only forever; compact on operator command (`eawf event compact`).
- (b) **Rotate per phase** — one `event-P<NN>.jsonl` per phase.
- (c) **Compress + archive** — current rolling, compressed archive older than 90 days.

**Recommendation:** **(a)** for v0.3-v0.5; defer to C07 for the long-term decision. Today's `event.jsonl` is 266 KB after 549 events [4:177] — no rotation pressure for 1-2 years of solo-developer usage.

## 9. References

[1] `.ea/local/research/long-term/2026-05-16-c00-spec-index.md` — C00 spec architecture index. V1 [1:24-53] daemon Day-1 + smart-spawn writer; V5 [1:127-151] reactive runtime fallback; V6 [1:153-182] per-OS native service + on-demand spawn; V7 [1:184-224] telemetry; V8 [1:226-271] hybrid session reuse. §C02 [1:362-425] full scope.

[2] `.ea/local/research/long-term/2026-05-16-c01-foundations.md` — C01 Foundations brief. §5.2 URN scheme [2:179-242]; §5.3 entity catalog (Wave [2:338-385], AgentSession [2:530-552], Event [2:553-588], Runtime [2:666-697]); §5.4 lifecycle DAGs (Wave [2:896-930], AgentSession [2:1063-1086]); §5.5 persona authority matrix [2:1240-1276] (daemon as sole writer of state.json); §5.6 trust + audit-replay model [2:1277-1325].

[3] `.ea/local/research/long-term/2026-05-15-language-and-pyo3-fit.md` — Language brief. §F5 [3:77-87] daemon concurrency model is asyncio JSON-RPC + threaded executor over portalocker mutator + validator. §D49 [3:144-145] locks the pick. §6 [3:89-94] v0.3-v0.4 release pattern is tag-only (no PyPI/npm yet).

[4] `.ea/local/research/long-term/2026-05-15-long-term-features-deep.md` — Long-term features brief. §"Axis B" [4:174-251] bottleneck resolution + work-stealing dispatcher + cycle time + token math + state-store scaling + pre-commit / inner loop + ToC sequencing. §"Axis C" [4:255-360] reconcile drift sweep + event-source rebuilder + Merkle hash-tree + typed Mutation. §"Axis D" [4:365-440] OTel + cost ledger + deterministic replay.

[5] AGENTS.md — non-negotiable rules. Rule 1 (CLI is dispatch; library implements); Rule 4 (state CLI is the only mutator of state.json + future daemon-RPC equivalent); Rule 9 (f-strings only; `logger = logging.getLogger(__name__)`); Rule 11 (worktree discipline); Rule 14 (commit prefix); Rule 16 (secrets and PII hygiene); Rule 17 (naming conventions — `scope_id` not `scope`, `wave=` not `wave_id=` in logs); Rule 19 (typed agent reports); Rule 20 (planned-scope revisability).

[6] `.ea/local/research/long-term/2026-05-15-long-term-roadmap-synthesis.md` — Roadmap synthesis. §"Spawn mechanism" [6:98-105] subprocess to vendor CLI + OAuth + subscription billing. §"Safety gates" [6:107-114] disjoint write scopes + per-wave wall-clock cap + idempotency key. §"Budget broker" [7:115-128] counterfactual cost ledger + harness session log ingestion + per-wave / phase quota tracking + soft-cancel breaker. §"Quota recovery" [7:130-133] vendor 429 auto-pause + auto-resume (extended by V5 to switchover). §"Proposed phase order" [6:154-181] P22-KERNEL + P27-DAEMON + P28-DISPATCH + P29-REPLAY landing the daemon surface.

[7] Aliased to [6] for the budget-broker + quota-recovery lines specifically. See [6] lines 115-133.

[8] `.ea/local/research/long-term/2026-05-15-state-history-cache-design.md` — State-history cache design. §line 277 [8:266-277] Windows file-ID portability concern — addressed by sha256(repo + relative path) cache keys rather than OS file IDs.

[9] `src/eawf/lock/stale.py` — Stale-lock detection. Heartbeat-based; integrated with portalock.acquire stealing.

[10] `src/eawf/lock/portalock.py` — `acquire(target, *, timeout, on_event) -> LockHandle`. Uses `portalocker.LOCK_EX | LOCK_NB`; writes `{pid, hostname, started_at, heartbeat_at}` into the sibling `.lock` file; `LockTimeout` on deadline.

[11] `src/eawf/cli/_mutation.py` — `state_transaction(state_path, *, timeout=5.0) -> Iterator[State]`. Acquires portalock; reads + decodes + schema-validates; yields; re-validates post-mutation; `atomic_write_json_locked` while lock held; release. NOT re-entrant.

[12] `src/eawf/state/writer.py` — `atomic_write_json(target, data)` + `atomic_write_json_locked(target, data)`. Tempfile + `os.replace` + parent-dir fsync; sibling lock acquired in the non-`_locked` form.

[13] AGENTS.md (same as [5]) — non-negotiable rules.

[14] `src/eawf/cli/commands/` — 22 modules that contain `state_transaction` references (counted via `grep -rl "state_transaction" src/eawf/cli/commands/ | wc -l`): `config`, `estimation`, `evidence`, `init`, `lifecycle`, `mcp`, `memory`, `pr_review`, `repo`, `roadmap`, `session`, `sync`, `wave_ci`, `wave_policy`, `worktree`, and others — all transition to daemon RPC clients during the §7.2 migration.

[15] `src/eawf/store/envelope.py` — `Envelope` top-level JSONL record. C02 wraps its `payload` in the JSON-RPC body for `state.mutate` request frames.

[16] `src/eawf/lifecycle/transitions.py` — Phase / Iter / Wave open/close/abandon helpers; `LifecycleError`. Lifecycle invariants enforce no-double-open / no-skip-state, which gives `Mutation` apply state-level idempotency.

[17] tmux server pattern (auto-spawn on `tmux attach`; idle-shutdown on no-clients). https://github.com/tmux/tmux/wiki — daemon ergonomics reference for V1 on-demand spawn.

[18] systemd `systemctl --user` documentation — https://systemd.io/USER_SERVICES/. Per-user service convention for V6 Linux path.

[19] launchd LaunchAgent reference — https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html. Per-user LaunchAgent convention for V6 macOS path.

[20] pywin32 `win32serviceutil` — https://github.com/mhammond/pywin32. Python-native Windows Service path for V6 Windows primary.

[21] NSSM (Non-Sucking Service Manager) — https://nssm.cc. Lightweight Windows service wrapper for V6 Windows fallback.

[22] JSON-RPC 2.0 specification — https://www.jsonrpc.org/specification. Wire format for §5.2.

[23] `git-daemon` reference — Unix-style coordinator daemon precedent for on-demand spawn semantics. https://git-scm.com/docs/git-daemon (referenced for design inspiration; not adopted directly).

[24] rust-analyzer LSP server — Long-lived language-server daemon precedent for IPC + subscription patterns. https://rust-analyzer.github.io (referenced for design inspiration).

[25] Jupyter kernel protocol — Per-kernel daemon spawn + ZMQ IPC + subscription. Reference for the spawn / subscribe pattern. https://jupyter-client.readthedocs.io/en/stable/messaging.html (referenced for design inspiration).

## 10. Provenance + Scrub

### Provenance

- `store_record=none (local-only research brief)`
- `commit=3b86f7a (parent at brief authoring time; revisions 2026-05-18)`
- `cluster=C02`
- `consumes=C00 verdicts V1, V5, V6, V8, V9 (locked 2026-05-16 [1:22-271])`
- `consumes=C01 entity catalog + URN scheme + persona authority matrix [2]`
- `supersedes=none`
- `last_revised=2026-05-18 (audit-driven: Q1 daemon = sole mutator absorbed; D1/D7/D8/D3 reversed per audit XB11/XB12/XB13; D7 flipped to drop-oldest per C02.F50; SessionAttempt path → opaque handle per XB05; SCM-asyncio bridge + Windows pywin32 listener thread added in §5.13 per XB14/XB11; cold-spawn benchmark open question per C02-I011)`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md (4 BLOCKERs XB11/XB12/XB13/XB14; 12 Codex issues)`
- `authority_binding=Q1 supersede (2026-05-18): daemon (eawfd) = sole canonical mutator for state.json + layered config + registry + event store + audit store + telemetry DB. Three legacy writers (state-CLI, layered-config writer, registry writer) migrate into daemon internals per 2026-05-18-migration-dag.md.`
- `session=eawf-spec-c02-daemon-topology-2026-05-16`
- `operator_decisions_locked=2026-05-16 (D1..D15 seeded); 2026-05-18 Q1/Q8/Q10 ratified (Q1 daemon-sole-mutator; Q8 pywin32 thread+queue bridge; Q10 outcome-WAL)`
- `verification ladder applied`:
  - source: `src/eawf/lock/portalock.py` [10] read in full; behaviour cited matches code at branch `feature/eawf-v0.3-p20` HEAD.
  - source: `src/eawf/cli/_mutation.py` [11] read in full; transaction lifecycle in §5.6 matches.
  - source: `src/eawf/state/writer.py` [12] read in full; atomic-write pattern in §5.6 cited verbatim.
  - source: `src/eawf/cli/commands/` directory listing [14] enumerated via `ls` + `grep` for `state_transaction` callsites (22 of 41 modules).
  - cross-brief: V1/V5/V6/V8 quoted from C00 [1] inline.
  - cross-brief: C01 entity catalog [2] cited for Wave / AgentSession / Event / Runtime field shapes.

### Scrub

- status: **clean** (per AGENTS rule 16 [13]).
- references: repo-relative paths only OR public external URLs (jsonrpc.org, github.com/tmux, systemd.io, developer.apple.com, github.com/mhammond/pywin32, nssm.cc, rust-analyzer.github.io, jupyter-client.readthedocs.io).
- local paths: none. The brief documents file paths conventional to per-OS service surfaces (`<local-path>`, `<local-path>`, `\\.\pipe\eawfd-<user>`) — these are *target install paths*, not host-specific paths.
- real emails: none. Author block is `claude-opus-4-7` (model id) per AGENTS provenance convention.
- abstract placeholder names: not applicable (no mockup repos in this brief).
- machine paths: `$XDG_RUNTIME_DIR` (Linux), `<local-path>` (macOS, fallback), `\\.\pipe\eawfd-<user>` (Windows) — all conventional substitution forms, no concrete host paths.
- hostnames / IPs: none (Unix socket / named pipe only; no network listener).
- secrets / tokens: none (auth model is peer-credential + file permissions; no shared-secret).
- companion-doc references: all repo-relative (`.ea/local/research/long-term/...`) or external URL.
