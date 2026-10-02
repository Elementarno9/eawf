# Upgrading from 0.6.8 to 0.7.0rc1

eawf 0.7 keeps a repository's records in a new store. The 0.6 store is *epoch 1*: one `.ea/state.json` document that every verb edits. The 0.7 store is *epoch 2*: a *generation*, a versioned snapshot under `.ea/generations/`, that the native verbs (`milestone`, `batch`, `task`, `run`) change. A 0.6.8 tree refuses every writing verb until it is cut over, once, by `eawf migrate epoch2`. The steps below run in the repository root, in order, with 0.7 installed. `PROJ` stands for your project code, as `eawf status` prints it; it also names the scratch paths beside the repository, so repositories upgraded side by side never share one.

## 0. Stop the 0.6.8 daemon

A 0.6.8 daemon keeps running after 0.7 is installed. Where 0.7 reaches it, it replaces it: before the first call it compares the daemon's version with its own, restarts an older daemon as 0.7 and logs `daemon restarted stale`, and refuses a newer one with a message naming both versions and `eawf daemon restart`. It reaches a daemon on a runtime directory pinned by `EAWF_RUNTIME_DIR`, which both releases share. Without that pin 0.7 runs one daemon per tree under `~/.eawfd/trees/` and never dials the address 0.6.8 used, `~/.eawfd/eawfd.sock` (`$XDG_RUNTIME_DIR/eawfd/eawfd.sock` on Linux when that is set), so stop a daemon there by hand; `eawf doctor` lists one left running under `stray_daemons`. `eawf daemon stop` is also the manual fallback for a daemon 0.7 refused.

```text
EAWF_RUNTIME_DIR=~/.eawfd eawf daemon stop
```

## 1. Back up, then close every live session

```text
git add -A && git commit --allow-empty -m "Record the tree before the 0.7 upgrade"
cp -R .ea ../PROJ-ea-before-epoch2
eawf migrate status
```

The commit is git's restore point; `--allow-empty` makes it even when everything is already committed. If `eawf migrate status` lists a pending schema step, run `eawf migrate` and commit the result. The cutover refuses while any agent session or worktree is still live, because a second writer could race it: stop your agent runtimes, read the live session ids with `eawf --json status` (the `active_sessions` list), close each one with `eawf session close <SES-id>`, and retire stale worktree rows with `eawf worktree reconcile`. Step 2 commits what these change.

## 2. Opt in, stage, register and plan

```text
eawf migrate epoch2 --opt-in --target-root .ea
git add .ea && git commit -m "Opt the tree into the epoch-2 cutover"
eawf migrate epoch2 --stage-to ../PROJ-stage --workspace-key PROJ --project-key PROJ
eawf workspace add PROJ --home PROJ --title "my project"
touch ../PROJ-allowlist.txt
eawf migrate epoch2 --plan --snapshot-root ../PROJ-stage --allowlist ../PROJ-allowlist.txt --workspace-key PROJ --project-key PROJ --repository-key PROJ --default-track-key TRK-PROJ-CORE
```

The opt-in takes an eawf backup of `.ea` (under `~/.eawf/backups/`) and writes `.ea/epoch2-opt-in.json`, the *opt-in declaration* that pins it; the apply refuses a tree with no declaration, or one whose pinned backup no longer verifies. `--stage-to` copies the committed `.ea` files at HEAD into a new or empty directory outside `.ea`; after any later commit, delete `../PROJ-stage` and stage again. A *workspace* groups repositories; every imported record is addressed under its key. The allowlist names epoch-1 symbols that were renamed or deliberately deleted; an empty file is valid. `--plan` writes nothing and prints an *approval digest*, the fingerprint of exactly this plan.

0.6 phases and goals name no *Track*, the 0.7 line of work a Milestone belongs to, and the importer does not guess one. `--default-track-key` is your answer for every record whose source names none: the plan records it in the manifest, and the apply refuses to run without it when such records exist. It does not create the Track, and those records arrive with no Track; the plan says so in its `declared Track:` line.

## 3. Apply, refresh and commit

```text
eawf migrate epoch2 --apply --plan-digest <approval digest> --target-root .ea --snapshot-root ../PROJ-stage --allowlist ../PROJ-allowlist.txt --workspace-key PROJ --project-key PROJ --repository-key PROJ --default-track-key TRK-PROJ-CORE
eawf sync
eawf plugin install claude
git add -A && git commit -m "Cut the tree over to epoch 2"
eawf repo register . --yes
eawf status
```

The apply re-plans and refuses if the digest no longer matches or the live `.ea` differs from the staged copy; commit, stage and plan again. If the plan reports unresolved rows, pass each address it lists with `--accept-unresolved`. A stopped apply is finished or undone by `eawf migrate epoch2 --recover --target-root .ea`.

The apply rewrites the managed `.gitignore` block so it ignores every machine-local path of the generation: `local/` (this machine's in-flight Task status), `indexes/` (rebuilt from the ledgers), the cutover journal and the restore copies of pre-cutover files git history already holds. Step 1 left the tree committed, so `git add -A` stages only what the upgrade wrote: the pointer to the selected generation, the epoch marker, the generation's document and its append-only ledgers, and the files `eawf sync` and `eawf plugin install claude` re-render. `.ea/state.json` stays as the frozen epoch-1 record. `eawf sync` re-renders `AGENTS.md`, the rules under `docs/rules/` and the `.gitignore` block if stale (`eawf doctor` warns when it is); `eawf plugin install claude` replaces the 0.6.8 Stop hook with the ten 0.7 hooks in `.claude/settings.json`. Review the rest with `git status`. Then run `eawf ui`; `eawf status` lists the open Tasks of the generation.

## Remove retired config leaves

A stock 0.6.8 `.ea/config.yaml` sets seven leaves 0.7 no longer reads: `estimation.buckets`, `mcp.enabled`, `project.code`, `project.domains`, `project.goals`, `project.slug` and `project.title`. They are ignored, so removing them only tidies the file. `eawf config unset` refuses them as unknown keys; delete the `estimation:`, `mcp:` and `project:` blocks from `.ea/config.yaml` by hand and check the file with `eawf config validate`. `eawf --no-input doctor --fix` previews the doctor's repair, whose `config.normalize` action names each retired leaf in each config layer; `eawf doctor --fix` applies every listed action after one confirmation, so read the whole list first, as it also stops stray daemons and makes any other repair the doctor found.

## Create your first Task

A Task's URN is `eawf://<workspace>/<project>/<repository>/task/<key>`, and its key is the project code, a hyphen and a four-digit ordinal: `PROJ-0001`. The smallest create document the tree admits is a JSON object with `key`, `priority` and `intent`:

```text
printf '%s\n' '{"key": "PROJ-0001", "priority": "P2", "intent": "Add a greeting module"}' > ../PROJ-task.json
eawf task create eawf://PROJ/PROJ/PROJ/task/PROJ-0001 --expected-revision 0 --idempotency-key create-PROJ-0001 --actor OPERATOR --from-spec ../PROJ-task.json --yes
```

`--expected-revision` is the tree's committed canonical sequence, the count of moves committed to the generation since the cutover. It is 0 right after the cutover, imported records included, and each committed move adds one: this create prints `result.canonical_sequence: 1`, so the next passes `--expected-revision 1`. The value is the `canonical_sequence` field of `.ea/generations/<generation id>/state.json`, absent until the first move; `eawf status` names the generation id. A stale value is refused with `the tree is at canonical sequence N`, and a key the tree already holds is refused too, so the next Task is `PROJ-0002`.

## Resume a claimed wave

Task status is machine-local, under the generation's ignored `local/` directory. On the machine that ran the cutover a wave CLAIMED in 0.6.8 stays CLAIMED; in a clone it imports as PLANNED, because the session that held the claim was closed in step 1 and its claim did not travel. Claim it again in the clone by its wave id: `eawf task advance-legacy P01-I01-W01 --to CLAIMED --actor OPERATOR --reason "Resume the wave claimed before the upgrade"`. `eawf task advance-legacy` moves an imported Task forward only, to CLAIMED, RUNNING, COMPLETED or DROPPED, and `eawf task release` refuses an imported Task, so an imported claim never returns to PLANNED; drop the wave with `--to DROPPED` if nobody will finish it.

## Retired verbs

The flag day retired 155 epoch-1 verbs; typing one prints what to run instead. 67 name the 0.7 verb that replaces them: `phase open`, `phase activate` and `phase close` became `milestone create`, `milestone activate` and `milestone accept`; `iter open` and `iter close` became `batch create` and `batch complete`; `wave plan` and `backlog add` became `task create`; `wave claim` became `task claim`; `wave close` and `backlog close` became `task complete`; `wave dispatch` and `session start` became `run create` and `run start`; `decision add` and `audit add` became `record append`; `state show` and `tui` became `status` and `ui`; `research campaign new` became `campaign new`; `dispatch pause` became `run pause-dispatch`; `skill resume` became `question reply`; `calibrate buckets` became `metrics refit`. The other 88 have no replacement, among them `goal define`, `hypothesis define` and `wave release`, and point at `eawf migrate epoch2 --plan`. The full table is `RETIRED_VERBS` in [`src/eawf/surfaces/cli/flag_day.py`](../../src/eawf/surfaces/cli/flag_day.py); the 0.7 surface is in the [CLI reference](../reference/autogen/cli.md), and each cutover mode in the [epoch-2 cutover guide](../reference/epoch2-cutover.md).

## What does not carry over

Epoch-1 bookkeeping with no epoch-2 meaning is dropped, and the plan records a proof for each drop: close attempts, wave dependency barriers and bindings, and wave integrations. Pointers, health, indexes and pause flags are rebuilt from the new records rather than copied. Plugin and worktree rows are kept read-only as legacy records. Rows the plan lists as unresolved stay only in the frozen `.ea/state.json` and git history. Machine-local files (`.ea/local/`, `.ea/telemetry.db`, the event firehose) and anything uncommitted are not staged, so they do not move.
