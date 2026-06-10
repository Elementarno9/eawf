# A18-P14 ship-gate audit (P14-I03 close)

Audit of the P14-I03-W01 hotfix wave covering three live defects
surfaced after A17 closed P14-I02:

- **B1** `eawf plugin doctor codex --scope user` rendered the
  workspace root instead of `~/.codex/plugins/eawf` whenever the
  first available `DoctorEntry` was the `config` kind (its parent
  walk fell out of the `_codex_doctor_text` heuristic).
- **B3** `codex plugin marketplace add <build/eawf-codex-marketplace>`
  rejected our packaged tree because Codex looks for the manifest at
  `.agents/plugins/marketplace.json`, not at the root.
- **B4** `eawf plugin install opencode` shipped only `plugin.js` +
  sidecar; AGENT_REGISTRY and SKILL_REGISTRY contents were never
  exposed under `<base>/agents/` and `<base>/commands/`, so OpenCode
  never saw our agents/skills.

Two commits, plus a follow-up:
- `de84745` `[P14-I03-CORE]` state reopen
- `6e04966` `[P14-I03-W01]` code + tests for B1+B3+B4
- `3rd commit` `[P14-I03-W01]` opencode command body uses
  `$ARGUMENTS` placeholder (post-audit polish)

## Per-criterion verdicts

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Codex doctor user-scope folder display correct (B1) | pass | `DoctorReport.plugin_root` carries the scope-correct dir; `_codex_doctor_text` reads it directly. Regression test `test_doctor_report_plugin_root_points_at_scope_dir` covers both project and user scopes. Live verify: `eawf plugin doctor codex --scope user → /Users/user/.codex/plugins/eawf`. |
| 2 | Codex marketplace manifest at `.agents/plugins/marketplace.json` (B3) | pass | `_MARKETPLACE_SUBDIR=(".agents","plugins")`; root `marketplace.json` no longer emitted, and legacy root file is stripped on rerun. Live verify: `codex plugin marketplace add ./build/eawf-codex-marketplace/` returned `Added marketplace eawf-local-codex`. |
| 3 | OpenCode install emits 8 agents + 10 commands (B4) | pass | `install_plugin` loops AGENT_REGISTRY and `user_invocable=True` skills. Sidecar records the rendered lists. Live verify: `doctor opencode --scope user → ok=21 drifted=0 missing=0` (plugin.js + sidecar + config + 8 agents + 10 commands). |
| 4 | OpenCode doctor classifies agents/commands as ok/drifted/missing | pass | `doctor_plugin` adds parallel hash-compare loops emitting `kind="agent"` / `kind="command"` entries; covered by `test_doctor_flags_missing_agent_files` and `test_doctor_flags_drifted_command_files`. |
| 5 | OpenCode command body substitutes invocation args | pass (polish) | Each command body closes with `ARGUMENTS: $ARGUMENTS`; argument hint preserved as HTML comment. Without this opencode would treat the body as static text and never inject `$1`/`$ARGUMENTS`. |
| 6 | All pytest passes; B1/B3/B4 have dedicated regression tests | pass | `uv run pytest`: 2240 passed, 12 deselected (129 s). New tests: `test_doctor_report_plugin_root_points_at_scope_dir`, `test_package_does_not_emit_root_marketplace_json`, `test_package_strips_legacy_root_marketplace_on_rerun`, `test_install_emits_agents_per_registry`, `test_install_emits_commands_for_invocable_skills`, `test_doctor_flags_missing_agent_files`, `test_doctor_flags_drifted_command_files`. |
| 7 | `pre-commit run --all-files` clean | pass | ran on full tree post-W01 commit (ruff, format, end-of-file, secrets baseline regenerated and committed). |
| 8 | State reflects reality | will pass after this audit registers + closes W01/I03/P14 | next CORE commit. |

## Operator-visible additions

- `eawf plugin install opencode` summary now shows `agents:` and
  `commands:` counts in addition to `plugin.js` / `sidecar` /
  `config`.
- `eawf plugin doctor opencode` ok/drifted/missing totals now account
  for per-agent and per-command files.
- `eawf plugin doctor codex` JSON payload exposes the resolved
  `plugin_root` field.
- `eawf plugin package codex` emits `<target>/.agents/plugins/marketplace.json`
  (Codex Build-plugin canonical path) and removes any leftover
  root-level `marketplace.json` from prior packagings.

## Non-blocking follow-ups

- `DoctorEntry.kind` is still a stringly-typed `str` with a Literal in
  a comment (pre-existing; not introduced by this wave).
- `_render_opencode_agent_md` omits the opencode `permission` ACL
  object; operators that want to lock down per-agent tool access need
  to layer that in `opencode.json` manually.
- Doctor loops for agents/commands duplicate the structure; collapsible
  into a helper if a fourth artifact kind is added later. Style only.

## Verdict: PASS

Wave delivers B1 + B3 + B4 end-to-end with live verification on the
operator's machine for all three flows. Full test suite green.
