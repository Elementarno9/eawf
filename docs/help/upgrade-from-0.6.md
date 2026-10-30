# Upgrading a 0.6 tree to 0.7.0

eawf 0.7 keeps a repository's records in a new store. A 0.6 tree keeps them in one `.ea/state.json` document. A 0.7 tree keeps them in a *generation*, a versioned snapshot under `.ea/generations/` that the native verbs (`milestone`, `batch`, `task`, `run`) change. The cutover that builds a generation from a 0.6 tree runs once, and 0.7.0rc4 is the last release that carries it. 0.7.0 does not carry it.

0.7.0 does not write a 0.6 tree. Each writing verb of the `track`, `milestone`, `batch`, `task`, `run`, `release`, `campaign`, `question`, `action`, `decision`, `workspace`, `config`, `daemon`, `memory` and `reflect` groups, except `eawf daemon stop` and `eawf daemon restart`, and the console (`eawf ui` or a bare `eawf`), exits 4 with `data.kind` set to `MigrationRequired` and this message, where `<verb>` is the command you ran:

```text
.ea is a 0.6 tree, which this release does not write, so `eawf <verb>` was refused and nothing was written; install eawf 0.7.0rc4, run its migrate epoch2 verb with --plan and then --apply, then upgrade to this release
```

A read verb still answers on a 0.6 tree and reports it as epoch 1. The upgrade has three parts: install 0.7.0rc4, migrate the tree with it, then upgrade to 0.7.0. The steps below run in the repository root, in order. `PROJ` stands for your project code, as `eawf status` prints it; it also names the scratch paths beside the repository, so repositories upgraded side by side never share one.

## 1. Install 0.7.0rc4

```text
uv tool install --force "eawf==0.7.0rc4"
eawf --version
```

Install 0.7.0rc4 the way you installed eawf: the line above is for a `uv tool` install from PyPI; from npm, run `npm install -g @elementarno/eawf@0.7.0-rc.4`. `--force` replaces the eawf tool for the whole machine, so every other repository runs 0.7.0rc4 until step 5. 0.7.0 still runs `eawf daemon stop` on a 0.6 tree, but the steps after it need 0.7.0rc4. The `migrate`, `session` and `worktree` commands in steps 2 to 4 exist only in 0.7.0rc4 and earlier 0.7 releases.

## 2. Stop the daemon, back up, then close every live session

```text
eawf daemon stop
git status
git add .ea .gitignore AGENTS.md && git commit --allow-empty -m "Record the tree before the 0.7 upgrade"
cp -R .ea ../PROJ-ea-before-epoch2
eawf backup create --note "before the 0.7 upgrade"
eawf migrate status
```

`eawf daemon stop` never starts a daemon to stop it, and with no daemon it prints `daemon not running` and exits 0. A 0.6.8 daemon listens on `~/.eawfd/eawfd.sock` (`$XDG_RUNTIME_DIR/eawfd/eawfd.sock` on Linux when that is set), which a 0.7 release does not dial unless `EAWF_RUNTIME_DIR` pins it; to stop that daemon, prefix the first line for that one command with `EAWF_RUNTIME_DIR=` set to the directory that holds the socket. `eawf doctor` lists a daemon left running under `stray_daemons`.

The commit is the git restore point; `--allow-empty` makes it even when everything is already committed. It stages only eawf's own files, so read `git status` first and commit or stash any other work of yours on its own. The copy of `.ea` beside the repository also holds the machine-local files git never tracked, and the rollback below restores from it. `eawf backup create` adds a snapshot of `state.json` and `config.yaml` under `~/.eawf/backups/`; `eawf backup list` names each snapshot, and `eawf backup restore --ts <timestamp>` copies one back.

If `eawf migrate status` lists a pending schema step, run `eawf migrate` and commit the result. The cutover refuses while any agent session or worktree is still live, because a second writer could race it: stop your agent runtimes, read the live session ids with `jq -r '.agent_sessions | to_entries[] | select(.value.status == "active") | .key' .ea/state.json` (without jq, open `.ea/state.json` and take each key under `agent_sessions` whose `status` is `active`), close each one with `eawf session close <SES-id>`, and retire stale worktree rows with `eawf worktree reconcile`. Step 3 commits what these change.

## 3. Opt in, stage, register and plan

```text
eawf migrate epoch2 --opt-in --target-root .ea
git add .ea && git commit -m "Opt the tree into the epoch-2 cutover"
eawf migrate epoch2 --stage-to ../PROJ-stage --workspace-key PROJ --project-key PROJ
eawf workspace add PROJ --home PROJ --title "my project"
touch ../PROJ-allowlist.txt
eawf migrate epoch2 --plan --snapshot-root ../PROJ-stage --allowlist ../PROJ-allowlist.txt --workspace-key PROJ --project-key PROJ --repository-key PROJ --default-track-key TRK-PROJ-CORE
```

The opt-in takes an eawf backup of `.ea` (under `~/.eawf/backups/`) and writes `.ea/epoch2-opt-in.json`, the *opt-in declaration* that pins it; the apply refuses a tree with no declaration, or one whose pinned backup no longer verifies. `--stage-to` copies the committed `.ea` files at HEAD into a new or empty directory outside `.ea`; after any later commit, delete `../PROJ-stage` and stage again. A *workspace* groups repositories; every imported record is addressed under its key. The allowlist names 0.6 symbols that were renamed or deliberately deleted; an empty file is valid. `--plan` writes nothing and prints an *approval digest*, the fingerprint of exactly this plan. 0.6 records name no *Track*, the 0.7 line of work a Milestone belongs to, and the importer does not guess one. `--default-track-key` is your answer for every record whose source names none: the plan records it in the manifest, and the apply refuses to run without it when such records exist. It does not create the Track, and those records arrive with no Track; the plan says so in its `declared Track:` line.

## 4. Apply, refresh and commit

```text
eawf migrate epoch2 --apply --plan-digest <approval digest> --target-root .ea --snapshot-root ../PROJ-stage --allowlist ../PROJ-allowlist.txt --workspace-key PROJ --project-key PROJ --repository-key PROJ --default-track-key TRK-PROJ-CORE
eawf sync
eawf plugin install claude
git rm --cached .ea/state.json
git ls-files '.ea/store/*.jsonl' | grep -Ev '/(event|release|release_record|release_checkpoint_receipt|release_train_advance|conformance_stage)\.jsonl$' | xargs git rm --cached --
git status
git add .ea .gitignore AGENTS.md docs/rules && git commit -m "Cut the tree over to epoch 2"
eawf repo register . --yes && eawf status
```

The apply re-plans and refuses if the digest no longer matches or the live `.ea` differs from the staged copy; commit, stage and plan again. If the plan reports unresolved rows, pass each address it lists with `--accept-unresolved`. A stopped apply is finished or undone by `eawf migrate epoch2 --recover --target-root .ea`. The apply writes the managed `.gitignore` block, between its `# BEGIN EAWF:gitignore` and `# END EAWF:gitignore` markers, so it ignores every machine-local path of the generation: `local/` (this machine's in-flight Task status), `indexes/` (rebuilt from the ledgers), the cutover journal and the restore copies of pre-cutover files git history already holds. It rewrites a block an older release wrote, appends the block to a `.gitignore` that has none, and keeps every line of yours outside the markers.

The `git add` names only the paths the upgrade writes: under `.ea` the pointer to the selected generation, the epoch marker, the generation's document and its append-only ledgers, and the files `eawf sync` and `eawf plugin install claude` re-render. It leaves out `.claude/settings.json`: the managed block ignores `.claude/` as machine-local runtime output, and `git add` refuses the whole command when it names an ignored path. Check `git status` before it runs, drop `docs/rules` from the list if `eawf sync` wrote no rule there, and leave anything else it shows for a commit of your own. `.ea/state.json` stays on disk as the frozen 0.6 record, but leaves git: the managed block ignores it and every `.ea/store/*.jsonl` except the event, release and conformance stores, and git keeps tracking a file it already tracked, so the two `git rm --cached` lines untrack them once while keeping the local copies. Git history keeps every earlier version.

If Claude Code already has eawf installed from its plugin marketplace, `eawf plugin install claude` refuses and names that install, since both copies would load: either run `/plugin uninstall eawf@eawf` inside Claude Code and rerun the line, or rerun it with `--force` to keep both. At a terminal it asks first; without one, as in a script, it refuses with the same choice.

The import keeps what has a 0.7 meaning. Bookkeeping with none is dropped, and the plan records a proof for each drop. Pointers, health, indexes and pause flags are rebuilt from the new records rather than copied. Plugin and worktree rows are kept read-only as legacy records. Rows the plan lists as unresolved stay only in the frozen `.ea/state.json` and git history. Machine-local files (`.ea/local/`, `.ea/telemetry.db`, the event firehose) and anything uncommitted are not staged, so they do not move.

## 5. Upgrade to 0.7.0

```text
eawf daemon stop
uv tool install --force "eawf==0.7.0"
eawf --version
eawf sync
eawf plugin install claude
git status
git add .ea .gitignore AGENTS.md docs/rules && git commit -m "Re-render the tree for eawf 0.7.0"
eawf status
```

Stop the rc4 daemon first, then install 0.7.0 the way you installed 0.7.0rc4 (from npm, `npm install -g @elementarno/eawf@0.7.0`) and check that `eawf --version` prints 0.7.0. A daemon of another release that is still running is refused with a message naming both versions and `eawf daemon restart`. `eawf sync` re-renders `AGENTS.md`, the rules under `docs/rules/` and the `.gitignore` block if stale, and `eawf plugin install claude` replaces the hooks in `.claude/settings.json` with the 0.7.0 hooks. Commit what `git status` shows from these two, as in step 4. `eawf status` then reads the generation and lists its open Tasks; `eawf ui` opens the console.

## Roll back to 0.6.8

A rollback returns `.ea` and every tracked file to the restore point step 2 made, so it is exact but not selective. Work done after the upgrade is not kept. Every commit made since step 2, every uncommitted change and every Task and Run of the generation are discarded. The `git branch` command keeps a pointer to those commits, so git can still show them. Run the steps in the repository root, the first one with the 0.7 release still installed:

```text
eawf daemon stop
git branch PROJ-after-epoch2 && git reset --hard "$(git log -1 --format=%H --grep='Record the tree before the 0.7 upgrade')"
rm -rf .ea && cp -R ../PROJ-ea-before-epoch2 .ea
diff -r .ea ../PROJ-ea-before-epoch2
uv tool install --force "eawf==0.6.8"
eawf plugin install claude --force
eawf status
```

`eawf daemon stop` stops this tree's 0.7 daemon, so nothing writes `.ea` while it is restored. The restore-point reset brings back every committed file as step 2 committed it, `.gitignore`, `AGENTS.md` and `docs/rules` included. `.ea` is then replaced by the copy step 2 took, which also holds the machine-local files git never tracked. When `diff -r` prints nothing, `.ea` is byte-identical to that copy and, with the reset, every tracked file to step 2; if it prints anything, run the `rm` and `cp` line again before going on. Reinstall 0.6.8 the way you installed 0.7; the `uv tool install` line is for a `uv tool` install, and `--force` replaces the eawf tool for the whole machine, so every other repository then runs 0.6.8 too. With 0.6.8 installed, `eawf plugin install claude --force` puts the 0.6.8 Stop hook back in `.claude/settings.json` in place of the 0.7 hooks, which the reset does not restore because `.claude/` is ignored. `eawf status` then reads the 0.6 tree. Files git ignores outlive the reset: the `.claude/`, `.mcp.json` and `CLAUDE.md` that the plugin install rendered stay, and these last two lines add `.ea/indexes/` and `.ea/state.json.lock`, so a comparison of the whole working tree with one taken before step 2 lists those five; the `diff -r` above is the check that `.ea` and every tracked file are restored exactly. Nothing here touches `~/.eawf`, so the registry entries `eawf init` and `eawf workspace add` made there are not removed.

## Create your first Task

A Task's URN is `eawf://<workspace>/<project>/<repository>/task/<key>`, and its key is the project code, a hyphen and a four-digit ordinal: `PROJ-0001`. The smallest create document the tree admits is a JSON object with `key`, `priority` and `intent`. `--expected-revision` is the tree's committed canonical sequence, the count of moves committed to the generation since the cutover. It is 0 right after the cutover, imported records included, and each committed move adds one: the create below prints `result.canonical_sequence: 1`, so the next passes `--expected-revision 1`. The value is the `canonical_sequence` field of `.ea/generations/<generation id>/state.json`, absent until the first move; `eawf status` names the generation id. A stale value is refused with `the tree is at canonical sequence N`, and a key the tree already holds is refused too, so the next Task is `PROJ-0002`.

```text
printf '%s\n' '{"key": "PROJ-0001", "priority": "P2", "intent": "Add a greeting module"}' > ../PROJ-task.json
eawf task create eawf://PROJ/PROJ/PROJ/task/PROJ-0001 --expected-revision 0 --idempotency-key create-PROJ-0001 --actor OPERATOR --from-spec ../PROJ-task.json --yes
```

Work in flight at the cutover imports as history, not as live work. Task status is machine-local, under the generation's ignored `local/` directory. On the machine that ran the cutover, work claimed in 0.6.8 stays CLAIMED; in a clone it imports as PLANNED, because the session that held the claim was closed in step 2 and its claim did not travel. The native verbs, `eawf task claim` and `eawf task release` among them, refuse an imported Task. Continue the unfinished work as a native Task, created as above, and cite the imported key in its intent.

## Commands and config leaves 0.7 dropped

0.7.0 does not carry the 0.6 commands or the cutover verbs, so typing one ends with `No such command`. The 0.7 surface is in the [CLI reference](../reference/autogen/cli.md), and each cutover mode in the [epoch-2 cutover guide](../reference/epoch2-cutover.md).

A stock 0.6.8 `.ea/config.yaml` sets ten leaves 0.7 no longer reads: `acceptance.lint`, `acceptance.tests`, `acceptance.typecheck`, `estimation.buckets`, `mcp.enabled`, `project.code`, `project.domains`, `project.goals`, `project.slug` and `project.title`. They are ignored, so removing them only tidies the file. `eawf config unset` refuses them as unknown keys; delete the `acceptance:`, `estimation:`, `mcp:` and `project:` blocks from `.ea/config.yaml` by hand and check the file with `eawf config validate`. `eawf --no-input doctor --fix` previews the doctor's repair, whose `config.normalize` action names each retired leaf in each config layer; `eawf doctor --fix` applies every listed action after one confirmation, so read the whole list first, as it also stops stray daemons and makes any other repair the doctor found.
