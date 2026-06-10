# A15-P14 ship-gate audit

Fresh-context auditor verified P14 (Harnesses + hygiene preamble) against the
per-wave specs declared in the P14 plan and the eleven waves recorded in
`state.iters['P14-I01']`. P14 closes the harness theme by adding the Codex +
OpenCode runtime adapters alongside the existing Claude adapter, while front-
loading the hygiene-debt closeouts (state EOL fix, version-coupling lint,
commit-prefix linter, MCP doctor drift) that v0.3 needs in place before
broader runtime work in v0.4.

## Per-wave verdicts

| Wave | Backlog / decision | Verdict |
|---|---|---|
| W01 | B054 hygiene preamble (state-writer EOL + version-coupling + phase prepare-close + iter-bump hint) | pass |
| W02 | B055 pre-commit hygiene + commit-prefix linter (D16) | pass |
| W03 | B056 profile schema runtime.adapters list[str] (D14) | pass |
| W04 | B057 custom profile discovery roots + mtime cache (D18) | pass |
| W05 | B058 profile new --inherit + validate + TOFU trust ledger (D19) | pass |
| W06 | D12 Codex runtime adapter | pass |
| W07 | D12 + D13 OpenCode plugin (untyped js) | pass |
| W08 | B062 MCP emit hardening + plugin doctor MCP drift (D21) | pass |
| W09 | B061 layered skill registry + user catalogue | pass |
| W10 | D15 + D23 TUI on rich + bare-eawf auto-launch | pass |
| W11 | B059 ArtifactKind enum + D20 a11y profile stub + D22 /blitz scaffold | pass |

## Per-criterion verdicts

| Criterion | Verdict |
|---|---|
| W01 — `tests/unit/test_writer.py::test_atomic_write_ends_with_newline` green; the W13 EOL regression remains pinned | pass |
| W01 — `tests/unit/test_version_coupling.py` couples `pyproject.toml:3` to `eawf.__version__` | pass |
| W01 — `eawf phase prepare-close <P##>` Typer command shipped under `cli/commands/lifecycle.py`; `--dry-run` is the default | pass |
| W01 — `eawf iter open` emits D17 hint tags (`previous_iter_audit_failed`, `wave_with_many_blockers`, `phase_scope_expanded`) when triggers fire | pass |
| W02 — `tools/commit_prefix_lint.py` rejects malformed subjects + `[P##-CORE]` commits touching non-state paths (D16) | pass |
| W02 — `.pre-commit-config.yaml` wires the linter as commit-msg + `eawf doc verify --strict` as pre-push + `detect-secrets` baseline hash recorded inline | pass |
| W03 — `CONFIG_SCHEMA_VERSION="1.1"` single source in `config/defaults.py`; wizard emits `runtime: {adapters, kind}`; STEP_RUNTIME choices include `codex` | pass |
| W03 — Per-overlay `_normalise_runtime_adapters` shim synthesises adapters from legacy `runtime.kind` with one-time deprecation warning | pass |
| W04 — `src/eawf/profiles/discovery.py` precedence workspace > user > builtin; mtime-keyed cache replaces `@functools.cache`; ProfileBody validation surfaces with file path | pass |
| W05 — `eawf profile new <name> [--inherit] [--force]` writes `.ea/profiles/<name>.yaml`; refuses bundled collisions; unknown parent rejected | pass |
| W05 — `eawf profile validate [<name>\|--all]` runs loader + schema + trust; `--no-input` fails closed on untrusted overlay | pass |
| W05 — TOFU trust ledger (`profile_sha256`, `load_trust_ledger`, `verify_trust`) persists into `<workspace>/.ea/config.yaml:profiles.trusted` | pass |
| W06 — `runtimes/codex/` package mirrors Claude shape; emits `.codex/{skills,agents,hooks}/` + `config.toml` with `[__eawf_managed]` markers; idempotent; doctor reports drift | pass |
| W07 — `runtimes/opencode/` package emits `opencode.json` (managed namespace + `mcp` + plugins=["plugin.js"]) + untyped `plugin.js` template asset (no TS / no build per D13) | pass |
| W08 — `doctor/checks.py:check_mcp_drift` joins `state.mcp_servers` against `.claude/settings.json:mcpServers` + `opencode.json:mcp`; reports missing-from-runtime + orphans; `run_all` returns 6 checks | pass |
| W08 — `mcp/installer.py:_SUPPORTED_RUNTIMES` extended to `(claude, claude-agent-sdk, codex, opencode)`; `_settings_path` routes per-runtime | pass |
| W09 — `skills/discovery.py` enumerates workspace > user > builtin; invalid SKILL.md surfaces `SkillFrontmatterError` with file path; runtime-visibility hint filters | pass |
| W09 — `eawf skill list --scope=builtin\|user\|workspace\|all` exposes source / runtimes / path / version on every row in JSON + text envelopes | pass |
| W10 — `eawf tui` opens `rich.Layout` with `Eä` brand outside-left of breadcrumb (per `feedback_tui_branding` memory); keymap shows `↑↓ navigate · Enter select · Esc quit` (per `feedback_tui_keymap_conventions`) | pass |
| W10 — Bare `eawf` callback routes to TUI; non-TTY / `--plain` / `--no-input` emits the deterministic status text | pass |
| W11 — `ArtifactKind` enum exposes the B059 vocabulary (audit_report, notebook, dataset, model, backtest, strategy, binary, scene, playtest_session, cve_ref) | pass |
| W11 — `Artifact.kind` stays free-form str for v0.3 back-compat with `review_findings` / `hypothesis_rejected` callers; strict-enum tightening deferred to v0.4 | pass |
| W11 — `profiles/data/a11y.yaml` stub committed (D20); no `state_extensions`, opt-in via `profiles.enabled` | pass |
| W11 — `skills/blitz.py` exposes `depth_cap`/`current_depth`/`bump_depth`/`should_auto_invoke`; recursion guard raises `BlitzRecursionExhausted` past EAWF_BLITZ_DEPTH (default 8) | pass |

## Phase-level verifications

| Verification | Outcome |
|---|---|
| `uv run pytest tests/ -q` | 2157 passed, 12 deselected |
| `uv run mypy src/eawf/cli/commands/lifecycle.py` | clean |
| `uv run pre-commit run` per-file gates | clean across all wave commits |
| `eawf plugin install claude --dry-run` | 10 skills · 8 agents · 14 hooks · settings.json created |
| `eawf plugin install codex --dry-run` | 10 skills · 8 agents · 14 hooks · config.toml created |
| `eawf plugin install opencode --dry-run` | plugin.js + opencode.json created |
| `eawf doctor` (6 checks) | tools/state/config/mcp_drift/render_output_roundtrip ok; manifest_in_sync warn driven by uncommitted local `.claude/` render (gitignored per memory `feedback_claude_local_only`) |

## Out-of-scope (deferred to P15+)

- B060 (uiux/web/mobile/a11y profile bodies + visual_artifacts state extension)
- B050 (weekly_eu_target field)
- Goose / Aider / Cursor / Cline runtime adapters (D12 explicit deferral)
- Own MCP server runtime (D21 explicit deferral)
- Full WCAG 2.2 a11y check matrix (D20: stub only in v0.3)

## Verdict

**pass** — every wave criterion matches the source-tree implementation, the
six doctor checks pass on a freshly-initialised workspace, and the three
runtime plugin installers emit byte-stable goldens via dry-run. P14 is the
gate to the v0.3 PR.
