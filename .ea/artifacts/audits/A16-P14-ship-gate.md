# A16-P14 ship-gate audit (P14-I02 closeout)

Fresh-context auditor verified P14-I02 — a single-wave reopen of P14 that
delivers the plugin-install native-layout rework deferred out of P14-I01.
P14 was reopened (closed → active) on 2026-05-12 via a `[P14-CORE]`
state-bookkeeping commit because `eawf phase open` lacks a `--reopen`
flag; the W01 plan documents this fallback. Iter P14-I02 opens and
contains a single wave (W01) covering both runtime renderers, CLI, tests,
and docs.

## Per-wave verdicts

| Wave | Decision / scope | Verdict |
|---|---|---|
| W01 | Plugin install native layout (codex `.codex-plugin/plugin.json`, opencode `.opencode/plugins/eawf.js` + sidecar) + `--scope project\|user` (claude rejects user scope) + conflict gates for codex/opencode user-vs-project clashes + parametrised tests + plugins.md doc | pass |

## Per-criterion verdicts (W01 success criteria 1-11)

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | `eawf plugin install opencode` writes `.opencode/plugins/eawf.js` + sidecar; `opencode.json` carries no installer-inserted `plugins:[...]` entry | pass | `tests/integration/test_plugin_install_full.py::test_plugin_install_opencode_drops_plugins_array[project]`; manual smoke confirms `plugins` key absent from generated `opencode.json` |
| 2 | `--scope user` writes under `$OPENCODE_CONFIG_DIR/plugins/eawf.js` (or `~/.config/opencode/plugins/eawf.js`) | pass | `tests/unit/test_plugin_install_opencode.py::test_install_opencode_user_scope_writes_into_xdg`, `…_env_var_fallback`, `…_home_default` |
| 3 | `eawf plugin install codex` writes `.codex/plugins/eawf/{manifest,skills,agents,hooks}` + `[plugins.eawf] enabled = true` in `.codex/config.toml` | pass | `tests/integration/test_plugin_install_full.py::test_plugin_install_codex_writes_native_layout[project]`; manual smoke shows `enabled = true` in `config.toml` |
| 4 | `--scope user` writes under `~/.codex/plugins/eawf/` and patches `~/.codex/config.toml` | pass | `tests/integration/test_plugin_install_full.py::test_plugin_install_codex_writes_native_layout[user]` |
| 5 | `eawf plugin install claude --scope user` fails with `InvalidInput` exit code 3 | pass | `tests/unit/test_plugin_install_conflict_gate.py::test_install_claude_user_scope_rejected`, `tests/integration/test_plugin_install_full.py::test_plugin_install_claude_user_scope_rejected`; manual smoke prints "claude is project-scope only; use the CC marketplace for user-scope installs" and exits 3 |
| 6 | Two runs at the same scope produce byte-identical output | pass | `tests/unit/test_plugin_install_codex.py::test_install_idempotent_second_run_unchanged`, `tests/unit/test_plugin_install_opencode.py::test_install_idempotent`, `tests/integration/test_plugin_install_full.py::test_plugin_install_codex_idempotent_at_scope` |
| 7 | `eawf plugin doctor {opencode,codex} --scope <s>` finds the install and exits 0 | pass | `tests/unit/test_plugin_install_codex.py::test_doctor_reports_clean_after_install[user]`, `tests/unit/test_plugin_install_opencode.py::test_doctor_reports_clean_after_install[user]`, `tests/integration/test_plugin_install_full.py::test_plugin_doctor_codex_finds_install_at_scope` |
| 8 | Plugin tests pass after parametrisation | pass | `pytest tests/unit/test_plugin_install_{codex,opencode}.py tests/unit/test_plugin_install_conflict_gate.py tests/integration/test_plugin_install_full.py` → 68 passed |
| 9 | `pre-commit run --all-files` clean | pass | local run during P14-I02-W01 commit |
| 10 | `mypy src/` clean | pass | `Success: no issues found in 238 source files` |
| 11 | State reflects reality | pass | P14 active, P14-I02 active, P14-I02-W01 closed (verified pre-audit); this audit closes the loop on phase re-close |

## Open follow-ups

- Codex `hooks.json` manifest sub-file not emitted. The Codex plugin
  schema names `"hooks": "./hooks/"` as a path; whether the runtime
  expects a `hooks.json` index file beyond directory references is
  not pinned by upstream docs. Track as a v0.4 follow-up; the doctor's
  hash-based drift detection works without it.
- Legacy-path migration is report-only (per AGENTS.md deletion rule).
  A future polish wave could ship a `plugin doctor --clean-legacy`
  flag that prompts before removing flat `<ws>/.codex/...` /
  `<ws>/plugin.js`.

## Verdict

**pass** — P14-I02 closes P14 with the plugin-install rework on the
native runtime layouts and a working `--scope project|user` surface
across the supported runtimes.
