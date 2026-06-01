# eawf CLI reference

Auto-generated from `eawf.surfaces.cli.app:app`. Every top-level command and
sub-group verb registered on the root Typer app is listed below; do
not hand-edit — regenerate via `eawf doc verify --strict`.

## Top-level commands

| Command | Summary |
|---|---|
| `clone-repo` | Clone *url* and run ``eawf init --no-input`` against the result. |
| `impact` | Render decision → wave → file-glob impact graph. |
| `init` | Initialise a new Eä Workflow workspace. |
| `metrics` | Show rolling workflow metrics — EU variance, audit pass rate, wave elapsed, and planned vs reactive split. |
| `render-output` | Convert between JSON and markdown forms of the output envelope (reads JSON or markdown from stdin). At a TTY with no piped data the command exits 2 with a hint instead of hanging. |
| `status` | Show active pointers, blockers, and git head. |
| `sync` | Re-render managed assets and report drift. |
| `tui` | Open the Eä Textual TUI (or deterministic status fallback off-TTY). |
| `validate` | Validate a state or envelope document. |
| `version` | Show the eawf version (text or JSON envelope). |
| `why` | Explain why an EAWF entity has its current trust tier. |

## Command groups

### `eawf actual`

Open / close / recover actual segments for a scope.

| Verb | Summary |
|---|---|
| `recover` | Walk active actuals and abandon any segment held by a stale lock holder. |
| `start` | Open a new actual segment for ``(scope, session)``. |
| `stop` | Close the latest open segment for *scope* and write the elapsed EU. |

### `eawf agent-report`

Manage typed agent reports.

| Verb | Summary |
|---|---|
| `add` | Append a typed agent report. |
| `list` | List typed agent reports. |
| `show` | Show a typed agent report. |

### `eawf artifact`

Manage artifacts (add / show / verify).

| Verb | Summary |
|---|---|
| `add` | Register a durable artifact. |
| `show` | Show artifact metadata. |
| `update` | Update mutable fields on a registered artifact. |
| `validate` | Validate one markdown artifact body. |
| `verify` | Recompute artifact sha256 and compare to the registered hash. |

### `eawf audit`

Manage audits (add / run / integrity / show / list).

| Verb | Summary |
|---|---|
| `add` | Register an audit; report-bearing audits land status=complete. |
| `integrity` | Append an integrity-check result to an existing audit. |
| `list` | List audits with optional filters. |
| `promote` | — |
| `run` | Run an audit. ``--checks`` drives the DSL runner; ``--fixture`` is the |
| `set-verdict` | Stamp a verdict on an existing audit. |
| `show` | Show metadata for one audit. |

### `eawf backlog`

Manage backlog items (add / close).

| Verb | Summary |
|---|---|
| `add` | Add a new backlog item. |
| `close` | Close a backlog item; requires --audit of a complete audit. |
| `edit` | Edit an open backlog item's title, description, and/or intent. |
| `set-priority` | Update the priority of an open backlog item. |

### `eawf backup`

Snapshot backups of state.json + config.yaml, plus legacy profile.yaml when present.

| Verb | Summary |
|---|---|
| `create` | Snapshot the repo's ``.ea/`` artifacts into a timestamped backup dir. |
| `list` | List every snapshot for the current repo, most-recent first. |
| `prune` | Keep the N most-recent snapshots; delete older ones. |
| `restore` | Restore ``state.json`` + ``config.yaml`` + optional legacy ``profile.yaml`` from *ts*. |

### `eawf bench`

Perf bench harness — seed corpora, time harnesses, flag regressions.

| Verb | Summary |
|---|---|
| `compare` | Flag any harness that regressed past the per-OS threshold. |
| `list` | List every fixture size x harness in the catalog. |
| `run` | Seed a corpus in-memory and time each harness against it. |

### `eawf calibrate`

Re-fit estimation parameters from recorded actuals.

| Verb | Summary |
|---|---|
| `apply` | Apply one fitted bucket centroid to layered config after confirmation. |
| `buckets` | Re-fit the XS..XL effort buckets from 90-day actuals and nudge on drift. |

### `eawf cc`

Claude Code adapter (statusline, plugin, hooks).

_No verbs registered._

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
| `validate` | Validate the merged config against the minimal Pydantic schema. |

### `eawf daemon`

Manage the eawfd background daemon (run, ping, status, stop, logs).

| Verb | Summary |
|---|---|
| `logs` | Print the trailing window of the daemon log file. |
| `ping` | Probe daemon liveness and report version + PID. |
| `replay-wal` | Inspect poisoned WAL records or GC the done window. |
| `run` | Boot the daemon process. |
| `service-disable` | Stop + uninstall the eawfd service. Idempotent. |
| `service-enable` | Install + start the eawfd service via the native OS supervisor. |
| `service-status` | Report the supervisor-level service state (no daemon RPC). |
| `status` | Print operational counters from the running daemon. |
| `stop` | Request graceful daemon shutdown. |

### `eawf decision`

Manage decisions (add / supersede / list / graph).

| Verb | Summary |
|---|---|
| `add` | Record a durable decision. |
| `graph` | Render the decision graph (text, Graphviz DOT, or Mermaid). |
| `list` | List decisions filtered by scope. |
| `promote` | — |
| `supersede` | Supersede an existing decision by another existing decision. |

### `eawf doc`

Read-only documentation drift + state-vs-doc cross-checks.

| Verb | Summary |
|---|---|
| `verify` | Verify that rendered docs match state.json + manifest hashes. |

### `eawf doctor`

Run install-readiness checks (tools, state, config).

_No verbs registered._

### `eawf draft`

Create and validate local draft artifacts.

| Verb | Summary |
|---|---|
| `new` | Create a templated local draft under ``.ea/local/<kind>/``. |
| `validate` | Validate a local draft artifact. |

### `eawf estimate`

Create or update EU estimates for a scope.

| Verb | Summary |
|---|---|
| `set` | Create (or replace) the estimate for *scope*. |
| `update` | Update the estimate for *scope*, replacing the current summary record. |

### `eawf evidence`

Attest verify-spine evidence (attest).

| Verb | Summary |
|---|---|
| `attest` | Append a typed verify-spine evidence row. |

### `eawf flow`

Operator surface for the /flow skill (run, status, abort).

| Verb | Summary |
|---|---|
| `abort` | Abort a flow run by appending an ``abandoned`` flow_record. |
| `run` | Run the ``/flow`` skill (fresh or resumed). |
| `status` | Print structured status for a flow run (read-only). |

### `eawf goal`

Manage project goals (define).

| Verb | Summary |
|---|---|
| `define` | Define a new goal under the current scope. |

### `eawf help`

Show prose help topics (exit-codes, daemon, profiles, urns, migration, streaming).

_No verbs registered._

### `eawf hook`

Dispatch hook events through the Eä hook runner.

| Verb | Summary |
|---|---|
| `dispatch` | Seed an interim verdict cohort from an ``agent_end`` event read from stdin. |
| `eawf012-design-provenance` | Reject design/audit/agent provenance breadcrumbs in source comments. |
| `eawf013-bracket-position` | Reject detached or post-punctuation numeric citation brackets. |
| `eawf014-no-manual-wrap` | Reject manually wrapped rendered Markdown paragraphs. |
| `eawf015-ears-advisory` | Warn on requirement-like prose outside EARS shape without blocking. |
| `email-leak-lint` | Reject email addresses outside the canonical author/no-reply allowlist. |
| `log-format-lint` | Run the EAWF001 log-format rule over changed library modules. |
| `path-leak-lint` | Reject home-directory path literals (macOS, Windows, and Linux home roots). |
| `plugin-doctor-drift` | Fail when ``plugin doctor --strict`` reports drift in the plugin tree. |
| `run` | Dispatch a hook event read from stdin and emit the result envelope. |

### `eawf hypothesis`

Manage hypotheses (define / verdict / list).

| Verb | Summary |
|---|---|
| `define` | Register a new pending hypothesis. |
| `list` | List hypotheses (read-only). |
| `promote` | — |
| `verdict` | Record a hypothesis verdict; requires --audit of a complete audit. |

### `eawf incident`

Manage incidents (open / close / view).

| Verb | Summary |
|---|---|
| `close` | Close an incident; requires --audit of a complete audit. |
| `open` | Open a new incident. |
| `promote` | — |
| `view` | View incident metadata + linked artifact ids. |

### `eawf iter`

Iteration lifecycle (open, close).

| Verb | Summary |
|---|---|
| `activate` | Flip a PLANNED iter to ACTIVE. |
| `close` | Close an active iter. Rejects when child waves are still open. |
| `open` | Open an iter. Pass an iter ID or a phase id (auto-allocates iter). |
| `plan` | Stage a PLANNED iter under an open phase without moving the current pointer. |

### `eawf mcp`

Manage MCP server entries (add/install/update/remove/list/grant/revoke).

| Verb | Summary |
|---|---|
| `add` | Register a new Eä-owned MCP entry in ``state.mcp_servers``. |
| `grant` | Bind an MCP server to a scope so dispatch can project allowed-tools. |
| `install` | Materialise an Eä-owned MCP entry into the runtime config. |
| `list` | List MCP entries from state and/or runtime config. |
| `remove` | Delete an Eä-owned MCP entry from state (and optionally runtime configs). |
| `revoke` | Remove an MCP grant from ``state.mcp_grants``. |
| `update` | Patch an existing Eä-owned MCP entry in ``state.mcp_servers``. |

### `eawf memory`

Manage curated durable memory entries.

| Verb | Summary |
|---|---|
| `add` | Write a new memory entry to ``memory.jsonl`` + ``state.memory_index``. |
| `compact` | Compact ``memory.jsonl`` (dedup by content; idempotent). |
| `gc` | Archive matched memory entries by flipping their ``tier`` to ARCHIVAL. |
| `list` | List memory entries from ``state.memory_index`` (the cache). |
| `promote` | Promote a record. ``--to memory`` (default) or ``--to artifact``. |
| `prune` | Soft-delete prune. Flips status to PRUNED; preserves the prior record. |
| `render-context` | Produce a token-budgeted Markdown rendering of memory entries. |
| `stale` | List memory entries that exceed ``--age`` days and are below high confidence. |
| `tier` | Set the tier on a single memory entry. |
| `view` | Show a single memory entry: cache summary + JSONL body. |

### `eawf migrate`

Migrate state.json across schema versions (v1.0 -> v1.1 chain).

| Verb | Summary |
|---|---|
| `status` | Show the current ``schema_version`` and available migration edges. |

### `eawf operator`

Operator report rollups.

| Verb | Summary |
|---|---|
| `rollup` | Render a read-only operator rollup for *phase_id*. |

### `eawf outcome`

Manage outcomes (define / set).

| Verb | Summary |
|---|---|
| `define` | Define a new pending outcome. |
| `set` | Record an outcome measurement; requires --audit of a complete audit. |

### `eawf phase`

Phase lifecycle (open, close, reopen).

| Verb | Summary |
|---|---|
| `activate` | Flip a PLANNED phase to ACTIVE (P19-W07, P19-W11, P19-W13). |
| `close` | Close an active phase. Rejects when child iters are still open. |
| `open` | Open a new phase. Provide an explicit ID or use ``--auto``. |
| `prepare-close` | Compute a pre-close checklist for *phase_id* without closing it. |
| `reopen` | Reopen a closed phase. Used for follow-up iters after a phase close. |

### `eawf plan`

Read-only iter plan view (DAG, waves, checks, risks).

| Verb | Summary |
|---|---|
| `promote` | — |
| `show` | Print the active iter plan view (markdown or JSON). |

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

### `eawf project`

Project-level lifecycle (init).

| Verb | Summary |
|---|---|
| `init` | Create or upgrade a project record at the active state path. |

### `eawf release`

Tag releases and render release notes / changelog reports.

| Verb | Summary |
|---|---|
| `changelog` | Mine the current ``CHANGELOG.md`` unreleased section. |
| `notes` | Render a scrubbed release-notes draft. |
| `tag` | Create the ``v<version>`` release tag and (with ``--push``) trigger the pipeline. |

### `eawf repo`

Repo-scoped init + workspace linkage.

| Verb | Summary |
|---|---|
| `add` | Explicitly add/register a repo to the user-scope registry. |
| `init` | Initialise a repo-scoped workspace at *target*. |
| `link` | Cross-link a repo state and a workspace state. |
| `link-workspace` | Cross-link a repo state and a workspace state. |
| `prune` | Drop registry entries whose on-disk paths no longer exist. |
| `register` | Explicitly add/register a repo to the user-scope registry. |
| `remove` | Drop the entry whose ``code == <code>`` from the registry. |

### `eawf research`

Show and promote research briefs.

| Verb | Summary |
|---|---|
| `promote` | — |
| `show` | Show one research store record. |

### `eawf roadmap`

Roadmap planner (propose / revise / apply / drop / show).

| Verb | Summary |
|---|---|
| `apply` | Confirm a PLANNED phase's wave DAG before handing off to ``/prep``. |
| `drop` | Archive a PLANNED phase (PLANNED → ARCHIVED). Irreversible via the |
| `propose` | Propose a PLANNED phase from flags or a strict roadmap plan file. |
| `revise` | Edit a PLANNED or ACTIVE phase's wave plan via structured flags. |
| `show` | Render the PLANNED queue plus the ACTIVE phase summary. |

### `eawf schema`

Dump JSON Schema + reference pages for the canonical models.

| Verb | Summary |
|---|---|
| `dump` | Dump JSON Schema (and reference pages) for the canonical models. |

### `eawf session`

Manage AI/human work sessions.

| Verb | Summary |
|---|---|
| `checkpoint` | Append a checkpoint event for an existing session. |
| `close` | Close a session; required to reach the ``closed/stale/failed`` set. |
| `recover` | Mark every active/checkpointed session whose heartbeat is older than ``--age`` as stale. |
| `start` | Start a new agent session; rejects (scope, runtime) collisions. |

### `eawf skill`

List, render, and run Eä workflow skills.

| Verb | Summary |
|---|---|
| `list` | List every skill resolvable across builtin / user / workspace layers. |
| `reconcile` | Reconcile the built-in skill registry against the disk skill tree. |
| `render` | Render a registered skill's metadata or SKILL.md body to stdout. |
| `resume` | Resume a paused needs_user question with the chosen option label. |
| `run` | Run a registered skill headlessly and emit its envelope. |

### `eawf snapshot`

Golden-fixture snapshot surfaces — list and regenerate per --kind.

| Verb | Summary |
|---|---|
| `list` | List every snapshot surface in the locked inventory. |
| `update` | Regenerate the golden subset for one snapshot surface. |

### `eawf spec`

Manage phase / iter / wave specs (init / validate / promote / archive / show).

| Verb | Summary |
|---|---|
| `archive` | Atomically ``git rm`` the spec file + write the archived cache entry. |
| `init` | Scaffold a new spec via daemon proxy (or in-process fallback). |
| `promote` | Forward-graduate DRAFT → READY → IMPLEMENTED through the daemon. |
| `show` | Print a spec body (cache + on-disk; ``--from-git`` walks history). |
| `validate` | Re-hash the on-disk spec body + refresh the daemon cache row. |

### `eawf state`

Read-only state introspection (resolve, show) + dev-mode raw RPC.

| Verb | Summary |
|---|---|
| `resolve` | Print the resolved ``state.json`` path and the reason for selection. |
| `show` | Print a read-only view of ``state.json``. |

### `eawf store`

JSONL store maintenance (compact, ...).

| Verb | Summary |
|---|---|
| `compact` | Compact the JSONL store for *kind* and emit the dedup report. |

### `eawf subproject`

Subproject lifecycle (add, switch).

| Verb | Summary |
|---|---|
| `add` | Add a subproject under the active project. |
| `switch` | Set the active subproject pointer. |

### `eawf telemetry`

Telemetry / observability subsystem — pricing currency, projection.

| Verb | Summary |
|---|---|
| `pricing-currency-check` | Validate the embedded pricing snapshot and emit a drift report. |

### `eawf wave`

Wave lifecycle (plan, claim, close, fail, graph, next-ready).

| Verb | Summary |
|---|---|
| `blocks-rebuild` | Rebuild ``Wave.blocks`` reverse-index from sister waves' ``deps``. |
| `claim` | Claim a pending wave for *session*. Exactly-once across concurrent calls. |
| `close` | Close a claimed/in-progress wave with an outcome string. |
| `dispatch` | Render the subagent prompt for *wave_id* (read-only). |
| `dispatch-batch` | Render prompts for every (or every ready) pending wave under an iter. |
| `fail` | Mark a claimed/in-progress wave as failed with *reason*. |
| `fix-ci` | Plan a follow-up wave that targets the failing files in *log*. |
| `fix-ci-loop` | Plan a chain of CI-fix follow-up waves until convergence or *max_iters*. |
| `graph` | Print the wave DAG for an iter in topological order. |
| `land` | Cherry-pick the wave's worktree commits onto the parent branch. |
| `land-batch` | Apply ``wave land`` to every eligible wave in dep order; stop on failure. |
| `next-ready` | List pending waves whose every dep is ``closed``. |
| `plan` | Plan a new pending wave under an open iter. |
| `release` | Release a claimed/in-progress wave back to pending (the inverse of claim). |
| `review` | Attach review findings to a wave, or render a reviewer prompt. |
| `show` | Inspect a wave. ``--commit`` prints SHA; ``--dispatch-prompt`` prints prompt. |
| `update` | Mutate a PENDING/CLAIMED wave's ``file_scopes``. |

### `eawf wiki`

Render a per-phase narrative project wiki from state.json.

| Verb | Summary |
|---|---|
| `render` | Render the project wiki as Markdown (or JSON envelope). |

### `eawf workspace`

Workspace-scoped state and repo linkage.

| Verb | Summary |
|---|---|
| `add-repo` | Append a :class:`WorkspaceRepoRef` to the workspace index. |
| `init` | Create a workspace state document at the resolved state path. |
| `registry-list` | Enumerate repos in ``~/.eawf/registry.json``. |
| `registry-status` | Render the workspace dashboard as text (top strip + W02 quadrant). |
| `remove-repo` | Drop a :class:`WorkspaceRepoRef` from the workspace index. |
| `status` | Print the workspace metadata + linked-repos summary. |
| `validate` | Check that every linked repo path exists and contains ``.ea/state.json``. |

### `eawf worktree`

Manage per-wave git worktrees (create / list / merge-back / cleanup).

| Verb | Summary |
|---|---|
| `cleanup` | Tear down the worktree directory + per-wave branch. |
| `create` | Create a per-wave worktree branched from the current feature branch. |
| `list` | Enumerate recorded worktrees with a git-side cross-check column. |
| `merge-back` | Replay worktree commits onto the parent branch. |
| `path-fix` | Rewrite WorktreeRecord.path values from absolute to repo-relative. |
