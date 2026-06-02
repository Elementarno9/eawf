# TUI MODES chassis — the canonical TUI hub

> Canonical TUI hub. Folds [textual-enrichment], [attention-routing], [v0.5 multi-repo], and the [operator review] (see "Folded surfaces"). Companions: [tui-agent-watch](2026-05-31-tui-agent-watch.md) (live agent-watch zoom, §B drill-in), [control-plane](research-orchestration/2026-05-31-control-plane.md) (operator-input consumer).

## Summary

The eawf TUI is far more mature than the v0.4 gap brief's "latent gaps" framing implied: 3 scopes, ~16 overlays, a fuzzy command palette, colour-blind-safe theming, V12 status glyphs, and daemon-push state binding all ship today. The open UI/UX question is not "build a TUI" but "the next ~10 dwell-on surfaces (autopilot cockpit / trust / research / doctor / evidence-spine) are coming — on what chassis, and which first."

Operator ratified (3 AUQ rounds, 2026-05-30): adopt a k9s-style chassis using Textual `MODES` — heavy surfaces become full-screen modes reached by a palette verb + a collision-free digit accelerator; the 2x2 stays "home"; modals demote to transient confirm/form/peek only. Settle the chassis BEFORE P29 builds surfaces on it (avoids a full rework). Capture as this brief + a drafted `D-TUI-MODES` Decision.

This brief is the canonical TUI-chassis contract for P29 TUI waves. Sections A-D are the ratified core; E-H extend it (research mode, evidence mode, onboarding, ProgressBar refresh). Mode 2's name ("autopilot" recommended) is pending a final confirm.

## Current state (verified, not assumed)

Navigation today uses `push_screen`/`switch_screen`, NOT Textual `MODES` (`src/eawf/surfaces/tui/app.py:371,685,791`; zero `MODES` hits under `surfaces/tui/`). Scope IS the screen — `action_switch_scope` swaps the whole screen (`app.py:649-687`). Every overlay routes through `push_modal` with a depth cap (was 3, bumped to 10 per the gap brief Q2).

Key map (deduped grep over `surfaces/tui/**` `Binding(`): global-taken = `w`/`r`/`u` + `ctrl+w/r/u` (scope), `q` + `escape` (quit/close), `h`/`j`/`k`/`l` + arrows (nav), `alt+left`/`alt+right` (back/forward), `slash` (palette), `question_mark` (help), `f5` (refresh). Scope-level = `c` (config), `z` (zoom). Reserved = `i` (needs_user inbox, T12). Modal-scoped (free at home level) = `d`/`e`/`m`/`h`/`f`/`s`/`r`/`L`/`space`/`tab`. Digits `0`-`9` are entirely unbound — the clean accelerator space.

Wave fields available to the cockpit (`src/eawf/kernel/state/models.py:310-325`): `status: WaveStatus`, `deps: list[WaveIdStr]`, `agent_role`, `effort_bucket`, `tokens_consumed: int`, `sessions: dict[int, SessionAttempt]`; `SessionAttempt.started_at` (`:266`). Live EU = `ActualSummary.attention_eu` (`:465`).

Cockpit control seams: `BudgetDecision.HALT` + SIGTERM/grace/SIGKILL ladder exist (`src/eawf/runtime/budget/policy.py:62`, `runtime/budget/service.py`); `flow.stop_after`/`flow.resume_start`/`flow.resume_end`/`flow.end` events exist (`src/eawf/workflow/skills/flow/__init__.py:602,644,734`). Halt/skip/kill/arm RPCs and the `SPAWN_UNAVAILABLE` toast do NOT exist yet (zero source hits) — they land with live spawn in P29-I03. Only `dispatch_cost` is in the closed `EventKind` literal today (`src/eawf/kernel/store/kinds/event.py:62`); `wave_state`/`activity`/`autopilot_status` are tui-contracts design-fiction, not in the enum.

## A. The MODES chassis (ratified core)

Two orthogonal axes. SCOPE = which repo's data (`w`/`r`/`u`, unchanged). MODE = what view (new). Today scope IS the screen, which conflates the axes and is the structural reason new surfaces could only be modals. Under Textual `MODES`, each mode owns an independent screen stack; the mode reads from `app._scope`/`app.state`. `w`/`r`/`u` swaps the data source and rebuilds the active mode; the palette/accelerator swaps the mode.

Mode map (canonical switch = palette verb; fast-path = collision-free digit; `esc` pops within a mode, then returns to Home):

| Digit | Mode | Verb | Scope behaviour | Lands |
|---|---|---|---|---|
| 1 | Home (2x2 / table / portfolio) | `/home` | scope-aware (ships today) | shipped |
| 2 | Autopilot (spawn cockpit, T15) | `/autopilot` | active repo (zoom-prompt at ws/user) | P29-I03 |
| 3 | Research (research_board, C7) | `/research` | active repo | P29-I03 |
| 4 | Trust (drift dash + metrics tiles, T2) | `/trust` | active repo | P29-I01 static -> lights up |
| 5 | Doctor (T8 + events tail + health + drift) | `/doctor` | active repo | P29-I01 |
| 6 | Evidence (why-peek / readiness / ledger / spec, T1/T3/T4/T5) | `/evidence` | active repo | v0.5 |

Mode 2 name = `autopilot` (locked 2026-05-30): `/autopilot` + digit 2, matches the `autopilot_status` concept, no palette collision with the `/flow` skill.

Modal demotion — the rule is dwell-on becomes a mode; transient stays a modal.

- Stay modals (transient): Confirm, EditField, MultichoiceChecklist, NeedsUser, PlanPreview, InitWizard, Config-form, Detail-peek, Reference-peek, Why (T1, a microscope on ONE entity = peek, not dwell), PrList, AuditFailed-repair-menu.
- Fold into modes: MetricsModal 3x2 -> Trust mode; EventsModal tail -> Doctor mode; AuditRunningModal -> a panel inside Autopilot when an audit wave is live.

Net effect: the depth-cap now only ever holds confirm-over-form-over-peek, never a navigation stack — modal-soup is structurally impossible.

Persistent furniture on every mode: the header breadcrumb `Ea > REPO > P29 > I03 - <mode>` and the footer attention badge cluster (section C). Mode hints in the footer teach the digit row.

Migration (incremental, P29-I01, no data-layer change — modes read the same bound `State`): (1) wrap the 3 scope screens as the `home` mode; (2) add the `MODES` dict + `switch_mode`; (3) register palette verbs + digit accelerators; (4) move the metrics/events handlers from modal-push to mode content. Scope rule for heavy modes: they operate on a single repo (the zoomed/active one); at workspace/user scope, a heavy mode prompts-to-zoom or operates on the focused row's repo.

## B. Autopilot cockpit (the `/autopilot` mode, T15)

The centerpiece new surface — where the operator lives once live spawn lands (P29-I03). The market converged here in 2025-2026 (Cursor Conductor "who's working on what", OpenAI Codex "command center for agents", Claude Squad, agtx kanban); eawf already is one-agent-per-independent-wave + rollup, so it needs the cockpit to match.

```
 Ea > DEMO > P29 > I03 - autopilot      run 4  q 1  warn 1  paused 0   burn 2.40/8.0
 ------------------------------------------------------------------------------
  st  wave    title                    role      effort  burn           elapsed
  ~   W09 *   pgid-kill ladder         executor  M       |####| 92k/100k!  9m03  92%
  >   W11 q   jury 3-juror vote        reviewer  S       |#   | 11k/100k   1m44  you
  ~   W08 *   env-scrub floor          executor  M       |##  | 48k/100k   6m12
  ~   W12 *   research orchestrator    executor  L       |##  | 39k/120k   4m20
  -   W13     AUQ bridge deliver       executor  M       --             --    dep W08,W09
  -   W14     frontier auto-drain      executor  L       --             --    dep W12
 ------------------------------------------------------------------------------
  W09 > pgid-kill ladder - IN_PROGRESS - feature/eawf-v0.5-p29-i03-w09 - try 1
   last: cherry-pick clean -> verify floor 4/6 green (env-isolation, pgid-leak pending)
   warn 92% per-wave token cap - soft-warn 75 / hard-halt 100 (opt-in)
 ------------------------------------------------------------------------------
  up/dn select  enter detail  H halt  S skip  K kill  space pause  a arm  / palette
```

Sort = attention-first: over-budget and needs-you float to top, then in-progress, claimed, blocked. Tint by status (shipped `STATUS_COLOURS`). Columns map to verified `Wave` fields: glyph=`status`; spawn-marker=has live session; question-marker=open needs_user; effort=`effort_bucket`; burn bar=`tokens_consumed` vs per-wave cap; elapsed=`sessions[*].started_at`; dep=unmet `deps`.

Intervention (I03 net-new RPCs): `H` halt -> `flow.stop_after` + budget SIGTERM-ladder; `S` skip -> drop from frontier; `K` kill -> SIGKILL pgid; `space` pause/resume; `a` arm (launch_flow, T16). Pre-spawn (today -> I02): keys stay BOUND, confirm -> `SPAWN_UNAVAILABLE` toast (D-TUI-06) so muscle-memory + discoverability ship before the forker.

Live data: the burn bar is driven by `dispatch_cost` events (capture-forward stamps `started_at`/`duration` per the synthesis T11); status transitions by state-refresh. Empty/pre-spawn state shows the claimable frontier + a "spawn disabled - `eawf wave autoland` for back-half" banner (autoland is the spawn-free I01 throughput win).

## C. Attention model ("where do I look?")

Recommendation: badge cluster (always-on) + `i` attention inbox (pull) + blocking-only auto-open. No permanent rail — the 2x2 home is already dense and a rail steals width on every mode.

Footer badge cluster rides every mode, hidden-at-zero: `run N | q N (questions) | warn N | paused N | burn $X/cap`. It is the persistent answer to "is anything on fire?".

`i` -> attention inbox — extends T12's needs_user inbox into a unified prioritized feed joining five sources already in state:

```
 Ea > DEMO - attention inbox                                          7 items
 ------------------------------------------------------------------------------
  q     W11   jury vote - pick winning approach (2 options)      needs answer
  warn  W09   token cap 92% - halt / raise cap / let run         budget
  fail  A44   audit FAIL - pgid-leak criterion                   audit
  drift W52   git drift - pinned SHA missing, re-pin?            drift
  stale W31   claimed 3h ago, no progress                        stale
 ------------------------------------------------------------------------------
  enter resolve   o open in mode   d defer   esc close
```

Sources (all exist in state today): open needs_user pauses (`workflow/skills/needs_user.py` list_open_pauses); `BudgetDecision.WARN`/`HALT` (`runtime/budget/policy.py`); audit verdict=fail (the toast path already fires); `detect_git_state_drift` DriftKinds; stale-wave (claimed, no recent session). Auto-open policy = `ui.needs_user_autopopup` `off|blocking|all`, default `blocking` (only an `urgency=blocking` question steals focus) — matches the synthesis T12 decision 8 and the orchestration-research rule "inject decisions without bottleneck". Everything else: badge + toast only, operator pulls via `i`.

## D. EU / trust light-up + empty-state honesty

Everything EU-driven renders dark today: agent-driven `close_wave` hard-codes `elapsed_eu=0.0` and `telemetry.db` is an empty skeleton (synthesis T9/T11). P29-I01 capture-forward (`dispatch_cost` -> `TelemetrySession`, unified `actual_eu = elapsed_eu or attention_eu` accessor) fixes it. Until then, never paint a misleading green-zero. Three honest states for every EU/calibration widget:

```
  empty (pre-capture)      accruing (data landing)      lit (>= N closed waves)
  EU ....  no data yet      EU |## | 2 waves (need 5)    EU |### | 1.8/2.0 +12% drift
```

Trust mode = drift dashboard (operator picked Option A/C in the synthesis — project calibration health, no per-role params):

```
 Ea > DEMO > P29 - trust                          window 30d   recompute 4m ago
 ------------------------------------------------------------------------------
  EU calibration       |#### |  inside-pessimistic 78%    bucket drift  XL -0.4 EU
  autonomy tier        Assistive  -> Autonomous: 95% hold + 30 clean (now 88% / 14)
  tier histogram       Insight ##  Assistive ######  Autonomous .
  verifier reliability  - no data yet (deferred_v0.4.1)   <- honest residual
 ------------------------------------------------------------------------------
  tiles  [variance] [weekly burn] [rework] [citations] [tokens/bucket] [drift]
```

Folds the existing 3x2 MetricsModal in as the tile strip. `verifier_reliability` shows its real `deferred` residual — never a `None` pass-rate painted green. The bucket-drift badge surfaces once `calibrate_buckets` refits on captured `attention_eu` (XL=3.5 kept, recalibrated-from-data per round-3 decision 11).

## E. Research mode (the `/research` mode, mode 3)

RECONCILED with the canonical research-cockpit design `.ea/local/research/research-orchestration/2026-05-29-cockpit.md`: the research MODE *is* that brief's `ResearchOrchestratorScreen`, mounted as mode 3 under this chassis — not a thinner re-derivation. That brief owns the full screen: a left topic-tree pane (campaign > round > topic > task > unresolved-question), a center claims/evidence pane (Claims / Options / Conflicts / Unresolved / Reports / Brief-preview tabs), a right progress/budget pane (RUN / ROUND / ACTIVE / WAITING / PAUSED / BUDGET / RISKS bands), a bottom `needs_user` checkpoint drawer, and a durable daemon-owned `ResearchCampaign` controller; depth vocab `shallow|medium|deep|exhaustive`; 10 operator checkpoints; plan-only-runner first.

The chassis contribution is placement + widget reuse, NOT a competing layout: (a) the research screen mounts as mode 3 reached by `/research` + digit 3; (b) its dispatch-round task list reuses the SAME fleet-table widget as the autopilot cockpit (B) — pending/claimed/running rows with live ProgressBar burn + elapsed — so one widget serves both modes; (c) its checkpoint drawer uses the C attention / needs_user machinery; (d) its bars follow H (ProgressBar). The earlier "research just reuses the autopilot fleet-table" framing was too thin — the cockpit brief's 3-pane + drawer is the canonical research-mode composition; this chassis only fixes WHERE it lives and WHICH shared widgets it draws on.

```
 Ea > DEMO > research   campaign tui-uiux   depth deep   round 1/2   ACTIVE 5
 ------------------------------------------------------------------------------
  topic tree           |  claims / evidence            |  progress / budget
  v campaign           |  [Claims][Options][Conflicts] |  RUN    round 1/2
   v round 1           |  C1 MODES scales    resolved  |  ACTIVE 5  WAIT 2
    # TUI IA patterns  |  C2 cockpit live    weak      |  BUDGET 39k/120k
    ~ agent-orch UX    |  C3 braille perf    conflict  |  RISKS  1 contradiction
    > spawn precedent  |  ...                          |
 ------------------------------------------------------------------------------
  checkpoint: resolve C3 contradiction -> [discriminator task] [accept stronger] [park]
  up/dn tree  enter peek  a approve  p park  r follow-up  s snapshot  / palette
```

Builds across P29 per the cockpit brief: screen shell + plan-only runner + depth-vocab + `ResearchCampaign` models + needs_user inbox in I01 (spawn-free); live dispatch + synthesizer + multi-round follow-up + research-to-roadmap bridge in I03.

## F. Evidence mode (the `/evidence` mode — v0.5; seam reserved in P29)

Unifies T3 close-readiness viewer + T4 evidence ledger + T5 spec inspector; the drill target is the T1 why-peek MODAL (microscope on one entity, stays transient per A).

```
 Ea > DEMO > P29 > W08 - evidence                  close-readiness: NOT READY 2 missing
 ------------------------------------------------------------------------------
  readiness  not ready   missing 2 - failed 0 - waived 1   gate floor+audit
 ------------------------------------------------------------------------------
  criterion          gate    kind          status     by
  env-isolation      floor   command       pass       executor
  pgid-no-leak       floor   command       missing    -
  egress-deny        audit   gate_result   pass       auditor
 ------------------------------------------------------------------------------
  enter why-peek (chain + verdicts + track-record)  f filter  s export  / palette
```

T3 readiness header + T4 ledger rows (EvidenceRecord: criterion / gate / evidence_kind / status / produced_by) + `s` local export (KEY_s -> `.ea/local/evidence/`, scrub-gated) + Enter -> T1 why-peek modal (WhyBundle: header + evidence_chain + independent_verdicts + track_record). Reserving `/evidence` + digit 6 + the why-peek seam in the P29 chassis means the v0.5 evidence-spine slots in with zero chassis churn.

## G. Onboarding / discoverability

Today nearly every binding is `show=False`; a newcomer relies on `?` help + `/` palette and the footer teaches nothing. Under MODES, discoverability improves for free (the digit mode-row is always visible). Plus:

- Adaptive footer: the mode row (`1 home 2 autopilot 3 research 4 trust 5 doctor`) always visible; +3-5 curated context keys per mode promoted to `show=True`.
- First-run tour: extend the existing InitWizard with 3 dismissible cards — scopes (`w/r/u`), modes (`1-6` / `/palette`), attention (`i`); `?`-recallable.
- Mode-aware `?`: HelpScreen exists; surface the active mode's keys first.
- show=False audit: promote a curated set so the footer teaches; keep the long tail in `?`/palette.

Cheap — footer hints ride P29-I01; the tour is optional polish.

## H. Visual-rich — ALL bars become Textual ProgressBar; braille = spinner only

Operator directive (2026-05-30, supersedes the earlier right-tool-per-context split): switch ALL progress/ratio bars to the Textual `ProgressBar` look; retask braille to a spinner only (indeterminate activity, "if any found"). The braille bars only redraw on state-refresh, so continuous metrics look frozen — ProgressBar's gradient + live `update()` is the fix everywhere.

Textual `ProgressBar` (textual.textualize.io/widgets/progress_bar): three toggleable sub-widgets (`#bar`/`#percentage`/`#eta`), `update(total=, progress=, advance=)`, `gradient` for smooth fill, `total=None` -> indeterminate pulse.

Every determinate bar migrates: roadmap completion (closed/total), workspace + cockpit token-burn (tokens/cap), EU burn (consumed/expected), verify (N/M criteria). The effort-size magnitude indicator renders as a ProgressBar-style magnitude bar (total=XL-rank, progress=bucket-rank).

Widget-vs-renderable constraint (verified, load-bearing for the build): a real `ProgressBar` is a compound Widget that mounts Bar + Label sub-widgets; Textual `DataTable` cells and `Tree` node labels accept Rich renderables, NOT mountable widgets. So the migration takes two forms, both visually identical:

- Single-focus contexts where a widget can mount (cockpit selected-wave detail strip, audit-running modal, research synthesis line) -> the ACTUAL `ProgressBar` widget (`gradient` + `show_eta`).
- Dense cells (roadmap tree nodes, workspace / backlog / cockpit table rows) -> a ProgressBar-EQUIVALENT block-gradient Rich renderable (same Unicode block glyphs `block-eighths` + the same gradient as ProgressBar's Bar), driven live off `dispatch_cost` so it animates per dispatch. No per-row widget mount (DataTable/Tree forbid it).

Braille is removed from all bars and retasked to ONE role: an indeterminate spinner glyph (rotating `braille-spinner` e.g. the 8-frame ⠋⠙⠹⠸⠼⠴⠦⠧ cycle) for "agent working, no ETA / unknowable duration" — the honest indeterminate state. (Textual ProgressBar's own indeterminate pulse is the built-in alternative; per the directive braille is reserved for this spinner.)

```
  W09 > pgid-kill ladder - IN_PROGRESS
   burn   |############|  92%  92k/100k    <- ProgressBar widget, gradient ok->warn->err
   verify |######|        4/6  ETA 2m       <- ProgressBar widget, total=6, show_eta
   spawn  (spinner) working...              <- braille spinner (indeterminate, no total)
```

Reconcile `src/eawf/surfaces/tui/widgets/eu_bar.py` (`render_bar_plain`/`render_completion_bar`/`render_size_bar`/`render_bar_rich`): replace the braille glyph fill with the block-gradient ProgressBar-equivalent renderer for ALL determinate bars; add a `render_spinner` braille helper for the indeterminate state. The ASCII fallback for low-capability terminals rides the existing `ui.glyphs=ascii` (block glyphs -> `#`/`-`); no separate `ui.progress_bars` key needed. All mockups in this brief illustrate the ProgressBar block style.

## I. UX scenarios (interactive walkthroughs)

The interactivity lives in the checkpoints, not just the watching.

Scenario A — run a research campaign end to end (`/research` mode): `3` -> research mode (idle = past-briefs list); `n` new -> depth AUQ. clarify (interactive: 1-3 scoping needs_user AUQs answered inline). decompose (interactive: `e` edits/adds/drops topics before dispatch). dispatch (watch + intervene: fleet-table, live ProgressBar burn, `enter` peeks a topic's partial findings, `H` halts a runaway topic). synthesize (live counter "12 claims, 8 evidence_refs resolved, 2 dup merged"; needs_user AUQ on a fork). blitz (guarded residual auto-chain). done -> `enter` read, `p` promote (only if it informs a Decision -> artifact-chassis + scrub gate).

Scenario B — supervise a spawn frontier (autopilot mode, highest interactivity): `2` -> cockpit; attention-sort floats over-budget + needs-you to top. Answer a blocking question (`enter` on the `q`-flagged wave -> needs_user AUQ -> resumes). Budget decision (`enter` -> detail shows cap% + verify N/M -> `H` halt with a confirm modal spelling the SIGTERM->grace->SIGKILL ladder / raise cap in config / let run). Repair a failure (wave flips fail -> toast + inbox -> `r` re-dispatch / `S` skip / `o` open the failed criterion in evidence mode). Watch burn (per-wave ProgressBars off `dispatch_cost`; aggregate burn vs cap). Pre-spawn (today -> I02): keys bound but confirm -> `SPAWN_UNAVAILABLE` toast; banner points to `eawf wave autoland`.

Scenario C — morning triage (attention-first): `eawf` -> badge `q2 warn1 fail1` -> `i` inbox (prioritized) -> `enter` each (resolve / decide budget / open audit-fail in evidence) -> clear -> `1` home.

Scenario D — tune preferences: `c` -> config -> `ui` tab -> set `default_mode=autopilot`, `needs_user_autopopup=blocking`, `progress_bars=animated` -> `s` save (layer global).

## J. Config pane — high-configurability gaps

Verified: 43 operator keys across 11 tabs; `ui.*` = exactly 7 (`src/eawf/kernel/config/registry/config_keys.py:100-487`). Two-registry system: CONFIG_REGISTRY (operator-facing) vs LEAF_KEY_REGISTRY (~150 internal, `kernel/config/.../leaf_catalog.py`). To expose a key = add one `ConfigKey{tab,key,label,type,default,choices,min,max,multiline}` (`config_keys.py:42-85`).

New `ui.*` prefs the chassis/design introduce (absent today): `ui.default_mode` (home/autopilot/research/trust/doctor, default home), `ui.default_scope` (repo/workspace/user, default repo), `ui.needs_user_autopopup` (off/blocking/all, default blocking — section C), `ui.confirm_destructive` (always/spawn-only/never, default always — cockpit halt/kill gating), `ui.attention_badge` (bool, default true), `ui.backlog_default_sort` (priority/id/status, default priority — issue b), `ui.backlog_default_filter` (open/active/all, default active — gap-brief Q6), `ui.relative_time` (bool, default true), `ui.density` (compact/comfortable, default comfortable).

`ui.dashboard_panes` choices are STALE (list hypotheses/audits/ship/memory/config — several become modes/peeks under the chassis). Reconcile: `ui.dashboard_panes` governs the HOME 2x2 tiles only; modes are a separate axis.

High-value INTERNAL keys to PROMOTE to operator-facing for high configurability: `flow.budget.per_wave_tokens` + `flow.budget.enforce` (soft/hard/off) + warn/act thresholds (the cockpit budget knob, `leaf_catalog.py:859-882`); `flow.auto_accept.<stage>` (research/prep/audit/ship/review/polish — the core autonomy dial); `trust.tier_thresholds` (D-TUI-02 autonomy gates); `acceptance.commands.*` (per-repo test/lint/typecheck/build); `runtime.adapter_catalog.<adapter>.enabled` (per-adapter enable). `planning.max_parallel_waves` (spawn fan-out cap) is ALREADY exposed.

Config drift to fix in P29: `research.default_depth` choices = `shallow|normal|deep` (`config_keys.py:268`) but the ratified vocab is `shallow|medium|deep|exhaustive` (synthesis decision 10) — fold into the depth-vocab migration.

## K. Minor TUI fixes (root-caused, verified)

a. Roadmap scroll/expand reset on refresh — CONFIRMED. `_rebuild` does `self.root.remove_children()` then repopulates and only calls `_scroll_to_active_phase`; `cursor_line`/`scroll_offset`/expanded-node set are all lost; user-expanded iters/waves silently re-collapse (`src/eawf/surfaces/tui/widgets/roadmap_tree.py:537-575,576-596`; refresh is in-place via `state_binding.py:325-335` + `scopes/repo.py:72-88`, tree instance persists). Fix: snapshot `(cursor node data-id, scroll_y, set of expanded node ids)` before rebuild; restore after repopulate (re-expand saved set, restore cursor to same id with active-phase fallback, restore scroll_y); stop force-collapsing user-expanded nodes. Effort S.

b. Backlog sort "missing" — ACTUALLY EXISTS. `SORT_KEYS=("priority","id","status")`, `sort_items`, `cycle_sort`, `sort_key` reactive + `watch_sort_key -> _rebuild`, and `/sort backlog` calls `cycle_sort()` end-to-end (`backlog_table.py:49,145-193,313,353-355,383-385`; `palette/verbs.py:_handle_sort`). The defect is DISCOVERABILITY: no table key binding, no sort glyph in the header, no column-header click. Fix: bind a table key to `cycle_sort()`; render the active sort as a header glyph (`id v`); add `ui.backlog_default_sort` to persist; optional column-header click. Effort XS-S.

c. `priority` column too wide — CONFIRMED. Header label `"priority"` (8 chars) exceeds the cell values `P0`-`P3` (2 chars) and drives `_fixed_columns_width` via `max(len("priority"), ...)` (`backlog_table.py:69,248,441`). Fix: `add_column(label="pri", key="priority")` (or "P") -> width 3; audit other wide headers. Effort XS.

d. Multichoice repeats field+type on every option row — CONFIRMED + root cause. `MultichoiceChecklist._repaint` prepends `self._prefix` to EVERY option line (`multichoice_checklist.py:131-138`); the prefix = `_meta_line(entry)` = `   {key:<width} {[type]:<14} ` (`config_modal.py:776,629-647`), added so option rows align with an inline editor's column. Result: `ui.dashboard_panes [multichoice]` repeats per row. Fix: render `_meta_line` ONCE as a header row above the checklist; option lines drop the prefix and indent-align under the value column (caret + `[X]` + choice). Effort S.

e. Breadcrumb stops at phase + is inert (issue 3e) — CONFIRMED. `build_breadcrumb` renders `scope > code > phase` only (`src/eawf/surfaces/tui/widgets/header.py:62-90`); no iter / mode / drill segment; the runtime cell is a stub (`idle`/`active`, `runtime_cell_text:93-110`); the header is a `Static` with no clickable segments. Make-useful design: (1) extend to the full location `scope > code > phase > iter > mode` (+ `> <entity>` when a peek/detail is open) so the breadcrumb always answers "where am I"; (2) make each segment a clickable nav target via Textual `[@click=...]` content-markup on the Static — scope switches scope, code -> home, phase -> roadmap@phase, iter -> roadmap filtered to that iter, mode -> mode switch; (3) replace the idle/active runtime stub with the real runtime id + running-wave count (e.g. `claude` + the run badge); keep the UTC clock. Threads `iter`/`mode`/`drill` params through `build_breadcrumb` (today takes `state, scope`). Effort S-M.

## L. Git pane -> VCS pane (too empty)

Today (`src/eawf/surfaces/tui/widgets/git_pane.py:146-166`): branch / dirty-count / ahead-behind / 3 recent commit subjects — all 1s-cached git shell-outs (`_git_run`), no PR/CI, not interactive; a `Static` widget. The `PrListModal` (`screens/overlays/pr_list.py`, `/pr` verb) already does worker-offloaded `gh pr list --json` with a 60s TTL + graceful-unavailable.

Extend to a VCS pane: add a PR line `gh pr list --head <branch> --json number,state,statusCheckRollup` (worker, 60s TTL cache, `gh?` on failure) rendering `PR: #27 open - checks 5/6 ok - 1 running`; make it interactive — `p` -> `gh pr view --web` (current branch's PR), `P` -> the existing `PrListModal`; keep the branch/dirty/ahead lines on the fast 1s cache, PR/CI on the slower lazy 60s cache. Effort M.

## P29 iter placement (chassis settled now -> built once)

Spawn-free (ride I01):
- MODES chassis refactor + digit accelerators + palette mode-verbs + modal demotion (A).
- Attention badge cluster + `i` unified inbox + `ui.needs_user_autopopup` blocking-only auto-open (C).
- Trust mode (static) + EU/metrics light-up on capture-forward (D); Doctor mode (events + health + drift, read-only).
- ProgressBar migration: all determinate bars -> ProgressBar widget / block-gradient renderable, braille -> spinner; `eu_bar` reconcile (H).
- Onboarding: adaptive footer mode-hints + `show=False` curation (G).
- Breadcrumb: full-location segments + clickable nav + real runtime cell (K-e).
- Minor fixes: roadmap scroll/expand preservation (K-a), backlog sort affordance + header glyph (K-b), `priority` -> `pri` header (K-c), multichoice field-once render (K-d).
- Config: new `ui.*` prefs + promote `flow.budget` / `flow.auto_accept` / `trust.tier_thresholds` / `acceptance.commands` to operator-facing + `research.default_depth` vocab fix + `ui.dashboard_panes` choices reconcile (J).
- Git -> VCS pane: PR line + `gh pr view --web` + PrListModal hook (L).
- Research mode SHELL + plan-only runner + depth-vocab + `ResearchCampaign` models behind a flag (per the deep-research-cockpit brief, E).

Spawn-gated (ride I03):
- Autopilot cockpit live + halt/skip/kill/arm RPCs + `SPAWN_UNAVAILABLE` pre-gate (B).
- Research mode live dispatch + synthesizer + multi-round follow-up + research-to-roadmap bridge (E).

v0.5:
- Evidence mode (why-peek / readiness / ledger / spec, F) + cross-repo dashboard (v05-multirepo brief).

The entire spawn-free half is large but independently shippable — if the I02 safety floor or I03 spawn slips, I01 still lands the chassis + attention + light-up + visual refresh + breadcrumb + the four fixes + config + VCS pane + the research shell.

## Drafted Decision (apply at /roadmap propose or via the daemon — NOT mutated by this read-only brief)

```
D-TUI-MODES — TUI adopts a Textual MODES chassis (k9s-style)

Verdict: Heavy dwell-on TUI surfaces become full-screen Textual MODES reached
by a palette verb + a collision-free digit accelerator (1 home / 2 autopilot /
3 research / 4 trust / 5 doctor / 6 evidence). The 2x2 stays the "home" mode.
Modals demote to transient only (confirm / form / quick-peek incl. the why-peek).
SCOPE (w/r/u) and MODE become orthogonal axes; scope is the data source, mode is
the view. Settled before P29 so all P29 TUI surfaces are built on the final
pattern. Attention = always-on footer badge cluster + `i` pull-inbox +
blocking-only auto-open (no permanent rail). EU/trust widgets render honest
empty/accruing/lit states (never green-zero). ALL determinate bars use a
Textual ProgressBar widget or an equivalent block-gradient renderable; braille
is retasked to an indeterminate spinner only.

Supersedes/extends: the v04-tui-contracts T1-T17 surfaces are now placed on the
MODES chassis (T15 wave_board = the autopilot mode; T1 why = a peek modal, not a
pane; T2 trust + metrics-modal = the trust mode; T8 + events = the doctor mode;
T3/T4/T5 = the v0.5 evidence mode).

Evidence: .ea/local/research/2026-05-30-tui-chassis.md (this brief).
```

This brief promotes from `.ea/local/research/` to `.ea/artifacts/research/` in the same commit that lands the `D-TUI-MODES` Decision row (artifact-chassis validator + scrub gate apply on promotion).

## Open items / pre-claim spikes

- Mode 2 name = `autopilot` (locked 2026-05-30) — `/autopilot` + digit 2.
- Research-mode layout: RECONCILED (no longer open) — the research mode = the `ResearchOrchestratorScreen` in `.ea/local/research/research-orchestration/2026-05-29-cockpit.md`, mounted as mode 3, reusing the autopilot fleet-table widget for its dispatch-round task list.
- Textual `MODES` migration spike: confirm scope-as-data-source + mode-as-screen-stack composes cleanly with the existing `_zoom` mixin and the degraded-mode banner (the zoom-teardown bug T13 must land first, since modes push/suspend like the modal-from-zoom path that T13 fixes).
- ProgressBar density spike: confirm the block-gradient Rich renderable (NOT a mounted `ProgressBar` widget — DataTable/Tree cells forbid widgets) animates off `dispatch_cost` at frontier-width N without thrashing the render thread; ASCII fallback rides `ui.glyphs=ascii`.
- Halt/skip/kill/arm RPC contract is net-new in I03 — design alongside `open_session` forking + the budget SIGTERM-ladder, not before.

## Finalization status

FINAL — mode-2 name = `autopilot` (locked 2026-05-30); no open items. Sections A-L cover the full TUI UI/UX scope for P29+: chassis (A), autopilot cockpit (B), attention (C), EU/trust light-up (D), research mode (E, reconciled to the cockpit brief), evidence mode (F, v0.5), onboarding (G), ProgressBar/visual (H), UX scenarios (I), config preferences (J), the four minor fixes + breadcrumb (K), VCS pane (L). Every behavioural claim is verified against source at the cited file:line. Read-only: no state mutated; `D-TUI-MODES` is drafted for `/roadmap propose` to apply, and the brief promotes to `.ea/artifacts/research/` on that ratify.

## Folded surfaces (textual-enrichment · attention-routing · v0.5 multi-repo · operator review)

The four TUI briefs the 2026-05-31 distill folds into this hub. Sections §A–§L above are the canonical MODES-chassis contract; the below preserves each folded brief's load-bearing decisions + dispatch waves. Originals recoverable under `archive/2026-05-31-distill/`.

### Textual widget enrichment (folds `tui-textual-enrichment`)

The TUI reaches for only 9 of Textual's widget classes; the enrichment gap is lopsided — **rich INPUT is the real hole** (every config/operator surface is single-line `Input`, which blocks the [control-plane](research-orchestration/2026-05-31-control-plane.md) D-2/D-3 operator-input channel). 16 proposals, 4 clusters, operator-ratified (`--final`): **A output** (`Sparkline` trend / `Digits` hero metrics / `Pretty` typed-entity / `RichLog` console — the event-tail use is spawn-free, the live agent stream is I03); **B input** (`Switch`/`RadioSet`/`Select`/`SelectionList`/`TextArea` — `RadioSet` serves D-2 autonomy tiers, `TextArea` is the prerequisite for the D-3 override-note); **C tools** (`Input(suggester)` palette type-ahead, native `CommandProvider` [spike-first — the one real fork], clickable breadcrumb/links via `@click` = §3/§L build, `Collapsible` release bands = the roadmap-render-cleanup fold); **D clarity** (glyph/bar tooltips → a doc-clarity win, `LoadingIndicator` on async cells). 14 of 16 are spawn-free → P29-I01 TUI half, additive to the §H/§K waves. Dispatch plan: **W-IN** rich-input substrate (L), **W-CLR** clarity affordances (M), **W-OUT** output niches (L), **W-RAT** ratified-unbuilt §H/§3/§L/Collapsible (L), + a CommandProvider spike (XS). YAGNI: `MaskedInput`, `ListView`; don't rip out braille beyond §H (it stays the spinner/sparkline glyph).

### Attention routing (folds `operator-attention-routing`)

**One feed, two renderers.** A single pure reducer `build_attention_feed(states: list[State]) -> list[AttentionItem]` in `surfaces/render/`, rendered by both the TUI `i` inbox AND a new headless surface — operator decision: **`eawf status` shows focus by default** (no new command/flag; the feed leads the status render). Decisions (3 AUQ rounds): **blocking-only interrupt** (only `urgency=blocking` auto-opens; rest = badge + pull); **pure projection, NO ack/snooze/dismiss** (items clear only when the underlying state resolves — k9s/Azure model; snooze deferred); **group by the existing workspace-scope** (NOT a new entity — `eawf workspace`/`WorkspaceIndex` already = a named repo group with its own state.json); **dead sources ship-the-slot** (`AUDIT_FAIL` re-targets to audit verdict=major since `AuditStatus.FAILED` is a dead enum; `BUDGET_HALT` ships dark — `classify_enforcement` has zero callers). 8 `AttentionSource`s, 6 live (4 direct + 2 derived: frontier-empty, ready-to-close) + 2 needing work. **Prerequisite wave:** `UserQuestion` has no `urgency` field today — add `urgency: blocking|warn|info` (shared with the control-plane). `eawf status` back-compat: flip the exact-key test (`test_cli_status.py:246`) to `issubset` + a 12th `attention` key. ~6-wave breakdown; extends §C of this hub.

### v0.5 multi-repo workspace (folds `v05-multirepo-workspace-uiux`)

**B011 = SPLIT** (reverses the P29-sweep cancel): read-only cross-repo dashboard + inbox + advisory PR view → **v0.5** (post-P29); cross-repo write/merge + multi-operator → **v0.6**. The read surface is ~80% already shipped (`WorkspaceTable` fans per-repo `state.json` reads per registry entry). Decisions: cross-repo inbox = **pull-only badge + `i`** (never auto-opens across repos); answer-routing = **in-place routed resolve** (a `DaemonClient` per repo socket, portalocker fallback, confirm-on-resolve — pre-claim spike required); EU/reactive columns ship **latent, honest empty-state** until P29-T9 + Iter.trigger land. ~14-18 read-only waves across 4 iters; the only write is a single targeted daemon-routed needs_user resolve. Sequences AFTER P29 prerequisites (T13 zoom-fix, T12 inbox+badge, T9 EU capture, Iter.trigger); independent of the v0.6 merge. Reuses the P29-T13-fixed zoom + the shipped `PrListModal` pattern (`gh pr list`, read-only, no merge action).

### Operator review — TUI/PR gaps (folds `user-review-tui-pr-gaps`, Q1–Q11)

The v0.4.0-ship review. Five TUI-fix clusters + systemic gaps, all folding into P29: **TUI-fixes** (Q1 Enter routing on phase/iter rows — emit `PhaseSelected`/`IterSelected`; Q2 modal-stack cap 3→10; Q3 dedup guard on `action_open_ref`; Q6 backlog open/active/all 3-state filter); **TUI-rich** (Q4 symbol catalog `B###`/`ACT-` + narrative linkify; Q7 bullet-aware `Markdown`-vs-`Static` widget swap); **TUI-consumer** (Q8 latent gaps — the systematic miss is that the verify/trust/metrics computation cores landed but their TUI panes + overlays + `event.subscribe` wiring did not: WhyBundle T1, TrustScorecard T2, CloseReadiness T3, evidence T4, doctor T8, wave_board T15/T16; **correction: `MetricsProjection` DOES exist** at `metrics_projection.py:123` — the Q8 "missing" claim is wrong, see [p29-scope](2026-05-29-p29-scope.md) T9); **data-backfill** (Q5 — ~40/71 backlog titles are 72-char truncated substrings; one-shot `backfill_backlog_titles.py` + operator-approval loop); **ship-process** (Q9 GitHub `mergeStateStatus=UNKNOWN` is a UI cache not a real conflict → `allow_update_branch=true` applied; **Q10** phase-close must be the branch tip — v0.4.0 violated it with 2 post-close commits, breaking auto-tag annotation extraction; **Q11** `gh pr merge --rebase` has a ~200-250-commit soft limit — PR #26 (227 commits) failed `rebaseable:false` and shipped via direct ff-push; document the fallback + cancel the `--auto` recommendation).
## References

- `.ea/local/research/2026-05-30-tui-chassis.md` Q1-Q11 (modal-stack cap, Enter routing, the T1-T17 latent-surface catalog).
- `.ea/local/research/2026-05-29-p29-scope.md` (P29 DAG, T9/T11 EU-dark root cause, T12 needs_user badge+inbox, T13 zoom-teardown, decisions 2/3/11/15).
- `.ea/local/research/archive/2026-05-31-distill/2026-05-26-v04-tui-contracts.md` (T1-T17 surface registry, D-TUI-01..09, scorecard + WhyBundle shapes, KEY_s export).
- `.ea/local/research/2026-05-30-tui-chassis.md` (the cross-repo dashboard that the evidence mode + home-workspace-scope sit beside).
- `.ea/local/research/research-orchestration/2026-05-29-cockpit.md` (the canonical research-mode screen = mode 3; this brief places it on the chassis + shares the cockpit fleet-table widget).
- `src/eawf/surfaces/tui/app.py:371,649-687,758-794` (push/switch_screen, scope switch, push_modal); `surfaces/tui/widgets/roadmap_tree.py:537-596` (rebuild/scroll-reset); `surfaces/tui/widgets/eu_bar.py` (bar renderers to migrate).
- `src/eawf/kernel/state/models.py:310-325,465` (Wave fields, attention_eu); `kernel/store/kinds/event.py:62` (closed EventKind); `runtime/budget/policy.py:62` (HALT); `workflow/skills/flow/__init__.py:602,644` (flow events).
- `src/eawf/kernel/config/registry/config_keys.py:42-87,100-487` (CONFIG_REGISTRY + ConfigKey shape; ui.* = 7 keys); `surfaces/tui/widgets/header.py:62-139` (breadcrumb + runtime cell); `surfaces/tui/widgets/git_pane.py:146-166` (git pane fields); `surfaces/tui/widgets/backlog_table.py:49,69,248,383` (sort wired + `priority` header width); `surfaces/tui/screens/overlays/multichoice_checklist.py:131-138` + `config_modal.py:629-647,776` (multichoice per-row prefix); `surfaces/tui/screens/overlays/pr_list.py` (PrListModal pattern for the VCS pane).
- Textual ProgressBar — https://textual.textualize.io/widgets/progress_bar ; Textual MODES + screen-scoped COMMANDS — https://textual.textualize.io/guide/screens/ , https://textual.textualize.io/guide/command_palette/
- Agent-orchestration convergence (2025-2026): Cursor Conductor, OpenAI Codex "command center", Claude Squad, agtx — cited in Round 1 web pass.
