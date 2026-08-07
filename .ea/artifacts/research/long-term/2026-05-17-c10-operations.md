# C10 — Operations (Distribution, Docs, DX, EU) — Eä framework long-term specs

**Cluster:** C10 (Operations — versioning + release channels + packaging matrix + per-OS service-file distribution + state-schema migration + telemetry contract + docs IA + per-persona onboarding + error UX template + progress / resumability / backup / remote-dev + EU calibration + budget enforcement + variance reporting)

**Title:** Operations

**Status:** `accepted` (operator ratified §8 Q1..Q12 on 2026-05-17 via 3-round AskUserQuestion blitz; see §10 Provenance for the 2 override deltas: Q3 PyPI-only + Q4-refr never-revisit; Q6 strict-local-no-network)

**Created:** `2026-05-17T00:00:00Z`

**Ratified:** `2026-05-17T00:00:00Z`

**Author:** `claude-opus-4-7`

**Depends on:** C00 [1] (V1 / V3 / V4 / V6 / V7 / V9 load-bearing); C01..C09 (every cluster contributes a surface C10 packages, documents, or budgets)

**Consumed by:** none in this batch (C10 is the penultimate cluster — C11 consumes only external-integration surfaces; C10's distribution / docs / budget / EU outputs ship to end-operators directly).

## 1. Purpose + scope statement

C10 locks the **operator-facing release substrate**: how Eä gets built, versioned, packaged, shipped, installed, upgraded, documented, observed, and accounted for. Every cluster from C01..C09 produces typed Python code, CLI verbs, schemas, hooks, telemetry rows, and operator surfaces; C10 wires those surfaces into a coherent distribution + docs + EU contract that an operator can install in five minutes and reason about across version bumps for years.

The brief locks ten subsurfaces:

1. **Versioning policy** — semver MAJOR.MINOR.PATCH with `-alphaN` / `-betaN` / `-rcN` suffixes; phase-bundled alpha cadence; daemon-protocol version-skew matrix.
2. **Release channels** — `alpha` (per-phase), `beta` (per cluster-batch close), `stable` (per quarterly minor / per breaking-change major).
3. **Packaging matrix** — **PyPI only** (operator ratified Q3 + Q4-refr 2026-05-17: PyPI-permanent; brew formula, Docker image, and PyInstaller all rejected across v0.3 → v1.0). Wheel + sdist on every channel.
4. **Per-OS service-file packaging** (V6) — launchd plist (macOS), systemd-user unit (Linux), pywin32 Windows Service (primary) + NSSM (fallback documented). Service-file templates shipped inside the PyPI wheel under `eawf/_data/service_templates/`; rendered + installed by `eawf daemon enable`.
5. **State-schema migration tooling** — `eawf migrate` verb auto-detects `state.schema_version`, prompts confirm, runs migrations, writes `state.json.bak.v<from>.<to>` backup; migration scripts live under `src/eawf/migrations/v<from>_to_v<to>.py`.
6. **Telemetry contract** (V7) — opt-in by default; **strictly local** (operator ratified Q6 2026-05-17 stricter than V7 default: no `telemetry.export.endpoint` HTTPS POST surface exists; data NEVER leaves the machine). What's collected per C09 §5.9 metrics catalog [9]; where it lives = user-scope DuckDB at `<local-path>` per C09 D7. Local-file export via `eawf metrics export --format prom|json|csv --out <local-path>` remains supported.
7. **Docs IA** — top-level layout, `mkdocs` + `mkdocs-material` toolchain, auto-generated CLI ref (mkdocs-typer plugin), auto-generated skill ref (mkdocs-skills custom plugin), auto-generated schema ref (mkdocs-pydantic custom plugin), arch deep-dives (one per cluster), per-profile tutorials (V3-derived), ADR registry, glossary.
8. **Per-persona onboarding flows** — operator-new-user, operator-new-repo, operator-new-project-type, agent-new-repo, daemon-first-spawn, telemetry-opt-in. Each flow has a quickstart path + a deep-dive doc + a failure-fallback path.
9. **Error UX template** — every CLI / TUI / skill error follows `<one-line cause>. <suggested next step>. (See: <doc anchor>)`. Encoded as `ErrorUXTemplate` Pydantic shape consumed by every error envelope at render time.
10. **EU calibration + budget enforcement** — empirical calibration from P09..P15 wave timestamps (per the archive brief [16]); bucket model (XS/S/M/L/XL) confirmed; per-wave / per-iter / per-phase soft-default budget enforcement with hard opt-in via `flow.budget.enforce=hard`; weekly variance dashboard tile.

**In scope (per C00 §C10 [1:896-944]).** Everything in §1.1–§1.10 above. Each subsurface gets a concrete schema, file path, CLI verb, or contract in §5.

**Out of scope.**

- Hosted SaaS deployment surface (deferred to v0.6+ per C00 §C10 non-goals [1:912-913]).
- Multi-language (i18n) docs (deferred to indefinite-future per [1:914]).
- TUI rendering of distribution / release-status panes — that's the C06 metrics overlay tile catalog [7].
- External integration surface (GitHub PR / Linear / Slack notifications of release events) — C11 owns the bridge contracts.
- Detailed CI pipeline shape — C09 [9] owns the matrix, threshold map, and per-OS lane budget. C10 references C09's pipeline; it does not redefine it.
- Per-runtime native plugin tree authoring — C07 [11][12] owns the `build/<runtime>-plugin/` source-of-truth surface; C10 owns *release packaging* of those bundles (PyPI wheel contents, post-install sync invocation, upgrade docs).

## 2. Goals + non-goals

### Goals

- **G1.** One Eä release per quarter (stable) + per-phase alpha cadence + per-cluster-batch beta. Every release has a `CHANGELOG.md` block auto-generated from per-phase commit prefixes.
- **G2.** Five-minute quickstart from `pip install eawf` → `eawf init` → first wave dispatched.
- **G3.** Per-OS service-file distribution baked into the PyPI wheel; `eawf daemon enable` installs the service file natively (launchd plist / systemd-user unit / pywin32 Service) per V6 [1:154-183].
- **G4.** State-schema migration tooling that handles `v1 → v2 → v3` chains automatically with a single command and a recoverable backup.
- **G5.** Telemetry opt-in by default; no implicit phone-home; operator-controlled export endpoint with regex + optional presidio scrubber per C09 [9:1320].
- **G6.** Per-profile tutorials (research / engineering / reverse-engineering / spike / hybrid) — one tutorial per profile bundle, each runnable end-to-end against a fresh repo. Per V3 [1:77-97].
- **G7.** Error UX template uniformly applied across CLI / TUI / skill envelopes so every operator-visible error has a suggested next step.
- **G8.** EU calibration grounded in P09..P15 empirical wave timestamps (per archive [16]); bucket model (XS/S/M/L/XL); soft-default budget enforcement.
- **G9.** Docs IA that auto-regenerates from source (CLI / skills / schemas) so doc-drift is structurally impossible for the auto-gen surfaces.
- **G10.** Resumability — every long-running operation (wave dispatch, daemon crash, interrupted phase) recoverable via `eawf <verb> --resume <token>` per the daemon-compatibility note in [17:266-273].

### Non-goals

- **NG1.** Standalone-binary distribution (PyInstaller, briefcase, py2app) **AND** brew formula **AND** Docker image. Operator ratified Q3 + Q4-refr 2026-05-17 as PyPI-permanent across v0.3 → v1.0. Operator base is Python-literate; the ~20-MB Python install cost is acceptable. NOT revisited at any subsequent MINOR.
- **NG2.** Hosted multi-tenant SaaS surface. Eä is a local-first tool; the daemon is single-user per V1 [1:31] and V6 [1:179].
- **NG3.** Cross-language doc i18n. English-only through v0.5.
- **NG4.** Auto-publishing release notes to external systems (Slack / Linear / Notion). C11 owns those bridges; C10 ships the canonical `CHANGELOG.md` + release-notes markdown only.
- **NG7.** Network telemetry shipping of ANY shape. Operator ratified Q6 2026-05-17 strict-local-only. There is no `telemetry.export.endpoint` HTTPS POST verb, no Anthropic-hosted upload, no SaaS dashboard sync. Operator may write local files only (`--format prom --out <path>`); subsequent transport off the machine is operator-orchestrated outside eawf.
- **NG5.** Cloud-agent fanout cost reconciliation — when the operator runs a wave on a paid API instead of a subscription. Deferred to v0.6+; v0.3..v0.5 assume subscription-billed runtimes (per [17:97-103]).
- **NG6.** Distributed-cost ledger across multiple users / orgs. EU + USD ledger is single-user only (matches V1 + V7 user-scope DB).

## 3. Prior verdicts cited

### From C00 spec index [1]

- **V1** [1:24-53] — eawfd daemon is Day-1 with smart-spawn writer + daemonless reader. Affects C10: daemon-protocol version-skew handling at install / upgrade time; service-file distribution (V6 layered on V1); session-resume semantics drive resumability story (G10).
- **V3** [1:76-96] — Composable profile bundle with declared precedence. Affects C10: per-profile tutorials (G6) — each profile bundle gets one tutorial; per-profile bootstrap-template flow consumes C08 D7 [8:139-145].
- **V4** [1:99-126] — Cluster-sequential batching under `.ea/local/research/long-term/`. Affects C10: C10 ships near the end of the brief series; release cadence (G1) aligns to cluster-batch closes for beta channel.
- **V6** [1:154-183] — Cross-platform daemon with per-OS native service surface. Affects C10: §5.3 packaging matrix + §5.4 service-file distribution (G3) implement V6's release-packaging affects line [1:182-183].
- **V7** [1:184-224] — Telemetry vendor-and-rebuild of the upstream telemetry prototype; opt-in by default; no implicit phone-home. Affects C10: §5.6 telemetry contract (G5); §5.16 onboarding flow includes a telemetry-opt-in path.
- **V9** [1:274-315] — Native per-runtime plugins remain first-class distribution channel. Affects C10: packaging matrix ships canonical `build/<runtime>-plugin/` trees inside the PyPI wheel under `eawf/_data/plugins/<runtime>/`; install / upgrade docs cover `eawf plugin sync` invocation (G2 + G9).

### From C01 (Foundations) [2]

- **C01 §5.x state-entity catalog with `schema_version` field** [2:754-840] — drives §5.5 migration tooling. Discriminated-union shape lifted from `2026-05-15-long-term-roadmap-synthesis.md` prereq-bundle [17:52-65].
- **C01 §URN scheme + 26-kind enum** [2:23-52, ratified D1] — referenced in docs glossary (G9) + onboarding (G2).

### From C02 (Daemon + topology) [3]

- **C02 §5.10 per-OS service file content** [3:493-650] — verbatim systemd-user unit, launchd plist, pywin32 service skeleton consumed by §5.4. C10 only specifies *release-packaging* path; C02 owns the content.
- **C02 daemon-protocol version field** [3:protocol section] — referenced in §5.1 version-skew matrix.

### From C03 (Spec infrastructure) [4]

- **C03 §5.x mockup-required WaveSpec validator** [4:DR-1 ratified] — referenced in §5.12 onboarding flow (operator-first-wave path) + §5.10 per-profile tutorials (engineering-profile carries a mockup-required walkthrough).

### From C04 (Workflow + skills) [5]

- **C04 §5.4.11 /init skill detail** [5:879-908] — drives §5.16 onboarding flow (operator-new-repo path); `/init` is the entry-point CLI for a new project.
- **C04 §skill manifest schema** [5:contract] — auto-doc-gen source for the mkdocs-skills custom plugin (G9).

### From C05 (CLI surface) [6]

- **C05 §5.9 stability tiers** [6:808-884] — drives `experimental` / `stable` / `deprecated` marker rendering in the auto-generated CLI ref (G9).
- **C05 §5.1.x verb-noun matrix** [6:135-405] — auto-doc-gen source for mkdocs-typer plugin (G9).
- **C05 §release verbs** [6:347-359] — `eawf release changelog` + `eawf release notes` (stable tier) drive §5.7 CHANGELOG auto-gen.
- **C05 §5.1.13 daemon verbs** [6:371-381] — `eawf daemon enable / disable / status` cited in §5.4 service-file install path.
- **C05 §5.1.15 completion install** [6:396-400] — covered under quickstart onboarding (G2).
- **C05 §exit code taxonomy** [6:467-525] — error UX template (G7) embeds the exit-code field per envelope.

### From C06 (Operator surface) [7]

- **C06 §metrics overlay tile catalog** [7:metrics section] — telemetry tiles (V7-projected) surface in TUI; C10 docs the tiles in the dashboard reference (G9).
- **C06 §onboarding TUI flow** [7:onboarding section] — first-launch TUI walks operator through `/init`; C10 owns the *non-TUI* onboarding paths in §5.16 (operator-new-user via CLI, agent-new-repo via dispatch envelope).

### From C07 (Subsystems — runtime + skill dispatch + worktree + events) [11][12]

- **C07a §plugin manifest schema** [11:310-378] — auto-doc-gen source for the per-runtime plugin matrix (G9).
- **C07a §5.7 plugin manifest** [11:310] — release packaging ships canonical `build/<runtime>-plugin/` trees per runtime (V9 implementation surface).
- **C07b §branding** [12:552-622] — `Eä` literal + Wong-orange accent (`#E69F00`) — drives logo + docs theme.

### From C08 (Config + profiles) [8]

- **C08 §5.7 five bootstrap templates** [8:554-690] — drives §5.10 per-profile tutorials (one per template) + §5.16 operator-new-project-type onboarding path.
- **C08 §schema migration framework** [8:1297-1310] — referenced in §5.5 migration tooling; same `v<from>_to_v<to>` discipline.

### From C09 (Quality + observability) [9]

- **C09 §5.9 telemetry subsystem schema + CLI + metrics catalog** [9:462-1035] — drives §5.6 telemetry contract; C10 ships the *operator-facing* contract; C09 ships the implementation. C10 does not redefine the schema; it documents the operator semantics + opt-in default.
- **C09 §5.9.6.1 PRICING dict + currency-check** [9:899-1008] — C10 ships the auto-PR pricing-drift CI workflow at release time.
- **C09 §5.4 CI pipeline DAG** [9:223-315] — C10 references the matrix at release time but does not redefine the lane shape.
- **C09 §5.10 incident-cause taxonomy** [9:1036-1100] — incident-cause enum drives the `ErrorUXTemplate` cause-classification field in §5.13.

## 4. Decision matrix

Every load-bearing axis for C10 with concrete options and recommendation. Recommendation column carries the (Recommended) marker for AskUserQuestion seeding in §8.

| # | Axis | Options | Recommendation | Rationale |
|---|---|---|---|---|
| **D1** | Versioning scheme | (a) PEP 440 only — `0.3.0a1`; (b) semver-with-PEP-440-suffixes — `0.3.0-alpha.1+phase.P20`; (c) calver `2026.05.0` | **(b) — semver core + PEP-440-compatible suffixes; `__version__` string carries `0.3.0a1` for pip-resolver compatibility AND `0.3.0-alpha.1+phase.P20` build-metadata for human-readable display** | semver+PEP-440 hybrid keeps `pip install eawf==0.3.0a1` resolvable while exposing the phase-context to operator-facing surfaces. Calver loses semver's breaking-change signal. Pure PEP 440 hides phase context from `eawf --version` output. |
| **D2** | Alpha cadence | (a) per-phase (cut on every phase merge); (b) per-iter; (c) per-cluster-batch | **(a) — per-phase alpha** | Phase = natural delivery unit. P20 = `0.3.0a20`. Per-iter is too noisy (iters merge in waves). Per-cluster-batch is too coarse for early-feedback alpha consumers. |
| **D3** | Beta cadence | (a) per-cluster-batch close; (b) per quarter; (c) per stable-candidate set | **(a) — per cluster-batch close** | Cluster batch = natural cohesion point (multiple related phases land together). C00 [1:1015-1030] already defines clusters. Quarterly is too slow for v0.3 → v0.5 pace; stable-candidate-set is fuzzy. |
| **D4** | Stable cadence | (a) per quarter (calendar-bound); (b) per validated MINOR (feature-bound); (c) per breaking-change MAJOR | **(b) — per validated MINOR; MAJOR triggers breaking-change** | MINOR ships when accumulated alphas + final beta pass the v0.3-ship-gate audit. Calendar binding pressures premature ship. MAJOR auto-cuts on `state.schema_version` MAJOR bump. |
| **D5** | Packaging matrix | (a) PyPI only; (b) PyPI + brew; (c) PyPI + brew + Docker (CI); (d) (c) + PyInstaller standalone | **(a) — PyPI only** ([Q3 + Q4-refr ratified 2026-05-17, override vs original (c) recommendation]) | Operator declared PyPI permanent across v0.3 → v1.0. Brew formula, Docker image, and PyInstaller all rejected as out-of-scope at any MINOR. Rationale: smallest maintenance surface; operator base is Python-literate; CI / reproducibility use is served by `uv sync --frozen` against `uv.lock`. |
| **D6** | Service-file distribution channel (V6) | (a) bundled inside PyPI wheel; (b) separate installer (brew formula post-install hook only); (c) separate `eawf-service` package | **(a) — bundled inside PyPI wheel under `eawf/_data/service_templates/`; rendered + installed by `eawf daemon enable`** | One install, one upgrade surface. `eawf daemon enable` reads the bundled Jinja template, substitutes `$HOME` + `$VENV_PATH` + `$DAEMON_ARGS`, writes to the OS-canonical location, registers with the service manager. brew post-install hook (option b) splits the upgrade story; separate package (c) couples version skew with the wheel. |
| **D7** | Telemetry default | (a) opt-in (operator must `eawf config set telemetry.enabled true`); (b) off-only (no telemetry surface at all in v0.3); (c) opt-out (telemetry on by default; opt-out via config) | **(a) — opt-in (per V7 hard non-negotiable [1:219])** | V7 already locks this. C10 documents the opt-in flow + adds a one-time onboarding nudge per C09 §7.3 Q3-W06 [9:1260]. |
| **D8** | Telemetry data shipping | (a) HTTPS POST to operator-configured `telemetry.export.endpoint`; (b) no-network-by-default + opt-in HTTPS POST; (c) automatic upload to Anthropic-hosted endpoint; (d) strictly local — no network shipping verb at all | **(d) — strictly local; no `telemetry.export.endpoint` surface exists** ([Q6 ratified 2026-05-17, stricter than V7 default]) | Operator override declared no network telemetry of any shape. V7 [1:219] sets opt-in default; Q6 strengthens it: the HTTPS POST verb is not built. Local-file export (`--format prom \| json \| csv --out <local-path>`) remains the only telemetry-out-of-DuckDB path; subsequent transport off the machine is operator-orchestrated outside eawf. |
| **D9** | Doc generation toolchain | (a) `mkdocs` + `mkdocs-material`; (b) `sphinx` + `furo`; (c) plain markdown rendered by GitHub; (d) `docusaurus` | **(a) — mkdocs + mkdocs-material** | mkdocs is Python-native (operator base is Python-literate), supports hot-reload during authoring, has a mature `mkdocs-typer` plugin for auto-CLI-ref, and `mkdocs-material` has a Wong-orange-compatible theme (per C07b branding [12:748]). sphinx is heavier; docusaurus pulls JS toolchain; plain markdown loses search + navigation + cross-ref. |
| **D10** | EU calibration source | (a) theoretical (manually estimate per-task); (b) empirical from P09..P15 wave timestamps; (c) hybrid (theoretical bucket + empirical multiplier per profile) | **(b) — empirical from P09..P15** | Archive brief [16] already did the calibration: iter elapsed clusters around 0.5-2 EU, max wave 0.4 EU outside hotfixes; bucket model (XS=0.25, S=0.5, M=1.0, L=2.0, XL=3.5) emerged from observed data. Theoretical estimates overshot 3-5×; hybrid adds complexity without measured gain. Recompute calibration weekly via `eawf metrics export --window 90d`. |
| **D11** | Budget enforcement default | (a) soft (warning + continue); (b) hard (fail wave at cap); (c) soft default + hard opt-in per scope | **(c) — soft default + hard opt-in via `flow.budget.enforce=hard`** | Synthesis brief [17:127-128] locks "soft-cancel breaker only" for v0.3 to gather warn-only distribution data. Hard enforcement opts in per-phase or per-wave when the operator wants determinism (e.g. CI budget-bound run). |
| **D12** | Quickstart format | (a) `docs/getting-started.md` only; (b) (a) + asciinema cast; (c) (b) + interactive `eawf tour` CLI | **(b) — markdown + asciinema cast** | Asciinema cast under `docs/casts/quickstart.cast` shows real terminal behaviour; C06 onboarding TUI [7] already specifies the asciinema determinism shape so the cast records cleanly. Interactive `eawf tour` (c) adds another CLI verb to maintain; deferred to v0.5+ if quickstart drop-off shows demand. |
| **D13** | Tutorial format | (a) one markdown per profile; (b) (a) + asciinema cast per tutorial; (c) full Jupyter-style notebook | **(b) — markdown + asciinema cast per profile bundle** | Per-profile tutorial answers V3 [1:97]. Notebook (c) inflates dependency footprint; (a) lacks the visual proof the cast provides. Five tutorials = five casts; recording budget is one operator-hour per tutorial. |
| **D14** | Brew formula auto-publish | (rejected) | **N/A — brew not shipped** ([Q4-refr ratified 2026-05-17]) | PyPI-permanent picks left no brew surface. CI publishes wheel + sdist only. |
| **D15** | Docker image audience | (rejected) | **N/A — Docker image not built** ([Q4-refr ratified 2026-05-17]) | CI reproducibility served by `uv sync --frozen` against `uv.lock`. No GHCR push from release CI. |
| **D16** | Migration prompt UX | (a) auto-run silently; (b) prompt with `[Y/n]`; (c) require explicit `eawf migrate --confirm` | **(b) — interactive prompt with backup announcement; `--no-input` mode for CI** | State migration writes a backup before running; the operator sees `state.json.bak.v<from>.<to>` path + chain length + estimated runtime. CI mode (`--no-input`) auto-accepts when the backup write succeeds. Silent auto-run (a) is too aggressive for a critical-data path. Explicit confirm (c) blocks `/flow` mid-run. |
| **D17** | Error UX template format | (a) free-form per-error string; (b) typed `ErrorUXTemplate(cause:str, next_step:str, see:str|None)`; (c) (b) + machine-readable error-code | **(c) — typed `ErrorUXTemplate` with `error_code: str`** | Operator pastes the error code into doc-search; machines parse it from JSON envelopes. C05 [6:467-525] exit codes give the *exit-level* taxonomy; `error_code` is the *cause-level* (`SCOPE_CONFLICT`, `STATE_VALIDATION_FAILED`, etc.) per C09 incident-cause [9:1036-1100]. |
| **D18** | Backup default | (a) `state.json.bak.v<X>.<Y>` adjacent to state.json; (b) `<local-path>`; (c) `.ea/backups/state.json.<ts>` (committed-but-gitignored) | **(a) for migration backups; (b) for snapshot backups via `eawf backup create`** | Migration backups stay adjacent so `git checkout` rollback is trivial. Manual operator-triggered snapshot backups (a different feature) go to a user-scope path so multi-repo backup-restore lives in one place. (c) conflicts with `.ea/` gitignore policy. |
| **D19** | Variance reporting cadence | (a) weekly auto-tile in TUI; (b) on-demand `eawf metrics variance`; (c) per-phase-close ship-gate output | **(a) + (b) + (c) — all three; same projection feeds all** | The TUI tile is the daily ambient surface; the CLI is the deep-dive; the ship-gate output forms part of the phase PR body. Single projection (C09 §5.9 [9:865]) feeds all three; no redundant compute. |
| **D20** | Per-OS service-file install location | (a) Linux `<local-path>` + macOS `<local-path>` + Windows registered via `pywin32`; (b) `/etc/...` system-wide; (c) `/opt/eawf/...` | **(a) — per-user only; matches V1 + V6 single-user invariant [1:179]** | System-wide install needs sudo at every upgrade; conflicts with V6 [1:179] "one service file per user". `/opt/eawf/...` is uncommon for a Python tool. |
| **D21** | Onboarding nudge for telemetry | (a) one-time prompt at first `eawf init`; (b) per-`eawf metrics show` invocation when disabled; (c) (a) + (b) | **(c) — (a) + (b)** | First-init nudge captures operators who never touch metrics. `eawf metrics show` invocation captures operators who discover metrics later. Both are dismissible via `eawf config set telemetry.opt_in_dismissed true`. Per C09 Q3-W06 [9:1260]. |
| **D22** | Long-running operation resumability | (a) every long-running verb supports `--resume <token>`; (b) only `wave dispatch` + `flow execute`; (c) auto-resume on next invocation | **(b) — `--resume` only on `wave dispatch`, `flow execute`, `eawf migrate`** | Three high-stakes long-running verbs. Other verbs are <30s and don't need resume. Auto-resume (c) hides decision points the operator must see (e.g. partial migration). |

## 5. Proposed schemas / APIs / protocols

### 5.1 Versioning policy + version-skew matrix

#### 5.1.1 Version string grammar

Eä carries three version concepts that MUST stay aligned:

```
package:        eawf 0.3.0                          # pyproject.toml [project.version]
runtime str:    eawf-0.3.0-alpha.1+phase.P20        # __version__ output
daemon proto:   eawfd-rpc/3.0                       # JSON-RPC initialize handshake (C02)
state schema:   state.json.schema_version = "1.2"   # written by /init, bumped by migration
plugin schema:  build/<runtime>-plugin/manifest.schema_version = "2.0"
```

`__version__` (in `src/eawf/__init__.py`) carries the **PEP-440 string** (`0.3.0a1`, `0.3.0rc1`, `0.3.0`) so `pip install eawf==0.3.0a1` resolves. The build-metadata extension (`+phase.P20`) shows in `eawf --version --verbose`:

```python
# src/eawf/_version.py (generated by release CI; do not edit by hand)
from __future__ import annotations

__version__ = "0.3.0a1"           # PEP-440-compatible; pip resolves on this
__version_long__ = "0.3.0-alpha.1+phase.P20.commit.{sha7}"
__release_channel__ = "alpha"      # alpha | beta | rc | stable
__phase__ = "P20"                  # phase symbol; empty on stable
__commit__ = "{sha7}"              # short sha
__released_at__ = "2026-05-17T00:00:00Z"
```

`eawf --version`:

```
eawf 0.3.0a1
```

`eawf --version --verbose`:

```
eawf 0.3.0a1 (alpha; phase P20; commit 3b86f7a; released 2026-05-17T00:00:00Z)
daemon protocol eawfd-rpc/3.0
state schema    1.2 (compatible: 1.0..1.2)
plugin schema   2.0 (compatible: 2.0)
```

#### 5.1.2 Version-skew matrix

Daemon protocol, state schema, and plugin schema bump independently from the package version, but MUST be back-compatible within a MINOR. When a skew is detected the operator sees a one-line cause + suggested next step (D17) instead of a stacktrace:

| Skew | Detection point | Operator action | Behaviour |
|---|---|---|---|
| Package < state schema | `eawf <verb>` at startup | `pip install -U eawf` | Refuse to mutate; allow read paths (V1 daemonless-reader carve-out) |
| Package > state schema | First mutating verb | `eawf migrate` | Auto-prompt per D16; backup + run migrations |
| Daemon protocol minor mismatch | `initialize` JSON-RPC | (none — auto-compat) | Daemon advertises max compatible version; CLI fits inside the envelope |
| Daemon protocol major mismatch | `initialize` JSON-RPC | `eawf daemon restart` | Daemon refuses; emits error-code `DAEMON_PROTOCOL_MAJOR_SKEW` + `daemon stop && daemon start` next-step |
| Plugin manifest schema mismatch | `eawf plugin sync` | `eawf plugin sync --regenerate` | Drop + regenerate the per-runtime plugin tree |

All five cases are mediated through `ErrorUXTemplate` (§5.13).

### 5.2 Release channels + cadence

#### 5.2.1 Channel table

| Channel | Trigger | PyPI tag | Audience | Cadence |
|---|---|---|---|---|
| `alpha` | Per-phase merge to long-running feature branch | `0.X.0aN` (PEP-440) | Eä-internal dogfood; early adopters who set `pip install eawf --pre` | Per-phase (~2-4 weeks) |
| `beta` | Per cluster-batch close (multi-phase aggregate) | `0.X.0bN` | Pre-release validators; CI runs against beta to surface breakage early | Per cluster batch (~6-12 weeks) |
| `rc` | When `audit ship-gate` passes for the candidate MINOR | `0.X.0rcN` | Release-candidate validators; one full week of warn-only metrics monitoring | Per MINOR ship (~6 months) |
| `stable` | When `rc` passes one full week without P0/P1 incident | `0.X.0` (no suffix) | All users | Per validated MINOR (D4) |

Per Q3 + Q4-refr ratification (2026-05-17): every channel publishes to PyPI only. No brew tap, no Docker image, no GHCR push.

#### 5.2.2 Release CI workflow

`.github/workflows/release.yaml` — new in v0.3:

```yaml
name: Release
on:
  push:
    tags:
      - 'v0.*'                  # v0.3.0a1, v0.3.0b1, v0.3.0rc1, v0.3.0
  workflow_dispatch:
    inputs:
      channel: {description: 'alpha | beta | rc | stable', required: true}
permissions:
  contents: write               # for CHANGELOG.md commit on stable
jobs:
  build-wheel:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - uses: astral-sh/setup-uv@v8.1.0
        with: {python-version: '3.14'}
      - run: uv sync --frozen
      - run: uv run python -m build --wheel --sdist
      - uses: actions/upload-artifact@v4
        with: {name: dist, path: dist/}
  publish-pypi:
    needs: build-wheel
    runs-on: ubuntu-24.04
    environment: pypi-publish
    steps:
      - uses: actions/download-artifact@v4
        with: {name: dist, path: dist/}
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          packages-dir: dist/
          # Trusted-publisher via PyPI OIDC; no API token in repo secrets.
  changelog-commit:
    needs: publish-pypi
    if: ${{ !contains(github.ref, 'a') && !contains(github.ref, 'b') }}
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - run: uv run eawf release changelog --since-last-stable --commit
```

Per Q3 + Q4-refr (2026-05-17): no `brew-formula` job, no `docker-image` job. Wheel + sdist via PyPI trusted-publisher OIDC is the only release artifact path.

#### 5.2.3 Pre-release gates

Each channel has a pre-release CI gate that must pass before the artifact ships:

| Channel | Gate |
|---|---|
| `alpha` | All P##-touching tests + pre-commit pass on the long-running feature branch HEAD; coverage MAY drop |
| `beta` | (alpha gates) + per-OS CI matrix (V6) passes on all three OSes; per-OS bench tolerance (per C09 [9:1244]) within threshold |
| `rc` | (beta gates) + acceptance pytest marker (full lifecycle scenarios) passes + telemetry projection rebuilds cleanly from canonical sources |
| `stable` | (rc gates) + one full week of warn-only metrics on `rc` with zero P0/P1 incidents (per [17:127-128] soft-cancel-only invariant) |

### 5.3 Packaging matrix

| Target | Audience | Trigger | Build pipeline | Output |
|---|---|---|---|---|
| PyPI wheel | All operators | Every channel (alpha/beta/rc/stable) | `python -m build --wheel`; `pypa/gh-action-pypi-publish@release/v1` with trusted-publisher OIDC | `eawf-0.X.YYY.whl` |
| PyPI sdist | Source-install operators; downstream packagers | Every channel | `python -m build --sdist` | `eawf-0.X.YYY.tar.gz` |
| Brew formula | (rejected per Q4-refr 2026-05-17) | — | — | — |
| Docker image | (rejected per Q4-refr 2026-05-17) | — | — | — |
| PyInstaller standalone | (rejected per Q4-refr 2026-05-17) | — | — | — |

Per Q3 + Q4-refr ratification: PyPI wheel + sdist is the complete packaging surface across v0.3 → v1.0. CI reproducibility served by `uv sync --frozen` against `uv.lock`; non-CI operators install via `pip install eawf` / `uv tool install eawf`.

**Wheel contents** beyond the source tree:

```
eawf-0.3.0-py3-none-any.whl
├── eawf/                              # source tree
└── eawf/_data/                        # NEW (release-bundled data)
    ├── service_templates/
    │   ├── linux-systemd-eawfd.service.j2
    │   ├── macos-launchd-dev.eawf.eawfd.plist.j2
    │   ├── windows-pywin32-eawfd.py.j2
    │   └── windows-nssm-install.bat.j2
    ├── plugins/                        # V9 — per-runtime canonical trees
    │   ├── claude/                     # build/eawf-plugin/ contents
    │   ├── codex/                      # build/codex-plugin/ contents
    │   └── opencode/                   # build/opencode-plugin/ contents
    ├── init/                           # C08 D7 — five bootstrap templates
    │   ├── research.yaml
    │   ├── engineering.yaml
    │   ├── reverse-engineering.yaml
    │   ├── spike.yaml
    │   └── hybrid.yaml
    └── docs/                           # rendered docs subset (CHANGELOG, getting-started, error-codes)
        ├── getting-started.md
        ├── CHANGELOG.md
        └── error-codes.md
```

The release CI wires the `_data/` tree at wheel-build time. The Hatchling configuration extension:

```toml
# pyproject.toml addition
[tool.hatch.build.targets.wheel]
packages = ["src/eawf"]
include = ["src/eawf/_data/**/*"]
sources = ["src"]

[tool.hatch.build.hooks.custom]
path = "tools/bundle_data.py"           # populates src/eawf/_data/ from build/ + templates/ + docs/build/

[tool.eawf.bundle]
service_templates_dir = "templates/service/"
plugins_canonical_root = "build/"
init_templates_dir     = "templates/init/"
docs_built_dir         = "docs/build/"
```

**Wheel size budget.** Current `eawf-0.2.0.whl` ≈ 350 KB (source only). Bundled `_data/` adds: ~5 KB service templates + ~250 KB three plugin trees + ~10 KB init templates + ~30 KB docs subset ≈ ~300 KB additional. Target ceiling: 1 MB. If breach: drop `plugins/codex/` + `plugins/opencode/` from the default extra, ship them via `eawf[codex]` / `eawf[opencode]` install extras.

### 5.4 Per-OS service-file packaging (V6)

#### 5.4.1 Distribution channel

Service-file templates ship inside the PyPI wheel under `eawf/_data/service_templates/`. `eawf daemon enable` reads the template, substitutes environment-specific values, writes to the OS-canonical path, registers with the service manager:

```python
# src/eawf/cli/commands/daemon.py — enable handler (sketch)
@daemon_app.command("enable")
def daemon_enable(
    ctx: typer.Context,
    auto_start: Annotated[bool, typer.Option("--auto-start/--no-auto-start", help="Register for auto-start at login.")] = True,
    idle_timeout: Annotated[int, typer.Option("--idle-timeout", help="Daemon idle-shutdown seconds.")] = 300,
) -> None:
    """Enable the eawfd daemon: write the OS-native service file and register it.

    Raises:
        UserError: if --auto-start is requested but the OS service manager is unavailable.
        StateConflict: if the daemon is already running and refusing to re-enable.
    """
    os_name = platform.system()           # 'Linux' | 'Darwin' | 'Windows'
    template_path = service_template_path(os_name)
    rendered = render_template(template_path, {
        "venv_python": sys.executable,
        "home": str(Path.home()),
        "idle_timeout": idle_timeout,
        "log_dir": str(Path.home() / ".eawf" / "logs"),
    })
    install_path = service_install_path(os_name)
    write_text(install_path, rendered)
    if auto_start:
        register_with_os(os_name, install_path)
    emit_json_or_text({"status": "ok", "service_file": str(install_path), "auto_start": auto_start})
```

`service_install_path(os_name)` mapping per V6 [1:158-160]:

| OS | Path |
|---|---|
| `Linux` | `<local-path>` |
| `Darwin` | `<local-path>` |
| `Windows` | `%APPDATA%\eawf\eawfd-service.py` (registered as `eawfd` Windows Service via `pywin32`) |

#### 5.4.2 Template contents

C02 §5.10 [3:493-650] owns the canonical service-file content (systemd-user unit at [3:494-537], launchd plist at [3:538-595], pywin32 Service at [3:597-656]). C10 ships those files as Jinja2 templates inside the wheel at `eawf/_data/service_templates/`; substitution variables are `{{ venv_python }}`, `{{ home }}`, `{{ idle_timeout }}`, `{{ log_dir }}`. `eawf daemon enable` reads the OS-appropriate template, substitutes, writes to `service_install_path(os_name)`, and registers with the OS service manager (`systemctl --user enable`, `launchctl bootstrap`, `pywin32` service install respectively).

#### 5.4.3 Uninstall

`eawf daemon disable` is the inverse: stops the service, removes the file, deregisters with the OS service manager. V6 [1:178] hard non-negotiable: reversibility.

### 5.5 State-schema migration tooling

#### 5.5.1 Migration discipline

`state.json.schema_version` is a closed-enum string field of the form `"<MAJOR>.<MINOR>"`. The current version is `"1.0"` (v0.1 / v0.2 baseline). v0.3 introduces `"1.1"` (per C01 prereq bundle [17:52-58]) and `"1.2"` (per C03 Spec entity).

Migration scripts live under `src/eawf/migrations/`:

```
src/eawf/migrations/
├── __init__.py
├── v1_0_to_v1_1.py           # adds State.principal_id field; renames scope → scope_id on events
├── v1_1_to_v1_2.py           # adds Wave.spec_path; adds Phase.spec_path; adds Iter.spec_path
└── ...
```

Each migration is a typed transformation:

```python
# src/eawf/migrations/_base.py
from __future__ import annotations
from typing import Protocol

class Migration(Protocol):
    from_version: str
    to_version: str
    def apply(self, state_dict: dict) -> dict:
        """Transform the raw state.json dict.

        Pre: input is verified-loadable against the from_version Pydantic model.
        Post: output is verified-loadable against the to_version Pydantic model.
        """
        ...
```

#### 5.5.2 `eawf migrate` CLI surface

```
eawf migrate                                # auto-detect from + to; prompt
eawf migrate --to 1.2                       # explicit target version
eawf migrate --dry-run                      # show what would change; no write
eawf migrate --no-input                     # auto-accept the prompt (CI mode)
eawf migrate --no-backup                    # skip backup write (NOT recommended; required for testing)
eawf migrate status                         # show current schema_version + available migrations
```

Algorithm:

```python
# src/eawf/cli/commands/migrate.py — top-level handler (sketch)
def migrate_cmd(to: str | None, dry_run: bool, no_input: bool, no_backup: bool) -> None:
    state = read_raw_state()                              # dict, before Pydantic
    from_version = state["schema_version"]
    to_version = to or current_target_version()
    chain = build_migration_chain(from_version, to_version)
    if not chain:
        emit_ok({"status": "no-op", "version": from_version})
        return
    if not dry_run and not no_backup:
        backup_path = state_path().with_name(f"state.json.bak.v{from_version}.{to_version}")
        write_text(backup_path, json.dumps(state, indent=2))
    if not no_input:
        confirm = ask_user_question(
            f"Migrate state.json from v{from_version} to v{to_version} "
            f"({len(chain)} step(s), backup at {backup_path})? [Y/n] ",
        )
        if confirm.lower() not in {"", "y", "yes"}:
            emit_error("user-aborted")
            return
    for step in chain:
        state = step.apply(state)
    if not dry_run:
        atomic_write_json(state_path(), state)
    emit_ok({"status": "ok", "from": from_version, "to": to_version, "steps": len(chain), "backup": str(backup_path)})
```

#### 5.5.3 Backup discipline

- Migration backups: `state.json.bak.v<from>.<to>` adjacent to `state.json` (D18a). git tracks them via the normal `.ea/state.json.bak.*` glob; rollback = `git checkout state.json.bak.v1.0.v1.1 && mv state.json.bak.v1.0.v1.1 state.json`.
- Snapshot backups (manual operator-triggered, distinct from migration): `<local-path>` (D18b). `eawf backup create` writes; `eawf backup restore --ts <ISO8601>` restores.

#### 5.5.4 Failure modes

| F# | Failure | Detection | Recovery |
|---|---|---|---|
| MIG-F1 | Migration target unknown | Migration registry lookup miss | Surface available targets list; exit 2 |
| MIG-F2 | Backup write fails | `OSError` from `write_text` | Refuse to migrate; emit `BACKUP_WRITE_FAILED` |
| MIG-F3 | Mid-chain migration raises | Per-step `apply` raises | Restore from backup; emit `MIGRATION_STEP_FAILED` with offending step name |
| MIG-F4 | Pre-condition fails (input not loadable against from-version) | Pydantic load before chain start | Surface; suggest `git log -- state.json` to find a clean state |
| MIG-F5 | Post-condition fails (output not loadable against to-version) | Pydantic load after chain end | Restore from backup; emit `MIGRATION_POSTCONDITION_FAILED` |

### 5.6 Telemetry contract (V7)

#### 5.6.1 Default state

`telemetry.enabled = false` ships in the built-in config layer (per V7 [1:219]). No telemetry projection runs, no DuckDB file is created, no event-jsonl scan happens.

When the operator runs `eawf metrics show` on a `telemetry.enabled=false` config, the verb emits a one-time onboarding nudge:

```
Telemetry is disabled. Eä can project per-session metrics from your event.jsonl
and per-runtime session logs into a user-scope DuckDB at <local-path>

Nothing ever leaves your machine. Eä does not ship telemetry over any network;
local-file export (--format prom|json|csv --out <path>) is the only data path
out of DuckDB. Subsequent transport off the machine is your decision.

Enable now? [y/N]
  y      → eawf config set telemetry.enabled true && eawf metrics rebuild
  N      → continue with telemetry off; suppress this prompt with
           `eawf config set telemetry.opt_in_dismissed true`
```

Per D7 + D21 + Q6 (2026-05-17 strict-local override).

#### 5.6.2 What's collected (when opt-in)

Reference: C09 §5.9.2 schema [9:490-618]. Operator-facing summary:

| Category | Rows | Source |
|---|---|---|
| Sessions | One per dispatched wave attempt OR interactive CLI session | Per-runtime session log (Claude / Codex / OpenCode) + per-repo `event.jsonl` `dispatch_cost` envelopes |
| Turns | One per LLM turn within a session | Per-runtime session log |
| Tool calls | One per tool invocation within a session | Per-runtime session log |
| Compactions | One per session compaction event | Per-runtime session log |
| Runtime switches | One per V5 fallback | Per-repo `event.jsonl` `runtime_switched` envelopes |
| Incidents | One per typed Incident emission | Per-repo `event.jsonl` `incident_*` envelopes |

**All seven tables stay user-local.** The DuckDB file at `<local-path>` never leaves the machine. Per Q6 (2026-05-17 strict-local-only) there is no network-shipping verb; the only data path out of DuckDB is local-file export.

#### 5.6.3 Where it goes (local-only)

Per Q6 ratification (2026-05-17 strict-local-only). Two local-file transports, no network:

1. **`eawf metrics export --format prom --out <path>`** — write to a local file. The Prometheus textfile-collector path (e.g. `<local-path>`) is the canonical operator-scrape path. No network.
2. **`eawf metrics export --format json --out <path>`** — local JSON dump for archive / external aggregator ingestion. CSV also supported (`--format csv`).

**Hard non-negotiable (strengthened by Q6).** There is no `telemetry.export.endpoint` config key. There is no HTTPS POST surface. There is no Anthropic-hosted upload. The data path off the DuckDB is local file only; subsequent transport off the machine is operator-orchestrated outside eawf (e.g. `rsync` to a private metrics host the operator runs). Pre-export scrubber MUST still run on local-file export to avoid leaking user paths into shared `.prom` files (per §5.6.4); scrubber regex default + presidio opt-in via `eawf[telemetry-scrub]` extra per C09 §7.3 Q3-W05 [9:1259].

#### 5.6.4 Privacy + scrubber

Reference C09 §5.9.7 scrubber [9:1320-ish]. Operator-facing rules:

- **Always scrubbed.** Absolute home-anchored paths (`<local-path>`, `<local-path>`, `<local-path>`). Replaced with `<USER_HOME>/...` placeholder.
- **Default-scrubbed.** Repo-relative paths inside session log JSONL — preserved (operator's own repo paths are not PII).
- **Optionally scrubbed.** When `--scrubber presidio` is passed, the `en_core_web_*` spaCy model adds NER-based PII detection (names, emails, IPs). Adds ~250 MB install footprint; gated on the `eawf[telemetry-scrub]` extra.

#### 5.6.5 Disable / pause

```
eawf config set telemetry.enabled false       # halt projection; existing DB preserved
eawf metrics rebuild --drop                   # halt + drop the projection DB
```

### 5.7 CHANGELOG auto-gen

`CHANGELOG.md` follows Keep a Changelog 1.1.0 [31] as already established in v0.2.0. v0.3 adds auto-generation from per-phase commit prefixes.

#### 5.7.1 Generation algorithm

```python
# src/eawf/cli/commands/release.py — changelog handler (sketch)
def release_changelog(
    since_last_stable: bool = False,
    until: str | None = None,
    write_to_file: bool = False,
) -> None:
    """Render a CHANGELOG.md block for the given range.

    Args:
        since_last_stable: range starts at the most recent `vX.Y.Z` tag (no suffix).
        until: explicit end tag; defaults to HEAD.
        write_to_file: prepend the rendered block to CHANGELOG.md under the [Unreleased] header.

    Raises:
        UserError: if no commits are found in the range.
    """
    range_start = last_stable_tag() if since_last_stable else last_alpha_tag()
    commits = run_git_log(range_start, until or "HEAD", grep=r"^\[P\d{2}")
    phases = group_by_phase(commits)
    block = render_changelog_block(phases)
    if write_to_file:
        prepend_to_changelog(block)
    else:
        print(block)
```

Each phase block follows the v0.2.0 shape:

```markdown
### Added
- Phase N: <one-line phase summary derived from `phase open` / `phase close` envelopes> —
  `<W##>` <commit subject>, `<W##>` <commit subject>, ...

### Changed
- <bulleted list of [type:refactor] commits>

### Fixed
- <bulleted list of [type:fix] commits>

### Known limitations (rolled forward to v0.X+1)
- <bulleted list extracted from phase-close envelopes' `body.known_limitations`>
```

#### 5.7.2 Manual edit policy

The auto-generated block is a *draft*. Operator edits it before tagging the stable release:

1. `uv run eawf release changelog --since-last-stable > <local-path>`
2. Operator reviews + edits.
3. Operator prepends to `CHANGELOG.md`.
4. Operator tags `git tag v0.X.0 && git push --tags`.
5. Release CI consumes the tagged `CHANGELOG.md` for the PyPI release notes.

`--commit` mode (`eawf release changelog --commit`) auto-prepends + commits with prefix `[P<NN>-CORE] docs: changelog for v0.X.0`.

### 5.8 License + attribution

#### 5.8.1 MIT — primary

`LICENSE` (top-level, MIT) is the canonical license. Unchanged from v0.1. Listed in `pyproject.toml [project.license]`.

#### 5.8.2 Runtime adapter attribution

Per V9 [1:283-288], native plugin trees include per-runtime attribution. Each `build/<runtime>-plugin/README.md` carries:

```markdown
# eawf — <runtime> plugin tree

This tree contains the canonical Eä Workflow plugin bundle for <runtime>.
Sync'd from AGENTS.md + the shared skill registry by `eawf plugin sync`.

## Attribution

This plugin bundle integrates with <runtime> via its native plugin shape.
Trademarks and tool semantics are property of their respective owners:

- Claude Code: Anthropic (https://github.com/anthropics/claude-code)
- Codex CLI:   OpenAI    (https://github.com/openai/codex)
- OpenCode:    SST       (https://github.com/sst/opencode)

Eä does not redistribute the runtime binaries. This tree only contains
plugin manifests, hook scripts, and skill / command definitions sync'd
from the canonical eawf source.
```

#### 5.8.3 Telemetry / upstream-prototype attribution

Per V7 [1:222-223] and C09 [9:Audit-Source-pinning section]: the `src/eawf/telemetry/_VENDOR_PROVENANCE.txt` file pins the audit-source revision and `src/eawf/telemetry/__init__.py` carries:

```python
"""Eä telemetry subsystem.

Schema + projection algorithm vendored from the upstream telemetry
prototype, at the revision pinned in `_VENDOR_PROVENANCE.txt`.
See C09 §5.9 for the audit trail.

Re-licensing under MIT (the eawf license). No third-party code is
redistributed; only the schema shape + algorithm logic was vendored
and rewritten in Pydantic v2.
"""
```

### 5.9 Docs IA

#### 5.9.1 Information-architecture tree

`docs/` is migrated to mkdocs structure (D9):

```
docs/
├── index.md                                # landing page; 5-minute quickstart link + project tagline
├── getting-started/
│   ├── install.md                          # pip / brew / docker / source
│   ├── quickstart.md                       # 5-minute path; ends with first wave dispatched
│   ├── concepts.md                         # phase / iter / wave / scope / spec / envelope glossary
│   └── troubleshooting.md                  # common first-run failures
├── tutorials/                              # per-profile (V3 / D13)
│   ├── research-profile.md                 # /research → spike → hypothesis → audit path
│   ├── engineering-profile.md              # /init → propose → execute → ship path
│   ├── reverse-engineering-profile.md      # symbol-naming + hypothesis-driven decompile path
│   ├── spike-profile.md                    # short-lived spike with promote-on-graduation
│   ├── hybrid-profile.md                   # research + engineering co-active path
│   └── casts/                              # asciinema casts per tutorial
│       ├── research-profile.cast
│       ├── engineering-profile.cast
│       ├── reverse-engineering-profile.cast
│       ├── spike-profile.cast
│       └── hybrid-profile.cast
├── how-to/                                 # task-oriented recipes
│   ├── add-a-phase.md
│   ├── claim-a-wave.md
│   ├── recover-from-daemon-crash.md
│   ├── recover-from-interrupted-wave.md
│   ├── migrate-state-schema.md
│   ├── enable-telemetry.md
│   ├── enable-daemon-auto-start.md
│   └── upgrade-eawf.md
├── reference/                              # auto-generated (D9)
│   ├── cli.md                              # auto via mkdocs-typer from src/eawf/cli/app.py
│   ├── skills.md                           # auto via custom mkdocs-skills plugin
│   ├── schemas.md                          # auto via custom mkdocs-pydantic plugin
│   ├── error-codes.md                      # auto from src/eawf/cli/errors.py + incident-cause enum
│   ├── exit-codes.md                       # auto from src/eawf/cli/exit_codes.py
│   ├── enums.md                            # auto from src/eawf/state/enums.py
│   ├── hook-events.md                      # auto from src/eawf/store/kinds/events/
│   ├── urn-namespace.md                    # auto from src/eawf/urn.py
│   ├── config-keys.md                      # auto from src/eawf/config/registry.py (C08 §5.x)
│   └── stability-tiers.md                  # auto from src/eawf/cli/stability.py
├── architecture/                           # one deep-dive per C00 cluster
│   ├── foundations.md                      # C01
│   ├── daemon-topology.md                  # C02
│   ├── spec-infrastructure.md              # C03
│   ├── workflow-skills.md                  # C04
│   ├── cli-surface.md                      # C05 narrative
│   ├── operator-surface.md                 # C06
│   ├── subsystems.md                       # C07a + C07b
│   ├── config-profiles.md                  # C08
│   ├── quality-observability.md            # C09
│   └── operations.md                       # C10 (this brief, polished for end-operator)
├── adr/                                    # ADR registry
│   ├── README.md                           # ADR index with date + status
│   ├── 0001-CLI-is-dispatch.md             # AGENTS rule 1
│   ├── 0002-state-CLI-only-mutator.md      # AGENTS rule 4
│   ├── 0003-eawfd-daemon-day-1.md          # V1
│   ├── 0004-cluster-sequential-batching.md # V4
│   ├── 0005-runtime-fallback-reactive.md   # V5
│   ├── 0006-per-os-native-service.md       # V6
│   ├── 0007-telemetry-vendor-prototype.md  # V7
│   ├── 0008-hybrid-session-reuse.md        # V8
│   ├── 0009-native-per-runtime-plugins.md  # V9
│   └── 0010-...                            # additional decisions accumulated through v0.3 → v0.5
├── policy/                                 # invariants
│   ├── agents-claude-md.md                 # already in v0.1; refreshed for AGENTS.md as canonical
│   ├── no-plan-md.md                       # already in v0.1; restated
│   ├── fixed-decisions.md                  # already in v0.1; refreshed per cluster verdicts
│   ├── deletion-rule.md                    # AGENTS rule 6
│   ├── secrets-pii-hygiene.md              # AGENTS rule 16 + Eä secrets policy
│   └── error-ux-template.md                # NEW; reference §5.13
└── glossary.md                             # alphabetical glossary of all canonical terms (C01 vocab)
```

#### 5.9.2 mkdocs.yml shape

```yaml
site_name: Eä Workflow
site_url: https://elementarno9.github.io/eawf/
site_description: Agent-driven development framework — Eä manifest at runtime.
repo_url: https://github.com/Elementarno9/eawf
edit_uri: edit/main/docs/

theme:
  name: material
  palette:
    primary: custom         # Wong-orange #E69F00 per C07b [12:748]
    accent: custom
  font: {text: Inter, code: JetBrains Mono}
  features:
    - navigation.tabs
    - navigation.sections
    - navigation.indexes
    - content.code.copy
    - content.code.annotate
    - search.suggest
    - search.share
    - toc.integrate

extra_css:
  - assets/wong-orange.css

plugins:
  - search
  - mkdocs-typer:            # auto CLI ref
      command: eawf.cli.app:main
      output_path: reference/cli.md
  - mkdocs-skills:           # custom; reads skill registry
      output_path: reference/skills.md
  - mkdocs-pydantic:         # custom; reads State + EventPayload + Spec models
      modules:
        - eawf.state.models
        - eawf.store.envelope
        - eawf.specs.models
      output_path: reference/schemas.md

markdown_extensions:
  - admonition
  - attr_list
  - def_list
  - pymdownx.details
  - pymdownx.highlight:
      anchor_linenums: true
  - pymdownx.snippets:
      base_path: ['.', 'src/eawf/_data/docs']
  - pymdownx.superfences
  - pymdownx.tabbed:
      alternate_style: true
  - tables
  - toc:
      permalink: true
```

#### 5.9.3 Auto-generation invariants

Three custom plugins (`mkdocs-skills`, `mkdocs-pydantic`, plus mkdocs-typer pinned upstream) are the *only* sources of truth for their respective reference pages. The plugin's `on_files` hook runs at every mkdocs build; the generated `reference/<x>.md` is committed to the repo so PR diffs surface drift.

The pre-commit hook `eawf doc verify --strict` (already promoted in C09 [9:1235]) runs the mkdocs build + diff against `docs/` to catch unintended changes.

### 5.10 Per-profile tutorials (V3)

Per D13 (markdown + asciinema cast), one tutorial per profile bundle. Five tutorials = five casts.

Each tutorial follows the same scaffold:

```markdown
# Tutorial: <Profile> profile

**Estimated time:** <NN> minutes
**Outcome:** by the end of this tutorial, you will have <one-sentence-outcome>.
**Profile bundle:** `profiles: [<profile-id>]` (composed with `core`)
**Prerequisites:** Python 3.14+, `eawf >= 0.3.0`.

## 0. Setup

[Asciinema cast embed]
{{ asciinema "casts/<profile-id>.cast" }}

## 1. Initialise an Eä workspace
[command + expected output]

## 2. <profile-specific step 1>
...

## 3. <profile-specific step N>
...

## Recovery paths
[what to do if step K fails]

## Where next
- [How to add a phase](../how-to/add-a-phase.md)
- [Architecture deep-dive](../architecture/<relevant-cluster>.md)
- [<Profile>-specific reference](../reference/<x>.md)
```

#### 5.10.1 Per-profile tutorial outcomes (one-sentence)

| Profile | Outcome | Estimated EU |
|---|---|---|
| `research` | Author one research brief + one hypothesis + one decision with full evidence-chain | 0.5 EU |
| `engineering` | Open one phase + plan two waves + dispatch one wave + cherry-pick one wave + ship PR | 1.0 EU |
| `reverse-engineering` | Open one phase + run one hypothesis-driven decompile + record one verdict against a binary symbol | 0.75 EU |
| `spike` | Author one spike brief + record one verdict + (optionally) graduate to a wave | 0.25 EU |
| `hybrid` (research + engineering) | Author one research brief, derive one phase from it, dispatch the first wave, audit-replay the evidence chain | 1.0 EU |

Total tutorial-authoring budget: ~3.5 EU for the markdown + ~5.0 EU for the casts (per-cast determinism setup is the bulk of the cost).

### 5.11 Doc generation toolchain

#### 5.11.1 mkdocs plugin inventory

Per D9. Three custom + four upstream:

| Plugin | Status | Source |
|---|---|---|
| `mkdocs-material` | upstream pinned `>=9.5,<10` | `pip install mkdocs-material` |
| `mkdocs-typer` | upstream pinned `>=0.0.3,<0.1` | `pip install mkdocs-typer` |
| `mkdocs-skills` | **custom** — new in v0.3 | `src/eawf/docs/_mkdocs_plugins/skills.py` |
| `mkdocs-pydantic` | **custom** — new in v0.3 | `src/eawf/docs/_mkdocs_plugins/pydantic.py` |
| `mkdocs-enums` | **custom** — new in v0.3 | `src/eawf/docs/_mkdocs_plugins/enums.py` |
| `pymdownx.snippets` | upstream pinned `>=10,<11` | `pip install pymdown-extensions` |
| `mkdocs-include-markdown-plugin` | upstream pinned `>=6,<7` | `pip install mkdocs-include-markdown-plugin` |

Total mkdocs dev-dep footprint: ~80 MB. Ship under `eawf[docs]` extra so non-doc operators skip the install.

#### 5.11.2 Custom plugin contract

Each custom plugin implements the mkdocs `BasePlugin` protocol:

```python
# src/eawf/docs/_mkdocs_plugins/skills.py
from __future__ import annotations
from pathlib import Path
from mkdocs.plugins import BasePlugin
from mkdocs.config.config_options import Type

from eawf.skills.registry import iter_skills
from eawf.docs.render import render_skills_reference

class SkillsRefPlugin(BasePlugin):
    config_scheme = (
        ("output_path", Type(str, default="reference/skills.md")),
    )
    def on_files(self, files, config, **kwargs):
        out = Path(config["docs_dir"]) / self.config["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_skills_reference(list(iter_skills())))
        return files
```

`render_skills_reference` is a typed Jinja2 renderer over the skill catalog [5:206]. Same shape for `mkdocs-pydantic` (walks Pydantic models, emits field tables) and `mkdocs-enums` (walks `StrEnum` subclasses, emits enum-value tables).

#### 5.11.3 Doc build pipeline

```bash
uv sync --extra docs               # install mkdocs-material + custom plugins
uv run mkdocs build                # generates site/ tree
uv run mkdocs serve                # hot-reload at <local-url>
uv run eawf doc verify --strict    # walks site/ for broken cross-refs + drift
```

The release CI `release.yaml` adds a `docs-publish` job after `publish-pypi`:

```yaml
docs-publish:
  needs: publish-pypi
  if: ${{ !contains(github.ref, 'a') && !contains(github.ref, 'b') }}   # stable + rc only
  runs-on: ubuntu-24.04
  steps:
    - uses: actions/checkout@v4
    - uses: astral-sh/setup-uv@v8.1.0
    - run: uv sync --frozen --extra docs
    - run: uv run mkdocs gh-deploy --force
```

### 5.12 Onboarding flows per persona

Each flow has: (i) **entry point** (the verb / file / event that triggers it), (ii) **happy path** steps, (iii) **failure fallback** path, (iv) **deep-dive doc anchor**.

#### 5.12.1 operator-new-user

**Entry point.** `pip install eawf && eawf --version` (or `brew install eawf` on macOS / Linux).

**Happy path:**

1. `pip install eawf` (or `brew install Elementarno9/homebrew-eawf/eawf`).
2. `eawf --version` confirms install.
3. `eawf doctor` checks: Python version, uv presence, git presence, repo currency (no stale `state.json`).
4. `eawf completion install --shell zsh` (or `bash` / `fish`) — shell completion.
5. Operator reads `docs/getting-started/quickstart.md` (link printed by `eawf --help` footer).
6. **Optional.** `eawf daemon enable` if operator wants auto-start at login (V6 [1:162]).

**Failure fallback.** If `eawf doctor` reports any failure: surface fix command via the `ErrorUXTemplate` next-step field (e.g. `Python 3.14 not found. Install: brew install python@3.14. See: docs/getting-started/install.md`).

**Deep-dive.** `docs/getting-started/install.md`.

#### 5.12.2 operator-new-repo

**Entry point.** `cd <repo> && eawf init`.

**Happy path:**

1. `cd <existing-or-new-repo>`.
2. `eawf init` — interactive `questionary` wizard per C04 §5.4.11 [5:879-908]:
   - Asks for `state_path` (default `.ea/state.json`).
   - Asks for `project_code` (auto-suggests from repo name).
   - Asks for `project_title`.
   - Asks for `lifecycle_depth` (`single-phase` / `multi-phase`).
   - Asks for `profiles` (multi-select from the five bootstrap templates per D7 in C08 [8:139-145]).
   - Asks for `runtime` (`claude` / `codex` / `opencode` / `all`).
3. Wizard writes: `.ea/state.json`, `.ea/config.yaml`, `.ea/profile.yaml`, `AGENTS.md` managed regions, per-runtime plugin tree.
4. Onboarding nudge for telemetry (D21a).
5. Operator runs `eawf doctor` to verify the bootstrap.
6. Operator reads `docs/tutorials/<profile-id>.md` for the next steps.

**Failure fallback.** Wizard rejects on `INIT-F1..INIT-F4` per [5:899-906]; each rejection surfaces a `next_step` via `ErrorUXTemplate`.

**Deep-dive.** `docs/how-to/add-a-phase.md` + per-profile tutorial.

#### 5.12.3 operator-new-project-type

**Entry point.** Operator decides to switch profiles mid-repo (e.g. add `spike` to an `engineering`-only repo).

**Happy path:**

1. `eawf profile list` — shows current bundle.
2. `eawf profile add spike` — appends `spike` to `profiles.enabled`; runs conflict check per V3 [1:78]; on conflict, surfaces the declared conflict + asks operator to declare `profile_priority`.
3. `eawf sync` — regenerates AGENTS.md managed regions + per-runtime plugin tree from the updated profile composition.
4. Operator reads the new profile's tutorial.

**Failure fallback.** On conflict, `ErrorUXTemplate` next-step: `Run: eawf profile inspect <profile-id> # to see what overrides`.

**Deep-dive.** `docs/architecture/config-profiles.md`.

#### 5.12.4 agent-new-repo

**Entry point.** Dispatch envelope (C04 [5:dispatch section]) when the daemon spawns a subagent into a worktree that hasn't been touched.

**Happy path:**

1. Daemon spawns subprocess `claude -p` (or Codex equivalent per V8 [1:226-271]) in the worktree.
2. Subagent reads `AGENTS.md` (worktree-local; sync'd by `/init`).
3. Subagent reads `state.json` digest (provided in dispatch envelope, not by fresh re-read).
4. Subagent executes per wave spec (C03 [4]).
5. Subagent emits typed `agent_end` report (AGENTS rule 19).

**Failure fallback.** On dispatch failure (V5 reactive switchover [1:127-151]), daemon swaps to the next runtime; subagent's `state.json` digest is re-emitted in the fallback envelope.

**Deep-dive.** `docs/architecture/workflow-skills.md` + `docs/architecture/daemon-topology.md`.

#### 5.12.5 daemon-first-spawn

**Entry point.** First `eawf <mutating-verb>` after install (on-demand spawn per V1 [1:24-53]).

**Happy path:**

1. CLI verifies daemon presence at `<local-path>`.
2. If absent: CLI spawns daemon via `python -m eawf.daemon.run --background --idle-timeout 300`.
3. CLI connects via JSON-RPC; emits the mutation envelope.
4. First-spawn user-facing note: `daemon started (pid <N>, idle-timeout 300s). For auto-start at login: eawf daemon enable.` Suppressible via `eawf config set onboarding.suppress_daemon_first_spawn_note true`.

**Failure fallback.** On spawn failure (Python missing, virtualenv broken): CLI falls back to daemonless read-only mode for read verbs; refuses mutating verbs with `DAEMON_SPAWN_FAILED` per `ErrorUXTemplate`.

**Deep-dive.** `docs/architecture/daemon-topology.md`.

#### 5.12.6 telemetry-opt-in

**Entry point.** Either `eawf init` first-run nudge (D21a) or `eawf metrics show` invocation with telemetry disabled (D21b).

**Happy path:**

1. Operator sees the opt-in prompt (§5.6.1).
2. Operator chooses `y`.
3. `eawf config set telemetry.enabled true` — writes to the user-scope config layer.
4. `eawf metrics rebuild` — runs initial full projection from canonical sources.
5. Operator reads `docs/how-to/enable-telemetry.md` for advanced configuration.

**Failure fallback.** If DuckDB install fails (per C09 F1 [9:1200]): auto-fall-back to SQLite; surface warning + doc link.

**Deep-dive.** `docs/how-to/enable-telemetry.md`.

### 5.13 Error UX template

Per D17. Typed Pydantic model in `src/eawf/errors/ux.py`:

```python
# src/eawf/errors/ux.py
from __future__ import annotations
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ErrorCode = Literal[
    # Schema / state
    "STATE_VALIDATION_FAILED", "STATE_VERSION_MISMATCH", "BACKUP_WRITE_FAILED",
    "MIGRATION_STEP_FAILED", "MIGRATION_POSTCONDITION_FAILED", "MIGRATION_TARGET_UNKNOWN",
    # Daemon / IPC
    "DAEMON_PROTOCOL_MAJOR_SKEW", "DAEMON_PROTOCOL_MINOR_SKEW", "DAEMON_SPAWN_FAILED",
    "DAEMON_LOCK_HELD", "DAEMON_SOCKET_UNREACHABLE",
    # Scope / lifecycle
    "SCOPE_CONFLICT", "WAVE_DEPS_NOT_SATISFIED", "PHASE_NOT_ACTIVE", "ITER_NOT_ACTIVE",
    "WAVE_OUT_OF_ORDER_REJECTED",
    # Worktree / git
    "WORKTREE_DIRTY", "WORKTREE_BRANCH_STALE", "CHERRY_PICK_CONFLICT",
    # Runtime / dispatch
    "RUNTIME_AUTH_EXPIRED", "RUNTIME_RATE_LIMIT", "RUNTIME_SERVER_ERROR",
    "DISPATCH_BUDGET_EXCEEDED", "SESSION_LOG_MISSING",
    # Plugin / sync
    "PLUGIN_MANIFEST_INVALID", "PLUGIN_DRIFT_DETECTED",
    # Config / profile
    "PROFILE_CONFLICT_UNDECLARED", "CONFIG_LAYER_NOT_WRITABLE", "CONFIG_FIELD_UNKNOWN",
    # User input
    "INVALID_INPUT", "MISSING_REQUIRED_ANSWER",
    # External
    "EXTERNAL_API_FAILURE",
    # Fallback
    "UNKNOWN",
]

class ErrorUXTemplate(BaseModel):
    """Operator-facing error contract.

    Every CLI / TUI / skill error envelope MUST render through this template.
    The rendered string follows: `<cause>. <next_step>. (See: <see>)`.
    """
    model_config = ConfigDict(extra="forbid")
    error_code: ErrorCode
    cause: str = Field(min_length=1, max_length=200, description="One-line cause.")
    next_step: str = Field(min_length=1, max_length=200, description="Suggested operator action.")
    see: str | None = Field(default=None, max_length=200,
                             description="Doc anchor, repo-relative or eawf:// URN.")
    exit_code: int = Field(ge=1, le=10, description="Per C05 exit-code taxonomy [6:467-525].")
    raw: dict | None = Field(default=None, description="Structured raw payload for --json consumers.")

    def render_human(self) -> str:
        base = f"{self.cause}. {self.next_step}."
        if self.see:
            return f"{base} (See: {self.see})"
        return base

    def render_json(self) -> dict:
        return self.model_dump(exclude_none=True, mode="json")
```

Every error site adopts the template:

```python
# Before:
raise ValueError(f"wave {wave_id!r} has unsatisfied deps: {deps}")

# After:
raise UserError(ErrorUXTemplate(
    error_code="WAVE_DEPS_NOT_SATISFIED",
    cause=f"wave {wave_id!r} has unsatisfied deps: {sorted(deps)}",
    next_step=f"close the blocking wave(s) first: eawf wave close {next(iter(deps))!r}",
    see="docs/how-to/claim-a-wave.md#deps",
    exit_code=2,
))
```

`UserError` is a typed `Exception` subclass carrying the `ErrorUXTemplate` payload; the CLI error-handler at `src/eawf/cli/app.py` renders human or JSON based on `--json`.

#### 5.13.1 Doc anchor mapping

Every `error_code` maps to exactly one anchor in `docs/reference/error-codes.md`. The reference page is auto-generated from the `ErrorCode` enum + the call-site catalog (grep for `ErrorUXTemplate(error_code=...)` across `src/eawf/`). The generator (run as part of `eawf doc verify --strict`) refuses to merge when an error code has no doc anchor.

### 5.14 Progress indicators + resumability

#### 5.14.1 Progress indicator surface

CLI verbs that take >1s emit progress via `rich.progress` per the existing pattern. Per D12, streaming output (`--stream`) replaces the static progress bar with NDJSON envelopes for machine consumers.

Long-running verbs:

| Verb | Typical duration | Progress shape | Resumable? |
|---|---|---|---|
| `wave dispatch` | 30s — 30min (per V5 [17:128]) | Live wave-board tile in TUI; NDJSON in CLI | Yes (D22) |
| `flow execute` | 5min — 60min | Multi-wave NDJSON stream | Yes (D22) |
| `eawf migrate` | 1s — 60s | Per-step `rich.progress` bar | Yes (D22) — checkpoint after each step |
| `eawf metrics rebuild --full` | 5s — 5min | Per-source `rich.progress` bar | No — partial rebuild is incremental anyway |
| `eawf plugin sync` | 1s — 10s | Single spinner | No — atomic operation |
| `eawf doctor` | 1s — 5s | None | No |

#### 5.14.2 Resumability

Per D22. Three verbs are resumable:

**`eawf wave dispatch --resume <token>`** — token = `state.waves[<wave_id>].dispatch_checkpoint_token`. Daemon-side state tracks per-(wave, attempt) checkpoint envelopes; resume picks up at the last checkpoint.

**`eawf flow execute --resume <token>`** — token = `state.flow.execute.checkpoint_token`. Resumes the phase pipeline (research → prep → audit → ship) at the last completed step.

**`eawf migrate --resume <from-version>`** — when a multi-step chain failed mid-way; resume from the last committed step. Backup files are how the chain reconstructs the start point.

#### 5.14.3 Checkpoint envelope

```python
# src/eawf/store/kinds/events/checkpoint.py
from __future__ import annotations
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

class CheckpointPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: Literal["checkpoint"] = "checkpoint"
    verb: str                             # 'wave_dispatch', 'flow_execute', 'migrate'
    scope_id: str                         # URN
    token: str                            # opaque; UUID-v4
    step: str                             # verb-specific step name
    progress_pct: float                   # 0..100
    ts: datetime
```

Daemon writes one envelope per checkpoint to `.ea/store/event.jsonl`; resume reads from the tail.

### 5.15 Backup + restore

Per D18. Two distinct surfaces:

#### 5.15.1 Migration backups (auto)

Already covered in §5.5.3. `state.json.bak.v<from>.<to>` adjacent to `state.json`.

#### 5.15.2 Snapshot backups (manual)

```
eawf backup create [--note <str>]               # writes <local-path>
eawf backup list [--scope <urn>]                 # lists all snapshot backups
eawf backup restore --ts <ISO8601>               # restores state.json to the snapshot
eawf backup prune --keep <N>                     # keeps the N most recent; deletes older
```

Snapshot backup payload:

```
<local-path>
├── 2026-05-17T12-00-00Z/
│   ├── state.json
│   ├── config.yaml
│   ├── profile.yaml
│   └── note.txt          # operator-supplied note
└── 2026-05-15T09-00-00Z/
    └── ...
```

`repo_sha = sha256(repo-absolute-path)[:12]` per C02 [3:174-175].

`eawf backup restore` writes a pre-restore backup of the current state automatically, so restore is itself reversible:

```
<local-path>
└── pre-restore-state.json.2026-05-17T12-30-00Z
```

#### 5.15.3 Restore semantics

`eawf backup restore` only restores `.ea/state.json` + `.ea/config.yaml` + `.ea/profile.yaml`. It does **NOT** restore: `.ea/store/*.jsonl` (append-only event log; restoring loses history), `state.json.bak.*` (migration backups; covered by their own surface), `.ea/local/` (gitignored scratch).

If the restored state's `schema_version` is older than the currently-installed `eawf`, the operator must run `eawf migrate` after restore.

### 5.16 Remote-dev surface

Eä works in three remote-dev shapes:

#### 5.16.1 SSH session

Operator SSHs into a remote machine, runs Eä there. Daemon spawns under the SSH user's account; service-file install (V6) registers in the SSH user's home, not the system. No special surface; works out of the box.

**Caveat.** If multiple SSH sessions for the same user run Eä concurrently, all four touch the same daemon (per V1 single-user invariant [1:179]). The daemon arbitrates; correct behaviour.

#### 5.16.2 Container session

Operator runs Eä inside a container (devcontainer, GitHub Codespace, Coder). Daemon spawns inside the container. State + config live on the bind-mounted volume; daemon socket lives at `<local-path>` inside the container.

**Caveat.** If the bind mount is shared with the host AND the host also runs Eä, both daemons compete for `state.json` locks. Mitigation: `eawf doctor` flags concurrent-daemon detection (two PIDs on the same `state.json` parent dir). Per C02 [3:lock section].

#### 5.16.3 IDE remote (VS Code Remote, JetBrains Gateway)

Operator runs Eä via IDE-managed remote. Same as SSH session topology; no special surface.

**Caveat.** The IDE's terminal may not be a TTY (depends on IDE). Verbs that emit progress fall back to non-TTY mode (no `rich.progress` animation; plain stdout). Per C05 D12 [6:115].

### 5.17 EU calibration model

Per D10. Empirical calibration from P09..P15 wave timestamps [16:24-39].

#### 5.17.1 EU definition

```
1 EU = 30 minutes of active operator + agent session time.
```

Wall-clock elapsed time, attention-time, and agent-runtime time are separate concepts (per [16:16]).

#### 5.17.2 Wave bucket model

Per archive brief [16:104-110]:

| Bucket | Default EU | Range intuition | Use when |
|---|---:|---:|---|
| XS | 0.25 | 0.1-0.25 | Single-file or enum/test/doc patch |
| S | 0.5 | 0.25-0.5 | Small schema/helper/CLI patch with tests |
| M | 1.0 | 0.5-1.0 | Renderer, migration shim, or multi-file helper |
| L | 2.0 | 1.0-2.0 | CLI transaction + integration tests + goldens |
| XL | 3.5 | 2.0-3.5 | Full-suite debug loop, runtime adapter, live smoke |

#### 5.17.3 Iter + phase EU roll-up rule

```
estimated_iter_eu = ceil_to_0.25(sum(default_eu_for_each_wave_bucket))
estimated_phase_eu = ceil_to_0.25(sum(estimated_iter_eu))
```

No bucket on iter or phase. The calculated EU number renders alongside the bucket-sum + the critical-path EU when the DAG exists (per [16:91-96]).

#### 5.17.4 Recalibration cadence

Run weekly via `eawf metrics export --window 90d --format json`. The export includes:

- `bucket_observations.<bucket>.median_active_minutes`
- `bucket_observations.<bucket>.p90_active_minutes`
- `bucket_observations.<bucket>.sample_size`

When the median for any bucket diverges from the default by >25%, the operator-facing nudge fires: `Bucket <bucket> calibration is stale (default: <D> EU, observed median: <M> EU). Run: eawf calibrate buckets`. `eawf calibrate buckets` re-issues a decision envelope `[P##-CORE] state: ...` with the new defaults.

Per V7 [1:200] (statistics over time + 7d/30d/90d burn rate) — the same telemetry projection feeds both burn-rate and bucket calibration.

### 5.18 Budget enforcement rules

Per D11. Soft default + hard opt-in.

#### 5.18.1 Budget hierarchy

```
phase_budget   = estimated_phase_eu × multiplier   # default multiplier 1.5 (50% safety)
iter_budget    = estimated_iter_eu × multiplier
wave_budget    = bucket_default_eu × multiplier
```

`multiplier` is configurable per scope via `flow.budget.multiplier`:

```
<local-path>    flow.budget.multiplier: 2.0   # operator preference
<repo>/.ea/config.yaml: flow.budget.multiplier: 1.5   # repo override
<repo>/.ea/state.json:  state.waves[<wave_id>].budget_eu: 2.5  # per-wave explicit
```

#### 5.18.2 Enforcement modes

| Mode | Behaviour |
|---|---|
| `soft` (default) | Wave continues past budget; daemon emits `BudgetExceededWarning` event; TUI banner shows red runtime indicator |
| `hard` | Daemon halts the wave at budget; SIGTERM → SIGKILL ladder per [17:120-124]; emits `dispatch_halted` event |

Setting:

```
eawf config set flow.budget.enforce soft   # default
eawf config set flow.budget.enforce hard   # opt-in
```

Per-wave override at dispatch time:

```
eawf wave dispatch <wave_id> --budget-enforce hard --budget-cap 1.5
```

#### 5.18.3 Token / $ tracking

Per C09 §5.9 metrics catalog [9:865-898]. Operator-facing tiles:

- **M01 — `eawf_tokens_total`** — per-direction × per-runtime × per-scope.
- **M02 — `eawf_cost_usd_total`** — per-runtime × per-scope × per-model; Decimal-quantised; uses §5.9.6.1 PRICING snapshot per C09 [9:870-1010].
- **M08/M09/M10 — `eawf_burn_rate_<N>d_usd`** — rolling 7d / 30d / 90d.
- **M26 — `eawf_estimate_actual_variance_pct`** — feeds C06 VarianceTile [7].

The PRICING dict is the *counterfactual* USD cost; the operator's actual billing depends on whether they're on subscription (no marginal $) or BYOK (real API charges). The metric still surfaces the counterfactual so the operator can reason about migration to BYOK if subscription quota becomes the bottleneck (per [17:117-119]).

#### 5.18.4 Alert thresholds

Default thresholds per scope:

| Scope | Soft alert at | Hard cap |
|---|---|---|
| Wave | 100% of `wave_budget` | 200% (config override) |
| Iter | 100% of `iter_budget` | 200% |
| Phase | 100% of `phase_budget` | 150% |

Hard cap is only enforced when `flow.budget.enforce=hard`. Soft alert is always emitted via `BudgetExceededWarning` event regardless of enforce mode.

### 5.19 Variance reporting + weekly burn

Per D19. Same telemetry projection feeds all three surfaces (TUI tile, CLI deep-dive, ship-gate output).

#### 5.19.1 TUI VarianceTile

C06 [7] metrics overlay shows a 3×2 tile grid; one tile is the VarianceTile:

```
┌── Variance ─────────────────┐
│  P20 actual: 4.2 EU         │
│  P20 planned: 3.5 EU        │
│  Δ: +20% (warn at +50%)     │
│  Trend (7d): ▁▂▃▅▇          │
└─────────────────────────────┘
```

#### 5.19.2 CLI deep-dive

```
eawf metrics variance [--scope <urn>] [--window 7d|30d|90d]
                       [--format table|json|csv]
```

Output:

```
Scope: repo/eawf (window: 30d)

Phase | Planned EU | Actual EU | Δ %   | Status
P18   |  4.50      |  4.20     | -6.7% | under
P19   |  5.50      |  6.10     | +10.9%| over (within budget)
P20   |  3.50      |  4.20     | +20.0%| over (within budget)

Iter (last 10):
P20-I01 | 1.00 | 0.85 | -15%
P20-I02 | 1.50 | 1.70 | +13%
P20-I03 | 1.00 | 1.65 | +65%   ← variance > 50% threshold; review
```

#### 5.19.3 Ship-gate output

The phase-close audit's PR body includes a `## Variance` section auto-rendered by `eawf release notes`:

```markdown
## Variance

| Iter   | Planned | Actual | Δ %    |
|--------|---------|--------|--------|
| P20-I01| 1.00    | 0.85   | -15%   |
| P20-I02| 1.50    | 1.70   | +13%   |
| P20-I03| 1.00    | 1.65   | +65%   |

Phase total: planned 3.50, actual 4.20 (+20%).

Calibration note: P20-I03 hotfix exceeded the >50% threshold; the hotfix
wave was an L-bucket (2.0 EU) but ran to 1.65 EU due to verification surface.
```

## 6. Failure modes + named edge cases

| F# | Failure | Surface | Detection | Recovery |
|---|---|---|---|---|
| F1 | Wheel build excludes `eawf/_data/` | PyPI release | `eawf daemon enable` raises `FileNotFoundError` on template | Pre-release CI gate: `python -c "from eawf._data import service_templates; assert (service_templates.path / 'linux-systemd-eawfd.service.j2').exists()"` |
| F2 | (vacated — brew not shipped per Q4-refr 2026-05-17) | — | — | — |
| F3 | Service-file install on unsupported OS | `eawf daemon enable` on a non-Linux/macOS/Windows OS (FreeBSD, etc.) | `platform.system()` returns unknown | Emit `UNSUPPORTED_OS` error code; next-step: `Run daemon manually: python -m eawf.daemon.run --foreground` |
| F4 | Migration backup write fails | `eawf migrate` | `OSError` from `write_text` | Refuse to migrate; emit `BACKUP_WRITE_FAILED`; next-step suggests `df -h .ea/` to check disk |
| F5 | Migration chain partial-success | `eawf migrate` | Per-step `apply` raises | Restore from backup; emit `MIGRATION_STEP_FAILED`; next-step suggests `eawf migrate --to <last-good-version>` |
| F6 | Telemetry projection rebuild OOM | `eawf metrics rebuild --full` on large corpus | Python `MemoryError` | Switch to `--incremental`; emit warning; document the medium/large fixture budgets per C09 [9:813] |
| F7 | Telemetry local-file export accidentally leaks PII | Operator runs `eawf metrics export --format prom --out <shared-path>`; payload contains user path | Pre-export scrubber MUST run on every `metrics export` invocation | Scrubber refuses to write when any field still matches a macOS, Linux or Windows user-home path root; emits `EXPORT_SCRUB_REFUSED` per C09 [9:1210] |
| F8 | Doc-gen plugin fails on stale skill registry | `mkdocs build` | Plugin raises `ImportError` | `eawf doc verify --strict` catches in pre-commit; surface line + next-step `Run: uv sync --extra docs` |
| F9 | (vacated — brew not shipped per Q4-refr 2026-05-17) | — | — | — |
| F10 | `eawf daemon enable` writes to system-wide path | Operator runs `sudo eawf daemon enable` | sudo-elevated; writes `/etc/systemd/system/` instead of `<local-path>` | Refuse to install when EUID == 0; emit `SYSTEM_WIDE_INSTALL_REFUSED`; next-step: `Run as user: eawf daemon enable` (V6 [1:179]) |
| F11 | `eawf migrate` writes backup but no migrations exist | Edge case: `from_version == to_version` | Migration chain is empty | Emit `no-op` status; skip backup write; surface `Already at v<X>` |
| F12 | PyPI publish fails mid-release | Network blip during `gh-action-pypi-publish` | Action exits non-zero | Re-run the failed job; PyPI is idempotent on file hash (refuses duplicate upload) |
| F13 | CHANGELOG auto-gen finds no commits | `eawf release changelog` between two adjacent stable tags | Range is empty | Surface `no commits in range`; exit 0 (not an error) |
| F14 | Per-profile tutorial cast goes stale | `mkdocs build` references missing cast | mkdocs warns; doesn't fail build | `eawf doc verify --strict` greps `casts/*.cast`; refuses build when any tutorial references a missing cast |
| F15 | Asciinema cast contains absolute paths | Cast was recorded under `<local-path>` | Path leaks in published doc | Recording protocol: `cd $(mktemp -d) && git clone ... && asciinema rec`. CI gate `path-leak-lint` (C09 hook 16) walks `.cast` files. |
| F16 | Variance tile renders zero rows | Brand-new repo without P##-close events | Tile reads from `telemetry_sessions` joined to `state.phases`; both empty | Render `(no data yet)` placeholder; suppress the tile from the 3×2 grid until ≥1 phase closes |
| F17 | EU bucket calibration drifts past 25% but operator ignores | Weekly metrics export | Nudge fires; operator dismisses | Nudge auto-dismisses for 7 days; re-fires until operator either calibrates or sets `flow.budget.bucket_calibration_locked=true` |
| F18 | Multi-step migration interrupted by Ctrl-C | Operator presses Ctrl-C mid-migration | SIGINT handler catches | Atomic per-step writes; backup is the recovery anchor; emit `MIGRATION_INTERRUPTED`; next-step: `Run: eawf migrate --resume <token>` |
| F19 | (vacated — brew not shipped per Q4-refr 2026-05-17) | — | — | — |
| F20 | `eawf backup restore` overwrites uncommitted state | Operator restored without checking `git status` | Atomic write replaces `state.json` | Pre-restore backup mitigates (§5.15.2); next-step in restore prompt: `Run: git status # before restore` |
| F21 | Quickstart cast records a real PyPI install (network-dependent) | Recording on a flaky network | Cast pauses on network wait | Recording protocol: `pip install --offline --no-index --find-links file://...` from a frozen wheel cache; cast plays back deterministically |
| F22 | (vacated — Docker image not built per Q4-refr 2026-05-17) | — | — | — |
| F23 | `eawf daemon enable` race with running daemon | Operator runs enable while daemon is up | `eawf daemon status` reports running | Refuse to overwrite running service file; next-step: `eawf daemon stop && eawf daemon enable` |
| F24 | Service-file template substitution leaves placeholder | Bug in `render_template` | Generated file contains `{{ venv_python }}` literal | Template-render-time validation: `if "{{" in rendered: raise UserError(...)` |
| F25 | Per-OS launcher-script path normalisation breaks on Windows | `\` vs `/` mismatch in `service_install_path` | `pywin32` Service install errors | `Path(...)` normalisation; CI integration test on Windows runner |
| F26 | Telemetry projection schema mismatch survives upgrade | Operator upgrades eawf past a telemetry schema bump but ignores the DuckDB schema-version check | Per C09 F4 [9:1203] — auto-drop + recreate | Surface warning; auto-rebuild from canonical sources |
| F27 | Onboarding nudge fires inside a CI agent | `eawf metrics show` in a CI script with `telemetry.enabled=false` | Nudge prompts on a non-TTY | TTY guard: `if not sys.stdout.isatty(): return without prompting` |

## 7. Migration plan

C10 implementation lands across four phases. Phase numbers below are placeholders (`P-O##`) until the v0.3 → v0.5 roadmap assigns concrete `P<NN>` IDs in `/prep`.

### 7.1 Phase O1 — Versioning + release CI (PyPI only)

Surface: `pyproject.toml`, `src/eawf/_version.py`, `.github/workflows/release.yaml`.

Waves:

- **O1-W01** — `src/eawf/_version.py` shape per §5.1.1; `tools/version_bump.py` writes the generated file from a git tag. M bucket.
- **O1-W02** — `.github/workflows/release.yaml` with two jobs (`build-wheel`, `publish-pypi`). PyPI trusted-publisher OIDC. No brew, no Docker per Q3 + Q4-refr (2026-05-17). M bucket.
- **O1-W03** — Version-skew matrix surface in `eawf doctor`; per §5.1.2 detection points. S bucket.

Iter total: 2.5 EU (M + M + S).

### 7.2 Phase O2 — Service-file packaging + daemon enable / disable

Surface: `src/eawf/_data/service_templates/`, `src/eawf/cli/commands/daemon.py` (extend), `tools/bundle_data.py`.

Waves:

- **O2-W01** — `tools/bundle_data.py` extends Hatchling build hook to copy `templates/service/` + `build/` + `templates/init/` + `docs/build/` into `src/eawf/_data/` at wheel-build time. M bucket.
- **O2-W02** — Per-OS service templates (Linux systemd-user, macOS launchd, Windows pywin32). Each ~80-150 LOC. M bucket.
- **O2-W03** — `eawf daemon enable` + `eawf daemon disable` implementations per §5.4. Per-OS registration logic. L bucket (touches three OS surfaces).
- **O2-W04** — Per-OS CI integration test (V6) — installs + starts + stops + uninstalls the service on each runner. M bucket.

Iter total: 6.0 EU (M + M + L + M).

### 7.3 Phase O3 — Migration tooling + backup / restore + error UX

Surface: `src/eawf/migrations/`, `src/eawf/cli/commands/migrate.py`, `src/eawf/cli/commands/backup.py`, `src/eawf/errors/ux.py`.

Waves:

- **O3-W01** — `Migration` Protocol + chain-builder + `eawf migrate` CLI per §5.5.2. M bucket.
- **O3-W02** — `v1_0_to_v1_1.py` + `v1_1_to_v1_2.py` migration scripts (per C01 prereq bundle + C03 Spec entity addition). L bucket (touches many state fields).
- **O3-W03** — `eawf backup create` + `eawf backup list` + `eawf backup restore` + `eawf backup prune` per §5.15.2. M bucket.
- **O3-W04** — `ErrorUXTemplate` model + `UserError` + `StateConflict` etc. typed exception hierarchy; CLI error-handler renders human or JSON; full error-code catalog. M bucket.
- **O3-W05** — `docs/reference/error-codes.md` auto-generator + `eawf doc verify --strict` gate per §5.13.1. S bucket.
- **O3-W06** — Refactor all error sites to `ErrorUXTemplate`. ~150 sites grepped; touches ~30 files. L bucket.

Iter total: 9.0 EU (M + L + M + M + S + L).

### 7.4 Phase O4 — Docs IA + per-profile tutorials + onboarding flows

Surface: `docs/` (full migration), `mkdocs.yml`, `src/eawf/docs/_mkdocs_plugins/`, `docs/tutorials/casts/`, `docs/how-to/`.

Waves:

- **O4-W01** — `mkdocs.yml` + theme + `eawf[docs]` install extra; baseline `index.md` + `getting-started/install.md` + `getting-started/quickstart.md`. M bucket.
- **O4-W02** — Three custom mkdocs plugins (`mkdocs-skills`, `mkdocs-pydantic`, `mkdocs-enums`) per §5.11.2. L bucket.
- **O4-W03** — Migrate v0.1 docs from `docs/architecture/`, `docs/policy/`, `docs/reference/` into the new IA per §5.9.1. M bucket.
- **O4-W04** — ADR registry seeding (ADRs 0001..0010 per §5.9.1 list). S bucket.
- **O4-W05** — Per-profile tutorials (5 markdown files + 5 asciinema casts) per §5.10. L bucket (~5 EU per cast + markdown across the five profiles).
- **O4-W06** — Onboarding flows (six personas per §5.12) — markdown how-to anchors + nudge implementations. M bucket.
- **O4-W07** — `eawf metrics variance` CLI + ship-gate output per §5.19; tie into existing `eawf release notes`. M bucket.
- **O4-W08** — Quickstart asciinema cast (`docs/casts/quickstart.cast`) per §5.9.1 D12. S bucket.
- **O4-W09** — `docs-publish` job in `release.yaml` + gh-pages bootstrap. S bucket.

Iter total: 13.5 EU (M + L + M + S + L + M + M + S + S).

### 7.5 Phase O5 — Telemetry contract + EU calibration + budget enforcement

Surface: `src/eawf/cli/commands/metrics.py` (extend), `src/eawf/cli/commands/calibrate.py` (new), `src/eawf/cli/commands/config.py` (telemetry keys).

Waves:

- **O5-W01** — Telemetry opt-in nudge per §5.6.1; one-time at `eawf init` + per-`eawf metrics show` when disabled. S bucket.
- **O5-W02** — `telemetry.export.scrubber` config key; `eawf metrics export --format prom|json|csv --out <local-path>` local-file path per §5.6.3 (no `telemetry.export.endpoint` per Q6 2026-05-17 strict-local). S bucket.
- **O5-W03** — `eawf calibrate buckets` verb + nudge logic per §5.17.4. M bucket.
- **O5-W04** — `flow.budget.enforce` + `flow.budget.multiplier` config keys; hard-enforce SIGTERM/SIGKILL ladder per §5.18.2. M bucket.
- **O5-W05** — Variance tile rendering in C06 metrics overlay (consumes existing telemetry projection). S bucket.

Iter total: 4.5 EU (S + M + M + M + S).

### 7.6 Compat shims

None required mid-rollout. Each phase ships behind a flag-free additive surface. The error-UX refactor (O3-W06) is the largest mechanical change; old callers continue to work because `UserError` is a `RuntimeError` subclass that renders the same human-readable output even when the `ErrorUXTemplate` body is absent (transitional shim removed in v0.4).

### 7.7 Rollback

| Phase | Rollback shape |
|---|---|
| O1 (versioning + release CI) | Revert release CI commit; PyPI does not support deletion but `yank` + new release suffices |
| O2 (service-file) | Revert `_data/service_templates/` + revert `daemon enable / disable`; on-demand spawn (V1) still works |
| O3 (migration tooling) | Revert `eawf migrate` + migration scripts; manual migration via SQL-style scripts as v0.2 mitigation |
| O4 (docs IA) | Revert `mkdocs.yml`; restore `docs/` from v0.2 layout; v0.2 doc surface preserved by gh-pages history |
| O5 (telemetry contract) | Revert telemetry opt-in nudges; existing C09 telemetry implementation continues unaffected (V7 projection rebuildable; Q6 strict-local invariant means no network surface to rollback) |

## 8. Open questions for operator

`AskUserQuestion` seeds for the C10 ratification round. **All 12 ratified 2026-05-17 via 3-round AskUserQuestion blitz.** Each answer recorded below; 2 override deltas (Q3 + Q4-refr PyPI-permanent; Q6 strict-local-no-network) noted in §10 Provenance. Each carries pre-drafted options with `(Recommended)` markers per the operator's `[[feedback_approval_via_askuserquestion]]` convention.

### Ratified verdict table

| # | Axis | Answer (2026-05-17) | Match recommendation? |
|---|---|---|---|
| Q1 | Versioning scheme (D1) | (b) semver+PEP-440 hybrid | Yes |
| Q2 | Alpha cadence (D2) | (a) per-phase merge | Yes |
| Q3 | Packaging matrix (D5) | (a) PyPI only | **No — override vs (c)** |
| Q4-refr | Brew + Docker deferral | (c) Never — PyPI permanent | **No — override vs revisit-at-v0.4** |
| Q5 | Service-file channel (D6) | (a) bundled in PyPI wheel | Yes |
| Q6 | Telemetry shipping (D8) | **strict-local-only; no `telemetry.export.endpoint` surface exists** | **No — stricter than (b) recommendation** |
| Q7 | Doc toolchain (D9) | (a) mkdocs + mkdocs-material | Yes |
| Q8 | Budget enforcement (D11) | (c) soft default + hard opt-in | Yes |
| Q9 | EU calibration source (D10) | (b) empirical from P09..P15 | Yes |
| Q10 | Stable cadence (D4) | (b) per validated MINOR | Yes |
| Q11 | Tutorial format (D13) | (b) markdown + asciinema cast | Yes |
| Q12 | Error UX template (D17) | (c) typed `ErrorUXTemplate` + `error_code` | Yes |

### Original seed text (preserved for traceability)

### Q1 — Versioning scheme

> **Q1.** Lock the versioning scheme (D1).

- **(a)** PEP-440 only (`0.3.0a1`)
- **(b)** semver + PEP-440 suffix hybrid (`0.3.0a1` package; `0.3.0-alpha.1+phase.P20` build-metadata) **(Recommended)**
- **(c)** Calver (`2026.05.0`)

### Q2 — Alpha cadence

> **Q2.** Pick the alpha-release cadence (D2).

- **(a)** Per-phase merge **(Recommended)**
- **(b)** Per-iter
- **(c)** Per cluster-batch close

### Q3 — Packaging matrix

> **Q3.** Which targets ship in v0.3 (D5)?

- **(a)** PyPI only
- **(b)** PyPI + brew
- **(c)** PyPI + brew + Docker (CI/bench use only) **(Recommended)**
- **(d)** (c) + PyInstaller standalone

### Q4 — Brew distribution

> **Q4.** Brew formula publication path (D14).

- **(a)** Manual PR to homebrew-core
- **(b)** Custom tap at `homebrew-eawf/eawf` (operator runs `brew tap`)
- **(c)** Custom tap, auto-pushed by release CI **(Recommended)**

### Q5 — Service-file distribution

> **Q5.** Service-file distribution channel (D6).

- **(a)** Bundled inside PyPI wheel under `eawf/_data/service_templates/` **(Recommended)**
- **(b)** Separate installer (brew post-install hook only)
- **(c)** Separate `eawf-service` package

### Q6 — Telemetry data shipping default

> **Q6.** Telemetry network-shipping default (D8).

- **(a)** HTTPS POST to operator-configured endpoint (opt-in on top of opt-in)
- **(b)** No-network-by-default; HTTPS POST opt-in via `telemetry.export.endpoint` **(Recommended)** — locked by V7 [1:220]
- **(c)** Auto-upload to Anthropic-hosted endpoint

### Q7 — Doc generation toolchain

> **Q7.** Doc toolchain pick (D9).

- **(a)** mkdocs + mkdocs-material **(Recommended)**
- **(b)** sphinx + furo
- **(c)** plain markdown
- **(d)** docusaurus

### Q8 — Budget enforcement default

> **Q8.** Budget enforcement default mode (D11).

- **(a)** Soft (warning + continue)
- **(b)** Hard (fail wave at cap)
- **(c)** Soft default + hard opt-in per scope via `flow.budget.enforce=hard` **(Recommended)** — matches [17:127-128] soft-only invariant for v0.3

### Q9 — EU calibration source

> **Q9.** EU calibration model source (D10).

- **(a)** Theoretical (manual per-task estimation)
- **(b)** Empirical from P09..P15 archive [16] **(Recommended)**
- **(c)** Hybrid (theoretical bucket + empirical multiplier per profile)

### Q10 — Stable cadence

> **Q10.** Stable-release trigger (D4).

- **(a)** Per quarter (calendar)
- **(b)** Per validated MINOR (feature-bound); MAJOR triggers breaking-change **(Recommended)**
- **(c)** Per breaking-change MAJOR only

### Q11 — Tutorial format

> **Q11.** Per-profile tutorial format (D13).

- **(a)** Markdown only
- **(b)** Markdown + asciinema cast **(Recommended)**
- **(c)** Full Jupyter-style notebook

### Q12 — Error UX template

> **Q12.** Error UX template format (D17).

- **(a)** Free-form per-error string
- **(b)** Typed `ErrorUXTemplate(cause, next_step, see)`
- **(c)** (b) + machine-readable `error_code` **(Recommended)**

## 9. References

[1] `.ea/local/research/long-term/2026-05-16-c00-spec-index.md` — C00 spec index; verdicts V1, V3, V4, V6, V7, V9 cited throughout.
[2] `.ea/local/research/long-term/2026-05-16-c01-foundations.md` — C01 foundations; URN scheme, entity catalog, persona authority matrix.
[3] `.ea/local/research/long-term/2026-05-16-c02-daemon-topology.md` — C02 daemon spine; §5.10 per-OS service-file content [3:493-650].
[4] `.ea/local/research/long-term/2026-05-16-c03-spec-infrastructure.md` — C03 spec infra; mockup-required WaveSpec validator referenced by tutorials.
[5] `.ea/local/research/long-term/2026-05-16-c04-workflow-skills.md` — C04 workflow skills; /init detail [5:879-908] drives operator-new-repo onboarding.
[6] `.ea/local/research/long-term/2026-05-16-c05-cli-surface.md` — C05 CLI surface; stability tiers [6:808-884], release verbs [6:347-359], exit codes [6:467-525].
[7] `.ea/local/research/long-term/2026-05-17-c06-operator-surface.md` — C06 operator surface; metrics overlay tile catalog referenced for VarianceTile.
[8] `.ea/local/research/long-term/2026-05-16-c08-configurability-profiles.md` — C08 config + profiles; bootstrap templates [8:554-690] drive per-profile tutorials.
[9] `.ea/local/research/long-term/2026-05-17-c09-quality-observability.md` — C09 quality + observability; telemetry subsystem §5.9 [9:462-1035], PRICING dict §5.9.6.1 [9:899-1008], incident-cause taxonomy [9:1036-1100].
[10] `pyproject.toml` — current packaging (v0.2.0).
[11] `.ea/local/research/long-term/2026-05-16-c07a-runtime-skill-dispatch.md` — C07a runtime + skill dispatch; plugin manifest schema [11:310-378].
[12] `.ea/local/research/long-term/2026-05-16-c07b-vcs-worktree-events.md` — C07b VCS + worktree + events; branding [12:552-622].
[13] `CHANGELOG.md` — current changelog; reference format for auto-gen.
[14] `docs/README.md` — current docs IA baseline.
[15] `docs/architecture/installation.md` — current install policy reference.
[16] `.ea/local/research/archive/2026-05-13-eu-estimation-calibration.md` — EU calibration archive; bucket model + P09..P15 timestamps.
[17] `.ea/local/research/long-term/2026-05-15-long-term-roadmap-synthesis.md` — synthesis brief; budget broker §"Budget broker" [17:117-129] + quota recovery §"Quota recovery" [17:131-134].
[18] `AGENTS.md` — non-negotiable rules; rule 14 commit-prefix, rule 16 secrets/PII hygiene, rule 18 artifact chassis, rule 21 roadmap procedure.
[19] `.github/workflows/ci.yaml` — current CI workflow baseline (referenced for release.yaml shape).
[20] `.pre-commit-config.yaml` — current pre-commit baseline (referenced for hooks 16-19 from C09 §5.3).
[21] `https://keepachangelog.com/en/1.1.0/` — Keep a Changelog format (already adopted in CHANGELOG.md).
[22] `https://semver.org/spec/v2.0.0.html` — semver 2.0 (already cited in CHANGELOG.md).
[23] `https://peps.python.org/pep-0440/` — PEP 440 version specifiers (PyPI / pip resolver compatibility).
[24] `https://squidfunk.github.io/mkdocs-material/` — mkdocs-material upstream docs (D9).
[25] `https://github.com/bruce-szalwinski/mkdocs-typer` — mkdocs-typer plugin (D9 auto-CLI-ref).
[26] `https://asciinema.org/` — asciinema format (D12 + D13 quickstart + tutorial casts).
[27] `telemetry-prototype source` — the operator's upstream telemetry prototype, the vendored schema source per V7 [1:187]; attribution in `src/eawf/telemetry/__init__.py`.
[28] `https://github.com/anthropics/claude-code` — Claude Code runtime (attribution per V9 [1:283]).
[29] `https://github.com/openai/codex` — Codex CLI runtime (attribution).
[30] `https://github.com/sst/opencode` — OpenCode runtime (attribution).

[31] `https://keepachangelog.com/en/1.1.0/` — Keep a Changelog 1.1.0, the format `CHANGELOG.md` has followed since v0.2.0.

## 10. Provenance

- `store_record=none (local-only research draft)`
- `commit=3b86f7a (parent; revisions 2026-05-18)`
- `supersedes=none`
- `session=eawf-spec-cluster-c10-2026-05-17`
- `derived_from=C00 verdicts V1+V3+V4+V6+V7+V9 + C01..C09 accepted briefs + archive EU calibration [16] + synthesis budget/quota sections [17]`
- `last_revised=2026-05-18 (audit-driven: migrations route through daemon per Q1 supersede / Codex C10-I002; PyPI-only confirmed — brew/Docker/PyInstaller all rejected; telemetry strict-local locked per Q6; single version source PEP 440 per Codex C10-I006; migration backups under ignored dir per Codex C10-I007; restore includes event log per Codex C10-I010; init wizard ops route through daemon canonical writer per Codex C10-I009)`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md (5 followups; 12 Codex issues)`
- `authority_binding=Q1 (2026-05-18): eawf migrate routes mutations through daemon (sole writer); legacy direct-state-write sketch in original brief invalidated. Migration commits emit through daemon's outcome-WAL.`
- `operator_verdicts_locked=Q1..Q12 ratified 2026-05-17 via 3-round AskUserQuestion blitz; 2 override deltas: (Q3 + Q4-refr) PyPI-permanent — drops brew, Docker, and PyInstaller across v0.3 → v1.0; (Q6) strict-local-no-network — drops telemetry.export.endpoint HTTPS POST surface entirely, stricter than V7 [1:219-220] opt-in default. All other 9 answers match the in-brief recommendation.`

## 11. Scrub

- status: clean
- references: repo-relative paths, external URLs, and Eawf URNs only
- local absolute paths: none
- hostnames: none (`<local-path>`, `<local-path>`, `<local-path>`, `%APPDATA%\...` are generic home-anchored or env-var-anchored)
- real emails: none (canonical author block in `pyproject.toml` excluded per AGENTS rule 16)
- credentials / API keys / tokens: none
- internal URLs: none (`elementarno9.github.io/eawf/` is the public docs domain; `ghcr.io/elementarno9/eawf` is the public registry)
- abstract placeholder names: not applicable (no mockup repos in this brief)
