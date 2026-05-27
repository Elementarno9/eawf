# Profile picker walkthrough

*Choose the policy bundle that matches the repo before the init wizard renders managed files.*

Profiles are composable bundles of rules, runtime defaults, hooks, and generated agent text. The picker appears in the [`/init` pipeline](../architecture/workflow.md#init-pipeline-dag) after project identity and before plugin / MCP selection, because profile composition must complete before `AGENTS.md`, `CLAUDE.md`, and runtime plugin trees are rendered.

For the lower-level model, see [Profiles](../architecture/profiles.md). For the command-only bootstrap path, see the [Quickstart](quickstart.md).

## 1. Start with a repo

Run the wizard from the repository root:

```bash
eawf init
```

For a deterministic dry-run shape in scripts, pass the target and profile flags explicitly:

```bash
eawf init --target . --project-code DEMO --project-title "Demo Project" --profiles core,python
```

`core` is the default. Add domain profiles only when the repo needs their rules and state keys; every enabled profile can affect generated docs, hooks, MCP recommendations, and acceptance checks.

## 2. Read the recommendation

The wizard detects repository signals such as `pyproject.toml`, `package.json`, existing test commands, and prior Eä files. It then proposes a profile set before it writes anything.

```text
Profile selection
  detected:
    - pyproject.toml
    - pytest config
  recommended:
    [x] core       required baseline
    [x] python     Python style, uv, pytest discipline
    [ ] research   hypotheses, audits, decisions as state
    [ ] quality    opt-in code-craft rules
```

Annotation:

- `detected` names repo evidence, not final policy.
- Checked rows are the proposed profile set.
- Unchecked rows are available but not selected.
- Custom workspace or user profiles can trigger a trust prompt before composition.

Text screenshot: [`docs/_static/tutorial/profile-picker.txt`](../_static/tutorial/profile-picker.txt).

## 3. Pick the smallest useful set

Use these defaults as a starting point:

| Repo shape | Profiles |
|---|---|
| Any managed repo | `core` |
| Python package or service | `core,python` |
| Evaluation-heavy research workflow | `core,python,research` |
| Reverse-engineering project | `core,reverse-engineering` when the bundled template fits |

If the wizard recommends a profile because it found a tool file, but the repo does not want that workflow enforced, leave the profile unchecked. The rendered `AGENTS.md` is policy, so avoid enabling aspirational rules.

## 4. Use templates for known bundles

Bundled init templates are shortcuts for common profile sets:

```bash
eawf init --list-templates
```

Current templates:

```text
engineering
research
reverse-engineering
```

Apply one when the repo matches the preset:

```bash
eawf init --target . --project-code DEMO --project-title "Demo Project" --template engineering
```

Do not combine `--template` with `--profile` or `--profiles`; the init command treats those modes as mutually exclusive so the final profile set stays unambiguous.

## 5. Confirm composition before render

Before the wizard writes files, inspect the plan summary. Profile composition must happen before generated agent files are rendered.

```text
Write plan
  profile set: core, python
  create:
    .ea/config.yaml
    .ea/state.json
    .ea/acceptance.yaml
    AGENTS.md
  render after composition:
    AGENTS.md
    CLAUDE.md
    runtime plugin tree
```

Choose `edit` if the profile set is wrong. Choose `apply` only when the selected profiles match the repository's real workflow.

## 6. Verify after init

Run validation after the wizard completes:

```bash
eawf validate --strict .ea/state.json
eawf doctor
```

For profile-specific checks:

```bash
eawf profile validate --all
```

If validation fails, fix the profile source or init answers, then re-run the owning command. Do not hand-edit rendered `AGENTS.md` to change profile behavior.
