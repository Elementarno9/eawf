# A19-P14 ship-gate audit (P14-I04 close)

Audit of the P14-I04 follow-up iter — Codex plugin schema
compliance. Root cause from operator report: ten registered eawf
skills landed at `~/.codex/plugins/cache/eawf-local-codex/eawf/1.0/`
after `codex plugin marketplace add`, yet none of them were loaded
by Codex. Investigation (research stage) surfaced three schema
violations against the Codex Build-plugin reference
(https://developers.openai.com/codex/plugins/build) and the Agent
Skills reference (https://developers.openai.com/codex/skills):

- **S1 Wrong skill layout.** Codex requires `skills/<name>/SKILL.md`
  (directory) per the Agent Skills spec; eawf was emitting flat
  `skills/<name>.md` files, which the Codex skill loader skips.
- **S2 Unreachable `agents/`.** Codex `plugin.json` has no
  top-level `agents` key; agent configs live nested inside skills as
  `skills/<name>/agents/openai.yaml`. eawf was rendering eight
  top-level `agents/<role>.md` files that Codex never reads.
- **S3 Missing `interface{}` block.** `.codex-plugin/plugin.json`
  carried only the canonical fields (`name`, `version`,
  `description`, `skills`, `hooks`). Without `interface.displayName`
  / `category` / etc the marketplace picker shows the plugin as raw
  name only.

Five waves closed (chronological):

- `2b07ea9` `[P14-I04-W01]` `eawf phase reopen` CLI + transition
- `90197ea` `[P14-I04-CORE]` state: reopen P14, open P14-I04
- `0305ece` `[P14-I04-W03]` drop `agents/` from codex render (S2)
- `e916bd4` `[P14-I04-W04]` skill render uses `skills/<name>/SKILL.md` (S1)
- `2a7ac18` `[P14-I04-W05]` add `interface{}` block to plugin.json (S3)
- `d548b3a` `[P14-I04-W03]` followup: drop "agents" from
  `_PLUGIN_DESCRIPTION` constant (post-W03 polish)

W03 / W04 / W05 executed in parallel via worktree-isolated
subagents; the parent worktree cherry-picked all three in order
with conflicts resolved in `_render_manifest` docstring,
`install_plugin.py` layout block, `plugin_package.py` layout block,
and `test_plugin_install_codex.py` (intersecting test assertion
blocks).

## Per-criterion verdicts

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | `eawf phase reopen` lifecycle transition + CLI (W01) | pass | `reopen_phase` flips status closed→active, clears `closed_at`, preserves `audit_id`; only takes over `current.phase_id` when no other phase is active. Covered by five new unit tests + four integration tests; full `test_lifecycle_transitions.py` and `test_cli_lifecycle.py` green. |
| 2 | Codex install emits no top-level `agents/` (S2) | pass | `_agent_target` / `_render_agent` / `AGENT_REGISTRY` import removed from `plugin_install.py`, `plugin_package.py`, `plugin_doctor.py`; `InstallResult.agents` and `PackageResult.agents` fields dropped; CLI fanout (`_codex_install_payload` etc.) caught. Negative regression: `test_install_creates_plugin_layout` asserts `(root / "agents").exists() is False`. Live verify: `find ~/.codex/plugins/eawf -type d` returns only `skills/`, `hooks/`, `.codex-plugin/`. Claude runtime AGENT_REGISTRY usage untouched. |
| 3 | Codex skills emitted as `skills/<name>/SKILL.md` dir layout (S1) | pass | `_skill_target` now returns `plugin_root / "skills" / spec.skill_name.lstrip("/") / "SKILL.md"`; same in `plugin_package.py`. `test_install_creates_plugin_layout` asserts the directory + file existence for every entry in `SKILL_REGISTRY`. Live verify: `~/.codex/plugins/eawf/skills/audit/SKILL.md` (plus nine siblings) present; no `skills/audit.md` flat file remains. |
| 4 | `plugin.json` carries an `interface{}` block (S3) | pass | `_render_manifest` body adds `interface.displayName="Eä Workflow"`, `category="Productivity"`, short/long descriptions, `developerName`, `capabilities=["Write"]`, `defaultPrompt`, `screenshots=[]`, `brandColor="#6B7280"`. URL fields (`websiteURL`, `privacyPolicyURL`, `termsOfServiceURL`) and asset paths (`composerIcon`, `logo`) deliberately omitted (PII / path-hygiene rule). Two tests cover shape + the negative URL-omission invariant. |
| 5 | `_PLUGIN_DESCRIPTION` matches reality (polish) | pass | Constant now reads "Eä Workflow plugin — agent-driven development skills and hooks." Old wording "skills, agents, and hooks" would have leaked into both the manifest `description` field and the marketplace short summary. |
| 6 | Full pytest passes | pass | `uv run pytest tests/ -q`: **2249 passed, 12 deselected** in 142s. |
| 7 | `pre-commit run` clean on every changed file | pass | ran in each worktree before its commit, and in the parent worktree after each cherry-pick. |
| 8 | Live install reflects the schema (smoke test) | pass | `uv run eawf plugin install codex --scope user` rewrites `~/.codex/plugins/eawf/` with 10 skill dirs each containing `SKILL.md`, 14 hooks, manifest carrying `interface` block, no `agents/` dir. `uv run eawf plugin package codex --target /tmp/eawf-codex-smoke` produces a marketplace-ready tree with the same layout. |
| 9 | State reflects reality | will pass after this audit registers + closes W01/W03/W04/W05/I04/P14 | next CORE commit. |

## Operator-visible additions

- `eawf phase reopen <PHASE_ID>` is now a first-class CLI command —
  prior to this iter the only way to put a closed phase back into
  the active state was hand-editing `state.json` (which would
  violate rule 4: state CLI is the only mutator).
- `eawf plugin install codex` and `eawf plugin package codex` no
  longer write a top-level `agents/` directory. Operators upgrading
  from prior installs should manually `rm -rf ~/.codex/plugins/eawf/agents`;
  the doctor's `legacy_paths` reporter still flags flat
  `<target>/.codex/agents/` for the same reason.
- `~/.codex/plugins/eawf/skills/` is now a dir-of-dirs instead of a
  flat-file directory. Any external tooling that walked the prior
  layout needs to use the directory form.
- The plugin's marketplace picker entry now carries `displayName`,
  `shortDescription`, `category`, and a default-prompt suggestion;
  previously rendered as raw `name` only.

## Non-blocking follow-ups

- `_PLUGIN_VERSION` is still `1.0`. A bump to `1.1` would let
  operators distinguish caches from the schema-compliant render;
  deferred — the manifest already changes byte-for-byte so doctor
  detects drift either way.
- `DoctorEntry.kind` still carries a stringly-typed `str` with a
  Literal in a comment — pre-existing, untouched here.
- `_render_opencode_agent_md` still omits the opencode `permission`
  ACL object — pre-existing P14-I03 follow-up, untouched.
- No per-skill `agents/openai.yaml` is emitted yet — Codex supports
  optional nested agent configs but our `AgentSpec` registry isn't
  attached to specific skills. Defer to a future iter when an actual
  operator-facing need surfaces.

## Verdict: PASS

P14-I04 closes the codex plugin schema-compliance gap discovered
post-A18. All three root causes (skill layout, agents slot,
interface block) addressed with regression tests and live verify on
the operator's machine. Full test suite green; pre-commit clean;
state model intact.
