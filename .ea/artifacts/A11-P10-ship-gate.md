# A11-P10 ship-gate audit

Fresh-context auditor verified P10 (multi-runtime: skill discovery API +
MCP scoping) against the four waves defined in
`.ea/local/research/p10-plan.md` and the roadmap ship-gate criterion
"run the same wave under Claude Code and a second harness" from
`.ea/local/research/survey-2026-05-10-roadmap.md`.

## Per-criterion verdicts

| Criterion | Verdict |
|---|---|
| W01 — `eawf skill render <name> --format=skill-md` byte-equal to `runtimes/claude/plugin_install.py` SKILL.md (shared `render_skill_md_from_spec` helper) | pass |
| W01 — `eawf skill render <name> --format=json` returns the `_list_payload` keys plus a `body` field; bare and slashed forms both accepted; unknown skill → `InvalidInput` exit 3 | pass |
| W01 — `_list_payload` surface contract pinned by golden test; no migration / no schema regen | pass |
| W02 — `McpGrant` _StrictModel with fields `id` (`^GRANT-[0-9]+$`), `scope_kind` (Literal["wave","profile","global"]), `scope_id`, `server_id`, `granted_at` | pass |
| W02 — `eawf mcp grant <scope_kind> <scope_id> <server_id>` + `eawf mcp revoke <grant_id>` route through `state_transaction`; auto-id picks `GRANT-<max+1>`; `--grant-id` override | pass |
| W02 — `INV.REF.MCP_GRANT_SERVER_MISSING` invariant fires + rolls back when `server_id` dangles; registered in `ALL_INVARIANTS` | pass |
| W02 — `state.schema.json` regenerated; diff scoped to the new `McpGrant` `$def` + `mcp_grants` nullable map | pass |
| W03 — `_SUPPORTED_RUNTIMES` extended; `claude-agent-sdk` registered without a new runtime dependency (`pyproject.toml`/`uv.lock` unchanged) | pass |
| W03 — `render_dispatch_envelope` pure function returns typed `DispatchEnvelope` for both branches; CLI surface `wave dispatch <wave> --runtime=...` validates against pool with canonical `unknown runtime ...; expected one of [...]` message | pass |
| W04 — dual-runtime envelopes are prompt-byte-equal modulo the `runtime` + SDK-only `mcp_servers`/`allowed_tools` fields | pass |
| W04 — wave-scoped grant for the dispatched wave projects to `["mcp__<server_id>__*"]`; grants scoped to a different wave do NOT leak | pass |
| W04 — empty / null `mcp_grants` ⇒ both envelopes render cleanly, SDK `allowed_tools` is `[]` | pass |
| W04 — `claude-code` envelope omits the SDK-only keys (`mcp_servers`, `allowed_tools`) | pass |
| Pytest 1901 passing (1895 pre-W04 + 6 new ship-gate integration cases) | pass |
| Pre-commit clean on every wave commit (no `--no-verify` bypasses) | pass |
| Commit-chain prefix discipline: `[P10-W0N]` / `[P10-CORE]` on every commit | pass |
| No merge commits on `feature/eawf-v0.2-p10-multi-runtime` (cherry-pick-only) | pass |

## Aggregate verdict

**pass.** The mind-change criterion from the research brief — "if the
dual-runtime envelopes diverge in a non-trivial way, `McpGrant` needs
richer fields and W02 must reopen" — did not trigger. Both envelopes
agree on the prompt body byte-for-byte; the SDK branch adds exactly the
typed superset (`runtime`, `mcp_servers`, `allowed_tools`) the planner
predicted.

## Evidence

- `git log feature/eawf-v0.2-p10-multi-runtime ^main --oneline` — seven
  commits with `[P10-W0N]` / `[P10-CORE]` prefix discipline intact.
- `git log feature/eawf-v0.2-p10-multi-runtime ^main --merges` — zero
  output (cherry-pick-only).
- `uv run pytest tests/integration/test_dual_runtime_envelope.py -q` —
  `6 passed in 2.50s`.
- `uv run pytest -q` — `1901 passed`.
- `uv run pre-commit run --all-files` — clean.
- `uv run eawf wave graph --iter P10-I01` — W01, W02, W03 closed; W04
  in_progress at audit time, closed on register.
- `uv run eawf wave dispatch P10-I01-W01 --runtime=claude-code --json |
  jq 'keys'` — `["prompt","runtime","wave"]`.
- `uv run eawf wave dispatch P10-I01-W01 --runtime=claude-agent-sdk
  --json | jq 'keys'` — `["allowed_tools","mcp_servers","prompt",
  "runtime","wave"]`.

## Out of scope (deferred to P11+)

- B043 federated user-skill registry (project-scoped only in v0.2).
- Profile-level `mcps_referenced` preset (v0.3 follow-up).
- Opencode installer path (B015) — runtime stays in the literal enum
  for hook events; no installer.
- Hooks-bridge for non-CC harnesses (separate phase).

## Carry-over to v0.3 backlog (not blocking P10)

- SDK envelope `system_prompt_addendum` per-grant body field — research
  brief flagged this as a possible richer-grant evolution. The
  envelope-equivalence demo did not require it, but a follow-up phase
  could add it without a schema break (additive nullable field on
  `McpGrant`).
- Cross-runtime smoke harness in CI — the integration test exercises
  both runtimes via the Typer CLI; a future job could shell out to a
  real `claude-agent-sdk` invocation and assert the envelope is
  consumed without modification.
