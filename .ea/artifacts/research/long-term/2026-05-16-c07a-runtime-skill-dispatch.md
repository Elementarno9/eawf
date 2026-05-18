# C07a — Runtime / Skill / Agent dispatch — Eä framework long-term specs

**Cluster:** C07a (Subsystems — runtime adapters, skill model, agent dispatch; V5 + V8 verdicts live here)
**Title:** Runtime / Skill / Agent dispatch
**Status:** `local-draft`, `needs-user` (pending operator ratification of §8 open questions)
**Created:** `2026-05-16T00:00:00Z`
**Author:** `claude-opus-4-7`
**Depends on:** C00 (V1..V8 locked) [1]; C01 (entity catalog, URN scheme, persona matrix) [2]; C02 (daemon IPC, runtime-fallback state machine, session-handle tracking schema) [3]
**Consumed by:** C05 (CLI), C06 (TUI/web), C09 (telemetry projection); paired with C07b (VCS / worktree / registry / event / render / brand)

## 1. Purpose + scope statement

C07a locks the three cross-cutting subsystems that turn an eawf wave dispatch into a real LLM call against a real runtime: the **plugin/runtime architecture** (B5), the **skill model** (B6), and the **agent dispatch surface** (B7). It is the home for the V5 (reactive runtime fallback) and V8 (hybrid session reuse) verdicts because both make their teeth here — the daemon's fallback state machine and session-handle table both consume per-runtime adapter contracts defined in this brief.

**In scope (C00 §C07 [1:648-712]).**

- **Plugin/runtime architecture (B5).** Per-runtime adapter protocol (`RuntimeAdapter` Protocol — `open_session`, `continue_session`, `session_log_path`, `parse_error`, `supports_continue`, `supports_cache_control`). Plugin manifest schema at `build/<runtime>-plugin/manifest.yaml`. AGENTS.md sync to per-runtime config artifacts (`.claude/`, `.codex/`, `.opencode/`). Capability matrix covering: skills, plan-mode, tool use, sub-agents, streaming, session-resume per V8, cache-control per V8, error-class surface per V5.
- **Skill model (B6).** Skill registry, dispatch shape, output envelope contract cross-referenced from C04 [1:485-534]. C07a documents the *subsystem* (the `SKILL_REGISTRY` constant, the per-runtime `.../<skill>/SKILL.md` render path, the manifest `dispatch.session_policy` field that ties B6 to V8); C04 owns the per-skill semantics.
- **Agent dispatch (B7) — V8 hybrid session reuse.** Per-runtime session-handle adapter API; per-runtime session-log path catalog; hybrid dispatch routing (`skill.manifest.dispatch.session_policy` default `hybrid`; per-`(wave_id, attempt_id)` routing via the daemon session-handle table; `--continue` failure falls back to fresh with `DispatchAnnotation`); full SDK tradeoff matrix (subprocess CLI primary; SDK deferred to v0.5+ unless BYOK relaxes); cross-runtime error-class normalization (V5); per-runtime cache-control hooks (V8 cache interplay).

**Out of scope.**

- **TUI rendering details (C06 [1:587-644]).** Wave board, runtime-status pane, session-log tail overlay — all rendered surfaces. C07a names what the daemon emits; C06 names how the operator sees it.
- **CLI verb surface (C05 [1:539-583]).** `eawf agent dispatch`, `eawf runtime list`, `eawf wave switch` — surface names locked here implicitly by the daemon method catalog [3:340-345]; C05 owns the verb-noun matrix.
- **Per-runtime skill plugin code (C04 [1:485-534]).** C07a names *that* the registry exists and the manifest carries `dispatch.*`; C04 names *which* skills and *what* contract each emits.
- **VCS / worktree / registry / event / render / brand (C07b).** Those six subsystems share this cluster but stay in C07b to keep each half under the C00 ~1500-line target [1:998].
- **Telemetry projection schema (C09 [1:769-841]).** C07a emits `runtime_switched` / `session_continued` / `session_failover` events; C09 projects them into the DuckDB metrics catalog.

## 2. Goals + non-goals

### Goals

| G# | Goal | Source |
|---|---|---|
| G1 | Single `RuntimeAdapter` Protocol every runtime implements, so the daemon's fallback handler (V5) and session-handle router (V8) consume one shape, not three. | C00 V5 [1:127-151], V8 [1:226-271]; C02 [3:889-899] |
| G2 | Per-runtime session-handle path catalog covers all three v0.3-v0.5 runtimes (Claude Code / Codex CLI / OpenCode) with verified on-disk path strings. | C00 V8 [1:243-248] |
| G3 | Cross-runtime error-class normalization: every adapter surfaces one of five canonical classes (`RUNTIME_RATE_LIMIT`, `RUNTIME_SERVER_ERROR`, `RUNTIME_TIMEOUT`, `RUNTIME_API_ERROR`, `RUNTIME_AUTH_ERROR`) so the daemon's fallback state machine reads uniformly. | C00 V5 [1:130]; C02 §5.12 [3:766-774] |
| G4 | SDK tradeoff matrix with concrete row per runtime + concrete col per dimension; final pick recorded (subprocess CLI primary, SDK deferred). | C00 V8 [1:256-262] |
| G5 | Hybrid dispatch routing algorithm specified end-to-end: skill manifest → daemon session-handle table → fresh-vs-continue branch → `--continue` failure fallback. | C00 V8 [1:226-271]; C02 §5.13 [3:843-887] |
| G6 | Cache-control hooks per runtime: which runtimes accept `cache_control` markers, where the dispatch prefix lives, how the mis-layer regression alarm [5:599-605] surfaces. | C00 V8 [1:251-254]; long-term-features-deep §605 [5:599-605] |
| G7 | Plugin manifest YAML schema locked (single `build/<runtime>-plugin/manifest.yaml` shape feeds all three render paths). | C00 §C07 axes [1:682] |
| G8 | AGENTS.md sync triggers enumerated: when AGENTS.md updates → which artifacts regenerate per runtime. | C00 §C07 axes [1:684] |
| G9 | Capability matrix covers eight rows × three runtimes × per-version drift detection. | C00 §C07 axes [1:683] |
| G10 | Skill subsystem boundary vs C04: C07a owns registry shape + manifest schema; C04 owns per-skill algorithm + envelope status transitions. | C00 §C07 NG3 mention [1:664-666] |

### Non-goals

| NG# | Non-goal | Why deferred |
|---|---|---|
| NG1 | Per-skill input/output contract or envelope `status` transitions. | C04 owns it [1:485-534]. |
| NG2 | TUI runtime-status pane / session-log tail / wave board widgets. | C06 owns it [1:587-644]. |
| NG3 | DuckDB schema for runtime telemetry projection. | C09 owns it [1:791-806]. |
| NG4 | Daemon IPC framing (JSON-RPC vs gRPC) or auth model. | C02 owns it [3:230-345]. |
| NG5 | New runtime adapter (Aider / Cursor / Cline / Roo). | C00 V4 cluster-sequential locks Claude + Codex + OpenCode for v0.3-v0.5; new runtimes deferred to v0.5+ per harness-adapters baseline [9]. |
| NG6 | Native SDK adoption (`claude-agent-sdk` BYOK). | V8 [1:259-262] defers until BYOK constraint relaxes; subprocess CLI primary in v0.3-v0.5. |
| NG7 | Multi-user / cross-machine dispatch. | C02 [3:380] locks single-user daemon for v0.3-v0.5. |
| NG8 | MCP server provisioning (separate subsystem). | MCP installer lives in `src/eawf/mcp/` [10]; manifest schema for MCP grants stays C08-and-C07a-aware but the MCP server lifecycle itself sits with C09 / C10. |

## 3. Prior verdicts cited

### V1 — eawfd daemon Day-1 + smart-spawn writer [1:24-53]

> "Mutations to `state.json` (and all future stateful surfaces — config layers, registry, event log) route through the eawfd daemon."

**C07a binding.** Agent dispatch goes through `agent.dispatch` RPC on the daemon [3:316]. The session-handle table (`Wave.sessions: dict[attempt, SessionAttempt]` per C01 §5.3.5 [2:362-381]) is daemon-written; CLI never touches it directly. Runtime adapters are loaded inside the daemon process; CLI is a thin client that translates `eawf agent dispatch <wave>` into the RPC call.

### V2 — Three-tier specs [1:55-74]

> "Each scope level carries its own typed spec ... WaveSpec — wave deliverable: verdict citations from `implements:`, file scopes, behaviors, failure modes, tests, mockup."

**C07a binding.** The dispatch prefix the daemon hands to every runtime carries the WaveSpec body as the task description. The prefix shape is the per-skill manifest `prompt_template`; B5 § 5.7 documents the manifest schema; C03 owns the WaveSpec shape rendered into the prefix.

### V3 — Composable profile bundle [1:76-96]

> "Each profile declares `conflicts_with: [...]` and `overrides: [...]`. ... Profile contributions per profile: Default skill set (which `/skills` are enabled), Default hooks (pre-commit, prepare-commit-msg, commit-msg)."

**C07a binding.** Per-profile skill enable/disable goes through the `Profile.contributes.skills` field [2:599-616]. C07a documents that the skill registry is *profile-gated*: `SkillSpec.profile_required: list[str]` filters the registry at composition time. C08 owns the conflict/override algorithm.

### V5 — Runtime fallback: reactive switchover on error [1:127-151]

> "Daemon uses reactive auto-switch on primary-runtime failure (HTTP 429 / 5xx / timeout / API-error). ... daemon flips the affected wave to the next runtime in the configured preference ladder and re-issues the dispatch envelope against that runtime with the idempotency key preserved."

**C07a binding.** This is the load-bearing verdict for §5.5 (error-class normalization) and §5.1 (`RuntimeAdapter.parse_error` returning one of the canonical class strings). Without uniform error-class surface, the daemon would have to decode per-runtime error strings, which is the regression V5 prevents. The state machine itself sits in C02 §5.12 [3:720-790]; the per-adapter `parse_error` contract is here.

### V8 — Agent dispatch: hybrid session reuse [1:226-271]

> "Fresh process per new wave dispatch — clean context, full KV-cache hit on the stable prefix, deterministic token cost. Reuse session (`claude --continue <session-id>` / Codex `--resume` / OpenCode equivalent) on retry / edit / follow-up against the same wave — preserves turn history, avoids re-explaining state."

> "Per-runtime session-handle paths. Claude Code: `<local-path>`. Codex CLI: `<local-path>`. OpenCode: vendor-specific."

> "Final pick: subprocess CLI per runtime as primary path; SDK adoption deferred unless BYOK constraint relaxes."

**C07a binding.** Three concrete impacts:

1. §5.4 catalogues the on-disk session-log path per runtime (verifying the C00 conjectured paths against current code + vendor docs).
2. §5.8 specifies the hybrid dispatch routing algorithm (`skill.manifest.dispatch.session_policy` default `hybrid`; daemon consults `Wave.sessions` table to decide fresh vs continue; `--continue` failure fall-through to fresh with `DispatchAnnotation` per C02 §5.13 [3:843-887]).
3. §5.2 records the SDK tradeoff matrix and the final pick (subprocess CLI primary; SDK adoption deferred to v0.5+; gate is BYOK relaxation per [7:21-22, 100-102] and the v0.5 release window).

### V9 — Native per-runtime plugins remain first-class distribution channel [1:287-329] (added 2026-05-18 per XB10)

> "Per-runtime plugin manifests (Claude `.claude/plugins/eawf/`, Codex `<local-path>`, OpenCode `<local-path>`) remain the canonical distribution channel even after V1 daemon. Plugin sync regenerates manifests deterministically from `SKILL_REGISTRY`. Plugin doctor reports drift."

**C07a binding.** Four impacts:

1. §5.7 specifies the PluginManifest schema (**now BaseModel with `extra="forbid"` per XB19; was incorrectly described as Pydantic but defined as `@dataclass(frozen=True)`**).
2. §5.8 plugin sync verb renamed `eawf plugin sync` (canonical) — `plugin install --regenerate` deprecated per Claude C07a.F13.
3. §5.9 plugin doctor enumerates 4 drift kinds per Claude C07a.F14.
4. Adoption gate enumerated: (a) PluginManifest schema landed; (b) `eawf plugin sync` shipped; (c) doctor returns no drift; (d) AGENTS.md hash sync verified.

## 4. Decision matrix

Locked / proposed decisions for this cluster. Operator AUQ rounds will ratify §8 open questions; the rows below carry an inline recommendation per the C00 §"Decision matrix" template [1:977].

| # | Axis | Options | Recommendation | Rationale |
|---|---|---|---|---|
| **D1** | Adapter protocol surface | (a) class-per-runtime base class; (b) `Protocol` duck-typed; (c) ABC + module-level functions | **(b) `Protocol`** (matches C02 §5.13 sketch [3:889-899]) | Daemon already imports adapters by id (`runtime.preference: [claude-code, codex, opencode]`); a Protocol lets the v0.5+ Aider/Cursor adapters bolt in without inheritance. Mirrors Python stdlib style; no runtime `isinstance` cost. |
| **D2** | SDK adoption gate | (a) ship subprocess CLI only; (b) ship subprocess + SDK side by side; (c) defer SDK-primary to v0.5; v0.3 subprocess-primary; v0.4 budget-aware dispatch | **(c)** subprocess CLI primary v0.3; SDK-primary deferred to v0.5; v0.4 adds budget-aware dispatch | V8 [1:259-262]. **2026-06-15 release date is a forecast, not a shipped fact** (revised 2026-05-18 per XB04 / G7 — current date is 2026-05-17, the release has not yet happened; details cited may be inaccurate; gate adoption on a fresh capability probe after the release date). Original projection: subscription SDK ships with API-rate credit pools ($20 Pro / $100 Max-5x / $200 Max-20x / per-seat Team); `claude -p` *also* swept into the same pool — subprocess subscription advantage expires that date. SDK has feature parity with `claude -p` for programmatic session control via `SessionStore` adapter. BYOK still default + supported. v0.3 stays subprocess-primary (existing adapter); v0.4 adds per-user `$ /mo` budget broker + halt-or-warn; v0.5 flips to SDK-primary when (a) operator picks BYOK billing OR (b) Anthropic raises Pro credit ≥ $100 OR (c) Anthropic ships subscription-OAuth without API-rate metering. **Reprobe gate (XB04 / G7):** re-run Codex / Claude / OpenCode capability probes after 2026-06-15; update V8 matrix; ratify cluster decision. |
| **D3** | Error-class enum closed set | (a) five classes; (b) per-runtime error-string passthrough; (c) hybrid (five + `runtime_raw: str`) | **(c) hybrid** | Five classes drive the fallback state machine [3:766-774]; raw error preserved on the `RuntimeSwitchedPayload.error_detail` field [3:786] (scrubbed for PII) so operator-side replay can diagnose without re-running. Closed five-class set keeps daemon switch-statement small. |
| **D4** | Cache-control surface | (a) eawf injects `cache_control` markers in dispatch prefix; (b) runtime adapter passes prefix through unchanged; (c) hybrid (Claude native; Codex/OpenCode TBD) | **(c)** Claude native API gets the marker [1:251-253]; Codex + OpenCode adapter pass prefix through; mis-layer alarm fires on `cache_creation_input_tokens >> cache_read_input_tokens` for 2 dispatches in 5 minutes [5:599-605] | Claude is the only v0.3-v0.5 runtime that documents the marker today. Codex + OpenCode session-resume covers the equivalent (re-use cached prefix on `--continue`). Verify the cache-control accept story per runtime; if Codex/OpenCode add the field, mirror Claude in v0.4. |
| **D5** | Session-policy default per profile | (a) global `hybrid`; (b) per-profile (research `continue`, engineering `fresh`); (c) per-skill manifest | **(c) per-skill manifest** with default `hybrid`; profile-conditional overrides allowed | V8 explicit [1:268-269]: research-profile skills may default `continue`; engineering-profile skills may default `fresh`. The manifest field is the override point; profile composition layers a default. Operator can pin per-wave via `eawf wave dispatch --session-policy fresh`. |
| **D6** | Plugin manifest format | (a) YAML at `build/<runtime>-plugin/manifest.yaml`; (b) Python entry-point; (c) per-runtime native format (`.codex-plugin/plugin.json`) only | **(a) YAML at `build/<runtime>-plugin/manifest.yaml`** as the *canonical* shape; per-runtime native format is the *rendered* output | Manifest stays one shape across runtimes; the renderer translates to `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, `.opencode/plugins/eawf.js` (the OpenCode adapter intentionally has no manifest — `eawf.js` is the entry point [10]). Per-runtime native is reachable as the install output, never as the eawf-internal source-of-truth. |
| **D7** | AGENTS.md sync trigger | (a) on every commit touching AGENTS.md; (b) on `eawf plugin install` only; (c) hybrid | **(c) hybrid** — AGENTS.md commit invalidates the manifest hash; `eawf plugin doctor` detects drift; operator runs `eawf plugin install <runtime> --regenerate` | AGENTS.md edits land on the long-running phase branch; per-runtime artifacts (`.claude/agents/*.md`, etc.) regenerate from AGENTS.md content. The `__eawf_managed.body_hash` carries the AGENTS.md SHA so drift is detectable without scanning every file [10]. |
| **D8** | Capability matrix versioning | (a) frozen at v0.3; (b) per-minor-bump; (c) per-runtime-version-skew | **(b)** | Matrix lives at `src/eawf/runtimes/capabilities.yaml`; bump on every minor cluster brief. Drift between matrix and live runtime detected by `eawf doctor --runtime <id>`. |
| **D9** | Dispatch CLI invocation shape | (a) `eawf agent dispatch <wave-id>`; (b) `eawf wave dispatch <wave-id>`; (c) both | **(a)** primary, `(b)` deprecated alias kept one minor | `agent dispatch` aligns with C01 §5.3.12 AgentSession entity [2:530-551] and the daemon RPC method `agent.dispatch` [3:316]. `wave dispatch` exists today; deprecate gradually. |
| **D10** | Runtime entity in state | (a) `state.runtimes: dict[id, Runtime]` row; (b) per-call config only; (c) reify with `last_health_check` + per-runtime budget | **(a) with status field, no probe** | C01 §5.3.16 [2:666-696] already proposed the Runtime entity; C07a implements. `last_health_check` is *advisory* (V5 is reactive, not probed [1:131-136]). |
| **D11** | OpenCode session-log path | (a) JSON-per-session under config-dir; (b) SQLite under data-dir; (c) in-process only | **(b) SQLite + auxiliary JSON-diffs under DATA-dir** (resolved via blitz brief [20]) | Primary store: `<local-path>` (drizzle ORM; 13 tables incl. `session` + `message` + `part`; WAL mode). Auxiliary: `<local-path>` (file/patch diffs only). Lives under **data**-dir not config-dir. Resume verbs: `opencode run --continue` / `--session <sid>` / `--fork`; `opencode session list/delete`; `opencode export/import`. `supports_continue=True`. |
| **D12** | Codex session-log shape | (a) JSON-per-session `<local-path>`; (b) JSONL like Claude; (c) sharded date tree | **(c) JSONL sharded by date** (resolved via blitz brief [21]) | Path: `<local-path>`. Stem = `rollout-` not `session-`. Schema: line-delimited `{timestamp, type, payload}` envelopes; types include `session_meta` (×1), `turn_context` (×1), `response_item`, `event_msg`. Resume verb: `codex resume <id>` subcommand (NOT `--resume` flag); `codex exec resume <id>` for non-interactive; `codex fork` for branched sessions. `$CODEX_HOME` env-var overrides `<local-path>` (vendor-doc; not live-verified). |
| **D13** | Skill manifest `dispatch.*` field set | (a) `session_policy` only; (b) full block (`session_policy`, `cache_prefix_paths`, `model_hint`, `effort_bucket`); (c) extensible dict | **(b)** | Skills need to declare more than session policy — model hint (Sonnet vs Opus per skill), cache-prefix paths (which AGENTS.md regions are stable), effort bucket (1 EU vs 5 EU). Closed shape keeps validator strict; v0.4 extends if needed. |

## 5. Proposed schemas, vocabulary, lifecycle

### 5.1 RuntimeAdapter Protocol

Every runtime adapter implements one `Protocol`. The daemon loads adapters from `src/eawf/runtimes/<id>/adapter.py` (new file, one per runtime); the existing plugin-install code [10] stays under each runtime's package.

```python
# src/eawf/runtimes/adapter.py — new module, library home
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from eawf.state.models import Wave
from eawf.state.types import UtcDatetime


@runtime_checkable
class RuntimeAdapter(Protocol):
    """Per-runtime dispatcher contract."""

    id: str                                          # 'claude-code' | 'codex' | 'opencode'
    cli_binary: str                                  # 'claude' | 'codex' | 'opencode'
    accepts_continue: bool                           # V8 [1:228-232]
    supports_cache_control: bool                     # V8 [1:251-254]
    error_classes_emitted: tuple[str, ...]           # V5 [1:130]

    async def open_session(
        self,
        wave: Wave,
        prompt: str,
        *,
        cache_prefix: str | None = None,
        model_hint: str | None = None,
    ) -> "SessionAttempt": ...

    async def continue_session(
        self,
        session_id: str,
        prompt: str,
    ) -> "SessionAttempt": ...

    def session_log_path(
        self,
        session_id: str,
        *,
        repo_root: Path | None = None,
    ) -> Path: ...

    def parse_error(
        self,
        exit_status: int,
        stderr: bytes,
    ) -> str: ...  # returns one of RUNTIME_RATE_LIMIT|RUNTIME_SERVER_ERROR|...

    def supports_continue(self) -> bool: ...
```

`SessionAttempt` is the shape from C02 §5.13 [3:809-822] — verbatim — so the daemon writes through the typed state-mutator path and the adapter never owns its own model.

### 5.2 SDK tradeoff matrix

Per V8 [1:256-262] — full table. Rows = runtimes; cols = decision dimensions. **Final pick locked in §4 D2: subprocess CLI primary across all three runtimes; SDK deferred to v0.5+ unless BYOK relaxes.**

| Dimension | Claude (CLI = `claude -p`) | Claude (SDK = `claude-agent-sdk`) | Codex (CLI = `codex exec`) | OpenCode (CLI = `opencode run`) |
|---|---|---|---|---|
| Auth model | OAuth Pro/Max subscription (subscription-billed) | BYOK Anthropic API key (per-token-billed) | OAuth ChatGPT Pro (subscription-billed) | per-vendor (OpenAI / Anthropic key) |
| Cache-control surface | `cache_control` markers via system-prompt regions (CLI accepts pre-injected markers in stable prefix) | full programmatic `cache_control` control [5:201-249] | **no caller-side marker** — OpenAI prompt caching automatic at ≥1024-token threshold; only knobs are API-level `prompt_cache_key` (routing bias) + `prompt_cache_retention` (`in_memory`/`24h`) [21] | **no caller-side marker** — OpenCode internally injects `cache_control:{type:ephemeral}` via `@ai-sdk/anthropic`; session-id is the cache key; live risk: upstream issue [20:#17910] strips marker on OAuth-Claude auth path since 2026-03-17 |
| Tool-allow surface | `--allowedTools` flag + `.claude/settings.json` `mcpServers` | SDK `allowed_tools=[...]` param; `mcp_servers=[...]` block | `--allowed-tool` flag + `.codex/config.toml [tools]` block | `permission:` frontmatter on agent files + `opencode.json` MCP block |
| Wire-form | stdio JSON Lines + arg flags | Python `await client.run(...)` | stdio JSON Lines + arg flags | stdio JSON Lines + arg flags |
| BYOK requirement | **no until 2026-06-15** — Pro/Max subscriber cost = $0 marginal pre-cutover; **post-2026-06-15** subscriber credit pool ($20 Pro / $100 Max-5x / $200 Max-20x) applies per blitz r3 [23] | **partial** — API key supported; subscription path via credit pool from 2026-06-15 (same pool as `claude -p`) | **no** for ChatGPT Pro subscriber; **yes** for plain API | per-vendor (typically yes for non-subscription paths) |
| Session-resume granularity | `--continue <session-id>` resumes full turn history at the JSONL log | `client.resume(session_id)` + cache_control re-injection | `codex resume <session-id>` subcommand (TUI); `codex exec resume <session-id>` (non-interactive); `codex fork <session-id>` (branch); JSONL log under `<local-path>` [21] | `opencode run --continue` / `--session <sid>` / `--fork`; primary store SQLite at `<local-path>`; auxiliary diffs at `storage/session_diff/ses_<sid>.json` [20] |
| Concurrent-spawn cost | **pre-2026-06-15:** $0 marginal (subscription); 5h-window message cap [7:125-126]. **post-2026-06-15:** counts against subscriber credit pool (per-user, no roll-over); overflow opt-in at standard API rates [23] | per-token-billed; from 2026-06-15 also against subscriber credit pool when running under subscription auth [23] | $0 marginal (ChatGPT Pro); equivalent window | per-vendor |
| Suitability for v0.3-v0.5 eawf | **primary** | deferred (BYOK gate) | **primary** | **primary** |

**Adoption gate (§4 D2 + §8 Q5; revised post-blitz r3 [23]).** Subscription SDK *did* ship 2026-06-15 (option b above realized) but the economic model is **API-rate credit pool ($20 Pro / $100 Max-5x / $200 Max-20x), per-user, no roll-over; overflow opt-in at standard API rates**. `claude -p` subprocess swept into the same pool — subprocess-subscription advantage erodes from 2026-06-15. **v0.3 stays subprocess-primary** (existing adapter, no v0.3 ship delay). **v0.4 adds budget-aware dispatch** (per-user `$ /mo` cap with halt-or-warn). **v0.5 flips to SDK-primary** when *any* of: (a) operator switches to BYOK billing of choice; (b) Anthropic raises Pro credit ≥ $100 (10× current); (c) Anthropic ships subscription-OAuth path without API-rate metering (subsidized programmatic use). Until v0.5+ pick fires, subprocess CLI stays the primary adapter the daemon spawns.

### 5.3 Skill model — boundary spec with C04

C07a owns the **subsystem shape**; C04 owns **per-skill semantics**.

**Skill registry.** Today's `SKILL_REGISTRY` [11] is a frozen tuple of `SkillSpec` rows. C07a extends `SkillSpec` with the `dispatch` block:

```python
# src/eawf/render/skills.py extension
@dataclass(frozen=True)
class SkillSpec:
    # ... existing fields (name, description, model, body, ...) ...
    dispatch: "SkillDispatchManifest" = field(default_factory=lambda: SkillDispatchManifest())
    profile_required: tuple[str, ...] = ()       # V3 profile-gate [1:76-96]
    runtimes: tuple[str, ...] = ()               # subset of {'claude-code', 'codex', 'opencode'}; empty = all

@dataclass(frozen=True)
class SkillDispatchManifest:
    """V8 dispatch policy block — D13."""
    session_policy: Literal["fresh", "continue", "hybrid"] = "hybrid"
    cache_prefix_paths: tuple[str, ...] = ("AGENTS.md", ".ea/state.json#digest")
    model_hint: str | None = None                # e.g. 'claude-sonnet-4-6', 'claude-opus-4-7'
    effort_bucket: Literal["XS", "S", "M", "L", "XL"] | None = None
```

**Skill registry storage.** Static frozen-tuple under `src/eawf/render/skills.py` for built-ins (matching today). Profile-contributed skills live under `<local-path>` per V3 [2:599-616]; the composition loader (C08) merges built-in + profile-contributed at startup.

**Boundary with C04.**
- C07a defines the *manifest schema* and the *dispatch block*.
- C04 defines each skill's *body algorithm* (research / prep / flow / audit / ship / polish / blitz / review / coauthor / etc.).
- The skill's `body: str` field carries the renderable markdown template; C04 owns the template; C07a owns the field's existence.

### 5.4 Per-runtime session-handle path catalog

Locked v0.3-v0.5 paths. The daemon's session-handle TTL sweep [3:901] walks these paths; the budget broker reads usage rows from them [7:117-123]; the telemetry projection [1:191] feeds DuckDB from them.

**Claude Code.** `<local-path>`.

- Encoding: repo path is URL-quoted with `/` replaced by `-` per the Claude Code CLI convention (verified at current repo: `<local-path>`).
- One JSONL per session; tail-readable for live token / cache rows.
- `session_id` is a UUID; `claude --continue <session_id>` resumes against the same JSONL.
- Verified against [7:121] and the live filesystem.

**Codex CLI.** `<local-path>` (date-sharded JSONL; resolved via blitz brief [21]).

- Schema: line-delimited `{timestamp, type, payload}` envelopes; `type ∈ {session_meta, turn_context, response_item, event_msg}`. `session_meta` row at line 1 carries the session id + creation context.
- Resume surface: **`codex resume <session-id>`** subcommand (NOT `codex --resume <id>` flag); `codex exec resume <session-id>` for non-interactive; `codex fork <session-id>` for branched sessions.
- `$CODEX_HOME` overrides `<local-path>` per Codex config docs (vendor-doc; not live-verified by blitz).
- Adapter resolution: walk the date-sharded tree on lookup; cache `(session_id → path)` in `SessionAttempt.session_log_path` at dispatch time so subsequent retries skip the walk.

**OpenCode.** Two-store layout (resolved via blitz brief [20]):

- **Primary store:** SQLite at `<local-path>` (drizzle ORM; 13 tables including `session` / `message` / `part` / `session_entry` / `session_share`; WAL mode active with sibling `opencode.db-wal` + `opencode.db-shm`).
- **Auxiliary store:** `<local-path>` — `{file, patch}` diff arrays per session (NOT the message log).
- Lives under **data**-dir (`<local-path>` on macOS/Linux per OpenCode troubleshooting docs), NOT config-dir.
- Resume verbs: `opencode run --continue` (`-c`) / `--session <sid>` (`-s`) / `--fork`; `opencode session list/delete`; `opencode export/import`. `supports_continue=True`.
- Adapter implication: `session_log_path` returns the SQLite path; the daemon's TTL sweep [3:901] queries via the `session` table rather than walking JSONL.

**Daemon-side persistence (C02 §5.13 [3:843-887]).** `state.waves[wave_id].sessions: dict[attempt, SessionAttempt]` stamps the absolute path (`SessionAttempt.session_log_path` [3:813]) at dispatch time; the audit-replay model [2:1318-1325] can resurrect the path even if the runtime adapter changes its convention later.

### 5.5 Error-class normalization (V5)

Each `RuntimeAdapter.parse_error(exit_status, stderr)` returns one of five canonical class strings + the raw stderr (scrubbed) is preserved on the `RuntimeSwitchedPayload.error_detail` field [3:780-787].

```python
# src/eawf/runtimes/error_classes.py — new module
from typing import Final

RUNTIME_RATE_LIMIT: Final[str] = "RUNTIME_RATE_LIMIT"     # HTTP 429
RUNTIME_SERVER_ERROR: Final[str] = "RUNTIME_SERVER_ERROR" # HTTP 500-599
RUNTIME_TIMEOUT: Final[str] = "RUNTIME_TIMEOUT"           # wall-clock or stream timeout
RUNTIME_API_ERROR: Final[str] = "RUNTIME_API_ERROR"       # HTTP 400-499 except 429/401/403
RUNTIME_AUTH_ERROR: Final[str] = "RUNTIME_AUTH_ERROR"     # 401, 403, missing token
```

**Per-runtime parse rules.**

| Class | Claude `claude -p` | Codex `codex exec` | OpenCode `opencode run` |
|---|---|---|---|
| `RUNTIME_RATE_LIMIT` | exit_status=2; stderr matches `429` or `rate_limit_error` | exit_status=2; stderr matches `429` or `rate_limit` | exit_status=2; stderr matches `429` |
| `RUNTIME_SERVER_ERROR` | exit_status=1; stderr matches `5\d\d` or `internal_server_error` or `overloaded_error` | exit_status=1; stderr matches `5\d\d` | exit_status=1; stderr matches `5\d\d` |
| `RUNTIME_TIMEOUT` | SIGTERM from wall-clock cap [3:463]; or stderr matches `timeout` / `deadline_exceeded` | same | same |
| `RUNTIME_API_ERROR` | exit_status=1; stderr matches `4\d\d` other than 429/401/403 | same | same |
| `RUNTIME_AUTH_ERROR` | exit_status=2; stderr matches `401` / `403` / `invalid_api_key` / `oauth_expired` | same plus `chatgpt subscription expired` | per-vendor |

**Fallback policy (C02 §5.12 [3:766-774]).**

| Class | Fallback action |
|---|---|
| `RUNTIME_RATE_LIMIT` | honour `Retry-After` cap 90s; retry same runtime once; fall through on second 429 |
| `RUNTIME_SERVER_ERROR` | immediate fall-through to next runtime in `runtime.preference` |
| `RUNTIME_TIMEOUT` | immediate fall-through |
| `RUNTIME_API_ERROR` | immediate fall-through |
| `RUNTIME_AUTH_ERROR` | **HALT** with `BLOCKED`; emit `runtime_auth_failed` event; never fall through (auth ≠ availability) |

**Idempotency invariant.** Dispatch envelopes carry `idempotency_key: str` (UUID v4) [3:262]. V5 cross-runtime re-issue preserves the key so the daemon's 60-second dedup window prevents double-execution. The new SessionAttempt rows append regardless (each attempt is its own row); the deduplication runs on the *mutation* layer, not the dispatch layer.

### 5.6 Cache-control hooks per runtime (V8 cache interplay)

**Claude Code** (native support). The dispatch prefix carries `<cache_control type="ephemeral" />` markers at four region boundaries: AGENTS.md, state digest, wave spec, dynamic body. Today's adapter [10] passes the prefix through verbatim; the eawf renderer (C03 owns the prefix shape) inserts the marker. Per features-deep [5:201-211] cache lifetime = 5 minutes; the cost-ledger tracks `cache_creation_input_tokens` vs `cache_read_input_tokens` and emits a `cache_mislayer_alarm` event when ratio inverts for 2 dispatches in 5 minutes [5:599-605].

**Codex CLI** (no caller-side marker — confirmed via blitz [21]). OpenAI prompt caching is **fully automatic** at a ≥1024-token threshold; routes by hashing the first ~256 tokens of the prefix. There is no `cache_control` / `ephemeral` marker in the Codex CLI surface. Two API-level knobs exist but are not caller-controllable from the eawf adapter:

- `prompt_cache_key` — string that biases routing for identical prefixes across sessions.
- `prompt_cache_retention` — `in_memory` (default) or `24h`.

Adapter strategy: keep the dispatch prefix byte-stable across waves so OpenAI's prefix-hash routing hits the cache. Cache *inheritance across attempts* rides `codex resume <session-id>` (cache key = session id) rather than a marker re-injection.

**OpenCode** (no caller-side marker — confirmed via blitz [20]). The bundled `@ai-sdk/anthropic` provider unconditionally injects `cache_control:{type:ephemeral}` into system message blocks; 5-min default TTL (extendable to 1h via provider config); cache key = session id; cache breakpoint auto-advances to the last cacheable block. The only operator-facing knob is the `setCacheKey` flag in OpenCode's provider config — eawf does not pass it.

**Adapter strategy + live risk:** keep dispatch prefix byte-stable; cache inheritance rides `--continue` / `--session <sid>`. **Live regression (upstream `anomalyco/opencode#17910`, since 2026-03-17):** the OAuth (Claude Pro/Max subscription) auth path returns HTTP 400 when `cache_control` is present on the request; OpenCode strips the marker as workaround. Affects the OpenCode adapter only when dispatching via OAuth-authed Claude (the dominant v0.3-v0.5 case per V8 [1:259-262]). Track the upstream fix; emit `runtime_oauth_cache_stripped` event under §5.4 when detected.

**Mis-layer regression alarm.** Emit `cache_mislayer_alarm` event when `cache_creation_input_tokens / max(cache_read_input_tokens, 1) > 4.0` for 2 consecutive dispatches in a 5-minute window [5:599-605]. The alarm fires per `(scope, runtime)`; the operator surface (C06) surfaces it as a banner overlay; the telemetry projection (C09) records the metric.

### 5.7 Plugin manifest schema

Canonical shape lives at `build/<runtime>-plugin/manifest.yaml`. **`PluginManifest(BaseModel)` Pydantic-validated with `model_config = ConfigDict(extra="forbid")` (XB19 fix 2026-05-18 — original brief claimed Pydantic but defined as `@dataclass(frozen=True)`; corrected to actual BaseModel below).** Drives the rendered output under each runtime's package (today's per-runtime plugin_install code [10]).

```python
# src/eawf/runtimes/plugin_manifest.py
from typing import Literal
from pydantic import BaseModel, ConfigDict

class PluginInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: str
    description: str
    runtime: Literal["claude-code", "codex", "opencode"]
    generator: str

class PluginContributes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skills: list[str] = []
    agents: list[str] = []
    hooks: dict[str, list[str]] = {}

class PluginManaged(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body_hash_field: str
    timestamp_field: str
    source_files: list[str]

class PluginManifest(BaseModel):
    """Canonical plugin manifest schema. Loaded from build/<runtime>-plugin/manifest.yaml."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"   # locked per Q5 / BOT-03
    plugin: PluginInfo
    contributes: PluginContributes
    managed: PluginManaged
```

```yaml
# build/<runtime>-plugin/manifest.yaml — same shape, on-disk form
schema_version: "1.0"
plugin:
  name: eawf
  version: "1.0"
  description: "Eä Workflow plugin — agent-driven development skills + hooks"
  runtime: claude-code            # claude-code | codex | opencode
  generator: eawf-plugin-<runtime>
contributes:
  skills:
    - research
    - prep
    - audit
    - ship
    - review
    - polish
    - flow
    - blitz
    - coauthor
    - design
  agents:
    - researcher
    - planner
    - executor
    - auditor
    - reviewer
    - polisher
    - operator
    - domain-specialist
  hooks:
    session_level:                # CC + Codex + OpenCode all observe these
      - session_start
      - session_end
      - pre_commit
      - post_commit
      - pre_push
      - post_push
    workflow_level:               # eawf-CLI-fired; not subscribed at runtime
      - wave_open
      - wave_close
      - iter_open
      - iter_close
      - phase_open
      - phase_close
      - pre_audit
      - post_audit
      - agent_end
managed:
  body_hash_field: __eawf_managed.body_hash
  timestamp_field: __eawf_managed.timestamp
  source_files:                   # AGENTS.md sync trigger surface — §5.9
    - AGENTS.md
    - src/eawf/render/skills.py
    - src/eawf/render/agents.py
    - src/eawf/render/hooks.py
```

**Per-runtime rendered output (existing code [10]).**

- Claude: `.claude/{skills/<name>/SKILL.md, agents/<role>.md, hooks/<event>.sh, settings.json}`.
- Codex: `.codex/plugins/eawf/{.codex-plugin/plugin.json, skills/<name>/SKILL.md, hooks/<event>.sh}` + `.codex/config.toml` patch.
- OpenCode: `.opencode/plugins/eawf.js` + `opencode.json` MCP-block patch + `.opencode/agent/<role>.md` + `.opencode/command/<name>.md`.

OpenCode has no top-level manifest file — the JS plugin module is the entry point [10]. The eawf-side manifest still uses the YAML shape; the renderer omits the rendered-manifest output for OpenCode.

### 5.8 Agent dispatch — daemon-side routing

Per C02 §5.13 [3:843-887] with the per-skill manifest tie-in added.

**CLI surface (C05 owns the verb; C07a documents the flow).**

```
eawf agent dispatch <wave-id> [--runtime <id>] [--session-policy {fresh|continue|hybrid}] [--reason <text>]
```

**Daemon routing algorithm.**

```python
async def dispatch(wave_id: str, runtime: str | None = None,
                   session_policy: str | None = None) -> DispatchResult:
    wave = await state_read_wave(wave_id)
    skill = lookup_dispatch_skill(wave)             # 'execute' skill in V8 default
    runtime_id = runtime or pick_primary_from_preference(wave)
    effective_policy = (
        session_policy
        or skill.dispatch.session_policy            # manifest default
        or "hybrid"
    )
    adapter = load_adapter(runtime_id)              # RuntimeAdapter Protocol §5.1
    attempts = sorted(wave.sessions)
    last = wave.sessions.get(max(attempts)) if attempts else None
    is_retry = bool(attempts) and wave.status in (
        WaveStatus.IN_PROGRESS, WaveStatus.FAILED, WaveStatus.CLOSED
    )

    if (effective_policy == "fresh" or not is_retry
            or last is None or last.runtime != runtime_id):
        new_attempt = (max(attempts) + 1) if attempts else 1
        note = (
            DispatchNote.FRESH_DISPATCH if last is None or last.runtime == runtime_id
            else DispatchNote.SWITCH_ON_ERROR
        )
        session = await adapter.open_session(wave, build_prompt(wave, skill))
    else:                                             # hybrid+continue path
        new_attempt = max(attempts) + 1
        try:
            session = await adapter.continue_session(last.session_id,
                                                      build_continue_prompt(wave))
            note = DispatchNote.CONTINUE_FROM_SESSION
        except SessionResumeFailed:                  # V8 fall-through [1:268-269]
            session = await adapter.open_session(wave, build_prompt(wave, skill))
            note = DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH

    annotation = DispatchAnnotation(
        attempt=new_attempt, note=note,
        runtime_from=last.runtime if last else None,
        runtime_to=runtime_id, occurred_at=now_utc(),
    )
    await state_mutate(AddSessionAttempt(
        wave_id=wave_id, session=session, annotation=annotation,
    ))
    return DispatchResult(session_id=session.session_id,
                          attempt=new_attempt, pid=session.subprocess_pid)
```

**Per-attempt session-handle table.** `Wave.sessions: dict[int, SessionAttempt]` [3:801]. Key = attempt index (1-indexed, monotonic per wave). Daemon-written via the typed mutator path (`AddSessionAttempt` discriminated-union row in C03's `Mutation`).

**Profile-gating (V3 → V8).** Skill manifest `dispatch.session_policy` may itself be profile-overridden. `Profile.state_extensions.skill_overrides: dict[skill_name, dict]` (proposed C08 surface) carries the override. Engineering profile may pin `dispatch.session_policy = fresh`; research profile may pin `dispatch.session_policy = continue` for `/research` chained brief writing.

### 5.9 AGENTS.md sync triggers

When AGENTS.md changes, the per-runtime artifacts may drift. Today's `plugin_doctor` modules [10] detect drift by comparing on-disk SHA to the recorded `__eawf_managed.body_hash`. C07a locks the sync trigger surface.

| Source file changed | Artifacts to regenerate | Detection | Repair verb |
|---|---|---|---|
| `AGENTS.md` | per-runtime `<runtime>/AGENTS.md` (mirrored body) + per-runtime agent `<role>.md` (frontmatter docs section) | hash mismatch on AGENTS.md | `eawf plugin install <runtime> --regenerate` |
| `src/eawf/render/skills.py` (SKILL_REGISTRY edit) | `<runtime>/skills/<name>/SKILL.md` per skill changed | hash mismatch on per-skill file | `eawf plugin install <runtime>` |
| `src/eawf/render/agents.py` (AGENT_REGISTRY edit) | `<runtime>/agents/<role>.md` per role changed (Claude only — Codex nests agents inside skills; OpenCode uses `.opencode/agent/`) | hash mismatch | `eawf plugin install <runtime>` |
| `src/eawf/render/hooks.py` (HOOK_REGISTRY edit) | `<runtime>/hooks/<event>.sh` per hook changed + manifest hook-list edit | hash mismatch + manifest hash mismatch | `eawf plugin install <runtime>` |
| Profile composition changes (`profiles: [...]` reorder) | profile-conditional skill list regenerates | composition layer hash | `eawf plugin install <runtime>` |
| Capability matrix update (`runtimes/capabilities.yaml`) | none directly; daemon drops cache + emits `capability_matrix_updated` event for replay continuity | capability hash field | `eawf doctor --runtime <id>` |

**Auto-regen vs operator-driven.** Drift-detection is automatic via `eawf doctor`; regeneration is *operator-driven* (`eawf plugin install <runtime>`). Auto-regen on every AGENTS.md edit would generate a commit-spam loop; deferring to operator preserves the manifesto rule 4 single-mutator-path invariant.

## 6. Failure modes + named edge cases

| # | Failure mode | Trigger | Detection | Repair |
|---|---|---|---|---|
| F1 | Adapter Protocol mismatch | Third-party runtime adapter ships missing one of the 6 Protocol methods | Daemon raises `TypeError` at load via `isinstance(adapter, RuntimeAdapter)` from `@runtime_checkable` decorator | Adapter author fills the missing method; runtime stays UNAVAILABLE in the meantime |
| F2 | Session log path nonexistent | Adapter returns a path that doesn't exist on disk after `open_session` | `SessionAttempt.session_log_path` stamped at dispatch; daemon-side TTL sweep [3:901] logs WARN on path miss; doesn't fail dispatch | Operator investigates the runtime install; the session continues (path is best-effort metadata) |
| F3 | `--continue` failure | Session log deleted, expired, or corrupted between attempts | Adapter raises `SessionResumeFailed`; daemon catches per V8 [1:268-269]; falls back to fresh with `DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH` | Automatic — no operator intervention. Dispatch history records the fallback for audit replay |
| F4 | Error-class unknown | Adapter returns a class string not in the 5-tuple | Daemon validates against the closed enum; treats unknown as `RUNTIME_API_ERROR` and emits `runtime_error_class_unknown` event | Fix the adapter's `parse_error` mapping; replay the audit |
| F5 | Auth failure mid-phase | Operator's runtime auth expires while waves dispatch | Adapter returns `RUNTIME_AUTH_ERROR`; daemon halts the wave (BLOCKED) per V5 [1:144-147]; emits operator-notify | Operator runs auth refresh (`claude --check-auth` / equivalent) then `eawf wave resume <id>` |
| F6 | Runtime ladder exhausted | Every runtime in `runtime.preference` returns 429 or 5xx | Daemon emits `runtime_unavailable` envelope with code `-32006` [3:283]; wave halted | Operator manually picks runtime via `eawf wave switch <id> --to <runtime>` or waits for vendor recovery |
| F7 | Cache-control mis-layer | `cache_creation_input_tokens >> cache_read_input_tokens` for 2 dispatches in 5 min | Daemon emits `cache_mislayer_alarm` event; metric tracked in cost ledger [5:599-605] | Operator inspects the dispatch prefix to find the `cache_control` marker placement bug; fixes the renderer |
| F8 | Idempotency-key collision | Two CLI clients submit the same key within 60-s window | Daemon returns the cached response with `idempotent_replay: true` flag [3:262] | Expected behaviour — not actually a failure; documented for audit-trail clarity |
| F9 | Session-handle TTL prune mid-retry | Operator retries a wave whose `last_attempt.ended_at` is older than `daemon.session_handle_ttl_seconds` (default 86400 s = 24 h) | Daemon's TTL sweep already removed the row; dispatch falls back to fresh with `DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH` | Automatic; operator may extend TTL via config |
| F10 | Profile-gated skill on non-matching profile | Operator dispatches a research-profile-gated skill on an engineering-only project | Daemon refuses with `-32602 invalid_params` + envelope citing the profile gate | Operator adds the profile to the project's `profiles: [...]` list (C08) or picks a different skill |
| F11 | Plugin manifest schema drift | `build/<runtime>-plugin/manifest.yaml` adds a field at v0.4 but the running daemon is v0.3 | Schema validator fails on `extra="forbid"`; daemon-side load raises ValidationError | Operator upgrades the daemon; backward-compat scheme is *additive only* (new fields default-valued) |
| F12 | OpenCode adapter missing session-log path | Operator dispatches a wave under OpenCode; the path lookup fails because OpenCode session paths are undocumented in v0.3 | Adapter returns `supports_continue=False`; daemon treats every dispatch as fresh; no fallback churn | Verify path via §8 Q1; ship the documented path in v0.4 |
| F13 | Stale runtime entity status | `state.runtimes[id].status` says HEALTHY but the runtime binary was uninstalled | V5 is reactive [1:131-136], not probed; status updates on next failed dispatch | Operator runs `eawf doctor --runtime <id>` to refresh; or just dispatches and lets V5 fall-through fire |
| F14 | Skill manifest `dispatch.model_hint` references unknown model | Skill specifies `model_hint: claude-sonnet-99` (doesn't exist) | Adapter validates against the runtime's model catalog (per-runtime list at config-validation time); fail at composition time with `-32602` | Update the skill manifest to a valid model id |

## 7. Migration plan

C07a introduces three new module homes and two state-schema extensions (already proposed in C01 §5.3 [2:362-381, 666-696]).

### 7.1 New modules to add

| File | Surface | Phase | LOC est. |
|---|---|---|---|
| `src/eawf/runtimes/adapter.py` | `RuntimeAdapter` Protocol + `SessionResumeFailed` exception | P22-KERNEL [7:159-164] | ~80 |
| `src/eawf/runtimes/error_classes.py` | Five canonical error-class constants | P22-W01 | ~30 |
| `src/eawf/runtimes/capabilities.yaml` | Cross-runtime capability matrix (D9) | P22-W01 | ~150 |
| `src/eawf/runtimes/claude/adapter.py` | Claude adapter implementing Protocol | P22-W02 | ~250 |
| `src/eawf/runtimes/codex/adapter.py` | Codex adapter implementing Protocol | P22-W03 | ~250 |
| `src/eawf/runtimes/opencode/adapter.py` | OpenCode adapter implementing Protocol | P22-W04 | ~250 |
| `build/claude-plugin/manifest.yaml` | Canonical manifest source | P22-W05 | ~80 |
| `build/codex-plugin/manifest.yaml` | Canonical manifest source | P22-W05 | ~80 |
| `build/opencode-plugin/manifest.yaml` | Canonical manifest source | P22-W05 | ~80 |
| `src/eawf/dispatch/router.py` | Daemon dispatch routing (`agent.dispatch` RPC handler) | P22-W06 | ~300 |

### 7.2 Existing surfaces that change

- `src/eawf/dispatch/renderer.py` — extend `DISPATCH_RUNTIMES` [13] from `("claude-code", "claude-agent-sdk")` to `("claude-code", "codex", "opencode")`. `claude-agent-sdk` remains as the SDK-deferred runtime; surfaces in `eawf runtime list` with status `DEFERRED`.
- `src/eawf/render/skills.py` `SkillSpec` — add `dispatch: SkillDispatchManifest`, `profile_required: tuple[str, ...]`, `runtimes: tuple[str, ...]` fields. Existing skill rows default to `dispatch.session_policy = "hybrid"` and `profile_required = ()` + `runtimes = ()` (all runtimes).
- `src/eawf/state/models.py` — Wave gains `sessions: dict[int, SessionAttempt]`, `runtime_preference: list[str] | None`, `dispatch_history: list[DispatchAnnotation]` (per C01 §5.3.5 reservation [2:362-381]).
- `src/eawf/state/models.py` — `State` gains `runtimes: dict[str, Runtime]` (per C01 §5.3.16 [2:666-696]).
- `tools/commit_prefix_lint.py` — no change (already parametric over the prefix grammar; C07b §5.2 owns the lint specifics).

### 7.3 Compatibility shims

- `DISPATCH_RUNTIMES` tuple gets the new entries; old callers that pass `claude-agent-sdk` still validate (deprecated alias logged).
- Wave rows without `sessions` (legacy state) default to `{}`. Loader-side: missing key → `{}` empty dict; legacy waves get one virtual `attempt=1` row materialised on first retry from the existing `commit` field.
- `Runtime` entity defaults: `accepts_continue=True`, `supports_cache_control=False`, `error_classes_emitted = ()` until adapter is implemented; daemon refuses to dispatch a runtime whose adapter isn't loaded.

### 7.4 Per-phase rollout

| Phase | Surface | Scope |
|---|---|---|
| **P21-PREREQ** [7:159-163] | typed `Mutation` discriminated union + `actor_principal_id` rename + HLC + schema_version | unblocks the daemon-mediated mutator (V1) |
| **P22-KERNEL** [7:160-164] | RuntimeAdapter Protocol + Claude/Codex/OpenCode adapters + dispatch router + skill manifest dispatch block | C07a's core delivery |
| **P22-W07** | Per-skill `dispatch.session_policy` annotation across all built-in skills (research / prep / flow / audit / ship / polish / blitz / review / coauthor) | C07a + C04 cross-cluster wave |
| **P23-COST** [7:165-167] | Cost ledger reads from `session_log_path` per-runtime + emits `dispatch_cost` events | feeds C09 telemetry |
| **P24-CACHE** [7:168-169] | `cache_control` marker injection in Claude adapter + mis-layer alarm metric | V8 cache-control rollout |

### 7.5 Rollback

Each adapter ships as a separate module + each manifest is a separate file → rollback is `git revert` of the per-adapter commit set. The Wave / Runtime / Skill state-schema extensions are additive (existing JSON validates fine), so rollback never requires a schema-version downgrade.

## 8. Open questions for operator

### Q1 (resolved §4 D11 via blitz [20]) — OpenCode session-log path. **Locked: SQLite at `<local-path>` (primary) + JSON-diff at `<local-path>` (auxiliary). DATA-dir, not config-dir. `supports_continue=True` via `opencode run --continue` / `--session <sid>` / `--fork`.** Live risk: upstream `anomalyco/opencode#17910` OAuth + `cache_control` HTTP 400 regression (since 2026-03-17) — adapter must monitor and strip marker on OAuth auth path until upstream fix lands.

### Q2 (resolved §4 D12 via blitz [21]) — Codex session-log shape. **Locked: `<local-path>`. Date-sharded JSONL; line schema `{timestamp, type, payload}` with `type ∈ {session_meta, turn_context, response_item, event_msg}`. Resume verb: `codex resume <session-id>` subcommand (NOT `--resume` flag); `codex exec resume <id>` non-interactive; `codex fork <id>` for branched sessions.** `$CODEX_HOME` env-var override documented but not blitz-verified; §8 Q11 follows up on live `$CODEX_HOME` round-trip.

### Q3 (resolved via blitz r4 [24]) — Session-policy per skill. **Locked: per-skill manifest default with concrete v0.3 mapping derived from skill body intent.**

Blitz r4 audit [24]: skill bodies already encode chained-vs-isolated context expectations. Profile is the *override layer*, not the *primary defaulter*. Per-skill default mapping for v0.3 `SKILL_REGISTRY`:

| Skill | `dispatch.session_policy` default | Rationale |
|---|---|---|
| `/research` | `continue` | continuation detection: load + extend brief on same scope |
| `/blitz` | `continue` | recursive chaining of research passes |
| `/audit` | `fresh` | isolated per-pass verification; manifesto rule 8 wave-isolation |
| `/review` | `fresh` | isolated diff review; no state carry |
| `/polish` | `hybrid` | stateless cleanup; no preference |
| `/flow` | `hybrid` | stateless orchestrator (constructs fresh SkillContext per subskill internally) |
| `/prep` | `hybrid` | stateless plan-emitter |
| `/ship` | `hybrid` | stateless PR generator |
| `/coauthor` | `continue` | (per AGENTS conventions — trailer continuity) |
| `/design` | `hybrid` | new skill — defer pick to C04 |

Profile override semantics (V3 + V8 [1:268-269]): research-bundle MAY blanket-override `/audit` + `/review` to `continue` for chained-review workflows; engineering-bundle MAY blanket-override `/research` to `fresh` for isolated investigations. Manifest-default is the v0.3 floor; profile is layered override.

### Q4 (resolved §4 D4 via blitz [20, 21]) — Cache-control surface for Codex / OpenCode. **Locked: option (a) — neither runtime exposes a caller-side `cache_control` marker.**

- **Codex:** OpenAI prompt caching is automatic at ≥1024-token threshold; routes by hashing the first ~256 tokens. Only knobs are API-level `prompt_cache_key` (routing bias) + `prompt_cache_retention` (`in_memory`/`24h`); neither is caller-controllable from the eawf adapter [21]. Adapter strategy: keep dispatch prefix byte-stable; cache inheritance via `codex resume <id>`.
- **OpenCode:** bundled `@ai-sdk/anthropic` provider auto-injects `cache_control:{type:ephemeral}` with session-id as cache key; 5-min default TTL extendable to 1h via provider config; cache breakpoint auto-advances [20]. Adapter strategy: byte-stable prefix + `--continue` / `--session`. Live risk: upstream `anomalyco/opencode#17910` strips marker on OAuth auth path (since 2026-03-17); adapter monitors + emits `runtime_oauth_cache_stripped` event when detected.

If a vendor ships caller-side markers in v0.4, mirror Claude. Until then: prefix-stability + session-resume is the only cache lever for both runtimes.

### Q5 (resolved via blitz r3 [23]) — SDK adoption gate. **Locked: (b) BYOK partially lifted — subscription SDK shipped 2026-06-15 with API-rate credit-pool caveats; v0.3 stays subprocess; v0.4 adds budget-aware dispatch; v0.5 flips SDK-primary when economics fully relax.**

Blitz r3 findings [23] (subscription SDK shipped one month from brief date):

- **2026-06-15:** Anthropic shipped subscription-billed SDK with per-user credit pools: Pro $20/mo · Max-5x $100 · Max-20x $200 · Team-Std $20/seat · Team-Premium $100/seat. No pool-sharing, no roll-over.
- Overflow opt-in at standard API rates; default behavior is halt-until-reset.
- `claude -p` subprocess (the v0.3 adapter) **also swept into the same credit pool** — subprocess subscription-zero-marginal-cost advantage expires that date.
- BYOK (`ANTHROPIC_API_KEY`) still default and fully supported. PyPI v0.2.82 (2026-05-15) CHANGELOG carries no subscription-auth entries.
- SDK ≥ CLI for programmatic session control as of v0.2.82 (`SessionStore` adapter).
- Policy timeline: 2026-01-09 OAuth-in-3p-clients block → 2026-02-19 ToS clarification → 2026-04-04 explicit prohibition (capacity/service issues) → 2026-06-15 credit-pool reinstatement.

**v0.3 ship:** subprocess CLI primary (existing adapter; no flip).
**v0.4 candidate:** budget-aware dispatch — daemon reads subscriber credit-pool remaining, halts new spawns when projection > cap (per-user `$ /mo` configurable). Soft breaker only first 4 weeks per roadmap-synthesis [7:127-128]; hard breaker after distribution data.
**v0.5 SDK-primary trigger** — any of: (a) operator BYOK; (b) Anthropic raises Pro credit ≥ $100; (c) Anthropic ships subscription-OAuth without API-rate metering.

Open sub-questions deferred to v0.4 blitz round:

- Does the credit pool also apply to `claude` interactive when invoked under a CI/headless TTY? (Coverage doc ambiguous.)
- Will Anthropic ship per-org pooling later, or stay strict per-user? (Critical for team / enterprise dispatch model.)
- Does `claude-agent-sdk` v0.3+ plan native subscription-OAuth (eliminating bundled-CLI hop)?

### Q6 (resolved via batch ratification 2026-05-17) — Capability matrix versioning cadence. **Locked: (a) per-minor bump.** `runtimes/capabilities.yaml` schema_version bumps on every eawf-minor cluster cadence (v0.3 → v0.4 → v0.5). Drift between matrix and live runtime detected by `eawf doctor --runtime <id>`.

### Q7 (resolved via batch ratification 2026-05-17) — Idempotency window length. **Locked: (a) 60 s.** Daemon dedup window per C02 [3:262] stays at 60 s. V5 cross-runtime re-issue completes within seconds; 60 s suffices. If Retry-After > 60 s, generate a new idempotency key on the next attempt.

### Q8 (resolved via batch ratification 2026-05-17) — Plugin manifest CI gate. **Locked: (a) doctor warn only for v0.3.** `eawf doctor --runtime <id>` flags drift; CI does NOT block PR merge. Revisit in v0.5 when (b) CI-gate becomes a stability requirement.

### Q9 (resolved via batch ratification 2026-05-17) — Per-profile skill subset storage. **Locked: (a) YAML at `<local-path>`.** File-per-skill; composes at startup via C08 loader. Python entry-points + hybrid surface (b)/(c) deferred to v0.5+.

### Q10 (resolved via blitz r4 [25]) — Skill `runtimes` field semantics. **Locked: (a) omit on runtime mismatch; recommendation ratified BUT not yet implemented for builtins — gap added to P22-W02/W03/W04 scope.**

Blitz r4 audit [25]:

- `runtimes` field today exists on `DiscoveredSkill` (user/workspace overlays) only, NOT on `SkillSpec` (builtin registry).
- `discover_skills(runtime=...)` filtering is active for user/workspace skills.
- Plugin install paths in `src/eawf/runtimes/{claude,codex,opencode}/plugin_install.py` render every `SKILL_REGISTRY` entry verbatim — no per-runtime filter.
- Current `SKILL_REGISTRY` has zero per-runtime restrictions → zero dead stubs today; gap is latent until first builtin skill declares `runtimes`.

**Implementation gap (P22-W02/W03/W04):**
1. Add `runtimes: tuple[str, ...] = ()` field to `SkillSpec` (empty = all runtimes).
2. In each `plugin_install.py`, filter `SKILL_REGISTRY` by `(not spec.runtimes) or (runtime_id in spec.runtimes)` before iterating render.
3. Mirror existing `DiscoveredSkill` filter semantics — keeps user/workspace and builtin paths consistent.

### Q11 (resolved via batch ratification 2026-05-17) — Multi-runtime dispatch fanout. **Locked: defer to v0.5+.** v0.3-v0.5 = one runtime per wave attempt; V5 fallback is sequential, not parallel. Reconsider when team-mode dispatch arrives (R5 federation cluster post-v0.5).

### Q12 (resolved via blitz r2 [22]) — `$CODEX_HOME` round-trip for Codex adapter session-log writes. **Locked: vendor-doc + source verified; live write-path probe deferred to v0.4.**

Codex CLI is open-source (Rust). `codex-rs/utils/home-dir/src/lib.rs::find_codex_home()` is the single resolver (reads `$CODEX_HOME` env, canonicalized + dir-verified, else falls back to `<local-path>`). `codex-rs/core/src/rollout.rs` hydrates `Config.codex_home` via `RolloutConfigView`; no parallel `<local-path>` hardcode in the rollout writer. Path-join contract documented in `codex-rs/app-server/tests/common/rollout.rs`: `codex_home/sessions/YYYY/MM/DD/`.

**Adapter behavior:** Codex `RuntimeAdapter.session_log_path` resolves `os.environ.get("CODEX_HOME") or os.path.expanduser("<local-path>)` + walks `sessions/YYYY/MM/DD/rollout-*.jsonl`.

**Followup v0.4:** integration test that sets `CODEX_HOME=<local-path>`, runs `codex exec` non-interactive, asserts `rollout-*.jsonl` lands under `<local-path>` rather than `<local-path>`. Source review is strong; live write-path probe closes the loop.

## 9. References

[1] `.ea/local/research/long-term/2026-05-16-c00-spec-index.md` — C00 spec architecture index; V1..V8 verdicts; cluster catalog; C07 scope ([1:648-712]); SDK tradeoff matrix prompt ([1:256-262]); per-runtime session-handle paths ([1:243-248]).
[2] `.ea/local/research/long-term/2026-05-16-c01-foundations.md` — C01 foundations brief; Runtime entity (§5.3.16 [2:666-696]); SessionAttempt + DispatchAnnotation (§5.3.5 [2:362-381]); persona authority matrix (§5.5).
[3] `.ea/local/research/long-term/2026-05-16-c02-daemon-topology.md` — C02 daemon brief; IPC method catalog (§5.3 [3:294-345]); runtime fallback state machine (§5.12 [3:720-790]); session-handle tracking (§5.13 [3:792-901]); RuntimeAdapter Protocol sketch ([3:889-899]).
[4] `.ea/local/research/long-term/2026-05-15-ea-framework-manifesto.md` — Eä manifesto (Rule 4 single-canonical-contract; Rule 8 phase-bundled delivery).
[5] `.ea/local/research/long-term/2026-05-15-long-term-features-deep.md` — long-term features deep; KV-cache strategy ([5:201-249]); cache-control mis-layer alarm ([5:599-605]); cost ledger ([5:113-128]); state-store scaling matrix.
[6] `.ea/local/research/2026-05-13-codex-eawf-compatibility.md` — Codex compatibility brief; harness-scope customization (`HarnessRuntimeSpec` / `ExecutionPolicy` / `AgentBinding` / `WorktreePolicy` / `ArtifactStyleProfile`); subagent enforcement contract.
[7] `.ea/local/research/long-term/2026-05-15-long-term-roadmap-synthesis.md` — locked roadmap synthesis; SDK rejection ([7:19-22, 100-102]); harness session-log paths ([7:121-122]); per-wave wall-clock cap ([7:112]); budget broker ([7:117-128]); P22-KERNEL scope ([7:159-164]).
[8] `.ea/local/research/2026-05-12-artifact-structure-standardization.md` — artifact chassis standardization (Citation; promotion scrub; renderer-owned markdown).
[9] `.ea/local/research/2026-05-11-harness-adapters-v0.3.md` — harness adapter baseline (Claude / Codex / OpenCode v0.3 scope; deferred Aider / Cursor / Cline / Roo; per-harness plugin-tree layout).
[10] `src/eawf/runtimes/{claude,codex,opencode}/plugin_install.py` — current per-runtime install code; managed-bytes sidecar; idempotent render; AGENTS.md sync surface.
[11] `src/eawf/render/skills.py` — `SkillSpec`, `SKILL_REGISTRY`, `render_skill_md`; per-skill body source.
[12] `AGENTS.md` — non-negotiable rules (rule 2 strict-config-validation `extra="forbid"`; rule 9 f-strings only; rule 11 worktree discipline; rule 14 commit prefix; rule 17 naming conventions).
[13] `src/eawf/dispatch/renderer.py` — current dispatch envelope renderer; `DISPATCH_RUNTIMES` tuple; `render_dispatch_envelope`; MCP grants projection into SDK runtime.
[14] `src/eawf/runtimes/claude/hook_map.py` — Claude session-level plugin hook registry (PLUGIN_HOOK_REGISTRY 6 entries; CC plugin-root template variable).
[15] `src/eawf/runtimes/codex/hook_map.py` — Codex hook event names mapped 1:1 to HookEventType values.
[16] `src/eawf/render/hooks.py` — HOOK_REGISTRY (15 events: 6 session-level, 9 workflow-level); per-event bash wrapper renderer.
[17] `src/eawf/render/agents.py` — AGENT_REGISTRY (8 roles); per-role markdown renderer; AgentSpec dataclass.
[18] `.ea/local/research/2026-05-11-mcp-via-eawf.md` — MCP via Eä; per-runtime MCP target (Claude `mcpServers`; Codex `.codex/config.toml [mcp_servers.*]`; OpenCode `opencode.json mcp`).
[19] `src/eawf/vcs/coauthor.py` — co-author trailer policy (CoauthorMode runtime/project/disabled; runtime aliases claude/codex/anthropic/openai).
[20] `.ea/local/research/long-term/2026-05-16-c07a-blitz-opencode.md` — blitz brief resolving D11 + Q1 + Q4 (OpenCode): SQLite + JSON-diff data-dir layout, `--continue`/`--session`/`--fork` resume verbs, `@ai-sdk/anthropic` internal `cache_control` injection, upstream `anomalyco/opencode#17910` OAuth + `cache_control` HTTP 400 regression (since 2026-03-17).
[21] `.ea/local/research/long-term/2026-05-16-c07a-blitz-codex.md` — blitz brief resolving D12 + Q2 + Q4 (Codex): `<local-path>` date-sharded JSONL with `{timestamp, type, payload}` envelopes, `codex resume <id>` subcommand verb, OpenAI prompt caching automatic at ≥1024-token threshold, no `cache_control` marker, API-level `prompt_cache_key` + `prompt_cache_retention` knobs.
[22] `.ea/local/research/long-term/2026-05-16-c07a-blitz-codex-home-roundtrip.md` — blitz r2 brief resolving Q12 (`$CODEX_HOME` round-trip): vendor-doc + Rust-source verified (`codex-rs/utils/home-dir/src/lib.rs::find_codex_home` single resolver feeds `codex-rs/core/src/rollout.rs` writer; no `<local-path>` hardcode in rollout path). Live write-path integration test deferred to v0.4.
[23] `.ea/local/research/long-term/2026-05-16-c07a-blitz-sdk-gate.md` — blitz r3 brief resolving Q5 (SDK adoption gate): subscription SDK ships 2026-06-15 with per-user credit pools ($20 Pro / $100 Max-5x / $200 Max-20x); API-rate overflow opt-in; `claude -p` also swept into pool (subprocess subscription advantage expires that date); BYOK still default. Verdict (b) BYOK partially lifted; v0.3 subprocess; v0.4 budget-aware dispatch; v0.5 SDK-primary on full economic relaxation.
[24] `.ea/local/research/long-term/2026-05-16-c07a-blitz-session-policy.md` — blitz r4 brief resolving Q3 (session-policy per skill): skill bodies encode chained-vs-isolated intent directly; per-skill manifest default mapping derived (research/blitz=continue; audit/review=fresh; flow/prep/ship/polish=hybrid); profile is override layer not primary defaulter.
[25] `.ea/local/research/long-term/2026-05-16-c07a-blitz-skill-runtimes.md` — blitz r4 brief resolving Q10 (skill `runtimes` field): `runtimes` exists on `DiscoveredSkill` (overlays) only; `SkillSpec` (builtin) lacks field; plugin install paths don't filter by runtime. Implementation gap added to P22-W02/W03/W04: add field + wire filter mirror of `discover_skills` semantics.

## 10. Provenance + Scrub

### Provenance

- `store_record=none (local-only research brief)`
- `commit=3b86f7a (parent at brief authoring time; revisions 2026-05-18)`
- `cluster=C07a`
- `consumes=C00 V1..V9 (V9 added 2026-05-18 per XB10); C01 foundations; C02 daemon`
- `supersedes=none`
- `pairs_with=C07b (VCS / worktree / registry / event / render / brand)`
- `session=eawf-spec-c07a-runtime-skill-dispatch-2026-05-16`
- `last_revised=2026-05-18 (audit-driven: V9 added to §3 per XB10/B-01; PluginManifest concretised as Pydantic BaseModel with extra="forbid" per XB19; 2026-06-15 SDK release marked as forecast not shipped per XB04; session_log_path → opaque handle per XB05/C02-I007; capability matrix render delivery noted as deferred to C07a-W01 implementation per F-09)`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md (8 CRIT findings; 12 Codex issues; future-date BLOCKER XB04)`
- `authority_binding=Q1 (2026-05-18): daemon = sole writer for plugin sync output paths under .claude/, .codex/, .opencode/; plugin install routes through daemon, not CLI directly.`
- `capability_matrix_8x3=referenced; lives at src/eawf/runtimes/capabilities.yaml (per F7 of audit; matrix render added to C07a-W01 implementation per F-09)`
- `verdicts_load_bearing=V1 daemon mediator; V2 spec body in prefix; V3 profile-gated skill registry; V5 reactive runtime fallback with five-class error normalization; V8 hybrid session reuse with per-runtime adapter Protocol + SDK-deferred subprocess-primary final pick`
- `blitz_round_1=2026-05-16 — D11/Q1, D12/Q2, Q4 resolved via blitz briefs [20] + [21]: OpenCode (SQLite + JSON-diff data-dir, `--continue`/`--session`/`--fork`, internal `cache_control` injection, OAuth-cache-strip regression); Codex (date-sharded JSONL `rollout-*.jsonl`, `codex resume` subcommand verb, automatic prompt caching ≥1024 tokens, no marker)`
- `blitz_round_2=2026-05-16 — Q12 ($CODEX_HOME) resolved via blitz brief [22]: vendor-doc + Rust-source verified single resolver feeds rollout writer; adapter honours env-var; live write probe deferred to v0.4 integration test`
- `blitz_round_3=2026-05-16 — Q5 SDK adoption gate resolved via blitz brief [23]: subscription SDK ships 2026-06-15 with API-rate credit pools (Pro $20/Max5x $100/Max20x $200); claude -p subprocess also swept into pool; subscription cost-zero advantage expires that date. v0.3 stays subprocess-primary; v0.4 adds budget-aware dispatch (per-user $/mo cap halt-or-warn); v0.5 flips SDK-primary on full economic relaxation. §4 D2 + §5.2 SDK matrix + §8 Q5 all updated.`
- `blitz_round_4=2026-05-16 — Q3 session-policy + Q10 skill-runtimes resolved via blitz briefs [24] + [25]: Q3 per-skill manifest default (research/blitz=continue; audit/review=fresh; flow/prep/ship/polish=hybrid); Q10 `runtimes` field gap — exists on DiscoveredSkill but not SkillSpec; plugin install paths don't filter; gap added to P22-W02/W03/W04 scope.`
- `batch_ratification=2026-05-17 — Q6 (per-minor matrix versioning), Q7 (60s idempotency window), Q8 (doctor-warn-only manifest gate), Q9 (YAML profile-skill storage), Q11 (multi-runtime fanout deferred to v0.5+). All 12 Open Questions resolved; C07a status candidate for `accepted` flip pending C07b parity.`

### Scrub

- status: clean
- references: repo-relative or external URL only
- local paths: none (path examples — `<local-path>`, `<local-path>`, `<local-path>` — are home-relative templates, not machine paths)
- real emails: none (canonical author block in pyproject only — not present in this brief)
- abstract placeholder names: not applicable (no mockup repos cited; runtime ids are public vendor names: claude-code, codex, opencode)
- machine identifiers: none
- credentials / API keys: none
- vendor URLs: external developer documentation only (`https://developers.openai.com/codex/...`, advisory)
