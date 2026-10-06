# Configuration reference

This page lists every configuration leaf eawf reads and every `EAWF_` environment variable it reads or sets. The leaf list mirrors the leaf catalog in `src/eawf/kernel/config/registry/leaf_catalog.py`, and the variable list mirrors the environment census in `tests/lint/test_env_control_var_census.py`; `tests/unit/test_config_reference_doc.py` fails when either gains an entry this page does not name.

## Layers

eawf composes its configuration from layers, later layers overriding earlier ones. Maps merge key by key; scalars and lists replace.

| Layer | Where it lives | Notes |
| --- | --- | --- |
| `built-in` | shipped with eawf | Defaults below; never written. |
| `global` | `~/.config/eawf/config.yaml` | One operator, every repository on the machine. |
| `workspace` | `<workspace>/.ea/config.yaml` | Shared by the repositories of one workspace. |
| `repo` | `<repo>/.ea/config.yaml` | Committed with the repository. |
| `branch` | `<repo>/.ea/branches/<branch>.yaml` | One file per branch. |
| `local` | `<repo>/.ea/local/config.yaml` | Machine-local, never committed. |
| `wave` | daemon memory | Held for one running unit of work, then dropped. |
| `env` | `EAWF_<SECTION>__<KEY>` variables | Runtime only; see below. |
| `cli` | command-line overrides | Runtime only. |

Each leaf names the layers that may write it (the Layers column below). `eawf config set` writes the file layers; a layer the leaf does not list is refused.

### Environment overrides

A variable shaped `EAWF_<SECTION>__<KEY>` sets a leaf from the environment: the double underscore separates the dotted path, so `EAWF_UI__THEME=light` sets `ui.theme`. `true`, `false`, `null` and numbers are coerced; everything else stays a string.

The override is held to the same catalog as a file layer: a variable naming no leaf, or a leaf whose Layers column does not include `env`, is ignored with a warning, and a value the leaf's type, choices or range refuses is an error. Today only `runtime.preference`, `ui.theme`, `ui.glyphs` and `ui.toasts` accept the `env` layer. The control variables listed further down use a single underscore and are never read as overrides.

## Leaves

Type is the value shape; Layers names the layers that may write the leaf.

### Configuration file

| Key | Type | Default | Layers | What it does |
| --- | --- | --- | --- | --- |
| `schema_version` | literal | `1.0` | none | Marks the on-disk layered-config schema shape; the config migration maintains it. |

### Profiles

| Key | Type | Default | Layers | What it does |
| --- | --- | --- | --- | --- |
| `profiles.enabled` | list of strings | `[core]` | global, workspace, repo, branch, local | Rule profiles enabled for this repository, rendered into its agent policy. |
| `profiles.trusted` | mapping | `{}` | repo, branch | Profile id to the sha256 digest of the profile body last trusted. |
| `profiles.certified` | mapping | `{}` | repo, branch | Profile id to the digest an enriched profile is certified as managed under. Not wired: its named reader has no production caller yet, so setting it changes nothing. |

### Runtime

| Key | Type | Default | Layers | What it does |
| --- | --- | --- | --- | --- |
| `runtime.preference` | list of strings | `[claude-code]` | global, workspace, repo, branch, local, env, `cli`, wave | Runtimes a dispatch may use, in fallback order; the first entry is the default provider. One of `claude-code`, `codex`, `opencode`. |
| `runtime.adapters` | list of strings | `[claude-code]` | global, workspace, repo | Older runtime list, read only when `runtime.preference` is unset. |
| `runtime.auto_certify` | true or false | `true` | global, workspace, repo | Probes, in the background, a runtime version no certification covers when a Run first reports it. |
| `runtime.profiles` | list | built-in provider profiles | global, workspace, repo | Provider profiles: whole records on the global and workspace layers, narrowing patches only on the repository layer. |
| `runtime.routes` | list | built-in routes | global, workspace | Provider routes choosing the profiles a Run may bind to. |
| `runtime.models.claude` | list of strings | unset | global, workspace, repo | Optional cheap, mid and top model ladder for the Claude runtime. |
| `runtime.models.codex` | list of strings | unset | global, workspace, repo | Optional cheap, mid and top model ladder for the Codex runtime. |
| `runtime.models.opencode` | list of strings | unset | global, workspace, repo | Optional cheap, mid and top model ladder for the opencode runtime. |
| `runtime.claude.stall_interval_s` | int | `600` | global, workspace, repo | Seconds a Claude Run may produce nothing before it is flagged stalled. |
| `runtime.claude.permission_wait_s` | int | `20` | global, workspace, repo | Seconds Claude's permission hook waits for a principal's decision. |
| `runtime.codex.stall_interval_s` | int | `600` | global, workspace, repo | Seconds a Codex Run may produce nothing before it is flagged stalled. |
| `runtime.opencode.stall_interval_s` | int | `600` | global, workspace, repo | Seconds an opencode Run may produce nothing before it is flagged stalled. |
| `runtime.transcript.retention_days` | int, 1 to 365 | `30` | global, workspace, repo | Days a Run's full message text stays in the machine-local store. |

### Operator and console

| Key | Type | Default | Layers | What it does |
| --- | --- | --- | --- | --- |
| `operator.principal` | string | unset | global | Principal key this machine's operator claims (for example `OP-0001`); a console launched without `--actor` acts as it, and as nobody without a claim. |
| `ui.theme` | literal | `dark` | global, workspace, repo, env | Console colour theme: `dark`, `light`, `cb` (colour-blind safe) or `auto`. |
| `ui.glyphs` | literal | `auto` | global, workspace, repo, env | Console glyph set: `unicode`, `ascii`, or `auto` to follow the terminal encoding. |
| `ui.toasts` | literal | `important` | global, workspace, repo, env | Toasts for state changes nobody keyed: `off` raises none, `important` and `all` raise them. Answers to a key and the quit prompt always show. |

### Telemetry

| Key | Type | Default | Layers | What it does |
| --- | --- | --- | --- | --- |
| `telemetry.enabled` | true or false | `true` | global, workspace, repo | Whether the `eawf metrics` commands open the machine-local telemetry store. |
| `telemetry.db_kind` | literal | `sqlite` | global | Database engine of the machine-local telemetry store: `sqlite` or `duckdb`. |

### Dispatch, agents and economics

| Key | Type | Default | Layers | What it does |
| --- | --- | --- | --- | --- |
| `dispatch.role_tier_token_cap` | int | `2400` | global, workspace, repo | Intended token ceiling per injected role-tier dispatch block. Not wired: its named reader has no production caller yet, so setting it changes nothing. |
| `agents.extra_tools` | mapping | `{}` | global, workspace, repo | Per-role extra tool names appended to each rendered subagent's allowlist; the `*` key grants to every role. |
| `economics.prompt_budget` | mapping | shipped policy | global, workspace, repo | Prompt budget: the input window, reserved output and per-class ceilings. |
| `economics.governor` | mapping | shipped policy | global, workspace, repo | In-flight governor: concurrent Runs, in-flight tokens and cost, admission. |
| `economics.notice_policy` | mapping | shipped policy | global, workspace, repo | When a Run at its hard ceiling opens a budget notice: the no-event grace. |

### Research, planning and estimation

| Key | Type | Default | Layers | What it does |
| --- | --- | --- | --- | --- |
| `research.default_depth` | literal | `medium` | global, workspace, repo | Survey depth a research campaign runs at when none is given: `shallow`, `medium`, `deep` or `exhaustive`. |
| `research.agent_count` | int, 1 to 12 | `4` | global, workspace, repo | Researchers a campaign round fans out to when none is given; a value outside the band is clamped into it. |
| `planning.max_parallel_waves` | int, 1 to 16 | `4` | global, workspace, repo | Repository-wide cap on waves (the older unit of planned work) that are claimed or in progress at once; `eawf doctor` reports a breach. |
| `estimation.eu_minutes` | int, 5 to 240 | `30` | global, workspace, repo | Minutes of agent-driven work one effort unit (EU) stands for. |
| `estimation.eu_basis` | literal | `api_duration` | global, workspace, repo | Captured quantity converted into elapsed effort units: `api_duration`, `tokens` or `wall_clock`. |
| `preferences.auto_choose` | literal | `off` | global, workspace, repo | Whether an operator question auto-picks its recommended option: `off`, `recommended` or `always`. |

### Verification

| Key | Type | Default | Layers | What it does |
| --- | --- | --- | --- | --- |
| `verify.odr_blocking` | true or false | `false` | global, workspace, repo | Repository opt-in: an oracle-determinism ratio (ODR) below its floor refuses an iter close. |
| `verify.require_iter_audit_accepted` | true or false | `false` | global, workspace, repo | Requires a completed, accepted audit before an iter close. |
| `verify.waiver_mode` | literal | `B` | global, workspace, repo | Operator waiver policy: `A`, `B`, `C` or `disabled`; `disabled` wins over every other layer's value. |
| `verify.retyped_rule_threshold` | int | `3` | global, workspace, repo | Re-typings of one rule within a release above which it is reported for review. |
| `verify.jury_max_brier` | float, 0 to 1 | `0.25` | global, workspace, repo | Highest jury Brier score that still earns blocking verdicts. |
| `verify.jury_max_co_error` | float, 0 to 1 | `0.1` | global, workspace, repo | Highest share of known-bad subjects a jury may pass unanimously. |
| `verify.juror_wall_clock_seconds` | float | `600.0` | global, workspace, repo | Wall-clock ceiling for close-time auditor and juror runs. |

### Token budget

| Key | Type | Default | Layers | What it does |
| --- | --- | --- | --- | --- |
| `flow.budget.enforce` | literal | `soft` | global, workspace, repo | Intended token-budget enforcement mode, `soft` (warn) or `hard` (halt at the cap). Not wired: its named reader has no production caller yet, so setting it changes nothing. |
| `flow.budget.multiplier` | float, at least 1 | `1.5` | global, workspace, repo | Intended multiplier from a base token budget to its enforced cap. Not wired: its named reader has no production caller yet, so setting it changes nothing. |

### Version control

| Key | Type | Default | Layers | What it does |
| --- | --- | --- | --- | --- |
| `vcs.checkpoint_requires_commit` | true or false | `true` | global, workspace, repo | Whether a scope close's checkpoint ref is checked to name a commit that exists. |
| `vcs.integration_commit_unit` | literal | `batch` | global, workspace, repo | Unit of one verified delivery commit: `batch` squashes a Batch's sealed candidates at integration, `task` keeps one delivery commit per Task. |
| `vcs.task_reference` | literal | `trailer` | global, workspace, repo | How a delivery commit names its Task: a `Task:` trailer, an in-subject identifier, or `none`. |
| `vcs.conventions.subject_style` | literal | `trailer` | global, workspace, repo | Commit-subject form the repository's commit lint expects: `trailer` or the deprecated `bracket`. Read by `tools/commit_prefix_lint.py`, not by the package. |
| `vcs.coauthor.mode` | string | `runtime` | global, workspace, repo | Co-author trailer policy: `runtime` names the agent runtime, `project` names the project identity, `disabled` writes none. |
| `vcs.coauthor.default_runtime` | string | `claude` | global, workspace, repo | Runtime a runtime-mode trailer names when none is detected. |
| `vcs.coauthor.project.name` | string | unset | global, workspace, repo | Co-author name a project-mode trailer names; set together with the email. |
| `vcs.coauthor.project.email` | string | unset | global, workspace, repo | Co-author email a project-mode trailer names; set together with the name. |
| `vcs.coauthor.trailers.claude.name` | string | `Claude` | global, workspace, repo | Co-author name a Claude runtime trailer names. |
| `vcs.coauthor.trailers.claude.email` | string | the vendor's no-reply address | global, workspace, repo | Co-author email a Claude runtime trailer names. |
| `vcs.coauthor.trailers.codex.name` | string | `Codex` | global, workspace, repo | Co-author name a Codex runtime trailer names. |
| `vcs.coauthor.trailers.codex.email` | string | the vendor's no-reply address | global, workspace, repo | Co-author email a Codex runtime trailer names. |
| `vcs.coauthor.require_trailer` | true or false | `true` | global, workspace, repo | Whether a runtime with no configured trailer identity is an error. |

### Daemon

| Key | Type | Default | Layers | What it does |
| --- | --- | --- | --- | --- |
| `daemon.proxy_enabled` | true or false | `true` | global, workspace, repo | When false, mutations bypass the daemon and write under a file lock instead. |
| `daemon.idle_timeout_seconds` | int, at least 1 | `300` | global, workspace, repo | Seconds the daemon stays up with no client request before it shuts down. `EAWF_DAEMON_IDLE_TIMEOUT` overrides it. |
| `daemon.session_handle_ttl_seconds` | int, at least 1 | `86400` | global, workspace, repo | Seconds after an agent session ends before the daemon prunes its handle. `EAWF_DAEMON_SESSION_TTL` overrides it. |

### Reserved leaves

These leaves stay in the catalog so an existing file that sets them still loads, but nothing reads them and the console does not offer them for editing:

- `runtime.adapter_catalog.claude.enabled`, `runtime.adapter_catalog.claude.plugin_install`, `runtime.adapter_catalog.claude.skills_path` and `runtime.adapter_catalog.claude.agents_path`
- `runtime.adapter_catalog.codex.enabled` and `runtime.adapter_catalog.codex.status`
- `runtime.adapter_catalog.opencode.enabled` and `runtime.adapter_catalog.opencode.status`
- `audit.fix_safe`, `ship.require_audit_pass` and `ship.require_memory_review`

Leaves retired outright are stripped from layer files by the config migration, and `eawf doctor` names any a layer still sets.

## Environment variables

These control variables use a single underscore after the `EAWF_` prefix and are never read as configuration overrides. Variables marked "set by eawf" are exported to a child process eawf starts; setting them yourself is not supported.

### Locations

| Variable | What it does |
| --- | --- |
| `EAWF_HOME` | Home directory under which eawf keeps its user-scope `.eawf` data, such as backups; defaults to the user's home. |
| `EAWF_REGISTRY_PATH` | Path of the repository and workspace registry; defaults to `~/.eawf/registry.json`. |
| `EAWF_RUNTIME_DIR` | Directory holding the daemon's socket, PID file, log and write-ahead log, used verbatim; without it each tree gets its own directory under the per-user runtime base. A daemon started with it set never registers with the system service manager. |
| `EAWF_STATE` | Reserved name; the configuration merge skips it and the package does not read it. |
| `EAWF_CLAUDE_PROJECTS_DIR` | Root of the Claude projects tree that session history and transcript counters read; defaults to the Claude user directory. |
| `EAWF_SPEC_CACHE_DIR` | Removed from a gate command's environment so the gate cannot reuse the caller's cache. |
| `EAWF_STATUSLINE_CACHE` | Directory the statusline caches its output in. |

### Daemon

| Variable | What it does |
| --- | --- |
| `EAWF_DAEMONLESS` | Set to `1` to request the daemon bypass `--daemonless` requests, for scripts that cannot pass the flag; it also overrides `daemon.proxy_enabled` to false. |
| `EAWF_DAEMON_IDLE_TIMEOUT` | Seconds of idleness before the daemon shuts down; overrides `daemon.idle_timeout_seconds`. A non-positive or unreadable value falls back to the configured one. |
| `EAWF_DAEMON_SESSION_TTL` | Session-handle lifetime in seconds; overrides `daemon.session_handle_ttl_seconds`, with the same fallback. |
| `EAWF_DAEMON_LOG_MAX_BYTES` | Size cap of each daemon log file before it rotates; defaults to 16 MiB. |
| `EAWF_DAEMON_LOG_BACKUP_COUNT` | Rotated daemon log files kept; defaults to 5. |
| `EAWF_DAEMON_WAL_GC_INTERVAL` | Seconds between write-ahead-log clean-up sweeps; defaults to 3600. |
| `EAWF_DAEMON_WAL_RETENTION` | Seconds a flushed write-ahead-log record is kept before a sweep may remove it; defaults to 3600. |
| `EAWF_MUTATION_HARD_LIMIT_SECONDS` | Seconds after which the daemon's watchdog cancels a hung mutation; defaults to a bound derived from `verify.juror_wall_clock_seconds`. |
| `EAWF_VERBOSE` | Set to `1` to print daemon start-up progress to standard error. |
| `EAWF_LOCK_TIMEOUT` | Reserved name; the configuration merge skips it and the package does not read it. |
| `EAWF_SUITE_SESSION` | Marks every daemon a test session spawns, so the suite can find and stop the daemons it started. |

### Command line and console

| Variable | What it does |
| --- | --- |
| `EAWF_DEBUG` | Set to `1` to enable developer mode, which exposes the raw daemon pass-through and hidden daemon control verbs. |
| `EAWF_ACTOR` | Default for the console's `--actor`: the principal key its writes are attributed to. |
| `EAWF_RECEIPT_REF` | Default for the console's `--receipt-ref`: the evidence reference its answers are recorded under. |
| `EAWF_WORKSPACE_KEY` | Session-local workspace selection that `eawf workspace select` prints an export line for. |
| `EAWF_NO_PAGER` | Set to `1` to print help topics without a pager. |
| `EAWF_NO_OSC` | When set, the console does not query the terminal's background colour. |
| `EAWF_POLL_INTERVAL_S` | Seconds between the console's state polls; defaults to 2. |
| `EAWF_DAEMON_PROBE_INTERVAL_S` | Seconds between the console's daemon liveness probes; defaults to 0.5. |
| `EAWF_RECONNECT_MIN_INTERVAL_S` | Minimum seconds between the console's event-stream reconnects; defaults to 5. |
| `EAWF_CONSOLE_COLD_PAINT_T0` | Set by eawf: the clock reading handed to the console cold-paint measurement child. |
| `EAWF_STATUSLINE_THEME` | Statusline theme used when `--theme` is not given. |

### Commits and co-author trailers

| Variable | What it does |
| --- | --- |
| `EAWF_COAUTHOR_MODE` | Set to `disabled` to make the repository's commit tools write and require no co-author trailer. Read by `tools/coauthor_policy.py`. |
| `EAWF_COAUTHOR_RUNTIME` | Runtime a runtime-mode co-author trailer names, overriding detection: `claude`, `codex` or another runtime id. |
| `EAWF_COAUTHOR_HARNESS` | Older name for `EAWF_COAUTHOR_RUNTIME`, still read by the package and the repository's commit tools. |
| `EAWF_SKIP_GLOBAL_HOOKS` | Reserved name; the configuration merge skips it and the package does not read it. |

### Gates, tests and snapshots

| Variable | What it does |
| --- | --- |
| `EAWF_GATE_FILES` | Set by eawf: the newline-separated files a gate command covers. |
| `EAWF_EGRESS_SOCKET` | Set by eawf: the socket of the egress proxy a sandboxed child reaches the network through. |
| `EAWF_SNAPSHOT_REGEN` | Set to `1` to make golden tests rewrite their fixtures instead of comparing; `eawf snapshot` sets it for its child. |
| `EAWF_REFRESH_GOLDEN` | Set by `eawf snapshot` for its child together with `EAWF_SNAPSHOT_REGEN`. |
| `EAWF_SNAPSHOT_OUT` | Set by `eawf snapshot` to redirect regenerated fixtures to an output directory. |
| `EAWF_SKIP_PERF` | Reserved name; the configuration merge skips it and the package does not read it. |
| `EAWF_EVIDENCE_DIRECT_WRITE` | Reserved name; the configuration merge skips it and the package does not read it. |
| `EAWF_EVIDENCE_DIRECT_WRITE_MODE` | Reserved name; the configuration merge skips it and the package does not read it. |
| `EAWF_BLITZ_DEPTH` | Reserved name; the configuration merge skips it and the package does not read it. |
| `EAWF_BLITZ_DEPTH_COUNTER` | Reserved name; the configuration merge skips it and the package does not read it. |
