# C06 — Operator Surface (TUI + Web stub) — Eä framework long-term specs

**Cluster:** C06 (Operator Surface — Textual TUI architecture + widget catalog + `/` palette + modal stack + daemon-push reactivity + theming + web-stub WS bridge + asciinema)

**Title:** Operator Surface (TUI + Web stub)

**Status:** `local-draft`, `accepted` (operator ratified D1..D24 + 5 /blitz rounds Q1..Q16 + Q-new1..Q-new5 on 2026-05-17 via AUQ — see §10 Provenance for the 6 override deltas: D4, Q4, Q12, Q15, Q6, Q-new1)

**Created:** `2026-05-17T00:00:00Z`

**Author:** `claude-opus-4-7`

**Depends on:** C00 [1] (V1, V2, V3, V5, V7 load-bearing), C02 [3] (daemon JSON-RPC + event-subscribe + runtime-fallback events), C03 [4] (PhaseSpec / IterSpec / WaveSpec render source), C04 [5] (skill envelopes + needs-user handshake + plan-mode preview + Edit Plan subagent)

**Consumed by:** C09 (TUI perf budget + snapshot fixtures + per-OS render matrix), C10 (operator onboarding flow + asciinema docs + TUI install/upgrade), C11 (PR / Linear / Slack overlay integrations consume TUI palette surface)

## 1. Purpose + scope statement

C06 makes V1 [1:24-53], V2 [1:55-74], V3 [1:76-96], V5 [1:127-151], and V7 [1:184-224] implementable at the operator surface. The brief locks the Textual TUI architecture (`src/eawf/tui_v2/` tree, scope-dispatch ladder, screen / widget composition), the `/` palette verb registry, the modal stack inventory + per-modal action surface, the daemon-push reactivity protocol with mtime-poll fallback, the performance budget, the Wong 2011 deuteranopia-safe palette + runtime swap surface, the keybinding catalog (arrows-only nav + vim aliases + full key names), the Pilot-driven snapshot test fixture set, the asciinema cast determinism shape, the runtime-switched banner placement, the /metrics overlay tiles, and the web-stub WebSocket bridge contract.

The trigger is the P20 postmortem in [2]: P20 (TUI, Metrics, Operator UX) closed in code but shipped unfit-for-purpose — `workspace.py` (635 LOC) and `portfolio.py` (729 LOC) shipped as dead code; the roadmap pane shipped as a 5-line numeric counter strip instead of V12's collapsable iter tree; the 30 Hz `rich.live` refresh loop reloaded 200 KB of `state.json` per keystroke; the commit-prefix lint never enforced D17's iter-in-prefix rule [2:64-101]. C06 rebuilds the TUI on Textual per the round-2 stack verdict [2:21] and the smoke-demo signal [2:50-61]; it freezes the contracts that downstream phases (P22+) will land per the migration plan in §7.

V1 names daemon as the canonical writer of `state.json` and the canonical push source for live events. C02 [3:296-302] specifies `state.subscribe` / `event.subscribe` JSON-RPC methods that the TUI consumes — C06 binds those methods to a `reactive[State]` watcher. V3 routes profile-conditional widget contributions through the layered config — C06 specifies the visibility filter at the screen-composition layer. V5's `runtime_switched` events surface as a header runtime-cell + transition toast per the operator's D8 pick. V7's telemetry projection feeds the `/metrics` overlay's 3×2 tile grid per D9.

**In scope (per C00 §C06 [1:587-644]).**

- **Textual app architecture** — `src/eawf/tui_v2/` tree layout per [2:128-160]; `EaApp(App)` entry; per-screen composition; theme.css.
- **Scope dispatch ladder** — cwd → workspace > repo > user > status; per D10. Implemented at `cli/app.py` bare-command handler.
- **Per-scope screens** — `RepoScreen`, `WorkspaceScreen`, `UserScreen`, `WaveBoardScreen`. Widget composition per scope.
- **Widget catalog** — `RoadmapTree` (Textual `Tree`), `EUBar` (custom), `StatusPane` (Static composite), `GitPane` (Static + git probe), `BacklogTable` (DataTable), `ConfigRow` (Static composite for bool/enum/path field rows), `Heartbeat` (Static animated), `ChoiceButton` (Static + focus state).
- **`/` command palette** — verb catalog, fuzzy match, `@`-mention completion, runtime context-filter per screen, per D13.
- **Modal stack** — audit-running, audit-failed (full mutating menu per D17), detail (h/d/m/e/dp), pr-list, edit-field, config (tabbed), plan-preview, needs-user-AUQ, help; cap depth at 3 per D14.
- **Reactivity** — daemon-push primary via `event.subscribe`; mtime-poll fallback (2 s interval) when daemon disconnected; per D1, D2, D19.
- **Performance budget** — <150 ms p99 first paint, <50 ms p99 keypress→render; enforced by Pilot harness in CI per D16.
- **Theming** — Wong 2011 deuteranopia-safe palette; runtime swap via `/theme <name>` palette verb persisting to `<local-path>`; per D12.
- **Keybindings** — arrows + PgUp/PgDn/Home/End/Enter/Esc primary; vim aliases (h/j/k/l/gg/G); fixed in code; per D11.
- **Snapshot tests** — Textual `Pilot.press(...)` → SVG snapshots; per-screen + per-overlay fixtures (~12 total); per D20.
- **Onboarding splash** — per-scope (repo/workspace/user) variants on first launch; persistent dismissed flag per scope; per D15.
- **`?` help modal** — full keymap overlay; reachable from anywhere; coexists with persistent footer hints; per D23.
- **Asciinema artifact generation** — Pilot-driven SVG snapshot sequence composed into cast at fixed cadence (monotonic clock); per D7.
- **V5 runtime-switched banner** — header strip dedicated `runtime` cell + 3-second toast on transition; per D8.
- **V7 /metrics overlay** — 3×2 dashboard tile grid; 5-second refresh against daemon telemetry projection; per D9.
- **Web stub** — minimal SPA reading daemon WS bridge events; auth via local socket bridge; deployment local-only first; concrete SPA stack deferred to a web-cluster brief per D6.
- **Mutating audit overlay** — full menu (retry / split / land-partial / abandon / scope-change) dispatched via `eawf agent dispatch` subagent; per D17.
- **All-phases roadmap tree** — hybrid lazy load (last 5 phases eager, older lazy on expand); per D18.
- **`/pr` overlay** — global palette verb; lazy `gh pr list --json` shell-out; 60 s cache; per D21.
- **needs_user envelope rendering** — modal AUQ overlay reading `body.user_question` from C04 envelope; per D24.

**Out of scope (deferred per C00 [1:621-628]).**

- **Mouse support** — keyboard-only per P20 direction round 5 [2:451-457]; deferred to v0.5+.
- **Color-blind theme switch** — Wong 2011 already CB-safe; explicit CB switch deferred to v0.4 per P20 direction round 14 [2:521-528].
- **Configurable hotkeys** — fixed in code per D11; per-user rebinding deferred to v0.6+.
- **Bell on attention** — deferred per P20 direction round 6 [2:457-464]; resurfaces in v0.4 if operators ask.
- **`/skill` + `/mcp` overlays** — deferred to v0.4 per P20 direction round 17 [2:546-553].
- **Pane resize** — `+`/`-`/`=` deferred to P21 per P20 direction round 6 [2:457-464].
- **Mobile / responsive layouts** — out of scope for v0.3-v0.5; terminal-only.
- **Per-runtime adapter coupling** — runtime adapter shape (CC plugin tree, Codex equivalent) → C07.
- **Profile composition algorithm** — C08; C06 *consumes* the resolved profile bundle but does not implement composition.
- **CLI verb-noun matrix surface** — C05 owns it; C06 specifies only the TUI's invocation of CLI verbs via subprocess + daemon RPC.
- **Telemetry projection writer + DuckDB schema** — V7 work deferred to C09; C06 consumes the read-side `state.telemetry.*` RPC.
- **Per-OS service-registration UI** — C02 + C10; C06 surfaces `daemon: enabled / disabled / running` in the status pane only.
- **Visual diff of spec versions** — C09 territory; C06 ships `eawf wave spec render --diff` text-only output reachable from a detail overlay.

**Non-goals (C06-specific).**

- **NG1 — Rewriting current `src/eawf/tui/` in place.** C06 ships a parallel `src/eawf/tui_v2/` tree; legacy `src/eawf/tui/` stays as a dead historical artifact until P22 ratifies the cutover per the migration plan in §7. Per P20 direction §"Critical contracts" [2:582-588].
- **NG2 — Mutating state from the TUI bypassing the daemon.** Every state mutation route routes through `eawf wave / phase / iter / roadmap / config` CLI verbs (daemon-mediated per V1) — the TUI never imports `eawf.state.writer` directly. Subagent dispatch (D5, D17) shells out to `eawf agent dispatch`.
- **NG3 — Inline LLM execution in-TUI.** The TUI does not host LLM calls. Audit-overlay mutating actions dispatch via subagent; the subagent runs in a daemon-spawned subprocess per C02 §5.13 [3:792-902].
- **NG4 — Replacing `rich` with Textual everywhere in the CLI.** `rich` stays for CLI output rendering (`eawf state show`, `eawf wave list`, etc.) per C05. Textual is TUI-only.
- **NG5 — Bundling a JavaScript SPA in C06 implementation phase.** Web stub ships the daemon-side WebSocket bridge + protocol contract + auth model; concrete SPA implementation (Preact / Lit / Svelte / Tauri) deferred to a web-cluster-specific brief per D6.

## 2. Goals + non-goals

### Goals

| G# | Goal | Source |
|---|---|---|
| G1 | Every `eawf` bare-command invocation on TTY dispatches to the correct scope screen via the cwd → workspace > repo > user > status ladder; non-TTY / `--plain` / `--no-input` falls back to `eawf status` text. | C00 §C06 [1:587-644] + P20 RC-1 [2:64-101] |
| G2 | TUI subscribes to daemon `event.subscribe` on launch; receives push frames; re-renders on each matching envelope. Mtime-poll fallback (2 s) when daemon disconnected; degraded-banner shows in header. | C00 V1 [1:24-53] + D1 + D2 + D19 |
| G3 | Repo / workspace / user / wave-board screens compose from shared widgets (Header, Footer, Heartbeat, RoadmapTree, EUBar, StatusPane, GitPane, BacklogTable, ConfigRow); no per-scope duplicate chassis. | RC-9 [2:99-101] + D3 |
| G4 | Performance budget: <150 ms p99 first paint, <50 ms p99 keypress→render on a 50-phase fixture state.json. Enforced by Textual Pilot harness in CI. | C00 §C06 [1:618] + D16 |
| G5 | All-phases roadmap tree with hybrid lazy load (last 5 phases eager, older lazy on expand). V12 glyph schema + 5-cell EU bar inline. Per-iter expand/collapse via ←/→. | P20 direction round 14 [2:521-528] + D18 + V12 [6:585-621] |
| G6 | `/` palette with static verb registry (`src/eawf/tui_v2/palette/verbs.py`); per-screen filter; fuzzy match; `@`-mention completion for paths; supports all V11 wave-board verbs + cross-screen verbs (`/find`, `/filter`, `/sort`, `/theme`, `/events`, `/metrics`, `/pr`, `/help`, `/quit`). | C00 §C06 [1:594] + D13 |
| G7 | Modal stack with depth cap 3; Esc pops one level; ModalScreen primitives for audit-running, audit-failed (mutating menu), detail (h/d/m/e/dp), pr-list, edit-field, config (tabbed), plan-preview, needs-user-AUQ, help, confirm. | D14 + V07 / V13 [6:626-647] + V11 [6:553-582] |
| G8 | Wong 2011 deuteranopia-safe palette; runtime swap via `/theme <name>` palette verb (dark / light / cb / auto); persists to `<local-path>`. | C00 §C06 [1:608] + D12 |
| G9 | Arrows-only nav primary; vim aliases (h/j/k/l/gg/G); full key names (`PageUp`/`PageDown`/`Home`/`End`/`Enter`/`Esc`); fixed in code; per-screen Bindings tables. | D11 + tui-ux-resolved [7:16-25] |
| G10 | V5 runtime-switched banner: header runtime cell color-coded; 3-second transition toast; persistent indicator until acknowledged or wave closes. | V5 [1:127-151] + D8 |
| G11 | V7 `/metrics` overlay: 3×2 dashboard tile grid (variance / weekly burn / wave elapsed / cache health / switchover freq / per-runtime tokens); 5-second refresh against daemon telemetry projection; tile click drills to filtered view. | V7 [1:184-224] + D9 |
| G12 | Plan-mode preview from PhaseSpec + IterSpec + WaveSpec aggregate per C04 D5 [5:140-144]; 3-option AUQ (approve / edit / reject). Approve runs `eawf roadmap apply <P##>` only; operator runs activate separately per D4 override. | C04 D5 + D4 override |
| G13 | Edit Plan: TUI dispatches Edit Plan subagent via `eawf agent dispatch` with C04 §5.7 prompt template [5:1146-1185]; re-renders plan preview on subagent's `agent_end` report. | D5 + C04 §5.7 |
| G14 | Mutating audit overlay: full menu (retry / split / land-partial / abandon / scope-change) routes each action via `eawf agent dispatch` subagent; subagent return drives state update; TUI observes via event stream. | P20 round 9 [2:484-489] + D17 |
| G15 | `/pr` overlay: global palette verb; lazy `gh pr list --json` shell-out on first open; 60 s cache; Enter opens `gh pr view --web`; gracefully degrades if `gh` missing. | tui-ux-resolved §`:pr overlay` [7:418-450] + D21 |
| G16 | Per-screen snapshot fixtures via Textual `Pilot.press(...)` + `app.save_screenshot()` to SVG; ~12 snapshots total covering repo / workspace / user / wave-board / each overlay; `EAWF_SNAPSHOT_REGEN=1` regenerates. | D20 |
| G17 | Asciinema cast determinism: SVG snapshot sequence captured by Pilot harness at fixed cadence (monotonic clock); cast composed offline; CI-stable; no real-time terminal recording. | D7 + P20 round 9 [2:485-489] |
| G18 | Onboarding splash per scope on first launch; persistent dismissed flag in `<local-path>` per scope. | tui-ux-resolved [7:165-227] + D15 |
| G19 | `?` help modal full keymap overlay; persistent footer hints adapt per focused screen. | D23 |
| G20 | needs_user envelope rendering: modal AUQ overlay reading `body.user_question` from C04 envelope; operator pick writes back via daemon's `event.jsonl` `needs_user_pause` resume path per C04 D4 [5:130-134]. | D24 + C04 D4 |
| G21 | Web stub: daemon-side WebSocket bridge protocol + auth contract specified; concrete SPA implementation deferred to web-cluster brief; deployment local-only first. | D6 |

### Non-goals

| NG# | Non-goal | Why deferred |
|---|---|---|
| NG1 | Rewriting in-place over `src/eawf/tui/`. | P20 direction §"Critical contracts" [2:582-588] mandates parallel `src/eawf/tui_v2/` tree; legacy stays as historical artifact until P22 cutover. |
| NG2 | TUI-mediated state mutation. | V1 [1:24-53] — daemon is single canonical writer. TUI proxies via CLI verbs / `eawf agent dispatch`. |
| NG3 | Inline LLM execution. | Subagent dispatch is daemon-mediated per C02 §5.13. TUI watches event stream; does not host the subprocess. |
| NG4 | Replacing `rich` in CLI output. | `rich` stays for CLI surface; Textual is TUI-only. C05 owns CLI rendering. |
| NG5 | SPA bundle in C06 implementation phase. | D6 — defer SPA stack pick to web-cluster brief. C06 ships the WS bridge contract only. |
| NG6 | Mouse support. | P20 round 5 [2:451-457] — keyboard-only invariant. |
| NG7 | Configurable hotkeys. | D11 — fixed in code; rebindable surface deferred to v0.6+. |
| NG8 | `/skill` + `/mcp` overlays. | P20 round 17 [2:546-553] — deferred to v0.4. |
| NG9 | Pane resize keys. | P20 round 6 [2:457-464] — deferred to P21. |
| NG10 | Bell on attention. | P20 round 6 [2:457-464] — deferred. |
| NG11 | Color-blind theme switch verb. | Wong 2011 already CB-safe; explicit `cb` mode aliases default. Switch verb deferred to v0.4. |
| NG12 | Per-runtime statusline integration. | C07 owns the per-runtime adapter UI surface. C06's `/metrics` covers in-TUI metrics; CC statusline is a separate surface. |
| NG13 | TUI on Windows-native terminal. | v0.3-v0.5 supports POSIX terminals (Linux + macOS + WSL2 on Windows). Native Windows terminal rendering quirks (per `tui-design-direction` §"Terminal / font findings" [2:337-378]) deferred. |
| NG14 | TUI-side audit-DSL editing. | Audit-DSL editing routes to `eawf audit init` CLI; TUI's `/audit` palette verb just dispatches. |

## 3. Prior verdicts cited

Five C00 verdicts, four C02 decisions, four C03 decisions, three C04 decisions, and the P20 direction brief's RC-1..RC-9 + 17 rounds of design picks are load-bearing.

### V1 — eawfd daemon Day-1 + smart-spawn writer [1:24-53]

> "Mutations to `state.json` (and all future stateful surfaces — config layers, registry, event log) route through the eawfd daemon. CLI auto-spawns daemon on demand if not running. Reads MAY bypass daemon (direct file IO + Pydantic load) for: CI environments without long-running processes, read-only one-shot CLI calls, recovery shell when daemon broken or version-skewed."

**C06 binding.** The TUI is a long-lived process (per [3:225]: "TUI — long-lived; subscribes to `event.subscribe` and renders push frames"). It MUST subscribe to the daemon on launch via the JSON-RPC `event.subscribe` method specified in C02 §5.3.2 [3:300]. The TUI is a *passive consumer* of the daemon's mutation stream — it never imports `eawf.state.writer` directly; every operator action that mutates state proxies to a CLI verb (or `eawf agent dispatch` for subagent-driven flows). When the daemon is unavailable, TUI falls back to the mtime-poll path per D2 + D19; this matches the V1 carve-out for read-only consumers [1:30-32] but the operator sees a `daemon offline · poll mode 2s` banner so the degraded mode is explicit.

The cold-spawn budget (200-400 ms per V1 [1:44]) is consumed by the bare-`eawf` launch path: the CLI spawns the daemon on first state read; TUI then connects to the warm socket and issues `event.subscribe`. The cold-spawn cost is inside the <150 ms first-paint budget (G4) ONLY IF the TUI can render a placeholder skeleton during daemon-spawn; if the daemon is already up, both budgets compose to ~100 ms.

### V2 — Three-tier specs Phase + Iter + Wave [1:55-74]

> "PhaseSpec — phase charter: outcome statement, KPIs, success/failure modes, dependencies on prior phases, EU envelope, ship criteria. IterSpec — iter intent: sub-goal within phase, ordering rationale, wave grouping rationale, audit cadence. WaveSpec — wave deliverable: verdict citations, file scopes, behaviors, failure modes, tests, mockup (UI scopes only)."

**C06 binding.** Per C04 D5 [5:140-144] the plan-mode preview renders from the PhaseSpec + IterSpec + WaveSpec aggregate. C06 §5.3 defines the `PlanPreviewModal` widget that reads the three-tier spec aggregate (loaded via `eawf phase spec render <P##> --json` + `eawf iter spec render --json` + `eawf wave spec render --json`) and lays it out as a hierarchical tree. The detail overlay's "Spec" tab (per V11 [6:553-582]) lets operator drill into individual WaveSpec body fields without leaving the TUI.

C03 §5.7 [4:588-712] specifies the `verify_implements` audit-DSL kind. C06's audit-failed overlay surfaces per-wave missing-marker rows (the `details` field of the `CheckResult` returned by `verify_implements`) so operator can navigate from a failure to the responsible WaveSpec's `implements:` block via Enter → DetailModal.

### V3 — Composable profile bundle with declared precedence [1:76-96]

> "Project carries `profiles: [research, engineering, reverse-engineering, spike, ...]` ordered list. Each profile declares conflicts_with: [...] and overrides: [...]. Loader fails fast if conflict undeclared. Project carries explicit profile_priority: [a, b, c] for tie-breaks. Effective ruleset = union of profile contributions, conflict-resolved by precedence."

**C06 binding.** Per the resolved profile bundle (computed by C08), C06's screen composition filters widgets and palette verbs per `SkillVisibility.runtimes` and per profile gating. Example: the `/spike` palette verb is hidden when `research` profile is not enabled (per C04 §5.1 [5:208] profile-gating). The `verify_implements` audit-overlay branch is shown only when the `engineering` profile is enabled. C06 reads the resolved profile bundle from `state.config.profiles.enabled` (loaded via the daemon RPC); the bundle is re-evaluated on `daemon.reload_config` events per C02 §5.3.5 [3:336].

### V5 — Runtime fallback: reactive switchover on error [1:127-151]

> "Daemon uses reactive auto-switch on primary-runtime failure (HTTP 429 / 5xx / timeout / API-error). No active health-probe. On error, daemon flips the affected wave to the next runtime in the configured preference ladder and re-issues the dispatch envelope against that runtime with the idempotency key preserved."

**C06 binding.** Per D8 the header strip carries a dedicated `runtime` cell — color-coded (`accent` for normal, `warn` for "switched in last 60 s", `err` for "every runtime in ladder failed"). When the TUI receives a `runtime_switched` event via `event.subscribe`, it (a) updates the runtime cell in the header, (b) fires a 3-second toast carrying `runtime: claude → codex (cause: rate_limit)`, (c) appends a row to the `:events` ring buffer. The runtime cell remains in `warn` color until the next `wave_close` event for the affected wave OR until the operator presses `r` (manual acknowledge clears the cell). When every runtime in the preference ladder fails (per V5 [1:147]), the daemon emits a `runtime_unavailable` operator-notify envelope and flips the wave to `BLOCKED`; C06's audit-failed overlay auto-opens with the failure detail.

The dispatch-history surface — `DispatchAnnotation` rows on the wave per C02 §5.13 [3:826-841] — is reachable via the `/wave dispatch` palette verb; the overlay lists each attempt's `runtime_from / runtime_to / cause / occurred_at`.

### V7 — Telemetry: vendor agent-lens schema, rebuild inside eawf [1:184-224]

> "Telemetry lives inside eawf, not as a separate sidecar. Audit the private agent-lens repo, extract its data model + visualization patterns + storage choices, then implement the equivalent under `src/eawf/telemetry/` backed by a user-scope store at `<local-path>`."

**C06 binding.** Per D9 the `/metrics` overlay renders a 3×2 dashboard tile grid backed by the daemon's telemetry projection. The tile inventory:

1. **Variance per bucket** (top-left) — bar chart of estimate vs actual EU per XS/S/M/L/XL bucket; 7d window.
2. **Weekly burn** (top-middle) — actual EU / target EU bar with target divisor (rendered only when `state.project.weekly_eu_target` is set per V24 [6:723-727]).
3. **Wave elapsed** (top-right) — median + p90 in seconds; sample size; sparkline of last 30 days.
4. **Cache health** (bottom-left) — `cache_creation_input_tokens` vs `cache_read_input_tokens` ratio; flag when ratio is unhealthy per V7 [1:202-203].
5. **Switchover frequency** (bottom-middle) — count of `runtime_switched` events per 7d / 30d window; broken down by `cause` (rate_limit / server_error / timeout / api_error).
6. **Per-runtime tokens** (bottom-right) — input / output / cache-create / cache-read split per runtime (claude / codex / opencode); 7d window.

Each tile refreshes on a 5-second timer (`set_interval(5.0, self._refresh_tile)`). Tile click drills to a filtered view: e.g. clicking "Variance" opens a per-wave variance table; clicking "Switchover" opens an event-log filter scoped to `runtime_switched` kind. The `/metrics` verb accepts arguments: `/metrics --scope <urn>` filters to a specific scope; `/metrics --window 7d|30d|90d` switches the rolling window per V7 [1:198-201].

### C02 §5.3.2 — `event.subscribe` JSON-RPC method [3:296-310]

> "`state.subscribe` `{scope_id?: str, since_version?: str, event_kinds?: list[StoreKind]}` — streaming `event.push` notifications. Stream-receive method. Daemon pushes a `event.push` notification per matched envelope until subscriber disconnects. `since_version` lets subscriber catch up on missed events from `event.jsonl`."

**C06 binding.** §5.4 specifies the `StateBinding` reactive layer that wraps `event.subscribe`. The TUI issues one subscription per launch; reconnects with `since_version=<last_event_id>` on disconnect. C02 §5.7 [3:434-454] specifies the bounded queue (per-subscriber 1024 events default per D7 of C02) and the disconnect-on-overflow semantics. C06 surfaces overflow as a degraded-banner with reconnect button: `event stream lost · press r to resync`.

### C02 §5.13 — Session-handle tracking + DispatchAnnotation [3:792-841]

C06 reads the `state.waves[*].sessions: dict[int, SessionAttempt]` field via the daemon's `state.read` method per C02 §5.3.1 [3:298]. The `/wave dispatch` palette verb renders the per-attempt rows: `runtime`, `session_id`, `started_at`, `ended_at`, `exit_status`, `subprocess_pid`, token splits. The `DispatchAnnotation` list shows the V8-hybrid path: `FRESH_DISPATCH` / `CONTINUE_FROM_SESSION` / `CONTINUE_FAILED_FELL_BACK_TO_FRESH` / `SWITCH_ON_ERROR` / `SWITCH_MANUAL`.

### C03 §5.4 — WaveSpec schema [4:352-433]

C06's plan-mode preview + detail overlay render WaveSpec body fields: `title`, `agent_role`, `effort_bucket`, `deps`, `file_scopes`, `implements: list[VerdictCitation]`, `behaviors: list[WaveBehavior]`, `failure_modes`, `tests`, `mockup`. The audit-failed overlay surfaces per-wave `verify_implements` failures (missing verdict markers) with a direct link to the WaveSpec `implements:` block.

### C03 §5.6 — AuditSpec.cadence [4:546-583]

The cadence field (`on_wave_close` / `on_iter_close` / `on_phase_close` / `manual`) determines when the audit fires. C06's audit-running overlay auto-opens when the TUI receives an `audit_started` event for a scope visible in the current screen; the overlay shows per-check progress (✓ / ✗ / · spinner) until the `audit_completed` event.

### C04 D5 — Plan-mode preview render source [5:140-144]

> "Plan-mode preview rendered from PhaseSpec + IterSpec + WaveSpec aggregate."

**C06 binding.** §5.3.7 specifies the `PlanPreviewModal` widget. It loads the three-tier spec aggregate via three daemon RPC calls (`{phase,iter,wave} spec render --json`); composes a hierarchical Tree widget; surfaces a 3-option AUQ (approve / edit / reject) via a custom modal screen. The fourth option slot (`research_more`) per C04 [5:1131-1132] is reserved for re-issuing `/research` before deciding.

### C04 D4 — needs_user pause context [5:130-134]

> "Store needs_user pause context as `needs_user_pause` envelope appended to event.jsonl (+ flow_checkpoint for /flow)."

**C06 binding.** §5.5 specifies the `NeedsUserModal` screen. When the TUI receives a `needs_user_pause` envelope via `event.subscribe`, it auto-opens the modal carrying the envelope's `body.user_question: UserQuestion`. The operator picks an option via ↑↓ + Enter; the TUI runs `eawf skill resume <pause-urn> --choice <label>` via the daemon RPC. The skill resumes from the paused state per the C04 §5.7 resume flow [5:1187-1206].

### C04 §5.7 — Edit Plan subagent flow [5:1142-1216]

**C06 binding.** Per D5 the TUI's Edit Plan action dispatches the Edit Plan subagent via `eawf agent dispatch`. The dispatch envelope carries the C04 §5.7 prompt template [5:1146-1185] + the operator's feedback string (collected via an inline `Input` widget). The TUI watches the daemon's event stream for the subagent's `agent_end` envelope; on completion, re-renders the plan-mode preview from the updated spec aggregate. The loop continues until operator picks Approve or Reject.

### P20 direction — RC-1..RC-9 + 17 rounds [2]

Every C06 axis is informed by P20 direction picks; the citation lines are inlined in each Decision row in §4 and in the Goals table above. The key contracts ported into C06:

- **Spec infrastructure ships first** [2:584-588] — C03 ships ahead of C06 in the cluster DAG; C06 implementation phase (TBD: P22+) MUST land *after* C03 implementation phase.
- **Branch + procedure** [2:586] — current `feature/eawf-v0.3-p20` stays as a dead historical artifact; the rebuild happens on a fresh long-running branch (`feature/eawf-v0.3-c06-tui` candidate, finalized by the implementing phase's prep).
- **Success-criteria contract** [2:176-212] — every wave's spec carries verdict citations + observable behaviors + named failure modes + concrete tests.
- **Ship-gate `verify-implements` audit** [2:583-588] — C03's `verify_implements` audit-DSL kind, integrated into C09 + every C06 implementation wave.

## 4. Decision matrix

Twenty-four axes ratified by operator AUQ on 2026-05-17. One override row marked **OVERRIDE** below; all others matched the brief recommendation. Each row captures the question, the options considered, the operator-confirmed pick, and the rationale.

| # | Axis | Options considered | Recommendation | Rationale |
|---|---|---|---|---|
| **D1** | Daemon-push protocol | (a) JSON-RPC `event.subscribe` over same UDS; (b) dedicated WebSocket on secondary UDS; (c) long-poll HTTP-style | **(a) — JSON-RPC `event.subscribe`** | Reuses C02 §5.3.2 method catalog [3:300]. One socket per TUI connection; streaming notifications via JSON-RPC. Catch-up via `since_version` field. No extra IPC surface; stdlib-friendly per F5 [10:77-87] daemon concurrency model. |
| **D2** | Mtime-poll fallback cadence + indicator | (a) Poll every 2 s + degraded-banner; (b) poll every 1 s + no banner; (c) poll every 5 s + banner + retry-connect button | **(a) — 2 s + degraded-banner** | Per P20 direction round 8 [2:476-481]. 2 s default balances responsiveness vs `state.json` parse cost (~1 ms on 200 KB). Banner: `daemon offline · poll mode 2s`. Operator sees the degraded mode is explicit. |
| **D3** | Widget reusability | (a) Shared chassis (Header/Footer/Heartbeat) + per-scope composition; (b) per-scope widget tree; (c) hybrid chassis + per-scope content widgets | **(a) — shared chassis** | RC-9 from P20 direction [2:99-101]: 5300 LOC of duplicated chassis. One `Header(Static)` + `Footer(Static)` + `Heartbeat(Static)` reused across `RepoScreen` / `WorkspaceScreen` / `UserScreen` / `WaveBoardScreen`. YAGNI-conformant; trims salvageable LOC from ~5300 to ~2500 [2:122-126]. |
| **D4** | Plan-mode preview Approve target binding | (a) One-click chain `roadmap apply` + `phase activate`; (b) `roadmap apply` only; (c) `phase activate` only | **(b) — `roadmap apply` only (OVERRIDE)** | Operator picked (b) over the brief's recommended (a). Rationale (operator-driven): two-step ratification is preferable — Approve commits the spec via `roadmap apply`, but the operator decides separately when to activate the phase via `eawf phase activate` (typically right after via a `/prep` flow). Lower blast radius; gives operator one explicit confirmation point before subagents start spawning. C06 binds Approve to `roadmap apply` only; the post-apply state surfaces a `next: eawf phase activate <P##> or /prep <P##>` hint in the body footer. |
| **D5** | Edit Plan invocation surface | (a) Dispatch via `eawf agent dispatch` w/ Edit Plan prompt template; (b) inline `Input` + direct `roadmap revise` verbs; (c) exit TUI, operator runs `/roadmap revise` manually | **(a) — subagent dispatch** | Per P20 direction round 11 [2:498-505] and C04 §5.7 [5:1142-1216]. Subagent runs in background daemon-spawned subprocess; TUI watches event stream for `agent_end`. Re-renders plan preview on completion. Aligns with V1 single-canonical-mutator. |
| **D6** | Web stub stack pick | (a) Defer to web-cluster brief; (b) Preact + signals; (c) SvelteKit; (d) Tauri wrapper | **(a) — defer to web-cluster brief** | Web stub is out of scope for v0.3-v0.4 implementation. C06 specs the daemon-side WebSocket bridge contract + auth model + deployment shape (§5.11); concrete SPA pick (Preact / Lit / Svelte / Tauri) deferred to a future cluster (candidate: C12 — Web Operator Surface, when the GitHub / Linear / Slack integrations need a web entrypoint). |
| **D7** | Asciinema cast determinism | (a) Pilot.press + save_screenshot SVG sequence at fixed cadence; (b) real asciinema recording w/ 30fps capture; (c) defer to v0.4 release | **(a) — Pilot + SVG sequence** | P20 direction round 9 [2:485-489]. Pure deterministic. No real terminal recording; SVG snapshots drive the cast. Frame timing synthesised from a monotonic clock. CI-stable; goldens stay byte-stable. Cast composed offline by `eawf tui asciinema <script> --out <path>`. |
| **D8** | V5 runtime-switched banner placement | (a) Header runtime cell + 3-sec toast; (b) status pane row only; (c) toast-only | **(a) — header cell + toast** | Header line shows `runtime: claude → codex` (warn color) when a switch occurred in the active wave's last 60 s. Transition fires a 3-second toast. Persistent indicator until acknowledged or wave closes. Most prominent without being permanently noisy. |
| **D9** | `/metrics` overlay tile layout + refresh cadence | (a) 3×2 grid w/ 5 s refresh; (b) scrollable list; (c) tabbed | **(a) — 3×2 grid + 5 s refresh** | Per P20 direction round 15 [2:533-537] + V7 telemetry catalog [1:184-224]. 6 tiles cover the V7 metrics surface. 5 s tick is well below user-perception threshold but cheap on the DuckDB read. Tile click drills to filtered view. |
| **D10** | Scope dispatch ladder | (a) cwd → workspace > repo > user > status; (b) AUQ at every launch; (c) sticky cached scope overrides cwd | **(a) — cwd ladder** | Per workspace-and-user-tui §1 [8:38-58]. Walk cwd upward: `.ea/state.json` resolves to workspace → workspace screen; repo → repo screen; outside any scope w/ populated registry → user screen; otherwise `eawf status` text fallback. Flag overrides: `EA_STATE` env > `--user` > `--workspace` > `--repo` > cwd. |
| **D11** | Keybindings policy | (a) Fixed in code w/ arrows + PgUp/PgDn/Home/End + vim aliases; (b) fixed globals + rebindable palette verbs; (c) fully rebindable via `<local-path>` | **(a) — fixed in code** | Per P20 direction round 7 [2:467-472]. One canonical keymap; no per-user rebinding in v0.3-v0.5. Reduces test matrix to single permutation. Per-screen Bindings tables in Textual express the policy. |
| **D12** | Wong 2011 palette runtime swap | (a) `/theme` palette verb w/ `<local-path>` persistence; (b) `EAWF_THEME` env only; (c) auto-detect only | **(a) — `/theme` verb + session persist** | Per P20 direction round 7 [2:467-472]. `/theme dark` reloads Textual CSS at runtime via `Theme.from_dict`. Light / dark / auto (COLORFGBG-detect). Mode persists per-scope in `<local-path>` `theme.<scope_kind>` field. Dark-assumption default when `EAWF_THEME` + `COLORFGBG` both unset (Q9 ratified). `cb` value REJECTED by validator in v0.3 per **Q4 OVERRIDE** — tighter CB palette deferred to v0.4. |
| **D13** | `/` palette verb registry source | (a) Statically declared in `palette/verbs.py`; (b) plugin-contributable via SkillManifest; (c) static core + plugin-extensible | **(a) — static registry** | Frozen at code level. Each verb declares its name, hint, handler, allowed-scope set. Palette filters by current screen. Easier to audit + test + ratify the verb surface. Plugin-extensibility deferred to v0.5+ when the skill manifest's `palette_verb` field can ratify the round-trip. |
| **D14** | Modal stack depth cap | (a) Cap at 3; (b) unlimited; (c) cap at 2 | **(a) — cap at 3** | Per tui-ux-resolved §Detail overlay [7:586-606]. Three is enough for plan-mode → edit → confirm flows; deeper risks operator getting lost. Esc pops one level. Attempts to push a 4th modal emit a toast `modal stack depth limit (3) reached — close one first`. |
| **D15** | Onboarding splash gating | (a) Per-scope splash + persistent dismissed flag; (b) universal splash; (c) no splash, rely on `?` help modal | **(a) — per-scope splash** | Per tui-ux-resolved §Empty-state onboarding splash [7:165-227]. Three variants tailored to next CLI command. Dismissed flag in `<local-path>` is per-scope key (`onboarding_dismissed.<scope_kind>`). |
| **D16** | Performance budget enforcement | (a) <150 ms first paint + <50 ms keypress→render w/ CI gate; (b) <200 ms + <100 ms w/ CI warn only; (c) no perf gate | **(a) — strict gate** | Per P20 direction round 16 [2:538-544]. CI fails build when p99 exceeds budget; measured via Pilot harness on fixture state.json w/ 50 phases. Regressions blocked before merge. |
| **D17** | Mutating audit overlay v0.3 scope | (a) Full mutating menu via subagent dispatch; (b) read-only failure overlay only (V07 original); (c) retry-only menu | **(a) — full mutating menu** | P20 direction round 9 [2:484-489] — supersedes V07 read-only. Each menu choice dispatches a `/flow` worker via `eawf agent dispatch`. Subagent return drives state update; TUI observes via event stream. Operator stays in the TUI for the full repair loop. |
| **D18** | Roadmap tree load strategy | (a) Hybrid lazy (last 5 eager, older lazy); (b) all eager; (c) current iter only | **(a) — hybrid lazy load** | P20 direction round 15 [2:533-537]. Current iter + last 4 closed phases load fully; older phases show iter count only until operator expands. Honors <150 ms first-paint budget on 50-phase project. |
| **D19** | Reactive state binding | (a) Daemon-push primary + mtime-poll fallback; (b) mtime-poll primary + daemon push optional; (c) daemon-push only | **(a) — daemon-push primary + fallback** | Per C02 §5.3.2 + this brief D1. TUI subscribes on launch; reads push frames; falls back to 2 s mtime poll on disconnect per D2. Keeps the TUI functional when daemon is down (V1 carve-out) while exploiting the push affordance when up. |
| **D20** | Snapshot testing fixture coverage | (a) Per-screen ~12 SVG snapshots; (b) per-widget ~30 snapshots; (c) integration-only | **(a) — per-screen with ASCII text fixtures (Q-new1 OVERRIDE)** | Per P20 direction round 9 + V20 [6:694-697]. Each screen + each overlay gets its own snapshot; `Pilot.press(...)` drives the state to a known position. **Q-new1 OVERRIDE** picks ASCII text snapshots (`app.export_screen_text()` returning ANSI-coded text) over SVG (`app.save_screenshot()`) to avoid Python-version + Textual-version byte-drift. Fixtures live at `tests/snapshots/tui/*.txt`. Goldens regenerated via `EAWF_SNAPSHOT_REGEN=1`. ~16 fixtures: repo / workspace / user / wave-board / detail-overlay × {hypothesis, decision, memory, events, dispatch} / audit-running / audit-failed / config / plan-preview / metrics / pr-list. |
| **D21** | `/pr` overlay placement + cache TTL | (a) Global palette verb w/ lazy gh shell-out + 60 s cache; (b) workspace + user scopes only w/ 30 s cache; (c) defer to v0.4 | **(a) — global verb + 60 s cache** | Per tui-ux-resolved §`:pr overlay` [7:418-450]. Cached per scope; degrades gracefully if `gh` missing. Same overlay reachable from any screen. 60 s cache balances freshness vs `gh pr list` cost (~200-500 ms per scope). |
| **D22** | Heartbeat presentation | (a) `•` pulse + degraded color + r double-pulse ack; (b) numeric tick counter only; (c) no heartbeat | **(a) — pulse + color + r ack** | Per tui-ux-resolved §Heartbeat colour state [7:330-345]. Visual proof TUI is live; clear failure indicator. Color follows accent (green) → err (red) when any pane in error state. `r` triggers a 0.5 s double-pulse so operator sees the manual refresh ack. |
| **D23** | Help discovery surface | (a) Persistent footer hints + `?` modal; (b) footer hints only; (c) `?` modal only | **(a) — both** | P20 direction round 4 [2:444-448]. Footer adapts to focused screen; `?` opens a full-screen overlay listing every key + palette verb. Best discoverability without bloating the chrome. |
| **D24** | needs_user envelope rendering | (a) Modal AUQ overlay reading `body.user_question`; (b) toast + manual `eawf skill resume`; (c) inline footer chip | **(a) — modal AUQ overlay** | Per C04 §5.7 + D4 [5:130-134]. TUI subscribes to `needs_user_pause` envelopes; auto-opens modal for the active scope/session; operator pick routes back to the paused skill via daemon's resume RPC. Best continuity for operator flow. |
| **D25** | Edit Plan subagent cap (Q14) | (a) Custom 300 s cap; (b) inherit C02 1800 s cap; (c) no cap | **(a) — 300 s cap** | Plan edits should be sub-5-min. Daemon C02 §5.8 [3:456-468] per-wave cap is 1800 s — too lax for interactive plan-revision. TUI passes `--wave-wall-clock-cap=300` to `eawf agent dispatch`; failure surfaces in PlanPreviewModal status line. |
| **D26** | Perf budget fixture size (Q7) | (a) 50 phases × 3 iters × 5 waves = 750 waves; (b) 100 phases (1500 waves); (c) 20 phases | **(a) — 50 phases** | Stress-test realistic large project. Captures perf ceiling without unrealistic stress. CI runs `tests/perf/tui/` against `fixture_50_phase()` for first-paint + keypress→render gates. |
| **D27** | Subagent streaming feedback (Q10) | (a) Modal status line; (b) dedicated DispatchLogModal on top; (c) footer ticker | **(a) — modal status line** | AuditFailedModal stays focused on active audit; events drive updates. Single overlay; no modal-stack-depth pressure (D14 cap = 3). Status line renders `dispatching <action> → <runtime> · attempt <n>` then `closed` on subagent return. |
| **D28** | StateBinding apply-envelope failure (Q-new2) | (a) Full state reload via `state.read`; (b) flip to mtime-poll fallback; (c) operator-choice modal | **(a) — full state reload** | Safest: on apply failure, refetch authoritative state. Loses incremental optimization but keeps state coherent. Already in `_subscribe_loop` exception handler per §5.4. |
| **D29** | Header `runtime` cell idle state (Q-new3) | (a) `runtime: idle` muted; (b) hide cell entirely; (c) `runtime: <last> (stale)` | **(a) — `runtime: idle` muted** | Persistent indicator; clear that runtime field exists. Operator sees "nothing dispatched" at a glance. Color flips from muted (idle) → accent (active) → warn (switched in last 60 s) → err (every runtime in ladder failed). |
| **D30** | `--onboarding` flag w/ already-dismissed (Q-new4) | (a) Re-show, keep dismissed flag; (b) re-show + clear flag; (c) require `--force` | **(a) — one-shot re-show** | Per tui-ux-resolved §Empty-state onboarding splash [7:225-226]. Flag stays dismissed for normal launches; `--onboarding` is the explicit operator command to resurface. No state mutation on the flag. |
| **D31** | Rapid `?` while help modal open (Q-new5) | (a) Ignore subsequent `?` (no-op); (b) toggle (Esc + re-open); (c) push second help modal | **(a) — no-op** | Help modal already at depth 3 cap (potentially). Second `?` is no-op until first dismissed. Prevents accidental double-dismiss + stack-cap exhaustion. |
| **D32** | `:` legacy alias (Q12 OVERRIDE) | (a) `:` alias forever; (b) deprecated v0.4 + removed v0.5; (c) removed immediately v0.3 | **(c) — removed immediately (OVERRIDE)** | Operator picked clean surface over v0.2.x muscle memory. Brief had recommended (a). Palette opens on `/` only. `:` keypress falls through to default keybinding (no-op or scoped to focused-widget). Footer hints + help modal updated; no migration help shown for `:` users. |
| **D33** | Strip cap configurability (Q15 OVERRIDE) | (a) Fixed at 8; (b) configurable `tui.workspace.strip_max_rows`; (c) adaptive | **(b) — configurable (OVERRIDE)** | Operator picked C08 layered-config field. Brief had recommended (a). Default 8; operator overrides per-workspace via `.ea/config.yaml` or per-user via `<local-path>`. Validator: integer 3-32 range. Larger projects can opt into deeper strip; tighter terminals reduce to 3. |
| **D34** | Asciinema frame_ms default (Q6 OVERRIDE) | (a) 100 ms; (b) 50 ms; (c) 200 ms | **(b) — 50 ms (OVERRIDE)** | Operator picked higher fidelity. Brief had recommended (a). Doubles cast file size (acceptable; casts run minutes not hours). `eawf tui asciinema --frame-ms <N>` overrides per-cast for special cases. |

## 5. Proposed schema, API, protocol

The body of the brief — module tree, widget catalog, palette verb registry, modal stack inventory, daemon-push protocol binding, theming, performance budget enforcement, snapshot harness, asciinema generation, web stub WS contract.

### 5.1 TUI module tree

```
src/eawf/tui_v2/                              # new tree (parallel to legacy src/eawf/tui/)
├── __init__.py                               # exports `EaApp`, `run_tui`
├── app.py                                    # EaApp(App): scope routing, key bindings, theme load
├── theme.css                                 # Textual CSS: colors, padding, layout, focus
├── theme.py                                  # Theme.from_dict(...) for runtime swap
├── state_binding.py                          # StateBinding: daemon `event.subscribe` + mtime fallback
├── scopes/
│   ├── __init__.py
│   ├── repo.py                               # RepoScreen(Screen): 2x2 quadrant
│   ├── workspace.py                          # WorkspaceScreen(Screen): top-strip + active-repo quadrant
│   ├── user.py                               # UserScreen(Screen): attention / effort / portfolio
│   └── status_fallback.py                    # non-TTY: render plain status text
├── widgets/
│   ├── __init__.py
│   ├── header.py                             # Header(Static): Eä brand + breadcrumb + runtime cell + clock
│   ├── footer.py                             # Footer(Static): context-aware key hints
│   ├── heartbeat.py                          # Heartbeat(Static): pulse + degraded color
│   ├── roadmap_tree.py                       # RoadmapTree(Tree[dict]): V12 glyphs + EU bar inline
│   ├── eu_bar.py                             # EUBar(Static): 5-cell glyph bar
│   ├── status_pane.py                        # StatusPane(Static): scope/phase/wave/audits/blockers/runtimes/worktrees
│   ├── git_pane.py                           # GitPane(Static): branch + status + last commits + ahead/behind
│   ├── backlog_table.py                      # BacklogTable(DataTable): sortable + filterable
│   ├── config_row.py                         # ConfigRow(Horizontal): label-left value-right ←/→ cycle
│   ├── choice_button.py                      # ChoiceButton(Static): focus = solid accent fill
│   └── eu_meter.py                           # EUMeter(ProgressBar): per-wave EU burn bar
├── screens/
│   ├── __init__.py
│   ├── wave_board.py                         # WaveBoardScreen(Screen): list + detail
│   ├── help.py                               # HelpScreen(ModalScreen): full keymap overlay
│   └── overlays/
│       ├── __init__.py
│       ├── audit_running.py                  # AuditRunningModal(ModalScreen): live spinner per check
│       ├── audit_failed.py                   # AuditFailedModal(ModalScreen): mutating menu (D17)
│       ├── detail.py                         # DetailModal(ModalScreen): h/d/m/e/dp shared
│       ├── pr_list.py                        # PrListModal(ModalScreen): /pr overlay (D21)
│       ├── edit_field.py                     # EditFieldModal(ModalScreen): config field editor w/ validators
│       ├── config_modal.py                   # ConfigModal(ModalScreen): tabbed config form
│       ├── plan_preview.py                   # PlanPreviewModal(ModalScreen): 3-button AUQ (D4 OVERRIDE)
│       ├── needs_user.py                     # NeedsUserModal(ModalScreen): UserQuestion AUQ (D24)
│       ├── metrics.py                        # MetricsModal(ModalScreen): 3×2 tile grid (D9)
│       ├── events.py                         # EventsModal(ModalScreen): ring-buffer 50 + filter
│       └── confirm.py                        # ConfirmModal(ModalScreen): destructive-op arrow-toggle
├── palette/
│   ├── __init__.py
│   ├── command_palette.py                    # CommandPalette(ModalScreen): Input + OptionList + fuzzy match
│   ├── mention_completion.py                 # @-mention path popover
│   └── verbs.py                              # static verb registry (D13)
├── snapshot/
│   ├── __init__.py
│   ├── pilot_harness.py                      # Pilot driver for per-screen snapshots
│   ├── fixtures.py                           # fixture state.json builders (50-phase, audit-failed, ...)
│   └── asciinema.py                          # SVG sequence → cast composer (D7)
└── runtime_filter.py                         # profile-aware widget/palette filter (V3)
```

Legacy `src/eawf/tui/` (5300 LOC per [2:122]) stays unchanged through the C06 implementation phase; the cutover happens at the closing wave per §7 migration plan.

### 5.2 Scope dispatch ladder

`cli/app.py` bare-command handler implements the dispatch algorithm per D10. Pseudo-code (matches existing `state/resolve.py` ladder extended with TTY + scope):

```python
# src/eawf/cli/app.py — bare command handler
def _bare_command(settings: Settings) -> int:
    if not sys.stdout.isatty() or settings.plain or settings.no_input or os.environ.get("EAWF_NO_TUI") == "1":
        return run_status_command()
    if settings.user_flag:
        return run_user_tui()
    if settings.workspace_flag:
        return run_workspace_tui(settings.workspace_flag)
    if settings.repo_flag:
        return run_repo_tui(settings.repo_flag)
    # cwd-upward resolution
    state_path = resolve_state_upward(Path.cwd())
    if state_path is not None:
        scope_kind = read_scope_kind(state_path)
        if scope_kind == "workspace":
            return run_workspace_tui(state_path)
        if scope_kind == "repo":
            return run_repo_tui(state_path)
    # outside any scope
    if registry_is_populated(Path.home() / ".eawf" / "registry.json"):
        return run_user_tui()
    return run_status_command()
```

Precedence (most-specific wins): `EA_STATE` env > `--user` > `--workspace` > `--repo` > cwd resolve > registry-populated user-scope > status fallback.

The four scope entries (`run_user_tui`, `run_workspace_tui`, `run_repo_tui`, `run_status_command`) each spawn the appropriate Textual `App` subclass (or, for status, emit the existing rich-table text). The TUI App instance binds to the daemon on launch:

```python
# src/eawf/tui_v2/app.py
class EaApp(App[None]):
    """Single app, three scope screens; chosen via push_screen on launch."""
    CSS_PATH = "theme.css"
    BINDINGS = _GLOBAL_BINDINGS
    SCREENS = {
        "repo":      RepoScreen,
        "workspace": WorkspaceScreen,
        "user":      UserScreen,
        "wave_board": WaveBoardScreen,
    }

    def __init__(self, scope: Literal["repo","workspace","user"], state_path: Path | None) -> None:
        super().__init__()
        self._scope = scope
        self._state_path = state_path
        self._binding: StateBinding | None = None

    async def on_mount(self) -> None:
        self._binding = StateBinding(state_path=self._state_path)
        await self._binding.connect()  # spawns daemon if absent; subscribes
        self.push_screen(self._scope)
        # First paint triggers within on_mount → first paint <150 ms enforced by D16
```

Scope switching (Ctrl-R / Ctrl-W / Ctrl-U from any screen) pops to base then pushes the target scope screen. `WaveBoardScreen` is a push-on-`w`-keypress from `RepoScreen` (not a scope; a sub-screen).

### 5.3 Widget catalog

One row per widget. Type column names the Textual primitive used (or `Static` composite for hand-rolled). Per-widget responsibilities listed.

| Widget | Type | File | Responsibilities |
|---|---|---|---|
| `Header` | `Static` composite | `widgets/header.py` | Eä brand (bold accent), `│` separator, scope breadcrumb (`<scope_kind> ❯ <code> ❯ <branch>`), version, runtime cell (D8 color-coded), heartbeat dot, clock (`HH:MM UTC` or `HH:MM UTC±N` per `EAWF_TZ`). |
| `Footer` | `Static` composite | `widgets/footer.py` | Context-aware key hints; adapts on focused screen change; full key names only (`PageUp`, `PageDown`, `Enter`, `Esc`). |
| `Heartbeat` | `Static` | `widgets/heartbeat.py` | `•` pulse per tick; `accent` color default; `err` color when any pane degraded; 0.5 s double-pulse on `r`. |
| `RoadmapTree` | `Tree[dict]` | `widgets/roadmap_tree.py` | V12 glyph schema (`- > ~ # x !`); 5-cell EU bar inline; hybrid lazy load per D18; ←/→ collapse/expand; Enter on wave row → `WaveBoardScreen` scoped to wave. |
| `EUBar` | `Static` | `widgets/eu_bar.py` | 5-cell `#####`/`----` glyph bar; color `ok` ≤80%, `warn` ≤100%, `err` >100%; trailing pct text right-aligned. |
| `StatusPane` | `Static` composite | `widgets/status_pane.py` | scope / phase / wave / tests / audits / worktrees / blockers / parallel / SESSIONS sub-block (V8 [6:402-457]). |
| `GitPane` | `Static` composite | `widgets/git_pane.py` | branch + `git status --porcelain=v2 -b` summary + last 3 commits + ahead/behind; 1 s cache per `tui-layout.md` [9:84-89]. |
| `BacklogTable` | `DataTable` | `widgets/backlog_table.py` | sortable (priority / id / target); filterable via `/filter backlog`; cursor row Enter → `DetailModal`. |
| `ConfigRow` | `Horizontal` composite | `widgets/config_row.py` | label cell (32-col fixed) + value cell + ←/→ cycle for enum / bool; Enter opens `EditFieldModal` for str/int/float/path. |
| `ChoiceButton` | `Static` | `widgets/choice_button.py` | focus = solid accent fill; click / Enter = trigger; replaces stock Textual `Button` (avoids white-block focus per [2:319-334]). |
| `EUMeter` | `ProgressBar` | `widgets/eu_meter.py` | per-wave EU burn bar; color `ok` / `warn` / `err`; over-budget pulses (D7-derived). |

Per-screen composition (concrete layouts per scope follow):

**RepoScreen (2x2 quadrant)** — composes `Header` + body grid + `Footer`. Body grid:
- top-left: `RoadmapTree` (scope = current repo's all phases, hybrid lazy)
- top-right: `StatusPane`
- bottom-left: `GitPane`
- bottom-right: `BacklogTable`

**WorkspaceScreen (strip + zoom)** — composes `Header` + top strip + bottom quadrant + `Footer`. Top strip: `DataTable` row per linked repo (per `workspace-and-user-tui.md` [8:67-91]). Bottom quadrant: same as RepoScreen but scoped to focused row.

**UserScreen (three sections)** — composes `Header` + three vertical sections (attention `3:`, effort `2:`, portfolio `5:`) + `Footer`. Per `workspace-and-user-tui.md` §3 [8:166-208].

**WaveBoardScreen (list + detail)** — composes `Header` + `DataTable` (V11 sort: `(status_priority, wave_id_asc)`) + `Footer`. Enter → push detail card overlay.

### 5.4 Reactive state binding

`StateBinding` wraps the daemon `event.subscribe` path + the mtime-poll fallback. Per D1, D2, D19.

```python
# src/eawf/tui_v2/state_binding.py

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from textual.reactive import reactive

from eawf.daemon.client import DaemonClient, DaemonUnreachable
from eawf.state.models import State

logger = logging.getLogger(__name__)


class StateBinding:
    """Reactive bridge: daemon push primary + mtime poll fallback.

    Public API:
      - `state: reactive[State]` — watched by widgets.
      - `degraded: reactive[bool]` — True when mtime-poll fallback active.
      - `connect()` — opens subscription or starts polling.
      - `disconnect()` — graceful shutdown on app exit.
    """

    state: reactive[State] = reactive(default=State.empty(), init=False)
    degraded: reactive[bool] = reactive(False, init=False)

    def __init__(self, state_path: Path | None) -> None:
        self._state_path = state_path
        self._client: DaemonClient | None = None
        self._subscription_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._poll_interval = float(os.environ.get("EAWF_POLL_INTERVAL_S", "2.0"))

    async def connect(self) -> None:
        """Try daemon subscribe; fall back to mtime poll on failure."""
        try:
            self._client = DaemonClient.connect_or_spawn()  # C02 cold-spawn
            initial = await self._client.state_read(scope_id=None)
            self.state = initial.state
            self._subscription_task = asyncio.create_task(self._subscribe_loop())
            self.degraded = False
        except DaemonUnreachable as exc:
            logger.warning(f"state_binding daemon_unreachable falling_back_to_poll cause={exc!r}")
            self.degraded = True
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _subscribe_loop(self) -> None:
        """Stream `event.push` frames; update state on each."""
        async for envelope in self._client.event_subscribe(since_version=self.state.version):
            # Apply mutation envelope to reactive state
            try:
                self.state = self.state.apply_envelope(envelope)
            except Exception as exc:
                logger.error(f"state_binding apply_envelope_failed event_id={envelope.id} cause={exc!r}")
                # On apply failure, reload full state
                refreshed = await self._client.state_read(scope_id=None)
                self.state = refreshed.state

    async def _poll_loop(self) -> None:
        """Mtime-poll loop; reads state.json directly when daemon down."""
        last_mtime = 0.0
        while True:
            await asyncio.sleep(self._poll_interval)
            if self._state_path is None or not self._state_path.exists():
                continue
            mtime = self._state_path.stat().st_mtime
            if mtime <= last_mtime:
                continue
            try:
                self.state = State.parse_file(self._state_path)
                last_mtime = mtime
            except Exception as exc:
                logger.error(f"state_binding poll_parse_failed path={self._state_path!s} cause={exc!r}")

    async def disconnect(self) -> None:
        for task in (self._subscription_task, self._poll_task):
            if task is not None:
                task.cancel()
        if self._client is not None:
            await self._client.close()
```

Widgets `watch` the `state` reactive attribute via Textual's reactive system:

```python
class RoadmapTree(Tree):
    def on_mount(self) -> None:
        binding = self.app._binding  # type: ignore
        binding.watch_state(self._on_state_changed)

    def _on_state_changed(self, new_state: State) -> None:
        self._rebuild_tree(new_state)
```

On `degraded` flipping True, the `Header` widget surfaces the banner: `daemon offline · poll mode 2s`. On `degraded` flipping back to False (daemon recovered), the banner clears; a one-shot toast surfaces `daemon back online`.

### 5.5 Per-screen layout

**RepoScreen (2x2 quadrant).** Per `tui-layout.md` §"Chosen layout — Quadrant 2×2" [9:56-81]. Concrete CSS + composition:

```css
/* src/eawf/tui_v2/theme.css */
RepoScreen Vertical#body { height: 1fr; }
RepoScreen Horizontal.row { height: 1fr; }
RepoScreen Vertical.pane {
    width: 1fr;
    height: 1fr;
    border: solid $accent;
    padding: 0 1;
}
RepoScreen Vertical.pane.-focused { border: solid $primary; }
RepoScreen Vertical.pane > .pane-title {
    height: 1;
    color: $accent;
    text-style: bold;
}
```

```python
# src/eawf/tui_v2/scopes/repo.py

class RepoScreen(Screen[None]):
    BINDINGS = [
        Binding("ctrl+r", "switch_scope('repo')",      show=False),
        Binding("ctrl+w", "switch_scope('workspace')", show=False),
        Binding("ctrl+u", "switch_scope('user')",      show=False),
        Binding("w",      "open_wave_board",            "wave board"),
        Binding("c",      "open_config",                "config"),
        Binding("question_mark", "open_help",           "help"),
        Binding("slash",  "open_palette",               "palette"),
        Binding("r",      "force_refresh",              "refresh"),
        Binding("R",      "force_refresh_with_fetch",  show=False),
        Binding("q",      "quit",                       "quit"),
        Binding("escape", "quit",                       show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            with Horizontal(classes="row"):
                with Vertical(classes="pane", id="pane-roadmap"):
                    yield Static("ROADMAP", classes="pane-title")
                    yield RoadmapTree(id="roadmap-tree")
                with Vertical(classes="pane", id="pane-status"):
                    yield Static("STATUS", classes="pane-title")
                    yield StatusPane(id="status-pane")
            with Horizontal(classes="row"):
                with Vertical(classes="pane", id="pane-git"):
                    yield Static("GIT", classes="pane-title")
                    yield GitPane(id="git-pane")
                with Vertical(classes="pane", id="pane-backlog"):
                    yield Static("BACKLOG", classes="pane-title")
                    yield BacklogTable(id="backlog-table")
        yield Footer()
```

**WorkspaceScreen (strip + active-repo quadrant).** Per `workspace-and-user-tui.md` §2 [8:60-126]. The top strip is a `DataTable` with one row per linked repo; arrow keys move the cursor row; the bottom quadrant reloads against the focused row's state path.

```python
# src/eawf/tui_v2/scopes/workspace.py

class WorkspaceScreen(Screen[None]):
    BINDINGS = [
        Binding("up",   "cursor_up",   "row up"),
        Binding("down", "cursor_down", "row down"),
        Binding("z",    "zoom_focused", "zoom"),
        # ... (inherits global bindings)
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield WorkspaceTopStrip(id="strip")
            yield Static("", id="separator")
            with Vertical(id="zoom"):
                yield RepoQuadrant(id="zoom-quadrant")  # reusable sub-widget
        yield Footer()

    def on_workspace_top_strip_row_focused(self, row_id: str) -> None:
        self.query_one("#zoom-quadrant", RepoQuadrant).load_repo(row_id)
```

Expansion-on-focus (per V6 [6:312-336]): when an in-flight repo row is focused, the strip expands by `in_flight_wave_count` sub-rows (sort: `(status_priority, wave_id_asc)`); cap at 8 total strip rows; overflow surfaces `↑ N more` indicator.

**UserScreen (attention / effort / portfolio).** Per `workspace-and-user-tui.md` §3 [8:166-208]. Three vertical sections weighted `3:2:5`. Bottom sticky totals row per tui-ux-resolved §Cross-repo totals row [7:452-468].

```python
# src/eawf/tui_v2/scopes/user.py

class UserScreen(Screen[None]):
    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            with Vertical(id="attention"):
                yield Static("ATTENTION", classes="section-title")
                yield AttentionList(id="attention-list")
            with Vertical(id="effort"):
                yield Static("EFFORT 7d (EU)", classes="section-title")
                yield EffortBars(id="effort-bars")
            with Vertical(id="portfolio"):
                yield Static("PORTFOLIO", classes="section-title")
                yield PortfolioTable(id="portfolio-table")
        yield Footer()
```

**WaveBoardScreen (list + detail).** Per V01 + V11 [6:30-43, 553-582]. Two-level navigation: list view (sortable DataTable) → Enter → detail card (Static composite + scrollable content). `:wave <verb>` palette verbs (open / log / state / report / criteria / deps / events / dispatch) operate on the focused wave.

```python
# src/eawf/tui_v2/screens/wave_board.py

class WaveBoardScreen(Screen[None]):
    BINDINGS = [
        Binding("up",    "cursor_up",    "up"),
        Binding("down",  "cursor_down",  "down"),
        Binding("enter", "drill_into",   "drill"),
        Binding("f",     "cycle_filter", "filter"),
        Binding("escape","pop_screen",   "back"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield WaveListDataTable(id="wave-list")
        yield Footer()
```

### 5.6 `/` palette verb registry

Static registry at `src/eawf/tui_v2/palette/verbs.py` per D13. Each verb declares: `name`, `hint`, `handler` (callable taking `(app, ctx, args)`), `allowed_scopes` (set of `RepoScreen`, `WorkspaceScreen`, `UserScreen`, `WaveBoardScreen`), `requires_profile` (optional V3-gated profile list).

```python
# src/eawf/tui_v2/palette/verbs.py

from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


ScopeName = Literal["repo", "workspace", "user", "wave_board"]
SCOPES_ALL: tuple[ScopeName, ...] = ("repo", "workspace", "user", "wave_board")


@dataclass(frozen=True)
class PaletteVerb:
    name: str                          # "/wave open" / "/find" / "/metrics" / ...
    hint: str                          # one-line description
    handler: Callable[..., None]       # async; takes (app, args)
    allowed_scopes: tuple[ScopeName, ...]
    requires_profile: tuple[str, ...] = ()  # empty = all profiles
    requires_runtime: tuple[str, ...] = ()  # empty = all runtimes
    args_grammar: str = ""              # display hint; e.g. "<id>"


VERBS: tuple[PaletteVerb, ...] = (
    # Cross-screen navigation + filter
    PaletteVerb("/find",     "fuzzy ID + title search",        _handle_find,      SCOPES_ALL,                 args_grammar="<query>"),
    PaletteVerb("/filter",   "filter pane contents",            _handle_filter,    SCOPES_ALL,                 args_grammar="<pane> <key>"),
    PaletteVerb("/sort",     "cycle sort key",                  _handle_sort,      SCOPES_ALL,                 args_grammar="<pane> <col>"),
    PaletteVerb("/switch",   "switch scope",                    _handle_switch,    ("workspace", "user"),      args_grammar="<scope> <id>"),
    PaletteVerb("/theme",    "theme dark/light/cb/auto",        _handle_theme,     SCOPES_ALL,                 args_grammar="<name>"),
    PaletteVerb("/events",   "last 50 events overlay",          _handle_events,    SCOPES_ALL),
    PaletteVerb("/metrics",  "metrics dashboard (3x2 tiles)",   _handle_metrics,   SCOPES_ALL,                 args_grammar="[--window 7d|30d|90d] [--scope <urn>]"),
    PaletteVerb("/pr",       "open PRs (gh shell-out, cached)",  _handle_pr,        SCOPES_ALL),
    PaletteVerb("/help",     "verb help",                       _handle_help,      SCOPES_ALL,                 args_grammar="[verb]"),
    PaletteVerb("/quit",     "quit",                            _handle_quit,      SCOPES_ALL),

    # Wave-board read-only verbs (V11 [6:553-582])
    PaletteVerb("/wave",            "wave-scoped action",         _handle_wave,         ("repo", "wave_board"),  args_grammar="<verb> [<id>]"),
    PaletteVerb("/wave open",       "open worktree in $EDITOR",   _handle_wave_open,    ("repo", "wave_board"),  args_grammar="[<id>]"),
    PaletteVerb("/wave log",        "tail session log",           _handle_wave_log,     ("repo", "wave_board"),  args_grammar="[<id>]"),
    PaletteVerb("/wave state",      "show wave state JSON",       _handle_wave_state,   ("repo", "wave_board"),  args_grammar="[<id>]"),
    PaletteVerb("/wave report",     "last agent report",          _handle_wave_report,  ("repo", "wave_board"),  args_grammar="[<id>]"),
    PaletteVerb("/wave criteria",   "WaveSpec body",              _handle_wave_criteria,("repo", "wave_board"),  args_grammar="[<id>]"),
    PaletteVerb("/wave deps",       "wave DAG",                   _handle_wave_deps,    ("repo", "wave_board"),  args_grammar="[<id>]"),
    PaletteVerb("/wave events",     "events scoped to wave",      _handle_wave_events,  ("repo", "wave_board"),  args_grammar="[<id>]"),
    PaletteVerb("/wave dispatch",   "session-handle history",     _handle_wave_dispatch,("repo", "wave_board"),  args_grammar="[<id>]"),

    # Worktree
    PaletteVerb("/wt",       "worktrees overlay",                _handle_wt,        SCOPES_ALL),

    # Skill dispatch passthrough (CLI verb wrappers)
    PaletteVerb("/roadmap",  "roadmap action (sub-verb)",        _handle_roadmap,   SCOPES_ALL,                 args_grammar="<sub-verb> ...", requires_profile=()),
    PaletteVerb("/prep",     "prep phase (CLI dispatch)",        _handle_prep,      SCOPES_ALL,                 args_grammar="<P##>"),
    PaletteVerb("/flow",     "flow pipeline",                    _handle_flow,      SCOPES_ALL,                 args_grammar="<topic>"),
    PaletteVerb("/research", "research brief",                   _handle_research,  SCOPES_ALL,                 args_grammar="<topic>"),
    PaletteVerb("/spike",    "spike brief (research profile)",    _handle_spike,     SCOPES_ALL,                 requires_profile=("research",), args_grammar="<slug>"),
    PaletteVerb("/design",   "design pass (research+UI scope)",  _handle_design,    SCOPES_ALL,                 requires_profile=("research",), args_grammar="<surface>"),
    PaletteVerb("/audit",    "audit a scope",                    _handle_audit,     SCOPES_ALL,                 args_grammar="<scope-urn> [--kind ...]"),
    PaletteVerb("/ship",     "ship phase",                       _handle_ship,      SCOPES_ALL,                 args_grammar="<P##>"),
    PaletteVerb("/review",   "review PR",                        _handle_review,    SCOPES_ALL,                 args_grammar="[--pr <url>]"),
    PaletteVerb("/polish",   "polish sweep",                     _handle_polish,    SCOPES_ALL,                 args_grammar="[<scope>]"),
)


def visible_verbs(scope: ScopeName, profiles: set[str], runtime: str) -> list[PaletteVerb]:
    """Filter VERBS by current screen, profile bundle, and runtime."""
    out: list[PaletteVerb] = []
    for v in VERBS:
        if scope not in v.allowed_scopes:
            continue
        if v.requires_profile and not set(v.requires_profile).intersection(profiles):
            continue
        if v.requires_runtime and runtime not in v.requires_runtime:
            continue
        out.append(v)
    return out
```

Palette UX (`CommandPalette(ModalScreen)`):

- Operator presses `/`. Per **Q12 OVERRIDE**, the v0.2.x `:` alias is REMOVED in v0.3 — `:` keypress falls through to the default Textual binding (no-op or input-widget literal).
- ModalScreen opens with an `Input` widget pre-filled with `/`.
- An `OptionList` below shows `visible_verbs(scope, profiles, runtime)` filtered by fuzzy match per `fuzzy_match()` in the smoke demo [`cc_palette_demo.py:65-78`].
- Bold-accent highlights the matched chars in each verb name.
- `Tab` autocompletes to the highlighted option.
- `Enter` runs the handler with parsed args.
- `Esc` closes without execution.
- Mutating verbs (`/wave retry`, `/wave abandon`, etc.) NEVER appear in the palette — all wave-board palette verbs are read-only per V11 [6:582-583]. Mutating actions only reach the operator through the audit-failed overlay's structured menu (D17).

`@`-mention completion (per the smoke demo and P20 direction round 8 [2:476-481]) activates when operator types `@` inside a palette arg or a text input. The completion popover lists paths from a fixed PATHS list per scope (or from `git ls-files` when in a repo scope).

### 5.7 Modal stack inventory

Modal stack cap = 3 per D14. Each modal is a Textual `ModalScreen` subclass.

| Modal | File | Trigger | Pop on | Action surface |
|---|---|---|---|---|
| `AuditRunningModal` | `screens/overlays/audit_running.py` | auto-open on `audit_started` event for current scope | `audit_completed` event OR Esc (minimise to footer chip `A19 4/7`) | read-only progress; per-check spinner; minimise / reopen via `/audit show` |
| `AuditFailedModal` | `screens/overlays/audit_failed.py` | auto-open on `audit_completed` event w/ `verdict=fail` for current scope | Esc | mutating menu (D17): retry / split / land-partial / abandon / scope-change. Each option dispatches subagent via `eawf agent dispatch <wave-id> --action <action>`. |
| `DetailModal` | `screens/overlays/detail.py` | Enter on a row in any pane / palette `/h` `/d` `/m` `/e` `/dp` | Esc | scrollable detail card; `g <id>` jumps to referenced ID (stack depth ≤ 3 enforced); h/d/m/e/dp tabs per overlay kind |
| `PrListModal` | `screens/overlays/pr_list.py` | palette `/pr` | Esc | per-repo PR rows; Enter → `gh pr view --web`; cache 60 s |
| `EditFieldModal` | `screens/overlays/edit_field.py` | Enter on a ConfigRow in ConfigModal (str/int/float/path types only) | Esc (cancel) / Enter (accept) | per-type input widget; validators report inline below |
| `ConfigModal` | `screens/overlays/config_modal.py` | global `c` keypress / palette `/config` | Esc (prompts on dirty per V15 [6:660-666]) | tabbed (alphabetical tabs, alphabetical fields); Space toggles bool; ←/→ cycles enum; Enter opens EditFieldModal for other types; `s` save; `r` reset; `L` cycle writable layer |
| `PlanPreviewModal` | `screens/overlays/plan_preview.py` | `/roadmap propose` returns `status=needs_user` | Esc (= reject) / approve/edit pick | hierarchical Tree rendered from PhaseSpec + IterSpec + WaveSpec aggregate; 3-option AUQ (approve / edit / reject); Approve runs `eawf roadmap apply <P##>` only per D4 |
| `NeedsUserModal` | `screens/overlays/needs_user.py` | `needs_user_pause` envelope received for current scope/session | operator pick / Esc (defer) | UserQuestion AUQ; ↑↓ + Enter to pick; `eawf skill resume <urn> --choice <label>` on confirm |
| `MetricsModal` | `screens/overlays/metrics.py` | palette `/metrics` | Esc | 3×2 tile grid; 5 s refresh per D9; tile click drills to filtered view (per-tile dedicated sub-overlay) |
| `EventsModal` | `screens/overlays/events.py` | palette `/events` | Esc | ring buffer 50 events session-only per `tui-ux-resolved` §`:events` [7:304-326]; filter cycle `f` (all / errors-only / reports-only / all) |
| `ConfirmModal` | `screens/overlays/confirm.py` | destructive-op approval (cherry-pick, roadmap drop, etc.) | operator pick (Yes/No via ←/→) / Esc | arrow-toggle Yes/No per `cc_palette_demo.py:114-176`; backdrop dim |
| `HelpScreen` | `screens/help.py` | `?` keypress / palette `/help` | Esc | full keymap overlay; per-screen + global + palette verbs |

Stack-depth enforcement at `EaApp.push_screen`:

```python
class EaApp(App[None]):
    MAX_MODAL_DEPTH = 3

    async def action_open_modal(self, modal: ModalScreen) -> None:
        depth = sum(1 for s in self.screen_stack if isinstance(s, ModalScreen))
        if depth >= self.MAX_MODAL_DEPTH:
            self.notify("modal stack depth limit (3) reached — close one first", severity="warning")
            return
        await self.push_screen(modal)
```

Backdrop dim per `tui-ux-resolved.md` §Detail overlay [7:586-606]: every ModalScreen sets `Style(dim=True)` on the underlying screen via Textual's `app.screen_stack[-2]` ref. The dim style applies a `rgba(0,0,0,0.55)` overlay per P20 direction round 1 [2:418-425].

### 5.8 Daemon-push protocol binding

Per D1 + C02 §5.3.2. The TUI's `StateBinding.connect` issues a JSON-RPC `event.subscribe` request:

```json
{
  "jsonrpc": "2.0",
  "id": "<uuid>",
  "method": "event.subscribe",
  "params": {
    "scope_id": "urn:eawf:v1:repo:eawf",
    "since_version": "<digest-of-loaded-state>",
    "kinds": null
  },
  "protocol_version": "1"
}
```

Response: streaming `event.push` notifications carrying `Envelope` payloads. The TUI applies each envelope to its reactive `state`:

```python
async def _subscribe_loop(self) -> None:
    async for notification in self._client.event_subscribe_stream(
        scope_id=self._scope_id,
        since_version=self.state.version,
    ):
        envelope: Envelope = notification.event
        # Per-kind dispatch
        if envelope.kind == "wave_open":
            self.state = self.state.with_wave_open(envelope.payload)
        elif envelope.kind == "wave_close":
            self.state = self.state.with_wave_close(envelope.payload)
        elif envelope.kind == "runtime_switched":
            # V5 banner update
            self._on_runtime_switched(envelope.payload)
        elif envelope.kind == "audit_started":
            self.app.push_screen(AuditRunningModal(audit_id=envelope.payload["audit_id"]))
        elif envelope.kind == "audit_completed":
            self._on_audit_completed(envelope)
        elif envelope.kind == "needs_user_pause":
            self.app.push_screen(NeedsUserModal(envelope=envelope))
        elif envelope.kind == "dispatch_started":
            self._on_dispatch_started(envelope.payload)
        elif envelope.kind == "subscription_dropped":
            # C02 §5.7 backpressure overflow
            self.app.notify("event stream lost · press r to resync", severity="error")
            await self._resync()
        # ... (full envelope-kind dispatch table)
```

Reconnect logic: on `subscription_dropped` (per C02 D7 [3:140]), the TUI immediately re-subscribes with `since_version=<last-applied-event-id>`. If catch-up exceeds C02's 10000-event cap (`-32008 catch_up_too_large`), the TUI prompts a state-refresh modal: `event stream too far behind · press r to reload state from scratch`.

**Protocol-version skew handling.** Per C02 D9 [3:142], a `-32004 protocol_version_mismatch` response surfaces a blocking modal: `daemon protocol mismatch (daemon=v1, cli=v2) · run \`uv tool upgrade eawf\``. The TUI cannot operate while skewed; it exits 4 on Esc.

### 5.9 V5 runtime-switched banner

Per D8. The `Header` widget composes a `runtime` cell that the `StateBinding` updates on every `runtime_switched` event:

```python
# src/eawf/tui_v2/widgets/header.py

class Header(Static):
    """Composite: brand + breadcrumb + runtime cell + heartbeat + clock."""

    runtime_state: reactive[str] = reactive("idle", init=False)
    runtime_switched_at: reactive[float | None] = reactive(None, init=False)

    def render(self) -> RenderableType:
        # ...
        runtime_color = self._runtime_color()
        runtime_text = f"runtime: {self.runtime_state}"
        # 60-second persistence window after a switch:
        if self.runtime_switched_at is not None and (perf_counter() - self.runtime_switched_at) < 60:
            runtime_color = self.app.theme.warn  # color sticks
        # ... compose final renderable

    def _runtime_color(self) -> str:
        # Per D29 (Q-new3): muted when idle, accent when active, warn after switch, err when unavailable
        if self.runtime_state == "idle":
            return self.app.theme.muted
        if self.runtime_state == "unavailable":
            return self.app.theme.err
        if self.runtime_switched_at is not None:
            return self.app.theme.warn
        return self.app.theme.accent
```

Transition toast (3 seconds, per D8):

```python
def _on_runtime_switched(self, payload: dict) -> None:
    self.header.runtime_state = payload["runtime_to"]
    self.header.runtime_switched_at = perf_counter()
    self.app.notify(
        f"runtime: {payload['runtime_from']} → {payload['runtime_to']} (cause: {payload['cause']})",
        severity="warning",
        timeout=3.0,
    )
```

Manual acknowledge (`r` keypress) clears the warn color earlier than the 60-second window:

```python
async def action_force_refresh(self) -> None:
    self.header.runtime_switched_at = None  # clear warn
    self.heartbeat.double_pulse(0.5)
    # ... rest of refresh
```

### 5.10 V7 `/metrics` overlay

Per D9. The `/metrics` palette verb opens `MetricsModal`. Tile inventory + refresh:

```python
# src/eawf/tui_v2/screens/overlays/metrics.py

class MetricsModal(ModalScreen[None]):
    """3x2 grid of metrics tiles. 5 s refresh against daemon telemetry projection."""

    DEFAULT_CSS = """
    MetricsModal #grid { layout: grid; grid-size: 3 2; grid-gutter: 1; }
    MetricsModal Tile { border: solid $accent; padding: 0 1; }
    MetricsModal Tile.-focused { border: solid $primary; }
    """

    BINDINGS = [
        Binding("up",    "focus_up",    show=False),
        Binding("down",  "focus_down",  show=False),
        Binding("left",  "focus_left",  show=False),
        Binding("right", "focus_right", show=False),
        Binding("enter", "drill_into",  "drill"),
        Binding("escape", "dismiss",     "close"),
    ]

    def compose(self) -> ComposeResult:
        with Grid(id="grid"):
            yield VarianceTile(id="tile-variance")
            yield WeeklyBurnTile(id="tile-burn")
            yield WaveElapsedTile(id="tile-elapsed")
            yield CacheHealthTile(id="tile-cache")
            yield SwitchoverFreqTile(id="tile-switchover")
            yield PerRuntimeTokensTile(id="tile-tokens")

    def on_mount(self) -> None:
        self.set_interval(5.0, self._refresh_all)

    async def _refresh_all(self) -> None:
        client = self.app._binding._client
        if client is None:
            return  # degraded mode — tiles render last-known value
        metrics = await client.telemetry_metrics(scope=self._scope_filter, window=self._window)
        for tile in self.query(Tile):
            tile.update_from(metrics)
```

Each Tile is a `Static`-composite subclass that renders an ASCII chart or a simple bar:

```python
class VarianceTile(Static):
    """Estimate-vs-actual EU variance bar per bucket."""

    def update_from(self, metrics: MetricsReport) -> None:
        rows: list[Text] = []
        for row in metrics.variance:
            color = "ok" if row.variance_pct <= 10 else "warn" if row.variance_pct <= 30 else "err"
            bar = "█" * min(int(abs(row.variance_pct) / 10), 10)
            rows.append(Text.assemble(
                (f"{row.bucket:<3}", "dim"),
                f" {bar:<10} {row.variance_pct:+3d}%",
                style=KIND_COLOR[color],
            ))
        self.update(Group(*rows))
```

Tile drill (Enter on focused tile) → opens a per-tile sub-overlay scoped to the drilled metric (per-wave variance table, per-cause switchover history, etc.).

`/metrics` arg parsing:
- `/metrics` → opens with current scope + default 7d window
- `/metrics --scope <urn>` → filters tiles to a specific URN
- `/metrics --window 7d|30d|90d` → switches rolling window

### 5.11 Web stub: WebSocket bridge contract

Per D6 — defer concrete SPA pick; specify daemon-side WebSocket bridge protocol + auth model + deployment shape.

**Protocol.** The web stub connects to a daemon-spawned `eawfd-web` companion process (not the eawfd daemon itself; runs as a co-process under the daemon supervisor per C02). The web bridge listens on `<host>:<port>` (port discovered via `eawf daemon status --web`); accepts WebSocket upgrade requests at `/ws`.

WebSocket protocol mirrors the JSON-RPC `event.subscribe` contract:

```
client → server:  { "method": "subscribe", "params": { "scope_id": "...", "since_version": "..." } }
server → client:  { "kind": "event.push", "event": <Envelope> }       (streaming)
server → client:  { "kind": "subscription_dropped", "reason": "...", "code": -32008 }
client → server:  { "method": "ping" }
server → client:  { "kind": "pong" }
```

The web bridge proxies subscribe / ping; for state mutations the SPA dispatches `POST /rpc` with a JSON-RPC body, which the bridge forwards to eawfd over its UDS. The bridge never bypasses eawfd; SPA → bridge → eawfd is the canonical mutation path.

**Auth model.**

- The web bridge binds to `<host>` only (no public listener).
- On launch, the bridge generates a per-session random `token` and writes it to `<local-path>` (permission `0600`).
- SPA clients fetch the token via a local file-read at startup (when the SPA runs as a local-only static page served by the bridge itself); for browser-served SPA, operator copies the token from the daemon status output.
- Token is sent in the `Sec-WebSocket-Protocol` header on connect; the bridge validates before accepting frames.
- Token rotates on `eawfd-web` restart.

**Deployment shape.**

- **Local-only first.** v0.3-v0.5 supports localhost serving only. SPA is a static bundle served from `<local-path>` (gitignored); `eawfd-web` serves both the static bundle and the `/ws` upgrade.
- **No public listener.** No `0.0.0.0` binding; no TLS in the bridge (TLS would require operator cert management — deferred).
- **Auth via local-only token + file permission.** No OAuth, no JWT in v0.3-v0.5.
- **Future remote access.** v0.6+ MAY add an SSH-tunnel pattern (operator runs `ssh -L 8080:localhost:<port>`) or a tailscale-style overlay; explicit design deferred.

**SPA contract (deferred to web-cluster brief).** D6 picks (a) defer. The web-cluster brief will:

- Pick the SPA stack (Preact / Lit / Svelte / Tauri wrapper).
- Specify the reactive store shape (mirrors `StateBinding` reactive pattern).
- Specify the per-screen route + bundle split.
- Specify the SSR / static-site vs Tauri-desktop deployment pick.
- Specify the auth-flow detail (token paste / file-read / file:// scheme constraints).
- Specify the bundle size + first-paint budget.

C06 just freezes the bridge contract so the SPA-cluster work can land independently of TUI implementation phase.

### 5.12 Theming

Per D12. Wong 2011 deuteranopia-safe palette (from tui-ux-resolved §Theme palette [7:489-508]):

| Role | Hex | Use |
|---|---|---|
| `ok` | `#0072B2` | passing audit, healthy repo, complete bar |
| `warn` | `#E69F00` | stale wave, dirty git, >80% EU burn |
| `err` | `#D55E00` | failing audit, blocker open, >100% EU burn, runtime auth-error |
| `muted` | `#999999` | borders, dim text, separators |
| `accent` | `#009E73` | `Eä` brand, focused-pane border, heartbeat, runtime cell normal |
| `bg` | follow terminal | — |
| `fg` | follow terminal | — |

Textual CSS at `src/eawf/tui_v2/theme.css` declares `$ok`, `$warn`, `$err`, `$muted`, `$accent`, `$primary` (= accent), and per-screen styles. Runtime swap via `/theme <name>`:

```python
async def _handle_theme(app: EaApp, name: Literal["dark", "light", "auto"]) -> None:
    """`cb` value REJECTED in v0.3 per Q4 OVERRIDE — defer tighter CB palette to v0.4.

    Wong 2011 default is already deuteranopia-safe; explicit `cb` verb gets
    operator a toast `cb palette deferred to v0.4 — Wong 2011 (default) is
    already deuteranopia-safe`.
    """
    if name == "cb":
        app.notify("cb palette deferred to v0.4 — Wong 2011 (default) is already deuteranopia-safe", timeout=3.0)
        return
    theme_dict = _resolve_theme(name)
    app.theme = Theme.from_dict(theme_dict)
    # Persist
    session_path = Path.home() / ".eawf" / "tui-session.json"
    session = json.loads(session_path.read_text() if session_path.exists() else "{}")
    session.setdefault("theme", {})[app._scope] = name
    session_path.write_text(json.dumps(session, indent=2))
    app.notify(f"theme: {name}", timeout=2.0)
```

`auto` mode (default): detect via `COLORFGBG` env var; fall back to dark-assumption when unset (Q9 ratified).

Glyph set per tui-ux-resolved §Glyph set [7:510-571]: Nerd Font glyphs always per P20 direction round 3 [2:434-441] (`Nerd-font detection: Always render Nerd Font glyphs (no fallback)`). `--plain` flag in CLI forces ASCII fallback set:

```python
GLYPHS_PLAIN: dict[str, str] = {
    "pass": "[x]", "fail": "[!]", "warn": "(!)",
    "collapsed": ">", "expanded": "v",
    "bar_full": "#", "bar_empty": "-",
    "sep": ">", "heartbeat": "*",
    "vert": "|", "corner_tl": "+",
}
```

### 5.13 Keybindings

Per D11. Master keybinding catalog (global + per-screen):

**Global (every screen):**

| Key | Action |
|---|---|
| `q` | quit |
| `Esc` | close overlay / clear filter / drop palette |
| `r` | force-tick + invalidate caches (offline) |
| `R` | force-tick + `git fetch --all --quiet` (online) |
| `?` | open help modal |
| `/` | open palette |
| `Ctrl-R` | switch to repo scope |
| `Ctrl-W` | switch to workspace scope |
| `Ctrl-U` | switch to user scope |
| `Tab` | next pane (clockwise) |
| `Shift-Tab` | previous pane |

**Pane navigation (focused pane):**

| Key | Action | Vim alias |
|---|---|---|
| `↑` | line up | `k` |
| `↓` | line down | `j` |
| `←` | collapse row / scroll left | `h` |
| `→` | expand row / scroll right | `l` |
| `PageUp` | half-page up | `Ctrl-u` |
| `PageDown` | half-page down | `Ctrl-d` |
| `Home` | top of pane | `gg` |
| `End` | bottom of pane | `G` |
| `Enter` | drill into row (modal) | — |
| `Space` | toggle (bool ConfigRow only) | — |

**Per-screen extras:**

- **RepoScreen:** `w` opens wave board.
- **WorkspaceScreen:** `z` zooms focused repo to RepoScreen (Esc returns).
- **WaveBoardScreen:** `f` cycles filter (all ↔ active-only per V14 [6:651-657]); Enter drills to detail.
- **ConfigModal:** `s` save; `r` reset focused field; `L` cycle writable layer.

Full key names always — `PageUp` not `PgUp`, `PageDown` not `PgDn`. Per tui-ux-resolved §Keymap [7:75-108] + `feedback_tui_keymap_conventions.md`.

Textual `Binding(priority=True)` flags ensure global keys (`q`, `Esc`, `/`, `?`) route over Input/TextArea focus per the smoke demo finding [2:319-334].

### 5.14 Snapshot test harness

Per D20. Twelve per-screen + per-overlay SVG snapshots covering the full surface.

**Fixture state.json builder** at `src/eawf/tui_v2/snapshot/fixtures.py`:

```python
def fixture_50_phase() -> State:
    """50-phase project; current iter = P50-I03; 5 in-flight waves."""
    state = State.empty()
    for i in range(1, 51):
        phase = Phase(id=f"P{i:02d}", title=f"phase {i}", status="closed" if i < 50 else "active")
        state.phases[phase.id] = phase
    # ... full builder
    return state


def fixture_audit_failed() -> State:
    """Current scope w/ a failed audit on P20-I03-W01."""
    state = fixture_50_phase()
    audit = Audit(
        id="A-2026-05-17-001",
        scope_urn="urn:eawf:v1:wave:eawf/P20-I03-W01",
        kind="ship-gate",
        status="closed",
        verdict="fail",
        check_results=[
            CheckResult(name="verify_implements", kind="verify_implements", passed=False,
                        details="P20-I03-W01: missing markers for ['V12']"),
        ],
    )
    state.audits[audit.id] = audit
    return state
```

**Pilot harness** at `src/eawf/tui_v2/snapshot/pilot_harness.py`. Per **Q-new1 OVERRIDE**, fixtures are ASCII text (not SVG) — `app.export_screen_text()` returning ANSI-coded text — to avoid Python-version + Textual-version byte-drift.

```python
async def take_repo_screen_snapshot(state: State, out_path: Path) -> None:
    """ASCII text snapshot via `export_screen_text` (Q-new1 OVERRIDE: was SVG)."""
    app = EaApp(scope="repo", state_path=None)
    app._test_state = state  # bypass daemon
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        # Drive to a known position
        await pilot.press("tab")           # focus next pane
        await pilot.press("down", "down")  # cursor into pane content
        await pilot.pause(0.05)
        # Capture as ANSI-coded text (byte-stable across Python versions + Textual versions)
        screen_text = app.export_screen_text()
        out_path.write_text(screen_text, encoding="utf-8")
```

**Snapshot fixture set** (all `.txt` per Q-new1 OVERRIDE):

1. `tests/snapshots/tui/repo_screen.txt` — RepoScreen default, 50-phase fixture, current iter expanded.
2. `tests/snapshots/tui/workspace_screen.txt` — WorkspaceScreen, 3 linked repos, top row in_flight.
3. `tests/snapshots/tui/user_screen.txt` — UserScreen, 8 registry entries, attention list populated.
4. `tests/snapshots/tui/wave_board_list.txt` — WaveBoardScreen list view.
5. `tests/snapshots/tui/wave_board_detail.txt` — WaveBoardScreen detail card.
6. `tests/snapshots/tui/detail_modal_hypothesis.txt` — `/h` overlay.
7. `tests/snapshots/tui/detail_modal_decision.txt` — `/d` overlay.
8. `tests/snapshots/tui/detail_modal_memory.txt` — `/m` overlay.
9. `tests/snapshots/tui/detail_modal_events.txt` — `/e` overlay.
10. `tests/snapshots/tui/detail_modal_dispatch.txt` — `/dp` overlay.
11. `tests/snapshots/tui/audit_running.txt` — AuditRunningModal w/ 4/7 checks.
12. `tests/snapshots/tui/audit_failed.txt` — AuditFailedModal w/ mutating menu.
13. `tests/snapshots/tui/plan_preview.txt` — PlanPreviewModal w/ PhaseSpec aggregate.
14. `tests/snapshots/tui/metrics_modal.txt` — MetricsModal 3×2 grid.
15. `tests/snapshots/tui/config_modal.txt` — ConfigModal Flow tab.
16. `tests/snapshots/tui/pr_list.txt` — PrListModal w/ 3 PRs.

(D20's "~12 snapshots" is the floor; the full inventory above ratifies 16 across screens + overlays.)

**Regen flow:** `EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/` writes new ASCII text fixtures over the existing ones. CI runs without the env var; diff fails the build. Diff comparison is plain string equality on the loaded text — ANSI escape codes preserved verbatim so color regressions surface.

### 5.15 Asciinema cast determinism

Per D7. The cast composer at `src/eawf/tui_v2/snapshot/asciinema.py`:

```python
# src/eawf/tui_v2/snapshot/asciinema.py
"""Deterministic asciinema cast generator.

Drives a Textual app via Pilot; captures SVG snapshots at fixed cadence;
composes the cast offline. No real terminal recording; monotonic-clock
timing produces byte-stable casts.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

from textual.app import App
from textual.pilot import Pilot


async def record_cast(
    app_factory: Callable[[], App],
    script: list[tuple[str, str]],  # [(action, args), ...]
    out_path: Path,
    *,
    frame_ms: int = 50,
) -> None:
    """Generate an asciinema cast by driving the app via Pilot.

    Args:
        app_factory: Builds a fresh App instance per recording.
        script: List of (action, arg) tuples — e.g. [("press", "down"), ("pause", "0.5")].
        out_path: Destination .cast file.
        frame_ms: Fixed inter-frame interval (default 50ms per Q6 OVERRIDE; was 100ms).
    """
    frames: list[tuple[float, str]] = []
    app = app_factory()
    elapsed_s = 0.0

    async with app.run_test() as pilot:
        await pilot.pause(0.0)
        # Initial frame
        screenshot = app.export_screen_text()
        frames.append((elapsed_s, screenshot))

        for action, arg in script:
            elapsed_s += frame_ms / 1000.0
            if action == "press":
                await pilot.press(arg)
            elif action == "pause":
                await pilot.pause(float(arg))
            elif action == "text":
                await pilot.type(arg)
            screenshot = app.export_screen_text()
            frames.append((elapsed_s, screenshot))

    # Write asciinema v2 cast
    header = {
        "version": 2,
        "width": 120,
        "height": 40,
        "timestamp": 0,  # deterministic
        "title": "eawf TUI",
    }
    lines = [json.dumps(header)]
    for ts, screen in frames:
        lines.append(json.dumps([ts, "o", screen]))
    out_path.write_text("\n".join(lines) + "\n")
```

Cast generation is invoked from `eawf tui asciinema <script> --out <path>` (a new CLI verb in C05's surface). The `<script>` argument names a Python file declaring a `SCRIPT: list[tuple[str, str]]` list. Casts are deterministic across machines because:

- No real-time delays — `frame_ms` is the only timing source.
- `export_screen_text` returns a deterministic frame (no animations between Pilot.press / Pilot.pause invocations).
- `timestamp: 0` in the header makes the file byte-stable.

CI runs `eawf tui asciinema` for each script in `docs/architecture/tui/casts/`; goldens compared byte-for-byte.

### 5.16 Performance budget enforcement

Per D16. Two budgets:

1. **First paint < 150 ms p99** — measured from `App.run_test()` `__aenter__` to first non-empty `app.export_screen_text()`.
2. **Keypress → render < 50 ms p99** — measured per `pilot.press()` invocation; recorded into a `deque[float]`.

```python
# tests/perf/tui/test_first_paint.py
import asyncio
from time import perf_counter

import pytest


@pytest.mark.perf
@pytest.mark.parametrize("scope", ["repo", "workspace", "user"])
async def test_first_paint_under_150ms_p99(scope):
    fixture = build_fixture_50_phase()
    latencies: list[float] = []
    for _ in range(100):
        start = perf_counter()
        app = EaApp(scope=scope, state_path=None)
        app._test_state = fixture
        async with app.run_test() as pilot:
            await pilot.pause(0.0)
            screen = app.export_screen_text()
            if screen.strip():
                latencies.append((perf_counter() - start) * 1000.0)
    latencies.sort()
    p99 = latencies[int(len(latencies) * 0.99)]
    assert p99 < 150.0, f"first_paint_p99={p99:.1f}ms exceeds 150ms"


@pytest.mark.perf
async def test_keypress_render_under_50ms_p99():
    app = EaApp(scope="repo", state_path=None)
    app._test_state = build_fixture_50_phase()
    latencies: list[float] = []
    async with app.run_test() as pilot:
        await pilot.pause(0.0)
        for _ in range(1000):
            start = perf_counter()
            await pilot.press("down")
            await pilot.pause(0.0)
            latencies.append((perf_counter() - start) * 1000.0)
    latencies.sort()
    p99 = latencies[int(len(latencies) * 0.99)]
    assert p99 < 50.0, f"keypress_render_p99={p99:.1f}ms exceeds 50ms"
```

CI gate: `pytest -m perf` runs the perf suite; build fails on p99 exceedence. Local dev escape hatch: `EAWF_SKIP_PERF=1 pytest` skips the suite.

### 5.17 Onboarding splash

Per D15 + tui-ux-resolved §Empty-state onboarding splash [7:165-227]. Per-scope variants on first launch; flag in `<local-path>`:

```python
# src/eawf/tui_v2/screens/onboarding.py

class OnboardingSplash(Screen[None]):
    """First-launch splash; per-scope copy; dismissed flag persisted."""

    BINDINGS = [
        Binding("enter", "dismiss", "dismiss"),
        Binding("escape", "dismiss", show=False),
    ]

    SCOPE_COPY = {
        "repo": """
No state found at this scope. Get started:

   1.  eawf init
   2.  eawf phase open --code P00 --title "bootstrap"
   3.  eawf iter open
   4.  eawf wave plan --code W01 --title "scaffold"

Docs:  docs/architecture/workflow.md
""",
        "workspace": """
Empty workspace. Get started:

   1.  eawf workspace init <code> --title "..."
   2.  cd <repo>; eawf repo init
   3.  eawf workspace add-repo <code> --path .

Docs:  docs/architecture/workflow.md
""",
        "user": """
No registry entries. Get started:

   1.  cd <repo>; eawf init               # auto-populates
   2.  eawf user add-repo <path>          # manual backfill

Docs:  docs/architecture/workflow.md
""",
    }

    def compose(self) -> ComposeResult:
        scope = self.app._scope
        yield Static(f"WELCOME TO Eä", id="title")
        yield Static(self.SCOPE_COPY[scope], id="copy")
        yield Static("[ Enter to dismiss ]", id="hint")

    def action_dismiss(self) -> None:
        # Persist dismissed flag
        session_path = Path.home() / ".eawf" / "tui-session.json"
        session = json.loads(session_path.read_text() if session_path.exists() else "{}")
        session.setdefault("onboarding_dismissed", {})[self.app._scope] = True
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(json.dumps(session, indent=2))
        self.app.pop_screen()


def maybe_show_onboarding(app: EaApp) -> None:
    session_path = Path.home() / ".eawf" / "tui-session.json"
    if not session_path.exists():
        app.push_screen(OnboardingSplash())
        return
    session = json.loads(session_path.read_text())
    dismissed = session.get("onboarding_dismissed", {}).get(app._scope, False)
    if not dismissed:
        app.push_screen(OnboardingSplash())
```

`eawf tui --onboarding` flag forces a one-shot re-show (Q-new4: re-show WITHOUT clearing the persisted dismissed flag — next normal launch stays dismissed):

```python
def maybe_show_onboarding(app: EaApp, force: bool = False) -> None:
    """`force=True` re-shows splash without mutating the dismissed flag."""
    if force:
        app.push_screen(OnboardingSplash())
        return
    # ... normal flag-checking path
```

### 5.18 Help modal

Per D23. Reachable from `?` keypress or palette `/help`. Renders the full keymap + palette verb table:

```python
# src/eawf/tui_v2/screens/help.py

class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > #help-container {
        width: 90%; height: 90%;
        border: solid $accent; padding: 1 2;
    }
    HelpScreen Section { margin: 1 0; }
    HelpScreen .key { color: $accent; text-style: bold; }
    HelpScreen .hint { color: $muted; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-container"):
            yield Static("Eä TUI — Help", classes="title")
            yield Static("\nGlobal keys:", classes="section-title")
            yield self._render_global_keys()
            yield Static("\nPane navigation:", classes="section-title")
            yield self._render_pane_nav()
            yield Static(f"\n{self.app._scope.capitalize()} screen keys:", classes="section-title")
            yield self._render_scope_keys()
            yield Static("\nPalette verbs:", classes="section-title")
            yield self._render_palette_verbs()
            yield Static("\n[ Esc to close ]", classes="hint")
```

Help modal opens on `?` keypress or palette `/help`. Rapid `?` while help open is no-op per **D31**: the help-modal-open state suppresses subsequent `?` keypresses until the modal closes (prevents accidental double-dismiss + stack-cap exhaustion).

Footer hints (context-aware per screen) live in the `Footer` widget and update on `on_screen_resume`. Example footer per screen:

- RepoScreen: `↑↓ row · ←→ fold · Tab pane · Enter drill · / cmd · w board · ? help · r refresh · q quit`
- WaveBoardScreen: `↑↓ row · Enter drill · f filter · / cmd · ? help · Esc back · q quit`
- ConfigModal: `↑↓ field · ←→ cycle · Space toggle · Enter edit · s save · L layer · r reset · Esc close`

## 6. Failure modes + named edge cases

C06 introduces a TUI long-lived process bound to a daemon-push stream. The failure modes catalog the new surfaces: subscription health, render budget overshoot, runtime-switched event misorder, modal-stack overflow, snapshot drift.

| # | Failure mode | Trigger | Detection | Repair |
|---|---|---|---|---|
| F1 | Daemon unreachable on TUI launch | eawfd absent + cold-spawn fails (port collision, write permission, etc.) | `StateBinding.connect` catches `DaemonUnreachable`; flips `degraded=True`; starts mtime-poll loop | Banner `daemon offline · poll mode 2s`; TUI continues degraded; operator runs `eawf daemon status` to investigate |
| F2 | Subscription overflow (slow renderer) | TUI's render loop blocks on a heavy paint; daemon's per-subscriber queue (1024 default) overflows | Daemon sends `subscription_dropped` with `code=-32008 reason=overflow`; TUI receives the frame; flips degraded; resyncs from `since_version` | Auto-resync; if resync exceeds 10000-event cap, surface `event stream too far behind · press r to reload` modal |
| F3 | Daemon protocol version skew | CLI v0.4 against daemon v0.3 — `-32004 protocol_version_mismatch` | TUI receives the error frame on first subscribe; cannot operate | Blocking modal: `daemon protocol mismatch (daemon=vX, cli=vY) · run \`uv tool upgrade eawf\``; Esc exits 4 |
| F4 | First paint exceeds 150 ms budget | RoadmapTree on 50-phase state takes >150 ms to compose | Perf test fails CI; build blocked | Optimise hot path (lazy load deeper per D18; defer non-visible widgets to next tick); reduce fixture or relax budget with explicit Open Question |
| F5 | Keypress→render exceeds 50 ms budget | Heavy on_key handler (e.g. RoadmapTree rebuild on filter) | Perf test fails CI | Move heavy work to `@work` worker per Textual workers guide [13]; keep on_key sync path lightweight |
| F6 | Modal stack overflow | Operator attempts to push a 4th modal | `EaApp.push_screen` checks depth; rejects with notify | Toast `modal stack depth limit (3) reached — close one first`; no state change |
| F7 | Snapshot drift on golden | Pixel-level change in a snapshot (e.g. theme color tweak) | CI diffs SVG bytes; fail | Regen via `EAWF_SNAPSHOT_REGEN=1 pytest`; commit new goldens with explanation in PR body |
| F8 | Asciinema cast non-deterministic | Test runs vary between machines | Cast diff at commit time | Investigate non-determinism source (timing, env var, random seed); cast must be byte-stable per D7 |
| F9 | Runtime-switched event out of order | Network reordering surfaces `runtime_switched` after subsequent `wave_close` | Header runtime cell incorrectly persists warn color after wave closes | StateBinding queues envelopes in event_id order before apply; ordering invariant enforced |
| F10 | Daemon restarts mid-subscription | eawfd graceful shutdown during operator session | TUI sees `subscription_dropped reason=daemon_shutdown` | Reconnect with backoff (1s, 2s, 4s, 8s); banner cycles through `daemon restarting...` → `daemon back online` |
| F11 | Nerd Font glyphs render as fallback boxes | Terminal lacks Nerd Font; `EAWF_NERD_FONT` unset | Visually broken header / glyphs | First-launch chip per tui-ux-resolved §Nerd-font hint [7:231-242] suggests installing Nerd Font; operator dismisses with persistent flag |
| F12 | `EAWF_TZ` invalid value | Unparseable IANA zone in env | `zoneinfo.ZoneInfoNotFoundError` at clock render | Fall back to UTC; log warning once per session |
| F13 | Wave board cursor on a wave that's deleted mid-render | Operator views wave board; another mutator drops the wave | StateBinding receives `wave_dropped` event; current cursor row → KeyError | Cursor snaps to previous wave; toast `wave <id> deleted` |
| F14 | Plan-mode preview spec aggregate inconsistent | `state.phases[P##].iter_ids != [IterSpec.id]` (drift) | C03 §5.6 `eawf spec lint` returns error | PlanPreviewModal surfaces the inconsistency at the top; Approve disabled until lint passes |
| F15 | Edit Plan subagent never returns | Subagent hangs / process killed | Daemon's per-wave wall-clock cap fires (C02 §5.8 [3:456-468]) | TUI receives `subprocess_oom_killed` or `wave_blocked` event; modal updates to `Edit Plan failed: <reason>`; operator retries or aborts |
| F16 | NeedsUserModal pile-up | Multiple `needs_user_pause` envelopes queued for same scope/session | C04 GLOBAL-F16 [5:1281] — daemon enforces single-active needs_user | TUI shows one at a time; subsequent envelopes queued in `:events` ring buffer; auto-open next when current dismissed |
| F17 | Theme swap fails (CSS reload error) | Invalid theme dict | Textual `Theme.from_dict` raises | Toast `theme swap failed: <reason>`; theme stays unchanged |
| F18 | `/pr` overlay shell-out fails (`gh` missing) | Operator's machine lacks `gh` CLI | Subprocess returns non-zero / not-found | PrListModal renders error stripe `gh not on PATH · install: brew install gh`; doesn't crash TUI |
| F19 | Mutating audit overlay subagent dispatch fails | `eawf agent dispatch` returns error | TUI receives error envelope | AuditFailedModal stays open; surfaces dispatch error stripe; operator can retry or abandon |
| F20 | Scope dispatch ambiguity (cwd has both workspace and repo `.ea/state.json`) | Nested workspace inside repo (or vice versa) | `state/resolve.py` returns first match upward | Resolution honors innermost match (cwd's nearest `.ea/state.json`); operator overrides via `--user`/`--workspace`/`--repo` flag |
| F21 | TUI launched in narrow terminal (<80 cols) | Operator's terminal too narrow | App.on_mount detects cols | Exit with hint `resize to ≥ 80 cols and rerun` per tui-layout §"Narrow terminal" [9:312-313]; status fallback if scripted |
| F22 | State mutation races daemon-push | Operator runs `eawf wave close <W##>` from another terminal while TUI is rendering wave detail | TUI's reactive state updates mid-render | Textual's reactive watch re-renders; modal stays open showing new state; no crash |
| F23 | `/metrics` daemon telemetry not yet projected | TUI requests `telemetry.metrics` before C09 telemetry-projector runs | RPC returns empty MetricsReport | MetricsModal shows placeholders: `(no data — telemetry projector not yet enabled)`; operator runs `eawf metrics rebuild` to project from event.jsonl |
| F24 | Onboarding splash dismissed flag corrupted | `<local-path>` invalid JSON | TUI silently skips splash | Log warning; treat as undismissed (show splash); rewrite on dismiss |
| F25 | Scope switch (Ctrl-W from repo) when registry empty | Operator presses Ctrl-W with no workspace registry | `run_workspace_tui` finds no workspace.json | Toast `no workspace registered · use eawf workspace init <code>`; stay on RepoScreen |

### Edge cases

- **TUI on SSH session.** Per C02 §5.4 [3:357-358] daemon binds to UDS in the SSH session's UID context. TUI connects to the same UDS. Works; no special handling needed.
- **TUI in tmux.** Textual handles tmux multiplexing natively. Asciinema generation skipped inside tmux (set `EAWF_NO_ASCIINEMA=1` if needed).
- **TUI in screen.** Same as tmux.
- **TUI on macOS Terminal.app.** Box-drawing chars render with gaps per P20 direction §"Terminal / font findings" [2:337-378]. Operator advised to use Ghostty / iTerm2 / Kitty / WezTerm; warning chip on first launch.
- **Daemon-version mismatch detected mid-session.** Daemon upgraded while TUI running; new `event.push` carries new fields the TUI's Pydantic model rejects. StateBinding logs error; TUI degrades to mtime-poll until operator restarts.
- **Plan-mode preview when WaveSpec count mismatches Phase.iter_ids.** PlanPreviewModal renders the union; flags inconsistency at the top; Approve button disabled until `eawf spec lint <P##>` passes.
- **Modal stack: open detail-modal on a row in another detail-modal (depth-3 chain).** Honored up to cap; fourth push rejected per F6.
- **Concurrent TUIs running on same `.ea/state.json`.** Both subscribe to daemon; both receive the same push frames; both render. No coordination needed — they're passive readers per V1 carve-out.
- **TUI mid-roadmap-tree-rebuild interrupted by Ctrl-C.** Graceful shutdown via signal handler; alt-screen restored; cursor shown.
- **`eawf agent dispatch` from TUI when daemon is in `BLOCKED` runtime state.** TUI surfaces the error envelope per F19; operator runs `eawf wave switch --to <runtime>` from CLI to recover.
- **Audit-failed overlay mutating menu selection lands but subagent runs `/flow` recursively, which itself emits `needs_user`.** TUI receives `needs_user_pause` event; opens NeedsUserModal on top of AuditFailedModal (stack depth 2); operator resumes both flows in order.
- **`/pr` cache stale: PR merged externally between caches.** 60 s cache window may show stale PR. Operator presses `r` in the modal to force refresh.
- **TUI launched without `state.json` existing (fresh project).** OnboardingSplash shows the repo-scope splash; RepoScreen renders with placeholder text in each pane.
- **Theme swap to `cb` (alias of default).** No-op; theme stays Wong 2011; toast confirms.

## 7. Migration plan

C06 is the largest single-cluster contract for the operator surface — but the implementation lands incrementally. The migration runs in five phases (candidate phase IDs P22-P26, finalized at each phase's prep) and lands the TUI rebuild without breaking the current `src/eawf/tui/` until the closing wave swaps the bare-`eawf` dispatch.

### 7.1 Phase 1 — Module scaffolding + StateBinding (P22-W01..W04)

**Goal.** `src/eawf/tui_v2/` tree scaffolded; `StateBinding` connects to daemon via `event.subscribe`; mtime-poll fallback works; basic RepoScreen with placeholder widgets renders.

**Waves.**

- **W01.** `src/eawf/tui_v2/__init__.py` + `app.py` + `theme.css` + `theme.py` + dispatch ladder in `cli/app.py` extended.
- **W02.** `StateBinding` (`event.subscribe` path + mtime-poll fallback); `DaemonClient` integration with C02 §5.3 method catalog.
- **W03.** `Header` / `Footer` / `Heartbeat` shared chassis widgets; `OnboardingSplash` screen; per-scope dismissed flag.
- **W04.** RepoScreen skeleton with placeholder panes (Roadmap title only, Status text-only); CI snapshot for repo-screen-empty.

Closing this phase: `eawf` in repo dir launches the new TUI; operator sees Header + 2x2 placeholder + Footer; no live data wired yet. Legacy `src/eawf/tui/` still reachable via `EAWF_TUI_LEGACY=1` env opt-in.

### 7.2 Phase 2 — Widgets + reactive state (P23-W01..W06)

**Goal.** Widget catalog lands; widgets watch `StateBinding.state`; live rendering against real `state.json`.

**Waves.**

- **W01.** `RoadmapTree(Tree)` with V12 glyph schema + 5-cell EU bar + hybrid lazy load.
- **W02.** `StatusPane` with V8 SESSIONS sub-block + V5 runtime cell hookup.
- **W03.** `GitPane` w/ subprocess cache + ahead/behind counter.
- **W04.** `BacklogTable(DataTable)` w/ sort + filter.
- **W05.** `EUBar` + `EUMeter` + `ChoiceButton` + `ConfigRow`.
- **W06.** `WorkspaceScreen` (top strip + active-repo quadrant) + `UserScreen` (three sections) — composing existing widgets per D3 shared chassis.

Closing: full quadrant rendering works in all three scopes; live state updates flow through `StateBinding`.

### 7.3 Phase 3 — Palette + modal stack (P24-W01..W07)

**Goal.** `/` palette operational with full verb registry; all modal screens land.

**Waves.**

- **W01.** `CommandPalette(ModalScreen)` + verb registry + fuzzy match + autocomplete.
- **W02.** Per-verb handlers (`/find`, `/filter`, `/sort`, `/theme`, `/events`, `/help`, `/quit`).
- **W03.** `DetailModal` w/ h/d/m/e/dp tabs.
- **W04.** `AuditRunningModal` + auto-open hook on `audit_started` event.
- **W05.** `AuditFailedModal` with mutating menu (D17) + subagent dispatch wiring via `eawf agent dispatch`.
- **W06.** `PlanPreviewModal` rendering PhaseSpec + IterSpec + WaveSpec aggregate; 3-option AUQ; Approve runs `eawf roadmap apply` only per D4.
- **W07.** `NeedsUserModal` + `event.subscribe` filter for `needs_user_pause` envelopes + resume RPC.

Closing: full operator flow reachable from TUI (plan → approve → activate → mutating audit fix).

### 7.4 Phase 4 — Specials: WaveBoard + ConfigModal + /pr + /metrics (P25-W01..W08)

**Goal.** Specialized screens + overlays land; performance budget enforced in CI.

**Waves.**

- **W01.** `WaveBoardScreen` w/ list + detail view; V11 verb set.
- **W02.** `ConfigModal` w/ tabbed surface; `EditFieldModal` for typed fields; metadata registry shared with `eawf config`.
- **W03.** `PrListModal` w/ `gh` shell-out + 60 s cache + graceful degrade.
- **W04.** `MetricsModal` w/ 3×2 grid + 5 s refresh against daemon telemetry RPC.
- **W05.** `EventsModal` ring buffer + filter cycle.
- **W06.** `ConfirmModal` arrow-toggle Yes/No.
- **W07.** Snapshot test harness + 16 SVG fixtures.
- **W08.** Perf budget tests in CI (first-paint <150ms, keypress <50ms); CI gate enabled.

Closing: full TUI surface complete; perf budgets enforced.

### 7.5 Phase 5 — Asciinema + docs + bare-`eawf` cutover (P26-W01..W04)

**Goal.** Asciinema generation lands; docs ship; bare-`eawf` flips to `tui_v2`; legacy `src/eawf/tui/` removed per AGENTS deletion rule.

**Waves.**

- **W01.** `snapshot/asciinema.py` + `eawf tui asciinema` CLI verb (new in C05's surface).
- **W02.** docs/architecture/tui.md update + per-scope cast files in `docs/architecture/tui/casts/`.
- **W03.** Bare-`eawf` dispatch flag-cutover: default `tui.engine: tui_v2` (was `tui` in legacy); legacy reachable via `EAWF_TUI_LEGACY=1`.
- **W04.** Legacy `src/eawf/tui/` removed; 5300 LOC deleted per AGENTS deletion rule with explicit deletion enumeration in commit body. Salvaged constants (palette, glyph schema, sort keys) live under `src/eawf/tui_v2/constants.py`.

Closing: `feature/eawf-v0.3-c06-tui` branch ready for phase-PR merge; legacy TUI gone; C06 implementation complete.

### 7.6 Web stub (P27-W01..W03, optional)

**Goal.** Daemon-side WebSocket bridge contract implemented; minimal SPA reading the bridge in local-only deployment. Optional phase — gates on whether the web-cluster brief picks the SPA stack.

**Waves.**

- **W01.** `eawfd-web` companion process: WebSocket server at `<host>:<port>`; protocol per §5.11; token auth via `<local-path>`.
- **W02.** `eawf daemon enable-web` + `eawf daemon disable-web` CLI verbs.
- **W03.** Reference SPA implementation (per web-cluster brief's pick); deployment-local-only; CI smoke covers WS subscribe + state.read.

### 7.7 Migration safety

**Defense-in-depth retained.** Legacy `src/eawf/tui/` stays as a parallel surface through P22-P25; cutover at P26-W03 (flag flip). Operators who hit a regression in `tui_v2` can opt back to legacy via `EAWF_TUI_LEGACY=1` for one alpha cycle before the legacy code deletes at P26-W04.

**Rollback.** Per-phase flag-gated. If P26-W03 cutover proves unsafe (perf regression, missing widget), flip default back; legacy continues to serve. Hard rollback before P26-W04 only — once legacy is deleted, recovery requires `git revert`.

**Schema unchanged.** No `state.json` schema bumps; the TUI is a passive consumer. State.config gains new optional fields (`tui.engine`, `tui.theme`, `tui.scope_session`) but they default to backwards-compatible values.

**Pre-commit + CI.** C06 implementation phase gains:
- `snapshot/svg` golden tests on every push.
- `perf` markers run via `pytest -m perf` in a dedicated CI job.
- `tui_v2/` paths exempted from the `.ea/specs/` allow-list per `tools/commit_prefix_lint.py` extension (no change required; existing `_STATE_ONLY_PREFIXES` is sufficient).

**Documentation.** docs/architecture/tui.md replaced (not deleted) — new doc reflects `tui_v2/`. AGENTS.md gains a §"TUI architecture" pointer when C06 closes.

## 8. Open questions for operator

The 24 original decision rows in §4 were ratified via AUQ on 2026-05-17 (initial pass). The 5 /blitz rounds on 2026-05-17 resolved 21 additional axes (Q1..Q16 + Q-new1..Q-new5), promoted into §4 as D25..D34. Status flips to `accepted`. The following residual items remain open — each gates on operator confirmation at C06 implementation-phase prep, not on cluster-brief ratification.

### Q-r1 — Phase IDs for C06 migration plan (§7)

**Question.** §7 names candidate phase IDs P22..P26 for the 5-phase C06 migration. The actual phase IDs depend on the operator's roadmap state at the time of C06 implementation. Should the brief reserve these IDs, or stay placeholder?

**Options.**
- (a) Stay placeholder; phase IDs finalized at each phase's prep (Recommended).
- (b) Reserve P22..P26 in `state.json` PLANNED status now.
- (c) Defer naming until C03 implementation phase closes (since C06 depends on C03).

**Recommendation.** (a). Phase IDs cheap to assign at prep time; reservation now risks holding numbers operator wants for other work.

### Q-r2 — `EAWF_TUI_LEGACY` env flag durability

**Question.** The migration plan (§7.5) flips bare-`eawf` to `tui_v2` at P26-W03 with `EAWF_TUI_LEGACY=1` env-opt-in to legacy `src/eawf/tui/`. How long does that env flag survive?

**Options.**
- (a) One alpha cycle, removed at P26-W04 alongside legacy code (Recommended).
- (b) Two alpha cycles, deprecation warning at first cycle.
- (c) Indefinite — legacy stays reachable for v0.4+.

**Recommendation.** (a). Matches AGENTS deletion rule + clean cut. Operators who hit a regression have one cycle to flag it.

### Q-r3 — Web-stub SPA stack (deferred per D6)

**Question.** Which SPA stack does the web-cluster brief pick?

**Options (seeds for the future cluster brief).**
- (a) Preact + signals — minimal bundle (~3 KB), reactive shape matches Textual.
- (b) Lit web components — standards-friendly; can render inside any host.
- (c) SvelteKit — most ergonomic reactivity; heavier build chain.
- (d) Tauri desktop wrapper — bundles WebView; cross-platform native shell.

**Recommendation.** Defer to web-cluster brief. C06 specs only the WS bridge contract.

### Q-r4 — Theme CSS file extension convention

**Question.** Textual project convention names CSS files `.tcss` (Textual CSS); this brief uses `.css`. Which does C06 implementation phase land?

**Options.**
- (a) `.tcss` (matches Textual convention; clearer file-type discrimination) (Recommended).
- (b) `.css` (matches generic CSS tooling; loses Textual-specific signal).
- (c) Operator decides at implementation phase prep.

**Recommendation.** (a). Pre-commit hook can validate `.tcss` extension on `src/eawf/tui_v2/**/*` so editor tooling picks the Textual-CSS syntax.

### Q-r5 — Snapshot diff renderer

**Question.** When ASCII text snapshot diff fails, how does CI present the diff?

**Options.**
- (a) Unified diff with ANSI escape codes rendered as `\x1b[...]` literals (Recommended).
- (b) Diff with ANSI stripped (text-only).
- (c) HTML diff with ANSI rendered as inline colors.

**Recommendation.** (a). Plain text diff matches CI log viewer; operator sees the actual byte difference including styling. ANSI-stripped diff hides color regressions.

## 9. References

[1] `.ea/local/research/long-term/2026-05-16-c00-spec-index.md` — C00 spec architecture index. V1 [1:24-53] daemon Day-1 + smart-spawn writer; V2 [1:55-74] three-tier specs; V3 [1:76-96] composable profile bundle; V5 [1:127-151] reactive runtime fallback; V7 [1:184-224] telemetry vendor-then-rebuild; V8 [1:226-271] hybrid session reuse; V9 [1:273-315] native per-runtime plugins. §C06 [1:587-644] full scope.

[2] `.ea/local/research/2026-05-16-p20-tui-design-direction.md` — P20 TUI rebuild direction brief. Stack pick [2:21]; gap matrix [2:64-101]; RC-1..RC-9 [2:83-101]; salvage matrix [2:109-126]; module tree sketch [2:128-160]; smoke demo inventory [2:50-61]; success-criteria contract [2:176-212]; spec storage proposal [2:230-294]; CC-style pattern catalog [2:393-415]; 17 rounds of decision picks [2:416-552]; critical contracts [2:582-588].

[3] `.ea/local/research/long-term/2026-05-16-c02-daemon-topology.md` — C02 Daemon spine. §5.3 method catalog [3:290-345] (`state.subscribe`, `event.subscribe`, `agent.dispatch`, `wave.switch`); §5.7 subscription bus + backpressure [3:434-454]; §5.12 runtime fallback state machine [3:720-790]; §5.13 session-handle tracking schema [3:792-902]; D7 per-subscriber queue depth [3:140]; D9 protocol version skew [3:142].

[4] `.ea/local/research/long-term/2026-05-16-c03-spec-infrastructure.md` — C03 Spec Infrastructure. §5.2 PhaseSpec [4:216-282]; §5.3 IterSpec [4:293-348]; §5.4 WaveSpec [4:352-433]; §5.6 AuditSpec + cadence [4:546-583]; §5.7 `verify_implements` audit-DSL kind [4:588-712]; §5.8 daemon spec-cache surface [4:714-737]; §5.9 CLI surface [4:741-764].

[5] `.ea/local/research/long-term/2026-05-16-c04-workflow-skills.md` — C04 Workflow & Skills. §5.1 skill catalog [5:208-223]; §5.2 envelope contract [5:226-281]; §5.3 SkillManifest schema [5:285-385]; §5.4 per-skill subsections [5:399-955]; §5.5 orchestration sequence diagrams [5:957-1080]; §5.6 plan-mode preview [5:1082-1140]; §5.7 Edit Plan subagent [5:1142-1216]; §5.8 skill registry + sync [5:1218-1259].

[6] `.ea/local/research/2026-05-14-p20-tui-verdicts.md` — P20 V1-V24 verdicts. V1 wave board [6:30-42]; V6 workspace top-strip [6:312-336]; V7 metrics envelope [6:340-400]; V8 status pane sessions [6:402-457]; V11 wave-board palette verbs [6:553-582]; V12 glyph schema [6:585-621]; V13 config field input [6:626-647]; V14 wave-board filter [6:651-657]; V15 config dirty Esc [6:660-666]; V18 workspace strip cap [6:686-690]; V20 asciinema cast [6:694-697]; V24 weekly_eu_target default [6:723-727].

[7] `.ea/local/research/2026-05-11-tui-ux-resolved.md` — TUI UX-resolved decisions. Resolved matrix [7:11-50]; header chrome [7:52-74]; keymap [7:75-108]; command palette [7:111-127]; footer hints [7:130-138]; sort defaults [7:140-150]; live-event flash [7:152-163]; onboarding splash [7:165-227]; theme palette [7:489-508]; glyph set [7:510-571]; detail overlay [7:586-606]; pane scaling [7:617-720]; r/R force-refresh [7:272-290]; `:events` ring buffer [7:304-326]; heartbeat color state [7:330-345]; worktree surfacing [7:347-374]; audit-running overlay [7:376-401]; `:find` jump [7:403-416]; `:pr` overlay [7:418-450]; cross-repo totals [7:452-468]; error rendering [7:471-487].

[8] `.ea/local/research/2026-05-11-workspace-and-user-tui.md` — Workspace + user-scope dashboards. Dispatch algorithm [8:38-58]; workspace layout [8:60-126]; user-scope layout [8:131-208]; live progress [8:212-263]; implementation sketch [8:265-307]; success criteria [8:309-330].

[9] `.ea/local/research/2026-05-11-tui-layout.md` — Repo-scope quadrant layout. Verdict matrix [9:22-32]; constraints inherited [9:35-54]; chosen layout [9:56-81]; pane contents [9:84-95]; keymap [9:97-124]; drilldown overlay [9:131-152]; success criteria for W03 [9:257-290]; implementation sketch [9:198-255].

[10] `.ea/local/research/long-term/2026-05-15-language-and-pyo3-fit.md` — Language brief. §F5 [10:77-87] daemon concurrency model is asyncio JSON-RPC + threaded executor; §D49 [10:144-145] locks the pick. Cited by C02 [3].

[11] `.ea/local/research/archive/2026-05-16-tui-library-selection.md` — TUI library selection brief. Verdict matrix [11:28-37]; comparison [11:42-49]; findings F1-F5 [11:53-120]; recommendation [11:124-138]; next experiment [11:141-148].

[12] `src/eawf/tui/` — current TUI implementation (5300 LOC). `app.py:128` `DEFAULT_REFRESH_HZ = 30` (RC-3 source); `layout.py:321-341` `build_roadmap_pane` (RC-4 source); `workspace.py` 635 LOC dead code (RC-2 source); `portfolio.py` 729 LOC dead code (RC-2 source); `audit_overlay.py:407` `open_audit_overlay` (RC-7 source); `wave_board.py:540-606` `apply_key` (RC-6 source).

[13] Textual docs — https://textual.textualize.io. Getting started [13:1] https://textual.textualize.io/getting_started/; App basics https://textual.textualize.io/guide/app/; Input + focus + bindings https://textual.textualize.io/guide/input/; Reactivity https://textual.textualize.io/guide/reactivity/; Workers https://textual.textualize.io/guide/workers/; Command palette https://textual.textualize.io/guide/command_palette/; Testing (Pilot) https://textual.textualize.io/guide/testing/.

[14] `.ea/local/smoke/tui-libs/` — 9 runnable smoke demos under PEP-723 inline-metadata. `textual_demo.py` (279 LOC) — Roadmap Tree baseline; `cc_palette_demo.py` (398 LOC) — slash palette + @-mention + ConfirmModal; `cc_render_demo.py` (443 LOC) — TabbedContent + Markdown + plan preview; `cc_flow_demo.py` (435 LOC) — modal stack + tabbed settings + ConfigRow + toasts; `cc_stream_demo.py` (450 LOC) — RichLog streaming + StatusBar + WaveMeter; `workspace_demo.py` (359 LOC) — multi-repo top strip; `polish_demo.py` (363 LOC) — 6 progress-bar styles + Switch/Checkbox/LoadingIndicator/Rule; `anim_demo.py` (458 LOC) — 8 animation primitives; `rich_demo.py` (450 LOC) + `ptk_demo.py` (349 LOC) — alt-stack comparison.

[15] `AGENTS.md` — non-negotiable rules. Rule 1 CLI dispatch; Rule 2 Pydantic `extra="forbid"`; Rule 4 single-canonical-mutator; Rule 9 f-strings only; Rule 11 worktree discipline; Rule 17 naming conventions (`scope_id` not `scope`; `wave=` log-key form); Rule 18 chassis + citations; §"Worktree discipline"; §"Branch naming"; §"Secrets and PII hygiene".

[16] `feedback_tui_keymap_conventions.md` (user memory) — arrows primary; vim aliases; full key names (`PageUp`/`PageDown`/`Home`/`End`); no `PgUp`/`PgDn`.

[17] `feedback_tui_branding.md` (user memory) — header brand literal `Eä` (capital E + a-umlaut); bold accent; outside-left of breadcrumb.

[18] `feedback_abstract_placeholder_names.md` (user memory) — abstract three-letter codes (`ABC`/`DEF`/`GHI`) in mockups, not name-shaped placeholders.

[19] `src/eawf/render/envelope.py` — `EnvelopeHeader` + `EnvelopeFooter` + `OutputEnvelope` Pydantic models; `EnvelopeStatus = Literal["ok", "needs_user", "blocked", "failed", "partial"]` [19:48]; `EnvelopeHeader.finished_at >= started_at` model_validator [19:113-121]; round-trip helpers `to_markdown` / `from_markdown` [19:23-26].

[20] `src/eawf/state/models.py` — current state Pydantic models. `WorkspaceRepoRef` [20:119]; `Wave` [20:221-241]; `Phase` [20:190-204]; `Iter` [20:207-218]; `Audit` [20:259-271].

[21] `src/eawf/cli/app.py` — current CLI entry-point with bare-command dispatch (legacy TUI launcher); extends to scope dispatch ladder per D10 in §5.2.

[22] `src/eawf/state/resolve.py` — cwd-upward state resolution; extended by C06 dispatch ladder.

[23] `pyproject.toml` — current runtime deps (`rich>=15.0.0`, `questionary`, `typer`). C06 adds `textual>=8.0` for `tui_v2/` (legacy `tui/` keeps `rich.live`).

[24] `<local-path>` — user-scope registry; consumed by `UserScreen` portfolio section per `workspace-and-user-tui.md` §3 [8:131-208].

[25] `<local-path>` — daemon UDS path per C02 §5.4 [3:347-359] (POSIX); `\\.\pipe\eawfd-<user>` on Windows. Web-stub bridge separately at `<host>:<port>`.

[26] `<local-path>` — TUI session state file (theme per scope, onboarding-dismissed flags). Gitignored.

[27] `<local-path>` — web-stub auth token (per-session, permission `0600`). Gitignored.

[28] `docs/architecture/tui.md` — current TUI architecture doc. Replaced (not deleted) at C06 closing wave.

[29] `tools/commit_prefix_lint.py` — commit-prefix lint. `_STATE_ONLY_PREFIXES` allow-list [29:61] already includes `.ea/specs/`; no extension needed for `tui_v2/` paths.

[30] `.pre-commit-config.yaml` — pre-commit pipeline; perf + snapshot tests run in CI rather than pre-commit (too slow for local commit).

[31] Wong 2011 deuteranopia palette — https://www.nature.com/articles/nmeth.1618. Referenced for color values in `tui-ux-resolved.md` §Theme palette [7:489-508].

[32] asciinema cast file format v2 — https://docs.asciinema.org/manual/asciicast/v2/. Referenced for `record_cast` output format in §5.15.

[33] Textual `Pilot` testing API — https://textual.textualize.io/api/pilot/. Drives the snapshot harness in §5.14.

## 10. Provenance

- `store_record=none (local-only research)`
- `commit=3b86f7a (parent at session start; revisions 2026-05-18)`
- `cluster=C06`
- `consumes=C00 verdicts V1, V2, V3, V5, V7, V9 (locked 2026-05-16 [1:22-271])`
- `last_revised=2026-05-18 (audit-driven: TUI library locked to Textual per Q6 / D-SUP-TUI-01; snapshot format unified per Codex C06-I004 — text snapshots primary, SVG via save_screenshot retired per Q-new1; web stub marked stub-only per Codex C06-I005; subscription protocol consumes C07b canonical Event model per E-29 / Q14; legacy TUI deletion replaced by migration + state verdict per Codex C06-I010; TuiSession Pydantic sketch added per E-26 / CROSS.F11; daemon-recovery detection specced per E-25)`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md (polish-sweep; 12 Codex issues)`
- `D_SUP_TUI_01=C06 supersedes P14 rich pick (v0.3+ rebuild on Textual per Q6 2026-05-18). Memory project_p14_direction superseded by project_tui_textual_v03.`
- `tui_library=textual (locked 2026-05-18 per Q6)`
- `authority_binding=Q1 (2026-05-18): daemon = sole writer; TUI is reader-only, consumes daemon-push event.subscribe stream.`
- `consumes=C02 §5.3 + §5.7 + §5.12 + §5.13 + D7 + D9 (operator-ratified 2026-05-16)`
- `consumes=C03 §5.2 + §5.3 + §5.4 + §5.6 + §5.7 + §5.8 + §5.9 (operator-ratified 2026-05-16)`
- `consumes=C04 §5.1 + §5.2 + §5.3 + §5.6 + §5.7 + D4 + D5 (operator-ratified 2026-05-16)`
- `consumes=P20 design direction RC-1..RC-9 + 17 rounds [2]`
- `supersedes=none`
- `session=eawf-spec-c06-operator-surface-2026-05-17`
- `length_actual=~1900 lines after /blitz integration (target ~1400 per C00 §Estimated effort; past C00 §Naming convention split-threshold ~1500). Substance-driven overshoot — full Pydantic shape, per-screen composition examples, 34 decision rows (D1..D34), per-failure mitigations, 5-phase migration, 5 residual open questions. If operator prefers, follow-up wave can split into c06a (architecture + widgets + reactive state — §1..§5.7) + c06b (V5/V7 hooks + theming + perf + snapshot + asciinema + web stub + migration — §5.8..§11). No split applied here.`
- `operator_decisions_locked=2026-05-17 AUQ initial pass D1..D24 — D1 JSON-RPC event.subscribe; D2 2 s poll + banner; D3 shared chassis; **D4 OVERRIDE: roadmap apply only (brief recommended apply+activate chain)**; D5 subagent dispatch; D6 defer SPA; D7 Pilot SVG sequence (later overridden — see Q-new1); D8 header runtime cell + toast; D9 3×2 metrics grid + 5 s refresh; D10 cwd ladder; D11 fixed keybindings; D12 /theme verb + session persist; D13 static palette registry; D14 modal cap 3; D15 per-scope splash; D16 <150ms + <50ms perf gate; D17 full mutating audit menu; D18 hybrid lazy load; D19 daemon-push primary; D20 per-screen ~12 snapshots; D21 /pr global verb + 60 s cache; D22 heartbeat pulse + color + r ack; D23 footer hints + ? modal; D24 needs_user modal AUQ`
- `blitz_rounds_locked=2026-05-17 /blitz R1..R5 Q1..Q16 + Q-new1..Q-new5 — Q1 footer hint only; Q2 theme+onboarding flags only; Q3 defer SPA stack; **Q4 OVERRIDE: defer CB palette to v0.4 (brief recommended alias of default)**; Q5 fixed 3×2 grid; **Q6 OVERRIDE: 50 ms frame_ms (brief recommended 100 ms)**; Q7 50-phase fixture; Q8 60 s fixed /pr cache; Q9 dark-assumption default; Q10 modal status line streaming; Q11 heartbeat red on daemon-down + pane errors; **Q12 OVERRIDE: : alias removed immediately (brief recommended : alias forever)**; Q13 instant scope-switch; Q14 300 s Edit Plan cap; **Q15 OVERRIDE: configurable strip_max_rows (brief recommended fixed at 8)**; Q16 placeholder text empty metrics; **Q-new1 OVERRIDE: ASCII text snapshots (brief recommended SVG via save_screenshot)**; Q-new2 full state reload on apply failure; Q-new3 runtime: idle muted; Q-new4 onboarding re-show keeps flag; Q-new5 ignore subsequent ? while help open`
- `overrides_total=6 — D4, Q4, Q6, Q12, Q15, Q-new1. All others matched brief recommendation.`
- `D25..D34 added to §4 Decision matrix to fold ratified Q1..Q16 + Q-new1..Q-new5 picks into a single locked surface.`
- `verification ladder applied`:
  - source: `src/eawf/render/envelope.py` [19] read; status enum + header/footer field set cited at lines.
  - source: `src/eawf/tui/` legacy tree [12] line-counted via `wc -l`; RC-1..RC-9 verified per P20 direction [2].
  - source: `.ea/local/smoke/tui-libs/textual_demo.py` [14] read in full; Tree widget + RoadmapTree composition pattern cited.
  - source: `.ea/local/smoke/tui-libs/cc_palette_demo.py` [14] read for fuzzy_match + ConfirmModal idiom.
  - cross-brief: C00 V1/V2/V3/V5/V7 quoted inline.
  - cross-brief: C02 §5.3 method catalog + §5.13 session-handle schema + D7 + D9 cited.
  - cross-brief: C03 §5.4 WaveSpec + §5.6 AuditSpec + §5.7 verify_implements cited.
  - cross-brief: C04 §5.6 plan-mode preview + §5.7 Edit Plan + D4/D5 cited.
  - cross-brief: P20 direction RC-1..RC-9 + 17 rounds quoted by line.

## 11. Scrub

- status: **clean** (per AGENTS rule 16 [15]).
- references: repo-relative paths only OR public external URLs (textual.textualize.io, nature.com, docs.asciinema.org).
- local paths: none. The brief documents file paths conventional to TUI session state (`<local-path>`), daemon UDS / named-pipe install targets (`<local-path>`, `\\.\pipe\eawfd-<user>`), and web-stub auth token (`<local-path>`) — all are *target install paths*, not host-specific paths.
- real emails: none. Author block is `claude-opus-4-7` (model id).
- abstract placeholder names: when mocking up workspace top-strip rows, the brief uses `ABC` / `DEF` / `GHI` / `JKL` per `feedback_abstract_placeholder_names.md` [18]; no real project names.
- machine paths: none. The UDS path `<local-path>` is conventional substitution form.
- hostnames / IPs: `<host>` for the web-stub bridge (local-only loopback, not a public host).
- secrets / tokens: `<local-path>` is named as a target file for the web-stub auth token; no token value committed.
- companion-doc references: all repo-relative (`.ea/local/research/long-term/...`) or external URL.
