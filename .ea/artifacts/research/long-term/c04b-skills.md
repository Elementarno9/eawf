# C04b — Skill manifests + envelope contract — Eä framework long-term specs

**Cluster:** C04b (Skill manifests + envelope contract + 6 missing skills per Q9 / XB16)
**Title:** Skill manifests
**Status:** `accepted` (split per Q19; 6 missing skills landed per Q9; 2026-05-18)
**Depends on:** C00 (V1..V9), C01 (foundations), C04 (parent), C07a (runtime adapter contract)
**Consumed by:** C05 (CLI), C06 (TUI), C07a (plugin manifest)

## 1. Purpose + scope statement

C04b owns the **skill manifest schema** + the **envelope contract** + the **6 previously-missing skills** that XB16 / Q9 land inline.

**Six missing skills landed inline (per XB16 / Q9 / D-02 fix 2026-05-18):**

1. **`/coauthor`** — manages the `Co-Authored-By:` trailer policy per repo. Modes: `runtime` (default — runtime-determined), `project` (per-repo override), `disabled` (no trailer). State mutation: `Project.coauthor_mode`. Escalation: `coauthor resolve` AskUserQuestion when policy ambiguous.
2. **`/memory`** — read/write/list memory records (`MemoryPayload`). Tier: `WORKING|ARCHIVAL|RETRIEVAL`. Append-only emit via daemon. Verbs: `/memory save <name>`, `/memory list`, `/memory forget <name>`.
3. **`/agent-dispatch`** — dispatches a wave to a runtime per V8 hybrid session reuse. Routes via daemon `agent.dispatch` RPC. Reads `Wave.runtime_preference` ladder.
4. **`/compress`** — compresses session conversation when context approaches limit. Emits `compression_emitted` event with token counts before/after. Wires to V8 cache-control hooks.
5. **`/wave-spec`** — scaffolds WaveSpec for a claimed wave (Mockup waiver path per C03 D11). Verbs: `/wave-spec init <wave-id>`, `/wave-spec validate <wave-id>`.
6. **`/security-review`** — runs security-audit DSL against a closed scope. Emits audit envelopes per C03 §5.6. Required for `phase close` when profile = security (C08 contributes).

## 2. Goals + non-goals

- Skill manifest is `PluginManifest(BaseModel)` with `extra="forbid"` + `schema_version: Literal["1.0"]` (per XB19 / Q5).
- Envelope status enum frozen: `ok | needs_user | blocked | failed | partial` (per BOT-10 / Codex C04-I006; `partial` ratified into the closed set).
- All 17 skills (11 from original C04 + 6 from Q9 landing) listed in the canonical catalog at §5.1.

## 3. Prior verdicts cited

V1..V9 from C00.

## 4. Decision matrix

| # | Axis | Recommendation | Rationale |
|---|---|---|---|
| **D-b1** | Envelope status enum closed set | `ok | needs_user | blocked | failed | partial` (5) — `partial` ratified | Per Codex C04-I006: freeze envelope enum once. Original 4-value contract grew to 5 with `partial` for cost-ledger surfaces. Ratify; do not let it grow. |
| **D-b2** | Skill manifest runtime field | `runtime: list[str]` (subset visibility) | Single source of truth; aligns with C07a PluginManifest. Drops the `visibility.runtimes` alternate per Codex C04-I010. |
| **D-b3** | `target_dir` rename | `output_dir` (per BOT-06 / Claude C04.F58) | Aligns with naming-conventions canon. |
| **D-b4** | Six missing skills home | Inline in C04b (per Q9) | Per XB16 / D-02: author inline rather than defer; closes the C00 17-skill catalog drift. |

## 5. Body — skill manifest schema

```python
# src/eawf/runtimes/plugin_manifest.py (cross-ref with C07a §5.7)
from typing import Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field

EnvelopeStatus = Literal["ok", "needs_user", "blocked", "failed", "partial"]   # 5-value closed per D-b1

class SkillManifest(BaseModel):
    """Per-skill manifest body inside PluginManifest.contributes.skills."""
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str
    runtime: list[Literal["claude-code", "codex", "opencode"]]   # per D-b2
    dispatch: dict[str, str | bool | int] = {}                    # session_policy, model_hint, etc.
    output_envelope_kind: str                                       # per envelope catalog
```

Skill catalog (17 total) — see §5.1 of parent C04 brief; 6 missing added per D-b4.

## 6. Failure modes

- `F-b01` Skill envelope status outside the closed 5-value set → `ValidationError` at envelope emit.
- `F-b02` Skill manifest with non-Literal runtime → loader rejects.
- `F-b03` Six-missing-skills referenced before C04b ratification → C05 verb-noun matrix raises envelope `status=blocked` with `repair_commands=["wait for C04b ratification"]` (closed once C04b ratifies on 2026-05-18 split).

## 7. Migration plan

`SkillManifest.schema_version` "1" → "1.0" (per Q5 / BOT-03); migrator zero-transform.

## 8. Open questions

- Q-b1 — Carry forward Codex C04-I011 reorder support to v0.5+.

## 9. References

[1] Parent C04 `2026-05-16-c04-workflow-skills.md`.
[2] C07a `2026-05-16-c07a-runtime-skill-dispatch.md` §5.7 (PluginManifest BaseModel; XB19 fix).
[3] `2026-05-17-spec-series-combined-audit.md` §XB16 / Q9.

## 10. Provenance + Scrub

### Provenance

- `store_record=none (local-only research brief)`
- `commit=3b86f7a (parent)`
- `cluster=C04b (split from C04 per Q19 2026-05-18; 6 missing skills landed inline per Q9)`
- `consumes=C00..C01, C04 (parent), C07a`
- `supersedes=none`
- `session=eawf-spec-c04b-skills-2026-05-18`
- `last_revised=2026-05-18`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md`
- `authority_binding=Q1 (2026-05-18): daemon = sole writer for skill-emitted state mutations.`

### Scrub

- status: clean
- references: repo-relative only
- local paths: none
- real emails: none
