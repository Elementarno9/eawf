# v0.7 finalization: ratified decisions, repaired roadmap, next command

> Status: research brief · Scope: the v0.7.0 proposal packet, the ten spikes under `.ea/local/spikes/`, the 2026-08-25 rules-delivery design, and the live roadmap · Baseline at start: checkout `ae04a5c1`, package `0.6.8`, state schema `1.19`, P31 ACTIVE with no iter open · Baseline at end: checkout `05f0394a` · Date: 2026-09-03

## Summary

The v0.7.0 packet was consistent as a specification but not executable as a plan: no P31 wave could close (78 criteria referenced gate ids that resolved to no `GateSpec`, and the daemon rejects that unconditionally), dev1 ended at APPROVED while dev2 opens only from BAKED, the effort ladder had no discriminating power, and none of the 2026-08-28 and 2026-09-03 spike findings or the 2026-08-25 rules-delivery design had reached the packet [1][2][3]. Eight read-only researchers audited the packet against the spikes and the design, verified every load-bearing claim at HEAD, designed the missing seam, drafted the dev2 plan and the dev3-to-stable outlines, and produced a decision register [4][5][6][7][8][9][10][11]. Four AskUserQuestion rounds then ratified sixteen decisions, every one on the long-term-correct option [12].

The roadmap is repaired and the specifications are finalized. P31 carries 38 waves with 130 typed gates and zero dangling references (26 original, 8 seam, 4 repair; W36 closed against `231656bf`); P32 (0.7.0.dev2) is PLANNED and applied with 22 waves and 71 gates; P33 to P37 are PLANNED shells with their outlines attached; decisions D34 to D48 are recorded; the packet carries 1,168 requirement identifiers with the spike, rules and hygiene amendments folded in under AUTH-039 to AUTH-058 [13][14][15]. The next command is `/flow`, whose `/prep P31` step is a no-op on the already-active phase and proceeds to claims.

## What was wrong, verified at HEAD

| Finding | Evidence | Disposition |
|---|---|---|
| No P31 wave could close: `_validate_wave_close_gate_refs` runs on every `WAVE_CLOSE` regardless of `verify.enforce`; 78 of 80 criteria carried `gate_ids` resolving to no gate; the blanket `criteria_floor_waiver` tested reference presence, never resolution | `src/eawf/runtime/daemon/methods/state.py` validator chain; `src/eawf/workflow/lifecycle/_errors.py` floor; state census 78 dangling refs [4][6] | Repaired by spec sync (D34, D38); floor tightening scheduled as P31-W37 |
| Eight criteria needed an `eawf` or `just` argv head the L0 allowlist rejected at `GateSpec` construction | `src/eawf/kernel/spec/promotion.py` allowlist; `src/eawf/kernel/spec/common.py` validator [6] | Widened as P31-W36 (`231656bf`), interim until the epoch-2 command-family contract (D34) |
| dev1 ended at APPROVED; the train opens dev2 from BAKED; `ReleaseTrain` and `ReleaseReadiness` absent; ten publication tokens in zero waves; the 8-gate profile had no function to the 12-signal table; three gates had no producer; `phase-release.yaml`'s tag regex never matched `v0.7.0.dev1` | seam spike; `13-seam-waves-notes.md` [3][7] | Eight seam waves W27 to W31 and W33 to W35 appended to P31; publish-to-BAKED is P32-I01-W01 post-merge (D35, D37, D40) |
| The XS..XL ladder spans 14x declared against 1.3x measured, non-monotonic, rho +0.048; fifteen size proxies separate nothing; MEAS-021's eight-times figure was the open-to-close clock | effort-size-proxy and measurement-reality spikes [3][5] | Ladder retired for a flat 0.8 EU per wave with dispersion (D36) |
| 0 of 36 measurement producers exist; only 28 of 849 actuals carry a real price; codex sums reasoning tokens into output against MEAS-055 | measurement-reality spike; `src/eawf/observability/telemetry/sources/codex_session.py` [5][6] | W17 split into W17 and W34; W18 declares its 28-row sample and freezes threshold 0.20 (D41) |
| The packet's steering surface (one committed root carrying every unscoped `must` rule) is incompatible with the ratified single generated policy projection; zero of ten measured repairs applied; `agents.extra_tools` shipped empty so the next plugin install strips Serena from six roles | rules-harness audit [8] | Packet amendments A1 to A15 applied; repair wave P31-W38 (D46) |
| The shipped TUI's pane focus ring is invisible on all three themes (`$primary == $accent`) and `$muted` sits at 2.47:1 across 13,037 cells; the design pack's `stage.css` is an unbound colour oracle | colour-conformance spike; `src/eawf/surfaces/tui/theme.py` [6] | Oracle bound at dev3 (P33); XS fix now as P31-W39 (D47) |
| dev3 to stable existed as prose; rc2 had no `REL` row; `PRX-050`/`PRX-064` circular; `LINT-038` reds on every merge; `REL-031` and `REL-032` name different windows | dev4-to-stable outline; decision register [10][11] | Shells P33 to P37 proposed; eleven hygiene fixes and seven defaults applied (D48) |

## The ratified decisions

| Decision | Ruling | Round |
|---|---|---|
| D34 | `eawf` and `just` admitted as gate argv heads (interim); the plan-time criteria floor must require every `gate_ids` entry to resolve | 1 |
| D35 | Eight seam waves join P31-I01 before the test-tree collapse; publish-to-BAKED opens P32 post-merge | 1 |
| D36 | Effort ladder retired: one constant of 0.8 EU per wave with dispersion; bucket labels are narrative only; no re-fit before 100 waves carry positive elapsed effort | 1 |
| D37 | All three producerless dev1 gates get producers at dev1 (W30); `security_review` is the supply-chain vulnerability component of the `dependencies` row | 1 |
| D38 | Minted gates use `scope: all` and `timeout_class: quick`; a repetition count is one looping test; residual halves live inside the named test; CI-only halves are cadence-ship gates | 2 |
| D39 | REL-025 "explained" is the waiver event itself; `waiver_count` is a readiness field; a non-zero count blocks APPROVED until acknowledged | 2 |
| D40 | A declared function binds each dev1 gate name to a readiness row, a row component or a proof command; required signals derive from `gates.required` plus the existing tree, ancestry and credential flags; `platform` computes at dev1 | 2 |
| D41 | Turn-cost producer split (W17 record and provenance, W34 CLI and check); dev1 baseline over the 28 priced rows at threshold 0.20; codex rows excluded until MEAS-055 is settled | 2 |
| D42 | REL-027: compact native tree with a separate legacy store; a legacy reference is a string on a native envelope, a legacy record an envelope in the legacy ledger | 3 |
| D43 | DOM-018 is an enumerated deletion list bound to the DOM-015 allowlist; `archived` maps to CANCELLED; `priority` and `intent` take annotated defaults | 3 |
| D44 | The workspace registry ships before importer apply mode; the packet's workspace verbs are canonical; the largest supported state is the live corpus frozen at band `thousands` | 3 |
| D45 | P32 proposed now with 22 waves; dev2 gate profile is the dev1 eight plus `migration`, `hosted_gate_runner`, `schema_strictness`, `waiver_count`; REL-037 legs importer/dev2, cross-provider/dev3, daemon-RPC/dev3 | 3 |
| D46 | The single generated policy projection design is adopted with packet amendments A1 to A15; the steering-surface migration rides dev4; P31-W38 repairs the symbol-tool plumbing now | 4 |
| D47 | The `stage.css` chrome is the colour oracle bound at dev3 with every console route; P31-W39 fixes the focus ring and muted contrast now | 4 |
| D48 | P33 to P37 proposed as PLANNED shells; eleven hygiene fixes and seven defaults applied to the packet | 4 |

Also ratified in round 4: the allowlist repair rides a dedicated wave (W36) rather than a widened commit lint, and the floor tightening is its own wave (W37).

## What changed in the repository

| Commit | Subject | Content |
|---|---|---|
| `e82b6d99` | `[P31] state: activate iter P31-I01 and record decisions D34-D48` | iter activation; fourteen decisions |
| `684566f5` | `[P31] state: append seam and repair waves W27-W39 and claim W36` | eight seam waves, four repair waves, W17/W18/W26 rewrites, W25/W26 dependency lists, W19 scope append |
| `231656bf` | `[P31-I01-W36] fix: admit eawf and just as gate argv heads` | `DEFAULT_GATE_ARGV_ALLOWLIST` plus five pinning tests; a pre-existing pyright invariance defect fixed at the call site |
| `192c755a` | `[P31] state: sync typed gates onto 38 waves and close W36` | 130 gate rows synced; W36 closed under an explained missing-runtime waiver with its integration revision adopted |
| `05f0394a` | `[P31] state: propose and apply P32 and stage the P33-P37 shells` | P32 with 71 synced gates; D45; five shells |

Spec bodies are not committed (D27 deprecated committed per-wave specs); they live under the gitignored finalization directory and were fed to `eawf spec sync` through `--spec-path` [13]. The plan file `2026-08-25-p31-dev1-roadmap-plan.yaml` in the packet directory is superseded by the live state; `eawf roadmap show --phase P31 --md` is authoritative.

## The roadmap after this pass

| Phase | Status | Waves | Gates | Checkpoint |
|---|---|---:|---:|---|
| P31 | active (iter P31-I01 active; W36 closed, 37 pending) | 38 | 130 | 0.7.0.dev1 to APPROVED |
| P32 | planned, applied | 22 | 71 | 0.7.0.dev2 to BAKED (W01 bakes dev1 post-merge) |
| P33 | planned shell | 0 | 0 | 0.7.0.dev3 native canary + console |
| P34 | planned shell | 0 | 0 | 0.7.0.dev4 planning + steering surfaces |
| P35 | planned shell | 0 | 0 | 0.7.0rc1 flag day |
| P36 | planned shell | 0 | 0 | 0.7.0rc2 evidence |
| P37 | planned shell | 0 | 0 | 0.7.0 stable |

P31 at the flat rate is 30.4 EU over 38 waves (47.75 EU on the retired ladder, quoted only for comparison); its critical path is eight waves. P32 is 17.6 EU flat over 22 waves with a twelve-wave critical path. The outlines for P33 to P37 live in the finalization directory [10][11].

## Next command

`/flow` — its `/research` step may cite this brief; its `/prep P31` step is an idempotent no-op on the ACTIVE phase and proceeds to claims. Before the first claim batch run `uv run eawf dispatch resume`; claim reactive or parallel-frontier waves with `--out-of-order` (W33 to W35 are numbered above W30 while earlier in the DAG). W37 to W39 are dispatchable on day one; W36 is closed.

## Known limits

- W36 was closed under a `--no-runtime` waiver because its executor ran inline with no captured runtime, so the close recorded 0 of 2 gate receipts; both gate commands were run directly afterwards and pass (7 and 4 tests). The waiver is counted and explained on the dev1 readiness receipt per D39.
- Two seam gates may exceed the 60 s `quick` class at close (W30 double clean build, W26 preflight reruns); a `slow` override at sync is the remedy if they time out.
- The measurement spike's MEAS-055 question (codex reasoning tokens summed into output) is settled only against a vendor rollout outside this repository; W17 marks codex rows and W18 excludes them until then.
- Two agents read `security_review` differently (existing skill vs supply-chain component); the supply-chain reading was ratified (D37) and the packet says so.
- The finalization directory holds the working artifacts of eight researchers and three executors; it is gitignored and is the provenance for every number in this brief.

## References

[1] `.ea/local/research/2026-09-02-v07-preflight-consistency.md` — the seven breaks, the eight decisions to sweep, the 2026-09-02 rulings.

[2] `.ea/local/research/2026-08-04-v07-proposal/README.md` and files `00` to `90` — the packet, amended 2026-09-03 (AUTH-039 to AUTH-058, REL-040 to REL-044, DOM-041/043/044/045, PLAN-051, DEL-034 to DEL-036, RULE-127 to RULE-131, SKILL-041/042, SURF-174).

[3] `.ea/local/spikes/2026-09-03-v07-spike-consolidated-report.md` — six spike verdicts and the eight decisions owed.

[4] `.ea/local/research/2026-09-03-v07-finalization/12-p31-closability-repair.md` — the spec sync mechanism proven on a copy; `12-gate-mint.py`.

[5] `.ea/local/research/2026-09-03-v07-finalization/11-spike-packet-amendment-audit.md` — 46 findings, 31 amendments, the 16-row binding table.

[6] `.ea/local/research/2026-09-03-v07-finalization/16-claim-verification-at-head.md` — C1 to C15 verdicts at `ae04a5c1`.

[7] `.ea/local/research/2026-09-03-v07-finalization/13-seam-waves-notes.md`, `13-seam-waves-final.yaml`, `13-seam-apply-commands.sh` — the seam design and its apply record.

[8] `.ea/local/research/2026-09-03-v07-finalization/10-rules-harness-amendment-audit.md` — amendments A1 to A15 and the status of the ten repairs.

[9] `.ea/local/research/2026-09-03-v07-finalization/14-p32-dev2-roadmap-plan-final.yaml`, `14-planning-notes.md` — the dev2 plan and its blocking decisions.

[10] `.ea/local/research/2026-09-03-v07-finalization/14-p33-dev3-outline.md` — the dev3 cluster outline and console decisions.

[11] `.ea/local/research/2026-09-03-v07-finalization/14-checkpoint-outline-p34-p37.md` — dev4 to stable outlines and eleven packet defects.

[12] `.ea/local/research/2026-09-03-v07-finalization/15-decision-register.md` — the 46-row decision register the AskUserQuestion rounds were built from.

[13] `.ea/local/research/2026-09-03-v07-finalization/specs/` and `minted/` — the spec bodies fed to `eawf spec sync`; `20-decisions.sh`, `21-shells.sh` — the recorded mutation scripts.

[14] `.ea/state.json` at `05f0394a` — phases P31 to P37, decisions D34 to D48.

[15] `.ea/local/research/2026-09-03-v07-finalization/13-repair-waves-w36-w39.yaml` — the four repair waves.

## Provenance

- kind: research
- slug: 2026-09-03-v07-finalization-brief
- method: eight read-only researcher and planner agents over disjoint scopes (Opus 5 and Fable 5.1), one docs-lookup agent, three executor agents (allowlist commit, two packet amendment passes), four AskUserQuestion rounds of four questions each, and direct daemon-backed state mutations by the lead
- state mutations: iter activation, twelve roadmap wave additions, three rewrites, three dependency-list edits, one scope append, 60 spec syncs, one claim, one integration adoption, one close, one phase proposal with apply, five shell proposals, fifteen decisions
- roadmap mutations: P31 26 to 38 waves; P32 proposed and applied; P33 to P37 proposed as shells

## Scrub

- status: clean
- references: repo-relative only
- credentials, PII, machine-specific paths, hostnames: none
