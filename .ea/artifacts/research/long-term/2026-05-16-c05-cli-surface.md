# C05 — CLI Surface — Eä framework long-term specs

**Cluster:** C05 (CLI Surface — verb-noun matrix, output formats, exit codes, error envelopes, help model, shell completion, daemon escalation, streaming output, stability tiers)

**Title:** CLI Surface

**Status:** `accepted` (ratified 2026-05-17 — 4-round AskUserQuestion blitz over §8 axes; see §8 ratified-verdict table)

**Created:** `2026-05-16T00:00:00Z`

**Ratified:** `2026-05-17T00:00:00Z`

**Author:** `claude-opus-4-7`

**Depends on:** C01 (URN + entity catalog + scope vocabulary) [2]; C02 (daemon IPC + protocol-version + error-code table + escalation rules) [3]; C03 (PhaseSpec/IterSpec/WaveSpec verbs) [4]; C04 (skill catalog + envelope contract) [5]

**Consumed by:** C06 (palette verb registry — TUI mirrors CLI verbs); C07 (subsystem CLI verbs — runtime, plugin, mcp); C09 (help-model docs + verb stability lints); C10 (operator-facing docs + shell-completion install path); C11 (external integrations call CLI verbs)

## 1. Purpose + scope statement

Lock the v0.3 → v0.5 CLI verb-noun matrix for the `eawf` Typer dispatcher [21], the canonical output format set (`--json` / `--plain` / `--md` / `--quiet` / `--verbose`), the exit-code taxonomy, the error-envelope Pydantic schema, the help model (top-level / per-verb / `eawf help <topic>` prose), shell-completion install path, daemon-vs-daemonless escalation rules per verb, streaming output for long-running ops, the stability-tier matrix per current verb, and the migration plan that lifts today's manually-registered Typer surface [21:114-388] into the C05-spec'd shape.

The CLI is dispatch only per AGENTS rule 1 [11]: every CLI handler parses args, resolves the typed config / state object, calls a library function on validated typed payloads, and routes the result through `emit_json_or_text` [22]. Domain logic stays in `eawf.<subpackage>.*`. C05 names the verb surface; the library implementations live in C01..C04 + C07 + C08.

**Out of scope:**
- TUI launch logic for the bare `eawf` command (the TTY routes to the TUI per [21:79-86]; the cold-spawn / fallback semantics are owned by C06 [1:619-621]).
- Skill invocation surface inside non-Typer runtimes — Claude Code `/<name>`, Codex `/<name>`, OpenCode `/<name>` (owned by C04 [5:106-114]).
- The TUI palette verb registry that mirrors C05 (owned by C06; reads C05's verb matrix as the canonical source).
- Per-subsystem internals — `runtime.*`, `plugin.*`, `mcp.*` field semantics live in C07.

## 2. Goals + non-goals

### Goals

1. **One canonical verb-noun matrix.** Every CLI command in v0.3 → v0.5 is enumerated in §5.1 with subcommand list, stability tier, mutation flag, daemon-escalation rule, exit codes raised.
2. **One output-format flag set across every verb** so machine consumers (CI, agents, tests) emit byte-stable JSON via `--json`; humans use the default text branch; markdown round-trip uses `--md`; verbose-debug uses `--verbose`; colour-bypass uses `--plain`; minimal-output uses `--quiet`.
3. **One exit-code taxonomy** anchored on `eawf.cli.exit_codes` [23] — codes 0..9 stay, codes 10..12 are added for daemon-mediated errors per V1 [1:24-53] + V5 [1:127-151].
4. **One error envelope shape** consumed by `--json` mode and `OutputEnvelope` data sections — Pydantic v2 `BaseModel` with `ConfigDict(extra="forbid")` per AGENTS rule 2 [11]; includes `suggested_next_step` per C00 §C05 goal 4 [1:583-597].
5. **Daemon-escalation rules per verb** — mutating verbs always route through daemon (`state.mutate`); read-only verbs MAY bypass via `EAWF_DAEMONLESS=1` or `--daemonless` per V1 carve-outs [1:26-30].
6. **Daemon-control verb surface (V6 [1:153-182]).** `eawf daemon enable | disable | status | restart | logs | version`, mirrored by the daemon-internal RPC method set in C02 §5.3.5 [3:330-336].
7. **Metrics CLI surface (V7 [1:184-224]).** `eawf metrics show | export | rebuild` backed by the user-scope DuckDB / SQLite store at `<local-path>`.
8. **Manual runtime-switch (V5 [1:148-149]).** `eawf wave switch <wave-id> --to <runtime>` exposes the operator-initiated override that complements V5's reactive auto-fallback.
9. **Help model.** `eawf --help` lists top-level verbs grouped by registry panel (current shape from [25]); `eawf <verb> --help` lists subcommands + options; `eawf help <topic>` ships a small prose-topic surface (`exit-codes`, `daemon`, `profiles`, `urns`, `migration`, `streaming`).
10. **Shell completion.** Opt-in `eawf completion install [bash|zsh|fish]` writes the completion script; Typer ships the underlying generator. Today's `add_completion=False` on the root app [21:32] stays.
11. **Streaming output for long ops.** `/flow`, `/audit`, `wave dispatch`, `wave dispatch-batch`, agent-dispatch subscribe to daemon `event.subscribe` and stream `event.push` notifications. `--stream` opt-in; off by default for CI determinism.
12. **Stability tiers.** Every verb tagged `stable | experimental | deprecated`. Experimental verbs live max 3 alpha versions; deprecated verbs live 1 alpha then are removed.
13. **Migration plan.** Today's [21:114-388] manually-registered Typer surface lifts into a static registration table per KISS-005 [6:48,71] while preserving every existing command name.

### Non-goals

- TUI launch UX, palette verb registry, mtime-poll fallback — owned by C06.
- Per-subsystem CLI internals (`mcp` server discovery, `runtime` adapter scaffolding, `plugin` sync algorithm) — owned by C07.
- Output-format additions beyond the locked five flags — `--yaml`, `--csv`, `--xml`, `--proto` are deferred to C09 / C10 if a consumer surfaces.
- Per-verb interactive prompts (questionary wizards, AUQ surfaces beyond `--no-input`) — owned by C04 (skills carry their own UserQuestion bodies) + C10 (operator onboarding).
- Plugin / skill registry mutation through ad-hoc `--<flag>` knobs — every per-domain mutation routes through its noun-app verbs.
- Auto-discovery of new repos / projects — explicit init-only per the existing registry-growth feedback [`feedback_explicit_registry_only.md`].

## 3. Prior verdicts cited

### V1 — eawfd daemon Day-1 + smart-spawn writer [1:24-53]

Affected here: every mutating CLI verb is a thin client for `state.mutate` (or `wave.*`, `agent.*`, `runtime.*` per C02 §5.3 [3:290-345]). Read-only verbs MAY bypass via `EAWF_DAEMONLESS=1` or `--daemonless`. The CLI's auto-spawn polling logic [3:362-376] is invoked transparently on first mutation; `--verbose` surfaces the spawn step.

### V2 — Three-tier specs: Phase + Iter + Wave [1:55-74]

Affected here: §5.1's verb matrix promotes `eawf phase spec`, `eawf iter spec`, `eawf wave spec` (two-noun, scope-first per C03 §5.9 [4:764]) as first-class entries; `eawf spec show | lint | graduate` are the three meta verbs.

### V3 — Composable profile bundle with declared precedence [1:76-96]

Affected here: D6 below — profile-conditional verb visibility (e.g. `/spike` only visible under `research` profile; `/design` under `research, engineering`). The visibility rule is enforced at help-rendering time, not at CLI registration time — every registered verb stays callable so scripts don't break across profile changes; profile-hidden verbs print a banner when invoked off-profile.

### V5 — Runtime fallback: reactive switchover on error [1:127-151]

Affected here: `eawf wave switch <wave-id> --to <runtime>` exposes the operator-manual override [1:148-149]. The fallback policy itself (`hybrid|backoff|immediate` per C02 D12 [3:145]) is read-only from the CLI perspective — operator configures it via `eawf config set runtime.fallback.retry_policy <value>`.

### V6 — Cross-platform daemon: per-OS native service + on-demand spawn [1:153-182]

Affected here: §5.1's `eawf daemon` noun-app — `enable | disable | status | restart | logs | version`. `enable`/`disable` write/remove the per-OS service file (systemd `--user` unit / launchd plist / pywin32 service per C02 D2 [3:135] + §5.10 [3:493-650]). `status`/`restart`/`logs`/`version` are runtime-control verbs that go via the daemon's `daemon.*` RPC method set [3:330-336].

### V7 — Telemetry: vendor agent-lens schema, rebuild inside eawf [1:184-224]

Affected here: §5.1's `eawf metrics` noun-app — `show | export | rebuild`. `show` reads the user-scope DuckDB / SQLite store at `<local-path>` [1:191]; `export` emits Prometheus textfile / JSON / CSV; `rebuild` reprocesses the per-repo `event.jsonl` projections + per-runtime session logs [1:194-197].

### V8 — Agent dispatch: hybrid session reuse [1:226-271]

Affected here: `eawf wave dispatch [--session-policy fresh|continue|hybrid]` exposes the manifest-level session-policy override [1:249]. Day-1 default `hybrid` per [1:229-235]; per-profile override resolved by C08 [1:296-300]; per-skill manifest override resolved by C04 [5:298-305].

### C01 D1 — broad single-word URN kinds [2:118]

Affected here: every verb that takes a URN argument accepts the C01 §5.2.2 [2:198-228] catalog of 26 single-word kinds. `eawf spec show <urn>` resolves any of `spec | wave | iter | phase | report | audit | hypothesis | decision | artifact | research | memory`.

### C02 §5.2 — JSON-RPC framing + error-code table [3:230-288]

Affected here: §5.4 (error envelope) extends the daemon error-code table into a CLI-visible map — the CLI translates daemon `-3200X` codes into `eawf.cli.exit_codes` integers per §5.3 (D3).

### C03 §5.9 — spec verb-noun shape [4:764]

Affected here: §5.1 keeps `eawf <scope> <action>` two-noun (e.g. `eawf phase spec init`) over the one-verb-everywhere alternative; aligns with `eawf phase activate` / `eawf iter close` / `eawf wave claim`.

### C04 §5.1 — skill catalog [5:206-224]

Affected here: §5.1 enumerates the 13 skills as first-class verbs under the `skill` noun (`eawf skill run <name>`) per C04 D2.c [5:106-114]. Adds `eawf skill resume <pause-urn>` for D4 needs_user handshake [5:127-134].

## 4. Decision matrix

Operator-confirmed axes seeded by C00 §C05 [1:581-627] + the V1 / V5 / V6 / V7 additional goals [1:594-595]. Each row records the locked recommendation + rationale; this brief promotes from `local-draft` to `accepted` when §8 open questions are operator-confirmed.

| # | Axis | Options considered | Recommendation | Rationale |
|---|---|---|---|---|
| **D1** | Verb-noun arity for scope-actions | (a) one-verb-everywhere (`eawf state phase show`); (b) noun-first scope-action two-noun (`eawf phase show`, `eawf phase spec init`); (c) hybrid | **(b) — noun-first scope-action (current dominant shape)** | Today's surface is already noun-first: `eawf phase open|close|activate|reopen|prepare-close` [21:127], `eawf iter open|close|activate`, `eawf wave plan|claim|close|show|fail|update|graph|next-ready|blocks-rebuild|dispatch|dispatch-batch|review|land|fix-ci`, `eawf audit add|run|integrity|set-verdict|show|list`, etc. C03 §5.9 [4:764] confirms two-noun is preferred for spec verbs. Lifting the entire surface into one-verb-everywhere would break every script that already calls `eawf phase open P20`. There is no `eawf state phase show` today — `eawf state resolve` is the only `state` subverb [27]. Reject (a) and (c); confirm (b). |
| **D2** | Output-format flag set | (a) status-quo (`--json`, `--plain`); (b) +`--md`, `--quiet`, `--verbose`; (c) +`--yaml`, `--csv` for machine consumers | **(b) — keep `--json` + `--plain`; add `--md`, `--quiet`, `--verbose`** | `--md` round-trip is needed for the C04 envelope-to-markdown bridge [5:226-281]; `--quiet` suppresses successful-mutation banners (CI pipelines append-only events); `--verbose` opts into daemon-spawn step + IPC framing logs [3:362-376 D15 [3:148]]. `--yaml`/`--csv` are YAGNI in v0.3-v0.5; no consumer surfaces them — defer to C09 / C10. |
| **D3** | Exit-code taxonomy | (a) compress current 0..9 into the C00 task 0..5 shape; (b) keep 0..9, add 10/11/12 for daemon classes; (c) full restructure with semantic gaps for future codes | **(a) — compress to 0..5 per C00 §C05 [1:587-588]** (operator ratified 2026-05-17, overrode draft reco (b); see §8 Q2) | Operator chose the C00 design-intent surface over backward-compat. Cascade: BREAKING change to `eawf.cli.exit_codes` [23]; single-PR cutover in W02 with `BREAKING:` CHANGELOG entry per Q2b; legacy nine-class distinctions preserved via `ErrorEnvelope.data.kind` string per Q2a; ProtocolMismatch folds into 1 USER_ERROR and RuntimeUnavailable folds into 3 STATE_CONFLICT per Q2c. Downstream CI consumers update during W02 review window. New surface: 0 OK, 1 USER_ERROR, 2 VALIDATION_ERROR, 3 STATE_CONFLICT, 4 DAEMON_UNREACHABLE, 5 INTERNAL_ERROR (§5.3). |
| **D4** | Error envelope shape | (a) current `{error, message, exit_code, exit_name}` [24]; (b) +`suggested_next_step`, `data: dict`, `correlation_id`, `protocol_version` (daemon-mediated only) | **(b) — additive extension** | C00 §C05 goal [1:589] explicitly requires `suggested_next_step`. `data: dict` carries verb-specific context (e.g. `data.held_by_pid` for LockConflict). `correlation_id` is the JSON-RPC `id` for daemon-mediated calls; absent for daemonless. `protocol_version` surfaces on `-32004` per C02 §5.9 [3:470-490]. Backward compatible — new fields are `Optional[...]`; old consumers ignore them. |
| **D5** | Stability tiers | (a) all-stable; (b) stable / experimental / deprecated tri-state; (c) per-verb opaque major/minor versioning | **(b) — explicit tri-state with rotation rules** | Experimental verbs (new daemon/metrics/spec/wave-switch, plus `coauthor`, `pr-review`, `wave-policy`) live max 3 alpha versions per C00 §C05 [1:594]; deprecated verbs live 1 alpha then are removed [1:594]. The tag is a Typer help-panel suffix `[experimental]` / `[deprecated]` so help readers see the lifecycle state. CI fails when an experimental verb crosses its 3-alpha threshold without being promoted to stable or removed. |
| **D6** | Daemon escalation rule | (a) per-verb opt-in; (b) per-verb opt-out; (c) class-wide rule (mutations escalate; reads bypass) | **(c) — class-wide rule + per-verb `--daemonless` opt-out for read-only carve-outs** | V1 [1:26-30] names exactly three read-only bypass classes: CI environments, read-only one-shot CLI calls (`state show`, `wave list`, `validate`), recovery shell. CLI handlers tag themselves `mutating=True` / `read_only=True`; the wrapper around every handler picks the right path. `--daemonless` is per-call override (read-only only); `EAWF_DAEMONLESS=1` is process-scope. Mutating verbs reject the flag with `daemon_required` envelope. |
| **D7** | Daemon control verb set | (a) `start | stop | status` (minimal); (b) `enable | disable | status | restart | logs | version`; (c) add `replay-wal | reload-config | shutdown --drain` | **(b) — task-spec set; (c) added under `--debug` flag** | C00 §C05 [1:595,610] + task brief name the (b) set explicitly. `replay-wal` (C02 §5.6 [3:430-432]) + `reload-config` (C02 §5.3.5 [3:335]) + `shutdown --drain` (C02 §5.5 [3:386]) are debug verbs; they live under `eawf daemon <verb> --debug` so they don't pollute `eawf daemon --help`. `start`/`stop` are aliases for `enable`/`disable` with an explicit foreground-mode option (`eawf daemon start --no-detach` for ad-hoc debugging). |
| **D8** | Metrics CLI surface (V7) | (a) `show | export`; (b) +`rebuild`; (c) +`watch | alert` | **(b) — `show | export | rebuild`** | V7 [1:206-207] names `show` + `export`; `rebuild` is the operator escape hatch when the user-scope DB falls behind / corrupts. `watch`/`alert` are deferred to v0.5+ telemetry surfaces (C09 owns the alerting / TUI dashboard); the CLI surface stays minimal. Default export format priority: `prom` (Prometheus textfile) > `json` (newline-delimited) > `csv`. |
| **D9** | wave-switch invocation surface (V5) | (a) `eawf wave switch <wave-id> --to <runtime>`; (b) reuse `eawf wave dispatch <wave-id> --runtime <runtime>` overload; (c) two verbs, one for ladder-switch + one for one-shot dispatch | **(a) — dedicated `switch` verb** | V5 [1:148-149] names `eawf wave switch` exactly. `dispatch` already carries `--runtime` for first-spawn pick; `switch` carries the V5 reactive-fallback override semantics including idempotency-key reissue [3:147] and the `runtime_switched` event emission with `cause=manual_override`. Two verbs make the audit-trail unambiguous. |
| **D10** | Help model | (a) Typer default + topic flags; (b) +`eawf help <topic>` prose surface; (c) +AI-summarise on miss | **(b) — register a `help` command with hand-authored prose topics** | The `eawf help <topic>` surface is the smallest prose layer the operator needs without leaving the terminal. v0.3 ships six topics: `exit-codes`, `daemon`, `profiles`, `urns`, `migration`, `streaming`. Each topic file lives under `docs/help/<topic>.md` and is rendered through the same envelope chassis. Each topic is ≤80 lines, paginated through `less -R` when stdout is a TTY. AI-summarise on miss is YAGNI — defer to v0.5+. |
| **D11** | Shell completion install | (a) Typer `--install-completion` (current `add_completion=False` [21:32]); (b) dedicated `eawf completion install [bash|zsh|fish]` verb; (c) shell-rc auto-edit on first `eawf` run | **(b) — explicit verb** | Typer's auto-install pollutes shell-rc on first `--help` invocation — surprising. (c) is worse — never auto-edit operator dotfiles. (b) keeps the cost explicit: operator runs `eawf completion install zsh > "$fpath[1]/_eawf"` (zsh) / equivalent for bash / fish. The verb's body is a thin wrapper around `typer.completion.get_completion_inspect_parameters` then writes to stdout. |
| **D12** | Streaming output for long ops | (a) always batch — collect all events then print; (b) `--stream` opt-in (default off); (c) auto-detect TTY + opt-out flag | **(b) — `--stream` opt-in; default off** | CI consumers want deterministic single-block stdout; humans want progress. (b) gives both at the cost of one extra flag. TTY autodetection [(c)] is brittle — `tmux` / `screen` / `nohup` confuse the heuristic. Streaming format: line-delimited JSON envelopes when `--json --stream`; line-delimited human text when `--stream` alone. EOF (`}}\n`) terminates the stream — script consumers parse line-by-line. |
| **D13** | Per-verb `--scope` global flag | (a) keep current state (`--scope` is per-command, not root [26]); (b) hoist `--scope` to root | **(a) — keep per-command** | The root callback [26:8-13] documents the rationale: nothing in the v0.1 surface filters / anchors on `--scope` cross-cutting; subcommands that need it declare it. Hoisting promises behaviour no handler implements. Future cluster-wide scope flag is YAGNI; can be added by C09 when it lands. |
| **D14** | Migration sequencing | (a) big-bang lift in one PR; (b) static-table refactor first (KISS-005 [6:71]) then per-cluster surface additions; (c) per-verb lifts in parallel waves | **(b) — KISS-005 lands first; new surfaces come in C05 implementation waves** | KISS-005 [6:71] is a P2 fix already enqueued. Its acceptance criterion is `uv run eawf --help` + command smoke tests pass — additive, non-breaking. After KISS-005, each new noun-app (`daemon`, `metrics`, spec verbs, `wave switch`) lands as its own wave in the C05-implementation phase. |

## 5. Proposed schema / API / protocol

### 5.1 Full verb-noun matrix

Every CLI command in v0.3 → v0.5 enumerated below. Columns:

- **Verb** — top-level Typer command name (left-most segment after `eawf`).
- **Subverb** — second-level command name (when the verb is a Typer app); `—` for one-shot verbs.
- **Tier** — `stable | experimental | deprecated` per D5.
- **Mut** — `R` (read-only) / `W` (mutating; routes through daemon per D6).
- **Esc** — daemon-escalation rule: `daemon` (always) / `bypass-ok` (read-only carve-out) / `daemon-or-spawn` (auto-spawn if absent) / `n/a` (no daemon dependency).
- **Exit codes raised** — non-zero codes the verb may surface (subset of §5.3).
- **Notes / source / new-in**.

#### 5.1.1 Workspace and project setup

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `init` | — | stable | W | daemon-or-spawn | 1, 2 | Initialise an Eä workspace per [21:236-237]. Q12: in-process bootstrap. |
| `clone-repo` | — | stable | W | daemon-or-spawn | 1, 3 | Clone + register a repo per [21:243]. |
| `workspace` | `init` | stable | W | daemon-or-spawn | 1, 3 | Per [29]. |
| `workspace` | `add-repo` | stable | W | daemon | 1 | |
| `workspace` | `remove-repo` | stable | W | daemon | 1 | |
| `workspace` | `validate` | stable | R | bypass-ok | 1, 2 | |
| `workspace` | `status` | stable | R | bypass-ok | 1 | |
| `workspace` | `registry-list` | stable | R | bypass-ok | 1 | |
| `workspace` | `registry-status` | stable | R | bypass-ok | 1 | |
| `repo` | `init` | stable | W | daemon | 1, 3 | Per [21:247]. |
| `repo` | `link` | stable | W | daemon | 1 | |
| `repo` | `add` | stable | W | daemon | 1 | |
| `repo` | `remove` | stable | W | daemon | 1 | |
| `repo` | `prune` | stable | W | daemon | 1 | |
| `project` | `init` | stable | W | daemon | 1 | Per [21:124] — backed by `Repo` row in C01 §5.3.1 [2:247-264]. |
| `subproject` | `add` | stable | W | daemon | 1 | |
| `subproject` | `switch` | stable | W | daemon | 1 | |

#### 5.1.2 Phase / iter / wave lifecycle

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `phase` | `open` | stable | W | daemon | 1, 2, 3 | Per [21:127]. |
| `phase` | `close` | stable | W | daemon | 1, 2, 3 | |
| `phase` | `activate` | stable | W | daemon | 1, 2, 3 | |
| `phase` | `reopen` | stable | W | daemon | 1, 2, 3 | Per AGENTS rule 20 [11]. |
| `phase` | `prepare-close` | stable | W | daemon | 1, 2 | |
| `iter` | `open` | stable | W | daemon | 1, 2, 3 | |
| `iter` | `close` | stable | W | daemon | 1, 2, 3 | |
| `iter` | `activate` | stable | W | daemon | 1, 2, 3 | |
| `wave` | `plan` | stable | W | daemon | 1, 2 | |
| `wave` | `claim` | stable | W | daemon | 1, 2, 3 | `--out-of-order` per AGENTS worktree discipline [11]. |
| `wave` | `close` | stable | W | daemon | 1, 2, 3 | |
| `wave` | `fail` | stable | W | daemon | 1, 2 | |
| `wave` | `update` | stable | W | daemon | 1, 2 | |
| `wave` | `show` | stable | R | bypass-ok | 1 | |
| `wave` | `graph` | stable | R | bypass-ok | 1 | |
| `wave` | `next-ready` | stable | R | bypass-ok | 1 | |
| `wave` | `blocks-rebuild` | stable | W | daemon | 1, 2 | |
| `wave` | `dispatch` | experimental | W | daemon | 1, 2, 3, 4 | `--session-policy fresh|continue|hybrid` per V8 [1:249]. `--runtime <name>` first-spawn pick. |
| `wave` | `dispatch-batch` | experimental | W | daemon | 1, 2, 3, 4 | |
| `wave` | `switch` | experimental | W | daemon | 1, 3, 4 | **New** — V5 manual override [1:148-149]. `--to <runtime> [--reason <str>]`. |
| `wave` | `fix-ci` | stable | W | daemon | 1 | |
| `wave` | `fix-ci-loop` | experimental | W | daemon | 1 | |
| `wave` | `land` | stable | W | daemon | 1, 3 | |
| `wave` | `land-batch` | experimental | W | daemon | 1, 3 | |
| `wave` | `review` | stable | W | daemon | 1 | |
| `wave budget` | `set` | stable | W | daemon | 1, 2 | |
| `wave budget` | `consume` | stable | W | daemon | 1, 2 | |
| `wave budget` | `show` | stable | R | bypass-ok | 1 | |
| `wave policy` | `set` | experimental | W | daemon | 1, 2 | |
| `wave policy` | `show` | experimental | R | bypass-ok | 1 | |

#### 5.1.3 Roadmap planning

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `roadmap` | `propose` | stable | W | daemon | 1, 2 | Per AGENTS rule 21 [11]. |
| `roadmap` | `revise` | stable | W | daemon | 1, 2 | |
| `roadmap` | `apply` | stable | W | daemon | 1, 2 | |
| `roadmap` | `drop` | stable | W | daemon | 1, 2 | |
| `roadmap` | `show` | stable | R | bypass-ok | 1 | |

#### 5.1.4 Research, hypothesis, decision, audit, incident, artifact, evidence

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `research` | `show` | stable | R | bypass-ok | 1 | Per [21:323]. |
| `draft` | `new` | stable | W | daemon | 1 | |
| `draft` | `validate` | stable | R | bypass-ok | 1, 2 | |
| `goal` | `define` | stable | W | daemon | 1 | |
| `outcome` | `define` | stable | W | daemon | 1 | |
| `outcome` | `set` | stable | W | daemon | 1 | |
| `hypothesis` | `define` | stable | W | daemon | 1 | |
| `hypothesis` | `verdict` | stable | W | daemon | 1, 2 | |
| `hypothesis` | `list` | stable | R | bypass-ok | 1 | |
| `audit` | `add` | stable | W | daemon | 1 | |
| `audit` | `run` | stable | W | daemon | 1, 2, 3 | `--stream` opt-in per D12. |
| `audit` | `integrity` | stable | R | bypass-ok | 1, 3 | |
| `audit` | `set-verdict` | stable | W | daemon | 1, 2 | |
| `audit` | `show` | stable | R | bypass-ok | 1 | |
| `audit` | `list` | stable | R | bypass-ok | 1 | |
| `incident` | `open` | stable | W | daemon | 1 | |
| `incident` | `close` | stable | W | daemon | 1 | |
| `incident` | `view` | stable | R | bypass-ok | 1 | |
| `decision` | `add` | stable | W | daemon | 1 | |
| `decision` | `list` | stable | R | bypass-ok | 1 | |
| `decision` | `graph` | stable | R | bypass-ok | 1 | |
| `artifact` | `add` | stable | W | daemon | 1 | |
| `artifact` | `update` | stable | W | daemon | 1 | |
| `artifact` | `show` | stable | R | bypass-ok | 1 | |
| `artifact` | `validate` | stable | R | bypass-ok | 1, 2 | |
| `artifact` | `verify` | stable | R | bypass-ok | 1, 3 | |
| `backlog` | `add` | stable | W | daemon | 1 | |
| `backlog` | `set-priority` | stable | W | daemon | 1 | |
| `backlog` | `close` | stable | W | daemon | 1 | |

#### 5.1.5 Estimation + actuals

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `estimate` | `set` | stable | W | daemon | 1 | |
| `estimate` | `update` | stable | W | daemon | 1 | |
| `actual` | `start` | stable | W | daemon | 1 | |
| `actual` | `stop` | stable | W | daemon | 1 | |
| `actual` | `recover` | stable | W | daemon | 1 | |
| `impact` | — | stable | R | bypass-ok | 1 | Per [21:347-350]. |

#### 5.1.6 Memory + session

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `memory` | `add` | stable | W | daemon | 1 | |
| `memory` | `promote` | stable | W | daemon | 1, 2 | |
| `memory` | `list` | stable | R | bypass-ok | 1 | |
| `memory` | `compact` | stable | W | daemon | 1 | |
| `memory` | `render-context` | stable | R | bypass-ok | 1 | |
| `memory` | `prune` | stable | W | daemon | 1 | |
| `memory` | `gc` | stable | W | daemon | 1 | |
| `memory` | `tier` | stable | W | daemon | 1 | |
| `memory` | `view` | stable | R | bypass-ok | 1 | |
| `memory` | `stale` | stable | R | bypass-ok | 1 | |
| `session` | `start` | stable | W | daemon | 1 | |
| `session` | `checkpoint` | stable | W | daemon | 1 | |
| `session` | `close` | stable | W | daemon | 1 | |
| `session` | `recover` | stable | W | daemon | 1, 3 | |

#### 5.1.7 State + store + config + validate + status

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `state` | `resolve` | stable | R | bypass-ok | 1 | Per [27]. |
| `state` | `show` | experimental | R | bypass-ok | 1 | **New** — read-only `state.json` view. Daemon `state.read` RPC per C02 §5.3.1 [3:294-302]. |
| `state` | `validate` | experimental | R | bypass-ok | 1, 2 | **New** — daemon `state.validate` RPC. |
| `state` | `digest` | experimental | R | bypass-ok | 1 | **New** — daemon `state.digest` RPC. |
| `store` | `compact` | stable | W | daemon | 1 | |
| `status` | — | stable | R | bypass-ok | 1 | Per [21:174-176]. |
| `config` | `get` | stable | R | bypass-ok | 1 | |
| `config` | `set` | stable | W | daemon | 1, 2 | Layered-config writer per AGENTS rule 17 [11]. |
| `config` | `validate` | stable | R | bypass-ok | 1, 2 | |
| `config` | `menu` | stable | R | bypass-ok | 1 | Interactive menu per [25]. |
| `validate` | — | stable | R | bypass-ok | 0, 2 | Per [21:113]. |
| `version` | — | stable | R | n/a | — | Per [21:89-97]. |

#### 5.1.8 Spec (V2 — new in v0.3)

Per C03 §5.9 [4:739-762] + D1 (two-noun shape).

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `phase spec` | `init` | experimental | W | daemon | 1, 2 | C03 §5.2 [4:216-291]. |
| `phase spec` | `validate` | experimental | R | bypass-ok | 1, 2 | |
| `phase spec` | `render` | experimental | R | bypass-ok | 1 | `--md` (default) / `--json` / `--diff <other>`. |
| `phase spec` | `implements` | experimental | R | bypass-ok | 1 | Lists VerdictCitation rows. |
| `phase spec` | `promote` | experimental | W | daemon | 1, 2 | |
| `iter spec` | `init` | experimental | W | daemon | 1, 2 | |
| `iter spec` | `validate` | experimental | R | bypass-ok | 1, 2 | |
| `iter spec` | `render` | experimental | R | bypass-ok | 1 | |
| `iter spec` | `implements` | experimental | R | bypass-ok | 1 | |
| `iter spec` | `promote` | experimental | W | daemon | 1, 2 | |
| `wave spec` | `init` | experimental | W | daemon | 1, 2 | |
| `wave spec` | `validate` | experimental | R | bypass-ok | 1, 2 | |
| `wave spec` | `render` | experimental | R | bypass-ok | 1 | |
| `wave spec` | `implements` | experimental | R | bypass-ok | 1 | |
| `wave spec` | `promote` | experimental | W | daemon | 1, 2 | |
| `spec` | `show` | experimental | R | bypass-ok | 1 | Meta verb — accepts any spec URN; `--from-git` walks history. |
| `spec` | `lint` | experimental | R | bypass-ok | 1, 2 | Cross-spec consistency per C03 §5.6 [4:546-588]. |
| `spec` | `graduate` | experimental | W | daemon | 1, 2 | `--to {READY,IMPLEMENTED,ARCHIVED}`. |

#### 5.1.9 Skills + runtime + agent

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `skill` | `list` | stable | R | bypass-ok | 1 | |
| `skill` | `render` | stable | R | bypass-ok | 1 | |
| `skill` | `run` | stable | W | daemon | 1, 2, 3, 4 | C04 D2.c [5:106-114]. `--stream` opt-in per D12. |
| `skill` | `resume` | experimental | W | daemon | 1, 2 | **New** — C04 D4 needs_user resume [5:127-134]. Arg: `<pause-urn>`. |
| `flow` | `run` | experimental | W | daemon | 1, 2, 3, 4 | Per [21:303]. `--stream` opt-in. |
| `flow` | `status` | experimental | R | bypass-ok | 1 | |
| `flow` | `abort` | experimental | W | daemon | 1 | |
| `agent-report` | `add` | stable | W | daemon | 1 | AGENTS rule 19 [11]. |
| `agent-report` | `list` | stable | R | bypass-ok | 1 | |
| `agent-report` | `show` | stable | R | bypass-ok | 1 | |
| `operator` | `rollup` | experimental | R | bypass-ok | 1 | |
| `runtime` | `list` | experimental | R | bypass-ok | 1 | **New** — daemon `runtime.list` RPC [3:343]. |
| `runtime` | `set-preference` | experimental | W | daemon | 1 | **New** — daemon `runtime.set_preference` RPC [3:344]. |
| `runtime` | `health` | experimental | R | bypass-ok | 1 | **New** — daemon `runtime.health` RPC [3:345]. |

#### 5.1.10 Plugin + hook + mcp + cc + profile

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `plugin` | `install` | stable | W | daemon | 1 | |
| `plugin` | `update` | stable | W | daemon | 1 | |
| `plugin` | `doctor` | stable | R | bypass-ok | 1, 3 | |
| `plugin` | `package` | stable | W | daemon | 1 | |
| `plugin` | `sync` | experimental | W | daemon | 1 | **New** — V9 [1:289-294]. |
| `hook` | `run` | stable | W | n/a | 1, 3 | Pre-/post-tool hook trampoline. Direct subprocess; bypasses daemon. |
| `mcp` | `add` | stable | W | daemon | 1 | |
| `mcp` | `install` | stable | W | daemon | 1 | |
| `mcp` | `update` | stable | W | daemon | 1 | |
| `mcp` | `remove` | stable | W | daemon | 1 | |
| `mcp` | `list` | stable | R | bypass-ok | 1 | |
| `mcp` | `grant` | stable | W | daemon | 1 | |
| `mcp` | `revoke` | stable | W | daemon | 1 | |
| `cc statusline` | — | experimental | R | bypass-ok | 1 | Claude Code statusline render path. |
| `profile` | `new` | stable | W | daemon | 1 | |
| `profile` | `enable` | stable | W | daemon | 1, 2 | |
| `profile` | `validate` | stable | R | bypass-ok | 1, 2 | |

#### 5.1.11 Ship surfaces — coauthor, pr, release, wiki, sync, doctor, doc, render-output

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `coauthor` | `resolve` | experimental | R | bypass-ok | 1 | |
| `pr` | `render` | stable | R | bypass-ok | 1 | |
| `release` | `changelog` | stable | R | bypass-ok | 1 | |
| `release` | `notes` | stable | R | bypass-ok | 1 | |
| `wiki` | `render` | stable | R | bypass-ok | 1 | |
| `sync` | — | stable | W | daemon | 1 | Per [21:268]. |
| `doctor` | — | stable | R | bypass-ok | 1, 3 | |
| `doc` | `verify` | stable | R | bypass-ok | 1, 2 | |
| `render-output` | — | stable | R | n/a | 1 | Pure format converter; no state read [21:255-262]. |

#### 5.1.12 Worktree

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `worktree` | `create` | stable | W | daemon | 1, 3 | AGENTS rule 11 [11]. |
| `worktree` | `list` | stable | R | bypass-ok | 1 | |
| `worktree` | `merge-back` | stable | W | daemon | 1, 3 | |
| `worktree` | `path-fix` | stable | W | daemon | 1 | |
| `worktree` | `cleanup` | stable | W | daemon | 1 | |

#### 5.1.13 Daemon (V6 — new in v0.3)

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `daemon` | `enable` | experimental | W | n/a | 1 | **New** — writes service file per OS (C02 §5.10 [3:493-650]). |
| `daemon` | `disable` | experimental | W | n/a | 1 | **New** — removes service file; on-demand spawn still works. |
| `daemon` | `status` | experimental | R | bypass-ok | 1, 4 | **New** — daemon `daemon.status` RPC [3:333]. |
| `daemon` | `restart` | experimental | W | daemon | 1, 4 | **New** — drain → shutdown → spawn. |
| `daemon` | `logs` | experimental | R | bypass-ok | 1 | **New** — tail `<local-path>`. `--follow`. |
| `daemon` | `version` | experimental | R | bypass-ok | 1, 4 | **New** — daemon `daemon.ping` RPC [3:332]. |
| `daemon` | `start` | experimental | W | n/a | 1, 4 | Hidden under `--debug` per D7. Force-spawn; `--no-detach` runs foreground. |
| `daemon` | `stop` | experimental | W | daemon | 1, 4 | Hidden under `--debug` per D7. Daemon `daemon.shutdown` RPC [3:334]. `--drain` waits for in-flight. |
| `daemon` | `replay-wal` | experimental | W | daemon | 1, 3, 4 | C02 §5.6 [3:430-432]. Hidden behind `--debug` per D7. |
| `daemon` | `reload-config` | experimental | W | daemon | 1, 4 | C02 §5.3.5 [3:335]. Hidden behind `--debug`. |

#### 5.1.14 Metrics (V7 — new in v0.3)

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `metrics` | `show` | experimental | R | bypass-ok | 1 | **Promoted** from one-shot `eawf metrics` [21:379-388]. `--scope user|repo|phase` `--window 7d|30d|90d`. |
| `metrics` | `export` | experimental | R | bypass-ok | 1 | **New** — `--format prom|json|csv` (default `prom`). |
| `metrics` | `rebuild` | experimental | W | daemon | 1, 3 | **New** — re-projects user-scope DB from per-repo `event.jsonl` + per-runtime session logs. |

#### 5.1.15 TUI + completion + help

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `tui` | — | stable | R | bypass-ok | 1 | Per [21:362-371]. |
| `completion` | `install` | experimental | W | n/a | 1 | **New** — `[bash|zsh|fish]`. Writes completion script to stdout for shell-rc integration per Q6. |
| `completion` | `show` | experimental | R | n/a | 1 | **New** — prints script without writing. |
| `help` | — | experimental | R | n/a | 1 | **New** — `eawf help <topic>` prose surface. Topics: `exit-codes`, `daemon`, `profiles`, `urns`, `migration`, `streaming` per Q5. |

#### 5.1.16 Hidden / internal verbs

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `scope-debug` | — | internal | R | n/a | 0 | Hidden per [21:100-110]; prints resolved GlobalFlags. |

**Total verbs:** 51 top-level verbs (counting noun-apps) + 152 subverbs across the 16 sub-sections above. **New verbs in v0.3:** 11 (daemon, metrics promoted to noun-app, spec meta, state show/validate/digest, runtime, wave switch, skill resume, plugin sync, completion, help).

### 5.2 Output-format flag set

All flags hoisted to the root callback per [21:43-78]. Subcommands inherit through `typer.Context.obj: GlobalFlags` [26].

```python
# src/eawf/cli/flags.py (C05-extended)

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GlobalFlags:
    """Resolved global flags carried via ``typer.Context.obj``.

    Output mode is exactly one of: text (default), json, md, quiet. Verbose
    and plain are independent modifiers. Daemonless is the per-call opt-out
    for read-only carve-outs per V1 [1:26-30].
    """

    # Output mode (mutually exclusive, picked by precedence quiet > json > md > text):
    json_output: bool = False
    md_output: bool = False
    quiet: bool = False
    # Independent modifiers:
    plain_output: bool = False
    verbose: bool = False
    # Daemon path:
    daemonless: bool = False
    no_input: bool = False
    # Streaming:
    stream: bool = False
    # Workspace anchor:
    workspace: Path | None = None
```

**Mode precedence.** `quiet` > `json` > `md` > `text` — when multiple are passed (operator error), the higher-precedence wins; the CLI emits a `WARN flags.mode_collision` envelope on `stderr` and continues. Rationale: deterministic — never error on flag overload, always pick the safest (most-machine-friendly) interpretation.

**Format semantics.**

- `--json` — orjson-serialised envelope with `OPT_INDENT_2 | OPT_SORT_KEYS` [22:37]; byte-stable across runs; the canonical machine-consumer format.
- `--md` — `OutputEnvelope.to_markdown()` (C04 §5.2 [5:226-262]); round-trippable per EV-07 [5:274].
- ~~`--tab` (text, default)~~ → **`--plain` (text, default)** — locked 2026-05-18 per XB18 / E-01: pick `--plain` to avoid `--tab` confusion with the tab character. `typer.echo(text)` with Rich markup (unless explicitly `--plain --no-color` for ANSI-incapable terminals).
- `--quiet` — suppresses the success body; emits envelope only on non-zero exit. Stderr stays open for warnings.
- `--plain` (default + ANSI-strip mode) — disables Rich colour / markup; reserved for terminals that cannot render ANSI sequences [26:38]. Also the canonical default text-output flag name (XB18 lock).
- `--verbose` — surfaces daemon-spawn step + IPC framing + WAL transitions on stderr per D7 + C02 §5.5 [3:362-376].
- `--stream` — line-delimited envelopes (JSON or human text) until daemon emits a terminal `event.push` for the call [3:300]. EOF terminates.

**Combinations.** `--json --stream` = NDJSON; `--md --stream` is rejected (markdown is non-streamable round-trip). `--quiet --verbose` is rejected at flag-parse time (`-32602` equivalent → exit 3).

### 5.3 Exit-code taxonomy

Operator ratified Q2 (round 1) + Q2a/Q2b/Q2c (round 4): **compress 0..9 to 0..5**, fold all current codes plus the three new daemon classes into the C00 §C05 [1:587-588] six-code surface. Single-PR cutover in W02 per Q2b. New mapping below replaces current `eawf.cli.exit_codes` [23] in full.

```python
# src/eawf/cli/exit_codes.py (C05-compressed — replaces current 0..9 surface)

from __future__ import annotations

OK: int = 0
USER_ERROR: int = 1
VALIDATION_ERROR: int = 2
STATE_CONFLICT: int = 3
DAEMON_UNREACHABLE: int = 4
INTERNAL_ERROR: int = 5

_NAMES: dict[int, str] = {
    OK: "OK",
    USER_ERROR: "USER_ERROR",
    VALIDATION_ERROR: "VALIDATION_ERROR",
    STATE_CONFLICT: "STATE_CONFLICT",
    DAEMON_UNREACHABLE: "DAEMON_UNREACHABLE",
    INTERNAL_ERROR: "INTERNAL_ERROR",
}
```

**Legacy code bucket map (Q2a)** — current `eawf.cli.exit_codes` [23] codes 1..9 fold per:

| Legacy code | Legacy name | New code | New name | Rationale |
|---|---|---|---|---|
| 1 | GENERIC_ERROR | 5 | INTERNAL_ERROR | Catchall raised path; not operator-fixable. |
| 2 | NOT_FOUND | 1 | USER_ERROR | Operator passed bad id/path — operator-fixable. |
| 3 | INVALID_INPUT | 1 | USER_ERROR | Bad CLI args — operator-fixable. |
| 4 | VALIDATION_FAILED | 2 | VALIDATION_ERROR | Schema/invariant rejection — direct rename. |
| 5 | LOCK_CONFLICT | 3 | STATE_CONFLICT | Sibling writer holds lock — state-side. |
| 6 | INSTRUMENT_MISSING | 1 | USER_ERROR | Missing external tool — operator-fixable env. |
| 7 | USER_DECLINED | 1 | USER_ERROR | Operator opted out at gate. |
| 8 | INTEGRITY_VIOLATION | 3 | STATE_CONFLICT | Hash mismatch / corrupt store — state-side. |
| 9 | HOOK_BLOCKED | 3 | STATE_CONFLICT | Hook gate rejected current state mutation. |

**New daemon classes (Q2c — no new codes; fold into existing buckets):**

| Daemon class | New code | Rationale |
|---|---|---|
| Daemon process unreachable | 4 | DAEMON_UNREACHABLE | Dedicated bucket per C00 §C05 [1:588]. |
| Protocol version mismatch | 1 | USER_ERROR | Operator must run `uv tool upgrade eawf` — operator-fixable. |
| Runtime ladder exhausted | 3 | STATE_CONFLICT | Configured-runtime fleet failed — config / state-side, not operator-input. |

**Mapping from daemon JSON-RPC error codes** (C02 §5.2.2 [3:266-289]) — rewritten for the 0..5 surface:

| Daemon code | CLI exit | CliError subclass |
|---|---|---|
| -32700 parse error | 5 INTERNAL_ERROR | `InternalError` |
| -32600 invalid request | 1 USER_ERROR | `UserError` |
| -32601 method not found | 1 USER_ERROR | `UserError` (carries upgrade hint per §5.4) |
| -32602 invalid params | 1 USER_ERROR | `UserError` |
| -32603 internal error | 5 INTERNAL_ERROR | `InternalError` |
| -32000 unauthorized | 5 INTERNAL_ERROR | `InternalError` (POSIX UDS auth is OS-enforced; this hit means daemon-side bug) |
| -32001 lock conflict | 3 STATE_CONFLICT | `StateConflict` |
| -32002 validation failed | 2 VALIDATION_ERROR | `ValidationError` |
| -32003 scope mismatch | 1 USER_ERROR | `UserError` |
| -32004 protocol version mismatch | 1 USER_ERROR | `UserError` (with `data.upgrade_command`) |
| -32005 resource exhausted | 3 STATE_CONFLICT | `StateConflict` (daemon-budget hit) |
| -32006 runtime unavailable | 3 STATE_CONFLICT | `StateConflict` |
| -32007 session expired | 5 INTERNAL_ERROR | `InternalError` (advisory only — daemon falls back to fresh; non-fatal) |
| -32008 subscription dropped | 5 INTERNAL_ERROR | `InternalError` |
| -32009 daemon shutting down | 4 DAEMON_UNREACHABLE | `DaemonUnreachable` |
| connection refused / pid stale | 4 DAEMON_UNREACHABLE | `DaemonUnreachable` |

**`CliError` subclass set (compressed):**

```python
# src/eawf/cli/errors.py (C05-compressed taxonomy)

class CliError(Exception):
    exit_code: int = exit_codes.INTERNAL_ERROR


class UserError(CliError):
    """Operator-fixable input / environment problem."""
    exit_code = exit_codes.USER_ERROR


class ValidationError(CliError):
    """Schema or invariant rejection."""
    exit_code = exit_codes.VALIDATION_ERROR


class StateConflict(CliError):
    """State-side conflict — lock, integrity, hook, or runtime ladder."""
    exit_code = exit_codes.STATE_CONFLICT


class DaemonUnreachable(CliError):
    """Daemon down / unresponsive / shutting down."""
    exit_code = exit_codes.DAEMON_UNREACHABLE


class InternalError(CliError):
    """Uncaught raised path — file an issue."""
    exit_code = exit_codes.INTERNAL_ERROR
```

**Disambiguation via `data` field.** Because nine legacy classes fold into five new ones, operator-side distinction (was-it-NotFound vs was-it-InstrumentMissing?) lives in `ErrorEnvelope.data.kind: str` — see §5.4. The `kind` field carries the historically-meaningful subclass-name string (`"NotFound"`, `"InstrumentMissing"`, etc.) for CI scripts that need fine-grained pivots without growing the exit-code surface.

**Backward compatibility — BREAKING.** Codes 0..9 are NOT preserved; W02 is a one-PR cutover (Q2b) with CHANGELOG `BREAKING:` entry. Every test pin in `tests/` updates in the same PR. CI gates external to the repo (downstream scripts, Slack runners) must update during the W02 review window.

### 5.4 Error envelope shape

Extends `eawf.cli.errors.emit_error` [24] with the C05-required fields. Five-class `CliError` taxonomy per Q2a; legacy nine-class distinction preserved via `ErrorEnvelope.data.kind: str`.

```python
# src/eawf/cli/errors.py (C05-compressed; revised 2026-05-18 per E-03 / Codex C05-I010)

from __future__ import annotations
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class ErrorEnvelope(BaseModel):
    """JSON-branch error envelope shape.

    Wired through ``emit_error`` when ``flags.json_output`` is true.
    Plain-text branch renders the same fields as ``error: <message>``
    followed by hint lines.
    """

    model_config = ConfigDict(extra="forbid")

    # E-03 fix 2026-05-18: + schema_version per BOT-03; datetime not str for timestamp;
    # default_factory for mutable dict default (Codex C05-I010 mutable-default audit).

    schema_version: Literal["1.0"] = "1.0"

    error: str                            # CliError subclass __name__
                                          # ("UserError", "ValidationError",
                                          #  "StateConflict", "DaemonUnreachable",
                                          #  "InternalError")
    message: str                          # str(err); user-facing
    exit_code: int                        # eawf.cli.exit_codes value (0..5)
    exit_name: str                        # name_for(exit_code)

    suggested_next_step: str | None = None
    # Operator-facing actionable hint per C00 §C05 goal 4 [1:589]
    # (e.g. "run `eawf daemon start` then retry").

    data: dict[str, Any] = Field(default_factory=dict)   # E-03: default_factory not bare {}
    # Verb-specific structured context. Always JSON-safe.
    # The "kind" key preserves the legacy nine-class subclass distinction
    # (e.g. data.kind="NotFound" / "InstrumentMissing" / "HookBlocked") so CI
    # scripts can pivot on specific failure modes without exit-code growth.
    # Other examples:
    #   StateConflict (kind=LockConflict): {"kind": "LockConflict",
    #                                       "held_by_pid": 1234,
    #                                       "waited_seconds": 5.0}
    #   UserError (kind=ProtocolMismatch): {"kind": "ProtocolMismatch",
    #                                       "cli_version": "0.3.0",
    #                                       "daemon_version": "0.2.5",
    #                                       "upgrade_command": "uv tool upgrade eawf"}
    #   StateConflict (kind=RuntimeUnavailable): {"kind": "RuntimeUnavailable",
    #                                             "tried": ["claude", "codex"],
    #                                             "last_error": "..."}
    #   ValidationError: {"violations": ["INV-001", "INV-014"]}

    correlation_id: str | None = None
    # JSON-RPC request id when the error is daemon-mediated.
    # Absent for daemonless errors. Lets operators join CLI errors to
    # daemon log entries by id.

    protocol_version: str | None = None
    # Daemon protocol version surfaced when data.kind == "ProtocolMismatch".
    # Absent otherwise.

    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=__import__("datetime").timezone.utc))   # E-03 fix 2026-05-18: typed datetime not str; default_factory for UTC-now
```

**Text-branch rendering.**

```
error: <message>
hint: <suggested_next_step>
exit_code: <exit_code> (<exit_name>)
kind: <data.kind>                   # omitted when absent
data: <data dict pretty-print>      # omitted when empty (after kind extraction)
correlation_id: <correlation_id>    # omitted when None
```

**Subclass-to-hint mapping** (each `CliError` subclass picks the default hint; the wrapper substitutes a finer-grained hint when `data.kind` is set):

| Subclass | Default `suggested_next_step` |
|---|---|
| `UserError` | `"run \`eawf <verb> --help\` for option shapes; check ids and env"` |
| `ValidationError` | `"run \`eawf validate\` to inspect schema errors"` |
| `StateConflict` | `"another writer / hook / runtime conflict; run \`eawf doctor\` for diagnosis"` |
| `DaemonUnreachable` | `"run \`eawf daemon start\` then retry; pass --daemonless for read-only verbs"` |
| `InternalError` | `"file an issue with the error envelope; include \`eawf daemon logs --lines 200\`"` |

**Per-`data.kind` hint refinement** — preserves legacy specificity inside the five buckets:

| `data.kind` | Parent class | Hint |
|---|---|---|
| `NotFound` | `UserError` | `"check the scope id or run \`eawf state resolve\` to see resolved paths"` |
| `InvalidInput` | `UserError` | `"run \`eawf <verb> --help\` to see option shapes"` |
| `InstrumentMissing` | `UserError` | `"install the missing tool then retry; run \`eawf doctor\` for inventory"` |
| `UserDeclined` | `UserError` | `"re-run without --no-input to interact, or pass --yes when supported"` |
| `ProtocolMismatch` | `UserError` | `"upgrade with \`uv tool upgrade eawf\` then retry"` |
| `LockConflict` | `StateConflict` | `"another writer holds the lock; retry in a moment or run \`eawf doctor\`"` |
| `IntegrityViolation` | `StateConflict` | `"run \`eawf doctor --repair\` to inspect the integrity violation"` |
| `HookBlocked` | `StateConflict` | `"the hook printed its reason above; fix the underlying issue and retry"` |
| `RuntimeUnavailable` | `StateConflict` | `"check runtime preference: \`eawf runtime list\` and \`eawf config get runtime.preference\`"` |

**Q9 — `--md` scope.** `--md` only meaningful on C04 envelope-emitting verbs (`skill run`, `audit run`, `roadmap propose`, etc.). Non-envelope verbs (`status`, `version`, `state resolve`, `daemon logs`, etc.) ignore the flag and emit plain text. The CLI wrapper sets `flags.md_output=False` silently when the active handler does not declare envelope-emitting capability — no operator-visible error.

### 5.5 Daemon escalation rules table

Per D6 the rule is class-wide (mutations always escalate; reads MAY bypass). Per-verb deviations enumerated below — every deviation cites V1 [1:26-30] as the carve-out.

| Class | Pattern | Daemon path | Carve-outs |
|---|---|---|---|
| Read-only (R) | `state show`, `wave show`, `wave list`, `audit show`, `decision list`, `validate`, `metrics show`, `daemon status`, `daemon logs` | bypass when `--daemonless` or `EAWF_DAEMONLESS=1`; otherwise daemon `state.read` for cache freshness | V1 carve-out 2 [1:28] |
| Mutating (W) | `phase open`, `wave claim`, `iter close`, `roadmap apply`, `state mutate`, all spec promotions, all config set, all memory mutations | daemon `state.mutate` always; refuses `--daemonless` with `daemon_required` envelope; auto-spawns daemon if absent | V1 [1:24-26] |
| Streaming (W with `--stream`) | `wave dispatch`, `flow run`, `audit run`, `skill run`, agent dispatch | daemon `state.subscribe` push frames after the mutation; subscription disconnects on EOF | V1 [1:35-38] |
| Service-registration (W) | `daemon enable`, `daemon disable` | direct file IO into systemd / launchd / pywin32 paths per C02 §5.10 [3:493-650]; never via daemon | new in V6 [1:153-182] |
| Daemon-control (W) | `daemon start`, `daemon stop`, `daemon restart` | direct spawn (start) or daemon `daemon.shutdown` RPC (stop/restart) | new in V6 |
| Hook trampoline (W) | `hook run` | direct subprocess; bypasses daemon entirely | pre-/post-tool hooks fire inside runtime adapter; daemon path would add hop |
| Pure converter (n/a) | `render-output`, `completion install`, `version`, `help` | no state touched | — |
| Recovery shell (R) | any read-only verb when daemon broken | `EAWF_DAEMONLESS=1` + `EAWF_RUNTIME_DIR=<path>` overrides per C02 §5.5 [3:362-366] | V1 carve-out 3 [1:30] |

**Auto-spawn flow** (every mutating verb when daemon is absent):

```
1. CLI parses args; resolves GlobalFlags.
2. Library wrapper checks for daemon liveness via PID file at $XDG_RUNTIME_DIR/eawfd/eawfd.pid
   (Linux) or <local-path> (macOS / Windows fallback).
3. If absent: spawn daemon detached (double-fork POSIX / CreateProcess DETACHED Windows).
4. Poll socket connection for up to 5 s; on connect, issue JSON-RPC.
5. On --verbose, CLI prints `verbose: spawning eawfd at <path>...` to stderr.
6. On --no-input + spawn-failed, CLI exits 4 DAEMON_UNREACHABLE.
7. On --json + spawn-failed, CLI emits ErrorEnvelope with exit_code=4.
```

**`--daemonless` semantics on mutating verbs.** The wrapper rejects the flag with:

```
error: --daemonless rejected: <verb> is a mutating verb (requires daemon-mediated transactions per V1)
hint: drop --daemonless and retry; daemon auto-spawns on first call
exit_code: 1 (USER_ERROR)
kind: InvalidInput
```

### 5.6 Help model

**`eawf --help`** — registry-ordered panels per [25] grouping verbs by domain tab:

```
Usage: eawf [OPTIONS] COMMAND [ARGS]...

Eä Workflow — agent-driven development framework.

╭─ audit ────────────────────────────────────────────────╮
│ audit        Audit records (add/run/show/list/...)     │
│ doc          Documentation drift verification          │
│ doctor       Environment + state diagnostics           │
│ validate     Validate state.json against the schema    │
╰────────────────────────────────────────────────────────╯

╭─ estimation ───────────────────────────────────────────╮
│ actual       Time-tracking actuals                     │
│ estimate     Per-wave EU estimates                     │
│ impact       Decision → wave file-glob impact graph    │
│ metrics      Workflow metrics (show/export/rebuild)    │
╰────────────────────────────────────────────────────────╯

[... 6 more panels ...]

╭─ Options ──────────────────────────────────────────────╮
│ --json, --md, --quiet, --plain, --verbose,             │
│ --stream, --daemonless, --no-input, -w/--workspace     │
│ --version                                              │
╰────────────────────────────────────────────────────────╯
```

**`eawf <verb> --help`** — Typer's default per-noun-app rendering. Adds a `Stability:` footer line per D5:

```
Usage: eawf wave [OPTIONS] COMMAND [ARGS]...

Wave lifecycle and dispatch.

╭─ Commands ─────────────────────────────────────────────╮
│ plan, claim, close, show, ...                          │
│ switch                                       [experimental]
│ dispatch                                     [experimental]
╰────────────────────────────────────────────────────────╯

Stability: stable (most verbs); experimental for switch/dispatch.
See `eawf help streaming` for --stream usage.
```

**`eawf help <topic>`** — six topics ship with v0.3:

| Topic | File | Purpose |
|---|---|---|
| `exit-codes` | `docs/help/exit-codes.md` | Full code table from §5.3 + repair commands |
| `daemon` | `docs/help/daemon.md` | Daemon lifecycle, `enable`/`disable`/`start`/`stop`, log path, troubleshooting |
| `profiles` | `docs/help/profiles.md` | V3 composable bundles + precedence + conflict resolution |
| `urns` | `docs/help/urns.md` | C01 URN grammar + kind catalog |
| `migration` | `docs/help/migration.md` | Per-cluster migration steps (state-version bumps, plugin sync) |
| `streaming` | `docs/help/streaming.md` | `--stream` flag, NDJSON shape, EOF semantics |

Each topic ≤80 lines. Renderer pages through `less -R` when stdout is TTY; non-TTY emits flat markdown. `eawf help` (no arg) lists topics.

### 5.7 Shell completion

**Generator.** Typer ships `typer.completion.get_completion_inspect_parameters` (current `add_completion=False` [21:32] turned the auto-install off; the generator itself still ships). `eawf completion show <shell>` renders the script to stdout; `eawf completion install <shell>` writes to the canonical path:

- bash: `$XDG_DATA_HOME/bash-completion/completions/eawf` (or `/usr/local/etc/bash_completion.d/eawf` on macOS-brew)
- zsh: prepends `$fpath[1]/_eawf`
- fish: `$XDG_DATA_HOME/fish/completions/eawf.fish`

Install verb is opt-in only — never modifies shell-rc files. Operator copies the script to their shell's completion path or sources it from their rc manually. The install verb is best-effort: on permission failure, it exits 1 USER_ERROR with `data.kind="InvalidInput"` and the suggested operator-side command.

**Dynamic completion.** Subcommand names complete via Typer's static introspection. Scope-id args (`<phase-id>`, `<wave-id>`, `<urn>`) complete via daemon `state.read` RPC when shell completion runs (best-effort; daemon-down completion falls back to static prefix-match against on-disk `state.json` cache).

### 5.8 Streaming output for long ops

Verbs that emit `--stream` output: `wave dispatch`, `wave dispatch-batch`, `flow run`, `audit run`, `skill run`, `metrics show --watch`, `daemon logs --follow`.

**NDJSON shape** (`--json --stream`):

```
{"type":"start","scope_id":"<urn>","started_at":"2026-05-16T12:00:00Z","correlation_id":"..."}
{"type":"event","kind":"wave_claimed","payload":{...},"timestamp":"..."}
{"type":"event","kind":"dispatch_log","line":"...","timestamp":"..."}
{"type":"event","kind":"wave_closed","payload":{...},"timestamp":"..."}
{"type":"end","status":"ok","finished_at":"...","correlation_id":"..."}
```

**Human shape** (`--stream` alone):

```
[12:00:00] starting wave dispatch for P20-I03-W01...
[12:00:01] runtime: claude-code (session: s-abc123)
[12:00:01]   dispatch log: ... (truncated; pass --verbose for full)
[12:00:30] wave closed: ok
```

**EOF semantics.** Final line is the `end` envelope (NDJSON) or a single `^$` marker line (human). Subscribers that need an explicit terminator can detect end-of-stream by parsing the `end` line; without `--stream`, the verb prints a single envelope at the end and exits.

**Backpressure.** CLI consumes pushes as fast as it can write to stdout. If stdout is blocked (pipe to slow consumer), the daemon's per-subscriber queue overflows per C02 D7 [3:140-144]; the subscriber is disconnected with `-32008 subscription_dropped`; CLI exits 1 with `subscription_dropped` envelope.

### 5.9 Stability tiers

Per D5. Tier markers (Typer help suffix `[experimental]` / `[deprecated]`) live in the registry and surface in `eawf <verb> --help`:

```python
# src/eawf/cli/stability.py (new)

from __future__ import annotations
from typing import Literal

Tier = Literal["stable", "experimental", "deprecated"]


class StabilityRegistry:
    """Per-verb stability tier with version-since-introduced tracking."""

    _MAP: dict[str, tuple[Tier, str]] = {
        # verb name -> (tier, since_version)
        "wave switch":   ("experimental", "0.3.0"),
        "wave dispatch": ("experimental", "0.3.0"),
        "wave dispatch-batch": ("experimental", "0.3.0"),
        "wave fix-ci-loop":    ("experimental", "0.2.0"),
        "wave land-batch":     ("experimental", "0.2.0"),
        "wave policy set":     ("experimental", "0.2.0"),
        "wave policy show":    ("experimental", "0.2.0"),
        "phase spec init":     ("experimental", "0.3.0"),
        "phase spec validate": ("experimental", "0.3.0"),
        "phase spec render":   ("experimental", "0.3.0"),
        "phase spec implements": ("experimental", "0.3.0"),
        "phase spec promote":  ("experimental", "0.3.0"),
        "iter spec init":      ("experimental", "0.3.0"),
        # ... (analogous for iter / wave spec)
        "spec show":           ("experimental", "0.3.0"),
        "spec lint":           ("experimental", "0.3.0"),
        "spec graduate":       ("experimental", "0.3.0"),
        "daemon enable":       ("experimental", "0.3.0"),
        # ... (analogous for all daemon verbs)
        "metrics show":        ("experimental", "0.3.0"),
        "metrics export":      ("experimental", "0.3.0"),
        "metrics rebuild":     ("experimental", "0.3.0"),
        "state show":          ("experimental", "0.3.0"),
        "state validate":      ("experimental", "0.3.0"),
        "state digest":        ("experimental", "0.3.0"),
        "runtime list":        ("experimental", "0.3.0"),
        "runtime set-preference": ("experimental", "0.3.0"),
        "runtime health":      ("experimental", "0.3.0"),
        "skill resume":        ("experimental", "0.3.0"),
        "plugin sync":         ("experimental", "0.3.0"),
        "completion install":  ("experimental", "0.3.0"),
        "completion show":     ("experimental", "0.3.0"),
        "help":                ("experimental", "0.3.0"),
        "coauthor resolve":    ("experimental", "0.2.5"),
        "operator rollup":     ("experimental", "0.2.0"),
        "cc statusline":       ("experimental", "0.2.0"),
        "flow run":            ("experimental", "0.2.0"),
        "flow status":         ("experimental", "0.2.0"),
        "flow abort":          ("experimental", "0.2.0"),
    }

    @classmethod
    def tier(cls, verb: str) -> Tier:
        return cls._MAP.get(verb, ("stable", "0.1.0"))[0]

    @classmethod
    def since(cls, verb: str) -> str:
        return cls._MAP.get(verb, ("stable", "0.1.0"))[1]
```

**Tier transitions.**

```
experimental ─── (3-alpha budget elapsed; no removal scheduled) ──► stable
experimental ─── (residual use too low; flagged for removal) ─────► deprecated
deprecated   ─── (1 alpha after deprecation announce) ─────────────► removed
```

**CI lint.** A new check `eawf-cli-stability-budget` walks the registry, compares each verb's `since` to the current `__version__`, and fails when an experimental verb has been in the registry for more than 3 alpha versions without being promoted or removed. Lives under `tests/lint/test_stability_budget.py`.

### 5.10 Per-verb signature samples (illustrative)

A handful of new / changed signatures from §5.1, with full Typer-style parameters. Authoring contract for the wave: every signature carries a docstring with `Args:` + `Raises:` blocks per AGENTS rule 17 [11].

```python
# src/eawf/cli/commands/wave_switch.py (new)

@wave_app.command("switch")
def wave_switch_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave id, e.g. P20-I03-W01")],
    to: Annotated[str, typer.Option("--to", help="Target runtime id (claude-code, codex, ...).")],
    reason: Annotated[str | None, typer.Option("--reason", help="Operator-visible reason; recorded on the runtime_switched event.")] = None,
) -> None:
    """Manual runtime-switch for a wave per V5 [1:148-149].

    Daemon-mediated; cannot be issued daemonless.

    Args:
        wave_id: Wave id; resolved against state.waves.
        to: Target runtime id; must appear in state.runtimes.
        reason: Optional human-readable reason for the switch.

    Raises:
        UserError (1, data.kind=NotFound): wave or runtime id not in state.
        DaemonUnreachable (4): daemon not responding.
        StateConflict (3, data.kind=RuntimeUnavailable): every runtime in
            the preference ladder already failed.
    """
    flags: GlobalFlags = ctx.obj
    ...  # daemon RPC: wave.switch
```

```python
# src/eawf/cli/commands/daemon.py (new)

daemon_app = typer.Typer(
    name="daemon",
    help="Daemon lifecycle and observability (V6 [1:153-182]).",
    no_args_is_help=True,
    add_completion=False,
)


@daemon_app.command("enable")
def daemon_enable(
    ctx: typer.Context,
    auto_start_on_login: Annotated[bool, typer.Option("--auto-start", help="Register for on-login auto-start.")] = True,
) -> None:
    """Register the eawfd service file for the current OS.

    Linux: writes <local-path>
    macOS: writes <local-path>
    Windows: registers via pywin32 win32serviceutil.

    Args:
        auto_start_on_login: When True, also enables on-login auto-start.

    Raises:
        UserError (1, data.kind=InstrumentMissing): systemctl/launchctl/
            pywin32 not available.
        UserError (1, data.kind=InvalidInput): service file already exists
            and operator did not pass --force.
    """
    ...


@daemon_app.command("status")
def daemon_status(ctx: typer.Context) -> None:
    """Print daemon status (pid, version, protocol_version, idle_for)."""
    ...


@daemon_app.command("logs")
def daemon_logs(
    ctx: typer.Context,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Stream new log lines.")] = False,
    lines: Annotated[int, typer.Option("--lines", "-n", help="Tail N lines.")] = 100,
) -> None:
    """Tail <local-path>
    ...
```

```python
# src/eawf/cli/commands/metrics.py (extended — promoted to noun-app)

metrics_app = typer.Typer(
    name="metrics",
    help="Workflow + token telemetry (V7 [1:184-224]).",
    no_args_is_help=True,
    add_completion=False,
)


@metrics_app.command("show")
def metrics_show(
    ctx: typer.Context,
    scope: Annotated[str, typer.Option("--scope", help="user | repo | phase")] = "repo",
    window: Annotated[str, typer.Option("--window", help="7d | 30d | 90d")] = "7d",
) -> None:
    """Print rolling metrics for the requested scope + window."""
    ...


@metrics_app.command("export")
def metrics_export(
    ctx: typer.Context,
    fmt: Annotated[str, typer.Option("--format", help="prom | json | csv")] = "prom",
) -> None:
    """Export metrics to stdout in the chosen format."""
    ...


@metrics_app.command("rebuild")
def metrics_rebuild(ctx: typer.Context) -> None:
    """Rebuild user-scope DuckDB / SQLite from event.jsonl + session logs."""
    ...
```

### 5.11 Static registration table (KISS-005)

Replace [21:114-388]'s manual import + `app.add_typer` ladder with a single declarative table:

```python
# src/eawf/cli/app.py (post-KISS-005 sketch)

from __future__ import annotations

import typer
from eawf import __version__
from eawf.cli.flags import GlobalFlags
from eawf.cli.help_panels import RegistryOrderedTyperGroup, panel_for

app = typer.Typer(
    name="eawf",
    help="Eä Workflow — agent-driven development framework.",
    no_args_is_help=False,
    add_completion=False,
    cls=RegistryOrderedTyperGroup,
)


# (verb name, panel, module path, attr name, kind)
# kind: 'app' for Typer noun-apps; 'cmd' for one-shot commands.
_VERB_REGISTRY: tuple[tuple[str, str, str, str, str], ...] = (
    # workspace / project setup
    ("workspace",     "vcs",         "eawf.cli.commands.workspace",       "workspace_app",       "app"),
    ("repo",          "vcs",         "eawf.cli.commands.repo",            "repo_app",            "app"),
    ("project",       "planning",    "eawf.cli.commands.lifecycle",       "project_app",         "app"),
    ("subproject",    "planning",    "eawf.cli.commands.lifecycle",       "subproject_app",      "app"),
    ("init",          "vcs",         "eawf.cli.commands.init",            "init_cmd",            "cmd"),
    ("clone-repo",    "vcs",         "eawf.cli.commands.clone_repo",      "clone_repo_cmd",      "cmd"),
    # phase / iter / wave lifecycle
    ("phase",         "planning",    "eawf.cli.commands.lifecycle",       "phase_app",           "app"),
    ("iter",          "planning",    "eawf.cli.commands.lifecycle",       "iter_app",            "app"),
    ("wave",          "planning",    "eawf.cli.commands.lifecycle",       "wave_app",            "app"),
    # roadmap
    ("roadmap",       "planning",    "eawf.cli.commands.roadmap",         "roadmap_app",         "app"),
    # research / draft / goal / outcome / hypothesis / decision / audit / incident / artifact / backlog
    ("research",      "planning",    "eawf.cli.commands.research",        "research_app",        "app"),
    ("draft",         "planning",    "eawf.cli.commands.draft",           "draft_app",           "app"),
    ("goal",          "planning",    "eawf.cli.commands.evidence",        "goal_app",            "app"),
    ("outcome",       "planning",    "eawf.cli.commands.evidence",        "outcome_app",         "app"),
    ("hypothesis",    "planning",    "eawf.cli.commands.evidence",        "hypothesis_app",      "app"),
    ("decision",      "planning",    "eawf.cli.commands.evidence",        "decision_app",        "app"),
    ("audit",         "audit",       "eawf.cli.commands.evidence",        "audit_app",           "app"),
    ("incident",      "planning",    "eawf.cli.commands.evidence",        "incident_app",        "app"),
    ("artifact",      "planning",    "eawf.cli.commands.evidence",        "artifact_app",        "app"),
    ("backlog",       "planning",    "eawf.cli.commands.evidence",        "backlog_app",         "app"),
    # estimation / actuals / impact
    ("estimate",      "estimation",  "eawf.cli.commands.estimation",      "estimate_app",        "app"),
    ("actual",        "estimation",  "eawf.cli.commands.estimation",      "actual_app",          "app"),
    ("impact",        "estimation",  "eawf.cli.commands.impact",          "impact_cmd",          "cmd"),
    ("metrics",       "estimation",  "eawf.cli.commands.metrics",         "metrics_app",         "app"),  # C05: promoted
    # memory / session
    ("memory",        "planning",    "eawf.cli.commands.memory",          "memory_app",          "app"),
    ("session",       "planning",    "eawf.cli.commands.session",         "session_app",         "app"),
    # state / store / config / validate / status / version
    ("state",         "vcs",         "eawf.cli.commands.state",           "state_app",           "app"),
    ("store",         "vcs",         "eawf.cli.commands.store",           "store_app",           "app"),
    ("status",        "ui",          "eawf.cli.commands.status",          "status_cmd",          "cmd"),
    ("config",        "runtime",     "eawf.cli.commands.config",          "config_app",          "app"),
    ("validate",      "audit",       "eawf.cli.commands.validate",        "validate_cmd",        "cmd"),
    # skill / agent-report / operator / flow
    ("skill",         "runtime",     "eawf.cli.commands.skill",           "skill_app",           "app"),
    ("agent-report",  "planning",    "eawf.cli.commands.agent_report",    "agent_report_app",    "app"),
    ("operator",      "planning",    "eawf.cli.commands.agent_report",    "operator_app",        "app"),
    ("flow",          "worktrees",   "eawf.cli.commands.flow",            "flow_app",            "app"),
    # plugin / hook / mcp / cc / profile
    ("plugin",        "runtime",     "eawf.cli.commands.plugin",          "plugin_app",          "app"),
    ("hook",          "runtime",     "eawf.cli.commands.hook",            "hook_app",            "app"),
    ("mcp",           "runtime",     "eawf.cli.commands.mcp",             "mcp_app",             "app"),
    ("cc",            "runtime",     "eawf.cli.commands.cc",              "cc_app",              "app"),
    ("profile",       "runtime",     "eawf.cli.commands.profile",         "profile_app",         "app"),
    # ship surfaces
    ("coauthor",      "vcs",         "eawf.cli.commands.coauthor",        "coauthor_app",        "app"),
    ("pr",            "ship",        "eawf.cli.commands.pr",              "pr_app",              "app"),
    ("release",       "ship",        "eawf.cli.commands.release",         "release_app",         "app"),
    ("wiki",          "ship",        "eawf.cli.commands.wiki",            "wiki_app",            "app"),
    ("sync",          "ship",        "eawf.cli.commands.sync",            "sync_cmd",            "cmd"),
    ("doctor",        "audit",       "eawf.cli.commands.doctor",          "doctor_app",          "app"),
    ("doc",           "audit",       "eawf.cli.commands.doc",             "doc_app",             "app"),
    ("render-output", "ui",          "eawf.cli.commands.render_output",   "render_output_cmd",   "cmd"),
    # worktree
    ("worktree",      "worktrees",   "eawf.cli.commands.worktree",        "worktree_app",        "app"),
    # tui / completion / help
    ("tui",           "ui",          "eawf.cli.commands.tui",             "tui_cmd",             "cmd"),
    ("completion",    "ui",          "eawf.cli.commands.completion",      "completion_app",      "app"),  # C05: new
    ("help",          "ui",          "eawf.cli.commands.help_topic",      "help_cmd",            "cmd"),  # C05: new
    # daemon (V6 — new in v0.3)
    ("daemon",        "runtime",     "eawf.cli.commands.daemon",          "daemon_app",          "app"),  # C05: new
    # spec meta + per-scope (V2 — new in v0.3)
    ("spec",          "planning",    "eawf.cli.commands.spec",            "spec_app",            "app"),  # C05: new
    # runtime (V5 surface — new in v0.3)
    ("runtime",       "runtime",     "eawf.cli.commands.runtime",         "runtime_app",         "app"),  # C05: new
    # internal / hidden
    ("version",       "ui",          "eawf.cli.commands.version",         "version_cmd",         "cmd"),
)


def _register_all() -> None:
    """Walk the registry; register every verb on the root app."""
    import importlib

    for name, panel, module_path, attr_name, kind in _VERB_REGISTRY:
        mod = importlib.import_module(module_path)
        target = getattr(mod, attr_name)
        if kind == "app":
            app.add_typer(target, name=name, rich_help_panel=panel_for(name) or panel)
        elif kind == "cmd":
            app.command(name=name, rich_help_panel=panel_for(name) or panel)(target)


_register_all()
```

Phase-spec / iter-spec / wave-spec subverbs are attached to `phase_app` / `iter_app` / `wave_app` from within `eawf.cli.commands.spec` (the typical pattern matches `eawf.cli.commands.wave_ci` and `eawf.cli.commands.pr_review` which attach to `wave_app` on import).

## 6. Failure modes + named edge cases

| ID | Failure mode | Behaviour |
|---|---|---|
| **F1** | Daemon process crash mid-mutation | CLI sees `connection reset` → exits 4 DAEMON_UNREACHABLE; suggested next step: `eawf daemon status` then `eawf daemon start`. Daemon restart replays WAL per C02 §5.6 [3:390-432]. |
| **F2** | Protocol version mismatch | CLI exits 1 USER_ERROR with `data.kind="ProtocolMismatch"` + `data.cli_version` + `data.daemon_version`; hint: `uv tool upgrade eawf`. |
| **F3** | `--daemonless` passed on mutating verb | CLI exits 1 USER_ERROR with `data.kind="InvalidInput"`, `error: --daemonless rejected: <verb> is mutating`. |
| **F4** | Read-only verb in CI (no daemon) | Auto-detects via `EAWF_DAEMONLESS=1` or `CI=true`; bypasses daemon; reads directly. |
| **F5** | Streaming verb output piped to slow consumer | Daemon disconnects subscription with `-32008`; CLI exits 5 INTERNAL_ERROR with `data.kind="SubscriptionDropped"` envelope; partial output already on stdout. |
| **F6** | `--json --stream` consumer parses partial line | NDJSON spec — each line is a complete JSON object; partial lines indicate consumer-side parser bug or process kill. |
| **F7** | `--md --stream` requested | CLI exits 1 USER_ERROR with `data.kind="InvalidInput"`, `error: --md is not streamable; use --json --stream`. |
| **F8** | `eawf` bare invocation off-TTY without daemon | TUI fallback path [21:79-86] emits text status; exits per `run_tui` rc. Daemon-unreachable adds a banner. |
| **F9** | `eawf completion install` on read-only `$fpath[1]` | Writes to stdout instead; emits hint with the explicit `mv` command for the operator. |
| **F10** | Two verbs with the same name registered (one stable, one experimental) | Registration asserts uniqueness at module load; the second registration raises `AssertionError`. |
| **F11** | Experimental verb past 3-alpha budget | `tests/lint/test_stability_budget.py` fails in CI; verb must promote to stable, deprecate, or remove. Soft-fail first 2 weeks per Q10. |
| **F12** | Verb missing from `COMMAND_PANELS` [25] | `test_cli_help_groups` fails (per [25:55-58] docstring). |
| **F13** | `eawf help <unknown-topic>` | Exits 1 USER_ERROR with `data.kind="NotFound"`; lists registered topics. |
| **F14** | `eawf daemon enable` on Windows without pywin32 | Exits 1 USER_ERROR with `data.kind="InstrumentMissing"`, `data.missing=["pywin32"]`; hint includes `pip install pywin32` and the NSSM fallback per C02 D2 [3:135]. |
| **F15** | `eawf wave switch` to a runtime not in `state.runtimes` | Exits 1 USER_ERROR with `data.kind="NotFound"`, `data.known_runtimes=[...]`. |
| **F16** | `eawf metrics rebuild` when `<local-path>` is locked | Exits 3 STATE_CONFLICT with `data.kind="LockConflict"`; hint: stop other rebuild / metrics processes. |
| **F17** | Mutating verb reaches daemon but daemon's state.json is stale (mtime newer than digest) | Daemon detects in `state.read` pre-flight; replies `-32002 validation_failed` with `data.reason="state_digest_drift"`; CLI exits 2 VALIDATION_ERROR; hint: `eawf doctor` then retry. |

### Named edge cases

- **`eawf <verb> --json --stream` to non-TTY pipe.** Subscribes; emits NDJSON; final `end` envelope. Exit code echoes the `status` field — `ok` → 0; `failed` → 5; daemon disconnect → 4.
- **`eawf wave dispatch` on a wave whose deps are not closed.** Library validator rejects with `ValidationError` *before* daemon call — exits 2 VALIDATION_ERROR with `data.violations=["dep_not_closed"]`; hint: `eawf wave next-ready`.
- **`eawf phase spec validate <P##> --strict` on UI-scope wave missing mockup.** Per C03 §5.4 [4:412-434] + G5 [4:57] exits 2 VALIDATION_ERROR with `data.violations=["WSV-04"]`.
- **`eawf state mutate` (Q8 hidden).** Not exposed as a CLI verb per Q8 — every mutation routes through its domain verb. Raw RPC clients (non-CLI consumers) using `state.mutate` directly via daemon socket: forbidden — daemon rejects with `-32602 invalid_params` unless the caller's principal is a registered CLI process. Hidden `--debug` escape hatch deferred until v0.5+ (operator chose option (a) — hide entirely).
- **`eawf daemon stop --drain` while a long-running `wave dispatch` is in flight.** Daemon drains for up to 30 s, then `subscription_dropped` to the dispatch subscriber, then exits. The CLI on the operator side exits 0 if the wave finished; 4 DAEMON_UNREACHABLE if drain timed out.
- **`eawf daemon enable` then OS reboot.** Auto-start path picks up the service file; daemon spawns at logon; first CLI call connects without spawn step. `--verbose` shows `connected to running daemon at <socket>` rather than `spawning`.
- **`EAWF_DAEMONLESS=1 eawf wave claim`.** Rejected — mutating verb. Exits 1 USER_ERROR with the same envelope as F3.
- **`eawf --version` with daemon down.** Exits 0; never touches daemon (pure `__version__` print per [21:89-97]).
- **`eawf help` (no arg).** Lists the six topics + hint to invoke `eawf help <topic>`.
- **`--json` on `eawf status`.** Returns the structured pointer / blocker / git-head dict; safe in CI.

## 7. Migration plan

### 7.1 Migration scope

Three concurrent change-sets:

1. **KISS-005 static registration table** [6:71] — non-breaking refactor of [21:114-388] into the table shape in §5.11. Lands first; gates all subsequent surface additions.
2. **Exit-code + error-envelope extensions (§5.3, §5.4)** — additive; updates `eawf.cli.exit_codes` + `eawf.cli.errors` + adds the three new `CliError` subclasses + extends `emit_error` to populate the new fields. Lands second; un-blocks the daemon escalation wave because daemon-mediated calls need the new codes.
3. **New noun-apps (`daemon`, `metrics` (promoted), `spec`, `runtime`, `completion`, `help`) + new verbs (`wave switch`, `skill resume`, `state show`/`validate`/`digest`, `plugin sync`, daemon debug verbs)** — additive; each lands as its own wave in the C05 implementation phase. Order:
   - W01: KISS-005 static table
   - W02: exit-code + error-envelope extensions
   - W03: daemon noun-app + daemon RPC client
   - W04: state read-only verbs (`state show`, `state validate`, `state digest`)
   - W05: runtime noun-app
   - W06: metrics noun-app (promote `metrics` one-shot to `metrics show`)
   - W07: spec meta + `phase|iter|wave spec` subverbs (gates on C03 implementation)
   - W08: `wave switch`
   - W09: `skill resume` (gates on C04 D4 [5:127-134])
   - W10: `plugin sync` (gates on V9 [1:289-294])
   - W11: `completion` + `help` topic surface
   - W12: streaming output (`--stream` flag wiring on `wave dispatch`, `flow run`, `audit run`, `skill run`, `metrics show --watch`, `daemon logs --follow`)
   - W13: stability-budget CI lint

### 7.2 Backward-compat constraints

- **Every existing verb name MUST remain callable.** No renames; no relocations across noun-apps. `eawf metrics` one-shot becomes `eawf metrics show` *but the one-shot is kept as a thin alias for one minor version*; emits a deprecation banner on stderr; removed at v0.4 per D5 rotation.
- **Exit codes 0..9 are BREAKING per Q2 (round 1) + Q2b (round 4).** Single-PR cutover in W02 with CHANGELOG `BREAKING:` entry. Every test pin updated in the same PR. Downstream CI consumers (Slack runners, custom scripts) must update during the W02 review window — release-note + Slack #eawf-dev announce 7 days before merge.
- **Every existing JSON envelope field MUST keep its name + type.** New fields (`suggested_next_step`, `data`, `correlation_id`, `protocol_version`, `timestamp`) are all optional. The new `data.kind` substring carries the legacy nine-class subclass name for CI scripts that still need fine-grained pivots.
- **`--json` and `--plain` MUST keep their current shapes.** New flags (`--md`, `--quiet`, `--verbose`, `--stream`, `--daemonless`) are additive.

### 7.3 Daemon-rollout sequencing

The daemon (C02) ships *before* the C05 daemon-control verbs. Sequencing:

```
1. C02 implementation phase ships `eawfd` daemon with on-demand spawn + JSON-RPC + state.mutate.
2. C05 W01-W02 prepare CLI for daemon-mediated mutations.
3. C05 W03 wires up the daemon CLI noun-app (enable/disable/status/restart/logs/version).
4. C05 W04-W13 add the new surfaces.
```

Until C02 ships, mutating verbs continue to use the existing in-process `_mutation.state_transaction` [28] path; the wrapper transparently switches to daemon-mediated when daemon is detected (PID file present + version-compatible).

### 7.4 Rollback plan

- **KISS-005 (W01)** — single PR; rollback reverts the commit. No state-shape changes.
- **Exit-code extensions (W02)** — single PR; the three new codes are additive. Rollback: remove the constants + subclasses; existing callers that don't raise the new subclasses are untouched.
- **New noun-apps (W03+)** — each PR adds one verb. Rollback per-PR. The registry table makes per-verb removal a one-line edit.
- **Streaming (W12)** — gated by `--stream` flag; rollback removes the flag from `GlobalFlags`; the underlying daemon subscription path is C02-owned and stays.
- **Stability-budget CI lint (W13)** — soft-fail for the first 2 weeks (warn-only); promotes to hard-fail thereafter. Rollback: remove the test.

### 7.5 Per-verb migration notes

| Old surface | New surface | Migration |
|---|---|---|
| `eawf metrics` (one-shot) | `eawf metrics show` (subverb) | Alias retained for one minor version with deprecation banner. |
| `eawf state resolve` (sole subverb) | `eawf state {resolve, show, validate, digest}` | Additive — `resolve` unchanged. |
| `eawf wave dispatch --runtime <name>` (first-spawn) | unchanged; **+** `eawf wave switch --to <name>` (reactive override) | Two verbs; first-spawn vs. mid-flight switch. |
| Direct state-CLI mutation (today's `_mutation.state_transaction`) | Daemon `state.mutate` when daemon detected | Transparent switch; no operator action. |
| Exit codes 0..9 (current `eawf.cli.exit_codes` [23]) | 0..5 (USER_ERROR, VALIDATION_ERROR, STATE_CONFLICT, DAEMON_UNREACHABLE, INTERNAL_ERROR) per Q2/Q2a | **BREAKING** — single-PR cutover in W02 per Q2b; legacy distinctions live in `ErrorEnvelope.data.kind`. |
| `CliError` subclasses (NotFound, InvalidInput, ValidationFailed, LockConflict, InstrumentMissing, UserDeclined, IntegrityViolation, HookBlocked) | Five subclasses (UserError, ValidationError, StateConflict, DaemonUnreachable, InternalError) | Renames in same PR. Legacy class names live on as `data.kind` string constants. |
| No completion install | `eawf completion install <shell>` | Opt-in per Q6. Never edits shell-rc. |
| No prose help | `eawf help <topic>` (six topics per Q5) | Additive. |
| No daemon control | `eawf daemon enable | disable | status | restart | logs | version` | Additive per Q3. |
| No stability tags | `[experimental]` / `[deprecated]` Typer help-panel suffix | Additive. CI lint soft-fail 2 weeks per Q10. |
| Manual import + `add_typer` ladder [21:114-388] | `_VERB_REGISTRY` tuple in `app.py` per Q11 | KISS-005 [6:71]; non-breaking refactor in W01. |
| Bootstrap `eawf init` | In-process write per Q12; auto-spawn daemon after | Avoids bootstrap paradox (daemon needs state.json that init creates). |

## 8. Ratified verdicts

All 12 axes plus the three Q2 follow-ups were ratified in a four-round blitz on 2026-05-17. Brief promoted to `accepted`.

| Round | Q | Axis | Verdict | Reco-match? |
|---|---|---|---|---|
| 1 | Q1 | Verb-noun arity (D1) | **Noun-first two-noun** (`eawf phase open`, `eawf phase spec init`) | ✓ |
| 1 | Q2 | Exit-code taxonomy (D3) | **Compress 0..9 → 0..5** per C00 §C05 [1:587-588] | OVERRIDE (operator chose option (b)) |
| 1 | Q3 | Daemon control verb set (D7) | **Six visible** (`enable | disable | status | restart | logs | version`) + debug-gated `start`/`stop`/`replay-wal`/`reload-config` | ✓ |
| 1 | Q4 | Metrics CLI surface (D8) | **`show | export | rebuild`** | ✓ |
| 2 | Q5 | Help-topic count (D10) | **Six topics** (`exit-codes`, `daemon`, `profiles`, `urns`, `migration`, `streaming`) | ✓ |
| 2 | Q6 | Completion install (D11) | **Opt-in stdout write** — never edits shell-rc | ✓ |
| 2 | Q7 | `--stream` default (D12) | **Opt-in, default off** — CI determinism wins | ✓ |
| 2 | Q8 | `eawf state mutate` exposure | **Hide entirely** — domain verbs only | ✓ |
| 3 | Q9 | `--md` scope across verbs | **Envelope-only** — non-envelope verbs ignore the flag | ✓ |
| 3 | Q10 | Stability-budget CI lint | **Soft-fail 2 weeks** then hard-fail | ✓ |
| 3 | Q11 | Static registration shape | **One table in `app.py`** | ✓ |
| 3 | Q12 | `eawf init` daemon dep | **In-process bootstrap**; auto-spawn after init | ✓ |
| 4 | Q2a | Legacy code bucket map | **Operator-fixable → 1, state → 3** per §5.3 table | ✓ |
| 4 | Q2b | CI migration shape | **Single-PR cutover** in W02 with CHANGELOG `BREAKING:` entry | ✓ |
| 4 | Q2c | Extra codes for daemon classes? | **No** — fold ProtocolMismatch into 1, RuntimeUnavailable into 3 | ✓ |

**Effects applied to brief:**

- §5.3 rewritten — six-code taxonomy, legacy bucket map, daemon-code→exit map all reshaped.
- §5.4 ErrorEnvelope re-scoped — five `CliError` subclasses; legacy nine-class distinction preserved via `data.kind` string.
- §5.1 verb matrix — every Exit-codes column remapped to the 0..5 surface.
- §5.5 daemonless-rejected envelope sample → exit 1 with `data.kind="InvalidInput"`.
- §5.10 sample `Raises:` blocks updated.
- §6 failure modes F1..F17 + edge cases reshaped onto the new codes.
- §7.2 backward-compat constraint flipped from "0..9 preserved" to "0..9 BREAKING — single-PR cutover".
- §7.5 migration table — new rows for exit-code rename + subclass rename + KISS-005 + bootstrap.

**Open follow-ups for subsequent phases / clusters:**

- **OF1 — Downstream-consumer announce.** Release-note + Slack #eawf-dev announce 7 days before W02 merge so downstream CI users adjust pinning. Owner: C10 (Operations).
- **OF2 — `data.kind` enum catalog.** Lock the legacy-nine list as a `Literal[...]` enum in `eawf.cli.errors` so CI scripts can validate the string set. Owner: this brief's implementation phase (C05-WXX); enumerate after W02 lands.
- **OF3 — Experimental rotation queue.** Existing experimental verbs (`coauthor resolve`, `operator rollup`, `cc statusline`, `flow run|status|abort`, `wave fix-ci-loop`, `wave land-batch`, `wave policy {set,show}`) carry pre-W13 `since` values. Audit which are due for promotion vs. removal when W13 ships; record in §5.9. Owner: C09 (Quality + Observability).
- **OF4 — Verb stability surfacing in TUI.** Palette verb registry in C06 mirrors §5.1; should it filter experimental verbs by default? Defer to C06.
- **OF5 — `state mutate` raw-RPC restriction.** §6 edge case states daemon rejects non-CLI principals. Principal model is C01-D4 deferred to v0.5+. Until then, daemon enforces a process-name check (`comm == "eawf"`) — recorded as the v0.3 interim policy. Owner: C02 implementation.

## 9. References

[1] `.ea/local/research/long-term/2026-05-16-c00-spec-index.md` — Eä framework long-term spec index (verdicts V1..V9, cluster catalog)

[2] `.ea/local/research/long-term/2026-05-16-c01-foundations.md` — Foundations: URN + entity catalog + lifecycle

[3] `.ea/local/research/long-term/2026-05-16-c02-daemon-topology.md` — Daemon + topology + security

[4] `.ea/local/research/long-term/2026-05-16-c03-spec-infrastructure.md` — Spec infrastructure (Phase / Iter / Wave specs)

[5] `.ea/local/research/long-term/2026-05-16-c04-workflow-skills.md` — Workflow & skills (catalog + envelope contract)

[6] `.ea/local/research/yagni-kiss-dry-codebase-review-2026-05-15.md` — YAGNI / KISS / DRY codebase review (KISS-005 root-app simplification)

[7] `.ea/local/research/2026-05-15-long-term-roadmap-synthesis.md` — locked roadmap-synthesis verdicts

[8] `.ea/local/research/2026-05-15-long-term-features-deep.md` — long-term features deep dive (cache-control, EU)

[9] `src/eawf/lock/portalock.py` — portalocker-backed sibling-file locking (referenced by C02 §5.6)

[10] `src/eawf/state/models.py` — typed `State` model used by `_mutation.state_transaction`

[11] `AGENTS.md` — non-negotiable rules + workflow lifecycle (rules 1, 2, 4, 8, 11, 16, 17, 19, 20, 21 cited)

[12] `src/eawf/state/writer.py` — `atomic_write_json_locked` (tempfile + os.replace pattern)

[13] (reserved)

[14] `src/eawf/state/registry.py` — `<local-path>` writer per AGENTS rule 4 [11]

[15] `src/eawf/state/ids.py` — phase/iter/wave id regexes

[16] `src/eawf/lifecycle/transitions.py` — lifecycle closure invariants

[17] `src/eawf/urn.py` — URN grammar + parser (C01 §5.2 source-of-truth)

[18] `src/eawf/daemon/__init__.py` (PROTOCOL_VERSION constant — defined by C02 implementation)

[19] (reserved)

[20] (reserved)

[21] `src/eawf/cli/app.py` — current Typer root callback + manual import + `add_typer` ladder (KISS-005 target [6:48])

[22] `src/eawf/cli/output.py` — `emit_json_or_text` unified emission helper

[23] `src/eawf/cli/exit_codes.py` — current exit-code constants (0..9)

[24] `src/eawf/cli/errors.py` — `CliError` taxonomy + `emit_error` envelope shape

[25] `src/eawf/cli/help_panels.py` — `RegistryOrderedTyperGroup` + `COMMAND_PANELS` per-panel ordering

[26] `src/eawf/cli/flags.py` — `GlobalFlags` dataclass + `--scope` non-hoist rationale

[27] `src/eawf/cli/commands/state.py` — current `state resolve` subverb (only `state` subverb today)

[28] `src/eawf/cli/_mutation.py` — `state_transaction` context manager (replaced by daemon path in V1 mutating verbs)

[29] `src/eawf/cli/commands/workspace.py` — workspace noun-app (init / add-repo / remove-repo / validate / status / registry-list / registry-status)

[30] `docs/reference/exit-codes.md` — canonical exit-code reference (referenced by [23])

[31] `https://www.jsonrpc.org/specification` — JSON-RPC 2.0 wire spec (C02 IPC protocol)

## Provenance

- `store_record=none (local-only research)`
- `commit=3b86f7a (parent, observed at session start 2026-05-16; revisions 2026-05-18)`
- `supersedes=none`
- `session=eawf-spec-cluster-c05-2026-05-16` (draft) + `eawf-spec-cluster-c05-blitz-2026-05-17` (ratification) + audit-revisions 2026-05-18
- `prior_clusters_accepted_at_session_start=C01..C04 (per session brief)`
- `operator_verdicts_locked=V1..V9 (per C00 [1:23-315])`
- `model=claude-opus-4-7`
- `ratification_blitz=4 rounds × AskUserQuestion (Q1-Q4 round1, Q5-Q8 round2, Q9-Q12 round3, Q2a/Q2b/Q2c round4) on 2026-05-17`
- `verdict_overrides=Q2 (operator chose compress 0..9→0..5 over reco of additive); see §5.3 + §7.2 for cascade`
- `last_revised=2026-05-18 (audit-driven: --plain locked per XB18/E-01; ErrorEnvelope schema_version + datetime + default_factory per E-03/Codex C05-I010; raw RPC behind dev-mode gate per E-11 noted in §5.5 daemon escalation; old-to-new exit-code table preserved at §5.3)`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md (1 BLOCKER XB18; 12 Codex issues)`
- `authority_binding=Q1 (2026-05-18): CLI is dispatch only (AGENTS rule 1). Mutating verbs route via JSON-RPC to daemon (sole writer); read verbs may bypass daemon for daemonless mode per V1 reader-bypass exemption.`
- `inputs_read=C00, C01..C04 (depended-on sections only), AGENTS.md, src/eawf/cli/{app.py, flags.py, output.py, errors.py, exit_codes.py, help_panels.py, _mutation.py, commands/state.py}, every src/eawf/cli/commands/*.py (grep-based verb inventory), .ea/local/research/yagni-kiss-dry-codebase-review-2026-05-15.md §KISS-005`

## Scrub

- status: clean
- references: repo-relative, JSON-RPC spec URL, or eawf URN
- local paths: none
- real emails: none
- machine-specific paths: none (every `<local-path>` reference is a per-user logical path expected on every install)
- abstract placeholder names: not applicable (no mockup repos)
- hostnames: none
- credentials / tokens: none
