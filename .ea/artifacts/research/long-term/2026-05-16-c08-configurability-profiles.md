# C08 — Configurability + Profile composition — Eä framework long-term specs

**Cluster:** C08 (Configurability + Profile composition — layered config taxonomy, field registry, profile manifest, composition loader, conflict + override grammar, bootstrap templates, schema migration)

**Title:** Configurability + Profile composition

**Status:** `local-draft`, `accepted` (operator ratified §4 D1-D12 + §8 Q1-Q15 on 2026-05-16)

**Created:** `2026-05-16T00:00:00Z`

**Author:** `claude-opus-4-7`

**Depends on:** C00 (verdicts V3, V5, V7, V8) [1], C01 (Profile entity URN kind + lifecycle; persona authority matrix; entity catalog) [2], C02 (daemon RPC `daemon.reload_config` + `runtime.set_preference` methods; config layer is daemon-mediated mutator) [3]

**Consumed by:** C03 (spec validators are profile-gated), C04 (skill manifest contributes via profile; `dispatch.session_policy` per profile), C05 (`eawf config` + `eawf profile` verb-noun surface), C06 (TUI config modal reads merged config + provenance), C10 (per-profile docs + bootstrap-template tutorials)

## 1. Purpose + scope statement

C08 makes V3 [1:76-96] implementable. The brief locks the **layered config taxonomy** (six precedence-ordered layers including a new branch layer), the **complete field registry** (every configurable surface enumerated with type + default + writable-layer), the **profile manifest schema** (`ProfileBody` v2 with `conflicts_with` + `overrides` per V3 + `dispatch_session_policy` per V8 + `state_extensions` + `contributes`), the **composition loader algorithm** (order-respecting, conflict-checked, provenance-tracked), the **conflict + override resolution rules**, **five bootstrap profile examples** (research / engineering / reverse-engineering / spike / hybrid), and the **migration plan** from today's three-functional-profile + nine-stub-profile reality [4:42-49] to the V3 composable bundle.

Today's surface is partially in place. Layered config has six layers (`built-in / global / workspace / repo / local / env / cli`) wired through `merge_config()` [5:264-363]; only writable layers are file-backed [5:55-60]. `ProfileBody` is closed-schema Pydantic v2 [6:74-94] with five typed fields (`state_extensions`, `instrument_requirements`, `render_blocks`, `skills_referenced`, `hooks_referenced`); `compose()` deep-merges by id with strictest-wins on `instrument_requirements[].kind` [7:175-227]. Three functional profiles (`core`, `python`, `research`) ship with bodies; nine catalog stubs (`apps`, `docs`, `game`, `infra`, `ml`, `quant`, `re`, `robotics`, `a11y`) ship as name-only YAML [4:42-49]. `ConfigKey` registry [8:54-99] enumerates 24 operator-tunable keys across 9 tabs (audit, estimation, planning, research, runtime, ship, ui, vcs, worktrees). `enable_profile()` [9:152-222] writes `profiles.enabled` to a layer + materialises `state_extensions.fields_required` keys on `state.json`.

C08 extends:

- **Branch layer** (new) — `<repo>/.ea/branches/<branch>.yaml` between `repo` and `local`; resolution via `git symbolic-ref --short HEAD` [§4 D1]; `/` in branch name maps to subdirectory (Q9 locked: subdirectory form).
- **Wave layer** (transient, in-memory) — daemon-resident map keyed by `wave_id`; sourced from `Wave.runtime_preference` [2:362-365] + per-wave overrides written via `wave.set_config` RPC.
- **`conflicts_with: list[str]`** + **`overrides: list[str]`** fields on `ProfileBody` per V3 [1:78-79]; loader fails fast when undeclared overlap detected [§5.4].
- **`dispatch_session_policy: Literal["fresh","continue","hybrid"] | None = None`** on `ProfileBody` per V8 [1:266-269]; profile contributes default to skills under its scope.
- **Goal system** — `goals: list[str]` (free-form) + `success_metrics: dict[str, float]` (per project) under the project layer [§5.6].
- **Five bootstrap templates** — `research`, `engineering`, `reverse-engineering`, `spike`, `hybrid` — selectable at `eawf init --profiles a,b,c` [§5.7].
- **Schema migration** — `config.schema_version` bump procedure with one-shot writers + rollback [§7].

**In scope (C00 §C08 [1:715-763]):**

- Layered config taxonomy (six layers + new branch layer + transient wave layer); precedence table.
- Field registry (every configurable key — current 24 + V5 `runtime.fallback.*` + V7 `telemetry.*` + V8 `dispatch.session_policy` + new `goals` / `success_metrics`).
- `ProfileBody` v2 schema (manifest YAML) with `conflicts_with`, `overrides`, `dispatch_session_policy`, `contributes` rollup.
- Composition loader algorithm: load N profiles in declared order, validate conflict declarations, apply contributions with override precedence, emit composed-view envelope.
- Conflict + override resolution rules (conflict = fail-fast; override = a's contributions win + audit-recorded).
- Project-type bootstrap templates: research / engineering / reverse-engineering / spike / hybrid.
- Goal system: `goals: list[str]` + `success_metrics: dict[metric_id, target]`.
- Language / locale: code-language preference (Python today; Rust/PyO3 at v0.5+ trigger only per language-fit brief [10:114-119]); human-language i18n deferred.
- Hook customization (project hooks vs global hooks, registration model).
- Skill enable/disable per project (profile-gated).
- Config schema migration: `config.schema_version: N` field; migration scripts on bump.

**Out of scope (deferred):**

- Per-runtime config translation (deferred to C07 [1:733]) — daemon owns the per-runtime `claude.md` / `<local-path>` write side.
- TUI config modal (deferred to C06 [1:733]) — C08 specifies what's editable; C06 specifies how.
- Telemetry DB schema (V7 → C09 [1:188-224]) — C08 specifies the `telemetry.*` config fields; C09 specifies the rollup.
- Per-runtime SDK adapter pick (V8 → C07 [1:670-679]) — C08 specifies the per-profile `dispatch_session_policy` default; C07 specifies how each runtime adapter honors it.
- Multi-tenant config (multi-user OS) — deferred to v0.6+ governance per C01 D4 [2:Q4].

## 2. Goals + non-goals

### Goals

| G# | Goal | Source |
|---|---|---|
| G1 | Layered config taxonomy is six-deep with declared precedence; every key resolves to exactly one source layer; provenance reconstructible by `eawf config get --explain`. | C00 §C08 [1:720]; today's `(merged, source_map)` shape [5:264-363] |
| G2 | Branch layer is first-class; `git symbolic-ref --short HEAD` resolves the current branch; branch override file lives at `<repo>/.ea/branches/<branch>.yaml`; loader skips silently when not in a git tree or symbolic-ref fails. | C00 §C08 axes [1:738]; V3 composability [1:78-83] |
| G3 | Wave layer is transient — daemon-resident map keyed by `wave_id`; written via `wave.set_config` RPC; reset when wave closes. | V8 per-wave runtime override [1:140-141]; C01 Wave fields [2:362-365] |
| G4 | `ProfileBody` v2 adds `conflicts_with`, `overrides`, `dispatch_session_policy` fields; existing fields preserved verbatim so v1 YAMLs continue to validate. | V3 conflict-declaration semantics [1:78-79]; V8 [1:266-269] |
| G5 | Composition loader fails fast on undeclared conflict + records override provenance; deterministic output regardless of caller order (except `render_blocks` where caller order locks slots, per current rule [7:178-188]). | V3 hard non-negotiable [1:78]; existing compose rule [7:178-188] |
| G6 | Field registry enumerates every operator-tunable key: type, default, writable-layer set, doc-string. Single source of truth shared by CLI menu + TUI modal. | Current `CONFIG_REGISTRY` shape [8:106-302]; C00 axis [1:721] |
| G7 | Five bootstrap profile templates ship: research / engineering / reverse-engineering / spike / hybrid. `eawf init --profiles a,b,c` writes `.ea/config.yaml` + scaffolds the materialised state keys. | C00 §C08 axes [1:744]; profiles-fulfilment Tier C [4:147-156] |
| G7b | **(Q14 verdict, Tier B in C08 ship)** Nine catalog-stub profiles (`apps`, `infra`, `docs`, `ml`, `quant`, `game`, `re`, `robotics`, `a11y`) gain real bodies in C08 ship: each ≥1 render_block + ≥1 instrument or state_extension. Stub-warning emit logic retired (no profile-enable warns under the new bundled set). | profiles-fulfilment Tier B [4:142-145]; Q14 verdict 2026-05-16 |
| G8 | Goal system: `project.goals: list[str]` + `project.success_metrics: dict[str, float]` per project; per-subproject rollup is deferred. | C00 §C08 axes [1:725-726] |
| G9 | Profile authoring API: YAML at `<repo>/.ea/profiles/*.yaml` (workspace-scope) + `<local-path>` (user-scope) + `eawf.profiles.data` (built-in); precedence workspace > user > built-in per profiles-fulfilment decision [4:124]. Python entry-point NOT supported in v0.3-v0.5. | profiles-fulfilment [4:124]; AGENTS rule 2 strict-schema [13] |
| G10 | Trust ledger: per-non-built-in profile content-hash entry under `profiles.trusted: {<id>: <sha256>}` in `.ea/config.yaml`; first-use prompts via `AskUserQuestion`; hash drift re-prompts. | profiles-fulfilment Tier C [4:151] |
| G11 | Language preference field `language.runtime: Literal["python"]` (locked) + `language.fast_extras: list[str]` (empty default; `["fast"]` opts into `eawf[fast]` PyO3 extra per language-fit brief [10:104-105]). | language-fit brief [10:102-119] |
| G12 | Schema migration: `config.schema_version: "1.2"` (bumped from current `"1.1"` [11:26]) drives one-shot migrator on bump; older config files refused-with-upgrade-hint rather than silently coerced. | AGENTS rule 2 [13]; current `CONFIG_SCHEMA_VERSION` [11:26] |
| G13 | Brief self-contained: V3 / V5 / V7 / V8 quoted inline; C01 + C02 cited; every source-file ref at file:line; ratifiable in one fresh CC session. | C00 V4 [1:99-125] |

### Non-goals

| NG# | Non-goal | Why deferred |
|---|---|---|
| NG1 | Per-runtime config translation (writing `<local-path>` from composed AGENTS.md). | C07 owns it [1:733]; covered by today's `eawf sync` plus future runtime-spine adapter. |
| NG2 | TUI config modal rendering. | C06 owns it [1:733]; C08 defines what's editable, C06 how. |
| NG3 | Telemetry DB schema + DuckDB rollup. | V7 → C09 [1:188-224]; C08 only defines the `telemetry.*` config keys. |
| NG4 | Skill manifest schema (full `dispatch.*` surface). | C04 owns it [1:485-534]; C08 only defines the per-profile default `dispatch_session_policy`. |
| NG5 | Python entry-point profiles. | profiles-fulfilment locked YAML-only [4:124] + AGENTS rule 2 strict-schema [13]. |
| NG6 | Profile rules-DSL (per design-doc `rules.add` etc. [4:88-93]). | The design doc has 14 fields ProfileBody lacks [4:88-93]; C08 keeps the closed schema and defers the rules DSL to a dedicated phase. |
| NG7 | Human-language i18n. | Deferred — every operator surface today is English-only; no near-term translator demand. |
| NG8 | Multi-tenant config (multi-user OS). | C01 D4 deferral [2:Q4]; v0.6+ governance phase. |
| NG9 | Migration of pre-`schema_version` configs. | The first v0.1 ship pinned `"1.0"` [11:26]; the C08 migrator only covers `1.0 → 1.1 → 1.2` because no earlier shape exists. |

## 3. Prior verdicts cited

C08 inherits four verdicts from C00 and consumes two cluster deps (C01 entity catalog, C02 daemon RPC).

### V3 — Composable profile bundle with declared precedence [1:76-96]

> "Project carries `profiles: [research, engineering, reverse-engineering, spike, ...]` ordered list. Each profile declares `conflicts_with: [...]` and `overrides: [...]`. Loader fails fast if conflict undeclared. Project carries explicit `profile_priority: [a, b, c]` for tie-breaks. Effective ruleset = union of profile contributions, conflict-resolved by precedence."

**C08 binding.** §5.3 specifies `ProfileBody` v2 with `conflicts_with: list[str]` + `overrides: list[str]` fields. §5.4 specifies the composition loader algorithm: load profiles in declared order, validate every (a, b) pair against the union of `a.conflicts_with` and `b.conflicts_with`; on any matched conflict not covered by a corresponding `overrides` declaration, raise `ProfileConflict` and return `status=blocked` envelope. Loader records per-field provenance (which profile contributed each leaf) in the composed view; overrides write `override_audit: dict[field_path, override_chain]` so a future operator can trace why a leaf landed where it did.

### V5 — Runtime fallback: reactive switchover on error [1:127-151]

> "`<local-path>`: `runtime.preference: [claude, codex, opencode]` — ordered ladder; first entry = primary. Per-scope override: `.ea/config.yaml runtime.preference: [...]`; per-wave override at `state.waves[*].runtime_preference: [...]`. Switch-on-error policy: `runtime.fallback.on_errors: [429, 5xx, timeout]` (default); operator may narrow."

**C08 binding.** §5.2 field registry adds `runtime.preference: list[str]`, `runtime.fallback.on_errors: list[str]`, `runtime.fallback.retry_policy: Literal["hybrid","backoff","immediate"]` (per C02 D12 [3:145]), `runtime.fallback.max_backoff_seconds: int` (per C02 §8 Q3 open). The per-scope override threads through layered config (user > workspace > repo > branch > local > env > cli); per-wave override lives in the wave layer.

### V7 — Telemetry: vendor agent-lens schema, rebuild inside eawf [1:184-224]

> "Telemetry opt-in by default — `telemetry.enabled: false` until operator sets it. No data leaves the machine without explicit `telemetry.export.endpoint` config (no implicit phone-home). Per-repo `event.jsonl` remains canonical; user-scope DB is a projection."

**C08 binding.** §5.2 field registry adds `telemetry.enabled: bool` (default `false`), `telemetry.export.endpoint: str | None` (default `None`), `telemetry.export.format: Literal["prom","otlp","json","csv"]` (default `"prom"`), `telemetry.window_default: str` (default `"7d"`), `telemetry.aggregate_window: str` (default `"24h"`). Schema lock prevents implicit phone-home.

### V8 — Agent dispatch: hybrid session reuse [1:226-271]

> "Skill manifest declares `dispatch.session_policy: fresh | continue | hybrid`; default `hybrid`. Skills that know better override — e.g., `/audit` always wants `fresh`; `/coauthor` always wants `continue`. Profile-gated per V3: research-profile skills may default `continue`; engineering-profile skills may default `fresh`."

**C08 binding.** §5.3 `ProfileBody` v2 adds `dispatch_session_policy: Literal["fresh","continue","hybrid"] | None = None`. When non-`None`, the composed profile contributes that policy as the default for skills under the profile's scope; skill manifest may override. §4 D10 locks per-profile defaults: research → `continue`, engineering → `fresh`, reverse-engineering → `continue`, spike → `fresh`, hybrid → `hybrid`.

### C01 — Profile entity URN + lifecycle [2:594-622, 2:1100-1123]

C08 consumes:

- **Profile URN** `urn:eawf:v1:profile:<owner>/<id>` [2:160-161] — `owner = user` for `<local-path>`; `owner = <repo-code>` for `<repo>/.ea/profiles/*`.
- **Profile lifecycle** LOADED → SHADOWED / CONFLICTED / UNLOADED [2:1100-1123]. C08 defines transitions: SHADOWED set when an overrider profile claims this one in its `overrides:` list; CONFLICTED set when undeclared overlap detected.
- **`ProfileBody` schema sketch** [2:594-616] — C08 finalises the v2 schema with `conflicts_with` + `overrides` + `dispatch_session_policy`.

### C02 — Daemon mediates config writes [3:299-336]

C08 consumes:

- **`state.mutate` is the canonical writer** for the daemon-up case [3:299]; config-layer writes go through a typed Mutation discriminator variant `WriteConfigLayer` (C03 owns the union shape; C08 names the variant).
- **`daemon.reload_config`** [3:335] re-reads merged config without daemon restart — composition loader runs against the freshly-loaded layered view; subscribers receive a `config_reloaded` push envelope.
- **`runtime.set_preference`** [3:343] mutates `runtime.preference` at the named layer; backed by the same writer path.

## 4. Decision matrix

| # | Axis | Options considered | Recommendation | Rationale |
|---|---|---|---|---|
| **D1** | Branch layer source-of-truth | (a) `git symbolic-ref --short HEAD`; (b) `git rev-parse --abbrev-ref HEAD`; (c) env var `EAWF_BRANCH` | **(a) — `git symbolic-ref --short HEAD`** | `symbolic-ref` returns the symbolic name of the ref pointed at by HEAD; fails with non-zero when HEAD is detached (which is correct — detached HEAD has no "branch" to overlay). `rev-parse --abbrev-ref HEAD` falls back to `HEAD` on detach, silently picking up the wrong layer. Env var override deferred to §8 Q2 (rarely useful in practice; CI flows already pass `--scope` explicitly). On `symbolic-ref` failure (detached HEAD or not a git tree) the branch layer silently contributes nothing — same as a missing YAML file. |
| **D2** | Profile contribution semantics | (a) Union for everything; (b) Precedence (later overrides earlier) for everything; (c) Per-field policy: union for additive lists, precedence for scalar / map values | **(c) — per-field policy** | Current `compose()` already encodes the right rule [7:175-227]: `state_extensions.fields_required` is sorted-union; `instrument_requirements[]` is keyed-merge with strictest-wins on `kind`; `render_blocks[]` is keyed-merge with later-wins on body + first-seen-position lock; `skills_referenced` / `hooks_referenced` are sorted-union. C08 extends the rule set for new fields: `conflicts_with` + `overrides` are sorted-union (declarations accumulate across profiles); `dispatch_session_policy` is last-non-None-wins (single scalar; only the rightmost profile contributing a non-None value wins). |
| **D3** | Conflict declaration grammar | (a) Symmetric — `a.conflicts_with: [b]` AND `b.conflicts_with: [a]` both required; (b) Asymmetric — either declaration is enough; (c) Either declaration plus a corresponding `overrides:` from one of them | **(c) — declaration alone fails the loader; `overrides` is the escape hatch** | If `a.conflicts_with: [b]` (or `b.conflicts_with: [a]`) is declared and both profiles appear in the project's `profiles:` list, the loader fails with `ProfileConflict` envelope unless one of them declares `overrides: [the_other]`. Symmetric requirement ((a)) is too brittle — a third-party profile cannot retroactively force the bundled `core` to declare the conflict. Asymmetric ((b)) silently picks one — surprising. Option (c) makes the operator decide explicitly: edit the profile body to add `overrides:`, or drop one of the profiles from `profiles:`. |
| **D4** | Override grammar | (a) `overrides: list[str]` — A overrides B; A's contributions win for every conflicting field; (b) `overrides: dict[str, list[str]]` — per-field override; A's contribution wins only for the named field paths | **(a) — whole-profile override (v0.3-v0.5)**; (b) deferred to v0.5+ | Whole-profile is simpler to reason about and matches the V3 "Effective ruleset = union of profile contributions, conflict-resolved by precedence" semantic [1:78]. Per-field override is the natural follow-up if operators want finer-grained control — but the v0.3-v0.5 bundle has at most ~5 profiles per project, and at that scale whole-profile suffices. Composition records `override_audit` so a future operator sees which fields the override actually claimed; if the audit shows only one field actually conflicted, the operator may want to revise the profile body to drop the unrelated overrides. |
| **D5** | Profile authoring API | (a) YAML only at `<repo>/.ea/profiles/` + `<local-path>`; (b) Python entry-point only; (c) both | **(a) — YAML only for v0.3-v0.5** | profiles-fulfilment locked YAML-only [4:124]. AGENTS rule 2 [13] requires strict Pydantic validation on ingestion; YAML at known paths is the simplest validation surface. Python entry-points re-introduce the "any installed package can change behaviour" surface that the AGENTS contract explicitly rejects [13]. Custom YAMLs are content-hash-trust-gated per §5.5; built-in YAMLs ship with the wheel and are auto-trusted [4:151]. |
| **D6** | Language extensibility | (a) Python is the only library impl for v0.3-v0.5; Rust/PyO3 trigger only on documented benchmark > 500ms; (b) Multi-language is first-class from v0.3; (c) Single locked language with no extras | **(a) — locked Python; PyO3 only on benchmark trigger** | Per language-fit brief verdict [10:102-119]: stay on Python 3.14+ through v0.4 ship; permit exactly one surgical PyO3 extension during v0.4 (P29 W04 / W06 event-source rebuilder + Merkle hash-tree verify), gated on the documented benchmark threshold. C08 reflects this with `language.runtime: Literal["python"]` (locked single-value) + `language.fast_extras: list[str]` (empty default; operator opts in to `eawf[fast]` per language-fit benchmark gate). The whole "code language preference" listed in C00 [1:726] reduces to one config knob; Rust / Go / TypeScript are deferred to v0.5+. |
| **D7** | Bootstrap template flow | (a) `eawf init --profiles a,b,c` writes config + scaffolds; (b) `eawf init --template <name>` picks a named bundle; (c) Both — `--profiles` is the explicit form, `--template` is a wrapper | **(c) — both surfaces, `--template` wraps `--profiles`. Ship 3 templates v0.3 (research + engineering + reverse-engineering); defer spike + hybrid to v0.4+ (revised 2026-05-18 per Q24)** | `--profiles research,engineering` is explicit and composable. ~~`--template hybrid` is shorthand~~ — hybrid + spike templates deferred to v0.4+ per Q24 (YAGNI trim; demand-signal unclear). C04 owns `eawf init` CLI; C08 specifies the **three** templates' YAML bodies under `templates/init/<template>.yaml` and the resolution rule (`--template X --profiles Y,Z` fails — choose one). spike + hybrid catalog rows remain in §5.7 documentation table marked `v0.4+` for future YAML body authoring. |
| **D8** | Per-profile session-policy default (V8) | (a) Single default for everyone (`hybrid`); (b) Per-profile default in the manifest; (c) Per-skill default | **(b) — per-profile in the manifest; skill manifest overrides** | Aligns with V8 [1:266-269] which explicitly names "research-profile skills may default `continue`; engineering-profile skills may default `fresh`". `ProfileBody.dispatch_session_policy: Literal["fresh","continue","hybrid"] | None = None`. Skill manifest still overrides per-skill (C04 owns). Composition rule: last-non-None-wins; if the project's profiles all leave it `None`, fall back to the global default `hybrid` (also configurable as `dispatch.session_policy_default` at the layered-config surface). |
| **D9** | Hook customization (project hooks vs global hooks) | (a) Two separate registration models — global at `<local-path>`, project at `<repo>/.git/hooks/` + `<repo>/.pre-commit-config.yaml`; (b) Single model, scope-tagged | **(a) — two separate models, both YAML-declared, both surfaced via profile `hooks_referenced`** | Today's surface is split: `<repo>/.pre-commit-config.yaml` carries the pre-commit hooks (project-scope); `<local-path>` carries the Claude-Code hooks (user-scope). C08 keeps the split. Profile `hooks_referenced: list[str]` accumulates hook IDs; `eawf sync` materialises them into the right destination per hook kind. The code-quality-profile proposal [12:99-110] specifies the `enforcement_hooks` field as a P21+ extension — C08 reserves the field name on the v2 schema for non-breaking forward compatibility. |
| **D10** | Per-profile session-policy concrete defaults (revised 2026-05-18 per Q24 — trim to 3 shipped profiles) | (a) research:hybrid + engineering:hybrid + reverse-engineering:hybrid; (b) **research:continue, engineering:fresh, reverse-engineering:continue** (3 profiles v0.3); (c) None for all (skill always decides) | **(b) — per V8 [1:266-269]; spike + hybrid defaults deferred to v0.4+ per Q24** | V8 explicitly names research:continue and engineering:fresh. Reverse-engineering inherits research's evidence-driven character (continue preserves the decompilation context). ~~spike:fresh + hybrid:hybrid~~ deferred to v0.4+ along with the templates themselves. Operator may override via `dispatch.session_policy` config key at any layer. |
| **D11** | Schema-version migration policy | (a) Auto-migrate silently on load; (b) Hard fail with upgrade hint; (c) Auto-migrate with backup + structured envelope | **(c) — auto-migrate with backup + structured envelope** | The on-disk YAML is hand-edited; a silent rewrite ((a)) loses operator-authored comments + formatting. A hard fail ((b)) blocks normal workflows on a version bump. (c) writes a backup at `<config>.bak.v<old>` before applying the migrator (one-shot Python function per bump), and emits a `config_schema_migrated` envelope so the operator sees the change. Migrators are idempotent + tested per bump. |
| **D12** | `goals` field shape | (a) `list[str]` free-form; (b) `list[Goal]` typed with id + text + verified_via; (c) Both — list at project, typed under subproject | **(a) — list[str] free-form for v0.3-v0.5; typed shape deferred** | C00 §C08 [1:725-726] specifies `goals: list[str]` (free-form) + `success_metrics: dict[metric_id, target]` (per project). Free-form list keeps the surface lightweight — projects with one goal don't need a typed schema; projects with many can move to per-subproject Goal rows in `state.json` [2:777]. Typed goal rows are already in `State` [2:777] as a separate entity; C08 does not re-spec them here. The config-layer `project.goals` is the *project-level statement*; `state.json`'s `Goal` rows are the *measurable outcomes*. |
| **D13** | Config writer authority (per Q1 supersede 2026-05-18) | (a) **layered-config writer migrates into daemon internals**; (b) layered-config writer stays canonical (path-a from XB01) | **(a) — daemon = sole writer** (revised 2026-05-18 per Q1) | Operator Q1 (2026-05-18) supersedes AGENTS rules 4 + 17: daemon is the sole canonical mutator for all stateful surfaces. The `_save_value_to_layer` writer at `eawf.cli.commands.config` migrates into daemon internals as part of the v0.4 hygiene wave. Per the migration DAG in `.ea/local/research/long-term/2026-05-18-migration-dag.md`. |
| **D14** | `telemetry.db_kind` default (per CROSS.F52) | (a) duckdb; (b) sqlite | **(b) sqlite** (revised 2026-05-18 per Q18 / c09-blitz-duckdb-sqlite r1) | C09 telemetry stack uses SQLite (locked); C08 default propagated. Migration: any operator config with `telemetry.db_kind: duckdb` auto-migrates on config-schema-bump. |
| **D15** | `actor_principal_id` placeholder field (per XB08 / Q3) | (a) not in v0.3-v0.5; (b) **placeholder field for v0.3-v0.5** | **(b) placeholder field landed** (revised 2026-05-18 per Q3) | Per Q3 (2026-05-18): minimum Principal model lands v0.3-v0.5 as placeholder. `Cost.attributed_to: Literal["cli"] = "cli"` populated when known. Full enforcement still v0.5+. |
| **D16** | `pr_merge_method` config-overridable schema entry (per F-28) | (a) globally hard-coded `"rebase"`; (b) **config-overridable in layered config** | **(b) config-overridable** (revised 2026-05-18 per F-28) | Per F-28: `pr_merge_method` belongs in the C08 field registry as config-overridable. **eawf-repo profile default = `"rebase"`** (matches memory `feedback_pr_merge_strategy`); other framework users pick per repo. Field shape: `Literal["rebase","squash","merge"]` with default-by-profile. /ship reads this at PR-merge time. |

## 5. Proposed schema, API, protocol

### 5.1 Layered config taxonomy

Six durable on-disk layers + one transient daemon-resident layer + two runtime overlays. Precedence top-to-bottom (highest wins):

| # | Layer | Path | Mutator | Scope | Status today |
|---|---|---|---|---|---|
| 7 | **cli** | (no file) | `--<key>=<value>` flags at the verb invocation | one-shot CLI process | shipped [5:50-52] |
| 6 | **env** | (no file) | `EAWF_<KEY>__<SUBKEY>=<value>` env vars | per-shell session | shipped [5:62-64, 5:179-198] |
| 5 | **wave** | (no file — daemon RAM) | `wave.set_config` RPC; tied to `Wave.id`; reset on wave-close | per-wave dispatch | **new — C08 introduces** |
| 4 | **local** | `<repo>/.ea/local/config.yaml` | layered-config writer | per-repo local working copy (gitignored) | shipped [5:50, 5:82-84] |
| 3 | **branch** | `<repo>/.ea/branches/<branch>.yaml` | layered-config writer | per-branch override (committed) | **new — C08 introduces** |
| 2 | **repo** | `<repo>/.ea/config.yaml` | layered-config writer | per-repo (committed) | shipped [5:50, 5:77-79] |
| 1 | **workspace** | `<workspace>/.ea/config.yaml` | layered-config writer | cross-repo aggregation scope | shipped [5:50, 5:72-74] |
| 0 | **global** | `<local-path>` | layered-config writer | per-user (also `<local-path>` per C00 [1:139], see §5.1.1) | shipped [5:50, 5:67-69] |
| -1 | **built-in** | (no file) | code-only [11] | wheel-shipped read-only baseline | shipped [11:29-311] |

**Resolution at load time.** `merge_config(workspace=W, repo=R, branch=B, env=E, cli_overrides=O) → (merged, source_map)`. Each layer's overlay is loaded as a YAML overlay dict; `_deep_merge_with_sources` applies the rule from D2 (per-field policy: union for lists, replace for scalars; keyed-list merge for `instrument_requirements`-shaped lists). The `source_map` records per-dotted-key the canonical layer label.

#### 5.1.1 Global config path collision (resolved here)

C00 V5 [1:139] names `<local-path>` for the global config; today's loader uses `<local-path>` [5:67-69]. C08 picks **`<local-path>`** (current code) and treats `<local-path>` as a fallback the loader consults if `<local-path>` is absent. Rationale: XDG-respecting path is the right Linux convention; `<local-path>` retained for daemon runtime artifacts (`<local-path>`, `<local-path>`, `<local-path>`, `<local-path>`). Migration is one-shot: the C08 migrator moves `<local-path>` → `<local-path>` on first run if both exist; writes a marker at `<local-path>`.

#### 5.1.2 Branch layer

Branch name resolved via `git symbolic-ref --short HEAD` (per D1). On failure (detached HEAD, not a git tree, git binary absent), branch layer is silently skipped — same semantic as a missing YAML file.

File path: `<repo>/.ea/branches/<branch>.yaml` with `/` in the branch name preserved as a subdirectory separator on disk (Q9 locked: subdirectory form). Example: branch `feature/eawf-v0.3-p20` lives at `<repo>/.ea/branches/feature/eawf-v0.3-p20.yaml`. Loader walks `<repo>/.ea/branches/**/*.yaml` and reconstructs the branch identity by stripping the prefix + `.yaml` suffix. Mirrors git's own ref namespace at `.git/refs/heads/<branch>` so directory structure on disk matches `git symbolic-ref` output 1:1.

The branch layer is **committed** (lives under `.ea/`, which is committed per AGENTS rule 3 [13]). Use case: a long-lived experimental branch needs a temporary `runtime.preference: [opencode, claude]` override; the override travels with the branch and disappears on rebase to main (because the file's not on main).

#### 5.1.3 Wave layer (transient, daemon-resident)

Daemon keeps a `wave_config_overrides: dict[wave_id, dict[str, Any]]` map. Mutated via `wave.set_config(wave_id, key, value)` RPC; reset on wave-close. Not file-backed.

Use case: V5 reactive runtime fallback [1:127-151]. When daemon flips a wave from `claude` → `codex`, it writes `wave_config_overrides[wave_id]["runtime.current"] = "codex"`. Subsequent dispatch envelopes for that wave see the override at the wave layer. Visible to operators via `eawf config get runtime.current --wave <wave-id>` (which the daemon answers from the wave map, not from disk).

Persistence on daemon restart: the wave layer is **lost on daemon shutdown** by design — wave config is execution-scoped, not durable. The relevant historical record lives in `Wave.dispatch_history` [2:362-365] + `runtime_switched` event log [3:1271]. Recovery: on daemon startup, the dispatcher rebuilds the in-progress wave overrides from `Wave.dispatch_history` (last `runtime_to` per attempt).

### 5.2 Field registry

The full enumeration. Each row: dotted key, type, default, writable layers, description. Tab grouping mirrors `ConfigKey.tab` [8:57-99]. New entries (C08-introduced) marked **(new)**.

#### 5.2.1 Top-level + schema

| Key | Type | Default | Writable | Description |
|---|---|---|---|---|
| `schema_version` | `Literal["1.2"]` | `"1.2"` | (code only) | Bumped from `"1.1"` [11:26] by C08. Loader fails fast on unknown version. |
| `config.layers_visible` | `bool` | `true` | global, workspace, repo | When `false`, `eawf config get` hides the layer source column. |

#### 5.2.2 `cli`

| Key | Type | Default | Writable | Description |
|---|---|---|---|---|
| `cli.canonical_command` | `str` | `"eawf"` | global, workspace, repo | Verb name in renderings. |
| `cli.preferred_command` | `str` | `"eawf"` | global, workspace, repo | Operator's preferred alias. |
| `cli.install_aliases` | `list[str]` | `["ea"]` | global, workspace, repo | Aliases installed into shell PATH on `eawf install`. |
| `cli.omit_ea_alias` | `bool` | `false` | global, workspace, repo | When `true`, `ea` alias is suppressed. |

#### 5.2.3 `project`

| Key | Type | Default | Writable | Description |
|---|---|---|---|---|
| `project.code` | `str \| None` | `None` | repo | Project code (`^[A-Z][A-Z0-9_-]{1,15}$` per AGENTS [13]). |
| `project.title` | `str \| None` | `None` | repo | Project title. |
| `project.slug` | `str \| None` | `None` | repo | URL-friendly slug. |
| `project.domains` | `list[str]` | `[]` | repo | Domain tags (e.g. `["devtools", "agents"]`). |
| `project.default_subproject` | `str \| None` | `None` | repo | Default scope for new dispatches. |
| **`project.goals`** | `list[str]` | `[]` | repo, branch, local | **(new — D12)** Free-form project goals. |
| **`project.success_metrics`** | `dict[str, float]` | `{}` | repo, branch, local | **(new — D12)** Per-metric target value. |

#### 5.2.4 `workspace`

| Key | Type | Default | Writable | Description |
|---|---|---|---|---|
| `workspace.enabled` | `bool` | `false` | workspace, global | Toggle multi-repo workspace mode. |
| `workspace.code` | `str \| None` | `None` | workspace | Workspace code. |
| `workspace.state_path` | `str` | `".ea/state.json"` | workspace | Per-repo state file path. |
| `workspace.repos` | `dict[str, WorkspaceRepoRef]` | `{}` | workspace | Indexed repos. |

#### 5.2.5 `profiles`

| Key | Type | Default | Writable | Description |
|---|---|---|---|---|
| `profiles.enabled` | `list[str]` | `["core"]` | global, workspace, repo, branch | Ordered profile bundle. |
| `profiles.catalog` | `list[str]` | (11 names) | (built-in only) | Available profile names (discovery). |
| `profiles.conflict_resolution` | `Literal["prompt","fail","first-wins"]` | `"prompt"` | global, workspace, repo | What to do when undeclared conflict found. **C08 changes default from `"prompt"` [11:65] to `"fail"`** per V3 [1:78] fail-fast rule. |
| `profiles.safety_policy` | `Literal["strictest_wins"]` | `"strictest_wins"` | (locked) | Locked at strictest-wins (today's only mode). |
| **`profiles.trusted`** | `dict[str, str]` | `{}` | repo, branch | **(new)** Content-hash trust ledger per profiles-fulfilment Tier C [4:151]. Key = profile id; value = sha256 of last-trusted body. |

#### 5.2.6 `runtime`

| Key | Type | Default | Writable | Description |
|---|---|---|---|---|
| `runtime.preference` | `list[str]` | `["claude"]` | global, workspace, repo, branch, local, env, cli, wave | **(was `runtime.adapters` [11:73])** Fallback ladder per V5 [1:139]. First entry = primary. |
| `runtime.default` | `str` | `"claude"` | (deprecated; alias of `runtime.preference[0]`) | Kept for backwards compat with v1.1 [11:69]. |
| **`runtime.fallback.on_errors`** | `list[str]` | `["RUNTIME_RATE_LIMIT","RUNTIME_SERVER_ERROR","RUNTIME_TIMEOUT","RUNTIME_API_ERROR"]` | global, workspace, repo | **(new — V5)** Error classes triggering switchover. |
| **`runtime.fallback.retry_policy`** | `Literal["hybrid","backoff","immediate"]` | `"hybrid"` | global, workspace, repo | **(new — V5; C02 D12 [3:145])** Hybrid retries 429 with backoff; falls through immediately on other errors. |
| **`runtime.fallback.max_backoff_seconds`** | `int` | `90` | global, workspace, repo | **(new — V5)** Max wall-clock backoff before fall-through on `RUNTIME_RATE_LIMIT`. |
| `runtime.slash_commands` | `list[str]` | (9 names) | global, workspace, repo | Slash-commands materialised per runtime [11:74-84]. |
| `runtime.adapter_catalog.<id>.enabled` | `bool` | per-id | repo | Per-adapter enable flag [11:88-97]. |
| `runtime.adapter_catalog.<id>.plugin_install` | `Literal["auto","ask","skip"]` | `"ask"` | global, workspace, repo | Plugin-install policy per adapter [11:91]. |
| `runtime.adapter_catalog.<id>.skills_path` | `str` | per-id | global, workspace, repo | Per-adapter skills install path [11:92]. |
| `runtime.adapter_catalog.<id>.agents_path` | `str` | per-id | global, workspace, repo | Per-adapter agents install path [11:93]. |

#### 5.2.7 `telemetry` (new — V7)

| Key | Type | Default | Writable | Description |
|---|---|---|---|---|
| `telemetry.enabled` | `bool` | `false` | global, workspace, repo | **(new — V7)** Master toggle [1:218]. |
| `telemetry.export.endpoint` | `str \| None` | `None` | global | **(new — V7)** External export endpoint; `None` = local-only [1:219]. |
| `telemetry.export.format` | `Literal["prom","otlp","json","csv"]` | `"prom"` | global | **(new — V7)** Export wire format. |
| `telemetry.window_default` | `str` | `"7d"` | global, workspace, repo | **(new — V7)** Default rolling window for `eawf metrics show`. |
| `telemetry.aggregate_window` | `str` | `"24h"` | global, workspace, repo | **(new — V7)** Per-aggregation row width. |
| `telemetry.db_kind` | `Literal["duckdb","sqlite"]` | `"duckdb"` | global | **(new — V7)** Backend pick per V7 [1:188]. C09 owns final decision. |

#### 5.2.8 `dispatch` (new — V8)

| Key | Type | Default | Writable | Description |
|---|---|---|---|---|
| **`dispatch.session_policy_default`** | `Literal["fresh","continue","hybrid"]` | `"hybrid"` | global, workspace, repo | **(new — V8)** Fallback when neither profile nor skill manifest specifies. |
| **`dispatch.session_handle_ttl_seconds`** | `int` | `86400` | global | **(new — C02 D13 [3:146])** TTL after wave close. |

#### 5.2.9 `language` (new — language-fit brief [10])

| Key | Type | Default | Writable | Description |
|---|---|---|---|---|
| **`language.runtime`** | `Literal["python"]` | `"python"` | (locked) | **(new — D6)** Locked at `python` for v0.3-v0.5. |
| **`language.fast_extras`** | `list[str]` | `[]` | global | **(new — D6)** Opt into PyO3 extras (`["fast"]`) per language-fit benchmark gate [10:104-105]. |

#### 5.2.10 `ui` (existing keys)

| Key | Type | Default | Writable | Description |
|---|---|---|---|---|
| `ui.bare_command` | `Literal["tui","help"]` | `"tui"` | global, workspace, repo | What `eawf` (no args) does. |
| `ui.color` | `Literal["auto","always","never"]` | `"auto"` | global, workspace, repo, env | Color rendering. |
| `ui.glyphs` | `Literal["auto","ascii","unicode"]` | `"auto"` | global, workspace, repo, env | Glyph rendering. |
| `ui.refresh_ms` | `int` | `1000` | global, workspace, repo | TUI tick interval (per C06; daemon push reduces reliance on poll). |
| `ui.dashboard_panes` | `list[str]` | (7 names) | global, workspace, repo | Pane catalog [11:104-112]. |

#### 5.2.11 Remaining sections (existing; field registry preserves verbatim)

`storage` (8 keys), `research` (5 keys), `planning` (4 keys), `estimation` (1 + 9 nested under `display`), `audit` (3 keys), `ship` (3 keys), `review` (3 keys), `polish` (4 keys), `flow` (6 nested under `auto_accept` + `ask_on_decisions`), `memory` (5 keys), `vcs` (~14 keys + `coauthor` nested), `worktrees` (8 keys), `acceptance` (4 nested under `commands` + 1 list), `security` (7 keys), `hooks` (5 keys), `mcp` (4 keys + `servers` map), `statusline` (2 keys), `docs` (3 keys), `commands` (1 key), `state_schema` (2 keys) — all preserved from current defaults [11:114-310]. The full per-section table mirrors `BUILT_IN_DEFAULTS` [11:29-311]; C08 does NOT re-print every leaf for brevity. The `ConfigKey` registry [8:106-302] covers the 24 operator-tunable keys (subset of the full ~120-key surface); C08 extends the registry to cover the new keys above so the interactive menu surfaces them.

**Total registry size after C08:** ~140 leaf keys (current ~120 + ~20 new from V5 / V7 / V8 / language / project.goals).

#### 5.2.12 `ConfigKey` registry catalog (Q2 verdict — all new keys surface in menu)

The interactive menu's `CONFIG_REGISTRY` [8:106-302] today carries 24 entries across 9 tabs (audit, estimation, planning, research, runtime, ship, ui, vcs, worktrees). Q2 verdict: every new C08 key gets a `ConfigKey` row so operators can tune via the menu without hand-editing YAML. New entries:

| Tab | New `ConfigKey` rows | Type | Default |
|---|---|---|---|
| `runtime` | `runtime.preference` (replaces today's `runtime.default`); `runtime.fallback.on_errors`; `runtime.fallback.retry_policy`; `runtime.fallback.max_backoff_seconds` | multichoice; multichoice; choice; int | per §5.2.6 |
| `telemetry` (new tab) | `telemetry.enabled`; `telemetry.export.endpoint`; `telemetry.export.format`; `telemetry.window_default`; `telemetry.aggregate_window`; `telemetry.db_kind` | bool; str; choice; str; str; choice | per §5.2.7 |
| `dispatch` (new tab) | `dispatch.session_policy_default`; `dispatch.session_handle_ttl_seconds` | choice; int | per §5.2.8 |
| `language` (new tab) | `language.fast_extras` (note: `language.runtime` locked, not surfaced) | multichoice | `[]` |
| `project` | `project.goals`; `project.success_metrics` (str-pair editor in menu) | str (multi-line); str (k:v pairs) | per §5.2.3 |
| `profiles` | `profiles.conflict_resolution`; `profiles.trusted` (read-only display of trust hashes) | choice; str (read-only) | per §5.2.5 |

Total new rows: 14 across 5 tabs (3 new tabs + 2 existing). Grand total after C08: ~38 menu rows. The full Pydantic `ConfigKey` entries land in C05 ship as part of the CLI surface migration.

### 5.3 Profile manifest schema (`ProfileBody` v2)

```python
# src/eawf/profiles/models.py — proposed v2 schema
# New / changed fields marked (new) / (changed). Existing fields preserved verbatim.

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StateExtensions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields_required: list[str] = []


class InstrumentReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: Literal["hard", "soft"] = "hard"
    probe: Literal["which", "version"] = "which"
    version_args: list[str] = []
    version_regex: str | None = None


class RenderBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    target: str
    body_template: str
    version: str = "1.0"


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # --- existing v1 fields (unchanged) ---
    name: str
    version: str = "1.0"
    description: str = ""
    extends: str | None = None    # informational metadata only in v0.3-v0.5 per [6:89];
                                  # Q4 verdict: does NOT implicitly add parent to `overrides`.
                                  # v0.5+ may wire ancestor auto-resolution; not now.
    state_extensions: StateExtensions = StateExtensions()
    instrument_requirements: list[InstrumentReq] = []
    render_blocks: list[RenderBlock] = []
    skills_referenced: list[str] = []
    hooks_referenced: list[str] = []

    # --- new in v2 (this brief) ---
    schema_version: Literal["1.0"] = "1.0"        # (new) profile schema marker; loader rejects unknown (BOT-03 / Q5 lock 2026-05-18: string MAJOR.MINOR project-wide)
    conflicts_with: list[str] = []                # (new — V3 [1:78]) profile ids that cannot coexist
    overrides: list[str] = []                     # (new — V3 [1:78]) profile ids whose contributions A claims
    dispatch_session_policy: Literal["fresh", "continue", "hybrid"] | None = None  # (new — V8 [1:266-269])

    # --- reserved for code-quality-profile-proposal P21+ extensions [12:73-110] ---
    # quality_thresholds: QualityThresholds | None = None    # P21-W01
    # enforcement_hooks: list[EnforcementHook] = []          # P21-W01
    # — declared in schema-v2 as reserved field NAMES (loader does not reject
    #   profile bodies that ship them); typed contribution rule lands in P21.
    #
    # --- Consumer-wiring annotations (Q6 verdict) ---
    # skills_referenced → C04 (skills cluster) consumes for skill-registry
    #                     contribution; today's field is dead [4:110] until C04 ships.
    # hooks_referenced  → dedicated future phase (post-C08) wires to three install
    #                     destinations: .pre-commit-config.yaml (project),
    #                     <local-path> (Claude-Code runtime), per-runtime
    #                     adapter (Codex / OpenCode). Today's field is dead [4:110].


class ComposedProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str                                      # "+"-joined input ids; "composed:empty" when empty
    version: str = "1.0"
    description: str = ""
    schema_version: Literal["1.0"] = "1.0"
    state_extensions: StateExtensions = StateExtensions()
    instrument_requirements: list[InstrumentReq] = []
    render_blocks: list[RenderBlock] = []
    skills_referenced: list[str] = []
    hooks_referenced: list[str] = []
    dispatch_session_policy: Literal["fresh", "continue", "hybrid"] | None = None
    provenance: dict[str, list[str]] = {}          # field_name -> contributors (caller order)
    override_audit: dict[str, list[str]] = {}      # (new) field_path -> override_chain [a, b, c]
    conflict_warnings: list[str] = []              # (new) non-fatal — e.g., undeclared render_block id overlap
```

**Schema-version compatibility.** A profile body with `schema_version: "1"` (or omitted, defaulting to absent) is auto-upgraded by the loader to schema `"2"` with empty `conflicts_with` + `overrides` + `dispatch_session_policy=None`. Schema `"3"` or higher raises `UnsupportedProfileSchema`.

**Lifecycle cross-ref (Q3 auto-fix).** Profile state machine LOADED → SHADOWED / CONFLICTED / UNLOADED lives in C01 §5.4.14 [2:1100-1123]. C08 composition-loader transitions:

- LOADED — profile in project's `profiles.enabled` list, validated, no undeclared conflict.
- SHADOWED — profile in `profiles.enabled` AND another profile in the list declares `overrides: [this_one]` covering all of this profile's contributions. SHADOWED profiles contribute nothing to the composed view but remain listed (audit trail).
- CONFLICTED — undeclared conflict detected; composition rejects per D3 unless `conflict_resolution: "first-wins"` set.
- UNLOADED — operator removed from `profiles.enabled` via `eawf config profile disable <id>`.

**Extends vs overrides (Q4 verdict).** `extends` and `overrides` are fully orthogonal. `a.extends: b` records that `a` was scaffolded from `b` (used by `eawf profile new --inherit b`) but does NOT auto-add `b` to `a.overrides`. Operator declares `overrides:` explicitly if the inheritance carries conflict-discharge intent. v0.5+ may wire `extends` to auto-resolve ancestor; v0.3-v0.5 keeps the field as documentation metadata.

### 5.4 Composition loader algorithm

```python
# src/eawf/profiles/compose.py — proposed extension to compose()

def compose(
    profiles: Iterable[ProfileBody],
    *,
    conflict_resolution: Literal["fail", "prompt", "first-wins"] = "fail",
) -> ComposedProfile:
    """
    Deep-merge profiles in declared order. Algorithm:

      1. Materialise list; verify every body validates as ProfileBody v2.
      2. Build the conflict graph: for each (a, b) with a≠b in the list,
         if b.name in a.conflicts_with OR a.name in b.conflicts_with,
         record an edge (a, b) in `conflicts`.
      3. Build the override graph: for each (a, b) with a≠b, if b.name in
         a.overrides, record an edge (a, b) in `overrides_map`.
      4. For each conflict edge (a, b):
         - if (a, b) or (b, a) appears in overrides_map: discharged (the
           overrider's contributions WILL win for fields the other touched,
           recorded in override_audit).
         - else: undeclared conflict — behaviour per conflict_resolution:
           * "fail" (default): raise ProfileConflict; return blocked envelope.
           * "prompt": surface to operator (AskUserQuestion: drop a / drop b
             / accept latest-wins).
           * "first-wins": warn-log only; first-declared profile contributes,
             later one's overlapping fields ignored (audit recorded).
      5. Apply per-field merge rules (D2):
         - state_extensions.fields_required: sorted union [7:142-152]
         - instrument_requirements: keyed by name, strictest-wins on kind
           [7:78-92]
         - render_blocks: keyed by id, later overrides earlier on body;
           first-seen position locks slot [7:105-115]
         - skills_referenced / hooks_referenced: sorted union [7:118-139]
         - conflicts_with / overrides: sorted union (informational on output)
         - dispatch_session_policy: last-non-None-wins
      6. Apply override_audit: for each (a, b) in overrides_map, walk the
         merged fields and tag any leaf b contributed under field_path
         with override_audit[field_path] = override_chain.
      7. Detect non-fatal conflict_warnings: same render_block id declared
         by two non-overriding profiles → log + add to conflict_warnings.
      8. Build the ComposedProfile envelope, including provenance + override_audit.

    Determinism (per [7:178-188]):
      - For non-render-block fields: order-insensitive across input orderings.
      - For render_blocks: caller-order locks slots; downstream overrides body
        but not position.
    """
    profile_list = list(profiles)

    # Validate schema versions; auto-upgrade v1 → v2
    profile_list = [_upgrade_v1_to_v2(p) for p in profile_list]

    # 2. Conflict graph
    conflicts: list[tuple[str, str]] = []
    for i, a in enumerate(profile_list):
        for b in profile_list[i + 1:]:
            if b.name in a.conflicts_with or a.name in b.conflicts_with:
                conflicts.append((a.name, b.name))

    # 3. Override graph
    overrides_map: list[tuple[str, str]] = []
    for a in profile_list:
        for target in a.overrides:
            if any(p.name == target for p in profile_list):
                overrides_map.append((a.name, target))

    # 4. Discharge conflicts
    undeclared = [
        (a, b) for (a, b) in conflicts
        if not _overrides_covers(a, b, overrides_map)
    ]
    if undeclared:
        if conflict_resolution == "fail":
            raise ProfileConflict(
                f"undeclared conflicts: {undeclared}; "
                f"either declare `overrides: [...]` on one of the profiles, "
                f"or drop one from the project's profiles: list."
            )
        elif conflict_resolution == "prompt":
            ...  # operator AUQ (C04 owns the surface)
        elif conflict_resolution == "first-wins":
            ...  # filter the later contributor out of merge passes

    # 5-7: merge + audit (existing _merge_* helpers + new _record_override_audit)
    ...

    return ComposedProfile(...)
```

**Override audit example.** Project `profiles: [reverse-engineering, research]` where `reverse-engineering` declares `overrides: [research]` and both contribute a `render_block` with id `hypothesis-format`. Loader merges; the `render_block` body comes from `reverse-engineering` (later contributor wins per [7:105-115]); `override_audit["render_blocks[id=hypothesis-format]"] = ["reverse-engineering", "research"]` records the chain.

### 5.5 Conflict + override resolution rules

**Conflict rules (D3):**

1. `a.conflicts_with: [b]` declared on either side → both cannot coexist in `profiles:` unless one declares `overrides:` covering the other.
2. Composition mode `fail` (D11 default `"fail"`) raises `ProfileConflict` envelope, exit code 4 (`VALIDATION_FAILED`).
3. Composition mode `prompt` → `AskUserQuestion`: drop a / drop b / proceed-with-last-wins. Operator answer persists as new `profiles.enabled` value in the layer where `profiles.enabled` was sourced.
4. Composition mode `first-wins` (advisory) → log + emit `conflict_warning` envelope; first declared profile contributes; later-declared profile's overlapping fields are dropped from merge.

**Override rules (D4):**

1. `a.overrides: [b]` declared → for every field where both a and b contribute, a's value wins.
2. Override is *whole-profile*: declaring `overrides: [b]` claims every overlap, not just one field. v0.5+ may introduce per-field override (`overrides: {b: ["render_blocks", "instrument_requirements"]}`).
3. Override is **declared on the overrider**, not the overridden. Profile b need not know it's being overridden.
4. Composed envelope records `override_audit: dict[field_path, override_chain]` so future operators can reconstruct.
5. Override does not relax the `conflicts_with` check unilaterally — both `conflicts_with` AND `overrides` need to agree. Loader rule: `a.overrides: [b]` discharges `(a, b)` from the conflict edge set; `a.conflicts_with: [b]` AND `a.overrides: [b]` simultaneously is legal (a says: "we conflict, and I win").

**Trust gate for non-bundled profiles (G10) — Q13 verdict: layered (both user + repo scopes):**

1. On first composition that pulls a non-built-in profile (lives at `<repo>/.ea/profiles/` or `<local-path>`), compute sha256 of the YAML body.
2. Check `profiles.trusted: {<id>: <sha256>}` resolved through the layered config (highest non-empty layer wins). User-scope trust at `<local-path>` extends to every repo where the repo layer has no contradicting entry; repo-scope trust at `<repo>/.ea/config.yaml` overrides for that repo only.
3. If missing: emit `AskUserQuestion` (trust-user / trust-repo / refuse / show-diff-against-builtin-`<id>`-if-exists). Operator answer determines layer:
   - `trust-user` writes to `<local-path>` → trusts everywhere.
   - `trust-repo` writes to `<repo>/.ea/config.yaml` → trusts only this repo.
4. If present but sha mismatch: re-prompt with diff vs trusted hash; operator decides which layer to update.
5. Bundled profiles (under `eawf.profiles.data`) are auto-trusted (shipped with the wheel; signed by release).

### 5.6 Goal system

Per D12, the project-level goal surface is intentionally lightweight:

```yaml
# .ea/config.yaml
project:
  code: EAWF
  goals:
    - "ship v0.3 alpha with daemon + TUI + telemetry"
    - "stabilise cluster spec series C01-C11"
  success_metrics:
    weekly_eu_burn_p50: 30.0
    audit_pass_rate: 0.9
    p99_command_latency_seconds: 0.5
```

Distinction:

- `project.goals: list[str]` — free-form *narrative* statements.
- `project.success_metrics: dict[str, float]` — *measurable* targets keyed by metric id; values are floats so the telemetry projection (V7 → C09) can compare actual to target.
- State-resident `Goal` rows [2:777] are the *operationalised* version — each goal materialises as a Goal entity when a wave begins implementing it. The config-layer entries are the *direction*; the state rows are the *execution*.

The full Goal entity (with id, owner, audit_id, due_date, achieved_at) stays in `state.json` per C01 [2:777]. C08 only specifies the config-layer free-form list. Per-subproject goals are deferred to v0.5+ when `Subproject` gains the per-scope EU target (C01 Q6 [2:1483-1496]).

### 5.7 Bootstrap templates

Five templates at `templates/init/<name>.yaml`. Each is a YAML doc that `eawf init --template <name>` reads, then merges with operator answers to produce `.ea/config.yaml`.

#### 5.7.1 `research`

```yaml
# templates/init/research.yaml
profiles:
  enabled: [core, research]
runtime:
  preference: [claude, codex]
dispatch:
  session_policy_default: continue   # research profile default per D10
planning:
  approval: ask
  max_parallel_waves: 2
audit:
  default_checks: [state, evidence_chain]
ship:
  require_audit_pass: true
  require_memory_review: true
project:
  goals: []
  success_metrics: {}
```

#### 5.7.2 `engineering`

```yaml
# templates/init/engineering.yaml
profiles:
  enabled: [core, python]
runtime:
  preference: [claude, codex]
dispatch:
  session_policy_default: fresh      # engineering profile default per D10
planning:
  approval: ask
  max_parallel_waves: 4
audit:
  default_checks: [state, tests, lint, typecheck, docs]
ship:
  require_audit_pass: true
  require_memory_review: false
acceptance:
  commands:
    tests: "uv run pytest"
    lint: "uv run ruff check ."
    typecheck: "uv run mypy ."
project:
  goals: []
  success_metrics: {}
```

#### 5.7.3 `reverse-engineering`

```yaml
# templates/init/reverse-engineering.yaml
profiles:
  enabled: [core, research, re]
runtime:
  preference: [claude, codex]
dispatch:
  session_policy_default: continue
planning:
  approval: ask
  max_parallel_waves: 1              # serial: each decomp builds on prior
audit:
  default_checks: [state, hypothesis_evidence]
ship:
  require_audit_pass: true
  require_memory_review: true
project:
  goals: []
  success_metrics: {}
```

`re` profile is name-only today [4:42-49]; the bootstrap template surfaces it but the body remains a stub — operator extends per profiles-fulfilment Tier B [4:142-145] or writes a custom YAML at `<repo>/.ea/profiles/re.yaml`.

#### 5.7.4 `spike`

```yaml
# templates/init/spike.yaml
profiles:
  enabled: [core]
runtime:
  preference: [claude]
dispatch:
  session_policy_default: fresh
planning:
  approval: skip                     # rapid iteration; no AUQ
  max_parallel_waves: 1
audit:
  default_checks: [state]            # minimum
ship:
  require_audit_pass: false          # spike: throwaway-OK
  require_memory_review: false
project:
  goals: []
  success_metrics: {}
```

The spike profile is the lightest gate — matches AGENTS §"Spike workflow" [13]: time-boxed, read-only investigation, brief-only output; no state mutation.

#### 5.7.5 `hybrid`

```yaml
# templates/init/hybrid.yaml
profiles:
  enabled: [core, python, research]
runtime:
  preference: [claude, codex]
dispatch:
  session_policy_default: hybrid     # hybrid profile default per D10
planning:
  approval: ask
  max_parallel_waves: 4
audit:
  default_checks: [state, tests, lint, typecheck, docs, evidence_chain]
ship:
  require_audit_pass: true
  require_memory_review: true
acceptance:
  commands:
    tests: "uv run pytest"
    lint: "uv run ruff check ."
    typecheck: "uv run mypy ."
project:
  goals: []
  success_metrics: {}
```

This is the eawf-itself bundle — research substrate + engineering ship-gate. C00 V3 [1:91] explicitly names `[research, engineering]` as "most common; Eawf itself uses this".

#### 5.7.6 Template resolution rule

`eawf init` arguments:

```
eawf init [--template <name>]
          [--profiles a,b,c]
          [--code <PROJECT-CODE>]
          [--title <title>]
          [--workspace <path>]
          [--no-interactive]
```

- `--template X --profiles Y,Z` → error: pick one (D7).
- `--template X` → read `templates/init/X.yaml`, apply operator answers for `code` / `title`, write `.ea/config.yaml`.
- `--profiles Y,Z` → bypass template; build `.ea/config.yaml` from scratch + chosen profiles' defaults via composition loader.
- No args + interactive: questionary wizard asks template choice + goals + project metadata.

After config is written: daemon (if up) calls `daemon.reload_config`; new profiles' `state_extensions.fields_required` materialise on `state.json` via `enable_profile()` flow [9:152-222].

### 5.8 Profile manifest examples (YAML on-disk)

#### 5.8.1 `engineering.yaml` (new profile, ships in v0.3)

```yaml
# src/eawf/profiles/data/engineering.yaml
name: engineering
schema_version: "2"
version: "1.0"
description: "Engineering profile: feature-PR-driven, full lint/test/coverage gate, fresh dispatch."

# V8 per-profile default
dispatch_session_policy: fresh

# Composability declarations
conflicts_with: []
overrides: []

state_extensions:
  fields_required:
    - audits

instrument_requirements:
  - name: git
    kind: hard
    probe: which
  - name: python
    kind: hard
    probe: version
    version_args: ["--version"]
    version_regex: "^Python\\s+\\d"

render_blocks:
  - id: engineering-ship-gate
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Engineering ship gate (engineering profile)

      Every phase ships through a PR. The ship gate requires: state
      validate clean, tests passing, lint passing, typecheck passing,
      docs rebuilt. The audit-DSL kind `verify-implements` confirms
      every WaveSpec's `implements:` citations resolve.

skills_referenced:
  - flow
  - audit
  - ship
  - review
  - polish
hooks_referenced:
  - pre-commit
  - prepare-commit-msg
```

#### 5.8.2 `research-v2.yaml` (upgrades current `research.yaml` [4:11] to v2)

```yaml
name: research
schema_version: "2"
version: "1.1"
description: "Research profile: hypotheses, audits, decisions, evidence chain, continue-session dispatch."

dispatch_session_policy: continue   # V8 [1:266-269]

conflicts_with: []
overrides: []

state_extensions:
  fields_required:
    - hypotheses
    - audits

instrument_requirements: []

render_blocks:
  - id: research-workflow
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Research workflow (research profile)
      [body verbatim from existing research.yaml]

skills_referenced:
  - research
  - audit
  - hypothesis
hooks_referenced: []
```

#### 5.8.3 `spike.yaml` (new profile, ships in v0.3)

```yaml
name: spike
schema_version: "2"
version: "1.0"
description: "Short-lived experimental profile: minimum gates, fresh dispatch, throwaway-OK."

dispatch_session_policy: fresh

# Q10 verdict 2026-05-16: drop the engineering conflict. Operator may legitimately
# combine [engineering, spike] (spike a feature while engineering the rest of repo);
# spike's gate-relaxation overrides engineering's `require_audit_pass` via
# last-non-None-wins on the relevant config keys.
conflicts_with: []
overrides: []

state_extensions:
  fields_required: []

instrument_requirements:
  - name: git
    kind: hard
    probe: which

render_blocks:
  - id: spike-workflow
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Spike workflow (spike profile)

      Spikes are time-boxed investigations under .ea/local/<slug>.md.
      Output is a brief, not a state mutation. Skip the ship gate;
      this profile waives lint/test/typecheck pre-commit blocking.

skills_referenced:
  - research
hooks_referenced: []
```

#### 5.8.4 `reverse-engineering.yaml` (new profile, ships as stub-body)

```yaml
name: reverse-engineering
schema_version: "2"
version: "1.0"
description: "Symbol-naming, decompilation-driven analysis; continue-session dispatch."

dispatch_session_policy: continue

# Decompilation needs the python lint set but overrides research's audit-cadence
conflicts_with: [spike]
overrides: []

state_extensions:
  fields_required:
    - hypotheses
    - audits

instrument_requirements:
  - name: git
    kind: hard
    probe: which

render_blocks:
  - id: re-workflow
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Reverse-engineering workflow (re profile)

      Per-symbol naming hypotheses live in `state.hypotheses` with the
      `H<NN>-<NN>` symbol per AGENTS rule 5. Decompilation output is
      stored as artifacts with content-hash provenance. Per-symbol
      verdict requires an audit with at least one cross-reference check.

skills_referenced:
  - research
  - hypothesis
hooks_referenced: []
```

#### 5.8.5 `engineering-and-research.yaml` (composed example output)

When operator runs `eawf init --profiles core,python,research`, the resulting `ComposedProfile` envelope (returned by `compose()`):

```yaml
name: "core+python+research"
schema_version: "2"
version: "1.0"
description: "Research profile: hypotheses, audits, decisions, evidence chain, continue-session dispatch."  # last-non-empty-wins [7:166-172]
dispatch_session_policy: continue       # research contributes; python/core left None
state_extensions:
  fields_required: [audits, hypotheses]
instrument_requirements:
  - {name: git, kind: hard, probe: which}
  - {name: python, kind: hard, ...}
  - {name: uv, kind: hard, ...}
  - {name: ruff, kind: soft, ...}
  - {name: mypy, kind: soft, ...}
render_blocks:
  - {id: python-style, target: AGENTS.md, ...}        # from python
  - {id: test-discipline, target: AGENTS.md, ...}     # from python
  - {id: research-workflow, target: AGENTS.md, ...}   # from research
  # ...16 blocks from core
skills_referenced: [audit, hypothesis, research]
hooks_referenced: []
provenance:
  state_extensions: [research]
  instrument_requirements: [core, python]
  render_blocks: [core, python, research]
  skills_referenced: [research]
  hooks_referenced: []
override_audit: {}
conflict_warnings: []
```

### 5.8.6 Catalog stub fulfilment (Q14 verdict) — nine new bundled profile bodies

Per operator Q14 verdict 2026-05-16, the nine name-only stubs gain real bodies in C08 ship. Each contributes at least one `render_block` + at least one instrument or `state_extensions.fields_required`. Schema v2; `dispatch_session_policy=None` (defers to skill / global). Conflict/override declarations empty unless natural pairing exists.

#### apps

```yaml
name: apps
schema_version: "2"
version: "1.0"
description: "Application development profile: web / CLI / desktop apps; build + dist tooling."
dispatch_session_policy: null
conflicts_with: []
overrides: []
state_extensions:
  fields_required: []
instrument_requirements:
  - {name: node, kind: soft, probe: version, version_args: ["--version"], version_regex: "^v\\d"}
  - {name: docker, kind: soft, probe: which}
render_blocks:
  - id: apps-discipline
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Apps profile

      Application code under `apps/` or `src/app/`. Build artifacts under
      `dist/` (gitignored). Each app declares its runtime + entry point in
      its own README. CI builds every app on PR; release tags trigger
      per-app version bumps.
skills_referenced: [flow, ship]
hooks_referenced: []
```

#### infra

```yaml
name: infra
schema_version: "2"
version: "1.0"
description: "Infrastructure / DevOps profile: terraform, k8s, CI/CD pipelines."
dispatch_session_policy: null
conflicts_with: [spike]
overrides: []
state_extensions:
  fields_required: [audits]
instrument_requirements:
  - {name: terraform, kind: soft, probe: version, version_args: ["version"], version_regex: "Terraform v\\d"}
  - {name: kubectl, kind: soft, probe: which}
render_blocks:
  - id: infra-discipline
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Infra profile

      Terraform state lives outside the repo (remote backend per env). Every
      module ships with `README.md` listing inputs + outputs. Production
      mutations require operator approval + audit-DSL kind `terraform_plan`
      cited on the change wave.
skills_referenced: [audit, ship]
hooks_referenced: []
```

#### docs

```yaml
name: docs
schema_version: "2"
version: "1.0"
description: "Documentation projects: mkdocs / sphinx; build + link-check pipeline."
dispatch_session_policy: null
conflicts_with: []
overrides: []
state_extensions:
  fields_required: []
instrument_requirements:
  - {name: pandoc, kind: soft, probe: version, version_args: ["--version"], version_regex: "^pandoc \\d"}
render_blocks:
  - id: docs-discipline
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Docs profile

      Source markdown under `docs/`; generated HTML under `site/` (gitignored).
      Every edit runs the link-check audit-DSL kind. Cross-document links use
      repo-relative paths.
skills_referenced: [polish, audit]
hooks_referenced: []
```

#### ml

```yaml
name: ml
schema_version: "2"
version: "1.0"
description: "Machine-learning profile: notebooks, model training, evaluation harness."
dispatch_session_policy: null
conflicts_with: []
overrides: []
state_extensions:
  fields_required: [hypotheses, audits]
instrument_requirements:
  - {name: python, kind: hard, probe: version, version_args: ["--version"], version_regex: "^Python\\s+\\d"}
  - {name: jupyter, kind: soft, probe: which}
render_blocks:
  - id: ml-discipline
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## ML profile

      Notebooks under `notebooks/`; reproducibility-checked (seed pinned;
      env recorded). Models versioned by content-hash. Every reported
      metric backed by an audit-DSL kind `eval_run` referencing the dataset
      + model + script SHAs.
skills_referenced: [research, audit, hypothesis]
hooks_referenced: []
```

#### quant

```yaml
name: quant
schema_version: "2"
version: "1.0"
description: "Quantitative research profile: backtests, factor analysis, statistical evidence."
dispatch_session_policy: null
conflicts_with: []
overrides: []
state_extensions:
  fields_required: [hypotheses, audits]
instrument_requirements:
  - {name: python, kind: hard, probe: version, version_args: ["--version"], version_regex: "^Python\\s+\\d"}
render_blocks:
  - id: quant-discipline
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Quant profile

      Backtest results pin `seed`, `start_date`, `end_date`, `universe_sha`,
      `factor_sha`. Significance reported with confidence intervals; any
      claim p<0.05 requires a holdout-set audit-DSL kind `holdout_eval`.
      Survivorship + look-ahead bias documented per hypothesis.
skills_referenced: [research, audit, hypothesis]
hooks_referenced: []
```

#### game

```yaml
name: game
schema_version: "2"
version: "1.0"
description: "Game-development profile: engine integration, asset pipeline, build matrix."
dispatch_session_policy: null
conflicts_with: []
overrides: []
state_extensions:
  fields_required: []
instrument_requirements:
  - {name: git, kind: hard, probe: which}
render_blocks:
  - id: game-discipline
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Game profile

      Source under `src/`; assets under `assets/` with content-hashed
      lockfile per engine version. Build matrix covers each target platform.
      Performance-budget audits run on representative scenes.
skills_referenced: [flow, ship]
hooks_referenced: []
```

#### re

```yaml
name: re
schema_version: "2"
version: "1.1"
description: "Reverse-engineering profile: decompilation, symbol-naming hypotheses, evidence-driven analysis."
dispatch_session_policy: continue
conflicts_with: [spike]
overrides: []
state_extensions:
  fields_required: [hypotheses, audits]
instrument_requirements:
  - {name: git, kind: hard, probe: which}
render_blocks:
  - id: re-workflow
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Reverse-engineering workflow (re profile)

      Per-symbol naming hypotheses live in `state.hypotheses` with the
      `H<NN>-<NN>` symbol per AGENTS rule 5. Decompilation output is
      stored as artifacts with content-hash provenance. Per-symbol
      verdict requires an audit with at least one cross-reference check.
skills_referenced: [research, hypothesis]
hooks_referenced: []
```

(`re` body promoted from §5.8.4 here so it ships as a built-in YAML, not as a separate "new profile" outside the catalog. The §5.8.4 example body is the authoritative source; §5.8.6 entry preserved for completeness in the catalog list.)

#### robotics

```yaml
name: robotics
schema_version: "2"
version: "1.0"
description: "Robotics / embedded profile: firmware, ROS, hardware-in-the-loop tests."
dispatch_session_policy: null
conflicts_with: []
overrides: []
state_extensions:
  fields_required: [audits]
instrument_requirements:
  - {name: git, kind: hard, probe: which}
render_blocks:
  - id: robotics-discipline
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Robotics profile

      Firmware under `firmware/`, host-side under `host/`. HW-in-the-loop
      tests document the bench rig + firmware SHA. Safety-critical changes
      require dual-operator review.
skills_referenced: [audit, ship, review]
hooks_referenced: []
```

#### a11y

```yaml
name: a11y
schema_version: "2"
version: "1.0"
description: "Accessibility profile: WCAG audit + screen-reader + contrast checks."
dispatch_session_policy: null
conflicts_with: []
overrides: []
state_extensions:
  fields_required: [audits]
instrument_requirements:
  - {name: git, kind: hard, probe: which}
render_blocks:
  - id: a11y-discipline
    target: AGENTS.md
    version: "1.0"
    body_template: |
      ## Accessibility profile (a11y)

      Every operator-facing surface tested against WCAG 2.2 AA. Screen
      reader output captured in `tests/a11y/*.snapshot`. Color choices
      verified for at least Wong-2011 deuteranopia-safe contrast.
skills_referenced: [audit, review, polish]
hooks_referenced: []
```

**Stub-warning logic retirement.** Per Q14 verdict, the `profile_stub` warning emitted at init/sync (planned in profiles-fulfilment Tier A [4:135-137]) is dropped — every catalog profile now ships with real content, so the warning has nothing to fire on. Future custom profiles can still trigger the trust gate (G10) but the empty-contribution warning is no longer needed.

### 5.9 Daemon-side config surface (RPC contract)

**Mutator-path precision (AGENTS rule 17, Q1 verdict).** C08 names three distinct canonical writers, each owning one file class:

- **state-CLI** writes `state.json` via `state.mutate` daemon RPC → mutator thread → `atomic_write_json_locked` [9 → state/writer.py]. Owns: `state.json`, `event.jsonl`, `audit.jsonl`, per-role report jsonls.
- **layered-config writer** writes per-layer YAML files via `_save_value_to_layer` in [16] (extended in C08 to cover the new `branch` layer files at `<repo>/.ea/branches/**/*.yaml`). Owns: `<local-path>`, `<workspace>/.ea/config.yaml`, `<repo>/.ea/config.yaml`, `<repo>/.ea/branches/**/*.yaml`, `<repo>/.ea/local/config.yaml`. When daemon is up, mutations are proxied through `state.mutate` carrying a `WriteConfigLayer` Mutation variant; daemon's mutator thread then invokes the layered-config writer.
- **registry writer** writes `<local-path>` via `_persist_registry` per AGENTS rule 17 [13]. Owns: `<local-path>`. Out of scope for C08 itself (registry is C07-owned).
- **wave layer (daemon RAM)** mutated via `wave.set_config` RPC (transient — no file backing per Q12). Daemon validates the requested key against the wave-layer whitelist (Q9: `runtime.*` + `dispatch.*` only) and rejects others with `-32602`.

Each writer obeys AGENTS rule 4 single-canonical-mutator invariant for *its* file class; conflating them in audit text fails the AGENTS naming-conventions precision rule [13].

C02 owns the daemon RPC surface [3:299-345]; C08 names the config-relevant methods:

| Method | Params | Result | Notes |
|---|---|---|---|
| `config.get` | `{key: str, scope?: "wave"\|"layered"}` | `{value: Any, layer: str}` | Read; when `scope=wave` checks the daemon's wave-config map first. |
| `config.set` | `{key: str, value: Any, layer: str, scope_id?: str}` | `{event: Envelope, before_version: str, after_version: str}` | Mutation; the layer determines the on-disk file; `scope_id` required when `layer=wave`. Routed via `state.mutate` with a `WriteConfigLayer` Mutation variant. |
| `config.list` | `{tab?: str}` | `{keys: list[ConfigKey]}` | Registry enumeration; backs the interactive menu + TUI modal. |
| `config.validate` | `{}` | `{report: ValidationReport}` | Runs merged config through `_ConfigSchema` [16:89-131]. |
| `daemon.reload_config` | `{}` | `{config_version: str}` | Re-reads layered config without restart [3:335]. |
| `wave.set_config` | `{wave_id: str, key: str, value: Any}` | `{event: Envelope}` | Mutates the daemon's transient wave-config map. Reset on wave-close. **Key whitelist (Q9):** only `runtime.*` (per V5 use case [1:127-151]) or `dispatch.*` (per V8 [1:266-269]). Other key prefixes rejected with `-32602 invalid_params`. Daemon validator pattern: `key.split(".")[0] in {"runtime", "dispatch"}`. |

The `WriteConfigLayer` Mutation variant carries: `{layer: str, dotted_key: str, value: Any, source_workspace?: Path, source_repo?: Path}`. Daemon-side handler locates the YAML file via `layer_path()` [5:87-113], runs through `_atomic_write_yaml` [9:72-96], emits the event row.

### 5.10 Provenance trace + `eawf config get --explain`

The `source_map` returned by `merge_config` [5:264-363] already records the per-leaf source layer. C08 extends with a profile-level provenance: when a value's source is `built-in`, the explain output additionally names the *profile* whose `render_blocks` / `instrument_requirements` / etc. contributed.

Sample:

```
$ eawf config get dispatch.session_policy_default --explain
hybrid                                  [layer: built-in]
                                        [contributing profile: <default>]

$ eawf config get runtime.preference --explain
[claude, codex]                         [layer: repo]
                                        [overrides: built-in (was [claude])]

$ eawf config get render_blocks.python-style.target --explain
"AGENTS.md"                             [layer: built-in]
                                        [contributing profile: python]
                                        [profile version: 1.1]
```

(Q5 auto-fix: "profile" is not a layer — it's the *contributing profile* trace within the `built-in` layer. The explain output uses `[layer: <one of the 7 precedence labels>]` + `[contributing profile: <id>]` for render-block / instrument / `state_extensions` leaves whose source is the composed-profile view.)

### 5.11 Downstream consumer: `eawf sync` (Q8 verdict)

The composed-profile envelope feeds **`eawf sync`** as the primary downstream consumer. Today's `_resolve_enabled_profiles` [4:62 → cli/commands/sync.py:176] reads `profiles.enabled` from the merged config, calls `compose()` over the loaded `ProfileBody` v2 entries, and walks the composed `render_blocks` list to render content into `AGENTS.md` managed regions [4:39]. C08's contract extension is the new fields (`conflicts_with`, `overrides`, `dispatch_session_policy`) — sync consumes them transparently because the composed envelope shape stays Pydantic-validated and `extra="forbid"`-strict.

Per-runtime install destinations (`.claude/skills/`, `<local-path>`, OpenCode equivalent) are C07's territory [1:653-696]; C08 only locks the composed-profile *contract* sync reads. The full sync flow:

```
1. eawf sync reads merged config (merge_config + layered overlays).
2. Calls compose(loaded_profile_bodies) → ComposedProfile envelope.
3. For each composed.render_blocks: materialise into target file's managed region
   via eawf.render.regions.replace_region.
4. For each composed.instrument_requirements: invoke probe pipeline
   (instrument_probe.py); record results in state.audits if mismatch.
5. For each composed.state_extensions.fields_required: ensure top-level
   key present on state.json (via enable_profile [9]).
6. For each composed.skills_referenced (C04 wiring, post-C08): register
   skill plugin for the active runtime adapter.
7. For each composed.hooks_referenced (future phase wiring, post-C08):
   materialise hook into the right destination per kind.
```

The C08 contract guarantees the composed envelope is byte-stable for a given input profile-list (order-insensitive except `render_blocks` per [7:178-188]) so `eawf sync --dry-run` diff renders deterministically.

## 6. Failure modes + named edge cases

| # | Failure mode | Trigger | Detection | Repair |
|---|---|---|---|---|
| F1 | Undeclared profile conflict | Project lists `profiles: [a, b]` where `a.conflicts_with: [b]` but neither declares `overrides:` | Composition loader detects conflict edge with no covering override; raises `ProfileConflict` | Operator drops one profile from `profiles.enabled`, or edits one's body to declare `overrides:` covering the other |
| F2 | Override chain cycle | `a.overrides: [b]`, `b.overrides: [a]` | Topological-sort step in `_record_override_audit` detects cycle; raises `ProfileOverrideCycle` | Operator picks one direction; remove the reverse override |
| F3 | Schema-version drift | Profile body ships `schema_version: "3"` against loader expecting `"2"` | `ProfileBody.model_validate` rejects on `Literal["2"]` violation | Upgrade eawf to the schema-3-aware version, or downgrade the profile body |
| F4 | Branch layer corrupt | `<repo>/.ea/branches/**/<branch>.yaml` is invalid YAML | `load_yaml_layer` raises `yaml.YAMLError`; loader fails fast at `_normalise_runtime_adapters` [5:312] | Fix the YAML; pre-commit hook validates branch YAMLs against the same schema |
| F4b | Branch subdirectory collision | Branches `a/b` (file) and `a/b/c` (dir of `c.yaml`) cannot coexist on a POSIX filesystem — `a/b` as file blocks `a/b/c/...` | Git itself forbids such ref combinations (`git update-ref` rejects creating `a/b/c` when `a/b` exists). Loader inherits the constraint. | Rename one of the branches. |
| F5 | Branch resolution fails on detached HEAD | `git symbolic-ref --short HEAD` exits non-zero | D1 silent-skip: branch layer contributes nothing; `source_map` shows no `branch`-layer entries | Reattach to a branch, or operate without branch overrides |
| F6 | XDG migration collision | Both `<local-path>` and `<local-path>` exist with different content | C08 migrator detects both at first run; emits `AskUserQuestion` (which to keep / merge) | Operator picks the canonical one; migrator writes the marker `<local-path>` |
| F7 | Trust ledger hash mismatch | Operator edits a custom profile YAML after trusting it | `profiles.trusted` lookup at compose time; new sha256 differs | Re-prompt `AskUserQuestion`; operator inspects diff vs trusted hash, re-approves or refuses |
| F8 | Wave layer leak after wave close | Daemon crash before wave-close clears `wave_config_overrides[wave_id]` | Daemon startup recovery walks `state.waves[*]` for any wave with `status ∈ {CLOSED, FAILED, ABANDONED}` and clears the corresponding wave-config entry | Recovery is automatic; emits `wave_config_cleanup` envelope |
| F9 | Runtime preference under wave layer references unknown runtime | `wave.set_config(wave_id, "runtime.current", "fictionalruntime")` | Validator: `runtime.adapter_catalog` lookup fails on commit; mutation rejected with `-32602 invalid_params` | Use a runtime id present in the adapter catalog |
| F10 | `eawf init --template X --profiles Y` ambiguity | Both flags passed | D7 resolution rule rejects with `InvalidInput` | Operator picks one |
| F11 | Profile body uses design-doc syntax (`rules:`, `agents:`) | YAML carries fields not in `ProfileBody` v2 schema | `model_validate` raises on `extra="forbid"` per F6 of profiles-fulfilment [4:113] | Author rewrites against the v2 schema; design-doc rules-DSL is deferred (NG6) |
| F12 | Composed `dispatch_session_policy` ambiguity | Two profiles set `dispatch_session_policy` to different non-None values, no `overrides:` declared | last-non-None-wins (D2); audit records the chain | Operator declares `overrides:` to make the precedence explicit |
| F13 | Built-in defaults section drift | New section added to `BUILT_IN_DEFAULTS` [11:29-311] without bumping `_ConfigSchema` [16:89-131] | `config.validate` fails on the merged config | Update `_ConfigSchema` to include the new section; bump `schema_version` |
| F14 | Profile YAML at custom path (e.g. `<repo>/profiles/<id>.yaml` outside `.ea/`) | Loader walks `<repo>/.ea/profiles/` only; custom path is ignored | Profile not discovered; not in `list_profiles()` | Move the YAML to one of the supported paths |
| F15 | Schema migration partial failure | Migrator `v1.0 → v1.1` runs, writes new shape, then crashes before backup file rename | Marker file `<local-path>` exists but new file is partial | Operator restores from backup; retries migration; bug-report the migrator |
| F16 | `profiles.enabled` references undiscovered profile id | Operator enables `engineering` before `engineering.yaml` ships in `eawf.profiles.data` | `load_profile()` raises `NotFound`; composition fails fast | Use `eawf profile list` to see discoverable profiles; upgrade eawf for new bundled profiles |
| F17 | Concurrent layered-config writes from two daemons | Two daemons on different OS users both write `<local-path>` simultaneously | `portalock.acquire(layer_path)` [9:189] serialises writes; second writer waits | OS-level locking is the safety net; no daemon should run as another user's process |
| F18 | Wave layer state lost on daemon SIGKILL mid-wave | Daemon killed before wave-close; in-memory map gone | Recovery: dispatcher rebuilds the in-progress wave config from `Wave.dispatch_history` last entry (V5 fallback annotation per C01 [2:362-365]) | Automatic recovery on next daemon spawn |

## 7. Migration plan

C08 spans three discrete migration surfaces: layered-config schema bump, profile schema bump, XDG path move. Each has a one-shot migrator + backup + rollback path.

### 7.1 What changes

1. **`config.schema_version: "1.1" → "1.2"`** on every operator-tunable config file.
   - New top-level keys: `dispatch.*`, `telemetry.*`, `language.*`, `runtime.fallback.*`, `profiles.trusted`.
   - Renamed: `runtime.kind` (deprecated alias) and `runtime.adapters` (current shape) consolidate to `runtime.preference`. Loader retains the existing v1.1 shim [5:222-261] which synthesises `runtime.adapters` from `runtime.kind`; C08 extends with a new shim synthesising `runtime.preference` from `runtime.adapters`.
   - New layer: `branch` files at `<repo>/.ea/branches/**/<branch>.yaml` (subdirectory form per Q9).

2. **Profile schema v1 → v2** on every profile YAML.
   - New fields: `schema_version: "2"`, `conflicts_with`, `overrides`, `dispatch_session_policy`.
   - Loader auto-upgrades v1 → v2 with empty defaults [§5.3]; no on-disk rewrite required at first run.
   - `eawf profile migrate` (new verb) rewrites every profile YAML to v2 form once operator opts in.

3. **XDG move**: `<local-path>` → `<local-path>` if the legacy path exists (§5.1.1).

4. **New bundled profiles**: `engineering.yaml`, `spike.yaml`, plus rewritten/fulfilled bodies for the nine catalog profiles (`apps`, `infra`, `docs`, `ml`, `quant`, `game`, `re`, `robotics`, `a11y`) per Q14 (§5.8.6) all ship in `eawf.profiles.data`.

5. **`profiles.conflict_resolution` default change**: `"prompt"` → `"fail"` per V3 fail-fast rule [1:78]. Migrator preserves `"prompt"` on existing configs explicitly (Q11 verdict); new `eawf init` writes `"fail"`.

### 7.2 Migrator stages (idempotent, one-shot per stage)

**Invocation point (Q7 verdict).** Migrator runs at two well-known points:

1. **Daemon startup** — before the asyncio server binds the listening socket [3:362-376]. Daemon walks every known config-layer YAML, runs `migrate_config_v1_1_to_v1_2` on each (idempotent — no-op when already v1.2). On any migration, emits a `config_schema_migrated` envelope on the event bus + writes the backup file. Failure aborts daemon startup with `daemon_spawn_failed` envelope citing the failed migrator + backup-path.
2. **Daemonless CLI first load** — `merge_config` [5:264-363] checks `config.schema_version` on each loaded layer; on mismatch invokes the migrator inline before applying the deep-merge. Same backup + envelope semantics; envelope rendered to stderr since no event bus is up.

Migration is **idempotent**: re-running on already-v1.2 config is a no-op. Backup files at `<path>.bak.v1.1.<timestamp>` are kept indefinitely; operator removes manually.

```
src/eawf/config/migrate.py — new module

def migrate_config_v1_1_to_v1_2(yaml_path: Path) -> MigrationReport:
    """Idempotent migrator:
       1. Read existing YAML.
       2. If schema_version == "1.2": no-op.
       3. If schema_version == "1.1":
          a. Backup to <path>.bak.v1.1.<timestamp>.
          b. Set schema_version = "1.2".
          c. If runtime.adapters present and runtime.preference absent: set
             runtime.preference = runtime.adapters; deprecate adapters.
          d. Insert defaults for new sections (dispatch, telemetry, language,
             runtime.fallback) only if absent — never overwrite operator values.
          e. Atomic-write back via _atomic_write_yaml [9:72-96].
       4. Return MigrationReport with backup path + diff summary.
    """

def migrate_profile_v1_to_v2(yaml_path: Path) -> MigrationReport:
    """Adds schema_version: "2", conflicts_with: [], overrides: [],
       dispatch_session_policy: null. Body unchanged otherwise."""

def migrate_xdg(legacy_path: Path, xdg_path: Path) -> MigrationReport:
    """Moves <local-path> → <local-path> when only the
       legacy exists. If both exist with differing content: emit AUQ."""
```

### 7.3 Per-phase rollout

| Phase | Surface | Scope |
|---|---|---|
| **C08 (this brief — itself)** | Bundled profiles | Nine catalog stubs rewritten with real bodies (Q14): `apps`, `infra`, `docs`, `ml`, `quant`, `game`, `re`, `robotics`, `a11y` (§5.8.6). Plus new `engineering` + `spike` (§5.8.1, §5.8.3). Golden fixtures under `tests/golden/agents_md/<profile-combo>.md` per profiles-fulfilment Tier B [4:144]. |
| **C03 (next)** | Spec subsystem | Per-tier spec validator may be profile-gated; uses composed-profile contributions for the `validate_state` check matrix [13]. |
| **C04** | Skills + workflow | Skill manifest carries `dispatch.session_policy: <override>`; falls back to profile default per V8 [1:266-269]. **Q6 verdict:** C04 also wires `skills_referenced` to the skill registry (today's field is dead per [4:110]). |
| **future phase (post-C08)** | Hooks install pipeline | **Q6 verdict:** `hooks_referenced` wires to three install destinations: `<repo>/.pre-commit-config.yaml`, `<local-path>`, per-runtime adapter (Codex / OpenCode). Spans three install surfaces; dedicated phase post-C08. |
| **C05** | CLI surface | `eawf config set <key> <value> --scope <layer>` extends `--scope` enum with `branch`, `wave`; `eawf profile {list,enable,disable,validate,new,migrate}` verbs surfaced. |
| **C06** | TUI | Config modal reads the registry + provenance; `/config` overlay surfaces the layer / contributing-profile breakdown. |
| **C07** | Runtime adapter | Per-adapter `dispatch.session_policy` honored; `runtime.preference` consulted on fallback. |
| **C09** | Telemetry | `telemetry.*` keys consumed by the DuckDB rollup [1:188-224]. |

### 7.4 Rollback

- **Config v1.2 → v1.1**: restore from `<path>.bak.v1.1.<timestamp>`; downgrade eawf to a pre-C08 build (which doesn't understand v1.2 anyway).
- **Profile v2 → v1**: loader auto-downgrades by dropping the new fields (they all have safe defaults); no on-disk rewrite needed.
- **XDG move rollback**: copy `<local-path>` back to `<local-path>`; remove the marker file.

The migrations are *forward-compatible* — a v1.1 config still loads under a v1.2-aware eawf (just without the new sections); a v1.2 config refuses to load under a v1.1-aware eawf (the unknown sections are present and `extra="forbid"` rejects them — by design, prevents silent loss of new config).

## 8. Open questions for operator

### Q1 (resolved §4 D1) — Branch layer source-of-truth: `git symbolic-ref`. **Locked.**

### Q2 (resolved §4 D2) — Profile contribution semantics: per-field policy. **Locked.**

### Q3 (resolved §4 D3) — Conflict declaration grammar: declaration fails, `overrides:` is the escape hatch. **Locked.**

### Q4 (resolved §4 D4) — Override grammar: whole-profile override (per-field deferred). **Locked.**

### Q5 (resolved §4 D5) — Profile authoring API: YAML only (no Python entry-point). **Locked.**

### Q6 (resolved §4 D6) — Language extensibility: Python locked through v0.4; PyO3 only on benchmark trigger. **Locked.**

### Q7 (resolved §4 D7) — Bootstrap template flow: both `--template` and `--profiles`, mutually exclusive. **Locked.**

### Q8 (resolved §4 D8-D10) — Per-profile session-policy default: per V8. **Locked.**

### Q9 (resolved 2026-05-16) — Branch layer naming for `/`-bearing branches. **Locked: subdirectory form.**

Operator picked subdirectory: branch `feature/eawf-v0.3-p20` lives at `<repo>/.ea/branches/feature/eawf-v0.3-p20.yaml`. Loader walks `<repo>/.ea/branches/**/*.yaml` and reconstructs branch identity from the path. Mirrors git's `.git/refs/heads/<branch>` namespace. Subdir-vs-file collisions blocked at git layer (git itself rejects `a/b/c` when ref `a/b` exists) — loader inherits the constraint. §5.1.2 + §6 F4/F4b updated.

### Q10 (resolved 2026-05-16) — `EAWF_BRANCH` env override. **Locked: no, never.**

Branch layer always sourced from `git symbolic-ref --short HEAD`. CI's `actions/checkout` already attaches the right branch; env var override is foot-gun (silent masking).

### Q11 (resolved 2026-05-16) — `profiles.conflict_resolution` default flip. **Locked: preserve-on-migrate, new-default-on-fresh-init.**

Migrator writes explicit `conflict_resolution: "prompt"` on existing operator configs (preserves current behavior). Fresh `eawf init` gets `"fail"` per V3 fail-fast [1:78]. CHANGELOG documents the flip.

### Q12 (resolved 2026-05-16) — Wave layer persistence. **Locked: transient (RAM only).**

Wave config execution-scoped; `Wave.dispatch_history` [2:362-365] is the durable record. Dispatcher rebuilds in-progress wave config from history on daemon spawn. §5.1.3 + §6 F18 already match.

### Q13 (resolved 2026-05-16) — Trust ledger scope. **Locked: both, layered.**

User-scope trust at `<local-path>` extends to every repo where the repo layer has no contradicting entry; repo-scope trust at `<repo>/.ea/config.yaml` overrides for that repo only. Operator chooses `trust-user` vs `trust-repo` at first-use AUQ. §5.5 updated.

### Q14 (resolved 2026-05-16) — Catalog stub fate. **Locked: fulfil bodies in C08 ship (Tier B).**

Nine stubs (`apps`, `infra`, `docs`, `ml`, `quant`, `game`, `re`, `robotics`, `a11y`) gain real bodies in C08 ship. Each ≥1 render_block + ≥1 instrument or state_extension. Bodies sketched in §5.8.6. Stub-warning logic retired. G7b added to §2 goals; §7.3 rollout updated.

### Q15 (resolved 2026-05-16) — Schema-version sentinel naming. **Locked: separate per surface.**

`config.schema_version: "1.2"`, `profile.schema_version: "2"`, `state.schema_version: "1.0"` [2:574]. Each subsystem evolves independently; migrator runs per surface. §7.1 already matches.

## 9. References

[1] `.ea/local/research/long-term/2026-05-16-c00-spec-index.md` — C00 spec architecture index (V1-V8 verdicts; cluster catalog; per-cluster scope contract); V3 [1:76-96], V5 [1:127-151], V7 [1:184-224], V8 [1:226-271].
[2] `.ea/local/research/long-term/2026-05-16-c01-foundations.md` — C01 Foundations (Profile entity URN + lifecycle [2:594-622, 2:1100-1123]; Wave fields [2:362-365]; entity catalog; persona authority matrix [2:1240-1276]; SDLC mapping [2:1330-1395]).
[3] `.ea/local/research/long-term/2026-05-16-c02-daemon-topology.md` — C02 daemon spec (IPC protocol [3:230-289]; method catalog [3:294-345]; config-RPC methods [3:299-345]; D12 runtime-fallback retry policy [3:145]; D13 session-handle TTL [3:146]).
[4] `.ea/local/research/2026-05-11-profiles-fulfilment-and-custom-path.md` — profiles fulfilment + custom-profile discovery; operator decisions on workspace > user > builtin precedence [4:124]; trust ledger [4:151]; Tier A/B/C/D plan.
[5] `src/eawf/config/layered.py` — current layered-config merge engine (`merge_config` [5:264-363], `LAYER_ORDER` [5:45-53], `layer_path` [5:87-113], `_normalise_runtime_adapters` D14 shim [5:222-261]).
[6] `src/eawf/profiles/models.py` — `ProfileBody` v1 schema [6:74-94]; `ComposedProfile` [6:97-121].
[7] `src/eawf/profiles/compose.py` — current `compose()` algorithm [7:175-227]; per-field merge helpers [7:61-152]; `STRICTEST_KEYS` [7:58].
[8] `src/eawf/config/registry.py` — interactive config-key metadata registry (`ConfigKey` [8:54-99]; `CONFIG_REGISTRY` 24 entries [8:106-302]).
[9] `src/eawf/config/profile.py` — `enable_profile` flow (writes profiles.enabled + materialises state keys) [9:152-222]; `_atomic_write_yaml` [9:72-96].
[10] `.ea/local/research/long-term/2026-05-15-language-and-pyo3-fit.md` — language choice + PyO3 fit; verdict [10:102-119]; decision rule [10:154-163]; D49 daemon concurrency [10:131-132]; D50 Python-only commitment [10:146].
[11] `src/eawf/config/defaults.py` — `BUILT_IN_DEFAULTS` baseline [11:29-311]; `CONFIG_SCHEMA_VERSION` [11:26].
[12] `.ea/local/research/2026-05-15-code-quality-profile-proposal.md` — P21+ ProfileBody extension: `quality_thresholds`, `enforcement_hooks`, `lint_overlays`, `config_overlays` [12:73-110]; wave breakdown [12:115-127]; deferred items [12:129-138].
[13] `AGENTS.md` — non-negotiable rules (CLI is dispatch; strict Pydantic with `extra="forbid"`; state CLI sole mutator; naming conventions including `runtime.preference` shape; spike workflow; commit prefix; planned-scope revisability).
[14] `src/eawf/profiles/loader.py` — `list_profiles` + `load_profile` enumerate `eawf.profiles.data` package [14:46]; cached.
[15] `src/eawf/profiles/data/*.yaml` — bundled profile bodies: `core.yaml` (full), `python.yaml` (full), `research.yaml` (full), `apps/docs/game/infra/ml/quant/re/robotics/a11y.yaml` (stubs).
[16] `src/eawf/cli/commands/config.py` — `eawf config` Typer sub-app [16:1-810]; `_ConfigSchema` minimal validator [16:89-131]; `_resolve_anchors` [16:75-83].
[17] `src/eawf/profiles/__init__.py` — public API exports for the profile subsystem.
[18] `src/eawf/profiles/discovery.py` — profile discovery surface (today: bundled only; C08 extends to workspace + user).
[19] `src/eawf/profiles/trust.py` — profile-trust ledger (today: stubbed; C08 specifies the full trust gate).
[20] `src/eawf/config/loader.py` — `load_yaml_layer` (per-layer YAML read; returns empty dict on missing or empty file).
[21] `.ea/local/research/long-term/2026-05-15-long-term-roadmap-synthesis.md` — V3 derivation prior; 429 vendor-pause logic [21:130-133] (V5 reactive switchover extends).
[22] `.ea/local/research/long-term/2026-05-15-long-term-features-deep.md` — Pydantic versioned migrations pattern [22:264-267]; KV-cache mis-layer alarm referenced by V7 [22:599].

## 10. Provenance + Scrub

### Provenance

- `store_record=none (local-only research brief)`
- `commit=3b86f7a (parent at brief authoring time)`
- `cluster=C08`
- `consumes=C00 verdicts V3, V5, V7, V8, V9 (locked 2026-05-16)`
- `consumes=C01 entity catalog + Profile URN + lifecycle`
- `consumes=C02 daemon RPC config methods; daemon = sole writer per Q1 supersede 2026-05-18`
- `supersedes=none`
- `session=eawf-spec-c08-configurability-profiles-2026-05-16`
- `last_revised=2026-05-18 (audit-driven: D13 config writer migrates into daemon per Q1; D14 telemetry.db_kind=sqlite per Q18; D15 actor_principal_id placeholder per Q3; D16 pr_merge_method config-overridable per F-28; schema_version literal locked to Literal["1.0"] per Q5/BOT-03; field registry complete; ProfileBody.contributes typed per C08-I003)`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md (12 Codex issues; no ship-blockers)`
- `operator_decisions_pending=none — Q1-Q15 + R2 Q1-Q10 resolved 2026-05-16; Q1/Q3/Q5/Q18/F-28 supersedes resolved 2026-05-18`
- `operator_decisions_locked=2026-05-16 via §4 D1-D12 + §8 Q9-Q15 + blitz R2 residuals:`
  - D1 branch via git-symbolic-ref; D2 per-field merge policy; D3 conflict-declares + overrides-discharges; D4 whole-profile override (per-field deferred); D5 YAML-only profile authoring; D6 Python-locked language + PyO3 benchmark trigger; D7 --template OR --profiles; D8 per-profile session-policy; D9 split global/project hooks; D10 research:continue / engineering:fresh / RE:continue / spike:fresh / hybrid:hybrid; D11 auto-migrate + backup + envelope; D12 list[str] goals + dict[str,float] success_metrics.
  - Q9 branch path = subdirectory form (mirrors git refs); Q10 no EAWF_BRANCH env override; Q11 preserve-on-migrate + new-default-on-fresh-init; Q12 wave layer transient (RAM only); Q13 trust ledger layered both user + repo scopes; Q14 catalog stubs fulfilled in C08 ship (Tier B, §5.8.6); Q15 separate schema_version per surface.
  - Blitz R2 verdicts: R2-Q1 name all three mutator surfaces (state-CLI / layered-config writer / wave-RPC); R2-Q4 extends + overrides fully orthogonal; R2-Q9 wave-layer whitelist `runtime.*` + `dispatch.*` only; R2-Q10 drop spike-engineering conflict; R2-Q2 all new C08 keys surface in `ConfigKey` registry (~14 new menu rows, 3 new tabs); R2-Q6 skills wire = C04, hooks wire = dedicated future phase; R2-Q7 migrator runs on daemon startup + first daemonless CLI load; R2-Q8 add `eawf sync` downstream consumer note (§5.11). Auto-fixes: Q3 lifecycle cross-ref to C01 §5.4.14; Q5 explain-output label corrected.

### Scrub

- status: clean
- references: repo-relative or external URL only
- local paths: none
- real emails: none (canonical author block lives in pyproject only; not present in this brief)
- abstract placeholder names: not applicable (no mockup repos cited; project codes used in examples are the real EAWF code which is published in repo metadata)
- machine identifiers: none
- credentials / API keys: none
