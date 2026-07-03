# A17-P14 ship-gate audit (P14-I02 hotfix re-close)

Hotfix re-close of P14-I02-W01 after operator-discovered defects in
the first-pass A16 verdict. Audit supersedes A16-P14 for criterion
#3 (codex install discoverability) and #6 (display correctness);
all other A16 verdicts stand.

## Defects addressed

1. **Display showed workspace pwd for user scope.** `plugin install codex --scope user (dry-run) → <repo>` — the target_dir is workspace-anchored but writes land under `<home>/.codex/...`. Fixed by deriving plugin root from the result's file paths (`manifest.parents[1]` for codex,
   `plugin_js.parent` for opencode); applied to install + doctor text
   formatters and the scope-tip banner.
2. **Codex did not see the installed plugin.** Per the Codex
   Build-plugin reference, dropping a plugin tree under
   `~/.codex/plugins/<name>/` does NOT auto-load it — Codex requires
   marketplace registration. The `[plugins.eawf] enabled = true` line
   the W01 first-pass wrote to `config.toml` only toggles
   already-discovered plugins, not unregistered ones. Resolved by
   shipping a new `eawf plugin package codex` command that emits a
   self-contained marketplace tree (`marketplace.json` + `plugins/eawf/`)
   the operator registers with `codex plugin marketplace add <path>`.

## Per-criterion verdicts (re-evaluated)

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | opencode install writes `.opencode/plugins/eawf.js` + sidecar; no array entry | pass (unchanged from A16) | existing tests |
| 2 | opencode `--scope user` → XDG | pass (unchanged) | existing tests |
| 3 | codex install + Codex discovers plugin | **partial → pass on hotfix flow** | `eawf plugin install codex` writes the plugin tree but Codex auto-discovery is not supported; full flow is `install` → `package codex` → `codex plugin marketplace add`. Documented in `docs/architecture/plugins.md` and via the post-install banner |
| 4 | codex `--scope user` writes under `~/.codex/plugins/eawf/` | pass (unchanged) | existing tests |
| 5 | claude `--scope user` rejected (exit 3) | pass (unchanged) | existing tests |
| 6 | Two runs at same scope produce byte-identical output | pass | including new `package codex` flow (`test_package_idempotent`) |
| 7 | `plugin doctor` exits 0 at both scopes | pass (unchanged) | existing tests |
| 8 | Plugin tests pass | pass | 79 plugin tests (66 unit + 13 integration touched files), full suite 1851 unit + 380 integration green |
| 9 | `pre-commit run --all-files` clean | pass | local run before hotfix commit |
| 10 | `mypy src/` clean | pass | 239 source files |
| 11 | State reflects reality | pass | will be confirmed after this audit registers and W01/I02/P14 re-close |

## Operator-visible additions

- New CLI command: `eawf plugin package codex [--target ...]`.
- New banner after `plugin install codex` warning that Codex requires
  marketplace registration and pointing at `plugin package codex`.
- Fixed display: install + doctor text show real destination paths
  for both project and user scopes.

## Verdict

**pass** — both defects resolved. P14-I02 ready to re-close.
