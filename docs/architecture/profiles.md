# Profiles and composition

*Declarative bundles of rules, generated files, skills, agents, hooks, MCP suggestions, and check commands per domain or runtime.*

Projects can enable multiple profiles. Each profile is a YAML file with
optional sections that compose deterministically into one merged
configuration: rules, agents, skills, hooks, MCP recommendations, check
commands, state extensions, memory policies, and installer questions.

## v0.1 profile shipping status

- **Functional**: `core`, `python`, `research`.
- **Catalog stub** (rule text only, profile body pending): `quant`,
  `ml`. The audit-check DSL skeleton landed in v0.2 (P13 W04); the
  profile bodies (B006/B007) are still on the v0.3 backlog.
- **Catalog stub** (no body): `re`, `game`, `apps`, `infra`, `docs`,
  `robotics`.

Profile combinations example:

- `core + python + research`
- `core + quant + ml + research + python`
- `core + apps + infra + docs`

## Profile schema

Top-level optional sections shared across profiles:

```yaml
instruments:
  hard: [git]              # missing → abort skill (composes strictest-wins)
  soft: [gh]               # missing → record not_configured + degrade
subprojects:
  scan_paths:
    add: []         # extend framework defaults (src/, packages/, apps/, services/)
    replace: []     # override defaults entirely
```

### Rule schema

Each rule entry under `rules.add` accepts the following fields:

```yaml
rules:
  add:
    - id: verify-before-claiming      # required, stable, kebab-case
      severity: required              # required | recommended | optional
      summary: "Verify quantitative claims against actual code path before assertion."
      body: |
        Verification ladder, in order:
        1. Read source file
        2. grep for actual call sites in active branch
        3. Inspect run artifacts if claim is about a measured run
        4. Quote the number only after the above
      cite: docs/specs/verify-before-claiming.md   # optional, link to deeper spec
```

Render rules:

- `summary` is the canonical one-line bullet rendered into the rule
  module's bullet list.
- `body` is optional markdown rendered as a nested subsection under the
  bullet. Bullet lists, code fences, tables, and inline links are
  preserved verbatim.
- `cite` renders as an inline `→ <path>` link after the summary.
- Rules without `body` render as plain bullets.
- `severity: required` rules are always rendered; `recommended` and
  `optional` may be hidden by `.ea/config.yaml` filters.

Composition: when two profiles `add` rules sharing the same `id`, the
strictest `severity` wins; `body` from the higher-priority profile
replaces, never concatenates (use `extends` for additive composition).

## v0.1 profile bodies

### `python.yaml`

```yaml
id: python
version: 1
name: Python
summary: Python/uv project conventions and checks
priority: 50
requires: [core]
applies_when:
  files_any: [pyproject.toml, uv.lock, requirements.txt]
instruments:
  hard: [uv]
  soft: [pre-commit]
rules:
  add:
    - id: python-use-uv-run
      severity: required
      summary: Use `uv run` for all Python invocations when uv detected.
      body: |
        Never invoke `.venv/bin/python` or bare `python`. Always `uv run python`,
        `uv run pytest`, `uv run ruff check`. Reason: uv lockfile guarantees
        reproducible env; bare invocations bypass the lock.
    - id: python-fstrings-only
      severity: required
      summary: f-strings only — no `%`-formatting or `.format()`.
      body: |
        f-strings are the canonical Python 3 string-format. Mixed styles
        complicate grep + readability. Logger calls included: `logger.info(f"...")`.
    - id: python-stdlib-logger
      severity: required
      summary: Library modules use `logger = logging.getLogger(__name__)`.
checks:
  detect:
    tests: ["uv run pytest"]
    lint: ["uv run ruff check"]
    typecheck: ["uv run mypy", "uv run pyright"]
    format: ["uv run ruff format"]
hooks:
  recommend:
    - id: post-edit-python-lint
      default: ask
      risk: low
mcp:
  recommend:
    - id: context7
      default: ask
agents:
  enable: [executor, auditor, reviewer]
  extensions:
    executor:
      tool_permissions:
        bash_allow_prefixes: ["uv run "]
installer_questions:
  - id: python.package_manager
    prompt: Package manager?
    choices: [uv, pip, poetry]
    default: uv
```

### `research.yaml`

```yaml
id: research
version: 1
name: Research
summary: Hypothesis-driven research conventions, peer review, evidence rigor
priority: 60
requires: [core]
applies_when:
  files_any: [.ea/artifacts/research]
  dirs_any: [research, hypotheses]
instruments:
  hard: []
  soft: [zotero]
rules:
  add:
    - id: research-hypothesis-required
      severity: required
      summary: Non-trivial scope requires research brief + registered hypothesis before /prep.
    - id: research-peer-review
      severity: required
      summary: /research dispatches one red-team reviewer per brief; rubber-stamp forbidden.
    - id: research-verify-before-claim
      severity: required
      summary: Quantitative claims verified against actual code path before assertion.
    - id: research-no-tautology
      severity: required
      summary: Evaluation indices must not overlap any fitting stage.
skills:
  enable: [research, hypothesis, incident]
  config:
    research:
      auto_save: false
      default_depth: normal
      default_sources: both
      peer_review: required
      agent_count: 4
audit:
  kind_default: evaluation
  evaluation_checks:
    - lookahead_bias
    - mz_tautology
    - oos_overlap
    - is_vs_oos_gap
mcp:
  recommend:
    - id: zotero
      default: ask
    - id: context7
      default: ask
agents:
  enable: [researcher, auditor, planner, executor, reviewer]
state_extensions:
  fields_required: [hypotheses, audits]
```

### `quant.yaml` (catalog stub for v0.1)

```yaml
id: quant
version: 1
status: stub_v0_1
requires: [core, python, research]
subprojects:
  scan_paths:
    add: [src/quant_research/strategy/, docs/strategies/]
rules:
  add:
    - id: quant-configs-committed
      summary: Behavior-defining configs in /configs/, committed.
    - id: quant-data-manifest
      summary: ML/backtest runs log config snapshot + data manifest (files+hashes+row counts+date ranges) + report HTML.
    - id: quant-symbol-classes
      summary: Symbol classes EQY/IDX/FUT/OPT/ETF per docs/specs/data.md.
    - id: quant-no-data-deletion
      summary: /data/, run artifacts, evaluation reports, briefs, milestones never deleted by agents.
audit_checks_planned_v0_2:
  - report_html_present
  - run_link_recorded
  - data_manifest_hash_recorded
  - walk_forward_no_overlap
mcp:
  recommend:
    - id: mlflow
      default: ask
      env_refs: [MLFLOW_TRACKING_URI]
```

### `ml.yaml` (catalog stub for v0.1)

```yaml
id: ml
version: 1
status: stub_v0_1
requires: [core, python, research]
rules:
  add:
    - id: ml-no-future-leakage
      summary: No full-sample normalization, no forward-looking rolling windows, no target-as-feature.
    - id: ml-stacker-walkforward
      summary: Walk-forward the stacker; never fit meta-learner on accumulated OOS then evaluate same data.
    - id: ml-mlflow-required
      summary: Every model train logs config + data manifest to a tracking server.
    - id: ml-oos-sanity
      summary: OOS metrics beating literature benchmarks by >20% must be audited for leakage.
audit_checks_planned_v0_2:
  - tracking_integrity
  - hpo_eval_disjoint
  - ensemble_overlap_warnings_respected
```

## What profiles do

- Add or tighten generated `AGENTS.md` rules.
- Enable default skills / agents and runtime render targets.
- Recommend hooks / MCPs / tools during install.
- Detect acceptance commands and quality gates.
- Add domain-specific audit checks and state validation rules.
- Configure memory scopes and injected context budgets.
- Add templates: commit / PR / audit / research / incident variants.
- Ask install questions only relevant to the detected project type.

## Merge / composition algorithm

1. Resolve profile graph: load selected profiles, then `requires`, then
   `extends`; detect cycles.
2. Sort by priority, then explicit user order; `core` always first.
3. Deep-merge maps by key; concatenate ordered lists with stable IDs.
4. For entities with `id` (`rules`, `hooks`, `mcp`, `skills`, `agents`,
   `checks`), merge by ID.
5. Conflicts:
   - safety: strictest wins,
   - required rule vs removed rule: prompt,
   - command / check collision: keep all candidates, detect best at
     install,
   - MCP / tool risk downgrade: forbidden unless user confirms,
   - template default collision: prompt and persist decision.
6. Apply removals / deprecations after additions.
7. Validate composed config against schema.
8. Persist composition result and user conflict decisions in
   `.ea/config.yaml` so `eawf sync` is deterministic.

Composition example:

```yaml
profiles:
  enabled: [core, quant, ml, research, python]
composition:
  safety_policy: strictest_wins
  conflict_resolution: prompt
  mcp_default: opt_in
  persisted_decisions:
    template.pr: iter
    vcs.commit_template: state_scoped
```

## Cross-references

- AGENTS.md / CLAUDE.md generation policy — `docs/policy/agents-claude-md.md`.
- Profile composition source — `src/eawf/profiles/compose.py`,
  `src/eawf/profiles/loader.py`.
- Profile YAML files — `src/eawf/profiles/*.yaml`.
- Plugin and adapter model — `docs/architecture/plugins.md`.
