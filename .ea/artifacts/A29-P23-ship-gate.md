# A29-P23 ship-gate audit

## Summary

- P23 (C01-IMPL kernel) shipped 3 waves inline under iter P23-I01; phase verdict **minor** (pass-with-followups: implementation correct + tests green + schema regen clean; 4 criterion-text drifts identified) [1].
- W01 ship: `URN_KINDS` extended from 10 to 26 single-word tokens per c01-foundations §5.2.2 + operator D1 2026-05-16; `_SLASH_KINDS` extended to 10 kinds (`+spec +report +event +memory +session +plugin +mcp`); module docstring documents the URN-only rename from underscored `agent_report` to single-word `report` per §7.1 item 3; `src/eawf/schemas/state.schema.json` regenerated (3 occurrences of the URN-pattern alternation expanded); 18 new unit tests (parametrised round-trip across all 26 kinds + slash-id acceptance + count + membership) [2].
- W02 ship: minimum `Principal` Pydantic `_StrictModel` added per c01-foundations §5.3.19 + Q3 2026-05-18 (`id: PrincipalIdStr ^u-[0-9a-f]{8}$`, `kind: Literal["operator","agent","cli"]`, `display_name: str`); `EventPayload.actor_principal_id: str | None = None` placeholder added per XB08; Principal model unreferenced from `State` so `schema_version` literal stays `"1.0"`; 7 Principal unit tests + 2 `EventPayload` placeholder tests [3].
- W02 deferral: criterion 1 (`Cost.attributed_to: Literal['cli'] = 'cli'`) was honestly deferred — no `Cost` class exists in `src/eawf/`; c01-foundations §5.3.19 line 791 frames `Cost.attributed_to` as an in-prose code-comment sketch, not a v0.3 landing target. Spike brief §3 W02 surface predates this admission. Followup tracked in I02 [4].
- W03 ship: `SpecStatus` StrEnum added to `src/eawf/state/enums.py` (DRAFT → READY → IMPLEMENTED → ARCHIVED per c01-foundations §5.4.15); `src/eawf/lifecycle/spec.py` exposes the canonical DAG (`SPEC_TRANSITIONS` dict + `validate_spec_transition` + `next_spec_statuses` + `is_terminal_spec_status` + `SpecTransitionError`); library-private (no CLI surface) per c01-foundations §7.3; 19 unit tests covering DAG match + status coverage + 3 legal edges + 9 illegal edges + 3 self-loops + terminal helpers [5].
- W03 scope reduction: criterion text named "phase/iter/wave/spec" transitions but the wave honestly delivered Spec-only. Phase/Iter/Wave transitions already exist in `src/eawf/lifecycle/transitions.py` (pre-P23). Followup tracked in I02 [4].
- Local pre-commit gauntlet (`uv run pre-commit run --all-files`) ✅ pass on every commit (ruff, ruff-format, trim-whitespace, eof-fixer, yaml, toml, large-files, merge-conflict, debug-statements, detect-secrets) [6].
- Full test suite (`uv run pytest tests/ -q`) ✅ 3285 passed, 12 deselected in 220s (35 new tests across W01..W03 + 1 schema-regen-driven test_state_schema_committed_matches_generated reset) [6].
- Schema regeneration ✅ exactly 4 URN-pattern alternation occurrences expanded; no other field touched [7].

## Followups (deferred to P23-I02)

- **F1** W01 criterion 4 — rewrite from "backward-compat alias maps legacy underscored agent_report token" to "URN-only rename per c01-foundations §7.1 item 3; `agent_report` URN form rejected; Python class name `AgentReportBody` unchanged". No alias was needed.
- **F2** W01 + W03 success-criteria + file_scopes — name real test paths (`tests/unit/test_urn.py`, `tests/unit/test_lifecycle_spec.py`). The wildcards `tests/unit/test_state_urn*.py` + `tests/unit/test_lifecycle*.py` match nothing on disk.
- **F3** W02 criterion 1 — drop `Cost.attributed_to` (no `Cost` class exists; spec line 791 is a code-comment sketch) OR add explicit deferral row; criterion 3 — admit that the runtime Principal model shipped (it exceeds the "no runtime model yet" wording).
- **F4** W03 criterion 1 — narrow scope text from "phase/iter/wave/spec" to "Spec lifecycle DAG helpers + SpecStatus enum vocabulary". Phase/Iter/Wave transitions already live in `transitions.py`.

Followups are criterion-text drift only. **No code change required.** Closed-wave success-criteria fields are immutable per AGENTS rule 20 — the I02 cleanup wave will write a post-mortem artifact + (optionally) propose a typed drift-detector lint that future waves run as a pre-close gate.

## References

[1] `.ea/state.json` — `state.waves["P23-I01-W##"]` outcome strings + closed_at + commit SHA chain (W01=5f39d62, W02=1fd64bc, W03=c88cf50)
[2] commit `5f39d62 [P23-W01] feat: URN_KINDS expansion to 26 + slash-friendly extension + golden fixture` + `src/eawf/state/urn.py` + `src/eawf/schemas/state.schema.json` + `tests/unit/test_urn.py`
[3] commit `1fd64bc [P23-W02] feat: minimum Principal model + EventPayload.actor_principal_id placeholder` + `src/eawf/state/models.py` + `src/eawf/store/kinds/event.py` + `tests/unit/test_state_models.py` + `tests/unit/test_kinds.py`
[4] Auditor agent_end report — eawf:auditor session via `/audit` 2026-05-18; per-criterion verdict table inlined into the orchestrator's flow envelope; refutations recorded for W02-1 + W03-1
[5] commit `c88cf50 [P23-W03] feat: Spec lifecycle DAG helpers (SpecStatus enum + transition guard)` + `src/eawf/state/enums.py` + `src/eawf/lifecycle/spec.py` + `tests/unit/test_lifecycle_spec.py`
[6] local `uv run pre-commit run --all-files` + `uv run pytest tests/ -q` invocations during W03 close
[7] `git diff 5f39d62~1..HEAD -- src/eawf/schemas/state.schema.json` — URN-pattern alternation alone changed
[8] `.ea/local/research/2026-05-18-p23-c01-kernel-waves.md` — P23 spike brief

## Provenance

- audit_id: A29-P23-ship-gate
- audit_kind: ship-gate
- scope_id: urn:eawf:v1:repo:eawf
- verdict: minor (pass-with-followups; 4 criterion-text drifts → I02 cleanup)
- created_at: 2026-05-18
- author: claude-opus-4-7 (session eawf-flow-p23-inline; auditor subagent a36531aa993...)
- supersedes: none (P23 is a new phase)

## Scrub

- status: clean
- references: repo-relative paths + commit SHAs only
- local paths: none in body
- real emails: none
- abstract placeholder names: not applicable
