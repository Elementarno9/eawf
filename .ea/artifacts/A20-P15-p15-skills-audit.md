# A20-P15 skills audit

Audit of the staged P15 implementation for skills work:
`/research` flags and persistence, registered `/blitz`, and executable
workspace/user skill overlays.

## Scope

- `src/eawf/skills/research.py`
- `src/eawf/skills/blitz.py`
- `src/eawf/skills/bodies/blitz.py`
- `src/eawf/cli/commands/skill.py`
- `src/eawf/render/envelope.py`
- `src/eawf/render/skills.py`
- `src/eawf/schemas/skill-output.schema.json`
- `tests/unit/test_skill_research.py`
- `tests/unit/test_blitz_recursion_guard.py`
- `tests/unit/test_cli_skill.py`
- `tests/unit/test_schema.py`
- `tests/golden/test_envelope_fixtures.py`
- `tests/golden/plugin_install/claude/skills/blitz/SKILL.md`

## Findings and fixes

| Finding | Severity | Status | Evidence |
|---|---:|---|---|
| Workspace/user overlays with canonical builtin names were still shadowed by in-process builtins, so a workspace `/research` overlay could not execute through `eawf skill run`. | P1 | fixed | Overlay lookup now runs before builtin dispatch in `src/eawf/cli/commands/skill.py`; `test_skill_run_workspace_overlay_overrides_builtin_name` covers the case. |
| Envelope schema/golden tests still treated unknown string skill names as invalid after opening `header.skill` for executable overlays. | P1 | fixed | Fixture tests now validate `/blitz` plus a workspace overlay string and keep invalid coverage via non-string `skill`; `test_schema.py` asserts the open-string contract. |
| Research persistence footer originally risked recording a bare brief id instead of the store artifact URN. | P2 | fixed | `/research final` now records the research store URN in `persisted_store_records`. |
| `/research` auto-chain needed to propagate `/blitz` recursion-cap exhaustion instead of hiding it behind an otherwise ok research body. | P2 | fixed | The research skill returns `blocked` with the blitz repair command when the blitz recursion guard is exhausted. |

## Verification

- `rtk uv run pytest tests/unit/test_skill_research.py tests/unit/test_blitz_recursion_guard.py tests/unit/test_skill_registry_user_catalogue.py tests/unit/test_cli_skill.py`
- `rtk uv run pytest tests/integration/test_skill_run_research.py tests/golden/test_plugin_install_claude.py`
- `rtk uv run pytest tests/golden/test_envelope_fixtures.py tests/integration/test_plugin_install_full.py tests/integration/test_validate_cli.py tests/unit/test_schema.py tests/unit/test_cli_skill.py -q`
- `rtk uv run pre-commit run --all-files`
- `rtk git diff --cached --check`

All commands passed after the fixes listed above.

## Verdict: PASS

P15 implementation satisfies the planned skill behavior and registry
seam criteria in the staged diff. Audit is registered through the EAWF
artifact and audit CLIs so state remains CLI-mutated.
