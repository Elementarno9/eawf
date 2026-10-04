# eawf CLI reference

Auto-generated from `eawf.surfaces.cli.app:app`. Every top-level command and sub-group verb registered on the root Typer app is listed below; do not hand-edit — regenerate via `eawf doc verify --strict`.

## Top-level commands

| Command | Summary |
|---|---|
| `clone-repo` | Clone *url* and run ``eawf init --no-input`` against the result. |
| `follow` | Stream an operation's states until it succeeds, fails or is lost. |
| `impact` | Render decision → wave → file-glob impact graph. |
| `init` | Initialise a new Eä Workflow workspace. |
| `metrics` | Show rolling workflow metrics — EU variance, audit pass rate, wave elapsed, and planned vs reactive split. |
| `render-output` | Convert between JSON and markdown forms of the output envelope (reads JSON or markdown from stdin). At a TTY with no piped data the command exits 2 with a hint instead of hanging. |
| `status` | Show active pointers, blockers, and git head. |
| `sync` | Re-render managed assets and report drift. |
| `ui` | Open the Eä console (or its plain frame off-TTY). |
| `validate` | Validate a state or envelope document. |
| `verbs` | List every verb with its entity, parameters, typed errors and effect class. |
| `version` | Show the eawf version (text or JSON envelope). |
| `why` | Explain why an EAWF entity has its current trust tier. |

## Command groups

### `eawf action`

What the Attention register asks a principal about (snooze, assign, decide-permission, notice).

| Verb | Summary |
|---|---|
| `assign` | Address a waiting pending action to one principal; anyone eligible may still answer. |
| `decide-permission` | Approve or deny a provider permission as the operator, before its deadline. |
| `notice` | Snooze, acknowledge or resolve a budget notice; none of them touches the work. |
| `snooze` | Hide a waiting pending action from yourself alone for a while; it answers nothing. |

### `eawf backup`

Snapshot backups of state.json + config.yaml, plus legacy profile.yaml when present.

| Verb | Summary |
|---|---|
| `create` | Snapshot the repo's ``.ea/`` artifacts into a timestamped backup dir. |
| `list` | List every snapshot for the current repo, most-recent first. |
| `prune` | Keep the N most-recent snapshots; delete older ones. |
| `restore` | Restore ``state.json`` + ``config.yaml`` + optional legacy ``profile.yaml`` from *ts*. |

### `eawf batch`

Delivery-batch lifecycle (activate, integrate, adopt-landed, ready, merge, reconcile, observe-merge, complete).

| Verb | Summary |
|---|---|
| `activate` | Move a PLANNED delivery Batch to ACTIVE. |
| `adopt-landed` | Adopt work that already landed on the Batch's target branch. |
| `close-legacy` | Complete an imported ACTIVE Batch (iter) once every Task in it is terminal. |
| `complete` | Complete a merged Batch whose landed commit matches the head it pinned. |
| `create` | Admit a new delivery Batch's create document into the addressed tree. |
| `integrate` | Integrate a Batch's sealed candidates into its next generation. |
| `label` | Pin the ground truth of one Batch criterion for the jury's calibration. |
| `merge` | Authorise the merge of a READY_TO_MERGE Batch at the head it pinned. |
| `observe-merge` | Record that the host landed a MERGING Batch, on a filed landed read-back. |
| `ready` | Declare an ACTIVE Batch ready to merge. |
| `reconcile` | File what a read-back of a MERGING Batch's target branch found. |

### `eawf bench`

Perf bench harness — seed corpora, time harnesses, flag regressions.

| Verb | Summary |
|---|---|
| `compare` | Flag any harness that regressed past the per-OS threshold. |
| `list` | List every fixture size x harness in the catalog. |
| `run` | Seed a corpus in-memory and time each harness against it. |
| `turn-cost` | Render wall clock + cost per completed unit of work, or check it. |

### `eawf campaign`

Plan, drive and cancel research Campaigns.

| Verb | Summary |
|---|---|
| `budget` | Set an active Campaign's budget limits, keeping what it already spent. |
| `cancel` | Cancel an active Campaign, recording why; its steps and artifacts stay. |
| `new` | Plan a research Campaign from its brief and approve the plan. |
| `run` | Drive an approved Campaign round by round in the daemon. |

### `eawf cc`

Claude Code adapter (statusline, plugin, hooks).

_No verbs registered._

### `eawf claim`

File a claim, and re-run and attest the checks behind its evidence rungs.

| Verb | Summary |
|---|---|
| `attest` | Record what an outside party decided about a claim's rung 4. |
| `check` | Re-run the check behind a rung of a claim, and every rung above it. |
| `file` | File a claim about a Task, Batch or Milestone and score its evidence ladder. |

### `eawf coauthor`

Resolve co-author trailers from VCS config.

| Verb | Summary |
|---|---|
| `resolve` | Resolve the configured co-author trailer. |

### `eawf completion`

Generate or install shell completion scripts (bash/zsh/fish).

| Verb | Summary |
|---|---|
| `install` | Write the completion script to *shell*'s canonical directory. |
| `show` | Print the completion script for *shell* to stdout (no file written). |

### `eawf config`

Manage layered configuration (built-in / global / workspace / repo / local).

| Verb | Summary |
|---|---|
| `get` | Print the merged value for ``key`` and the layer it came from. |
| `menu` | Open an interactive ``questionary`` menu for tunable config keys. |
| `set` | Write *value* under *key* to the chosen layer file. |
| `unset` | Remove *key* from one layer without changing lower-precedence values. |
| `validate` | Validate the merged config against the minimal Pydantic schema. |

### `eawf daemon`

Manage the eawfd background daemon.

| Verb | Summary |
|---|---|
| `logs` | Print the trailing window of the daemon log file. |
| `ping` | Probe daemon liveness and report version + PID. |
| `reclaim` | Reclaim disk: sweep the WAL once and trim aged state.json backups. |
| `replay-wal` | Inspect poisoned WAL records or GC the done window. |
| `restart` | Restart the daemon with the current executable and protocol. |
| `run` | Boot the daemon process. |
| `service-disable` | Stop + uninstall the eawfd service. Idempotent. |
| `service-enable` | Install + start the eawfd service via the native OS supervisor. |
| `service-status` | Report the supervisor-level service state plus daemon-health advisories. |
| `start` | Ensure the current daemon release is running. |
| `status` | Print operational counters from the running daemon, never starting one. |
| `stop` | Request graceful daemon shutdown. |

### `eawf doc`

Read-only documentation drift + state-vs-doc cross-checks.

| Verb | Summary |
|---|---|
| `verify` | Verify that rendered docs match state.json + manifest hashes. |

### `eawf doctor`

Run install-readiness checks (tools, state, config).

_No verbs registered._

### `eawf help`

Show prose help topics (exit-codes, daemon, profiles, urns, migration, upgrade-from-0.6, streaming).

_No verbs registered._

### `eawf hook`

Dispatch hook events through the Eä hook runner.

| Verb | Summary |
|---|---|
| `agent-output` | Ingest one output chunk forwarded for an externally dispatched session. |
| `dispatch` | Seed an interim verdict cohort from an ``agent_end`` event read from stdin. |
| `eawf002-log-key` | Reject ``_id``-suffixed wave/iter/phase keys in library log messages. |
| `eawf003-logger-acquire` | Reject library ``getLogger`` calls that do not pass ``__name__``. |
| `eawf010-module-length` | Reject Python modules over the physical line-count budget. |
| `eawf011-cognitive-complexity` | Reject functions over the cognitive-complexity budget. |
| `eawf012-design-provenance` | Reject design/audit/agent provenance breadcrumbs in source comments. |
| `eawf013-bracket-position` | Reject detached or post-punctuation numeric citation brackets. |
| `eawf014-no-manual-wrap` | Reject manually wrapped rendered Markdown paragraphs. |
| `eawf015-ears-advisory` | Warn on requirement-like prose outside EARS shape without blocking. |
| `eawf016-title-clarity` | Reject unclear entity titles added to the staged ``state.json`` delta. |
| `eawf017-inline-refs` | Reject inline bare URLs and inline ``path:line`` reference soup. |
| `eawf018-structure-smell` | Warn on block-bloat structure smells in Markdown and docstrings without blocking. |
| `eawf019-math-facets` | Reject math-explainer claims missing a facet, an unresolved citation, or a dead gate. |
| `eawf023-artifact-placement` | Reject misplaced or date-stem-less artifacts under ``.ea/artifacts/``. |
| `eawf024-test-tier-contract` | Reject non-unit imports in ``tests/unit/`` and mis-tiered kind markers. |
| `eawf025-test-placement` | Reject a newly added test filed outside its taxonomy address. |
| `eawf026-settings-categories` | Reject a configuration catalog section the settings rail cannot reach. |
| `eawf027-citation-scope` | Reject committed text quoting a reflection row it may not. |
| `eawf028-brief-immutable` | Reject a staged wording edit to a committed research brief. |
| `email-leak-lint` | Reject email addresses outside the canonical author/no-reply allowlist. |
| `log-format-lint` | Run the EAWF001 log-format rule over changed library modules. |
| `path-leak-lint` | Reject home-directory path literals (macOS, Windows, and Linux home roots). |
| `plugin-doctor-drift` | Fail when ``plugin doctor --strict`` reports drift in the plugin tree. |
| `run` | Dispatch a hook event read from stdin and emit the result envelope. |
| `sigil-totality` | Assert every TUI-render status value resolves to a real ratified glyph. |
| `vale-prose` | Run the Vale prose linter over Markdown and emit the findings. |
| `validate-prose` | Compose every Layer-2 prose check over changed Markdown — the chokepoint. |

### `eawf jury`

Cross-vendor jury calibration surfaces (gold-label writer).

| Verb | Summary |
|---|---|
| `label` | Append an operator gold label for a wave via the daemon. |

### `eawf mcp`

Manage MCP server entries (add/install/update/remove/list/grant/revoke).

| Verb | Summary |
|---|---|
| `add` | Register a new Eä-owned MCP server. |
| `grant` | Bind an MCP server to a scope so dispatch can project allowed-tools. |
| `install` | Write a registered MCP server into a runtime config and record the install. |
| `list` | List MCP entries from the registry and/or the runtime config. |
| `remove` | Retire an Eä-owned MCP server (and optionally its runtime config entries). |
| `revoke` | Revoke an MCP grant; its row stays under its id so the id is never reused. |
| `run-config` | Render how one lane is told about a Run's semantic tool server. |
| `serve` | Serve one Run's granted semantic tools over MCP stdio. |
| `update` | Replace fields of a registered Eä-owned MCP server. |

### `eawf memory`

Manage curated durable memory entries.

| Verb | Summary |
|---|---|
| `add` | File a new memory note on the generation's memory ledger. |
| `compact` | Compact ``memory.jsonl`` (dedup by content; idempotent). |
| `digest` | Emit a state-derived standup: current focus, recent closes, decisions. |
| `gc` | Archive matched memory notes by moving them to the ARCHIVAL tier. |
| `list` | List the memory notes the tree stands at. |
| `promote` | Promote a record. ``--to memory`` (default) or ``--to artifact``. |
| `prune` | Soft-delete prune. Flips status to PRUNED; the prior revision stays on the ledger. |
| `render-context` | Produce a token-budgeted Markdown rendering of memory entries. |
| `stale` | List memory entries that exceed ``--age`` days and are below high confidence. |
| `tier` | Set the tier on a single memory note. |
| `view` | Show a single memory note: its summary and its body. |

### `eawf migrate`

Migrate state.json across schema versions (v1.0 -> v1.1 chain).

| Verb | Summary |
|---|---|
| `status` | Show the current ``schema_version`` and available migration edges. |

### `eawf milestone`

Milestone lifecycle (activate, open-review, open-approval, accept, cancel, set-target).

| Verb | Summary |
|---|---|
| `accept` | Accept a Milestone in review against a sealed approval receipt. |
| `activate` | Move a PLANNED Milestone to ACTIVE under an active Track. |
| `cancel` | Cancel a Milestone that has not completed. |
| `cancel-legacy` | Cancel an imported PLANNED Milestone against a recorded decision or artifact. |
| `close-legacy` | Complete an imported ACTIVE Milestone (phase) against a recorded audit. |
| `create` | Admit a new Milestone's create document into the addressed tree. |
| `open-approval` | Ask the operator to accept a Milestone in review on its acceptance bundle. |
| `open-review` | Open acceptance review on an ACTIVE Milestone. |
| `seal-approval` | Seal the operator's answer onto a waiting acceptance question. |
| `set-target` | Set or clear the day an open Milestone is aimed at; its status does not move. |

### `eawf plan`

Iter plan view (read-only: DAG, waves, checks, risks) plus the submit/approve/apply plan-revision verbs.

| Verb | Summary |
|---|---|
| `apply` | Materialise one APPROVED plan revision into its Milestone. |
| `approve` | Seal a human principal's approval onto a VALIDATED plan revision. |
| `show` | Print the active iter plan view (markdown or JSON). |
| `submit` | Record one planner proposal as a VALIDATED plan revision. |

### `eawf plugin`

Install, update, or diagnose runtime plugins (claude, codex, opencode). Use 'install' for all three; 'package' is Claude-only (marketplace export).

| Verb | Summary |
|---|---|
| `doctor` | Report drift in an installed runtime plugin tree. |
| `install` | Render a runtime plugin tree. |
| `package` | Emit an installable runtime plugin tree. |
| `sync` | Regenerate per-runtime plugin artifacts deterministically. |
| `update` | Re-render a runtime plugin tree, aborting on hand-edits. |

### `eawf pr`

Render a phase PR body from state.json.

| Verb | Summary |
|---|---|
| `render` | Render the PR body for a phase or iter as Markdown (or JSON envelope). |

### `eawf profile`

Profile body scaffolding + trust ledger management.

| Verb | Summary |
|---|---|
| `new` | Scaffold a workspace profile at ``.ea/profiles/<name>.yaml``. |
| `validate` | Validate a profile (or every profile) against the layered loader. |

### `eawf question`

File and answer operator decisions.

| Verb | Summary |
|---|---|
| `answer` | Seal the option a reply to a numbered question prompt chooses. |
| `open-decision` | File a reversible operator decision the host shows as a typed question. |
| `reply` | Answer an open question a host asked, by one of its options or in your own words. |

### `eawf record`

Append audit, decision and artifact records to an epoch-2 tree.

| Verb | Summary |
|---|---|
| `append` | Append one audit, decision or artifact to the generation's ledger. |
| `evidence` | File one evidence row an acceptance step or answer may cite. |

### `eawf reflect`

Report where effort, time and money went; no canonical write.

| Verb | Summary |
|---|---|
| `export` | Write the newest report as a static page that opens from the filesystem. |
| `prune` | Remove local reports and cached titles past their retention class. |
| `run` | Read the tree's Runs, fill their titles, and write the report. |
| `serve` | Serve the local collection read-only on loopback until interrupted. |
| `show` | Print the newest report in the local collection. |

### `eawf release`

Tag releases and drive the release train's checkpoint records.

| Verb | Summary |
|---|---|
| `adopt` | Adopt a publication that ran without a release record. |
| `advance` | Walk the train past one finished checkpoint, or refuse and change nothing. |
| `approve` | Approve a candidate against a readiness sweep, and record it. |
| `bind-regime` | Bind a Milestone or Batch to the steady, fast or experimental regime. |
| `burn` | Burn the version: record the spent checkpoint as partially released. |
| `cancel` | Abandon a checkpoint that never touched a registry, or refuse. |
| `candidate` | Freeze the manifest from the receipts and record the CANDIDATE. |
| `changelog` | Mine the current ``CHANGELOG.md`` unreleased section. |
| `create` | Open one checkpoint's DRAFT record, after measured admission. |
| `discharge-debt` | Discharge a verification debt once its deferred gate has passed. |
| `notes` | Render a scrubbed release-notes draft. |
| `observe` | Read one publication target back and settle it against the manifest. |
| `pipeline` | Take a merged phase's checkpoint from tag to BAKED and the train advance. |
| `preflight` | Sweep every readiness signal for one checkpoint over this checkout. |
| `publish` | Open the publication episode and return its reference at once. |
| `readiness` | Ask the daemon for a checkpoint's readiness sweep. |
| `receipts` | Prove every required gate of one checkpoint on its pinned source. |
| `reconcile` | Settle one leg against what its publish job finally reported. |
| `retry` | Re-queue one leg of the open episode under the idempotency proof. |
| `show` | Describe the train ladder, one checkpoint rung, and its record. |
| `tag` | Create the ``v<version>`` release tag and (with ``--push``) trigger the pipeline. |

### `eawf repo`

Repo-scoped init + workspace linkage.

| Verb | Summary |
|---|---|
| `add` | Explicitly add/register a repo to the user-scope registry. |
| `init` | Initialise a repo-scoped workspace at *target*. |
| `prune` | Drop registry entries whose on-disk paths no longer exist. |
| `register` | Explicitly add/register a repo to the user-scope registry. |
| `remove` | Drop the entry whose ``code == <code>`` from the registry. |

### `eawf repository`

Repository rows a plan binds its head to (create).

| Verb | Summary |
|---|---|
| `create` | Admit a repository row at the head its git history holds now. |

### `eawf rules`

Read, migrate and roll back the projections rendered from .ea/rules.yaml.

| Verb | Summary |
|---|---|
| `migrate` | Check every legacy block and profile field has exactly one disposition. |
| `rollback` | Re-select a complete stored generation of the rule projections. |
| `view` | Print the detailed view of one selected rule module. |

### `eawf run`

Run lifecycle (create, start, finish, fail).

| Verb | Summary |
|---|---|
| `cancel` | Ask a Run to end without finishing its work. |
| `create` | Admit a QUEUED Run against the scope its create document names. |
| `drain-dispatch` | Ask the scheduler to start nothing new and let what runs finish; cancels nothing. |
| `fail` | Fail a RUNNING Run; the payload carries the reason, ended_at and failure. |
| `finish` | Complete a RUNNING Run once its report is bound; updates carry ended_at. |
| `interrupt` | Ask a Run to stop at its next safe point; it keeps its work. |
| `pause-dispatch` | Ask the scheduler to admit no new Run until a resume; claimed Runs go on. |
| `reconcile` | Ask a Run to reconcile a control whose outcome is unknown. |
| `report` | Write the plain-text report of one Run; no record moves. |
| `resume-dispatch` | Ask the scheduler to admit queued Runs again after a pause or a drain. |
| `start` | Start a QUEUED Run; the payload's updates carry started_at. |

### `eawf runtime`

Certify the runtime harnesses installed on this machine.

| Verb | Summary |
|---|---|
| `certify` | Probe the installed runtime and record its certification or quarantine. |

### `eawf schema`

Dump JSON Schema + reference pages for the canonical models.

| Verb | Summary |
|---|---|
| `dump` | Dump JSON Schema (and reference pages) for the canonical models. |

### `eawf session`

Manage AI/human work sessions.

| Verb | Summary |
|---|---|
| `close` | Close a session; required to reach the ``closed/stale/failed`` set. |
| `recover` | Mark every active/checkpointed session whose heartbeat is older than ``--age`` as stale. |

### `eawf skill`

List, render, and run Eä workflow skills.

| Verb | Summary |
|---|---|
| `check-report` | Validate a terminal report against the skill's typed output schema. |
| `list` | List every skill resolvable across builtin / user / workspace layers. |
| `reconcile` | Reconcile the built-in skill registry against the disk skill tree. |
| `render` | Render a registered skill's metadata or SKILL.md body to stdout. |
| `run` | Run a registered skill headlessly and emit its envelope. |

### `eawf snapshot`

Golden-fixture snapshot surfaces — list and regenerate per --kind.

| Verb | Summary |
|---|---|
| `list` | List every snapshot surface in the locked inventory. |
| `update` | Regenerate the golden subset for one snapshot surface. |

### `eawf store`

JSONL store maintenance (compact, ...).

| Verb | Summary |
|---|---|
| `compact` | Compact the JSONL store for *kind* and emit the dedup report. |

### `eawf task`

Task lifecycle (promote, claim, start, submit, seal, prove, assess, ready, complete).

| Verb | Summary |
|---|---|
| `advance-legacy` | Move an imported Task (wave or backlog row) along the legacy edge table. |
| `assess` | Judge a Task for completion and print the document task complete needs. |
| `claim` | Claim a PLANNED Task for the actor that will run it. |
| `complete` | Complete a Task on the Batch head its passing assessment proves. |
| `create` | Admit a new Task's create document into the addressed tree. |
| `demote` | Hand a PLANNED Task that was never claimed back to the backlog as a DRAFT. |
| `drop` | Drop a DRAFT or DEFERRED Task from the backlog, recording why. |
| `promote` | Promote a DRAFT Task to PLANNED once its contract is complete. |
| `prove` | Run a Task's gates at the generation each leg binds and file the receipts. |
| `ready` | Declare a RUNNING Task ready to integrate on its bound report and evidence. |
| `release` | Release a CLAIMED Task's lease, handing it back to PLANNED. |
| `seal` | Bind a Run's accepted report to its candidate and attempt the seal. |
| `start` | Start a CLAIMED Task under the Run the payload binds it to. |
| `submit` | File one Run's claim that its leased workspace is ready to integrate. |

### `eawf telemetry`

Telemetry / observability subsystem — pricing currency, projection.

| Verb | Summary |
|---|---|
| `pricing-currency-check` | Validate the embedded pricing snapshot and emit a drift report. |

### `eawf track`

Track lifecycle (create, retire).

| Verb | Summary |
|---|---|
| `create` | Admit a new Track's create document into the addressed tree. |
| `retire` | Retire an ACTIVE Track once no Milestone under it is open. |

### `eawf vfl`

Visual-fidelity-layer golden management -- approve regenerated goldens.

| Verb | Summary |
|---|---|
| `approve` | Regenerate and approve one surface's golden bytes. |

### `eawf wal`

Inspect the daemon write-ahead log (read-only: status, list, show).

| Verb | Summary |
|---|---|
| `list` | List WAL records (id, status, envelope kind + summary, timestamp). |
| `show` | Dump one WAL record's decoded envelope by record id. |
| `status` | Summarise the WAL: per-status counts, newest/oldest, total size. |

### `eawf wiki`

Render a per-phase narrative project wiki from state.json.

| Verb | Summary |
|---|---|
| `render` | Render the project wiki as Markdown (or JSON envelope). |

### `eawf workspace`

Workspace-scoped state and repo linkage.

| Verb | Summary |
|---|---|
| `add` | Register a workspace record with an explicit membership. |
| `list` | List every registered workspace, ordered by key. Read-only. |
| `registry-list` | Enumerate repos in ``~/.eawf/registry.json``. |
| `registry-status` | Render the workspace dashboard as text (top strip + W02 quadrant). |
| `select` | Select a workspace for the current session only. |
| `show` | Show one workspace record, resolving it when no key is given. |

### `eawf worktree`

Manage per-wave git worktrees (create / list / merge-back / cleanup / reconcile).

| Verb | Summary |
|---|---|
| `cleanup` | Tear down the worktree directory + per-wave branch. |
| `merge-back` | Replay worktree commits onto the parent branch. |
| `reconcile` | Retire active worktree and session rows whose holder is gone. |
