# C03 — Spec Infrastructure — Eä framework long-term specs

**Cluster:** C03 (Spec Infrastructure — Phase/Iter/Wave/Research/Hypothesis/Decision/Audit specs + CLI + audit-DSL kind)
**Title:** Spec Infrastructure
**Status:** `local-draft`, `accepted` (operator ratified Q1..Q14 on 2026-05-16 via /blitz; see §10 Provenance for the override deltas)
**Created:** `2026-05-16T00:00:00Z`
**Author:** `claude-opus-4-7`
**Depends on:** C00 [1] (V1, V2, V3 load-bearing), C01 [2] (Spec entity + URN + lifecycle + Wave/Research/Hypothesis/Decision/Audit catalog), C02 [3] (daemon-mediated writes + spec-cache reservation)
**Consumed by:** C04 (skill envelopes emit specs), C05 (CLI `eawf {phase,iter,wave} spec` surface), C06 (TUI render of specs), C09 (verify-implements audit + spec-graduation test coverage)

## 1. Purpose + scope statement

C03 makes V2 [1:55-74] implementable. C01 [2:624-664] reserved the **Spec** entity (URN kind, filesystem-only storage at `.ea/specs/<phase>/[<iter>/]<wave|spec>.md`, DRAFT → READY → IMPLEMENTED → ARCHIVED lifecycle, daemon-cached index, no `State.specs` row); C03 now locks every per-tier Pydantic schema, the CLI verb table, the audit-DSL extension, the migration plan, and the mockup-required validator that together drive specs from "convention" to "wave-claimable, ship-gated, audit-replayable contract".

The trigger is RC-1 from the P20 direction brief [4:83-87]: P20 shipped because wave success criteria were written as scaffold-shape ("quadrant 2x2 / offline + online tick modes") rather than outcome-shape with cited verdicts, named failure modes, and mockup-vs-implementation parity. The new contract — every wave cites at least one verdict, names at least one failure mode, points at real test paths, and (for UI scopes) draws a mockup — is enforced at three independent layers: Pydantic load (`init` / `validate`), pre-commit hook (real-test-paths grep), and ship-gate audit (`verify-implements` walks closed-wave specs and greps changed files for verdict markers). Any one of the three catches the RC-1 class of failure [4:307-314].

**In scope (C00 §C03 [1:430-479]).**

- **PhaseSpec** Pydantic schema — outcome statement, KPIs, success/failure modes, depends-on, EU envelope, ship criteria, profile constraints.
- **IterSpec** Pydantic schema — sub-goal, ordering rationale, wave grouping rationale, audit cadence, profile constraints.
- **WaveSpec** Pydantic schema — `implements:` verdict citations, file scopes, behaviors B1..Bn, failure modes, tests, optional mockup, dispatch metadata (agent role + effort bucket).
- **ResearchBriefSpec** — extension of the existing eawf-template chassis with typed `implements:` + `consumed_by:` fields.
- **HypothesisSpec** — extends current `Hypothesis` row [10:244-256] with `evidence_chain: list[EvidenceRef]`.
- **DecisionSpec** — extends current `Decision` row [10:294-305] with explicit `supersedes:` chain (today's `superseded_by` [10:304] points one way; the spec carries the bi-directional chain).
- **AuditSpec** — typed audit document extending today's `Audit` row [10:259-271] with declarative check-spec list, including the new `verify-implements` kind.
- **Audit-DSL kind `verify-implements`** — walks closed-scope specs, greps changed files for verdict-id markers, fails when a wave's `implements:` set is not represented in the diff under the wave's `file_scopes:`.
- **CLI surface** — `eawf {phase,iter,wave} spec {init,validate,render,implements,promote}` plus three meta verbs (`eawf spec show`, `eawf spec lint`, `eawf spec graduate`).
- **Migration plan** — `Wave.success_criteria` retained for legacy waves but deprecated; new waves authored as WaveSpec. Backfill writer hydrates one-shot specs from existing criteria text for closed waves.
- **Mockup-required validator** — heuristic firing when any `file_scopes:` entry lives under `src/eawf/tui_v2/` or `src/eawf/render/`.

**Out of scope (per C00 [1:446-449]).**

- TUI rendering of specs (overlays, edit modal, plan preview). → **C06**.
- Skill that *generates* specs from a prompt (`/spec`, `/wave-spec` skill body). → **C04**.
- Visual diff of spec versions (side-by-side render across phase reopens). → **C09**.
- Per-runtime adapter coupling (subprocess argv shape, session-handle paths). → **C07**.
- Skill-registry profile gating. → **C08**.

**Non-goals (C03-specific).**

- **NG1 — Removing `Wave.success_criteria`.** Schema migration in v0.4+. C03 ships parallel rails: new waves write WaveSpec; closed waves keep `success_criteria` until a one-shot writer backfills (§7).
- **NG2 — Replacing AGENTS.md non-negotiables with PhaseSpec front-matter.** AGENTS.md remains canonical contract per manifesto Rule 4 [11:50-54]. PhaseSpec cites; never replaces.
- **NG3 — `spec_path` field on Phase/Iter/Wave models.** Per C01 D3 [2:120,624-638] the URN is derivable from the entity ID; no state field is added.
- **NG4 — In-state Spec row.** No `State.specs: dict[str, Spec]`; the spec body is filesystem-only and graduated via `git rm` on phase close per C01 D3.

## 2. Goals + non-goals

### Goals

| G# | Goal | Source |
|---|---|---|
| G1 | Every wave dispatched after C03 implementation phase carries a WaveSpec with at least one verdict citation and at least one named failure mode, validated at `eawf wave claim` time. | C00 §C03 [1:432-444], P20-DIR §RC-1 [4:83-87] |
| G2 | Every phase opened after C03 carries a PhaseSpec with outcome statement + KPI(s) + ship criteria, validated at `eawf phase activate` time. | C00 V2 [1:60-62], P20-DIR §Critical Contracts [4:584-585] |
| G3 | Every iter opened after C03 carries an IterSpec with sub-goal + ordering rationale + audit cadence, validated at `eawf phase activate` (downstream activation also re-validates). | C00 V2 [1:60-62] |
| G4 | A new `verify-implements` audit-DSL check kind walks closed-scope specs and greps the diff for verdict-id markers; missing marker fails the gate. | C00 §C03 [1:441-444], P20-DIR §Anti-error mechanisms [4:288-294] |
| G5 | UI-scope waves (file scope under `src/eawf/tui_v2/` or `src/eawf/render/`) MUST carry a non-null `mockup:` field — otherwise `eawf wave spec validate` fails. | C00 §C03 [1:444-445], P20-DIR [4:291] |
| G6 | Closed-scope specs are recoverable via `eawf spec show <urn> --from-git` even after the file is `git rm`-ed at ARCHIVED transition. | C01 §5.4.15 [2:1125-1151], C02 [3:77] |
| G7 | Migration from today's `Wave.success_criteria: list[str]` to WaveSpec is forward-only and idempotent: closed waves stay readable; new waves go through WaveSpec; the one-shot writer is a *generator*, not a *normaliser*. | C00 §C03 [1:443-444] |
| G8 | Brief is self-contained — quotes V1..V3 inline, cites all source-tree file:line refs, ratifiable in one fresh CC session. | C00 V4 [1:99-125] |

### Non-goals

| NG# | Non-goal | Why deferred |
|---|---|---|
| NG1 | Spec render in the TUI (overlay, edit modal, plan preview). | C06 owns it [1:587-644]. C03 ships markdown render only. |
| NG2 | `/spec` skill body (prompt-driven scaffold). | C04 owns the skill envelope contract [1:485-534]. C03 ships the validator + scaffolder libraries the skill calls. |
| NG3 | Side-by-side diff of spec versions across phase reopen. | C09 visualisation / observability scope [1:769-841]. C03 ships only `eawf spec render --diff <other-id>` text output. |
| NG4 | Schema bump beyond `schema_version: 1`. | Migrations after the first bump are C09 territory (Alembic-style runners). C03 reserves `Literal[1]` and lays the fail-fast loader. |
| NG5 | Profile-driven spec template variation. | C08 owns profile composition [1:715-763]. C03 makes the validator profile-aware (accepts a `profile_bundle: list[str]` parameter) but the per-profile *defaults* live in C08. |
| NG6 | Roadmap-render of spec graphs (dep DAG visualisation). | C06 (TUI) + C04 (`/roadmap` skill) territory. C03 emits machine-readable `--json` only. |

## 3. Prior verdicts cited

Three C00 verdicts and two C01 decisions are load-bearing.

### V1 — eawfd daemon Day-1 + smart-spawn writer [1:24-53]

> "Mutations to `state.json` (and all future stateful surfaces — config layers, registry, event log) route through the eawfd daemon."

**C03 binding.** Spec body lives on disk, not in `state.json`, so the V1 "daemon arbitrates state mutations" rule narrows to two specific Spec-related writes that *do* mutate state: (a) the IMPLEMENTED transition is recorded on the parent Wave/Iter/Phase row via `eawf {wave,iter,phase} close`, which already routes through the state CLI per AGENTS rule 4 [11]; (b) the ARCHIVED transition is performed by the daemon as part of `phase close` finalisation — daemon does the `git rm` and writes the spec-cache row. Direct file-write to `.ea/specs/` from agents during the DRAFT phase is **permitted** (it is filesystem-resident content, not state-resident metadata), but `eawf spec validate` is the gate that a draft must pass before the parent scope transition is allowed.

### V2 — Three-tier specs: Phase + Iter + Wave [1:55-74]

> "PhaseSpec — phase charter: outcome statement, KPIs, success/failure modes, dependencies on prior phases, EU envelope, ship criteria. IterSpec — iter intent: sub-goal within phase, ordering rationale, wave grouping rationale, audit cadence. WaveSpec — wave deliverable: verdict citations, file scopes, behaviors, failure modes, tests, mockup (UI scopes only)."

**C03 binding.** The three-tier shape is the spine of §5. Each tier is its own Pydantic v2 model. Cross-tier validation (PhaseSpec.iters == sum-of-IterSpec.waves == count of WaveSpecs under the phase) lives in `eawf spec lint` (§5.6). C03 §4 D2 records the IterSpec necessity defense: IterSpec is **not** just bookkeeping; it carries (a) ordering rationale that the wave DAG cannot express on its own (why I02 happens after I01 in narrative terms), (b) audit cadence (which audit kinds run on iter-close vs phase-close), and (c) the wave grouping rationale that the prep activation gate uses to verify the operator is dispatching a coherent set of waves.

### V3 — Composable profile bundle with declared precedence [1:76-96]

> "Project carries `profiles: [research, engineering, reverse-engineering, spike, ...]` ordered list ... Effective ruleset = union of profile contributions, conflict-resolved by precedence."

**C03 binding.** Spec validators are profile-conditional. Examples:

- `engineering` profile contributes the "tests must reference real paths" pre-commit hook + a "mockup required for UI scopes" validator with the heuristic frozen.
- `research` profile *relaxes* the mockup requirement (research-profile waves emit briefs, not UI), and contributes the "every wave cites at least one source artifact id" validator instead.
- `spike` profile contributes a "WaveSpec.behaviors may be empty if `kind: spike`" relaxation — spike waves graduate to a brief, not a code deliverable.
- `reverse-engineering` profile contributes the "WaveSpec must reference a symbol id" validator (so decompiled-symbol waves cite their target).

Per V3 [1:80-86] each profile's contributions are declared and conflicts must be explicit. C03 ships the *validator registry* (a dict keyed on `(profile_id, spec_kind)` mapping to a callable); C08 ships the profile manifest schema that *populates* the registry.

### C01 D3 — Spec storage shape [2:120]

> "Filesystem-only, URN-derivable, archived on phase close. Spec lives at `.ea/specs/<phase>/[<iter>/]<wave|spec>.md` per V2. URN `urn:eawf:v1:spec:<repo>/<phase>[/<iter>[/<wave>]]` is derivable from the entity id; no `spec_path` field on Phase/Iter/Wave."

**C03 binding.** No `State.specs` dict. The spec's filesystem path is computed from the entity's id at every read; the daemon optionally caches a per-phase `{spec_urn -> (file_sha, status, last_modified)}` index at `<local-path>` so `eawf spec show <urn>` after ARCHIVED doesn't need a manual `git log`. C02 [3:77] reserves the cache path but defers its implementation to C03 — owned by §5.7 below.

### C01 D8 — Per-entity bespoke spec lifecycle [2:125]

> "Spec uses DRAFT/READY/IMPLEMENTED/ARCHIVED per V2 and operator §4 D3."

**C03 binding.** The lifecycle DAG is C01 §5.4.15 [2:1125-1151]. C03 §5.5 records every transition gate (predicate + writer) so each enum value's invariants are testable.

## 4. Decision matrix

The 11 axes named in C00 §C03 "Key axes to lock" [1:453-464], each row carrying the operator-ratification options the brief proposes for §8 AUQ.

| # | Axis | Options considered | Recommendation | Rationale |
|---|---|---|---|---|
| **D1** | PhaseSpec field count | (a) **minimum** — outcome + KPIs + ship_criteria only; (b) **maximum** — also EU envelope, depends_on, profile_constraints, deferred_to, supersedes; (c) **medium** | **(c) medium** — outcome + KPIs + ship criteria + EU envelope (`expected_eu_total`, optional `pessimistic_eu_total`) + depends_on + profile_constraints | Minimum makes PhaseSpec a glorified title bar — operator can't ratify "is this phase ready to activate?" without ship criteria, EU envelope, and profile constraints. Maximum bloats the authoring cost in a phase where many fields are unknown until iters are planned. Medium covers ratification surface; everything beyond is optional. |
| **D2** | IterSpec necessity defense | (a) deprecate IterSpec, fold into PhaseSpec; (b) keep IterSpec for ordering + audit cadence only; (c) **keep IterSpec as full first-class** carrying sub-goal + ordering rationale + wave grouping rationale + audit cadence + profile constraints | **(c) keep full** | V2 [1:60-62] says three-tier. IterSpec carries information that lives nowhere else: (a) prose "why this iter happens after the previous one" — a wave DAG cannot encode narrative ordering, (b) audit cadence ("V07 audit kind runs on every iter-close, not just phase-close"), (c) wave grouping rationale that prep activation checks. Without IterSpec these go un-cited or land in commit messages, both anti-Rule 4. |
| **D3** | WaveSpec mockup format | (a) **ASCII art block** in frontmatter `mockup:` field; (b) PNG link to `.ea/local/mockups/<wave>.png`; (c) SVG inline; (d) Mermaid; (e) ASCII + optional Mermaid | **(e) ASCII + optional Mermaid** | ASCII is operator-readable in any text editor, diff-renderable in `git log -p`, doesn't require image tooling, and stays inside the spec frontmatter so the validator catches its absence. Mermaid is optional for dep DAGs and edge graphs where ASCII becomes unreadable. PNG / SVG break terminal-first review and add a second mutation surface (image churn vs frontmatter churn). |
| **D4** | `implements:` citation format strictness | (a) free-form string; (b) `(verdict_id, brief_path)` tuple; (c) **`(verdict_id, brief_path, line)` triple via Pydantic VerdictCitation model with regex on `verdict_id`** | **(c) typed VerdictCitation with regex** | V2 anti-error mechanism [4:288-289] requires `implements:` ≥ 1 entry. Free-form strings let "implements: V12" pass with no traceable source. Typed VerdictCitation with `verdict_id: Annotated[str, Field(pattern=r"^[VDRH]\d+(-[A-Z0-9]+)?$")]` rejects malformed citations at load time. `line` is optional (round-numbered briefs without per-line anchors stay readable). |
| **D5** | Tests-must-reference-real-paths hook | (a) Pydantic validator at `init`; (b) pre-commit hook scanning staged spec files; (c) **both** | **(c) both** | Pydantic catches paths missing from the filesystem at the moment of authoring (fast feedback); pre-commit catches the case where the test path existed at `init` but was deleted between authoring and commit (the test got renamed or moved). Both run cheaply (just `os.path.exists` + an in-repo `git ls-files | grep` for the staged path). |
| **D6** | Spec lifecycle transitions | DRAFT→READY: `eawf <tier> spec validate` returns ok. READY→IMPLEMENTED: parent scope `close` writes the IMPLEMENTED transition. IMPLEMENTED→ARCHIVED: parent phase `close` triggers daemon `git rm` + cache write. | **freeze the C01 §5.4.15 [2:1125-1151] DAG** | C01 already locked the four-state DAG. C03 only adds the per-transition gate predicates and writer surface: `validate` is read-only; `close` is the state-CLI mutator path for IMPLEMENTED; daemon is the only writer for ARCHIVED (so the `git rm` and cache update happen atomically). No transitions back from ARCHIVED except via `eawf phase reopen` per C01 [2:1503-1505]. |
| **D7** | Cross-spec validation rule | (a) `PhaseSpec.iter_ids == set(IterSpec.id)` only; (b) **(a) + `sum(IterSpec.wave_ids) == set(WaveSpec.id)`**; (c) (b) + per-wave dep-graph consistency check | **(b)** — Phase ↔ Iter ↔ Wave count parity; dep-graph check stays in `eawf wave plan` (already enforced) | Spec-time check catches the "PhaseSpec lists 3 iters; only 2 IterSpecs on disk" class of drift. Dep-graph is already validated on wave-claim per `eawf wave claim` [11:Worktree discipline]; duplicating it in `spec lint` is churn. |
| **D8** | `schema_version` model | (a) one global `Literal["1.0"]` (matches today's State [10:492]); (b) ~~per-spec-kind `Literal[1]`~~; (c) embedded inside the kind discriminator | **(a) — string MAJOR.MINOR `Literal["1.0"]` per Q5 / BOT-03 (revised 2026-05-18)** | ~~Per-kind integer versioning rejected because it forks the literal format (state uses `"1.0"`, spec used `1`, config used `"1.2"`, plugin manifest used `"1"` — four formats). Migration tooling must handle every format.~~ **Per Q5 lock (2026-05-18): every Pydantic state model uses `schema_version: Literal["1.0"]` string MAJOR.MINOR.** When PhaseSpec needs a v2 field, bump to `Literal["2.0"]` (full discriminator-aware migration). Pre-commit lint rejects deviations. Daemon protocol stays composite (`eawfd-rpc/3.0`). |
| **D9** | Render targets | (a) `--md` only; (b) `--md` + `--json`; (c) **`--md` + `--json` + `--diff <other-spec-urn>`** | **(c)** | `--md` is human read; `--json` is machine read (TUI consumes JSON to avoid markdown-parse cost); `--diff` is operator-facing for phase reopen + supersede flows. Diff is text-only in C03; C09 may add visual diff later. |
| **D10** | Audit `verify-implements` trigger | (a) on wave close; (b) on iter close; (c) on phase close; (d) configurable per AuditSpec.cadence | **(d) configurable per AuditSpec.cadence** (operator override, 2026-05-16 /blitz) | Operator picks per-AuditSpec when the check fires (`on_wave_close` / `on_iter_close` / `on_phase_close` / `manual`). Rationale: phase-close-only is the cheapest default but flexibility is needed for (a) waves with high-cost verdict markers that want early-fail at wave-close, (b) iter-level audit kinds covering cross-wave drift before phase ships, (c) manual-run during operator-driven incident replays. The AuditSpec.cadence Literal already enumerates the four values (§5.6); the runner dispatches by reading the field rather than hard-coding phase-close. Today's A27 [4:106] becomes one of many configured cadence triggers, not the sole one. |
| **D11** | Mockup-required heuristic | (a) explicit annotation per wave; (b) **file-scope path-prefix heuristic** (`src/eawf/tui_v2/` or `src/eawf/render/`); (c) profile-driven (engineering profile requires; research relaxes) | **(b) + (c) layered** | Path-prefix is the cheap default (no extra annotation). Profile-driven overrides land in C08; today the heuristic is hard-coded in `eawf wave spec validate`. Authors who feel the heuristic mis-fires can opt out per-wave via explicit `mockup: null` + a `mockup_waiver_reason: str` field (which the validator records but does not reject if the path-prefix doesn't match). |

## 5. Proposed schemas, CLI surface, audit-DSL extension

The body of the brief — Pydantic schemas, CLI verb table, audit-DSL kind, validator implementations. C04 / C05 / C06 / C09 each consume one of these subsections.

### 5.1 Common building blocks

These types are shared across PhaseSpec / IterSpec / WaveSpec and are defined once.

```python
# src/eawf/spec/common.py

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    """Forbid unknown keys (AGENTS rule 2)."""
    model_config = ConfigDict(extra="forbid")


VerdictIdStr = Annotated[
    str,
    Field(pattern=r"^[VDRH]\d+(-[A-Z0-9]+)?$"),
]
# Examples: "V12", "V12-RC3", "D17", "R5", "H03-12"
# V = verdict (cluster brief or earlier research)
# D = decision (operator-ratified D# in a brief's §4 matrix)
# R = recommendation (long-term-features long-term R# in §"Final picks")
# H = hypothesis id (per-state H<NN>-<NN>)


BriefPathStr = Annotated[
    str,
    Field(
        min_length=1,
        # Repo-relative path beneath .ea/local/research/ OR .ea/artifacts/research/
        pattern=r"^\.ea/(local|artifacts)/research/.+\.md$",
    ),
]


class VerdictCitation(_StrictModel):
    """One verdict citation. Per D4."""

    verdict_id: VerdictIdStr
    brief: BriefPathStr
    line: int | None = Field(default=None, ge=1)
    note: str | None = None  # one-line annotation (≤200 chars; lint warns over)


TestRef = Annotated[
    str,
    Field(
        min_length=1,
        # Repo-relative path under tests/. Loose regex per Q13 override
        # (2026-05-16 /blitz): any extension permitted — accommodates SVG
        # snapshots, JSON fixtures, markdown golden, .txt diff baselines,
        # asciinema casts, future test artefacts.
        pattern=r"^tests/.+$",
    ),
]


FileScopeRef = Annotated[
    str,
    Field(
        min_length=1,
        # Repo-relative path under src/, tools/, .ea/, docs/, or build/
        pattern=r"^(src|tools|\.ea|docs|build|tests)/.+$",
    ),
]


class EvidenceRef(_StrictModel):
    """One row of a HypothesisSpec.evidence_chain. Slim by design."""

    kind: Literal["audit", "artifact", "store_record", "external_url"]
    ref: str  # URN for in-state refs; URL for external
    summary: str = Field(min_length=1, max_length=400)
```

### 5.2 PhaseSpec

```python
# src/eawf/spec/phase.py

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from eawf.spec.common import (
    FileScopeRef,
    VerdictCitation,
    _StrictModel,
)
from eawf.state.models import PhaseIdStr, IterIdStr


class PhaseKPI(_StrictModel):
    """Quantitative KPI for the phase."""

    metric: str
    target: float
    direction: Literal["min", "max", "equal"]
    threshold_kind: Literal["hard", "soft"]
    note: str | None = None


class PhaseShipCriterion(_StrictModel):
    """One ship-gate criterion. Tied to the audit DSL via `audit_kind`."""

    id: str
    text: str
    audit_kind: str | None = None  # optional audit-DSL kind that proves this criterion


class PhaseEUEnvelope(_StrictModel):
    """Effort-unit envelope per AGENTS test discipline + EU calibration."""

    expected_eu_total: float = Field(ge=0)
    pessimistic_eu_total: float | None = Field(default=None, ge=0)
    confidence: Literal["low", "medium", "high"] = "medium"


class PhaseSpec(_StrictModel):
    """Phase charter per V2 [1:60-62]. C03 D1: medium field set."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["PhaseSpec"] = "PhaseSpec"

    id: PhaseIdStr
    title: str = Field(min_length=1, max_length=120)
    outcome: str = Field(min_length=20, max_length=1500)  # outcome statement; prose
    kpis: list[PhaseKPI] = Field(default_factory=list)
    success_modes: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list, min_length=1)
    depends_on: list[PhaseIdStr] = Field(default_factory=list)
    eu_envelope: PhaseEUEnvelope | None = None
    ship_criteria: list[PhaseShipCriterion] = Field(default_factory=list, min_length=1)
    iter_ids: list[IterIdStr] = Field(default_factory=list)
    profile_constraints: list[str] = Field(default_factory=list)
    # Optional cross-cite tracking
    implements: list[VerdictCitation] = Field(default_factory=list)
    consumed_by: list[PhaseIdStr] = Field(default_factory=list)
    related_file_scopes: list[FileScopeRef] = Field(default_factory=list)
```

Invariants enforced by `eawf phase spec validate`:

- **PSV-01.** `failure_modes` non-empty (forces negative-space thinking per [4:289]).
- **PSV-02.** `ship_criteria` non-empty (no PhaseSpec graduates to READY without a ship gate).
- **PSV-03.** `outcome` length ≥ 20 chars (titles-only rejected — outcome is prose, not a label).
- **PSV-04.** Every `kpis[*].metric` matches a metric the project's outcome registry tracks (cross-check against `state.outcomes` keys when present; warning otherwise).
- **PSV-05.** `depends_on` entries refer to phases that exist in `state.phases`; loader-time check fails if not.
- **PSV-06.** Every `ship_criteria[*].audit_kind` (when set) is a registered audit-DSL kind per `eawf.audit_dsl.registry.CHECK_REGISTRY` [25:11-19].

### 5.3 IterSpec

```python
# src/eawf/spec/iter.py

from __future__ import annotations

from typing import Literal

from pydantic import Field

from eawf.spec.common import VerdictCitation, _StrictModel
from eawf.state.models import IterIdStr, PhaseIdStr, WaveIdStr


class IterAuditCadence(_StrictModel):
    """When audit-DSL kinds fire for this iter."""

    on_iter_close: list[str] = Field(default_factory=list)  # audit_kind names
    on_phase_close: list[str] = Field(default_factory=list)


class IterWaveGroup(_StrictModel):
    """One grouping of waves with shared narrative purpose."""

    label: str
    wave_ids: list[WaveIdStr] = Field(default_factory=list, min_length=1)
    rationale: str = Field(min_length=20, max_length=600)


class IterSpec(_StrictModel):
    """Iter intent per V2 [1:60-62]. C03 D2: keep full."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["IterSpec"] = "IterSpec"

    id: IterIdStr
    phase_id: PhaseIdStr
    title: str = Field(min_length=1, max_length=120)
    sub_goal: str = Field(min_length=20, max_length=800)
    ordering_rationale: str = Field(min_length=20, max_length=1000)
    wave_groups: list[IterWaveGroup] = Field(default_factory=list)
    audit_cadence: IterAuditCadence = Field(default_factory=IterAuditCadence)
    profile_constraints: list[str] = Field(default_factory=list)
    implements: list[VerdictCitation] = Field(default_factory=list)
    wave_ids: list[WaveIdStr] = Field(default_factory=list)  # mirror of state.iters[id].wave_ids
```

Invariants enforced by `eawf iter spec validate`:

- **ISV-01.** `sub_goal` length ≥ 20 chars.
- **ISV-02.** `ordering_rationale` length ≥ 20 chars (prevents "because" one-liners).
- **ISV-03.** If `wave_groups` is non-empty, every wave id under any group must appear in `wave_ids`.
- **ISV-04.** `wave_ids` matches `state.iters[id].wave_ids` (cross-state-and-spec consistency).
- **ISV-05.** Every audit cadence entry refers to a registered audit-DSL kind.
- **ISV-06.** `phase_id` matches `id.rsplit('-', 1)[0]` (P20-I03 lives under P20).

### 5.4 WaveSpec

```python
# src/eawf/spec/wave.py

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from eawf.spec.common import (
    FileScopeRef,
    TestRef,
    VerdictCitation,
    _StrictModel,
)
from eawf.state.enums import AgentSessionRole, EffortBucket
from eawf.state.models import IterIdStr, PhaseIdStr, WaveIdStr


class WaveBehavior(_StrictModel):
    """One observable behaviour the wave delivers (B1..Bn)."""

    id: Annotated[str, Field(pattern=r"^B\d+$")]
    text: str = Field(min_length=20, max_length=1000)
    latency_budget_ms: int | None = Field(default=None, ge=0)
    test_refs: list[TestRef] = Field(default_factory=list)


class WaveMockup(_StrictModel):
    """ASCII (+ optional Mermaid) mockup per D3."""

    ascii: str = Field(min_length=1)
    mermaid: str | None = None
    note: str | None = None


class WaveSpec(_StrictModel):
    """Wave deliverable per V2 [1:60-62]."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["WaveSpec"] = "WaveSpec"

    id: WaveIdStr
    iter_id: IterIdStr
    phase_id: PhaseIdStr
    title: str = Field(min_length=1, max_length=120)
    agent_role: AgentSessionRole
    effort_bucket: EffortBucket
    deps: list[WaveIdStr] = Field(default_factory=list)
    file_scopes: list[FileScopeRef] = Field(default_factory=list, min_length=1)
    implements: list[VerdictCitation] = Field(default_factory=list, min_length=1)
    behaviors: list[WaveBehavior] = Field(default_factory=list, min_length=1)
    failure_modes: list[str] = Field(default_factory=list, min_length=1)
    tests: list[TestRef] = Field(default_factory=list)
    mockup: WaveMockup | None = None
    mockup_waiver_reason: str | None = None  # required if mockup is None for a UI-scope wave (heuristic — D11)

    @model_validator(mode="after")
    def _consistent_ids(self) -> WaveSpec:
        if not self.id.startswith(f"{self.iter_id}-W"):
            raise ValueError(
                f"wave id {self.id!r} does not nest under iter {self.iter_id!r}"
            )
        if not self.iter_id.startswith(f"{self.phase_id}-I"):
            raise ValueError(
                f"iter id {self.iter_id!r} does not nest under phase {self.phase_id!r}"
            )
        return self
```

Invariants enforced by `eawf wave spec validate`:

- **WSV-01.** `implements` non-empty (forces verdict citation per [4:288]).
- **WSV-02.** `failure_modes` non-empty (forces negative-space thinking per [4:289]).
- **WSV-03.** `behaviors` non-empty (every wave has at least one observable behaviour).
- **WSV-04.** `file_scopes` non-empty.
- **WSV-05.** Every `tests[*]` path exists on disk at validate time (D5 Pydantic side; pre-commit re-checks).
- **WSV-06.** Each `behaviors[*].test_refs[*]` path exists.
- **WSV-07.** Mockup-required heuristic (D11): if any `file_scopes[*]` starts with `src/eawf/tui_v2/` or `src/eawf/render/`, then `mockup` must be non-None OR `mockup_waiver_reason` must be set + non-empty.
- **WSV-08.** `deps` entries appear in `state.waves` keys (loader-time DAG consistency).
- **WSV-09.** `id` / `iter_id` / `phase_id` are linked by prefix (covered by `_consistent_ids` model validator).
- **WSV-10.** Every `implements[*].brief` path exists on disk OR the file is a `.ea/local/` draft (warns) OR the file is a `.ea/artifacts/` promoted artifact (passes).

### 5.5 ResearchBriefSpec, HypothesisSpec, DecisionSpec

Three lightweight extensions of existing chassis. None of them lives on `state.json` (HypothesisSpec extends the `Hypothesis` row, DecisionSpec extends the `Decision` row, ResearchBriefSpec is the frontmatter the chassis already requires).

```python
# src/eawf/spec/research.py

from __future__ import annotations

from typing import Literal

from pydantic import Field

from eawf.spec.common import BriefPathStr, _StrictModel


class ResearchBriefSpec(_StrictModel):
    """Frontmatter for `.ea/local/research/*.md` and `.ea/artifacts/research/*.md`.

    Promoted artifacts MUST validate; local drafts MAY have status='draft'
    and skip implements: + consumed_by:. Loader keys on the eawf-template
    sentinel comment to discriminate.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["ResearchBriefSpec"] = "ResearchBriefSpec"

    title: str = Field(min_length=1)
    status: Literal["draft", "local-draft", "needs-user", "accepted", "promoted", "archived"]
    created: str  # ISO-8601 date; loose to allow datetime + date
    author: str
    depends_on: list[str] = Field(default_factory=list)
    consumed_by: list[str] = Field(default_factory=list)
    implements: list[str] = Field(default_factory=list)  # verdict ids cited by the brief
    supersedes: BriefPathStr | None = None  # link to brief this one replaces
```

```python
# src/eawf/spec/hypothesis.py

from __future__ import annotations

from typing import Literal

from pydantic import Field

from eawf.spec.common import EvidenceRef, _StrictModel
from eawf.state.enums import HypothesisVerdict


class HypothesisSpec(_StrictModel):
    """Extends current Hypothesis row [10:244-256] with `evidence_chain`.

    Stored alongside the hypothesis itself; the state row carries the
    same `text` / `metric` / `confirm` / `reject` fields; the spec adds
    the audit-replayable evidence chain.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["HypothesisSpec"] = "HypothesisSpec"

    id: str  # H<NN>-<NN>
    text: str
    metric: str
    confirm: str
    reject: str
    evidence_chain: list[EvidenceRef] = Field(default_factory=list, min_length=1)
    verdict: HypothesisVerdict | None = None
    verdict_audit_id: str | None = None  # urn:eawf:v1:audit:... when verdict set
```

```python
# src/eawf/spec/decision.py

from __future__ import annotations

from typing import Literal

from pydantic import Field

from eawf.spec.common import VerdictCitation, _StrictModel


class SupersedeLink(_StrictModel):
    """One link in a Decision supersede chain."""

    decision_id: str
    direction: Literal["supersedes", "superseded_by"]
    note: str | None = None


class DecisionSpec(_StrictModel):
    """Extends current Decision row [10:294-305] with explicit supersede chain.

    Today's `Decision.superseded_by: str | None` only points one way; the
    spec records the bi-directional `supersedes` chain so a future
    operator can walk the lineage in either direction without rebuilding
    it from `event.jsonl`.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["DecisionSpec"] = "DecisionSpec"

    id: str
    summary: str = Field(min_length=20, max_length=400)
    rationale: str = Field(min_length=20)
    alternatives: list[str] = Field(default_factory=list)
    supersede_chain: list[SupersedeLink] = Field(default_factory=list)
    implements: list[VerdictCitation] = Field(default_factory=list)
```

### 5.6 AuditSpec

The AuditSpec is the typed document the audit-DSL runner consumes when a phase / iter / wave close fires its audit cadence. Today's `eawf.audit_dsl.models.CheckFile` [25] is a flat `schema_version + checks: list[CheckSpec]`; the AuditSpec **extends** that surface with cadence binding, verdict citations, and per-audit-kind ordering.

```python
# src/eawf/spec/audit.py

from __future__ import annotations

from typing import Literal

from pydantic import Field

from eawf.audit_dsl.models import CheckSpec
from eawf.spec.common import VerdictCitation, _StrictModel
from eawf.state.enums import AuditKind


class AuditSpec(_StrictModel):
    """Declarative audit document. Extends CheckFile [25:69-77].

    Lives at `.ea/audits/<scope>.audit.yaml` (NOT under `.ea/specs/`
    because audits attach to scopes already in state.json, not to spec
    docs). Each AuditSpec produces an `Audit` row [10:259-271] when run.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["AuditSpec"] = "AuditSpec"

    id: str
    scope_urn: str  # urn:eawf:v1:phase:<scope>/<id> or wave/iter equivalent
    audit_kind: AuditKind
    cadence: Literal["on_wave_close", "on_iter_close", "on_phase_close", "manual"]
    implements: list[VerdictCitation] = Field(default_factory=list)
    checks: list[CheckSpec] = Field(default_factory=list, min_length=1)
    fail_fast: bool = False  # stop on first failing check; default = run all + summarise
```

**Existing check-kind catalog [25:30-38].** `file_exists`, `path_glob_nonempty`, `regex_in_file`, `state_field_equals`, `command_exit_zero`.

**C03 adds one kind: `verify_implements`.**

### 5.7 Audit-DSL kind: `verify_implements`

The check kind that closes the RC-1 loop [4:83-87]: at phase close (per D10), walks every closed-wave WaveSpec under the phase, and for each `implements[*]` entry asserts that the verdict id appears as a comment marker in the diff of files matched by the wave's `file_scopes:`.

```python
# src/eawf/audit_dsl/kinds/verify_implements.py

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from eawf.audit_dsl.models import CheckResult, CheckSpec


VERDICT_MARKER_RE = re.compile(
    r"#\s*IMPLEMENTS:\s*\(([VDRH]\d+(?:-[A-Z0-9]+)?)"
    r"\s*,\s*([^,]+)\s*(?:,\s*(\d+))?\)"
)
# Matches: `# IMPLEMENTS: (V12, .ea/local/research/p20-tui-verdicts.md, 585)`


def check_verify_implements(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Walk closed-wave WaveSpecs, grep file_scopes for verdict markers.

    args = {
        phase_id: str (P##),
        diff_base: str = "main" — git ref to compare HEAD against,
    }
    """
    phase_id = spec.args.get("phase_id", "")
    diff_base = spec.args.get("diff_base", "main")
    if not phase_id:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            details="missing args.phase_id",
        )

    phase_dir = cwd / ".ea" / "specs" / phase_id
    if not phase_dir.is_dir():
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            details=f"no spec dir at {phase_dir.relative_to(cwd)}",
        )

    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{diff_base}...HEAD"],
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    changed = set(line.strip() for line in proc.stdout.splitlines() if line.strip())

    from eawf.spec.wave import WaveSpec
    from eawf.spec.loader import load_wave_spec

    missing: list[str] = []
    for spec_path in phase_dir.rglob("*.md"):
        if spec_path.name == "spec.md":
            continue  # PhaseSpec or IterSpec; not a WaveSpec
        ws: WaveSpec = load_wave_spec(spec_path)
        verdict_ids = {c.verdict_id for c in ws.implements}
        # Files in this wave's scope that are part of the phase diff
        scope_changed = [p for p in ws.file_scopes if p in changed]
        if not scope_changed:
            missing.append(f"{ws.id}: no file_scopes in diff")
            continue
        seen: set[str] = set()
        for path in scope_changed:
            try:
                text = (cwd / path).read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue
            for match in VERDICT_MARKER_RE.finditer(text):
                seen.add(match.group(1))
        unsatisfied = verdict_ids - seen
        if unsatisfied:
            missing.append(f"{ws.id}: missing markers for {sorted(unsatisfied)}")

    if missing:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            details="; ".join(missing),
        )
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=True,
        details=f"phase {phase_id}: all WaveSpec.implements satisfied",
    )
```

**Grammar additions to `eawf.audit_dsl.models.CheckKind` [25:30-38].**

```python
CheckKind = Literal[
    "file_exists",
    "path_glob_nonempty",
    "regex_in_file",
    "state_field_equals",
    "command_exit_zero",
    "verify_implements",  # new — C03
]
```

**Marker grammar.** Comment-style verdict marker accepted in any file format the diff touches:

```
# IMPLEMENTS: (V12, .ea/local/research/p20-tui-verdicts.md, 585)
// IMPLEMENTS: (V12, .ea/local/research/p20-tui-verdicts.md, 585)
<!-- IMPLEMENTS: (V12, .ea/local/research/p20-tui-verdicts.md, 585) -->
```

The leading comment marker (`#`, `//`, `<!--`) is consumed before the marker regex matches; the regex `VERDICT_MARKER_RE` above captures verdict id, brief, and optional line. Per-language comment prefixes are handled by the caller (the regex itself is comment-agnostic).

**Failure-mode output.** When the check fails, `details` is a `; `-joined list of `<wave-id>: <reason>` rows — operator can grep the audit report by wave id and see which verdict is missing which marker.

### 5.8 Daemon spec-cache surface

Per C01 §5.3.15 [2:638] the daemon optionally caches the spec index so `eawf spec show` after ARCHIVED works without `git log`. C02 [3:77] reserves the path; C03 specifies the cache contents.

**Path.** `<local-path>`

**Schema.**

```python
class SpecCacheEntry(_StrictModel):
    spec_urn: str            # urn:eawf:v1:spec:<scope>/<phase>[/<iter>[/<wave>]]
    file_sha: str            # git blob SHA at last commit
    file_path: str           # repo-relative path
    status: Literal["DRAFT", "READY", "IMPLEMENTED", "ARCHIVED"]
    last_modified: str       # ISO-8601 datetime
    archived_commit: str | None = None  # SHA of the git rm commit, when ARCHIVED


class SpecCachePhase(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    phase_id: str
    entries: list[SpecCacheEntry]
```

**Writer.** The daemon writes the cache atomically (write-to-temp + `os.rename`) on every spec graduation (DRAFT→READY, READY→IMPLEMENTED, IMPLEMENTED→ARCHIVED). When the daemon is not running (CI / read-only one-shot), `eawf spec show <urn> --from-git` falls back to walking `git log -- <path>` to find the spec body.

### 5.9 CLI surface

The CLI verbs are dispatch shells over the library functions in `eawf.spec`. AGENTS rule 1 [11]: handlers accept typed objects only.

| Verb | Args | Pre-conditions | Post-conditions |
|---|---|---|---|
| `eawf phase spec init <P##>` | `--title`, `--from-brief <path>` | phase row exists in state; no existing `.ea/specs/<P##>/spec.md` | scaffolded PhaseSpec at `.ea/specs/<P##>/spec.md`; status = DRAFT; outcome / kpis pre-filled from `--from-brief` when supplied |
| `eawf phase spec validate <P##>` | `--strict` | spec file exists | exits 0 on pass; exits non-zero with diagnostic on PSV-01..PSV-06 failures; updates daemon cache when `--strict` and validation passes |
| `eawf phase spec render <P##>` | `--md` (default) / `--json` / `--diff <other-P##>` | spec file exists | writes to stdout |
| `eawf phase spec implements <P##>` | — | spec file exists | lists VerdictCitation rows as `<verdict_id>\t<brief>\t<line>` |
| `eawf phase spec promote <P##>` | `--to .ea/artifacts/...` | spec is IMPLEMENTED; phase is CLOSED | copies (does NOT move) the spec into `.ea/artifacts/phases/<P##>.md` with chassis wrapping |
| `eawf iter spec init <P##-I##>` | `--title`, `--sub-goal` | iter row exists; parent PhaseSpec is at least DRAFT | scaffolded IterSpec at `.ea/specs/<P##>/<I##>/spec.md` |
| `eawf iter spec validate <P##-I##>` | `--strict` | spec file exists | exits 0 on pass; non-zero on ISV-01..ISV-06 failures |
| `eawf iter spec render <P##-I##>` | `--md` / `--json` / `--diff` | spec file exists | writes to stdout |
| `eawf iter spec implements <P##-I##>` | — | spec file exists | lists VerdictCitation rows |
| `eawf iter spec promote <P##-I##>` | `--to .ea/artifacts/...` | spec is IMPLEMENTED; iter is CLOSED | copies with chassis wrapping |
| `eawf wave spec init <P##-I##-W##>` | `--title`, `--implements V##:<brief>:<line>` | wave row exists; parent IterSpec is at least DRAFT | scaffolded WaveSpec at `.ea/specs/<P##>/<I##>/<W##>.md`; pre-fills implements / mockup placeholder for UI scopes |
| `eawf wave spec validate <P##-I##-W##>` | `--strict` | spec file exists | exits 0 on pass; non-zero on WSV-01..WSV-10 failures |
| `eawf wave spec render <P##-I##-W##>` | `--md` / `--json` / `--diff <other-W##>` | spec file exists | writes to stdout |
| `eawf wave spec implements <P##-I##-W##>` | — | spec file exists | lists VerdictCitation rows |
| `eawf wave spec promote <P##-I##-W##>` | `--to .ea/artifacts/...` | spec is IMPLEMENTED; wave is CLOSED | copies with chassis wrapping |
| `eawf spec show <urn>` | `--from-git` | URN parses to a known kind | prints spec body; if `--from-git` and current HEAD has no file, daemon-cache → `git log -- <path>` recovery walk |
| `eawf spec lint <P##>` | — | PhaseSpec exists | runs cross-spec validation (D7) over PhaseSpec + IterSpecs + WaveSpecs under `<P##>`; emits CSV diagnostic table |
| `eawf spec graduate <urn>` | `--to {READY,IMPLEMENTED,ARCHIVED}` | current status is one step below target; gate predicates pass | mutates state.json wave/iter/phase row OR (for ARCHIVED) daemon performs `git rm` + cache write |

**Verb-noun shape.** The two-noun form `eawf phase spec init` is preferred over `eawf spec phase init` to align with the existing `eawf phase activate` / `eawf iter close` / `eawf wave claim` shape [16] — `eawf <scope-kind> <action>` is the dominant pattern. C05 may reshape the entire surface; C03's recommendation is "two-noun, scope-first".

### 5.10 Renderer integration

`eawf {phase,iter,wave} spec render --md` produces a markdown document with the standard eawf chassis:

```markdown
<!-- eawf-template: spec-{phase,iter,wave} -->

# {PhaseSpec.title} (or IterSpec / WaveSpec)

**Spec URN:** urn:eawf:v1:spec:<repo>/<phase>[/<iter>[/<wave>]]
**Status:** DRAFT | READY | IMPLEMENTED | ARCHIVED
**Created:** YYYY-MM-DD
**Author:** <agent>

## Summary

<auto-rendered from outcome / sub_goal / title-and-implements-and-behaviors>

## Implements

[1] V12 — .ea/local/research/p20-tui-verdicts.md:585
[2] D17 — .ea/local/dispatch-P20-I03-W01.txt:88

## (per-tier body)

- PhaseSpec body: KPI table, success/failure modes, ship criteria, EU envelope
- IterSpec body: sub-goal prose, ordering rationale, wave groups, audit cadence
- WaveSpec body: file scopes, behaviours B1..Bn (with latency budgets + test refs),
  failure modes, tests, mockup (ascii + optional mermaid)

## References

[1] {brief paths from implements: + consumed_by:}

## Provenance

- kind: spec
- record_id: <urn>
- scope_id: <P##|P##-I##|P##-I##-W##>

## Scrub

- status: clean
```

The renderer hooks into the existing `eawf.render.artifact_chassis` [29] for References / Provenance / Scrub; the spec body is per-tier and lives at `eawf.render.spec_*` (new module).

## 6. Failure modes + named edge cases

| F# | Failure mode | Detection / mitigation |
|---|---|---|
| **F1** | Spec exists on disk but no state row | `eawf spec lint <P##>` reports orphan; CI gate fails when running on a feature branch. C03 ships `eawf spec lint --orphans-only`. |
| **F2** | State wave exists; no WaveSpec on disk | `eawf wave claim` refuses; CLI emits the `eawf wave spec init` scaffold command in its error envelope. |
| **F3** | WaveSpec validates at init; test path deleted before commit | Pre-commit hook re-runs WSV-05 + WSV-06 against staged paths (D5). |
| **F4** | UI-scope wave with mockup waiver but obviously UI-shaped behaviour text | Today's heuristic is path-prefix only; can't be improved without a heuristic on `behaviors[*].text`. Operator-side waiver review is the safety net. |
| **F5** | `implements:` cites a verdict id that doesn't exist in any brief | Validator-time grep across `.ea/local/research/` + `.ea/artifacts/research/`; warns when no brief contains the verdict id at the cited line. Hard-fail only when `--strict`. |
| **F6** | Phase reopened; ARCHIVED specs need restoring | `eawf phase reopen` daemon-side: walk spec cache, `git show <last_commit>:<spec_path> > <spec_path>` for every ARCHIVED entry under the phase. Status flips back to IMPLEMENTED (matching parent wave's reopened status). |
| **F7** | verify_implements check fails for a wave that's intentionally a scaffolding-only commit | Wave authors mark such waves with `agent_role: planner` + explicit `behaviors: [{id: B1, text: "scaffold-only — no shippable verdict"}]`; check passes when verdict set is empty AND `agent_role == "planner"`. |
| **F8** | Spec frontmatter YAML parse error (operator hand-edit broke quoting) | `eawf <tier> spec validate` surfaces line + column from PyYAML / Pydantic; suggested fix line points at the validator that rejected it. |
| **F9** | Cache stale (daemon down; specs mutated by editor) | `eawf spec show --no-cache` bypasses the cache; CI builds always run with `--no-cache`. |
| **F10** | Two waves under the same iter with overlapping `file_scopes` | Allowed — not a failure mode at spec time. Wave-claim still enforces the worktree-isolation invariant per AGENTS rule 11. Flag only with `eawf spec lint --warn-overlap`. |
| **F11** | Migration backfill writer over-eagerly populates WaveSpec for legacy waves | Backfill is opt-in per-phase: `eawf spec migrate <P##> --backfill` is operator-triggered. Never auto-runs on read of legacy state. |
| **F12** | Schema bump (Literal[1] → Literal[2]) loads old spec | Loader emits `schema_version` failure with explicit migration command (`eawf spec migrate-version <urn> --to 2`); never silently upgrades a file the validator cannot freshly approve. |

### Edge cases

- **Wave claims before WaveSpec exists.** `eawf wave claim` requires spec status READY for the target wave. The error envelope from `eawf wave claim P20-I03-W01` when no spec exists is `WaveSpec not found at .ea/specs/P20/I03/W01.md — run \`eawf wave spec init P20-I03-W01\` first`. P20-DIR §"Spec infrastructure ships first" [4:584] is the load-bearing constraint.
- **Spec graduation transitions vs state mutations.** READY→IMPLEMENTED graduation is *triggered* by `eawf wave close`, but the writer is the state CLI (which already mutates the parent wave's status). The Spec-side bookkeeping lives in the daemon cache; the state-side bookkeeping lives in `Wave.status: CLOSED`. The two writers commit in the same transaction (state-CLI begins the transaction, daemon writes cache, transaction commits or rolls back). When daemon is unavailable, state-CLI proceeds without cache write — cache becomes lazy-rebuilt by next `eawf spec show`.
- **`mockup_waiver_reason` empty string.** Validator distinguishes `None` (mockup is required) from `""` (waiver explicitly cleared by author — fails). Authors must write prose; empty strings rejected at Pydantic `min_length=1`.
- **VerdictCitation.line out of range.** Validator does not load the cited brief at init time (would slow load). `eawf wave spec validate --strict` reads each cited brief and warns if `line` exceeds the file's line count.
- **Profile bundles with conflicting validators.** C08 owns conflict resolution. C03 reads the resolved validator chain from the project's effective ruleset; if two validators disagree on whether a field is required, the loader fails at project-config-load (per V3 [1:80-86]).
- **AuditSpec for `verify_implements` referring to a phase that has no waves.** Check returns `passed=True, details="phase P## has no closed waves; nothing to verify"`. This avoids spurious failure on the first close of a brand-new phase with no implementations yet.

## 7. Migration plan

C03 is a forward-only change. Legacy waves are not retroactively coerced.

### 7.1 Migration scope

| Item | Action |
|---|---|
| `Wave.success_criteria: list[str]` field [10:231] | **Keep.** Marked deprecated in docstring; new code paths (after C03 implementation phase ships) do not write to it. Legacy waves that already carry it remain readable. |
| Closed waves with `success_criteria` text | One-shot **backfill writer** generates a WaveSpec under `.ea/specs/<P##>/<I##>/<W##>.md` from the legacy text. Status = IMPLEMENTED. Operator-triggered: `eawf spec migrate <P##> --backfill`. |
| Active / pending waves with `success_criteria` text | Operator decides per-wave: keep legacy or scaffold a new WaveSpec via `eawf wave spec init` (overwrites only after `--force`). Default: keep legacy until next dispatch. |
| Phase / iter rows | **No state-row change.** PhaseSpec / IterSpec are filesystem-only per C01 D3 [2:120]. |
| `eawf wave plan` CLI | Extend to accept `--spec` flag pointing at an existing WaveSpec; preserves the existing positional `success_criteria` argument for legacy callers. |
| `eawf wave claim` CLI | Add gate: if a `.ea/specs/<phase>/<iter>/<wave>.md` exists, it MUST validate at `eawf wave spec validate --strict` before claim succeeds. If no spec exists, the legacy path runs unchanged. |
| `eawf phase activate` CLI (V11 hard gate) | Extend gate: when `.ea/specs/<P##>/spec.md` exists, validate strict; refuse activation on fail. When absent, today's behaviour preserved with a warning suggesting `eawf phase spec init`. |
| `tools/commit_prefix_lint.py` [12] | No change required — `_STATE_ONLY_PREFIXES` [12:61] already includes `.ea/specs/` (P20-W01 landed this allow-list). Verified at `commit_prefix_lint.py:61`. |
| `<local-path>` daemon cache | New on first run of C03-instrumented daemon; auto-creates parent dirs on first write. |

### 7.2 Backfill writer algorithm

```python
# src/eawf/spec/migrate.py

from __future__ import annotations

from pathlib import Path

from eawf.spec.wave import WaveBehavior, WaveSpec
from eawf.spec.common import VerdictCitation
from eawf.state.models import State


def backfill_phase(state: State, phase_id: str, dest_root: Path) -> list[Path]:
    """Write WaveSpec for every closed wave under phase_id that lacks one.

    Idempotent: if a WaveSpec already exists at the destination path, skip.
    Verdict citations are NOT invented — implements stays empty and the
    spec is written with status_meta='backfilled, implements_pending'.
    Operator is responsible for re-running `eawf wave spec validate`
    after editing in the citations.
    """
    written: list[Path] = []
    for wave_id, wave in state.waves.items():
        if wave.status != "CLOSED":
            continue
        if not wave.iter_id.startswith(f"{phase_id}-I"):
            continue
        spec_path = dest_root / phase_id / wave.iter_id.split("-")[1] / f"{wave_id.rsplit('-', 1)[1]}.md"
        if spec_path.exists():
            continue
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_text = " · ".join(wave.success_criteria) or "(no legacy criteria)"
        ws = WaveSpec(
            id=wave_id,
            iter_id=wave.iter_id,
            phase_id=phase_id,
            title=wave.title,
            agent_role=wave.agent_role or "executor",
            effort_bucket=wave.effort_bucket or "M",
            deps=wave.deps,
            file_scopes=wave.file_scopes or ["src/"],  # fallback
            implements=[],  # empty — operator backfills
            behaviors=[
                WaveBehavior(
                    id="B1",
                    text=f"(backfilled from legacy success_criteria) {legacy_text[:900]}",
                )
            ],
            failure_modes=["(backfilled — operator should specify)"],
            tests=[],
            mockup=None,
            mockup_waiver_reason=(
                "backfilled from legacy criteria; no mockup available retroactively"
            ),
        )
        spec_path.write_text(render_wave_spec_md(ws), encoding="utf-8")
        written.append(spec_path)
    return written
```

### 7.3 Rollback plan

- **State-row safety.** Migration does not touch `state.json`. Rolling back is `git rm -r .ea/specs/<P##>` + commit. The legacy `Wave.success_criteria` text remains untouched.
- **Cache safety.** Daemon cache at `<local-path>` is gitignored. Deleting it forces lazy rebuild on next read; no data loss.
- **Schema bump.** If `schema_version: 1` proves insufficient and a v2 needs to land mid-phase, the loader's `Literal[1]` rejects v2 files outright — operator must run `eawf spec migrate-version <urn> --to 2` per-file. C09 owns the migration runner shape (Alembic-style step files); C03 reserves the verb.

### 7.4 Rollout sequencing

| Step | Wave | Surface | Validation |
|---|---|---|---|
| 1 | C03-impl-W01 | `eawf.spec.common` + `eawf.spec.{phase,iter,wave,research,hypothesis,decision,audit}` Pydantic models; unit tests per WSV-* / ISV-* / PSV-* | `pytest tests/spec/` |
| 2 | C03-impl-W02 | `eawf.spec.loader` + `eawf.spec.render` + `eawf.spec.migrate` (backfill writer) | smoke: `eawf spec migrate P20 --backfill` produces N specs |
| 3 | C03-impl-W03 | CLI verbs (§5.9) wired into `eawf.cli.commands.lifecycle` and `eawf.cli.commands.research`; help text updated | `eawf phase spec --help` lists the four verbs |
| 4 | C03-impl-W04 | Audit-DSL `verify_implements` kind + registry update [25:11-19]; AuditSpec model | unit + integration on a fixture phase |
| 5 | C03-impl-W05 | `eawf wave claim` / `eawf phase activate` gate extension | integration: claim refuses on missing spec; passes on valid spec |
| 6 | C03-impl-W06 | Daemon spec-cache writer + reader; cache hydration on `eawf spec show --from-git` | integration: cache invalidated after `git rm` survives |
| 7 | C03-impl-W07 | Migration runbook + docs; AGENTS.md cross-link (no rule rewrite — just pointer) | doc-render passes; AGENTS-md unchanged |

## 8. Open questions for operator

All 14 ratified 2026-05-16 via `/blitz` rounds (R1..R4). Recommendations matched §4 D# rows except for **Q10** (operator picked configurable AuditSpec.cadence over phase-close-only) and **Q13** (operator picked loose `tests/.+` regex over tight extension list). The brief body (§4 D10, §5.1 TestRef, §5.6 AuditSpec.cadence) reflects the overrides.

### Q1 — PhaseSpec field minimum vs maximum (D1)

**Question.** Which PhaseSpec field set should ship in C03?

**Options.**
- (a) **Medium** (recommended): outcome + KPIs + success_modes + failure_modes + ship_criteria + EU envelope + depends_on + profile_constraints.
- (b) Minimum: outcome + KPIs + ship_criteria only.
- (c) Maximum: medium + deferred_to + supersedes + bench_targets + risk_register.

### Q2 — IterSpec necessity defense (D2)

**Question.** Is IterSpec a load-bearing tier or a bookkeeping convention?

**Options.**
- (a) **Keep IterSpec as full first-class** (recommended). Carries sub-goal + ordering rationale + wave grouping rationale + audit cadence + profile constraints.
- (b) Keep IterSpec as ordering + audit cadence only; sub-goal lives on Phase.
- (c) Deprecate IterSpec; fold into PhaseSpec via per-iter subsections.

### Q3 — WaveSpec mockup format (D3)

**Question.** Which mockup format(s) should the WaveSpec frontmatter accept?

**Options.**
- (a) ASCII art block in `mockup.ascii:` field; no other formats.
- (b) PNG link only.
- (c) **ASCII + optional Mermaid** (recommended). ASCII is required; Mermaid optional for dep graphs.
- (d) SVG inline.

### Q4 — `implements:` citation format strictness (D4)

**Question.** How strictly should the validator enforce `implements:` citation shape?

**Options.**
- (a) Free-form string per entry.
- (b) `(verdict_id, brief_path)` tuple without line.
- (c) **`(verdict_id, brief_path, line)` triple via typed VerdictCitation; `verdict_id` regex `^[VDRH]\d+(-[A-Z0-9]+)?$`; `line` optional** (recommended).
- (d) (c) + mandatory line.

### Q5 — Tests-must-reference-real-paths hook layering (D5)

**Question.** Where does the "tests reference real paths" check fire?

**Options.**
- (a) Pydantic validator at `init` only.
- (b) Pre-commit hook only.
- (c) **Both — Pydantic catches at authoring; pre-commit re-catches at commit** (recommended).
- (d) Pre-commit + ship-gate audit (every layer).

### Q6 — Spec lifecycle transitions (D6)

**Question.** Confirm the C01-locked DAG (DRAFT → READY → IMPLEMENTED → ARCHIVED) and the per-transition gates.

**Options.**
- (a) **Freeze C01 §5.4.15 [2:1125-1151] DAG** (recommended). `validate` is the DRAFT→READY gate; parent scope `close` triggers READY→IMPLEMENTED; parent phase `close` triggers daemon `git rm` for ARCHIVED.
- (b) Add an explicit `eawf <tier> spec graduate <urn> --to READY` verb so authors can opt-in to READY without running `validate --strict`.

### Q7 — Cross-spec validation rule (D7)

**Question.** Which cross-tier consistency checks does `eawf spec lint` enforce?

**Options.**
- (a) **PhaseSpec.iter_ids ≡ set(IterSpec.id); sum(IterSpec.wave_ids) ≡ set(WaveSpec.id)** (recommended).
- (b) (a) + per-wave dep-DAG cycle check.
- (c) (a) only.

### Q8 — `schema_version` model (D8)

**Question.** How does `schema_version` evolve?

**Options.**
- (a) Global `Literal["1.0"]` matching State [10:492].
- (b) **Per-spec-kind `Literal[1]`** so PhaseSpec, IterSpec, WaveSpec, ResearchBriefSpec, HypothesisSpec, DecisionSpec, AuditSpec evolve independently (recommended).
- (c) Embedded in the kind discriminator (e.g., `WaveSpec_v1`).

### Q9 — Render targets (D9)

**Question.** Which `eawf <tier> spec render` output targets ship in C03?

**Options.**
- (a) `--md` only.
- (b) `--md` + `--json`.
- (c) **`--md` + `--json` + `--diff <other-urn>`** (recommended). `--diff` is text-only; visual diff lives in C09.

### Q10 — Audit `verify_implements` trigger (D10) — RATIFIED

**Question.** When does the `verify_implements` audit fire?

**Operator pick (2026-05-16 /blitz).** **(d) Configurable per AuditSpec.cadence** — override of brief's original recommendation (c). AuditSpec.cadence enumerates `on_wave_close` / `on_iter_close` / `on_phase_close` / `manual`; the runner dispatches by reading the field per check. C03 implementation phase wires the per-cadence dispatcher into `eawf wave close` / `eawf iter close` / `eawf phase close` and `eawf audit run --manual`.

**Other options considered.**
- (a) On wave close — high frequency, catches drift early but noisy.
- (b) On iter close — medium frequency, misses cross-iter drift.
- (c) On phase close — cheapest default but inflexible.

### Q11 — Mockup-required heuristic (D11)

**Question.** When does the validator require a WaveSpec mockup?

**Options.**
- (a) Always (every WaveSpec needs a mockup).
- (b) **Path-prefix heuristic** (recommended): require when any `file_scopes[*]` starts with `src/eawf/tui_v2/` or `src/eawf/render/`. Author can opt out per-wave via `mockup_waiver_reason: str`.
- (c) Profile-driven only (engineering profile requires; research relaxes).
- (d) Never (mockup is optional everywhere).

### Q12 — Decision on backfill writer aggressiveness

**Question.** When `eawf spec migrate <P##> --backfill` runs, how should it behave?

**Options.**
- (a) **Operator-triggered, idempotent, never overwrite** (recommended). Empty `implements:` left for operator to fill.
- (b) Operator-triggered; attempt to infer `implements:` from legacy `success_criteria` text via heuristic regex on `V##` / `D##` tokens.
- (c) Auto-run on first `eawf spec lint` after C03 ships, with a confirmation prompt.

### Q13 — Loose-path tolerance on `tests` regex — RATIFIED

**Question.** Should `TestRef` accept extensions beyond `py|svg|json|md`?

**Operator pick (2026-05-16 /blitz).** **(b) Loose under `tests/`** — pattern `^tests/.+$`, any extension under `tests/`. Override of brief's original recommendation (a). Rationale: future test artefacts (asciinema casts, `.txt` diff baselines, perf-bench reports) avoid validator churn. Path-existence check in WSV-05 / WSV-06 still rejects phantom files at validate + pre-commit time, so the loosened regex does not weaken the real-path guarantee.

**Other options considered.**
- (a) Tight `py|svg|json|md` — original recommendation; bumps the regex every time a new test format appears.
- (c) Accept `tests/` + `.ea/golden/` — would split test-root authority; deferred unless C09 introduces a golden-only test surface.

### Q14 — PhaseSpec.iter_ids vs state.phases[id].iter_ids

**Question.** Does PhaseSpec own its `iter_ids` list, or does it mirror state?

**Options.**
- (a) **PhaseSpec.iter_ids is authoritative when the parent phase is PLANNED** (recommended, matches mutability tier [11]); state mirrors it on activate.
- (b) State is always authoritative; PhaseSpec.iter_ids is a derived display field.
- (c) Both writable; loader picks state when they diverge.

## 9. References

[1] `.ea/local/research/long-term/2026-05-16-c00-spec-index.md` — C00 spec architecture index; V1..V8 + cluster catalog; C03 scope at [1:430-479]
[2] `.ea/local/research/long-term/2026-05-16-c01-foundations.md` — C01 Foundations; Spec entity §5.3.15 [2:624-664]; Spec lifecycle §5.4.15 [2:1125-1151]; D1..D8 decision matrix
[3] `.ea/local/research/long-term/2026-05-16-c02-daemon-topology.md` — C02 Daemon spine; spec-cache reservation [3:77]; per-OS service registration §5.10 [3:492]
[4] `.ea/local/research/2026-05-16-p20-tui-design-direction.md` — P20 TUI rebuild direction; §"Spec storage proposal" [4:230-282]; §"Anti-error mechanisms" [4:286-294]; §"Critical contracts" [4:584-585]; RC-1..RC-9 [4:83-103]
[5] `.ea/local/research/2026-05-14-p20-tui-verdicts.md` — P20 V1-V24 verdicts (V12 glyph schema, V07 audit overlay, V11 wave palette)
[6] `.ea/local/research/2026-05-11-tui-layout.md` — TUI layout brief (chassis baseline)
[7] `.ea/local/research/2026-05-11-tui-ux-resolved.md` — TUI UX-resolved decisions (D17 iter-prefix rule)
[8] `.ea/local/research/2026-05-15-ea-framework-manifesto.md` — Eä manifesto (Rule 4: AGENTS.md canonical contract; Rule 7: verify before claim)
[9] `.ea/local/research/2026-05-15-long-term-features-deep.md` — long-term features; cache-control + bio-memory (informs C09 metrics but not C03 directly)
[10] `src/eawf/state/models.py` — current Pydantic state models; Wave [10:221-241]; Hypothesis [10:244-256]; Audit [10:259-271]; Decision [10:294-305]; Phase [10:190-204]; Iter [10:207-218]; State.schema_version [10:492]
[11] `AGENTS.md` — non-negotiable rules (Rule 1 CLI dispatch, Rule 2 Pydantic forbid-extra, Rule 4 state CLI is the only mutator, Rule 7 verify before claiming, Rule 17 naming conventions, Rule 18 chassis + citations, Rule 20 planned-scope revisability)
[12] `tools/commit_prefix_lint.py` — commit subject + diff scope linter; `_STATE_ONLY_PREFIXES` includes `.ea/specs/` [12:61]
[13] `src/eawf/profiles/models.py` — current Profile schema (ProfileBody, ComposedProfile); foundation for C08 profile composition
[14] `src/eawf/registry/` — workspace + cross-repo registry surface
[15] `src/eawf/state/ids.py` — PROJECT / PHASE / ITER / WAVE / HYPOTHESIS id regex patterns
[16] `src/eawf/cli/commands/lifecycle.py` — current wave/iter/phase verbs; `wave_plan_cmd` [16:1347]; `wave_claim_cmd` [16:1441]; verbs map to library functions per AGENTS rule 1
[17] `src/eawf/state/urn.py` — URN parser; `URN_KINDS`; `_SLASH_KINDS`; `identity()`
[18] `src/eawf/cli/commands/roadmap.py` — current `eawf roadmap propose|revise|apply|drop` surface; PhaseSpec init draws from `roadmap propose` arguments
[19] `src/eawf/cli/commands/research.py` — current `eawf research init` + brief authoring surface; ResearchBriefSpec extends the existing chassis here
[20] `src/eawf/cli/commands/wave_ci.py` — current `eawf wave audit-run` + CI hooks; integration point for `verify_implements` kind
[21] `src/eawf/audit_dsl/__init__.py` — DSL entry-point; `load_spec`, `run`
[22] `src/eawf/audit_dsl/runner.py` — check-dispatch loop; register new kinds in `CHECK_REGISTRY`
[23] `src/eawf/agent_report/` — typed agent report contract; `AgentReportBody` Pydantic union per role
[24] `src/eawf/render/envelope.py` — `EnvelopeHeader` shape; spec render produces an envelope-aware body
[25] `src/eawf/audit_dsl/models.py` — `CheckKind` Literal [25:30-38]; `CheckSpec` [25:41-52]; `CheckResult` [25:55-65]; `CheckFile` [25:69-77]
[26] `src/eawf/render/artifact_chassis.py` — `render_references` / `render_provenance` / `render_scrub_status`; spec render hooks here for chassis sections
[27] `src/eawf/state/enums.py` — `AuditKind`, `AuditStatus`, `AgentSessionRole`, `EffortBucket`, `HypothesisVerdict`, `WaveStatus`, `IterStatus`, `PhaseStatus`
[28] `.pre-commit-config.yaml` — pre-commit pipeline (D5 second layer registers there)
[29] `src/eawf/render/research.py` — existing research-brief renderer; ResearchBriefSpec validates the frontmatter the renderer already consumes
[30] `https://textual.textualize.io/guide/reactivity/` — Textual reactive guide (TUI side; consumed by C06 not C03)

## 10. Provenance

- `store_record=none (local-only research)`
- `commit=3b86f7a (parent at session start; revisions 2026-05-18)`
- `supersedes=none`
- `last_revised=2026-05-18 (audit-driven: D8 reversed to global Literal["1.0"] per Q5/BOT-03; spec writer ownership = daemon per Q1; URN_KINDS expansion is hard precondition per XB15; spec writer enforces PENDING-only parent per CL03.F-STATUS-CHECK; backfill default fixed per Codex C03-I005; status_meta defined per Codex C03-I006; verify_implements supports scope patterns per Codex C03-I008)`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md (1 BLOCKER from C01; 12 Codex issues)`
- `session=eawf-spec-c03-spec-infrastructure-2026-05-16`
- `authority_binding=Q1 (2026-05-18): daemon = sole writer for .ea/specs/<phase>/[<iter>/]<wave|spec>.md + spec-cache. Spec lifecycle status (DRAFT/READY/IMPLEMENTED/ARCHIVED) lives in state.json (durable lifecycle source picked per G3).`
- `operator_decisions_locked=2026-05-16 /blitz Q1..Q14 (per below); 2026-05-18 Q1+Q5+Q9 supersedes`
- `2026-05-16 /blitz Q1..Q14 — Q1 medium PhaseSpec; Q2 IterSpec full first-class; Q3 ASCII + optional Mermaid; Q4 typed triple, line optional; Q5 Pydantic + pre-commit dual layer; Q6 freeze C01 §5.4.15 DAG; Q7 count parity only; Q8 ~~per-kind Literal[1]~~ **reversed 2026-05-18 to global Literal["1.0"]**; Q9 md + json + diff; **Q10 configurable AuditSpec.cadence**; Q11 path-prefix + waiver; Q12 operator-triggered idempotent backfill; **Q13 loose ^tests/.+$**; Q14 spec authoritative when PLANNED`
- `dependencies=C00 (V1..V9 ratified 2026-05-16; V9 added) + C01 (D1..D8 ratified; URN_KINDS expansion landed) + C02 (daemon = canonical writer per Q1)`

## 11. Scrub

- status: clean
- references: repo-relative + external URL only
- local paths: none
- real emails: none (canonical author block only when promoted to .ea/artifacts/)
- abstract placeholder names: not applicable (no mockup repos cited)
- secret patterns: none
