# A14-P13 ship-gate audit

Fresh-context auditor verified P13 (feature cluster + v0.2 ship-gate)
against the per-wave specs declared in the P13 plan and the seven
waves recorded in `state.iters['P13-I01']`. P13 closes v0.2 by
combining backlog hygiene (W01) with four net-new features (W02 B009,
W04 B019, W05 B015, W07 B017), the self-eval scoring loop (W03 B042),
the version-bump + CHANGELOG mine (W06), and this final CORE
ship-gate.

## Per-wave verdicts

| Wave | Backlog / scope | Verdict |
|---|---|---|
| W01 | Backlog hygiene — 13 closures (B025, B027, B029-B034, B037-B041) | pass |
| W02 | B009 end-to-end golden scenarios | pass |
| W03 | B042 self-eval semantic scoring | pass |
| W04 | B019 audit-check DSL skeleton (D02 resolved) | pass |
| W05 | B015 session-level plugin-mode hooks | pass |
| W06 | Version bump 0.1.0 → 0.2.0 + CHANGELOG mine | pass |
| W07 | B017 user-scope install probe | pass |

## Per-criterion verdicts

| Criterion | Verdict |
|---|---|
| W01 — 13 backlog items closed against resolving commit + audit | pass |
| W02 — `tests/golden/scenarios/` ships fresh_repo + enrich_existing + flow_full + byte-stable agents_md projection | pass |
| W02 — `golden_scenarios` pytest marker registered in `pyproject.toml` | pass |
| W02 — `uv run pytest -m golden_scenarios -v` — 4 passed | pass |
| W03 — `eawf.eval.score.score_envelope` weighted scoring over 6 dimensions (status, body_keys, warnings ±1, repair ±1, evidence_refs presence, state_mutation kinds) | pass |
| W03 — `EvalScore` Pydantic v2 model with `extra="forbid"` + `frozen=True` + per-dim breakdown | pass |
| W03 — `test_skill_envelope_score_meets_threshold` parametrised over 6 skills meets 0.85 floor | pass |
| W03 — 19 unit tests in `tests/unit/test_eval_score.py` cover boundary + error per dimension | pass |
| W04 — `eawf.audit_dsl` package: `CheckSpec`/`CheckResult`/`CheckFile` Pydantic v2 models, frozen `CHECK_REGISTRY` | pass |
| W04 — 5 initial check kinds: file_exists, path_glob_nonempty, regex_in_file, state_field_equals, command_exit_zero | pass |
| W04 — `eawf audit run --checks <yaml>` wires the DSL; `--fixture` stays as escape hatch; mutual exclusion enforced | pass |
| W04 — 38 unit tests + canonical `tests/golden/audit_dsl/sample.yaml` exercising every kind | pass |
| W04 — `docs/architecture/audit-checks.md` documents the grammar + sandbox-policy boundary (B049 follow-up) | pass |
| W05 — `runtimes/claude/hook_map.py` exports `PluginHookSpec` + 6-entry `PLUGIN_HOOK_REGISTRY` (`SessionStart`/`Stop` + `PreToolUse`/`PostToolUse` on `Bash` `git commit`/`git push`) | pass |
| W05 — `plugin_package` renders `hooks.json` at plugin root + 6 `hooks/<event>.sh` wrappers via `render_hook_sh`; default `include_hooks=True`; `PackageResult` carries `wrote_hooks` | pass |
| W05 — `docs/architecture/plugins.md` § hooks rewritten; `README.md` parenthetical removed; eawf-internal lifecycle events stay fired by state CLI | pass |
| W05 — `test_package_emits_full_tree` + 3 new integration tests pin the 6-wrapper count + `${CLAUDE_PLUGIN_ROOT}` portability | pass |
| W06 — `pyproject.toml` + `uv.lock` + `src/eawf/__init__.__version__` bumped 0.1.0 → 0.2.0 | pass |
| W06 — `eawf --version` reports `0.2.0` | pass |
| W06 — `CHANGELOG.md` [0.2.0] - 2026-05-11 section mines P07..P13 into Added/Changed/Fixed groups; known-limitations rolled to v0.3 | pass |
| W06 — `docs/architecture/profiles.md` + `docs/policy/fixed-decisions.md` scrub stale "deferred to v0.2" claims invalidated by W04 | pass |
| W06 — Backlog items B048 (state CLI version-target setter) and B049 (sandbox-policy enforcement in command_exit_zero) filed for v0.3 | pass |
| W07 — `eawf doctor --user-scope` probes `uv tool list`, reports ok / warn / info / warn-uv-missing without crashing when uv absent | pass |
| W07 — `update_plugin(check=True)` delegates to `install_plugin(dry_run=True)`; no bytes written | pass |
| W07 — 10 unit tests under `tests/unit/test_doctor_user_scope.py` cover all four probe states + check-mode | pass |
| W07 — `docs/architecture/installation.md` § "User-scope install" documents `uv tool install --from . eawf` + plugin update/doctor probes | pass |
| Full pytest `2029 passed, 12 deselected in 204.24s` | pass |
| Eval gate `12 passed, 2029 deselected in 3.54s` (6 shape + 6 score-threshold) | pass |
| Mypy `Success: no issues found in 220 source files` | pass |
| Ruff lint clean on `src/` + `tests/` | pass |
| Pre-commit clean after secrets-baseline line-number refresh + ruff-format reflow; no `--no-verify` bypasses | pass |
| `eawf doctor` overall warn (manifest_in_sync warns because no workspace anchor; not introduced by P13) | pass |
| `eawf doc verify` reports 33 drift entries — identical count on `main`; drift is the gitignored `.claude/` plugin tree, not introduced by P13 | pass |
| Commit-prefix discipline: every commit is `[P13-W0N]` or `[P13-CORE]`; no untagged commits | pass |

## Aggregate verdict

**pass.** P13 closes v0.2 by landing four net-new features (B009 /
B019 / B015 / B017), the self-eval scoring loop (B042), a 13-item
backlog hygiene sweep, and the `0.1.0 → 0.2.0` version bump. Three
decisions are recorded (D09 defers PyPI publish to v0.3, D10 keeps
the one-PR-per-phase cadence through v0.2 close, D11 locks P13 scope
at the 7 declared waves). The audit-check DSL skeleton resolves D02.
Two v0.3 carry-overs are filed as backlog items (B048 version-target
setter, B049 sandbox-policy enforcement).

## Evidence

- `git log 02edd44..HEAD --oneline` — seven `[P13-W0N]` commits +
  CORE state commits flanking each wave.
- `uv run pytest -q` — `2029 passed, 12 deselected in 204.24s`.
- `uv run pytest -m eval -q` — `12 passed, 2029 deselected in 3.54s`.
- `uv run pytest -m golden_scenarios -q` — `4 passed`.
- `uv run mypy src/` — `Success: no issues found in 220 source files`.
- `uv run pre-commit run --all-files` — every hook passes (after the
  secrets-baseline line-number refresh in the prior CORE commit).
- `uv run eawf --version` — `0.2.0`.
- `uv run eawf doctor --user-scope` — runs clean; reports `warn`
  because the locally-`uv tool install`'d eawf at `v0.1.0` differs
  from the new `0.2.0` — exactly the stale-detection contract.
- `uv run eawf audit run A99-SMOKE --scope-id EAWF --kind evaluation
  --checks tests/golden/audit_dsl/sample.yaml` — verdict pass, all
  DSL checks emitted in `check_results`.

## Out of scope (rolled forward to v0.3 backlog)

- PyPI publish (B005, D09 — defer to v0.3).
- Quant + ML profile bodies (B006/B007) — DSL is functional, bodies
  still pending.
- True user-scope plugin auto-update (background process); P13 W07
  ships the probe + `--check` mode only.
- `state.project.version_target` setter (B048).
- Sandbox-policy enforcement at DSL `command_exit_zero` dispatch
  time (B049).
- `eawf doc verify --strict` in pre-commit (carried from P12 audit).
- `eawf wiki render` as post-merge CI artefact (carried from P12).

## Carry-over to v0.3 backlog (not blocking P13)

- Background process for plugin auto-update (W07 only ships the
  probe).
- PyPI publish workflow + tag-driven release (D09).
- Quant + ML profile bodies on top of the v0.2 DSL skeleton.
