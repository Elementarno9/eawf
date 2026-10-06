# Upgrading from 0.6.8 to 0.7.0rc1

eawf 0.7 keeps a repository's records in a new store. The 0.6 store is *epoch 1*: one `.ea/state.json` document that every verb edits. The 0.7 store is *epoch 2*: a *generation*, a versioned snapshot under `.ea/generations/`, that the native verbs (`milestone`, `batch`, `task`, `run`) change. A 0.6.8 tree refuses every writing verb until it is cut over, once, by `eawf migrate epoch2`. The steps below run in the repository root, in order, with 0.7 installed. `PROJ` stands for your project code, as `eawf status` prints it; it also names the scratch paths beside the repository, so repositories upgraded side by side never share one.

## 1. Stop the 0.6.8 daemon, back up, then close every live session

A 0.6.8 daemon keeps running after 0.7 is installed. Where 0.7 reaches it, it refuses it: before the first call it compares the daemon's version with its own and, on any difference, stops with a message naming both versions and `eawf daemon restart`. It never replaces the daemon on its own, since that daemon may be serving live Runs; `eawf daemon restart` and `eawf daemon stop` skip the check so you can always act on it. It reaches a daemon on a runtime directory pinned by `EAWF_RUNTIME_DIR`, which both releases share. Without that pin 0.7 runs one daemon per tree under `~/.eawfd/trees/` and never dials the address 0.6.8 used, `~/.eawfd/eawfd.sock` (`$XDG_RUNTIME_DIR/eawfd/eawfd.sock` on Linux when that is set), so stop a daemon there by hand with the first line below; `eawf doctor` lists one left running under `stray_daemons`. `eawf daemon stop` is also the manual fallback for a daemon 0.7 refused.

```text
EAWF_RUNTIME_DIR=~/.eawfd eawf daemon stop
git status
git add .ea .gitignore AGENTS.md && git commit --allow-empty -m "Record the tree before the 0.7 upgrade"
cp -R .ea ../PROJ-ea-before-epoch2
eawf migrate status
```

The commit is the git restore point; `--allow-empty` makes it even when everything is already committed. It stages only eawf's own files, so read `git status` first and commit or stash any other work of yours on its own. If `eawf migrate status` lists a pending schema step, run `eawf migrate` and commit the result. The cutover refuses while any agent session or worktree is still live, because a second writer could race it: stop your agent runtimes, read the live session ids with `eawf --json status` (the `active_sessions` list), close each one with `eawf session close <SES-id>`, and retire stale worktree rows with `eawf worktree reconcile`. Step 2 commits what these change.

## 2. Opt in, stage, register and plan

```text
eawf migrate epoch2 --opt-in --target-root .ea
git add .ea && git commit -m "Opt the tree into the epoch-2 cutover"
eawf migrate epoch2 --stage-to ../PROJ-stage --workspace-key PROJ --project-key PROJ
eawf workspace add PROJ --home PROJ --title "my project"
touch ../PROJ-allowlist.txt
eawf migrate epoch2 --plan --snapshot-root ../PROJ-stage --allowlist ../PROJ-allowlist.txt --workspace-key PROJ --project-key PROJ --repository-key PROJ --default-track-key TRK-PROJ-CORE
```

The opt-in takes an eawf backup of `.ea` (under `~/.eawf/backups/`) and writes `.ea/epoch2-opt-in.json`, the *opt-in declaration* that pins it; the apply refuses a tree with no declaration, or one whose pinned backup no longer verifies. `--stage-to` copies the committed `.ea` files at HEAD into a new or empty directory outside `.ea`; after any later commit, delete `../PROJ-stage` and stage again. A *workspace* groups repositories; every imported record is addressed under its key. The allowlist names epoch-1 symbols that were renamed or deliberately deleted; an empty file is valid. `--plan` writes nothing and prints an *approval digest*, the fingerprint of exactly this plan. 0.6 phases and goals name no *Track*, the 0.7 line of work a Milestone belongs to, and the importer does not guess one. `--default-track-key` is your answer for every record whose source names none: the plan records it in the manifest, and the apply refuses to run without it when such records exist. It does not create the Track, and those records arrive with no Track; the plan says so in its `declared Track:` line.

## 3. Apply, refresh and commit

```text
eawf migrate epoch2 --apply --plan-digest <approval digest> --target-root .ea --snapshot-root ../PROJ-stage --allowlist ../PROJ-allowlist.txt --workspace-key PROJ --project-key PROJ --repository-key PROJ --default-track-key TRK-PROJ-CORE
eawf sync
eawf plugin install claude
git status
git add .ea .gitignore AGENTS.md docs/rules && git commit -m "Cut the tree over to epoch 2"
eawf repo register . --yes
eawf status
```

The apply re-plans and refuses if the digest no longer matches or the live `.ea` differs from the staged copy; commit, stage and plan again. If the plan reports unresolved rows, pass each address it lists with `--accept-unresolved`. A stopped apply is finished or undone by `eawf migrate epoch2 --recover --target-root .ea`. The apply rewrites the managed `.gitignore` block so it ignores every machine-local path of the generation: `local/` (this machine's in-flight Task status), `indexes/` (rebuilt from the ledgers), the cutover journal and the restore copies of pre-cutover files git history already holds. The `git add` names only the paths the upgrade writes: under `.ea` the pointer to the selected generation, the epoch marker, the generation's document and its append-only ledgers, and the files `eawf sync` and `eawf plugin install claude` re-render. It leaves out `.claude/settings.json`: the managed block ignores `.claude/` as machine-local runtime output, and `git add` refuses the whole command when it names an ignored path. Check `git status` before it runs, drop `docs/rules` from the list if `eawf sync` wrote no rule there, and leave anything else it shows for a commit of your own. `.ea/state.json` stays as the frozen epoch-1 record. `eawf sync` re-renders `AGENTS.md`, the rules under `docs/rules/` and the `.gitignore` block if stale (`eawf doctor` warns when it is); `eawf plugin install claude` replaces the 0.6.8 Stop hook with the ten 0.7 hooks in `.claude/settings.json`. Then run `eawf ui`; `eawf status` lists the open Tasks of the generation.

## Roll back to 0.6.8

A rollback returns the tree to the restore point step 1 made, so it is exact but not selective. Work done after the upgrade is not kept. Every commit made since step 1, every uncommitted change and every Task and Run of the generation are discarded. The `git branch` line keeps a pointer to those commits, so git can still show them. Run the steps in the repository root, the first one with 0.7 still installed:

```text
eawf daemon stop
git branch PROJ-after-epoch2
git reset --hard "$(git log -1 --format=%H --grep='Record the tree before the 0.7 upgrade')"
rm -rf .ea && cp -R ../PROJ-ea-before-epoch2 .ea
diff -r .ea ../PROJ-ea-before-epoch2
uv tool install --force "eawf==0.6.8"
eawf plugin install claude --force
eawf status
```

`eawf daemon stop` stops this tree's 0.7 daemon, so nothing writes `.ea` while it is restored. The restore-point reset brings back every committed file as step 1 committed it, `.gitignore`, `AGENTS.md` and `docs/rules` included. `.ea` is then replaced by the copy step 1 took, which also holds the machine-local files git never tracked. The restore is byte-identical when `diff -r` prints nothing; if it prints anything, run the `rm` and `cp` line again before going on. Reinstall 0.6.8 the way you installed 0.7; the `uv tool install` line is for a `uv tool` install. With 0.6.8 installed, `eawf plugin install claude --force` puts the 0.6.8 Stop hook back in `.claude/settings.json` in place of the 0.7 hooks, which the reset does not restore because `.claude/` is ignored. `eawf status` then reads the epoch-1 tree. Nothing here touches `~/.eawf`, so the registry entries `eawf init` and `eawf workspace add` made there are not removed.

## Create your first Task

A Task's URN is `eawf://<workspace>/<project>/<repository>/task/<key>`, and its key is the project code, a hyphen and a four-digit ordinal: `PROJ-0001`. The smallest create document the tree admits is a JSON object with `key`, `priority` and `intent`. `--expected-revision` is the tree's committed canonical sequence, the count of moves committed to the generation since the cutover. It is 0 right after the cutover, imported records included, and each committed move adds one: the create below prints `result.canonical_sequence: 1`, so the next passes `--expected-revision 1`. The value is the `canonical_sequence` field of `.ea/generations/<generation id>/state.json`, absent until the first move; `eawf status` names the generation id. A stale value is refused with `the tree is at canonical sequence N`, and a key the tree already holds is refused too, so the next Task is `PROJ-0002`.

```text
printf '%s\n' '{"key": "PROJ-0001", "priority": "P2", "intent": "Add a greeting module"}' > ../PROJ-task.json
eawf task create eawf://PROJ/PROJ/PROJ/task/PROJ-0001 --expected-revision 0 --idempotency-key create-PROJ-0001 --actor OPERATOR --from-spec ../PROJ-task.json --yes
```

To finish a wave that was in flight: Task status is machine-local, under the generation's ignored `local/` directory. On the machine that ran the cutover a wave CLAIMED in 0.6.8 stays CLAIMED; in a clone it imports as PLANNED, because the session that held the claim was closed in step 1 and its claim did not travel. An imported Task is history rather than live work: the native verbs, `eawf task claim` and `eawf task release` among them, refuse it. Continue the unfinished work as a native Task, created as above, and cite the wave id in its intent.

## Retired verbs, config leaves and records

The flag day retired 161 epoch-1 verbs; typing one prints what to run instead. 67 name the 0.7 verb that replaces them: `phase open`, `phase activate` and `phase close` became `milestone create`, `milestone activate` and `milestone accept`; `iter open` and `iter close` became `batch create` and `batch complete`; `wave plan` and `backlog add` became `task create`; `wave claim` became `task claim`; `wave close` and `backlog close` became `task complete`; `wave dispatch` and `session start` became `run create` and `run start`; `decision add` and `audit add` became `record append`; `state show` and `tui` became `status` and `ui`; `research campaign new` became `campaign new`; `dispatch pause` became `run pause-dispatch`; `skill resume` became `question reply`; `calibrate buckets` became `metrics refit`. The other 94 have no replacement, among them `goal define`, `hypothesis define` and `wave release`, and name two commands instead: `eawf migrate status`, which shows where an epoch-1 tree stands, and `eawf help upgrade-from-0.6`, this guide. The full table is `RETIRED_VERBS` in [`src/eawf/surfaces/cli/flag_day.py`](../../src/eawf/surfaces/cli/flag_day.py); the 0.7 surface is in the [CLI reference](../reference/autogen/cli.md), and each cutover mode in the [epoch-2 cutover guide](../reference/epoch2-cutover.md).

A stock 0.6.8 `.ea/config.yaml` sets ten leaves 0.7 no longer reads: `acceptance.lint`, `acceptance.tests`, `acceptance.typecheck`, `estimation.buckets`, `mcp.enabled`, `project.code`, `project.domains`, `project.goals`, `project.slug` and `project.title`. They are ignored, so removing them only tidies the file. `eawf config unset` refuses them as unknown keys; delete the `acceptance:`, `estimation:`, `mcp:` and `project:` blocks from `.ea/config.yaml` by hand and check the file with `eawf config validate`. `eawf --no-input doctor --fix` previews the doctor's repair, whose `config.normalize` action names each retired leaf in each config layer; `eawf doctor --fix` applies every listed action after one confirmation, so read the whole list first, as it also stops stray daemons and makes any other repair the doctor found.

Epoch-1 bookkeeping with no epoch-2 meaning is dropped, and the plan records a proof for each drop: close attempts, wave dependency barriers and bindings, and wave integrations. Pointers, health, indexes and pause flags are rebuilt from the new records rather than copied. Plugin and worktree rows are kept read-only as legacy records. Rows the plan lists as unresolved stay only in the frozen `.ea/state.json` and git history. Machine-local files (`.ea/local/`, `.ea/telemetry.db`, the event firehose) and anything uncommitted are not staged, so they do not move.
