# Python package structure review (compounded: Codex + Claude)

**Date:** 2026-05-24
**Skill:** `/research`
**Status:** draft, local, gitignored (`.ea/local/`)
**Question:** Is the `eawf` Python package well-designed, or is it hard to
navigate because it has too many small subpackages, stale modules, or obsolete
files? If refactor is warranted, what concrete tasks — and does a **deeper,
layered package hierarchy** fit a project this complex?
**Operator steer (2026-05-24):** prefer a **deeper hierarchy** (group the flat
top-level packages into layered domains) rather than flatten; review file
removals; target a **P27-I05** hygiene iter.

---

## Summary

The package is structurally sound by Python packaging standards: `src/` layout,
console scripts via `pyproject.toml`, tests outside the package, `py.typed`
shipped, strict mypy / ruff / coverage gates [1][2][9][10][11]. That matches
current PyPA and pytest guidance [14][15].

The navigation pain is **real but mis-attributed to "too many single-file
subpackages."** A scan found 472 source modules, 70 packages, but only **7–8**
true single-module leaf packages — and every one of them is a **complete,
heavily-wired domain seam**, not a stub. The real friction is:

1. **A flat list of ~44 top-level packages at one level** — no layered grouping
   to carry the architecture's mental model. This is what the operator wants
   fixed via a deeper hierarchy.
2. **God-files** — `config/registry.py` (1999 loc) plus 6 modules >1000 loc, all
   complexity-grandfathered (`C901`) [10].
3. **`cli/app.py` registration scroll-wall** + 60 files in `cli/commands/` with
   no maintainer source-map [12].
4. **Doc drift** — README/overview still describe v0.1 state-CLI-only mutator
   authority, contradicting the daemon-canonical-mutator rule [4][5][7].
5. **A small, real removal set** (see Removal Sweep): one dead helper, one
   duplicate module, two unused deps, one mis-classified dep.

Recommended path: **(a)** ship the zero-risk wins first (source-map doc, doc
refresh, dep hygiene, dead-file removals); **(b)** then pursue the operator's
deeper hierarchy as a *guarded, incremental* regroup with transitional
re-export shims — because the blast radius is large (3355 import lines, 249
string module-path refs, golden fixtures).

---

## Method

Two independent read-only passes, reconciled here.

- **Codex pass:** package metadata, scripts, lint/coverage/build config, CLI
  root registration, AGENTS/README/docs, template/resource packages, WAL admin
  surface, gitignored generated files; AST inbound-import scan; package/file
  counts; largest modules; `rg` for stale/deferred markers; PyPA/pytest/
  setuptools guidance; popular trees (Requests, pytest, Django, Pydantic).
- **Claude pass:** per-package real-module distribution; `__init__` facade vs
  empty audit; god-file census vs the `pyproject` `C901` list; heuristic
  unused-module scan verified by hand; review of the operator-run `vulture` /
  `deptry` outputs (`.ea/local/vulture.txt`, `.ea/local/deptry.txt`); v0.4/v0.5
  design-doc check for CLI-dispatch plans; regroup blast-radius probe (string
  module refs, golden fixtures, import-line counts).

No source, state, or version-controlled artifact mutated. Brief is local-only.

---

## What the code does (reconciled facts)

### Packaging shape — sound

`src/` layout, Hatchling build, wheel packages `src/eawf`, `py.typed` shipped,
console scripts `eawf`/`ea`/`eawfd`, strict mypy over `eawf`, pytest → `tests/`
[9][10][11]. Aligns with PyPA `src`-layout and pytest good-practice [14][15].

### Package-count reality

| Metric | Value |
|---|---:|
| Source modules under `src/eawf` | 472 |
| Packages (dirs with code) | 70 |
| Top-level packages (real) | ~44 |
| Max nest depth | 4 (`store/kinds/events/`) — fine |
| Pkgs w/ 0 real modules (namespace/resource) | 9 |
| Pkgs w/ 1 real module | 8 |
| Pkgs w/ 2 real modules | 17 |
| `__init__` re-export facades (non-empty) | 65 / 69 |
| Modules >1000 loc | 7 |
| Functions `C901`-grandfathered in `pyproject` | 30+ |

### Single-module leaf packages — complete, not stubs

The operator's worry ("single-file subpackages = unfinished?") is **disproven**.
Each is fully wired:

| pkg/module | loc | src callers | tests | verdict |
|---|---:|---:|---:|---|
| `scrub/scan.py` | 94 | 8 | 3 | complete, core (PII scanner) |
| `sandbox/policy.py` | ~200 | 7 | 4 | complete, core (dispatch deny-list) |
| `vcs/coauthor.py` | 203 | 8 | 2 | complete, core (Co-Authored-By) |
| `dispatch/renderer.py` | 529 | many | yes | complete, core (wave dispatch) |
| `logging/scrub.py`, `docs/autogen.py`, `runtimes/probes/sdk_baseline.py` | — | yes | yes | complete domain seams |

→ **No "implement these" work exists.** They are single-*module* only because
the domain is small. Both passes agree: do **not** flatten them.

### Top-level weight (navigation hotspots)

| Group | Files | ~LOC | Note |
|---|---:|---:|---|
| `cli` | 75 | 28k | hotspot; `app.py` scroll-wall + 60 command files |
| `runtimes` | 50 | 9k | legit adapter domain |
| `skills` | 43 | 8k | logic (`skills/X.py`) + envelope-body (`skills/bodies/X.py`) split — deliberate |
| `tui` | 39 | 11k | legit UI domain |
| `daemon` | 27 | 9k | legit process/RPC domain |
| `store` | 27 | 1.7k | many tiny typed event payloads — acceptable |
| `render` | 24 | 6.5k | legit render boundary |

God-files (all `C901`): `config/registry.py` 1999, `cli/commands/repo.py` 1348,
`render/skills.py` 1272, `cli/commands/plugin.py` 1243,
`tui/screens/overlays/config_modal.py` 1140, `cli/commands/memory.py` 1042,
`skills/flow.py` 1024.

### `cli/app.py` registration wall

Real logic up top, then a long `# --- W## registrations ---` tail of
`from … import … ` + `app.add_typer(...)` per wave [12]. Wave-era provenance
comments in source brush rule 25.

### Doc drift

README:34 still frames the state CLI as the mutator [5]; AGENTS rule 4 says the
**daemon** is canonical mutator with the state CLI as proxy/fallback [4];
`docs/architecture/overview.md` package tree is v0.1-shaped [7]. New readers
trust docs, then hit a different tree — compounding navigation pain.

### Tooling-output review (Claude)

- **`vulture` → all noise** [25]. Every hit is a known false-positive class: Typer
  `@app.command()` handlers, Pydantic `model_config`/validators, `__exit__`
  params, the `model_hint` forward-contract param (live spawn lands P26), and
  the intentional `yield`-after-`raise` carrying
  `# pragma: no cover — keeps the generator typed`. **No vulture deletions.**
  To make it usable, add an allowlist for Typer+pydantic or drop it.
- **`deptry` → the only real dep signal** [26], after filtering its config gaps
  (`DEP001 eawf` = src-layout misconfig; `pyyaml` false-positive — used via
  `import yaml`; `pywin32` import-guarded Windows-only; `mkdocs*` CLI-invoked).

---

## Standards baseline

PyPA: `src` vs flat layout; `src` prevents importing the in-dev checkout [14].
pytest: tests should run against the installed package; `--import-mode=importlib`
+ `src` recommended [15]. Hatchling here; same structural standards [16][17].

| Project | Lesson | vs `eawf` |
|---|---|---|
| Requests | flat modules under one package [21] | too flat for `eawf` (daemon/TUI/runtime/CLI are separate products) |
| pytest | `src/_pytest` mixes modules + real subpackages (`assertion`, `config`, `_io`, `_code`) [22] | closest match: big tool, many domains |
| Django | domain packages (`apps`, `conf`, `contrib`, `core`, `db`, `forms`) [23] | **precedent for layered domain grouping** |
| Pydantic | module-first + subpackages only for stable boundaries (`_internal`, `v1`, `plugin`) [24] | warning: package only when boundary is durable |

**Reconciliation with the operator steer:** the popular-tree survey says "don't
flatten," not "don't deepen." Django is direct precedent that a broad product
surface earns *layered domain packages*. A two-level hierarchy is defensible for
a 44-package, multi-product tool — provided the grouping edges are principled
and the migration is mechanical + guarded.

---

## Deeper hierarchy — the operator's preferred shape

Goal: replace a flat list of ~44 top-level packages with **~6 layered
super-packages** so the import path encodes the architectural layer. The
project's own phase vocabulary already names these layers
(KERNEL → DAEMON → CONTRACTS → SURFACES → OBSERV) — the package tree should
mirror it.

### Candidate target tree

```text
src/eawf/
  kernel/         # typed domain core + persistence (P23-KERNEL)
    state/  store/  config/  validate/  spec/  migrations/
  workflow/       # research + lifecycle entities & contracts (P25-CONTRACTS)
    lifecycle/  evidence/  skills/  agents/  agent_report/
    audit_dsl/  dispatch/  pr_review/  estimation/
  runtime/        # process / integration plane (P24-DAEMON)
    daemon/  runtimes/  mcp/  sandbox/  session/  lock/
    budget/  ci_loop/  worktree/  hooks/  vcs/
  surfaces/       # human-facing output (P26-SURFACES)
    cli/  tui/  render/
  observability/  # telemetry + health (P27-OBSERV)
    telemetry/  logging/  doctor/  bench/  eval/
  platform/       # cross-cutting bootstrap & assets
    profiles/  registry/  install/  templates/  artifacts/
    memory/  scrub/  lint/  backup/  docs/
  schemas/   _data/   py.typed        # resource/data — unchanged
```

Public-API stability via each super-package `__init__` re-export facade (the
existing dominant pattern: 65/69 inits already re-export).

### Honest caveats (both passes + blast-radius probe)

- The grouping **has arbitrary edges.** `platform/` is a catch-all; `memory`,
  `artifacts`, `estimation`, `vcs` could each sit in two layers. Expect bikeshed.
- **Blast radius is large:** 3355 `from eawf.` import lines, **249** string
  `"eawf.<pkg>"` refs (3 are `importlib.resources`/dynamic loaders that mypy/
  ruff will **not** catch — `eawf.templates`, `eawf.templates.claude`,
  `eawf.runtimes`; the rest live in tests/specs asserting module paths), plus
  golden fixtures embedding module paths.
- A **source-map doc captures ~80% of the navigation win at ~2% of the risk.**
  The deeper hierarchy is the operator's stated preference and is defensible,
  but it should be done *after* the cheap wins and *incrementally*.

### Safe migration recipe (per super-package, one wave each)

1. Create `eawf/<layer>/` and `git mv` the member packages in.
2. Add **transitional re-export shims** at the old top-level paths
   (`eawf/state/__init__.py` → `from eawf.kernel.state import *`) so the 3355
   imports keep working during transition.
3. Codemod imports with `libcst`/`bowler` (not sed — respects strings/comments);
   hand-audit the 249 string refs + `importlib.resources` names + golden
   fixtures.
4. `uv run mypy src/` + full test suite + import-budget perf tests green.
5. Remove the shims in a final wave once no old-path imports remain.

---

## Consolidated removal sweep (operator asked: what can we delete?)

| Path / item | Evidence | Verdict |
|---|---|---|
| `src/eawf/cli/dispatch.py` (`register_subcommand`) | Zero call sites; root uses `app.add_typer` directly in `cli/app.py`; untouched since P02 [8][12]. v0.4 "dispatch" = wave→role agent dispatch (`dispatch/renderer.py`), a different concept — checked the v0.4/v0.5 design doc [27], `register_subcommand` appears in **no** spec. | **DELETE** (record verdict in state.json per rule 6). v0.4 will **not** require it. |
| `src/eawf/state/schema.py` (`dump_schemas`/`generate_state_schema`) | Prod path `cli/commands/schema.py` → `docs/autogen.py` has its **own** `dump_schemas` and imports only `state.enums`; `state/schema.py` reachable only from `tests/unit/test_schema.py`. Duplicate impls = drift trap. | **DEDUP** — keep `docs/autogen.py`, delete `state/schema.py`, repoint the test. |
| `platformdirs` (runtime dep) | Zero use in src/tests/benches. | **REMOVE** from `pyproject`. |
| `pydantic-settings` (runtime dep) | Zero use anywhere. | **REMOVE** from `pyproject`. |
| `jsonschema` (runtime dep) | Imported only in `tests/`. | **RECLASSIFY** runtime → dev/test dep. |
| `src/eawf/daemon/methods/wal_admin.py` | No non-test caller; docstring: "main dispatcher does NOT import it yet — W09 wires it in when `state.mutate` is delivered" [13]; test imports manually [6]. | **KEEP + TRACK** — it is a *forward contract* for the v0.4 daemon-mutator wave (AGENTS rule 4), not dead. Add a backlog item so it cannot rot silently; revisit at v0.4 `state.mutate`. |
| `templates/`, `templates/claude/`, `templates/init/`, `runtimes/opencode/templates/` | Resource packages loaded via `importlib.resources.files("eawf.templates…")` [18][19][20]. | **KEEP** (and they pin layer placement in any regroup). |
| Single-module leaf pkgs (`scrub`, `sandbox`, `vcs`, …) | Fully wired (table above). | **KEEP** — no flatten, no implement. |
| `__pycache__` / `*.pyc` | None tracked; ignored [17]. | No action. |

`vulture`-only items (`model_hint`, `yield`-after-`raise`) are intentional — **no
action.** All deletions gated by the project deletion rule (committed ancestry +
state.json verdict + commit-body enumeration) [3].

---

## Backlog title+description rollout (operator-requested 2026-05-24)

The `BacklogItem` **model is already correct** — `title`
`Annotated[str, max_length=72]` + optional `description`
`Annotated[str, max_length=500]` (matches the entity-title rationale in
AGENTS). The gap is **data + surfaces**, not schema:

- **Data:** all 65 backlog items have `description = None`; the `title`
  carries everything (some are label-lists, e.g. B008 at 54 chars).
- **CLI:** `backlog add` exposes `--title` but **no `--description`**, and
  there is **no `backlog edit`/`update` verb** at all (only `add` /
  `set-priority` / `close`) — so existing items' descriptions cannot be set.
- **TUI:** `description` is rendered **nowhere** — the backlog table columns
  are `(id, priority, status, title)` and the detail card shows
  `(id, title, priority, status, resolution?)`. No description block.

This is genuinely undone and worth doing; folded into P27-I05 below (W11–W12).

## Proposed P27-I05 — structure & hygiene iter

Operator approved a P27-I05. Note: P27-I04 (TUI richer views, PLANNED, 12 waves)
is **not** the home — mixing a package regroup into a TUI iter violates
separation-of-concerns. EU ≈ 25–30 min agent-driven. Group the cheap, low-risk
wins first; gate the regroup behind them.

| Wave | Task | Type | EU | Risk |
|---|---|---|---:|---|
| W01 | `docs/architecture/source-map.md` — top-package table, "start here" entry points, mutation-authority map, command→module map, resource-package notes, test-location map; README links it | doc | 1–2 | none |
| W02 | Refresh stale docs — README mutator wording → daemon-canonical; `overview.md` package tree → current shape | doc | 1 | none |
| W03 | Dep hygiene — drop `platformdirs` + `pydantic-settings`; move `jsonschema` to dev; `deptry` config (`known_first_party=eawf`, `package_module_name_map{pyyaml=yaml}`, `docs` extra); add to CI | chore | 1 | low |
| W04 | Delete `cli/dispatch.py` (+ state.json verdict) | delete | 0.5 | low |
| W05 | Dedup schema dump — keep `docs/autogen.py`, delete `state/schema.py`, repoint `test_schema.py` | dedup | 1–2 | med |
| W06 | Add backlog item tracking `wal_admin.py` → v0.4 `state.mutate` wiring | state | 0.5 | none |
| W07 | Thin `cli/app.py` — declarative registry list `(module, attr, name, panel)`; preserve lazy imports for import-budget perf; normalize wave-era comments (rule 25) | refactor | 2–3 | med |
| W08 | Split `config/registry.py` → `config/` submodules (models / coercion / io / defaults / formatting); facade re-exports; drop from `C901` list | split | 4–6 | med |
| W09 | Split `cli/commands/{repo,plugin,memory}.py` + `render/skills.py` + `skills/flow.py` + `tui/.../config_modal.py` | split | 3 ea | med |
| W10 | Dead-file advisory script under `tools/` (AST inbound-zero report; allowlist entry points + `importlib.resources` pkgs); report-only, not CI-gating | tooling | 1–2 | none |
| W11 | Backlog CLI — add `--description` to `backlog add`; new `backlog edit` verb (set `title`/`description`); `edit_backlog` in `evidence/backlog.py`; wire `lint_entity_title` backstop | feat | 2 | low |
| W12 | Rewrite all 65 backlog items — concise ≤72 `title` + populated ≤500 `description` (split label from prose); via `backlog edit` (daemon-canonical mutator) | state | 2–3 | low |
| W13 | TUI — render `description` in the backlog detail card (`detail.py`) as a wrapped block under the title; table columns unchanged | feat | 1–2 | low |
| W14+ | **(gated, optional)** Deeper hierarchy — one super-package per wave with transitional shims + `libcst` codemod + string-ref/golden audit; final shim-removal wave | restructure | 3–5 ea | high |

Suggested order: **W01→W02→W03→W04→W05→W06 (cheap, do now) → W11→W12→W13
(backlog rollout) → W07→W10 → W08/W09 (god-files) → W14+ (hierarchy, only if
greenlit after a spike).**

---

## Decision options

1. **Hygiene-first** (recommended, low risk): W01–W06 + W10 now; defer god-file
   splits and the hierarchy. Immediate navigation gain via the source-map.
2. **Hygiene + god-file splits** (medium): add W07–W09. Bigger payoff; CLI
   autogen + import-budget perf are the regression surfaces.
3. **Full deeper hierarchy** (operator-preferred, high risk/reward): W14+ after a
   spike validates the codemod + string-ref audit on one super-package
   (`kernel/` is the cleanest pilot). Do **only** with transitional shims.

---

## References

[1] `AGENTS.md` — CLI dispatch / strict validation rule.
[2] `AGENTS.md` — CLI parses/formats; library implements domain logic.
[3] `AGENTS.md` — deletion rule (committed ancestry + verdict + enumeration).
[4] `AGENTS.md` rule 4 — daemon canonical mutator; state CLI proxy/fallback v0.3-v0.5.
[5] `README.md:34` — stale state-CLI mutator wording.
[6] `tests/daemon/test_wal_admin_cli.py` — WAL admin registers only on manual import.
[7] `docs/README.md`, `docs/architecture/overview.md` — design-intent docs; v0.1-shaped tree.
[8] `src/eawf/cli/dispatch.py:1-22` — unused `register_subcommand` helper.
[9] `pyproject.toml` — console scripts, pytest, strict mypy, Hatchling.
[10] `pyproject.toml` — ruff `C901` grandfather list + EAWF010/011 gates.
[11] `pyproject.toml` `[build-system]`/`[tool.hatch…]` — wheel `src/eawf`.
[12] `src/eawf/cli/app.py:194-524` — command registration wall + wave-era comments.
[13] `src/eawf/daemon/methods/wal_admin.py:1-15` — dispatcher does not import yet; W09 future wiring.
[14] PyPA, "src layout vs flat layout": https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/
[15] pytest, "Good Integration Practices": https://doc.pytest.org/en/latest/explanation/goodpractices.html
[16] setuptools, "Package Discovery and Namespace Packages": https://setuptools.pypa.io/en/stable/userguide/package_discovery.html
[17] `.gitignore` — Python cache, generated data, local EAWF dirs.
[18] `src/eawf/templates/__init__.py` — resource package (`importlib.resources.files("eawf.templates")`).
[19] `src/eawf/templates/claude/__init__.py` — Claude templates resource package.
[20] `src/eawf/templates/init/__init__.py` — init templates resource package.
[21] Requests source tree: https://github.com/psf/requests/tree/main/src/requests
[22] pytest source tree: https://github.com/pytest-dev/pytest/tree/main/src/_pytest
[23] Django source tree: https://github.com/django/django/tree/main/django
[24] Pydantic source tree: https://github.com/pydantic/pydantic/tree/main/pydantic
[25] `.ea/local/vulture.txt` — vulture run (all false-positive classes).
[26] `.ea/local/deptry.txt` — deptry run (DEP002 platformdirs/pydantic-settings/jsonschema).
[27] `.ea/local/research/2026-05-23-v04-v05-design-and-spec.md` — v0.4 "dispatch" = wave→role agent dispatch, not `cli/dispatch.py`.

---

## Provenance

Two read-only passes on 2026-05-24 (Codex + Claude), reconciled. Probes:
`git ls-files`, `find`/`wc -l` over `src`/`tests`, AST + `grep` inbound-import
scans, per-package real-module distribution, `C901` cross-check, hand-verified
unused-module scan, review of operator-run `vulture`/`deptry` outputs, v0.4/v0.5
design-doc check, and a regroup blast-radius probe (string module refs, golden
fixtures, import-line counts). No source/state/VCS artifact mutated.

---

## Scrub

- status: clean
- No absolute local paths. No hostnames. No secrets. No real emails.
- External references are public documentation or public GitHub source trees.
