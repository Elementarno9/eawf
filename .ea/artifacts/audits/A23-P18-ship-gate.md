# A23-P18 ship-gate audit

## Summary

- P18 state records W01 through W12 closed and leaves W13 as the final audit
  wave, with dependencies spanning report primitives, hooks, renderers,
  validation, docs, and policy updates [1].
- Strict typed report payloads, append-only report storage, and the
  `agent_end` hook writer cover schema validation, attempt persistence, and
  runtime ingestion for agent completions [2][3][4].
- Runtime hook wiring, report CLI rollups, Markdown renderers, PR-body
  integration, and cross-kind invariants cover the operator-facing report
  surfaces and the phase readiness checks [5][6][7][8][9].
- Rendered agent contracts, architecture docs, AGENTS managed rules, and the
  fresh-repo AGENTS scenario golden now reflect the typed `agent_end` report
  contract for new and existing workspaces [10][11][12][13].
- Ship-gate verification passed after refreshing the scenario golden:
  `uv run ruff check .`, `uv run mypy src/eawf`, and `uv run pytest -q`
  (`2373 passed, 12 deselected`) [13][14].

## References

[1] .ea/state.json
[2] src/eawf/store/kinds/agent_report.py
[3] src/eawf/agent_report/store.py
[4] src/eawf/cli/commands/hook.py
[5] src/eawf/render/hooks.py
[6] src/eawf/cli/commands/agent_report.py
[7] src/eawf/render/agent_report.py
[8] src/eawf/render/pr_body.py
[9] src/eawf/validate/invariants.py
[10] src/eawf/render/agents.py
[11] docs/architecture/agent-reports.md
[12] AGENTS.md
[13] tests/golden/scenarios/fresh_repo/agents.golden.json
[14] tests/golden/scenarios/test_scenarios.py

## Provenance

- kind: ship-gate
- phase: P18
- iter: P18-I01
- audit_id: A23-P18
- artifact_id: ART-A23-P18
- scope_id: P18
- branch: feature/eawf-v0.3
- verification: `uv run ruff check .`; `uv run mypy src/eawf`;
  `uv run pytest -q`

## Scrub

- status: clean
