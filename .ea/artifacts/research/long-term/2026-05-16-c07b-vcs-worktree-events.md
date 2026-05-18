# C07b — VCS / Worktree / Multi-repo / Event log / Render / Brand — Eä framework long-term specs

**Cluster:** C07b (Subsystems — worktree, VCS integration, multi-repo registry, event/audit log, render/envelope, branding)
**Title:** VCS / Worktree / Multi-repo / Event log / Render / Brand
**Status:** `local-draft`, `needs-user` (pending operator ratification of §8 open questions)
**Created:** `2026-05-16T00:00:00Z`
**Author:** `claude-opus-4-7`
**Depends on:** C00 (V1..V8 locked) [1]; C01 (entity catalog, URN scheme, persona matrix) [2]; C02 (daemon IPC + event-bus + subscription model) [3]
**Consumed by:** C05 (CLI), C06 (TUI/web), C09 (telemetry/audit projection); paired with C07a (runtime / skill / agent dispatch)

## 1. Purpose + scope statement

C07b locks the six cross-cutting subsystems that move bytes between repositories, surface eawf's identity to the operator, and form the audit-replayable evidence chain. Where C07a sits at the runtime boundary (LLM dispatch), C07b sits at every other boundary the eawf user crosses: the git worktree they branch from, the commits they cherry-pick, the per-repo + cross-repo registry the daemon arbitrates over, the append-only logs that record everything, the typed envelope the CLI hands back, and the visual identity the TUI brands.

**In scope (C00 §C07 [1:648-712]).**

- **Worktree subsystem (B8).** Create / teardown / cherry-pick semantics, conflict resolution conventions, branch-currency gate enforcement, per-wave worktree-record lifecycle, registry lock against git concurrent-add collisions.
- **VCS integration (B9).** Commit-prefix lint full grammar (the regex, every prefix type, the `[P##-CORE]` state-only-paths rule), `prepare-commit-msg` hook auto-insert, `commit-msg` hook auto-reject, PR flow (`eawf ship` → `gh pr create`), coauthor trailer policy (runtime / project / disabled modes).
- **Multi-repo / portfolio (B10).** `<local-path>` registry shape, scope dispatch (cwd → workspace > repo > user), cross-repo workflows (workspace dashboard, registry list), explicit-init-only growth.
- **Event / audit log (B11).** Per-repo `.ea/store/<kind>.jsonl` JSONL layout, Envelope schema, append-only invariant, compaction (dedup by id), projections + replay, retention policy.
- **Render / envelope system (B12).** Three-part `OutputEnvelope` (header / body / footer), markdown ⇄ JSON round-trip via `to_markdown` / `from_markdown`, artifact chassis (Summary / References / Provenance / Scrub) renderers, dense `[N]` citation rows.
- **Branding (B13).** `Eä` logotype (capital E + a-umlaut), Wong 2011 deuteranopia-safe palette, glyph set, status-priority constants.

**Out of scope.**

- **Plugin / runtime / skill / agent dispatch.** C07a owns those — paired brief.
- **TUI rendering details (C06 [1:587-644]).** Widget catalog, scope dispatch ladder, modal stack. C07b names the *envelope* and *brand element* shapes; C06 names how they appear on screen.
- **CLI verb surface (C05 [1:539-583]).** `eawf ship`, `eawf wt`, `eawf repo`, `eawf workspace`, `eawf coauthor`, `eawf store` — verbs locked here implicitly via lifecycle phases; C05 owns the verb-noun matrix.
- **Per-skill semantics (C04 [1:485-534]).** `/ship` algorithm, `/audit` DSL kinds catalog.
- **Telemetry projection schema (C09 [1:769-841]).** C07b emits envelope rows; C09 projects them into DuckDB.

## 2. Goals + non-goals

### Goals

| G# | Goal | Source |
|---|---|---|
| G1 | Worktree create/teardown/cherry-pick contract specced end-to-end so AGENTS rule 11 (cherry-pick, never merge) is enforceable. | AGENTS rule 11 [13]; current code [14] |
| G2 | Commit-prefix lint grammar fully written out (regex + every type + `-CORE` state-only-paths whitelist). | AGENTS rule 14 [13]; current lint [15] |
| G3 | Coauthor trailer policy modes (runtime / project / disabled) and per-mode failure-rejection rules complete. | Codex-compatibility brief Q6 [5:252-284]; current code [16] |
| G4 | Multi-repo registry shape locked; explicit-init-only growth invariant preserved; staleness rules. | C00 axes [1:688]; feedback memory; current code [17] |
| G5 | Event-log append-only invariant + per-kind JSONL layout + retention policy spec (forever vs snapshot+tail). | C00 axes [1:689]; long-term-features-deep §9 [4:Q3] |
| G6 | Envelope chassis schemas (header / body / footer; status enum; markdown round-trip invariants) preserved verbatim from current code; the brief documents *which fields exist* and *what changes in v0.4*. | Current code [18] |
| G7 | Brand element specs: Eä logotype glyph string, Wong 2011 palette hex values, glyph set, status-priority enum. | C06 axes [1:599-600]; TUI direction §"palette + visual" [9:426-429] |
| G8 | Subsystem boundaries vs paired C07a clean: C07a runtime/skill/dispatch; C07b vcs/worktree/registry/event/render/brand. | C00 split prompt [1:709] |

### Non-goals

| NG# | Non-goal | Why deferred |
|---|---|---|
| NG1 | Per-runtime adapter / skill registry / dispatch routing. | C07a owns. |
| NG2 | TUI widget rendering (RoadmapTree / EUBar / GitPane / palette popover). | C06 owns. |
| NG3 | DuckDB telemetry schema; metric tile inventory. | C09 owns. |
| NG4 | Per-CLI-verb exit-code matrix / `--help` text. | C05 owns. |
| NG5 | External-system integrations (GitHub PR bridge details, Linear, Slack). | C11 owns. The PR-creation invocation is named here ("eawf ship → gh pr create"); the auth model + signing surfaces stay in C11. |
| NG6 | Multi-user / cross-machine event log federation. | C00 V8 single-user gate; v0.5+ work. |
| NG7 | Sigstore release signing / OPA bundle. | Roadmap synthesis [10:65-69] defers. |

## 3. Prior verdicts cited

### V1 — eawfd daemon Day-1 [1:24-53]

> "Mutations to `state.json` (and all future stateful surfaces — config layers, registry, event log) route through the eawfd daemon."

**C07b binding.** Three mutator surfaces named here (state-CLI, layered-config writer, registry writer; per AGENTS §"Mutator-path precision in wave success criteria" [13]) all eventually proxy through the daemon. Event-log appends happen daemon-side as part of the mutation transaction (C02 §5.6 [3:402-410] — append `event.jsonl` after state.json write, before WAL fsync). Audit-log appends similarly proxy through the daemon's `audit run` runner.

### V2 — Three-tier specs [1:55-74]

> "WaveSpec — wave deliverable: verdict citations from `implements:`, file scopes, behaviors, failure modes, tests, optional mockup."

**C07b binding.** WaveSpec storage at `.ea/specs/<phase>/<iter>/<wave>.md` per V2 [1:69-73]. The commit-prefix lint allowlist for `[P##-CORE]` commits extends to include `.ea/specs/**` (already in the lint [15:61]). The render chassis renders specs the same way as research briefs (Summary / References / Provenance / Scrub) once promoted.

### V3 — Composable profile bundle [1:76-96]

> "Profile contributions per profile: Default hooks (pre-commit, prepare-commit-msg, commit-msg)."

**C07b binding.** Hooks listed under "Default hooks" are exactly the three VCS hooks specced in §5.2 (commit-prefix lint at `commit-msg`; coauthor auto-insert at `prepare-commit-msg`; pre-commit fast-fail stack). Profile composition (C08) picks which hooks fire; the *shapes* are locked here.

### V5 — Runtime fallback [1:127-151]

> "Switchover never silently rewrites Wave.runtime; daemon emits a runtime_switched event with from, to, cause fields so audit replay shows the trace."

**C07b binding.** `runtime_switched` is one event kind in the catalog (§5.4); its payload shape is specified in C02 [3:778-787]. Event-log retention policy must keep it forever (it's audit-evidence per the persona matrix [2:1318-1325]).

### V6 — Per-OS daemon service [1:153-182]

> "Service-file install/uninstall is reversible; eawf daemon disable removes the file cleanly."

**C07b binding.** The registry (`<local-path>`) is the user-scope index of repos the daemon serves. V6 ensures the daemon's bootstrap is OS-native; C07b ensures the registry the daemon arbitrates over has clean explicit-init-only growth + per-repo staleness signal [17:218-254].

### V7 — Telemetry projection [1:184-224]

> "Per-repo event.jsonl remains canonical; user-scope DB is a projection, rebuildable from a full event.jsonl sweep."

**C07b binding.** Two implications: (1) Event-log retention is **forever** (or, at minimum, until snapshot — §5.4); the telemetry DB never replaces the JSONL as source-of-truth. (2) The event-kind catalog is closed (StoreKind enum); new kinds bump the schema version.

### V8 — Hybrid session reuse [1:226-271]

> "session_continued / session_failover events emit on retry / fallback for audit replay."

**C07b binding.** Two new event kinds in §5.4 (`session_continued`, `session_failover`). Retention: forever. Replay: audit replay walks them to reconstruct dispatch history per C01 §5.6.3 [2:1318-1325].

## 4. Decision matrix

| # | Axis | Options | Recommendation | Rationale |
|---|---|---|---|---|
| **D1** | Worktree cherry-pick policy | (a) per-commit cherry-pick; (b) per-wave squash; (c) hybrid | **(a) per-commit** (matches AGENTS rule 11 [13]; current code [14]) | Preserves the `[P##-W##]` commit history that commit-prefix lint enforces. Squash would force a single CORE commit per wave and obliterate the per-wave atomic history. |
| **D2** | Conflict resolution location | (a) in the worktree; (b) in the main worktree; (c) operator picks | **(b) main worktree** (current behaviour [14:155-200]) | Cherry-pick onto the parent feature branch happens in the main worktree (where the parent branch is checked out). On conflict, leave `.git/CHERRY_PICK_HEAD` set so operator resolves in main, then `git cherry-pick --continue`. The worktree-side branch already has the source commit. |
| **D3** | Branch currency gate timing | (a) at `/prep`; (b) at every wave dispatch; (c) at every CLI mutation | **(a) at `/prep` + at `/flow` claim** | Per AGENTS §"Branch currency" [13]: fetch + compare current branch to intended source before opening or resuming phase/iter/wave; rebase or fast-forward when stale. Adding a gate at every CLI mutation thrashes the operator with rebases on every action. |
| **D4** | Commit-prefix grammar | (a) `[P##]` only; (b) `[P##(-W##|-CORE)]` (current); (c) `[P##(-I##)?(-W##|-CORE)]` | **(b)** (locked by P19-W05 [15]) | The full regex is `^\[P\d{2}(-I\d{2})?(-W\d{2}|-CORE)\]\s+<type>:\s+\S.*$`. Optional `-I##` already supported. `-W##` or `-CORE` is mandatory; bare `[P##]` rejected. |
| **D5** | `-CORE` allowed paths | (a) `state.json` only; (b) state-bookkeeping set (current); (c) flexible | **(b)** locked by P19-W05 [15]: `.ea/state.json`, `.ea/store/event.jsonl`, `.ea/store/audit.jsonl`, `.secrets.baseline`, `.ea/specs/**` | Whitelist enforces that CORE commits don't smuggle code changes. C03's spec subsystem extends `.ea/specs/**` to the allowlist (already in current lint [15:61]). |
| **D6** | Coauthor mode default | (a) `runtime` (current default); (b) `disabled`; (c) `project` | **(a) `runtime`** (current default [16]) | Matches AGENTS commit-prefix rule [13] (every commit carries a recognized Claude/Codex Co-Authored-By trailer). `disabled` available for projects that opt out via `vcs.coauthor.mode: disabled` in config. |
| **D7** | Multi-repo registry growth | (a) explicit-init-only (current); (b) scan/walk; (c) hybrid | **(a)** locked by `feedback_explicit_registry_only` [17:1-29] + memory | Scan/walk adds repos the operator never opted into; explicit `eawf init` / `eawf workspace add-repo` is the only growth path. |
| **D8** | Event-log retention | (a) forever; (b) snapshot every N events then cold-tier; (c) per-kind | **(a) forever for v0.3-v0.5; (b) snapshot+tail planned for v0.5+** | Roadmap synthesis Q3 [4] recommends snapshot every 100 events with cold tier to `.ea/archive/<phase>/event.jsonl`. C07b records the policy *intent* but ships forever-retention until the snapshot tooling lands (P27-REPLAY [10:174-176]). |
| **D9** | Envelope status enum | (a) close at 5 (current); (b) extend; (c) per-skill | **(a) 5 statuses** [18:48]: `ok`, `needs_user`, `blocked`, `failed`, `partial` | Today's `EnvelopeStatus` Literal is byte-stable + tested. Extension forces footer-validator + downstream readers to update. v0.4 may add `partial_with_retries` if cost-ledger surfaces enough cases. |
| **D14** | Event schema ownership (per Q14 / XB07) | (a) C07b canonical owner; (b) C02 streaming-side owner; (c) split per consumer | **(a) C07b canonical owner** (ratified 2026-05-18 per Q14) | C07b already owns the event store. C02 streaming references C07b shape. C06 + C09 + C11 consume the same Pydantic `Event` model. One canonical owner kills the four-incompatible-event-models risk. See §5.4 for the canonical `Event` + `EventPayload` model. |
| **D15** | Worktree path location (per Q13 / CROSS.F59) | (a) `.claude/worktrees/` permanent; (b) `.ea/local/worktrees/` migration; (c) **`.ea/worktrees/` permanent** | **(c) `.ea/worktrees/` permanent** (ratified 2026-05-18 per Q13; supersedes prior `.claude/worktrees/` and `.ea/local/worktrees/` candidates) | Operator pick reverses prior recommendations. `.ea/worktrees/` aligns the worktree home with the rest of eawf state (`.ea/state.json`, `.ea/store/`, `.ea/specs/`). Memory `feedback_worktree_location` updated; `.gitignore` adds `.ea/worktrees/`. Existing worktrees under `.claude/worktrees/` migrate at operator discretion (no forced sweep). |
| **D16** | pr_merge_method default (per F-28) | (a) globally hard-coded "rebase"; (b) config-overridable in layered config; (c) per-repo only | **(b) config-overridable in layered config** (revised 2026-05-18) | The `pr_merge_method` setting belongs in the C08 field registry — config-overridable per repo / profile. The **eawf-repo profile defaults `pr_merge_method: rebase`** (matches memory `feedback_pr_merge_strategy`); other framework users pick their own per repo. C08 schema gains the field; C07b /ship reads it at PR-merge time. |
| **D10** | Branding palette source | (a) Wong 2011 (locked by C06 [1:599]); (b) custom; (c) per-profile theme | **(a) Wong 2011 + `/theme` palette verb at runtime** | C06 axes [1:599] locks Wong 2011 deuteranopia-safe; `/theme` palette verb at runtime [9:471]. Eä logotype is the literal `Eä` (capital E + a-umlaut, bold accent) per `feedback_tui_branding` memory + brand axes [1:691]. |
| **D11** | Citation grammar | (a) `[N]` indexed (current `Citation` model); (b) named refs; (c) inline | **(a)** | `[N]` numbered refs with `Citation { n, kind, ref, title, accessed, note }` rows feed `## References`. Renderer + chassis are already implemented [11:179]. |
| **D12** | Glyph set | (a) Nerd Font always (C06 [1:600]); (b) unicode fallback; (c) ASCII fallback | **(a) Nerd Font always** + ASCII-fallback for piped output | P20 TUI direction [9:22] locks Nerd Font always for interactive; the renderer falls back to ASCII glyphs when stdout is not a TTY (CI grep-ability). |

## 5. Proposed schemas, subsystems, lifecycle

### 5.1 Worktree subsystem (B8)

**Module home.** `src/eawf/worktree/` — already present [14]; C07b documents the public surface and locks behavior.

**Public surface.**

```python
# src/eawf/worktree/__init__.py — current shape (preserved)
def create_worktree(state, *, repo_root, wave_id, branch=None, base=None,
                    path=None, session_id=None, force=False,
                    explicit_base=False, default_branch=None) -> WorktreeRecord: ...

def merge_back(state, *, repo_root, wave_id, target,
               strategy="cherry_pick", continue_=False, abort=False) -> MergeBackResult: ...

def cleanup_worktree(state, *, repo_root, record_id, force=False) -> CleanupResult: ...

def list_worktrees(state, *, repo_root, include_terminal=False) -> Iterator[WorktreeListing]: ...

def wave_land(state, *, repo_root, wave_id, target_branch=None, outcome=None,
              keep_worktree=False) -> WaveLandResult: ...

def worktree_registry_lock(repo_root) -> AbstractContextManager: ...   # advisory file lock
```

**Default paths + names.**

- Worktree path: `<repo_root>/.claude/worktrees/<slug>/` where slug = `p<NN>-w<MM>` (lowercased phase + wave; strips iter). Per `feedback_worktree_location` memory: keep worktrees inside the repo's `.claude/worktrees/`. Per C07a §"Codex-compatibility" [5:560-565], v0.4 may move default to `.ea/local/worktrees/`; v0.3 stays at `.claude/worktrees/` for back-compat.
- Branch: `feature/eawf-v0.1-p<NN>-w<MM>` (matches current default [14:31-35]); the long-running phase branch is `feature/<symbol>-v<X.Y>` per AGENTS rule 15 [13].

**Branch currency gate (D3) — REWRITTEN 2026-05-18 per XB20.**

~~Original algorithm used `if rhs < 0`, which is impossible: `git rev-list --left-right --count A...B` returns `(left_count, right_count)` where both are non-negative integers.~~

Corrected semantics: `git rev-list --left-right --count <base>...HEAD` returns `(L, R)` where L = commits in base not in HEAD, R = commits in HEAD not in base. Stale-branch ⇔ R > 0 and L > 0 (diverged) OR L > 0 (we are missing upstream commits).

```
1. git fetch origin <base-branch>
2. read L R < <(git rev-list --left-right --count origin/<base>...HEAD)
   # L = commits in origin/<base> we do not have    (stale-upstream signal)
   # R = commits we have that origin/<base> does not (local-ahead signal; expected for a feature branch)
3. if L > 0 and R > 0:
     # diverged — feature branch lags origin/<base>
     refuse with envelope status=blocked + repair_commands=[
         "git fetch origin && git rebase origin/<base>"
     ]
4. if L > 0 and R == 0:
     # purely behind (fast-forwardable)
     refuse with envelope status=blocked + repair_commands=[
         "git pull --ff-only origin <base>"
     ]
5. if dirty working tree:
     emit warning; require operator confirm (AskUserQuestion) before rebase
6. else (L == 0): proceed
```

**Cherry-pick policy (D1) — REVISED 2026-05-18 per XB21.**

~~Original algorithm defaulted `target` to `state.project.default_branch or "main"`, which could land worktree commits on the wrong branch (e.g. main) if subagent dispatched from wrong context.~~

Corrected: cherry-pick captures the **parent feature branch** at dispatch time and persists it on the WorktreeRecord. Cherry-pick targets the recorded parent. **Refuse if parent is `main` / `master` / configured default** per AGENTS rule 15 branch-naming.

```python
class WorktreeRecord(_StrictModel):
    # ... existing fields ...
    parent_branch: Annotated[str, Field(pattern=r"^feature/.+")]  # captured at dispatch (per XB21); never main/master
```

```
# Cherry-pick procedure:
parent = worktree.parent_branch  # captured at dispatch; persisted on WorktreeRecord
if parent in {"main", "master"} or parent == state.project.default_branch:
    refuse with envelope status=blocked + reason="cherry-pick target cannot be default branch"

for sha in $(git -C <wt> log --reverse --oneline <base>..HEAD):
    git -C <main-worktree> checkout <parent>
    git -C <main-worktree> cherry-pick $sha
    # on conflict: stop loop, mark record CONFLICTED, surface envelope
    # operator resolves in main-worktree, git cherry-pick --continue
```

On conflict the worktree record's `WorktreeStatus` flips `ACTIVE → CONFLICTED` [4:191-195]; the envelope footer carries `repair_commands=["resolve conflicts in <main-worktree>", "git cherry-pick --continue", "eawf wt merge-back <wave> --continue"]`. `.git/CHERRY_PICK_HEAD` is intentionally preserved so the resume path stays atomic.

**Teardown rule.** `cleanup_worktree` refuses to remove a CONFLICTED or non-MERGED worktree without `--force`. Worktree directory is `git worktree remove`'d; the per-wave branch is deleted only after MERGED status is recorded.

**Registry lock.** `worktree_registry_lock` is an advisory file lock at `<repo_root>/.git/worktrees/.eawf-registry.lock`. Two parallel `eawf wt add` calls serialize on this lock to prevent git's own registry corruption.

**Lifecycle DAG (C01 §5.4 [2:891-892] + WorktreeStatus enum).**

```
   eawf wt add (under claimed wave)
            │
            v
   ┌─────────────────┐
   │     ACTIVE      │ ── wave_land cherry-pick clean ──┐
   └────────┬────────┘                                    │
            │ conflict                                    v
            v                                    ┌─────────────────┐
   ┌─────────────────┐                            │     MERGED      │
   │   CONFLICTED    │ ─ resolve + --continue ───┘└──────┬──────────┘
   └────────┬────────┘                                    │ cleanup
            │ abandon                                     v
            v                                       (worktree dir + branch removed;
   ┌─────────────────┐                              record retained for audit)
   │   ABANDONED     │
   └─────────────────┘
```

### 5.2 VCS integration (B9)

**Commit-prefix lint (D4 + D5).** Already P19-W05 hardened [15]; C07b documents the full grammar.

```python
_SUBJECT_RE = re.compile(
    r"^\[P\d{2}(-I\d{2})?(-W\d{2}|-CORE)\]\s+"
    r"(feat|fix|chore|docs|refactor|test|build|perf|ci|revert|state):\s+\S.*$"
)
_CORE_TAG_RE = re.compile(r"^\[P\d{2}(-I\d{2})?-CORE\]\s+")
_STATE_ONLY_ALLOWED = (
    ".ea/state.json",
    ".ea/store/event.jsonl",
    ".ea/store/audit.jsonl",
    ".secrets.baseline",
)
_STATE_ONLY_PREFIXES = (".ea/specs/",)
```

**Full prefix taxonomy (AGENTS rule 14 [13] + `feedback_commit_prefix_taxonomy` memory).**

| Prefix shape | Use | Allowed paths |
|---|---|---|
| `[P##-W##] feat\|fix\|chore\|docs\|refactor\|test\|build\|perf\|ci\|revert: <text>` | Planned wave deliverable (B-coded waves; ordered) | any |
| `[P##-I##-W##] ...` | Identical to above with iter segment (optional) | any |
| `[P##-CORE] state: <text>` | Phase-scope state bookkeeping | `_STATE_ONLY_ALLOWED ∪ _STATE_ONLY_PREFIXES` only |
| `[P##-W##] state: <text>` | Per-wave state bookkeeping (when wave commits state alongside code) | any |
| `[P##-I##-CORE] state: <text>` | Iter-scope state bookkeeping (rare; iter-level reactive wave) | same as `[P##-CORE]` |
| Bare `[P##] ...` | **REJECTED** | — |
| Missing `Co-Authored-By` trailer (when `vcs.coauthor.mode != disabled`) | **REJECTED** | — |

**Reactive-wave append rule.** Per `feedback_commit_prefix_taxonomy`: reactive waves get the next available `W##` number, appended chronologically. `[P##-W00]` is **REJECTED** by the lint (W00 not a valid wave index).

**Pre-commit fast-fail stack.**

```yaml
# .pre-commit-config.yaml (excerpt — actual list locked by C09)
- repo: local
  hooks:
    - id: ruff
      name: ruff lint
      stages: [pre-commit]
    - id: ruff-format
      name: ruff format
      stages: [pre-commit]
    - id: mypy
      name: mypy type-check (changed files)
      stages: [pre-commit]
    - id: detect-secrets
      name: detect-secrets baseline scan
      stages: [pre-commit]
    - id: eawf-no-pii
      name: eawf path/email PII scrub
      stages: [pre-commit]
    - id: insert-coauthor
      name: insert co-author trailer (auto)
      stages: [prepare-commit-msg]
    - id: commit-prefix-lint
      name: enforce [P##-W##|CORE] grammar
      stages: [commit-msg]
```

`AGENTS.md` rule 13 [13]: pre-commit-before-commit; hook failures are root-caused, never `--no-verify`'d.

**Coauthor trailer policy (D6, current code [16]).**

```python
# src/eawf/vcs/coauthor.py shape
CoauthorMode = Literal["runtime", "project", "disabled"]

class CoauthorConfig(BaseModel):
    mode: CoauthorMode = "runtime"                     # D6 default
    default_runtime: str = "claude"
    project: CoauthorIdentity | None = None
    trailers: dict[str, CoauthorIdentity] = ...        # claude → noreply@anthropic.com; codex → noreply@openai.com
    require_trailer: bool = True
```

**Mode behavior.**

- `runtime`: resolve identity from active runtime (env-var sniff: `CLAUDE*` → claude; `CODEX*` → codex; explicit `EAWF_COAUTHOR_RUNTIME` override). Trailer line inserted at `prepare-commit-msg`; lint rejects commits missing it.
- `project`: use `project: CoauthorIdentity` explicitly. Trailer line is the project's canonical identity (no per-runtime fan-out).
- `disabled`: never insert; lint rejects *any* existing `Co-Authored-By` trailer (strict — protects projects that contractually can't carry coauthor lines).

**PR flow.** `/ship` (phase close) calls:

```
1. uv run pre-commit run --all-files
2. eawf audit run --kind ship-gate --scope <phase-id>
3. (if audit PASS) generate PR title + body via eawf render pr_body
4. gh pr create --title "..." --body-file <stdin>
5. operator runs gh pr merge --rebase   (per feedback_pr_merge_strategy memory)
```

`feedback_pr_merge_strategy` memory locks rebase merge (never squash); squashing would obliterate the `[P##-W##]`/`[P##-CORE]` history C07b §5.2 protects.

### 5.3 Multi-repo / portfolio (B10)

**Registry file shape (current code [17]).**

```json
{
  "version": "1",
  "updated_at": "2026-05-01T12:34:56+00:00",
  "active_code": "EAWF",
  "repos": {
    "EAWF": {"code": "EAWF", "path": "/repos/eawf", "title": "Eä", "last_seen": "..."},
    "DEMO": {"code": "DEMO", "path": "/repos/demo", "title": "Demo", "last_seen": "..."}
  }
}
```

**Growth surface (D7 — locked).**

- `eawf init` adds the current repo to the registry.
- `eawf workspace add-repo <path>` adds an existing repo.
- `eawf workspace remove-repo <code>` removes (does **not** delete the repo on disk).
- `eawf workspace set-active <code>` updates `active_code`.

**Forbidden surfaces** (memory-locked): no scan, no walk, no import-from-discovery. Per `feedback_explicit_registry_only`: manual backfill is the supported bootstrap.

**Mutator (canonical writer per AGENTS rule 17 [13] / mutator-path precision).** `eawf.cli.commands.repo._persist_registry` is the sole writer (per AGENTS §"Mutator-path precision in wave success criteria" [13]). All registry-touching CLI commands route through it; ad-hoc Python writers are forbidden.

**Scope dispatch ladder (C06 [1:593]).** `cwd → workspace > repo > user`.

```
1. cwd: which dir was eawf invoked from?
2. resolve cwd to a repo via registry: find entry whose `path` is an ancestor of cwd.
3. on match: scope = repo (operate on that repo's `state.json`).
4. on no match: fall through to workspace scope (multi-repo dashboard).
5. workspace scope: render workspace summary (active_code + repos list).
6. user scope: `<local-path>` only (no per-repo state).
```

**Staleness (current code [17:218-254]).**

```python
def is_stale(entry, *, registry_mtime_at, now=None) -> bool:
    # OR-chain of three signals:
    # (a) registry mtime > STALE_AFTER (14 days)
    # (b) <entry.path>/.ea/state.json mtime > STALE_AFTER
    # (c) state.json load failed (missing or JSONDecodeError)
```

Staleness threshold = 14 days; cadence justification: eawf's own dogfood phase cadence is ~2 weeks per phase [17:46-48].

**TUI workspace dashboard (C06 surface; mentioned here for boundary clarity).** Renders the registry as a "strip" of repo cards, marks stale entries with a `(stale)` chip, lets operator pick the active repo, then dives into per-repo scope.

### 5.4 Event / audit log (B11)

**Per-kind JSONL layout (current code [19] + paths [20]).**

```
.ea/store/
├── event.jsonl           # every state mutation envelope
├── audit.jsonl           # every audit-DSL evaluation
├── decision.jsonl        # every decision row
├── incident.jsonl        # every incident timeline entry
├── estimate.jsonl        # EU estimate snapshots
├── actual.jsonl          # EU actual rollups
├── memory.jsonl          # memory appends
├── research.jsonl        # promoted research store records
├── flow.jsonl            # /flow execution checkpoints
└── <role>_report.jsonl   # one JSONL per AgentSessionRole [2:506-507]
```

**Envelope schema (current code [19]; updated 2026-05-18 per Q14 / XB07 — C07b owns the canonical Event model).**

```python
class Envelope(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    id: Annotated[str, Field(min_length=1)]
    kind: StoreKind                    # closed enum
    scope_id: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime | None = None   # EVENT-kind forces None (invariant)
    summary: Annotated[str, Field(max_length=500)]
    payload: dict[str, Any]            # per-kind validated separately
    blob_refs: list[str] = []
    artifact_ids: list[str] = []
```

**Canonical Event model (Q14 ownership 2026-05-18).** Per audit XB07 / G4, C07b owns the canonical Pydantic `Event` model. C02 streaming, C06 subscriptions, C09 telemetry projection, C11 webhook ingress all **consume** this shape — they MUST NOT define their own event envelopes.

```python
# Canonical Event — single source of truth for event envelopes
from typing import Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field

EventKind = Literal[
    "state_mutated", "wave_claimed", "wave_closed", "phase_activated", "phase_closed",
    "iter_activated", "iter_closed", "runtime_switched", "session_continued",
    "session_failover", "cache_mislayer_alarm", "dispatch_cost",
    "audit_emitted", "memory_appended", "spec_validated", "config_reloaded",
    "subscription_lag",   # backpressure signal per D7 drop-oldest
]

class EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_kind: EventKind
    actor: str                              # legacy CLI dispatch identifier; "cli" until Principal migration (XB08 / Q3)
    actor_principal_id: str | None = None   # placeholder per XB08 / Q3 (2026-05-18); populated when known
    command: str                             # CLI verb or skill name
    args_hash: str                           # SHA256 of canonical-JSON arg payload
    before_state_version: str                # state.json schema_version snapshot
    after_state_version: str                 # state.json schema_version snapshot
    error_class: str | None = None           # runtime error taxonomy (V5 retryable taxonomy)
    extras: dict[str, str | int | float | bool] = {}   # per-event-kind metadata

class Event(BaseModel):
    """Canonical event envelope. C02 streaming + C06 + C09 + C11 consume this shape."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    id: Annotated[str, Field(min_length=1)]   # 'e-<YYYY-MM-DD>-<seq>-<event_kind>'
    scope_id: str                              # wave/iter/phase URN
    occurred_at: UtcDatetime
    idempotency_key: str | None = None         # for cross-runtime re-issue dedup
    payload: EventPayload                       # validated per event_kind
```

C02 §5.7 subscription bus re-exports `Event` (not a separate envelope). C06 §reactivity consumes `Event` via `event.subscribe`. C09 telemetry projector ingests `Event` rows. C11 webhook ingress maps inbound GitHub/Linear callbacks to `Event` rows before emit.

**Append invariant.** `eawf.store.append.append_envelope` is the single canonical writer [21]:

```python
def append_envelope(path, envelope, *, timeout=5.0) -> None:
    # 1. mkdir parent if missing
    # 2. acquire portalock on sibling lockfile (timeout 5 s default)
    # 3. open path 'ab' (append-binary)
    # 4. write envelope.model_dump_json() + "\n"
    # 5. flush + os.fsync
    # 6. release lock
```

**EventPayload shape (current code).**

```python
class EventPayload(BaseModel):
    timestamp: datetime
    event_type: str               # 70+ stringly values today; C03 promotes to discriminated union
    actor: str                    # "cli" literal at 16+ sites; v0.5+ promoted to actor_principal_id
    command: str
    args_hash: str
    before_state_version: str | None = None
    after_state_version: str | None = None
    status: str
    message: str
```

**Event kind catalog (closed StoreKind enum + V5/V8/V7 extensions per C00).**

```
StoreKind (v0.3-v0.5, ordered alpha):
- AUDIT
- ACTUAL
- AGENT_RESEARCHER_REPORT
- AGENT_PLANNER_REPORT
- AGENT_EXECUTOR_REPORT
- AGENT_AUDITOR_REPORT
- AGENT_REVIEWER_REPORT
- AGENT_POLISHER_REPORT
- AGENT_OPERATOR_REPORT
- AGENT_DOMAIN_SPECIALIST_REPORT
- DECISION
- ESTIMATE
- EVENT
- FLOW
- INCIDENT
- MEMORY
- RESEARCH

New event sub-types under EVENT (V5 / V8 / V7):
- runtime_switched         (V5 [1:144-147])
- runtime_paused           (V5; vendor 429 auto-pause)
- runtime_auth_failed      (V5 RUNTIME_AUTH_ERROR halt)
- runtime_unavailable      (V5 ladder exhausted)
- session_continued        (V8 retry continue path)
- session_failover         (V8 continue→fresh fallback)
- session_handle_pruned    (V8 TTL sweep [3:901])
- cache_mislayer_alarm     (V8 cache-control interplay [5:599-605])
- dispatch_cost            (V7 cost-ledger emit [10:117-128])
- daemon_service_enabled   (V6)
- daemon_service_disabled  (V6)
- wal_recovery             (V1 crash-safety [3:427])
- subscription_dropped     (C02 §5.7 overflow [3:444])
- subprocess_oom_killed    (C02 §5.8 [3:465])
```

The enum is closed; new kinds require a schema_version bump (planned for v0.5+ when typed Mutation discriminated union lands per [4]).

**Compaction (current code [22]).** `compact_store(path)` dedupes by `Envelope.id`, keeping the *last* row, preserving first-seen insertion order. Atomic-write via tempfile + `os.replace`. EVENT-kind never compacted (append-only invariant — every mutation is its own row, ids are unique by construction).

**Retention policy (D8).**

- **v0.3-v0.5:** event.jsonl + audit.jsonl + every per-role report.jsonl is **forever-retained** (no compaction, no rotation). Disk usage estimate: 2 KB/wave × 30 waves/phase × 4 phases/year ≈ 240 KB/year per repo. Survivable.
- **v0.5+ snapshot+tail (P27-REPLAY [10:174-176]):** snapshot every 100 events; cold-tier old to `.ea/archive/<phase>/event.jsonl`. Hot tier stays under 10K events; reconcile sweep walks both [4:Q3].

**Projection + replay (V7 [1:191-192]).** The user-scope DuckDB (C09) reads `event.jsonl + audit.jsonl + <role>_report.jsonl` and projects:

- Tokens-burnt per session / wave / phase (from `dispatch_cost` events).
- Switchover frequency (from `runtime_switched` events).
- Cache-control health (from `dispatch_cost.cache_creation_input_tokens` vs `dispatch_cost.cache_read_input_tokens`).
- Errors + cause classification (from `Incident` rows + `runtime_*` event sub-types).

Projection is *rebuildable*: drop the DuckDB and replay every JSONL → identical projected table. The JSONL is canonical.

**Audit-replay (C01 §5.6 [2:1318-1325]).** Four-link evidence chain — claim site (state.json) → typed audit payload (audit.jsonl) → typed agent report body (<role>_report.jsonl) → mutation envelope (event.jsonl). C07b ensures every link is on-disk, append-only, replayable.

### 5.5 Render / envelope system (B12)

**OutputEnvelope (current code [18]).**

```python
class OutputEnvelope(BaseModel):
    header: EnvelopeHeader    # skill, scope_id, session, started_at, finished_at, status, instrument_probe
    body: str | dict          # markdown string OR typed body model
    footer: EnvelopeFooter    # persisted_artifacts, persisted_store_records, state_mutations,
                              # evidence_refs, next_valid_actions, warnings, repair_commands?
```

**EnvelopeStatus (D9).** Closed Literal: `ok`, `needs_user`, `blocked`, `failed`, `partial` [18:48].

**Repair commands invariant.** Required when `header.status ∈ {blocked, failed}` [18:144]. The CLI surface renders them as "next steps"; the TUI surface renders them as a button row in the failure modal.

**Markdown ⇄ JSON round-trip.** `to_markdown(env)` + `from_markdown(md)` are exact inverses per [18:23-26]:

```python
from_markdown(to_markdown(env)) == env
to_markdown(env) == to_markdown(from_markdown(to_markdown(env)))
```

Markdown form: YAML frontmatter (header) → raw body → HTML-comment block carrying footer YAML. `yaml.safe_dump(sort_keys=True)` ensures determinism.

**Artifact chassis (current code [11]).**

```
## Summary             # 3-5 sentences, plain prose
<body>
## References          # dense [N] citation rows
[1] <ref> — <title> (<note>)
[2] ...
## Provenance          # kind, record_id, scope_id triplet
- kind: <StoreKind.value>
- record_id: <store URN>
- scope_id: <state URN>
## Scrub               # status flag for promotion-safe artifacts
- status: clean
```

Local drafts under `.ea/local/` carry the sentinel `<!-- eawf-template: <kind> -->`; promoted artifacts under `.ea/artifacts/` do not. The promotion-gate validator [8:428-481] fails closed when promoting an artifact missing chassis sections.

**Citation model (current code).**

```python
class Citation(BaseModel):
    n: Annotated[int, Field(ge=1)]
    kind: Literal["code", "web", "paper", "doc", "artifact"]
    ref: str                        # repo-relative path OR external URL OR Eawf URN
    title: str | None = None
    accessed: UtcDatetime | None = None
    note: str | None = None
```

**Reference scrub rules** (AGENTS §"Artifact chassis and citations" [13]):

- Repo-relative paths only OR external URL OR Eawf URN.
- Absolute local paths fail validation before promotion.
- Host-local URLs fail validation.
- PII fails validation.

**Per-kind renderer matrix.**

| Kind | Renderer module | Output shape |
|---|---|---|
| Research brief | `eawf.render.research.render_brief` | chassis + topic + findings + sources + Decision section |
| Audit report | `eawf.render.audit_report.render_audit` | per-wave verdicts → per-criterion verdicts → phase verifications → out-of-scope → verdict |
| Agent report | `eawf.render.agent_report.render` | per-role typed body + verdict + evidence_refs + followups |
| Plan view | `eawf.render.plan_view.render_plan` | rendered from state — DAG + wave table + dependency graph |
| PR body | `eawf.render.pr_body.render_pr_body` | phase summary + test plan + phase deliverables |
| Skills | `eawf.render.skills.render_skill_md` | per-runtime SKILL.md body |
| Agents | `eawf.render.agents.render_agent_md` | per-runtime agent body (Claude only; Codex nests; OpenCode at `.opencode/agent/`) |
| Hooks | `eawf.render.hooks.render_hook_sh` | per-runtime bash wrapper |
| AGENTS.md | `eawf.render.agents_md.render` | managed-region body composition |
| Release notes | `eawf.render.release_notes.render` | per-phase summary aggregate |

**Format matrix.** CLI verbs accept `--md` (default), `--json`, `--yaml`. Daemon RPC always returns JSON (envelope serialized); CLI optionally renders to markdown via `to_markdown`. The renderer is pure; CLI handlers own stdout writes.

### 5.6 Branding (B13)

**Logotype (D10, `feedback_tui_branding` memory).**

- Literal string: `Eä` — capital E + lowercase a-umlaut (U+00E4).
- TUI header position: **outside-left** of the scope breadcrumb (`Eä  workspace > repo > P20 > I03 > W01`).
- Style: bold + accent color (default Wong-orange `#E69F00` per §8 Q6 ratification 2026-05-17).

**Wong 2011 deuteranopia-safe palette (D10).**

```yaml
# src/eawf/render/palette.yaml — proposed home
palette:
  name: "Wong 2011 deuteranopia-safe"
  source: "Wong B. (2011) Color blindness. Nat Methods 8, 441."
  schema_version: "1"
  colors:
    black:          "#000000"
    orange:         "#E69F00"
    sky_blue:       "#56B4E9"
    bluish_green:   "#009E73"
    yellow:         "#F0E442"
    blue:           "#0072B2"
    vermillion:     "#D55E00"
    reddish_purple: "#CC79A7"
  semantic_map:
    accent:        orange          # Eä logotype + primary action chips (per §8 Q6 ratification 2026-05-17 — flipped from blue)
    success:       bluish_green    # status=ok, audit PASS, wave CLOSED
    warning:       yellow          # status=partial, warning chips, stale (re-assigned: warning was orange before §8 Q6 flip)
    error:         vermillion      # status=failed, audit MAJOR
    needs_user:    sky_blue        # status=needs_user pause-for-input (re-assigned: needs_user was yellow before §8 Q6 flip)
    info:          blue            # neutral chips, in_progress (re-assigned: info was sky_blue before §8 Q6 flip; blue now info-semantic since accent moved to orange)
    accent_alt:    reddish_purple  # secondary highlight (rare)
    foreground:    black           # default text
```

Hex values from Wong 2011 Nature Methods 8:441. Operator-confirmed color-blind safe; no CB switch needed in v0.3-v0.5 per C06 NG [1:609].

**Glyph set (D12).** Nerd Font (NF) always for interactive TTY; ASCII fallback for piped output.

```yaml
glyphs:
  brand:               "Eä"          # the literal logotype
  scope_separator:     " ❱ "          # NF U+2771 (medium right-pointing parenthesis)
  ascii_separator:     " > "          # piped fallback
  status_ok:           " "          # NF U+F00C (check) NF=true
  status_needs_user:   " "          # NF U+F128 (question)
  status_blocked:      " "          # NF U+F254 (hourglass)
  status_failed:       " "          # NF U+F00D (x-mark)
  status_partial:      " "          # NF U+F071 (warning triangle)
  spinner:             "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # braille animation [9:22]
  ascii_spinner:       "|/-\\"
```

**Status-priority constants.**

```python
# src/eawf/render/status_priority.py
STATUS_PRIORITY: dict[str, int] = {
    "failed":     0,   # highest priority (red)
    "blocked":    1,
    "needs_user": 2,
    "partial":    3,
    "ok":         4,   # lowest priority (green)
}
```

Used by chip-aggregation widgets in C06 (which color wins when multiple scopes show on screen).

**Theme switching.** `/theme` palette verb at runtime [9:471] cycles through palettes; Wong 2011 is default. Custom palettes via `<local-path>` (same shape as Wong default); profile composition picks which palettes are available.

## 6. Failure modes + named edge cases

| # | Failure mode | Trigger | Detection | Repair |
|---|---|---|---|---|
| F1 | Worktree path collision | Two parallel `eawf wt add` calls with same wave id | `worktree_registry_lock` serializes; second caller waits + re-checks; if record already exists, refuses with InvalidInput | Operator chooses to reuse or pick different wave id |
| F2 | Cherry-pick conflict mid-loop | Mid-replay file conflict | Loop halts; record → CONFLICTED; envelope footer carries repair_commands | Operator resolves in main-worktree, `git cherry-pick --continue`, `eawf wt merge-back --continue` |
| F3 | Branch currency stale at `/prep` | Feature branch behind base by N commits | gate fetches + compares; refuses with status=blocked + rebase repair command | Operator runs `git rebase` then re-issues `/prep` |
| F4 | `[P##] bare prefix in commit message | Operator commits without W##/CORE suffix | commit-msg hook lint rejects with diagnostic | Operator rewrites the commit message with `git commit --amend` |
| F5 | `[P##-CORE]` commit touches code path | Operator runs CORE commit with src/ files staged | commit-msg hook lint rejects with explicit "non-state paths" diagnostic | Operator separates state-bookkeeping commit from code commit (-W## or another -CORE) |
| F6 | Missing co-author trailer when required | `vcs.coauthor.mode=runtime` + trailer missing | commit-msg hook lint rejects; suggests prepare-commit-msg auto-insert path | Operator pastes trailer or re-runs through prepare-commit-msg |
| F7 | Co-author trailer present when disabled | `vcs.coauthor.mode=disabled` + trailer present | commit-msg hook lint rejects (strict disable) | Operator removes trailer or changes mode |
| F8 | Registry corrupted (JSON decode error) | `<local-path>` invalid JSON | `read_registry` raises RegistryReadError; TUI strip renders empty placeholder | Operator runs `eawf workspace doctor` to rebuild from per-repo states |
| F9 | Repo state.json missing in registry entry | Registry lists repo path that has no `.ea/state.json` | `is_stale` returns True (case c); TUI shows `(stale)` chip | Operator runs `eawf init` in the missing repo, OR `eawf workspace remove-repo <code>` |
| F10 | Registry growth attempt via scan | Bug in tooling tries to walk filesystem to populate registry | Memory `feedback_explicit_registry_only` forbids; reviewer rejects PR; lint hook (proposed C09) catches scan in source | Refuse the change; require explicit init / add-repo |
| F11 | Event-log append fails due to disk full | `append_envelope` raises OSError during write | LockConflict / OSError propagates to daemon; mutation rolls back (WAL stays pending) | Operator clears disk; daemon WAL replay [3:413] re-runs the mutation |
| F12 | Compaction loses events | Bug in `compact_store` drops rows | EVENT-kind never compacted (invariant); other kinds compact idempotent + tested | Replay JSONL pre-compaction copy from git history (everything under `.ea/store/` is committed) |
| F13 | Envelope status `failed` without repair_commands | Caller emits failed status missing the required field | Pydantic validator raises ValidationError [18:144] | Caller fixes the emit; CI catches in test |
| F14 | Markdown round-trip drift | `to_markdown(from_markdown(md)) != md` | Property test catches in CI [18:23-26] | Renderer fix; usually YAML key ordering |
| F15 | Citation `ref` carries absolute local path | Artifact promoted with an absolute-home-path pattern in references | Scrub validator catches at promotion [8:428-481] | Author rewrites citation to repo-relative or URL |
| F16 | Brand glyph fails to render | Terminal doesn't support Nerd Font | Renderer detects via `EAWF_STATUSLINE_THEME=ascii-fallback` env / non-TTY stdout; falls back to ASCII glyph (per F-24) | Automatic |
| F17 | Palette load error | Operator's custom theme YAML invalid | Pydantic ValidationError; renderer falls back to Wong 2011 default | Operator fixes the theme YAML |
| F18 | Worktree default path conflict with v0.3 → v0.4 move | Codex compat brief [5:560-565] suggests `.ea/local/worktrees/`; v0.3 stays at `.claude/worktrees/` | Migration: doctor detects both paths populated; warns operator | Operator picks one default and moves the other (v0.4 migration; v0.3 untouched) |

## 7. Migration plan

### 7.1 Net-new modules

| File | Surface | Phase | LOC est. |
|---|---|---|---|
| `src/eawf/render/palette.yaml` | Wong 2011 palette definition + semantic map | P22-W08 | ~50 |
| `src/eawf/render/status_priority.py` | Status-priority constants | P22-W08 | ~30 |
| `src/eawf/render/brand.py` | Logotype + glyph set + theme switcher | P22-W08 | ~150 |
| `src/eawf/store/retention.py` | Snapshot-and-tail tooling (v0.5+ deferred) | P27-REPLAY | ~250 |

### 7.2 Existing surfaces that change

- `src/eawf/worktree/create.py` — default path stays `.claude/worktrees/` for v0.3; configurable via `worktree.root` config field; v0.4 migration moves default to `.ea/local/worktrees/` per Codex compat brief [5:560-565].
- `tools/commit_prefix_lint.py` — already P19-W05 hardened [15]; no grammar change in v0.3-v0.5. `state.json` mutator path becomes daemon-mediated under V1 [1:24-53] but the lint sees only commit messages + staged paths so it doesn't change.
- `src/eawf/registry/__init__.py` — read-only today [17]; the canonical writer `_persist_registry` [13] in CLI commands stays the single mutator path. C07b adds a daemon RPC method `registry.read` that the TUI subscribes to for live updates.
- `src/eawf/store/append.py` — already canonical single writer [21]; under V1 the daemon owns this — CLI tooling that today calls `append_envelope` directly will be wrapped by a daemon RPC `event.append` in P21-PREREQ.
- `src/eawf/render/envelope.py` — no schema change. Body field gains optional typed bodies (already in place via Pydantic models under `eawf.skills.bodies`).

### 7.3 Per-phase rollout

| Phase | Surface | Scope |
|---|---|---|
| **P21-PREREQ** [10:159-163] | typed Mutation discriminated union + HLC envelope | precedes daemon-mediated event-append (V1) |
| **P22-KERNEL** [10:160-164] | RuntimeAdapter + dispatch + V5/V8 event sub-types emitted | C07a delivery; C07b consumes via new event kinds |
| **P22-W08** | brand module + palette YAML + status priority + glyph set | C07b §5.6 delivery |
| **P22-W09** | daemon RPC `registry.read` + TUI subscription | C07b §5.3 dynamic registry |
| **P23-COST** [10:165-167] | `dispatch_cost` event kind + cost-ledger emit | C07b §5.4 event-catalog extension |
| **P26-TUI** [10:172-173] | TUI consumes envelope + brand surface; `/theme` palette verb | C07b §5.5 + §5.6 surface |
| **P27-REPLAY** [10:174-176] | event-source rebuilder + snapshot+tail (D8) | C07b §5.4 retention v0.5+ |

### 7.4 Rollback

- C07b is mostly *naming + invariant* — no breaking schema changes. Each new event sub-type adds rows to JSONL but the closed StoreKind enum stays back-compat.
- Brand module is purely additive (palette YAML; if the renderer fails to load, fall back to ASCII).
- Worktree path migration (v0.4) is reversible via config; v0.3 untouched.

## 8. Open questions for operator

### Q1 (resolved via blitz [24]) — Worktree default path (D-cross v0.3 → v0.4). **Locked: option (a) — v0.3 stays at `.claude/worktrees/`; v0.4 flips default to `.ea/local/worktrees/` + doctor warning + `eawf wt migrate-default` verb.**

Blitz findings [24]:

- **On-disk audit:** zero worktrees under either `.claude/worktrees/` or `.ea/local/worktrees/` in the current repo. Both dirs exist + are empty; both implicitly gitignored via parent-dir rules. Migration cost on operator's filesystem is therefore zero today.
- **Code-path cost:** 13 hard-coded `.claude/worktrees` hits across 7 files (5 in tests, 2 in lib docstrings/help). Migration cost = 3–5 lib code changes + 8–10 test fixture updates. Public API signatures unchanged (`create_worktree`'s `path` is already optional).
- **v0.3 ship:** keep `.claude/worktrees/` default locked; add `worktree.root` config field (no default change yet). Zero code changes required this phase.
- **v0.4 ship:** flip default to `.ea/local/worktrees/`; add doctor warning when both dirs populated; add `eawf wt migrate-default` verb to move on-disk worktrees + update `state.worktrees[*].path` rows.

### Q2 — Event-log retention default (D8)

### Q2 (resolved via blitz r2 [25]) — Event-log retention. **Locked: forever-retention safe through v0.5; snapshot+tail deferred to P27-REPLAY as planned [10:174-176]; phase-boundary cadence (option b) when snapshot lands.**

Blitz r2 measurements [25]:

- Current: 371 KB total across all `.ea/store/*.jsonl`; 694 event rows over 22 phases (P00-P21). Recent trend 7.8 events/phase (baseline 4.1; P20 TUI spike at 20).
- Year-5 worst-case projection: 1.23 MB (assumes 130 phases at 7.8 events/phase × ~600 B/row). Conservative: 0.25 MB. Realistic: 0.48 MB.
- Year-8+ before any disk concern fires.
- Snapshot threshold of 100 events from features-deep [4:Q3] already passed 6× over historically but disk impact remains negligible — confirms threshold should be policy not forced trigger.

**v0.5+ snapshot+tail cadence (when P27-REPLAY ships):** phase-close boundary (option b — matches phase-bundled delivery [3:75-78]).

### Q3 (resolved via blitz r3 [28]) — Coauthor mode default. **Locked: `runtime` mode confirmed.**

Blitz r3 git-log audit [28]:

- **50/50 recent commits** carry the canonical Claude trailer `Co-Authored-By: Claude <noreply@anthropic.com>`.
- Zero non-canonical trailers — no project-mode identity overrides, no disabled-mode periods, no drift.
- `.ea/config.yaml` has no `vcs.coauthor` override; defaults resolve to `mode: "runtime"` + `default_runtime: "claude"`.

**Followup:** AGENTS.md hygiene addendum to document the implicit lock (absence of `vcs.coauthor` override) as drift-prevention. Reviewer to flag any future addition of `vcs.coauthor.mode: project` without operator AUQ.

### Q4 (resolved via blitz r2 [26]) — Commit-prefix grammar `[P##-I##-CORE]`. **Locked: option (a) — allow current regex; iter-CORE is intentional, in active use.**

Blitz r2 git-log audit [26]:

- 14 commits use `[P##-I##-CORE]` form (all in P14, iters I02-I04).
- Commit `7741491` explicitly widened the lint regex to support iter-scoped CORE commits for multi-iter phases.
- Use case: disambiguating state bookkeeping tied to a specific iteration boundary (e.g., close I02 → open I03 transition).
- No lint change needed; regex + test coverage already correct.

Updates `feedback_commit_prefix_taxonomy` memory note from "CORE for phase-meta only" to "CORE for phase-OR-iter-scope state bookkeeping; iter-CORE rare but intentional for multi-iter phase boundaries".

### Q5 (resolved via blitz r2 [27]) — Registry path on Windows. **Locked: option (b) — `<local-path>` on every OS (Windows → `%USERPROFILE%\.eawf\registry.json` via `Path.home()`). Original recommendation (a) FLIPPED.**

Blitz r2 findings [27] override the original `%APPDATA%` bias on four grounds:

1. **CLI-tool precedent.** git, claude-code, codex, opencode all use `%USERPROFILE%\.<tool>\` on Windows. npm is the outlier with `%APPDATA%\npm` and is contested upstream.
2. **Daemon namespace consistency.** C02 §5.10.3 D5 places eawfd daemon artifacts at `<local-path>` on Windows (named pipe `\\.\pipe\eawfd-<user>` for IPC). Registry under `%APPDATA%` would fragment the `<local-path>` namespace and break daemon-runtime symmetry.
3. **Win11 deprecated UWP roaming-AppData auto-sync.** `%APPDATA%`'s original motivation (cross-device user-config roaming) no longer applies; modern Windows treats it as `%LOCALAPPDATA%` with extra indirection.
4. **Zero migration cost.** `src/eawf/registry/__init__.py:51-58` already does `Path.home() / '.eawf' / 'registry.json'` — no per-OS branch exists. Adopting `%APPDATA%` requires migration with negative operator value.

**Future-proofing:** add optional `EAWF_HOME` env override (parallel to `$CODEX_HOME`) for corporate / CI / Store-sandbox escape hatches. Not required for v0.3 default.

**Per-OS registry path matrix (locked):**

| OS | Path | Resolver |
|---|---|---|
| Linux | `<local-path>` | `Path.home() / '.eawf' / 'registry.json'` |
| macOS | `<local-path>` | same |
| Windows | `%USERPROFILE%\.eawf\registry.json` | `Path.home() / '.eawf' / 'registry.json'` (Python `Path.home()` returns `%USERPROFILE%` on Windows since 3.8) |
| All OSes | `$EAWF_HOME/registry.json` when env set | `Path(os.environ['EAWF_HOME']) / 'registry.json'` |

### Q6 (resolved via batch ratification 2026-05-17) — Wong palette accent default. **Locked: (b) Wong-orange `#E69F00`** — operator flipped from the brief's recommendation. Higher contrast on dark themes + deuteranopia-safe (Wong 2011 confirmed). §5.6 brand `semantic_map.accent: blue` flips to `accent: orange`. Eä logotype renders Wong-orange (bold). Wong-blue retains `info` semantic (neutral chips, in_progress).

### Q7 (resolved via batch ratification 2026-05-17) — `/theme` palette verb scope. **Locked: (b) per-user persistent now (v0.3).** Operator flipped from the brief's `(a) session-only v0.3 → (b) v0.4` two-phase recommendation. v0.3 wires `/theme` directly to the layered-config writer (`<local-path> tui.theme`); per-user persistence takes effect immediately. Adds `tui.theme: str = "wong-2011"` field to the user-scope config schema. Layered-config writer surfaces `tui.theme` as a writable layer per `feedback_naming_conventions` memory + `eawf.cli.commands.config._save_value_to_layer` canonical mutator.

### Q8 (resolved via blitz r4 [31]) — Glyph fallback semantics. **Locked: (a) ASCII fallback via Rich auto-downgrade — already in place.**

Blitz r4 audit [31]:

- `EAWF_NO_NF` env var does NOT exist; only `EAWF_STATUSLINE_THEME` is implemented.
- TTY detection in place via `sys.stdout.isatty()` at `src/eawf/tui/app.py:243-244`.
- Non-TTY renders use `Console(force_terminal=False)` → Rich's native ASCII downgrade (box-drawing → ASCII; per-glyph fallback handled by Rich).
- 3 statusline themes already defined: `default`, `powerline`, `ascii-fallback` — `EAWF_STATUSLINE_THEME=ascii-fallback` gives explicit zero-color zero-glyph rendering.
- 20+ golden snapshots use `force_terminal=False`; CI passes; no glyph corruption observed.

**Followup:** add explicit ASCII-only assertion test for non-TTY output validation (currently passes but doesn't assert absence of high-bit glyphs). C07b §5.6 may rename `EAWF_NO_NF` reference to point at the existing `EAWF_STATUSLINE_THEME=ascii-fallback` surface to match implementation.

### Q9 (resolved via blitz r3 [29]) — Per-role report JSONL location. **Locked: flat layout confirmed.**

Blitz r3 codebase audit [29]:

- All 17 `StoreKind` enum values (`audit`, `decision`, `event`, `flow`, `research`, `memory`, `incident`, `estimate`, `actual` + 8 agent-report kinds) resolve to flat `.jsonl` files under `.ea/store/` with no subdirectories.
- `src/eawf/store/paths.py::store_path()` has no branching for nesting: `<state_dir>/store/<kind.value>.jsonl`.
- Agent reports follow the same pattern: `.ea/store/executor_report.jsonl`, `.ea/store/auditor_report.jsonl`, etc.
- URN semantics already isolate roles via the `id` field (`urn:eawf:v1:store:P20/executor_report/AR-executor-W01-01`); filesystem nesting is unnecessary.

Flat layout is the canonical store-subsystem architecture; ratify in v0.3 ship.

### Q10 (resolved via blitz r3 [30]) — PR merge strategy enforcement. **Locked: operator-manual rebase confirmed; `eawf ship` stays generator-only.**

Blitz r3 PR merge history audit [30]:

- **100% rebase-landed:** all 17 shipped PRs + 100/100 recent main commits carry the `[P##]` prefix (rebase pattern).
- **Zero squash regressions:** no `Merge pull request #N from ...` commits on main.
- **No auto-merge wiring:** CI workflow files have no `gh pr merge` hooks; the `/ship` skill is generator-only (v0.1 stub).

**Followup (v0.3 ship-blocker):** config default `vcs.pr_merge_method: "merge"` is misaligned with observed discipline. Realign to `"rebase"` before v0.3 ship. Tracked as P20-CORE candidate or per-phase reactive wave.

### Q11 (resolved via blitz r4 [32]) — Brand glyph in commit messages. **Locked: (a) ASCII-only subject; commit-prefix lint to enforce; body MAY carry non-ASCII for audit-reference markers (current pattern: 3 commits use `🔵` emoji harmlessly).**

Blitz r4 audit [32]:

- 50/50 commits sampled = 100% ASCII subjects (zero non-ASCII bytes in subject).
- Zero `Eä` logotype in any commit subject or body in eawf history.
- 3 commits use `🔵` emoji in body (audit-reference markers; harmless; cross-platform git renders fine).
- **Lint gap:** `tools/commit_prefix_lint.py` does NOT enforce ASCII on subjects today — foot-gun open.

**Followup (v0.3 ship-blocker companion):** add `subject.encode('ascii')` check to `commit_prefix_lint.py:107` with diagnostic for non-ASCII codepoints; document in AGENTS.md rule 14 (commit-prefix subject ASCII-only). Body remains non-ASCII-tolerant for audit markers + ASCII art per existing convention.

## 9. References

[1] `.ea/local/research/long-term/2026-05-16-c00-spec-index.md` — C00 spec architecture index; V1..V8 verdicts; C07 scope ([1:648-712]); brand element specs ([1:691]); split prompt ([1:709]).
[2] `.ea/local/research/long-term/2026-05-16-c01-foundations.md` — C01 foundations; entity catalog (§5.3); URN scheme (§5.2); persona authority matrix (§5.5); trust + audit-replay model (§5.6 [2:1318-1325]); WorktreeStatus enum (§5.4.19 [2:1235]).
[3] `.ea/local/research/long-term/2026-05-16-c02-daemon-topology.md` — C02 daemon brief; IPC method catalog (§5.3); WAL + crash safety (§5.6 [3:402-410, 413-432]); subscription bus (§5.7); runtime fallback state machine (§5.12); session-handle table (§5.13).
[4] `.ea/local/research/long-term/2026-05-15-long-term-features-deep.md` — long-term features deep; KV-cache mis-layer alarm (§605); event-log compaction Q3; cache-control 5m breakpoint.
[5] `.ea/local/research/2026-05-13-codex-eawf-compatibility.md` — Codex compat brief; coauthor strict-disable (Q6 [5:252-284]); worktree policy two-class (Eä-managed vs harness-managed [5:540-566]); harness-scope customization.
[6] `.ea/local/research/2026-05-12-artifact-structure-standardization.md` — artifact chassis (Summary / References / Provenance / Scrub); typed `Citation` model; promotion scrub gate.
[7] `.ea/local/research/2026-05-13-artifact-structure-standardization-v2.md` — chassis v2 + draft-sentinel behavior + validator modes.
[8] `.ea/local/research/2026-05-12-artifact-structure-standardization.md:428-481` — draft sentinel + promotion validator behavior.
[9] `.ea/local/research/2026-05-16-p20-tui-design-direction.md` — P20 TUI direction; deuteranopia-safe Wong 2011 palette [9:429]; `/theme` palette verb [9:471]; Nerd Font always [9:22]; braille spinner.
[10] `.ea/local/research/long-term/2026-05-15-long-term-roadmap-synthesis.md` — locked roadmap synthesis; SDK rejection ([10:19-22]); harness session-log paths ([10:121-122]); P22-KERNEL ([10:159-164]); P23-COST + P26-TUI + P27-REPLAY phases ([10:165-176]); harness session-log read-only ingestion ([10:117-128]).
[11] `src/eawf/render/artifact_chassis.py` — chassis renderer (Summary / References / Provenance / Scrub).
[12] `src/eawf/artifacts/references.py` — `Citation` model.
[13] `AGENTS.md` — non-negotiable rules (rule 11 worktree discipline; rule 13 pre-commit-before-commit; rule 14 commit prefix; rule 17 naming conventions including mutator-path precision; rule 18 artifact chassis and citations).
[14] `src/eawf/worktree/{__init__,create,merge_back,wave_land,cleanup,git,locks}.py` — current worktree subsystem.
[15] `tools/commit_prefix_lint.py` — P19-W05 hardened commit-prefix lint; `_SUBJECT_RE`; `_CORE_TAG_RE`; `_STATE_ONLY_ALLOWED` / `_STATE_ONLY_PREFIXES`.
[16] `src/eawf/vcs/coauthor.py` — coauthor trailer policy (CoauthorMode runtime/project/disabled; runtime aliases; `resolve_coauthor_trailer`).
[17] `src/eawf/registry/__init__.py` — read-only registry surface (Registry, RegistryRepoEntry, read_registry, registry_mtime, is_stale; STALE_AFTER=14d).
[18] `src/eawf/render/envelope.py` — OutputEnvelope shape (EnvelopeHeader, EnvelopeFooter, EnvelopeStatus, EnvelopeWarning; markdown ⇄ JSON round-trip; CANONICAL_SKILL_NAMES).
[19] `src/eawf/store/envelope.py` — top-level JSONL store record Envelope (schema_version 1.0; EVENT-kind force updated_at=None).
[20] `src/eawf/store/paths.py` — canonical `<state_dir>/store/<kind>.jsonl` resolution.
[21] `src/eawf/store/append.py` — canonical `append_envelope` (per-file portalock + fsync).
[22] `src/eawf/store/compact.py` — `compact_store` (dedup by id, preserve first-seen order, atomic rewrite).
[23] `src/eawf/render/{agents,agents_md,hooks,skills,research,audit_report,agent_report,plan_view,pr_body,release_notes,wiki}.py` — per-kind renderers.
[24] `.ea/local/research/long-term/2026-05-16-c07b-blitz-worktree-path.md` — blitz brief resolving §8 Q1 (worktree default path): zero on-disk worktrees today; 13 hard-coded path hits across 7 files; v0.3 lock + v0.4 flip + doctor + `eawf wt migrate-default` verb.
[25] `.ea/local/research/long-term/2026-05-16-c07b-blitz-event-retention.md` — blitz r2 brief resolving §8 Q2 (event-log retention): 371 KB / 694 rows / 22 phases current; year-5 worst-case 1.23 MB; forever-retention safe through v0.5; phase-boundary snapshot cadence (option b) for v0.5+ P27-REPLAY.
[26] `.ea/local/research/long-term/2026-05-16-c07b-blitz-iter-core-audit.md` — blitz r2 brief resolving §8 Q4 (iter-CORE prefix): 14 commits in P14 I02-I04 use `[P##-I##-CORE]`; commit `7741491` widened lint regex intentionally; confirm allow.
[27] `.ea/local/research/long-term/2026-05-16-c07b-blitz-windows-registry-path.md` — blitz r2 brief resolving §8 Q5 (Windows registry path): flip from `%APPDATA%\eawf\` to `<local-path>` everywhere on CLI-tool precedent + C02 daemon namespace consistency + Win11 roaming-AppData deprecation + zero migration cost; add `EAWF_HOME` env override for future-proofing.
[28] `.ea/local/research/long-term/2026-05-16-c07b-blitz-coauthor-audit.md` — blitz r3 brief resolving §8 Q3 (coauthor mode default): 50/50 recent commits use canonical Claude noreply trailer; zero drift; `runtime` mode confirmed; followup AGENTS.md addendum to document implicit lock.
[29] `.ea/local/research/long-term/2026-05-16-c07b-blitz-report-layout.md` — blitz r3 brief resolving §8 Q9 (per-role report JSONL location): all 17 StoreKind values flat under `.ea/store/`; no nested subdirs in `store/paths.py`; URN id-field isolates roles; flat confirmed.
[30] `.ea/local/research/long-term/2026-05-16-c07b-blitz-pr-merge-audit.md` — blitz r3 brief resolving §8 Q10 (PR merge enforcement): 100% rebase-landed across all 17 PRs and 100/100 recent main commits; zero squash regressions; `/ship` generator-only confirmed; config anomaly `pr_merge_method: "merge"` should flip to `"rebase"` before v0.3 ship.
[31] `.ea/local/research/long-term/2026-05-16-c07b-blitz-glyph-fallback.md` — blitz r4 brief resolving §8 Q8 (glyph fallback): ASCII fallback already in place via Rich `Console(force_terminal=False)` + `sys.stdout.isatty()` detection; 3 statusline themes (`default`/`powerline`/`ascii-fallback`); 20+ golden snapshots pass. Followup: explicit ASCII assertion test + replace conceptual `EAWF_NO_NF` with existing `EAWF_STATUSLINE_THEME` ref in §5.6.
[32] `.ea/local/research/long-term/2026-05-16-c07b-blitz-commit-ascii.md` — blitz r4 brief resolving §8 Q11 (commit subject ASCII): 50/50 sampled commits 100% ASCII subjects; zero `Eä` in history; 3 commits use `🔵` emoji in body (harmless audit markers); lint gap — `commit_prefix_lint.py` doesn't enforce subject ASCII. Followup: add `subject.encode('ascii')` check + AGENTS rule 14 doc.

## 10. Provenance + Scrub

### Provenance

- `store_record=none (local-only research brief)`
- `commit=3b86f7a (parent at brief authoring time; revisions 2026-05-18)`
- `cluster=C07b`
- `consumes=C00 V1..V9 (locked 2026-05-16 [1:22-271]); C01 foundations; C02 daemon`
- `supersedes=none`
- `pairs_with=C07a (runtime / skill / agent dispatch)`
- `session=eawf-spec-c07b-vcs-worktree-events-2026-05-16`
- `last_revised=2026-05-18 (audit-driven: branch-currency math rewritten per XB20; cherry-pick parent-branch capture per XB21; D14 event-schema ownership added per Q14/XB07; D15 worktree path .ea/worktrees/ per Q13; D16 pr_merge_method config-overridable per F-28; F16 env-var EAWF_STATUSLINE_THEME=ascii-fallback per F-24; canonical Event model added in §5.4 per Q14)`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md (3 ship-blockers; 12 Codex issues)`
- `owns_event_schema=true (per Q14 / XB07; C02 streaming + C06 subscriptions + C09 telemetry + C11 webhook all consume)`
- `authority_binding=Q1 (2026-05-18): daemon = sole writer for event store (event.jsonl) + audit store (audit.jsonl); store/append.append_envelope serialized through daemon. Per Q1 supersede, the AGENTS rule 17 4th-mutator (telemetry projector) also folds into daemon internals.`
- `verdicts_load_bearing=V1 daemon-mediated mutators (state-CLI, layered-config writer, registry writer); V2 spec storage paths under .ea/specs/; V3 profile-contributed hooks; V5/V8/V7 event sub-types (runtime_switched, session_continued, session_failover, cache_mislayer_alarm, dispatch_cost, etc.); V6 OS-aware registry paths`
- `blitz_round_1=2026-05-16 — §8 Q1 resolved via blitz brief [24]: v0.3 lock at .claude/worktrees/ + v0.4 flip to .ea/local/worktrees/ with doctor warning + eawf wt migrate-default verb. Zero on-disk worktrees today; 13 hard-coded path hits across 7 files (3-5 lib + 8-10 test fixtures).`
- `blitz_round_2=2026-05-16 — §8 Q2, Q4, Q5 resolved via blitz briefs [25] + [26] + [27]: Q2 forever-retention safe through v0.5 (year-5 worst-case 1.23 MB); Q4 confirm allow [P##-I##-CORE] (14 P14 commits intentional); Q5 FLIP recommendation — <local-path> on every OS including Windows (CLI-tool precedent + C02 daemon namespace + Win11 AppData deprecation + zero migration cost); EAWF_HOME env override added for future-proofing.`
- `blitz_round_3=2026-05-16 — §8 Q3, Q9, Q10 resolved via blitz briefs [28] + [29] + [30]: Q3 runtime mode confirmed (50/50 commits canonical Claude trailer; zero drift); Q9 flat layout confirmed (all 17 StoreKind values flat; URN id-field isolates roles); Q10 operator-manual rebase confirmed (100% rebase-landed; zero squash regressions; /ship stays generator-only) + ship-blocker config-anomaly note (pr_merge_method default "merge" → flip to "rebase" before v0.3 ship).`
- `blitz_round_4=2026-05-16 — §8 Q8 + Q11 resolved via blitz briefs [31] + [32]: Q8 ASCII fallback already in place via Rich auto-downgrade + isatty detection + ascii-fallback statusline theme (followup: add explicit assertion + replace conceptual EAWF_NO_NF with existing EAWF_STATUSLINE_THEME ref); Q11 ASCII-only subject confirmed (50/50 commits clean, zero Eä in history, body MAY carry non-ASCII for audit markers); ship-blocker companion: add subject.encode('ascii') check to commit_prefix_lint.py + AGENTS rule 14 doc.`
- `batch_ratification=2026-05-17 — §8 Q6 Wong accent default flipped to orange (operator override of brief's blue recommendation; §5.6 semantic_map.accent + warning + needs_user + info re-assigned to keep palette coherent); §8 Q7 /theme scope flipped to per-user persistent in v0.3 (operator override of brief's session-only v0.3 + v0.4-persistent two-phase recommendation; adds tui.theme: str = "wong-2011" field to user-scope config schema; wires through layered-config writer). All 11 Open Questions resolved; C07b status candidate for accepted flip pending C07a parity (already done).`

### Scrub

- status: clean
- references: repo-relative or external URL only
- local paths: none (the `<local-path>`, `<local-path>`, `<local-path>`, `<local-path>` examples are home-relative templates; `.ea/store/`, `.ea/specs/`, `.ea/artifacts/`, `.ea/local/` are repo-relative)
- real emails: `noreply@anthropic.com` + `noreply@openai.com` only — public vendor-canonical machine identities (no PII); the lookup is part of the coauthor trailer policy [16] and is committed in the codebase already
- abstract placeholder names: not applicable (no mockup repos cited; project codes used are the public EAWF code)
- machine identifiers: none
- credentials / API keys: none
- vendor URLs: none in this brief body
